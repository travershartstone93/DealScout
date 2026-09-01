"""BuyMicroStartups — Next.js SSR. /marketplace renders every active card (title, blurb, monthly
revenue/profit, asking, status, category, verified badge); detail pages add founded date, team,
customers, business model, reason for selling. Details fetched only for cards under max_price."""
import re, html as _html
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "buymicrostartups"
BASE = "https://www.buymicrostartups.com"
_CARD = re.compile(r'<a class="[^"]*card-lift[^"]*" href="(/marketplace/([a-z0-9-]+))">(.*?)</a>', re.S)
_H3 = re.compile(r"<h3[^>]*>([^<]*)</h3>")
_P = re.compile(r'<p class="[^"]*line-clamp-2[^"]*">([^<]*)</p>')
_METRIC = re.compile(r'>(Revenue|Profit|Asking)</p><p[^>]*>([^<]*)<')
_STATUS = re.compile(r'rounded-full[^>]*></span>([^<]*)</span>')
_CAT = re.compile(r'<span class="text-muted">([^<]*)</span></div></div></a>?$')


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    max_detail = int(cfg.get("buymicrostartups", {}).get("max_detail", 150))
    h = get(http, f"{BASE}/marketplace").text
    n = 0
    for m in _CARD.finditer(h):
        path, slug, body = m.groups()
        c = _card(path, slug, body)
        d = {}
        if n < max_detail and (c["asking"] is None or c["asking"] <= max_price):
            n += 1
            try:
                d = _detail(get(http, BASE + path).text)
            except Exception as e:
                log.warning("bms %s: %s", path, e)
        yield _convert(c, d)


def _card(path, slug, body):
    metrics = {k: v for k, v in _METRIC.findall(body)}
    verified = "Verified" in body and "Unverified" not in body
    cats = re.findall(r'<span class="text-muted">([^<]*)</span>', body)
    st = _STATUS.search(body)
    return {"url": BASE + path, "slug": slug, "title": _html.unescape(_H3.search(body).group(1)) if _H3.search(body) else slug,
            "blurb": _html.unescape(_P.search(body).group(1)) if _P.search(body) else "",
            "revenue": money(metrics.get("Revenue")), "profit": money(metrics.get("Profit")),
            "asking": money(metrics.get("Asking")), "verified": verified,
            "status": st.group(1).strip() if st else "", "cat": cats[-1] if cats else ""}


def _text(page):
    t = re.sub(r"<(script|style|svg)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    return re.sub(r"\|[\s|]*", "|", t)


def _sect(t, label, stop):
    m = re.search(r"\|" + re.escape(label) + r"\|(.*?)\|(?:" + "|".join(re.escape(s) for s in stop) + r")\|", t, re.S)
    return m.group(1).strip() if m else ""


def _detail(page):
    t = _text(page)
    stops = ["Business Model", "Competitive Landscape", "Reason for Selling", "Related categories", "Growth Opportunities",
             "Traffic", "Customers", "Key Metrics", "Tech Stack", "About the Business", "Highlights"]
    d = {"about": _sect(t, "About the Business", stops), "model": _sect(t, "Business Model", stops),
         "reason": _sect(t, "Reason for Selling", stops)}
    m = re.search(r"\|Founded\|[^|]*\|\(\|([^|]*)\|\)", t)
    d["founded"] = m.group(1).strip() if m else None
    for lab in ("Team", "Audience", "Pricing", "Listed", "Revenue", "Customers", "Users", "MRR", "Annual Revenue", "Annual Profit"):
        m = re.search(r"\|" + lab + r"\|([^|]*)\|", t)
        d[lab.lower()] = m.group(1).strip() if m else None
    m = re.search(r"\|Market\|(.*?)\|(?:Pricing|Tech Stack|About the Business)\|", t, re.S)
    d["market"] = [x for x in m.group(1).split("|") if x.strip()] if m else []
    return d


def _months(founded):
    if not founded:
        return None
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", founded)
    if not m:
        return None
    from datetime import datetime
    months = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
    mo = months.index(m.group(1).lower()) + 1 if m.group(1).lower() in months else 6
    now = datetime.now()
    return float(max(0, (now.year - int(m.group(2))) * 12 + now.month - mo))


def _convert(c, d) -> Listing:
    text = f"{c['title']} {c['blurb']} {d.get('about','')} {d.get('model','')}"
    paying, free = extract_customers(text)
    if d.get("customers"):
        paying = intval(d["customers"]) or paying
    if d.get("users"):
        free = intval(d["users"]) or free
    rev = money(d.get("mrr")) if money(d.get("mrr")) else c["revenue"]
    prof = c["profit"]
    if (prof is None or prof == 0) and money(d.get("annual profit")):
        prof = money(d["annual profit"]) / 12
    margin = round(100 * prof / rev, 1) if prof and rev else None
    st = c["status"].lower()
    status = "open" if st in ("open", "live", "") else "sold"
    mon = monetization(f"{d.get('pricing','')} {d.get('model','')} {text}")
    return Listing(
        id=f"bms:{c['slug'].rsplit('-',1)[-1]}", source=NAME, url=c["url"], title=c["title"][:200],
        category=category(c["cat"], *d.get("market", []), c["title"]),
        asking_price=c["asking"], monthly_profit=prof, monthly_revenue=rev, margin=margin,
        customers=paying, users_free=free, age_months=_months(d.get("founded")),
        verified_revenue=c["verified"], sale_method="classified", status=status,
        reason_for_selling=(d.get("reason") or "")[:500],
        summary=(d.get("about") or c["blurb"])[:2000], monetization=mon,
        raw={"blurb": c["blurb"], "cat": c["cat"], "market": d.get("market"), "team": d.get("team"),
             "audience": d.get("audience"), "pricing": d.get("pricing"), "listed": d.get("listed"),
             "founded": d.get("founded"), "status_text": c["status"], "annual_revenue": d.get("annual revenue"),
             "annual_profit": d.get("annual profit"), "business_model": (d.get("model") or "")[:800]},
    )
