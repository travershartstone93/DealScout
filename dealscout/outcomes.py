"""What happened to listings that vanished: sold (final price), ended unsold, removed, relisted.

Flippa v3 (verified 2026-08-17): GET /v3/listings/<id> -> data.status in
  open | won (sold; current_price = winning/agreed price; page title "... Sold on Flippa", ld+json SoldOut)
  | completed (ended, not sold; title "... Listing Ended on Flippa") | expired | cancelled; 404 -> {"errors":[..]}.
`filter[status]=won` lists sold ones (1185 total), `expired` 6979, `completed`/`cancelled` capped at 10000;
there is no id batch filter, so we page `won` per property type first (cheap) then GET the rest one by one.
Other sources: light page re-fetch (404/410 -> removed, sold badge -> sold, otherwise unknown)."""
import json, re, time, logging
from datetime import datetime, timezone, timedelta

from . import db

log = logging.getLogger("dealscout")
API = "https://flippa.com/v3/listings"
OUTCOMES = ("sold", "ended_unsold", "removed", "relisted", "unknown")
_SOLD_RX = re.compile(r"\b(sold out|has been sold|listing (?:has )?sold|(?:^|\W)sold(?:\W|$)|acquired|under offer|no longer available)\b", re.I)
_DELAY = 2.0      # seconds between v3 GETs; Cloudflare 1015 (429) still hits after ~10 -> HTML fallback


class RateLimited(Exception):
    pass


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _candidates(con, limit: int):
    """Rows worth checking: vanished/ended/sold, not already resolved (unknown/relisted re-checked weekly)."""
    week_ago = _iso(datetime.now(timezone.utc) - timedelta(days=7))
    return con.execute(
        "SELECT l.*, o.outcome AS prev_outcome, o.checked_at AS prev_checked FROM listings l "
        "LEFT JOIN outcomes o ON o.listing_id=l.id "
        "WHERE (l.status IN ('stale','ended','sold') OR l.miss_count>=2) "
        "AND (o.listing_id IS NULL OR (o.outcome IN ('unknown','relisted') AND o.checked_at < ?)) "
        "ORDER BY l.last_seen DESC LIMIT ?", (week_ago, limit)).fetchall()


def _first_price(con, listing_id: str, fallback):
    r = con.execute("SELECT asking_price FROM price_history WHERE listing_id=? AND asking_price IS NOT NULL "
                    "ORDER BY seen_at LIMIT 1", (listing_id,)).fetchone()
    return r["asking_price"] if r else fallback


def _write(con, row, outcome: str, final_price=None, sold_at=None, listing_status=None):
    """Upsert one outcomes row (+ optionally the listing status). Commits."""
    raw = json.loads(row["raw_json"] or "{}")
    now = datetime.now(timezone.utc)
    started = _parse(raw.get("starts_at")) or _parse(row["first_seen"]) or now
    ended = _parse(sold_at) or _parse(raw.get("ends_at")) or now
    days = max(0, (ended - started).days)
    con.execute(
        "INSERT INTO outcomes (listing_id, checked_at, outcome, final_price, sold_at, first_price, days_listed, "
        "category, monthly_profit, source) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET "
        "checked_at=excluded.checked_at, outcome=excluded.outcome, final_price=excluded.final_price, "
        "sold_at=excluded.sold_at, first_price=excluded.first_price, days_listed=excluded.days_listed",
        (row["id"], _iso(now), outcome, final_price, sold_at, _first_price(con, row["id"], row["asking_price"]),
         days, row["category"], row["monthly_profit"], row["source"]))
    if listing_status and row["status"] != listing_status:
        con.execute("UPDATE listings SET status=? WHERE id=?", (listing_status, row["id"]))
    con.commit()


# ---------------------------------------------------------------- flippa
def flippa_outcome(d: dict) -> tuple[str, float | None, str | None, str | None]:
    """v3 record -> (outcome, final_price, sold_at, listing_status)."""
    st = d.get("status")
    price = d.get("current_price") or d.get("display_price")
    if st == "won":
        return "sold", price, d.get("sold_at") or d.get("ends_at"), "sold"
    if st in ("completed", "expired"):
        return "ended_unsold", None, None, "ended"
    if st == "cancelled":
        return "removed", None, None, "ended"
    if st == "open":
        return "relisted", None, None, "open"
    return "unknown", None, None, None


def _flippa_won_index(cfg, http, max_price) -> dict[str, dict]:
    """id -> record for recently won listings of our property types (a handful of 100-row pages)."""
    from .sources.base import get
    out = {}
    types = cfg.get("flippa", {}).get("property_types", ["saas"])
    for pt in types:
        page = 1
        while page <= 5:
            try:
                data = get(http, API, params={"filter[status]": "won", "filter[property_type]": pt,
                                              "filter[price][max]": max_price, "page[size]": 100,
                                              "page[number]": page}, delay=_DELAY).json()
            except Exception as e:
                log.warning("outcomes: won index %s p%d: %s", pt, page, e)
                break
            items = data.get("data", [])
            for d in items:
                out[str(d["id"])] = d
            if len(items) < 100:
                break
            page += 1
    log.info("outcomes: won index has %d flippa ids", len(out))
    return out


def _flippa_fetch(http, native_id: str) -> dict | None:
    """One v3 GET; {} for 404, None for transport failure; RateLimited on 429 (caller switches to HTML)."""
    import httpx
    try:
        r = http.get(f"{API}/{native_id}")
    except httpx.HTTPError:
        return None
    if r.status_code == 429:
        raise RateLimited()
    time.sleep(_DELAY)
    if r.status_code == 404:
        return {}
    if r.status_code != 200:
        return None
    try:
        return r.json().get("data") or {}
    except ValueError:
        return None


def _flippa_html(http, url: str) -> tuple[str, float | None, str | None, str | None] | None:
    """Fallback when v3 is rate-limited: the public page title says 'Sold on Flippa' / 'Listing Ended on Flippa'."""
    from .enrich import parse_flippa_page
    from .sources.base import is_challenge
    try:
        r = http.get(url, headers={"Accept": "text/html,*/*;q=0.8"})
    except Exception:
        return None
    time.sleep(1.0)
    if r.status_code in (404, 410):
        return "removed", None, None, "ended"
    if r.status_code != 200 or is_challenge(r.text):
        return None
    page = parse_flippa_page(r.text)
    if not page:
        return None
    bid = page.get("bid", {})
    st = page.get("status")
    if st == "sold":
        return "sold", bid.get("highest_bid") or bid.get("buy_it_now") or bid.get("asking_price"), None, "sold"
    if st == "ended" or "Listing Ended" in (page.get("page_title") or ""):
        return "ended_unsold", None, None, "ended"
    if "for Sale on Flippa" in (page.get("page_title") or ""):
        return "relisted", None, None, "open"
    return "unknown", None, None, None


# ---------------------------------------------------------------- other sources
def _page_outcome(http, url: str) -> tuple[str, str | None]:
    """(outcome, listing_status) from a plain page fetch."""
    import httpx
    try:
        r = http.get(url, headers={"Accept": "text/html,*/*;q=0.8"})
    except httpx.HTTPError:
        return "unknown", None
    if r.status_code in (404, 410):
        return "removed", "ended"
    if r.status_code != 200:
        return "unknown", None
    html = r.text
    head = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html[:400000], flags=re.S)
    title = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    title = title.group(1) if title else ""
    if "schema.org/SoldOut" in html or _SOLD_RX.search(title):
        return "sold", "sold"
    text = re.sub(r"<[^>]+>", " ", head[:60000])
    if re.search(r"\b(this (?:listing|project|business|startup) (?:has been|was|is) (?:sold|acquired)|sold out|status:\s*sold)\b", text, re.I):
        return "sold", "sold"
    return "unknown", None


# ---------------------------------------------------------------- entry point
def check_outcomes(con, http, limit: int = 200, cfg: dict | None = None) -> dict:
    """Resolve outcomes for vanished listings; writes `outcomes` rows and updates listing status. Idempotent."""
    if cfg is None:
        from . import load_config
        cfg = load_config()
    rows = _candidates(con, limit)
    counts = {k: 0 for k in OUTCOMES}
    counts.update(checked=0, errors=0)
    if not rows:
        return counts
    flippa_rows = [r for r in rows if r["source"] == "flippa"]
    won = _flippa_won_index(cfg, http, cfg["filters"]["max_price"]) if flippa_rows else {}
    limited = False
    for r in rows:
        counts["checked"] += 1
        outcome, price, sold_at, lst = "unknown", None, None, None
        if r["source"] == "flippa":
            nid = r["id"].split(":", 1)[1]
            d = won.get(nid)
            if d is None and not limited:
                try:
                    d = _flippa_fetch(http, nid)
                except RateLimited:
                    limited = True
                    log.warning("outcomes: flippa v3 rate-limited - falling back to HTML pages")
            if d is None and limited:
                res = _flippa_html(http, r["url"])
                if res is None:
                    counts["errors"] += 1
                    continue
                outcome, price, sold_at, lst = res
            elif d is None:
                counts["errors"] += 1
                continue
            elif d == {}:
                outcome, lst = "removed", "ended"
            else:
                outcome, price, sold_at, lst = flippa_outcome(d)
        elif r["status"] == "sold":                    # the source itself told us
            outcome, price, lst = "sold", r["asking_price"], "sold"
        elif r["url"]:
            outcome, lst = _page_outcome(http, r["url"])
            if outcome == "sold":
                price = r["asking_price"]
            time.sleep(0.5)
        if outcome == "relisted" and r["status"] == "open":
            outcome = "unknown"                        # still open, we just missed it in the scan
        if outcome == "sold" and not sold_at:
            sold_at = _iso(datetime.now(timezone.utc))
        _write(con, r, outcome, price, sold_at, lst)
        counts[outcome] += 1
    log.info("outcomes: %s", counts)
    return counts


if __name__ == "__main__":  # python -m dealscout.outcomes [limit]
    import sys
    from .sources.base import client
    logging.basicConfig(level=logging.INFO)
    con = db.connect()
    with client() as http:
        print(check_outcomes(con, http, int(sys.argv[1]) if len(sys.argv) > 1 else 200))
