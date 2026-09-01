"""IndieExit — WordPress/WooCommerce. Listings are `product` posts: wp/v2/product gives slug, date,
listing-type; wc/store/v1/products gives the asking price. Metrics (MRR, customers, visitors, start
date, overview) are in the server-rendered /listing/<slug>/ HTML. Details fetched only under max_price.
The "Revenue verified" badges are static Elementor widgets on every page -> not trusted."""
import re, html as _html
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "indieexit"
BASE = "https://indieexit.com"
TYPES = {62: "mobile_app", 63: "saas", 64: "marketplace", 65: "marketplace", 66: "saas", 67: "other",
         68: "newsletter", 69: "service", 70: "ecommerce"}
TYPE_NAMES = {62: "Mobile App", 63: "Web Apps", 64: "Directory", 65: "Community", 66: "Micro SaaS",
              67: "Digital Product", 68: "Newsletter", 69: "Service", 70: "E-commerce"}


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    posts = {}
    for page in range(1, 20):
        r = get(http, f"{BASE}/wp-json/wp/v2/product", params={"per_page": 100, "page": page})
        for p in r.json():
            posts[p["id"]] = p
        if page >= int(r.headers.get("x-wp-totalpages", 1)):
            break
    prices = {}
    for page in range(1, 20):
        r = get(http, f"{BASE}/wp-json/wc/store/v1/products", params={"per_page": 100, "page": page})
        for p in r.json():
            pr = p.get("prices") or {}
            unit = int(pr.get("currency_minor_unit") or 0)
            prices[p["id"]] = (money(pr.get("price")) or 0) / (10 ** unit) if pr.get("price") else None
        if page >= int(r.headers.get("x-wp-totalpages", 1)):
            break
    for pid, p in posts.items():
        if not p.get("listing-type"):
            continue  # premium-account / non-listing products
        price = prices.get(pid)
        d = {}
        if price is None or price <= max_price:
            try:
                d = _detail(get(http, p["link"]).text)
            except Exception as e:
                log.warning("indieexit %s: %s", p["link"], e)
        yield _convert(p, price, d)


def _text(page):
    t = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    return re.sub(r"\|[\s|]*", "|", t)


def _before(t, label):
    """IndieExit renders metrics as '|value|label|' (sometimes '|value|+|label|')."""
    m = re.search(r"\|([^|]*)\|(?:\+\|)?" + re.escape(label) + r"\|", t)
    return m.group(1).strip() if m else None


def _after(t, label):
    m = re.search(r"\|" + re.escape(label) + r"\|([^|]*)\|", t)
    return m.group(1).strip() if m else None


def _detail(page):
    t = _text(page)
    d = {}
    m = re.search(r"\|View Startup Details →\|([^|]*)\|", t)
    d["headline"] = m.group(1).strip() if m else ""
    d["annual_revenue"] = _before(t, "Annual Revenue")
    d["asking"] = _before(t, "Asking Price")
    d["model"] = _after(t, "Business Model")
    d["mrr"] = _before(t, "Monthly Revenue")
    d["customers"] = _before(t, "Number of Customers")
    d["visitors"] = _before(t, "Monthly visitors/users")
    d["start"] = _before(t, "Business Start Date")
    d["multiple"] = _before(t, "Asking multiple")
    d["expenses"] = _after(t, "Expenses")
    m = re.search(r"\|Project \|Overview \|(.*?)\|(?:See few FAQs|View More Details|Tech \|Stack)", t, re.S)
    d["overview"] = m.group(1).replace("|", " ").strip() if m else ""
    m = re.search(r"\|Reason For \|Selling \|(.*?)\|(?:Traffic\|Metrics|Revenue\|Metrics|Contact \|Seller)", t, re.S)
    d["reason"] = m.group(1).replace("|", " ").strip() if m else ""
    m = re.search(r"hours per week[^|]*\|([^|]*)\|", t)
    d["hours"] = m.group(1).strip() if m else ""
    m = re.search(r"\|Tech \|Stack\|(.*?)\|Target \|Audience\|", t, re.S)
    d["stack"] = [x for x in m.group(1).split("|") if x.strip()] if m else []
    return d


def _months_since_text(s):
    if not s:
        return None
    m = re.search(r"([A-Za-z]{3})[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if not m:
        return None
    from datetime import datetime
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    mo = months.index(m.group(1).lower()) + 1 if m.group(1).lower() in months else 6
    now = datetime.now()
    return float(max(0, (now.year - int(m.group(2))) * 12 + now.month - mo))


def _convert(p, price, d) -> Listing:
    title = _html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"]))
    tid = (p.get("listing-type") or [None])[0]
    text = f"{title} {d.get('headline','')} {d.get('overview','')} {d.get('model','')}"
    paying, free = extract_customers(text)
    cust = intval(d.get("customers"))
    if cust:
        paying = cust
    vis = intval(d.get("visitors"))
    if vis:
        free = vis
    mrr = money(d.get("mrr"))
    if not mrr and money(d.get("annual_revenue")):
        mrr = money(d["annual_revenue"]) / 12
    exp = money(d.get("expenses"))
    profit = (mrr - exp) if (mrr is not None and exp is not None and exp < mrr) else None
    margin = round(100 * profit / mrr, 1) if profit and mrr else None
    if price is None:
        price = money(d.get("asking"))
    mon = monetization(f"{d.get('model','')} {text}")
    hours = money(d.get("hours")) if d.get("hours") else None
    return Listing(
        id=f"ie:{p['id']}", source=NAME, url=p["link"], title=title[:200],
        category=TYPES.get(tid) or category(title, d.get("model", "")),
        asking_price=price, monthly_profit=profit, monthly_revenue=mrr, margin=margin,
        customers=paying, users_free=free, age_months=_months_since_text(d.get("start")),
        sale_method="classified", status="open" if p.get("status", "publish") == "publish" else "ended",
        reason_for_selling=(d.get("reason") or "")[:500],
        summary=(d.get("overview") or d.get("headline") or "")[:2000], hours_per_week=hours, monetization=mon,
        raw={"listing_type": TYPE_NAMES.get(tid), "listed": p.get("date"), "headline": d.get("headline"),
             "annual_revenue": d.get("annual_revenue"), "business_model": d.get("model"), "expenses": d.get("expenses"),
             "multiple": d.get("multiple"), "start": d.get("start"), "stack": d.get("stack"), "hours_text": d.get("hours")},
    )
