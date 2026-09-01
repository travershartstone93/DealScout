"""Parallel drivers for the two slow, independent-per-listing stages: gated enrichment (N headless
browser workers using imported Firefox cookies, avoiding the persistent-profile lock) and judging
(N concurrent `claude -p` subprocesses, each thread with its own SQLite connection)."""
import logging, queue, threading
from . import db, load_config
from .judge import set_status

log = logging.getLogger("dealscout")


# ---------------------------------------------------------------- enrichment workers
def enrich_parallel(limit: int = 400, workers: int = 3) -> dict:
    from .cookie_import import read_cookies
    from .flippa_auth import _capture_page, _parse_gated, _apply
    from .scan import row_to_listing
    from .score import evaluate
    cfg = load_config()
    con0 = db.connect()
    rows = con0.execute(
        "SELECT * FROM listings WHERE source='flippa' AND status='open' AND passes=1 AND hidden=0 "
        "AND json_extract(raw_json,'$.gated') IS NULL ORDER BY score DESC LIMIT ?", (limit,)).fetchall()
    cookies = read_cookies("flippa.com")
    q: queue.Queue = queue.Queue()
    for r in rows:
        q.put(dict(r))
    stats = {"enriched": 0, "failed": 0}
    lock = threading.Lock()

    def worker(wid: int):
        from playwright.sync_api import sync_playwright
        con = db.connect()
        with sync_playwright() as pw:
            browser = pw.firefox.launch(headless=True)
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            ctx.add_cookies(cookies)
            pg = ctx.new_page()
            while True:
                try:
                    r = q.get_nowait()
                except queue.Empty:
                    break
                l = row_to_listing(r)
                cap = None
                for attempt in (1, 2):
                    try:
                        cap, _ = _capture_page(pg, l.url)
                        break
                    except Exception as e:  # noqa: BLE001
                        log.warning("w%d %s (try %d): %s", wid, l.id, attempt, str(e)[:120])
                if cap is not None:
                    new = _apply(l, _parse_gated(cap))
                    db.upsert(con, new, evaluate(new, cfg))
                    con.commit()
                with lock:
                    stats["enriched" if cap is not None else "failed"] += 1
                    done = stats["enriched"] + stats["failed"]
                if done % 10 == 0:
                    print(f"enrich {done}/{len(rows)}", flush=True)
            ctx.close(); browser.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stats | {"total": len(rows)}


# ---------------------------------------------------------------- judge workers
def judge_parallel(limit: int = 500, workers: int = 4, force_ids: list[str] | None = None) -> dict:
    """First-pass judgments in parallel, then skeptic second passes in parallel."""
    from .judge import judge, second_opinion
    from .diligence import rules_preverdict
    from .scan import row_to_listing, _priority_sql, _scored_from_row
    cfg = load_config()
    con0 = db.connect()
    if force_ids:
        rows = [r for i in force_ids if (r := con0.execute("SELECT * FROM listings WHERE id=?", (i,)).fetchone())]
    else:
        rows = con0.execute(
            "SELECT l.* FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id "
            "WHERE l.passes=1 AND l.status='open' AND l.hidden=0 AND (j.listing_id IS NULL OR j.content_hash!=l.content_hash) "
            f"ORDER BY {_priority_sql(cfg)} LIMIT ?", (limit,)).fetchall()
    q: queue.Queue = queue.Queue()
    for r in rows:
        q.put(dict(r))
    stats = {"judged": 0, "rules": 0, "errors": 0}
    lock = threading.Lock()
    total = len(rows)

    def w_first():
        con = db.connect()
        while True:
            try:
                r = q.get_nowait()
            except queue.Empty:
                return
            l = row_to_listing(r)
            scored = _scored_from_row(r)
            try:
                pre = None if (force_ids or r["starred"]) else rules_preverdict(l, scored, cfg)
                if pre:
                    db.save_judgment(con, l.id, db.content_hash(l), "rules", pre["verdict"], pre, "")
                    con.execute("UPDATE judgments SET verdict1=? WHERE listing_id=?", (pre["verdict"], l.id)); con.commit()
                    k = "rules"
                else:
                    judge(con, l, scored, cfg, force=bool(force_ids))
                    k = "judged"
            except Exception as e:  # noqa: BLE001
                log.warning("judge %s: %s", l.id, e)
                k = "errors"
            with lock:
                stats[k] += 1
                done = stats["judged"] + stats["rules"] + stats["errors"]
            set_status(running=True, phase="first", done=done, total=total, current=l.id)
            print(f"  [{done}/{total}] {k:6s} {l.title[:60]}", flush=True)

    threads = [threading.Thread(target=w_first, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # skeptic pass over all fresh BUY/NEGOTIATE lacking a second opinion
    con0 = db.connect()
    rows2 = con0.execute(
        "SELECT l.* FROM listings l JOIN judgments j ON j.listing_id=l.id "
        "WHERE j.verdict1 IN ('BUY-CANDIDATE','NEGOTIATE') AND j.json2 IS NULL AND l.status='open' AND l.hidden=0").fetchall()
    q2: queue.Queue = queue.Queue()
    for r in rows2:
        q2.put(dict(r))
    stats["skeptic"] = 0
    t2 = len(rows2)

    def w_second():
        con = db.connect()
        while True:
            try:
                r = q2.get_nowait()
            except queue.Empty:
                return
            l = row_to_listing(r)
            try:
                second_opinion(con, l, _scored_from_row(r), cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("skeptic %s: %s", l.id, e)
            with lock:
                stats["skeptic"] += 1
                d2 = stats["skeptic"]
            set_status(running=True, phase="second", done=d2, total=t2, current=l.id)
            print(f"  skeptic [{d2}/{t2}] {l.title[:60]}", flush=True)

    threads = [threading.Thread(target=w_second, daemon=True) for _ in range(min(workers, 3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    set_status(running=False, phase=None, current=None, ids=[])
    return stats | {"skeptic_total": t2}


if __name__ == "__main__":
    import argparse, subprocess, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["enrich", "judge", "all"])
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--force-existing", action="store_true")
    a = ap.parse_args()
    if a.stage in ("enrich", "all"):
        print("ENRICH:", enrich_parallel(a.limit, min(a.workers, 3)), flush=True)
    if a.stage == "all":
        subprocess.run([sys.executable, "-m", "dealscout.scan", "rescore"], check=False)
    if a.stage in ("judge", "all"):
        if a.force_existing:
            con = db.connect()
            ids = [r[0] for r in con.execute(
                "SELECT j.listing_id FROM judgments j JOIN listings l ON l.id=j.listing_id "
                "WHERE l.status='open' AND l.hidden=0")]
            print(f"forcing {len(ids)} existing verdicts first", flush=True)
            print("FORCED:", judge_parallel(workers=a.workers, force_ids=ids), flush=True)
        print("JUDGE:", judge_parallel(a.limit, a.workers), flush=True)
