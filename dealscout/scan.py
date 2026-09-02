"""CLI: dealscout scan | judge | report | login | sources"""
import argparse, json, logging, sys, time, traceback
from datetime import date
from . import load_config, db, ROOT
from .models import Listing
from .score import evaluate
from .sources import ALL, BROWSER, get as get_source
from .sources.base import client
from .judge import set_status
from .diligence import watch_alerts, rules_preverdict

log = logging.getLogger("dealscout")
SCAN_STATUS = ROOT / "scan.status.json"


def scan_status(**kw):
    try:
        cur = json.loads(SCAN_STATUS.read_text()) if SCAN_STATUS.exists() else {}
    except json.JSONDecodeError:
        cur = {}
    cur.update(kw)
    SCAN_STATUS.write_text(json.dumps(cur))


def row_to_listing(r) -> Listing:
    raw = json.loads(r["raw_json"] or "{}")
    fields = {k: r[k] for k in Listing.__dataclass_fields__ if k != "raw" and k in r.keys()}
    fields["verified_revenue"] = bool(fields.get("verified_revenue"))
    fields["verified_traffic"] = bool(fields.get("verified_traffic"))
    return Listing(raw=raw, **fields)


ENRICH_FIELDS = ("customers", "users_free", "churn_pct", "reason_for_selling", "hours_per_week", "monetization",
                 "age_months", "verified_revenue", "verified_traffic")


def merge_enrichment(con, l: Listing) -> Listing:
    """A fresh API row lacks what the detail-page enrichment found; carry it over so it isn't lost/refetched."""
    r = con.execute("SELECT raw_json, summary, " + ", ".join(ENRICH_FIELDS) + " FROM listings WHERE id=?", (l.id,)).fetchone()
    if not r:
        return l
    raw = json.loads(r["raw_json"] or "{}")
    if not raw.get("enriched"):
        return l
    for f in ENRICH_FIELDS:
        cur, old = getattr(l, f), r[f]
        if (cur in (None, "", False)) and old not in (None, ""):
            setattr(l, f, bool(old) if f.startswith("verified") else old)
    if r["summary"] and len(r["summary"]) > len(l.summary or ""):
        l.summary = r["summary"]
    for k in ("page", "enriched", "enriched_hash", "enrich_error"):
        if k in raw:
            l.raw[k] = raw[k]
    return l


def cmd_scan(args):
    cfg = load_config()
    con = db.connect()
    db.backup()
    started = db.now()
    names = args.source or [n for n in ALL if cfg["sources"].get(n)]
    summary = {}
    scan_status(running=True, started=started, current=None, done=[], summary={})
    with client() as http:
        for name in names:
            t0 = time.time()
            seen, n_pass = set(), 0
            scan_status(current=name)
            try:
                fetch = get_source(name)
                for l in fetch(cfg, http):
                    l = merge_enrichment(con, l)
                    scored = evaluate(l, cfg)
                    prev = con.execute("SELECT starred, watch, asking_price, bid_count, reserve_met FROM listings WHERE id=?", (l.id,)).fetchone()
                    for a in watch_alerts(con, l, prev):
                        db.ui_event(con, "alert", {"id": l.id, "title": l.title[:80], "text": a})
                    db.upsert(con, l, scored)
                    seen.add(l.id)
                    n_pass += scored["passes"]
                    con.commit()  # short transactions: keeps the DB unlocked for the dashboard/other scans
                stale = db.stale_out(con, name, seen)
                con.commit()
                summary[name] = {"seen": len(seen), "pass": n_pass, "staled": stale, "secs": round(time.time() - t0)}
            except Exception as e:  # one broken site never kills the scan
                con.commit()
                summary[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}", "seen": len(seen)}
                log.error("source %s failed: %s", name, e)
                if args.verbose:
                    traceback.print_exc()
            print(f"{name:20s} {summary[name]}", flush=True)
            scan_status(done=list(summary), summary=summary)
            try:
                from .health import record
                sm = summary[name]
                record(con, name, sm.get("seen", 0), sm.get("pass", 0), sm.get("error"), sm.get("secs"))
            except ImportError:
                pass
        try:
            from .enrich import enrich_batch
            scan_status(current="enrich:flippa")
            n = enrich_batch(con, cfg, http, int(cfg.get("enrich", {}).get("max_per_scan", 60)))
            print(f"{'enrich(flippa)':20s} {n}", flush=True)
        except ImportError:
            pass
        except Exception as e:
            print(f"{'enrich':20s} {{'error': '{type(e).__name__}: {str(e)[:160]}'}}", flush=True)
        for label, fn in (("flippa-login-enrich", lambda: __import__("dealscout.flippa_auth", fromlist=["enrich_batch_logged_in"]).enrich_batch_logged_in(con, cfg, 40)),
                          ("outcomes", lambda: __import__("dealscout.outcomes", fromlist=["check_outcomes"]).check_outcomes(con, http, 150)),
                          ("signals", lambda: _signals_batch(con, cfg, http, int(cfg.get("signals", {}).get("max_per_scan", 15))))):
            try:
                scan_status(current=label)
                print(f"{label:20s} {fn()}", flush=True)
            except ImportError:
                pass
            except Exception as e:
                print(f"{label:20s} {{'error': '{type(e).__name__}: {str(e)[:160]}'}}", flush=True)
    con.execute("INSERT INTO scans (started, finished, summary) VALUES (?,?,?)",
                (started, db.now(), json.dumps(summary)))
    con.commit()
    try:
        from .health import alarms
        for a in alarms(con):
            db.ui_event(con, "alert", {"id": None, "title": "source health", "text": a})
    except ImportError:
        pass
    scan_status(running=False, current=None, finished=db.now())
    if not args.no_judge and cfg["judge"]["enabled"]:
        _judge_pending(con, cfg, cfg["judge"]["max_per_scan"])
    write_report(con, cfg)


def _signals_batch(con, cfg, http, limit):
    """Collect independent signals for candidates (BUY/NEGOTIATE, starred, watched) that lack them."""
    from .signals import collect_and_store
    from .diligence import auto_checks_from_signals
    rows = con.execute(
        "SELECT l.* FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id LEFT JOIN signals s ON s.listing_id=l.id "
        "WHERE l.status='open' AND l.hidden=0 AND (l.starred=1 OR l.watch=1 OR j.verdict IN ('BUY-CANDIDATE','NEGOTIATE')) "
        "AND (s.listing_id IS NULL OR s.collected_at < datetime('now','-7 days')) ORDER BY l.starred DESC, l.score DESC LIMIT ?", (limit,)).fetchall()
    n = 0
    for r in rows:
        l = row_to_listing(r)
        try:
            sig = collect_and_store(con, l, http, cfg)
            auto_checks_from_signals(con, l.id, sig)
            n += 1
        except Exception as e:
            log.warning("signals %s: %s", l.id, e)
    return {"collected": n, "pending": max(0, len(rows) - n)}


def _priority_sql(cfg):
    cats = cfg["judge"].get("priority_categories", [])
    cat_case = " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(cats))
    return (f"CASE l.category {cat_case} ELSE {len(cats)} END, "
            "CASE WHEN l.customers IS NOT NULL THEN 0 ELSE 1 END, "
            "CASE WHEN l.flags LIKE '%traffic_ads_dependent%' THEN 1 ELSE 0 END, l.score DESC")


def _scored_from_row(r):
    return {k: (json.loads(r[k]) if k == "flags" else r[k]) for k in ("payback_months", "payback_75", "payback_65", "flags")}


def _second_pass(con, cfg, limit):
    from .judge import second_opinion
    rows = con.execute(
        "SELECT l.* FROM listings l JOIN judgments j ON j.listing_id=l.id "
        "WHERE j.verdict1 IN ('BUY-CANDIDATE','NEGOTIATE') AND j.json2 IS NULL AND l.status='open' AND l.hidden=0 "
        f"ORDER BY {_priority_sql(cfg)} LIMIT ?", (limit,)).fetchall()
    if not rows:
        return
    print(f"second opinions on {len(rows)} listings with {cfg['judge'].get('second_model')} …", flush=True)
    for i, r in enumerate(rows):
        l = row_to_listing(r)
        set_status(running=True, phase="second", done=i, total=len(rows), current=l.id, ids=[x["id"] for x in rows])
        try:
            d = second_opinion(con, l, _scored_from_row(r), cfg)
            if d:
                print(f"  {d.get('verdict','?'):14s} (was {r['id']}) {l.title[:60]}", flush=True)
        except Exception as e:
            print(f"  ERROR {l.id}: {e}", flush=True)
    set_status(running=False, phase=None, current=None, ids=[])


def _judge_pending(con, cfg, limit, force=False, ids=None, category=None, second=True):  # noqa: C901
    from .judge import judge, second_opinion
    if ids:
        rows = [con.execute("SELECT * FROM listings WHERE id=?", (i,)).fetchone() for i in ids]
    else:
        cat_sql = "AND l.category=? " if category else ""
        params = ([category] if category else []) + [limit]
        rows = con.execute(
            "SELECT l.* FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id "
            "WHERE l.passes=1 AND l.status='open' AND l.hidden=0 AND (j.listing_id IS NULL OR j.content_hash!=l.content_hash) "
            f"{cat_sql}ORDER BY {_priority_sql(cfg)} LIMIT ?", params).fetchall()
    rows = [r for r in rows if r is not None]
    print(f"judging {len(rows)} listings with {cfg['judge']['model']} …", flush=True)
    for i, r in enumerate(rows):
        l = row_to_listing(r)
        scored = _scored_from_row(r)
        set_status(running=True, phase="first", done=i, total=len(rows), current=l.id, ids=[x["id"] for x in rows])
        try:
            pre = None if (force or r["starred"]) else rules_preverdict(l, scored, cfg)
            if pre:
                db.save_judgment(con, l.id, db.content_hash(l), "rules", pre["verdict"], pre, "")
                con.execute("UPDATE judgments SET verdict1=? WHERE listing_id=?", (pre["verdict"], l.id)); con.commit()
                print(f"  {pre['verdict']:14s} (rules) {l.title[:60]}", flush=True)
                continue
            d = judge(con, l, scored, cfg, force=force)
            print(f"  {d.get('verdict','?'):14s} {l.title[:70]}", flush=True)
            if force and ids and second and cfg["judge"].get("second_pass") and d.get("verdict") in ("BUY-CANDIDATE", "NEGOTIATE"):
                d2 = second_opinion(con, l, scored, cfg)
                if d2:
                    print(f"  ↳ skeptic: {d2.get('verdict')}", flush=True)
        except Exception as e:
            print(f"  ERROR {l.id}: {e}", flush=True)
            if "daily judge cap" in str(e):
                break
    set_status(running=False, phase=None, current=None, ids=[])
    if second and cfg["judge"].get("second_pass") and not ids:
        _second_pass(con, cfg, cfg["judge"].get("second_max_per_scan", 10))


def cmd_judge(args):
    cfg = load_config()
    con = db.connect()
    _judge_pending(con, cfg, args.limit, force=args.force, ids=args.id, category=args.category, second=not args.no_second)


def cmd_rescore(args):
    """Re-run filters/score on everything already in the DB (after editing config.toml). No network."""
    cfg = load_config()
    con = db.connect()
    n = 0
    for r in con.execute("SELECT * FROM listings").fetchall():
        l = row_to_listing(r)
        scored = evaluate(l, cfg)
        keep = {k: r[k] for k in ("starred", "hidden", "note", "first_seen", "miss_count", "relisted", "price_prev")}
        db.upsert(con, l, scored)
        con.execute("UPDATE listings SET starred=?, hidden=?, note=?, first_seen=?, miss_count=?, relisted=?, price_prev=? WHERE id=?",
                    (*keep.values(), l.id))
        n += 1
    con.commit()
    print(f"re-scored {n} listings; passing now: "
          f"{con.execute('SELECT count(*) FROM listings WHERE passes=1 AND status=\'open\'').fetchone()[0]}")
    write_report(con, cfg)


def cmd_enrich(args):
    from .enrich import enrich_batch
    cfg = load_config()
    con = db.connect()
    with client() as http:
        print(enrich_batch(con, cfg, http, args.limit))
    write_report(con, cfg)


def cmd_signals(args):
    from .signals import collect_and_store
    from .diligence import auto_checks_from_signals
    cfg = load_config(); con = db.connect()
    r = con.execute("SELECT * FROM listings WHERE id=? OR id LIKE ? OR title LIKE ? LIMIT 1", (args.id, f"%:{args.id}", f"%{args.id}%")).fetchone()
    if not r:
        print("no such listing"); return
    l = row_to_listing(r)
    with client() as http:
        sig = collect_and_store(con, l, http, cfg)
    auto_checks_from_signals(con, l.id, sig)
    print(json.dumps({"flags": sig.get("flags"), "crosschecks": sig.get("crosschecks")}, indent=1))
    db.ui_event(con, "focus", {"id": l.id})


def cmd_diligence(args):
    from .app_bundle import bundle_for
    from .diligence import run_diligence_verdict
    cfg = load_config(); con = db.connect()
    r = con.execute("SELECT * FROM listings WHERE id=? OR id LIKE ? OR title LIKE ? LIMIT 1", (args.id, f"%:{args.id}", f"%{args.id}%")).fetchone()
    if not r:
        print("no such listing"); return
    l = row_to_listing(r)
    st = ROOT / "diligence.status.json"
    st.write_text(json.dumps({"running": True, "id": l.id}))
    try:
        d = run_diligence_verdict(con, l, bundle_for(con, r, cfg), cfg)
    finally:
        st.write_text(json.dumps({"running": False, "id": l.id}))
    print(json.dumps(d, indent=1))
    db.ui_event(con, "focus", {"id": l.id})


def cmd_set_password(args):
    import getpass
    from werkzeug.security import generate_password_hash
    pw = args.password or getpass.getpass("New shared password for remote users: ")
    if len(pw) < 8:
        print("use at least 8 characters"); return
    from app import _write_config_preserving_comments  # noqa: WPS433 (reuse the comment-preserving writer)
    cfg = load_config()
    cfg["server"]["password_hash"] = generate_password_hash(pw)
    _write_config_preserving_comments(cfg)
    print("saved. Restart the dashboard: systemctl --user restart dealscout")


def cmd_share_status(args):
    import subprocess, shutil
    if not shutil.which("tailscale"):
        print("tailscale: not installed - see SHARING.md step 1"); return
    for cmd in (["tailscale", "status", "--self", "--peers=false"], ["tailscale", "serve", "status"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(f"$ {' '.join(cmd)}\n{(r.stdout or r.stderr).strip()}\n")
    cfg = load_config()
    print("public url in config:", cfg["server"].get("public_url") or "(not set - put the https://….ts.net URL in config.toml [server].public_url)")


def cmd_flag(args):
    con = db.connect()
    r = con.execute("SELECT id FROM listings WHERE id=? OR id LIKE ? OR title LIKE ? LIMIT 1", (args.id, f"%:{args.id}", f"%{args.id}%")).fetchone()
    if not r:
        print("no such listing"); return
    col = {"star": "starred", "unstar": "starred", "hide": "hidden", "unhide": "hidden", "watch": "watch", "unwatch": "watch"}[args.cmd]
    con.execute(f"UPDATE listings SET {col}=? WHERE id=?", (0 if args.cmd.startswith("un") else 1, r["id"])); con.commit()
    db.ui_event(con, "focus", {"id": r["id"]})
    print(f"{args.cmd} {r['id']}")


def cmd_evidence(args):
    from .diligence import add_evidence
    con = db.connect()
    add_evidence(con, args.id, args.kind, args.title, args.body or "", args.url or "")
    db.ui_event(con, "focus", {"id": args.id})
    print("saved")


def cmd_import_login(args):
    from .cookie_import import import_cookies
    n = import_cookies(args.domain)
    print(f"imported {n} {args.domain} cookies from your Firefox into the app's browser profile")
    if "flippa" in args.domain:
        from .flippa_auth import is_logged_in
        print("flippa session:", "logged in ✓" if is_logged_in() else "still not logged in - are you signed in to flippa.com in Firefox?")


def cmd_enrich_login(args):
    from .flippa_auth import enrich_batch_logged_in, is_logged_in
    cfg = load_config(); con = db.connect()
    print("logged in" if is_logged_in() else "NOT logged in - run ./dealscout.sh login flippa first")
    print(enrich_batch_logged_in(con, cfg, args.limit))


def cmd_outcomes(args):
    from .outcomes import check_outcomes
    con = db.connect()
    with client() as http:
        print(check_outcomes(con, http, args.limit))


def cmd_health(args):
    from .health import report, alarms
    con = db.connect()
    for r in report(con):
        print(f"{r['source']:20s} {r['status']:9s} last {r.get('last_seen')} (median {r.get('median_seen')}) {r.get('last_error') or ''}")
    for a in alarms(con):
        print("ALARM:", a)


def cmd_show(args):
    """Ask the dashboard to scroll to / expand a listing (used by Claude in the terminal)."""
    con = db.connect()
    r = con.execute("SELECT id, title FROM listings WHERE id=? OR id LIKE ? OR title LIKE ? LIMIT 1", (args.id, f"%:{args.id}", f"%{args.id}%")).fetchone()
    if not r:
        print("no such listing"); return
    db.ui_event(con, "focus", {"id": r["id"]})
    print(f"dashboard → {r['id']}: {r['title'][:70]}")


def cmd_feedback(args):
    con = db.connect()
    db.add_feedback(con, args.id, 0 if args.disagree else 1, args.comment or "")
    print("saved")


def write_report(con, cfg, path=None):
    rows = con.execute(
        "SELECT l.*, j.verdict, j.json AS jjson FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id "
        "WHERE l.passes=1 AND l.status='open' AND l.hidden=0 ORDER BY "
        "CASE j.verdict WHEN 'BUY-CANDIDATE' THEN 0 WHEN 'NEGOTIATE' THEN 1 WHEN 'PASS' THEN 3 ELSE 2 END, l.score DESC").fetchall()
    out = [f"# DealScout shortlist - {date.today()}", "",
           f"Filters: price ≤ ${cfg['filters']['max_price']:,}, payback ≤ {cfg['filters']['max_payback_months']} mo, "
           f"≥ {cfg['filters']['min_customers']} customers. {len(rows)} listings pass.", ""]
    for r in rows:
        j = json.loads(r["jjson"]) if r["jjson"] else {}
        out += [f"## {r['title']}", f"- Source: {r['source']} - {r['url']}",
                f"- Asking ${r['asking_price']:,.0f} · profit ${(r['monthly_profit'] or 0):,.0f}/mo · payback {r['payback_months']} mo "
                f"(at 75%: {r['payback_75']}, at 65%: {r['payback_65']}) · score {r['score']}",
                f"- Customers: {r['customers']} paying / {r['users_free']} free · margin {r['margin']}% · age {r['age_months']} mo · "
                f"verified rev={bool(r['verified_revenue'])} · flags: {', '.join(json.loads(r['flags'] or '[]')) or '-'}"]
        if j:
            out += [f"- **Claude verdict: {j.get('verdict')}** (turnkey {j.get('turnkey_score')}/10, niche risk {j.get('niche_risk')}/10, "
                    f"suggested offer {j.get('suggested_offer')}) - {j.get('rationale','')}"]
        out.append("")
    path = path or ROOT / "reports" / f"{date.today()}.md"
    path.write_text("\n".join(out))
    (ROOT / "reports" / "latest.md").write_text("\n".join(out))
    print(f"report → {path}")


def cmd_report(args):
    con = db.connect()
    write_report(con, load_config())


def cmd_login(args):
    from .sources.browser import login
    login(args.site)


def cmd_sources(args):
    cfg = load_config()
    for n in ALL:
        kind = "browser" if n in BROWSER else "http"
        print(f"{n:20s} {'on ' if cfg['sources'].get(n) else 'off'}  {kind}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="dealscout")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("scan", help="fetch all enabled sources, score, judge, write report")
    s.add_argument("--source", action="append", help="only this source (repeatable)")
    s.add_argument("--no-judge", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_scan)
    j = sp.add_parser("judge", help="run Claude judge on passing listings")
    j.add_argument("--limit", type=int, default=20)
    j.add_argument("--force", action="store_true")
    j.add_argument("--id", action="append")
    j.add_argument("--category", help="only this category (saas, chrome_extension, …)")
    j.add_argument("--no-second", action="store_true", help="skip the skeptic second pass")
    j.set_defaults(fn=cmd_judge)
    sp.add_parser("rescore", help="re-apply filters/score from config.toml to the stored listings (no network)").set_defaults(fn=cmd_rescore)
    en = sp.add_parser("enrich", help="fetch Flippa detail pages for passing listings (fills customers/churn/description)")
    en.add_argument("--limit", type=int, default=300)
    en.set_defaults(fn=cmd_enrich)
    sg = sp.add_parser("signals", help="collect independent verification signals for a listing (domain age, store numbers, mentions)")
    sg.add_argument("id"); sg.set_defaults(fn=cmd_signals)
    dg = sp.add_parser("diligence", help="Claude diligence verdict (PROCEED / MORE-INFO / WALK) using all evidence")
    dg.add_argument("id"); dg.set_defaults(fn=cmd_diligence)
    for c in ("star", "unstar", "hide", "unhide", "watch", "unwatch"):
        x = sp.add_parser(c); x.add_argument("id"); x.set_defaults(fn=cmd_flag)
    ev = sp.add_parser("evidence", help="attach evidence to a listing")
    ev.add_argument("id"); ev.add_argument("--kind", default="note"); ev.add_argument("--title", required=True); ev.add_argument("--body"); ev.add_argument("--url")
    ev.set_defaults(fn=cmd_evidence)
    il = sp.add_parser("import-login", help="copy a site's cookies from your real Firefox (no separate login window)")
    il.add_argument("domain", nargs="?", default="flippa.com")
    il.set_defaults(fn=cmd_import_login)
    el = sp.add_parser("enrich-login", help="logged-in Flippa enrichment (P&L, traffic, Q&A) - needs `login flippa` once")
    el.add_argument("--limit", type=int, default=40); el.set_defaults(fn=cmd_enrich_login)
    oc = sp.add_parser("outcomes", help="check what happened to listings that disappeared (sold / ended / removed)")
    oc.add_argument("--limit", type=int, default=200); oc.set_defaults(fn=cmd_outcomes)
    sp.add_parser("health", help="per-source scraper health + alarms").set_defaults(fn=cmd_health)
    spw = sp.add_parser("set-password", help="set the shared password remote users need (SHARING.md)")
    spw.add_argument("--password"); spw.set_defaults(fn=cmd_set_password)
    sp.add_parser("share-status", help="Tailscale + serve status for the shared dashboard").set_defaults(fn=cmd_share_status)
    sh = sp.add_parser("show", help="scroll the dashboard to a listing (id or title fragment)")
    sh.add_argument("id")
    sh.set_defaults(fn=cmd_show)
    fb = sp.add_parser("feedback", help="record agreement/disagreement with a verdict")
    fb.add_argument("id"); fb.add_argument("--disagree", action="store_true"); fb.add_argument("--comment")
    fb.set_defaults(fn=cmd_feedback)
    sp.add_parser("report", help="write reports/latest.md").set_defaults(fn=cmd_report)
    lg = sp.add_parser("login", help="open a browser window to log into a gated site")
    lg.add_argument("site")
    lg.set_defaults(fn=cmd_login)
    sp.add_parser("sources").set_defaults(fn=cmd_sources)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
