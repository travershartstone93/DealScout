"""Logged-in Flippa enrichment via the persistent Firefox profile (~/dealscout/.browser).

Anonymous listing pages (verified 2026-08-17) render only "About the Business" plus a sign-up wall;
the accordion sections are declared as `new FlippaAccordion(["collapse-performance_overview",
"collapse-about_the_business", "collapse-comparisons_and_benchmarking", "collapse-revenue_and_expenses",
"collapse-performance_data", "collapse-google_analytics_data", "collapse-traffic_insights",
"collapse-monetization_methods", "collapse-products_and_services", "collapse-sale_inclusions",
"collapse-social_media", "collapse-attachments", "collapse-first_access", "collapse-disclaimer"])`
but no `#collapse-*` element exists until you are logged in. Capture is therefore generic: click every
expandable header, then record heading -> body text for each `#collapse-*` / toggle-class block.
Run `./dealscout.sh login flippa` once to create the session."""
import re, logging
from dataclasses import replace

from . import db
from .models import Listing
from .normalize import money, intval
from .sources.browser import _ctx

log = logging.getLogger("dealscout")

LOGIN_URL = "https://flippa.com/login"          # for browser.LOGIN_URLS["flippa"]
ACCOUNT_URL = "https://flippa.com/users/edit"   # 302 -> /login?to=... when logged out (verified 2026-08-17)
SECTIONS = ("performance_overview", "about_the_business", "comparisons_and_benchmarking", "revenue_and_expenses",
            "performance_data", "google_analytics_data", "traffic_insights", "monetization_methods",
            "products_and_services", "sale_inclusions", "social_media", "attachments", "first_access", "disclaimer")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{2,4}"
_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(?:\d+(?:\.\d+)?)?\s*(?:hours?|hrs?)\s*(?:per|a|/|each)\s*week", re.I)
_REASON = re.compile(r"reason(?:s)? for (?:selling|sale|the sale)\s*[:\-–?]*\s*(.{20,400}?)(?:\n|$)", re.I | re.S)

# JS: click every collapsed accordion header / toggle so gated bodies render, return how many were clicked.
_EXPAND_JS = """() => {
  const sel = ['[data-toggle="collapse"]', '[data-bs-toggle="collapse"]', '[aria-expanded="false"]',
               '[data-action*="toggle-class#toggle"]', '.accordion-header', '.card-header', '[id^="heading-"]'];
  const seen = new Set(); let n = 0;
  for (const s of sel) for (const el of document.querySelectorAll(s)) {
    if (seen.has(el)) continue; seen.add(el);
    const exp = el.getAttribute('aria-expanded');
    if (exp === 'true') continue;
    try { el.click(); n++; } catch (e) {}
  }
  for (const el of document.querySelectorAll('[id^="collapse-"], .collapse')) {
    el.classList.add('show'); el.style.display = 'block'; el.style.height = 'auto';
  }
  return n;
}"""

# JS: heading -> body text for every accordion/toggle block; also tables and links inside them.
_CAPTURE_JS = """() => {
  const txt = el => (el.innerText || el.textContent || '').replace(/[ \\t]+/g, ' ').replace(/\\n{2,}/g, '\\n').trim();
  const out = {};
  const headingOf = el => {
    let h = el.previousElementSibling;
    for (let i = 0; i < 3 && h; i++, h = h.previousElementSibling) {
      const t = txt(h); if (t && t.length < 120) return t;
    }
    const p = el.parentElement;
    if (p) { const hh = p.querySelector('h1,h2,h3,h4,h5,h6,[data-action*="toggle"]'); if (hh && txt(hh).length < 120) return txt(hh); }
    return el.id || 'section';
  };
  const add = (key, el) => {
    if (!el) return;
    const t = txt(el); if (!t) return;
    const tables = [...el.querySelectorAll('table')].map(tb => [...tb.querySelectorAll('tr')].map(tr => [...tr.querySelectorAll('th,td')].map(txt)));
    const links = [...el.querySelectorAll('a[href]')].map(a => ({text: txt(a).slice(0, 120), href: a.href})).filter(a => a.text || a.href);
    const imgs = [...el.querySelectorAll('img[src]')].map(i => i.src).slice(0, 30);
    if (out[key] && out[key].text.length >= t.length) return;
    out[key] = {heading: headingOf(el), text: t, tables, links: links.slice(0, 60), imgs};
  };
  for (const el of document.querySelectorAll('[id^="collapse-"]')) add(el.id.replace(/^collapse-/, ''), el);
  let i = 0;
  for (const el of document.querySelectorAll('[data-controller="toggle-class"]')) {
    const h = el.querySelector('[data-action*="toggle-class#toggle"]');
    const bodies = [...el.querySelectorAll('[data-toggle-class-target="element"]')].filter(b => b.tagName !== 'HR' && b.tagName !== 'svg');
    if (!bodies.length) continue;
    const key = (h ? txt(h) : ('toggle_' + (i++))).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 60) || ('toggle_' + (i++));
    add(key, bodies[bodies.length - 1]);
    if (h) out[key].heading = txt(h);
  }
  return out;
}"""

_LOGGED_OUT_JS = """() => {
  const b = (document.body.innerText || '').toLowerCase();
  const hasLoginForm = !!document.querySelector('form[action*="login"], form[action*="sign_in"], input[name*="password"]');
  const hasAccount = !!document.querySelector('a[href*="/logout"], a[href*="/sign_out"], a[href*="/dashboard"], a[href*="/account"], [data-testid*="avatar"], .avatar');
  return {hasLoginForm, hasAccount, url: location.href, title: document.title, wall: b.includes('create your account to view')};
}"""


def _open(headless=True):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        ctx = _ctx(pw, headless=headless)
    except Exception:
        pw.stop()
        raise
    return pw, ctx


def _login_state(pg) -> dict:
    return pg.evaluate(_LOGGED_OUT_JS)


def is_logged_in() -> bool:
    """Open the account page with the persistent profile; True if we are not bounced to /login."""
    try:
        pw, ctx = _open()
    except Exception as e:
        log.warning("flippa_auth: cannot start browser: %s", e)
        return False
    try:
        pg = ctx.new_page()
        pg.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(2500)
        st = _login_state(pg)
        ok = "/login" not in st["url"] and "/signup" not in st["url"] and not st["hasLoginForm"]
        log.info("flippa_auth: %s (url=%s)", "logged in" if ok else "not logged in", st["url"])
        return ok
    except Exception as e:
        log.warning("flippa_auth: login check failed: %s", e)
        return False
    finally:
        ctx.close(); pw.stop()


def _series(text: str) -> list[dict]:
    """'Jan 2026 $1,200 $300 ...' rows -> [{period, values:[..]}] (best-effort, from flat text)."""
    rows = []
    for m in re.finditer(rf"({_MONTH})((?:\s*-?\$?\s?[\d,]+(?:\.\d+)?%?){{1,6}})", text):
        vals = [money(x) for x in re.findall(r"-?\$?\s?[\d,]+(?:\.\d+)?", m.group(2))]
        rows.append({"period": m.group(1), "values": vals})
    return rows


def _table_series(tables: list) -> list[dict]:
    out = []
    for tb in tables or []:
        if len(tb) < 2:
            continue
        hdr = [c.lower() for c in tb[0]]
        for row in tb[1:]:
            if len(row) < 2:
                continue
            d = {"period": row[0]}
            for k, v in zip(hdr[1:], row[1:]):
                d[k or "value"] = money(v)
            out.append(d)
    return out


def _parse_gated(cap: dict) -> dict:
    """Turn captured sections into the raw['gated'] structure."""
    g = {"revenue_series": [], "expenses": None, "traffic": {}, "monetization": None, "inclusions": None,
         "qa": [], "attachments": [], "raw_text": {}, "sections": sorted(cap)}
    for key, sec in cap.items():
        text = sec.get("text") or ""
        g["raw_text"][key] = text[:4000]
        if "revenue" in key or "financ" in key or "profit" in key:
            g["revenue_series"] = _table_series(sec.get("tables")) or _series(text) or g["revenue_series"]
            m = re.search(r"^expenses?\s*[:\-–]?\s*\n((?:.*\n?){1,12})", text, re.I | re.M)
            if m and not g["expenses"]:
                g["expenses"] = m.group(1).strip()[:1500]
        elif "traffic" in key or "analytics" in key or "performance_data" in key:
            t = g["traffic"]
            for lab in ("uniques", "unique visitors", "users", "pageviews", "page views", "sessions"):
                m = re.search(rf"{lab}[^\d\n]{{0,30}}([\d,\.]+\s?[kKmM]?)", text, re.I)
                if m and lab not in t:
                    t[lab] = intval(m.group(1))
            src = {m.group(1).strip(): money(m.group(2)) for m in
                   re.finditer(r"\n([A-Za-z][A-Za-z /]{2,30}?)\s*[:\-–]?\s*(\d{1,3}(?:\.\d+)?%)", text)}
            if src:
                t["sources_pct"] = src
            if sec.get("tables"):
                t.setdefault("tables", sec["tables"][:3])
        elif "monetization" in key or "products_and_services" in key:
            g["monetization"] = (g["monetization"] + "\n" if g["monetization"] else "") + text[:2000]
        elif "inclusion" in key:
            g["inclusions"] = text[:2000]
        elif "attachment" in key:
            g["attachments"] = [{"text": a["text"], "url": a["href"]} for a in sec.get("links", [])
                                if not re.search(r"flippa\.com/(login|signup|help)", a["href"])]
        elif "question" in key or "comment" in key or key.startswith("q_a") or "seller_note" in key:
            g["qa"].append(text[:3000])
    return g


def _apply(l: Listing, g: dict) -> Listing:
    """Fill Listing fields only when clearly stated in gated text."""
    upd, raw = {}, dict(l.raw)
    alltext = "\n".join(g["raw_text"].values())
    ser = g.get("revenue_series") or []
    prof = [r.get("profit") for r in ser if isinstance(r, dict) and r.get("profit") is not None]
    rev = [r.get("revenue") for r in ser if isinstance(r, dict) and r.get("revenue") is not None]
    if l.monthly_profit is None and prof:
        upd["monthly_profit"] = round(sum(prof[-3:]) / len(prof[-3:]), 2)
    if l.monthly_revenue is None and rev:
        upd["monthly_revenue"] = round(sum(rev[-3:]) / len(rev[-3:]), 2)
    if l.hours_per_week is None:
        m = _HOURS.search(alltext)
        if m:
            upd["hours_per_week"] = float(m.group(1))
    if not l.reason_for_selling:
        m = _REASON.search(alltext)
        if m:
            upd["reason_for_selling"] = m.group(1).strip()[:400]
    if l.customers is None:
        m = (re.search(r"(?:paying customers|active subscribers|paid subscribers|customers)\s*[:\-–]?\s*([\d,]+)\b", alltext, re.I)
             or re.search(r"\b([\d,]{2,})\s+(?:paying customers|active subscribers|paid subscribers|paying users)", alltext, re.I))
        if m:
            upd["customers"] = intval(m.group(1))
    if l.churn_pct is None:
        m = re.search(r"churn(?: rate)?\s*[:\-–]?\s*(\d{1,2}(?:\.\d+)?)\s*%", alltext, re.I)
        if m:
            upd["churn_pct"] = float(m.group(1))
    raw["gated"] = g
    raw["gated_at"] = db.now()
    upd["raw"] = raw
    return replace(l, **upd)


def _capture_page(pg, url: str) -> tuple[dict | None, dict]:
    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_timeout(2500)
    st = _login_state(pg)
    if "/login" in st["url"] or st["wall"]:
        return None, st
    for _ in range(2):
        pg.evaluate(_EXPAND_JS)
        pg.wait_for_timeout(1200)
    return pg.evaluate(_CAPTURE_JS), st


def enrich_logged_in(listing: Listing, con=None) -> Listing | None:
    """Open listing.url in the logged-in profile and store gated sections under raw['gated']. None if not logged in."""
    if listing.source != "flippa" or not listing.url:
        return None
    try:
        pw, ctx = _open()
    except Exception as e:
        log.warning("flippa_auth %s: cannot start browser: %s", listing.id, e)
        return None
    try:
        pg = ctx.new_page()
        cap, st = _capture_page(pg, listing.url)
    except Exception as e:
        log.warning("flippa_auth %s: %s", listing.id, e)
        return None
    finally:
        ctx.close(); pw.stop()
    if cap is None:
        log.warning("flippa_auth %s: not logged in (sign-up wall) — run ./dealscout.sh login flippa", listing.id)
        return None
    return _apply(listing, _parse_gated(cap))


def enrich_batch_logged_in(con, cfg: dict, limit: int = 40) -> dict:
    """Gated-section pass over passing flippa rows lacking raw.gated; one browser context for the batch."""
    from .scan import row_to_listing
    from .score import evaluate
    if not is_logged_in():
        return {"skipped": "not logged in — run ./dealscout.sh login flippa"}
    rows = con.execute(
        "SELECT * FROM listings WHERE source='flippa' AND status='open' AND passes=1 AND hidden=0 "
        "AND json_extract(raw_json,'$.gated') IS NULL ORDER BY score DESC LIMIT ?", (limit,)).fetchall()
    n_ok = n_fail = 0
    pw, ctx = _open()
    try:
        pg = ctx.new_page()
        for r in rows:
            l = row_to_listing(r)
            try:
                cap, st = _capture_page(pg, l.url)
            except Exception as e:
                log.warning("flippa_auth %s: %s", l.id, e)
                cap = None
            if cap is None:
                n_fail += 1
                continue
            new = _apply(l, _parse_gated(cap))
            db.upsert(con, new, evaluate(new, cfg))
            con.commit()
            n_ok += 1
    finally:
        ctx.close(); pw.stop()
    return {"enriched": n_ok, "failed": n_fail}


if __name__ == "__main__":  # quick manual test: python -m dealscout.flippa_auth [listing_id]
    import sys, json
    logging.basicConfig(level=logging.INFO)
    print("logged in:", is_logged_in())
    if len(sys.argv) > 1:
        from .scan import row_to_listing
        con = db.connect()
        r = con.execute("SELECT * FROM listings WHERE id=?", (sys.argv[1],)).fetchone()
        out = enrich_logged_in(row_to_listing(r))
        print(json.dumps(out.raw.get("gated"), indent=1)[:3000] if out else None)
