import sqlite3, json, hashlib, re, shutil
from datetime import datetime, timezone, date
from urllib.parse import urlparse
from . import DB_PATH, ROOT
from .models import SCHEMA, MIGRATIONS, Listing


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            con.execute(m)
        except sqlite3.OperationalError:
            pass  # already applied
    return con


def backup(keep: int = 14):
    """Daily copy of the DB into backups/ (starred/notes/judgments are the valuable part)."""
    d = ROOT / "backups"
    d.mkdir(exist_ok=True)
    target = d / f"dealscout-{date.today()}.db"
    if not target.exists() and DB_PATH.exists():
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(target)
        src.backup(dst)
        dst.close(); src.close()
    for old in sorted(d.glob("dealscout-*.db"))[:-keep]:
        old.unlink()


def dupe_key(l: Listing) -> str:
    """Same business listed on two sites: prefer hostname from raw/url hints, else normalized title."""
    for cand in (l.raw.get("hostname"), l.raw.get("external_url"), l.raw.get("website"), l.raw.get("domain")):
        if cand:
            h = urlparse(cand if "//" in str(cand) else f"http://{cand}").hostname or str(cand)
            h = h.lower().removeprefix("www.")
            if "." in h and not any(x in h for x in ("flippa", "acquire", "empireflippers")):
                return "host:" + h
    t = re.sub(r"[^a-z0-9 ]", " ", (l.title or "").lower())
    t = " ".join(w for w in t.split() if len(w) > 3)[:60]
    return "title:" + t


def content_hash(l: Listing) -> str:
    key = f"{l.title}|{l.asking_price}|{l.monthly_profit}|{l.monthly_revenue}|{l.customers}|{l.summary[:400]}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def upsert(con, l: Listing, scored: dict):
    row = l.to_row()
    row.update(scored)
    row["content_hash"] = content_hash(l)
    row["last_seen"] = now()
    row["miss_count"] = 0
    row["verified_revenue"] = int(l.verified_revenue)
    row["verified_traffic"] = int(l.verified_traffic)
    row["dupe_key"] = dupe_key(l)
    row["bid_count"] = l.raw.get("bid_count")
    row["reserve_met"] = int(bool(l.raw.get("reserve_met"))) if l.raw.get("reserve_met") is not None else None
    prev = con.execute("SELECT asking_price, status, first_seen FROM listings WHERE id=?", (l.id,)).fetchone()
    flags = list(scored.get("flags") or [])
    if prev is not None:
        if prev["asking_price"] and l.asking_price and abs(prev["asking_price"] - l.asking_price) > 0.5:
            row["price_prev"] = prev["asking_price"]
            con.execute("INSERT INTO price_history VALUES (?,?,?,?,?,?)",
                        (l.id, now(), l.asking_price, l.status, l.raw.get("bid_count"), int(bool(l.raw.get("reserve_met")))))
            if l.asking_price < prev["asking_price"]:
                flags.append(f"price_drop:{round(100 * (1 - l.asking_price / prev['asking_price']))}%")
        if prev["status"] in ("stale", "ended", "sold") and l.status == "open":
            row["relisted"] = 1
            flags.append("relisted")
    else:
        con.execute("INSERT INTO price_history VALUES (?,?,?,?,?,?)",
                    (l.id, now(), l.asking_price, l.status, l.raw.get("bid_count"), int(bool(l.raw.get("reserve_met")))))
    if l.raw.get("reserve_met") is False and l.raw.get("bid_count"):
        flags.append("reserve_not_met")
    # keep an earlier price_drop flag alive across scans (flag list is recomputed each scan)
    if prev is not None and "price_prev" not in row:
        old = con.execute("SELECT flags FROM listings WHERE id=?", (l.id,)).fetchone()
        for f in json.loads(old["flags"] or "[]") if old else []:
            if f.startswith("price_drop:") and f not in flags:
                flags.append(f)
    row["flags"] = flags
    for k in ("flags", "fail_reasons"):
        if isinstance(row.get(k), (list, tuple)):
            row[k] = json.dumps(row[k])
    cols = ", ".join(row)
    con.execute(
        f"INSERT INTO listings ({cols}, first_seen) VALUES ({', '.join(':'+c for c in row)}, :last_seen) "
        f"ON CONFLICT(id) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in row if c != "id"),
        row,
    )


def stale_out(con, source: str, seen_ids: set[str], threshold: int = 2) -> int:
    """Listings of `source` not in seen_ids: bump miss_count; mark stale after `threshold` misses."""
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM listings WHERE source=? AND status='open'", (source,))]
    missing = [i for i in ids if i not in seen_ids]
    for i in missing:
        con.execute("UPDATE listings SET miss_count=miss_count+1 WHERE id=?", (i,))
    cur = con.execute(
        "UPDATE listings SET status='stale' WHERE source=? AND status='open' AND miss_count>=?",
        (source, threshold))
    return cur.rowcount


def get_judgment(con, listing_id: str):
    return con.execute("SELECT * FROM judgments WHERE listing_id=?", (listing_id,)).fetchone()


def save_judgment(con, listing_id: str, chash: str, model: str, verdict: str, data: dict, raw: str):
    con.execute(
        "INSERT INTO judgments (listing_id, content_hash, model, judged_at, verdict, json, raw_text) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET content_hash=excluded.content_hash, "
        "model=excluded.model, judged_at=excluded.judged_at, verdict=excluded.verdict, json=excluded.json, "
        "raw_text=excluded.raw_text",
        (listing_id, chash, model, now(), verdict, json.dumps(data), raw))


def add_feedback(con, listing_id: str, agree: int, comment: str):
    r = con.execute("SELECT l.title, j.verdict FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id WHERE l.id=?",
                    (listing_id,)).fetchone()
    con.execute("INSERT INTO feedback (listing_id, at, agree, comment, verdict, title) VALUES (?,?,?,?,?,?)",
                (listing_id, now(), agree, comment or "", r["verdict"] if r else None, r["title"] if r else None))
    con.commit()


def recent_disagreements(con, n: int = 10):
    return con.execute("SELECT * FROM feedback WHERE agree=0 ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def ui_event(con, kind: str, payload: dict):
    con.execute("INSERT INTO ui_events (at, kind, payload) VALUES (?,?,?)", (now(), kind, json.dumps(payload)))
    con.commit()


def judged_today(con) -> int:
    return con.execute("SELECT count(*) FROM judgments WHERE substr(judged_at,1,10)=?",
                       (date.today().isoformat(),)).fetchone()[0]
