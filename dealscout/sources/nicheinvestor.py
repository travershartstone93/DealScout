"""Niche Investor — WordPress + Estatik (real-estate plugin repurposed for websites). Index at
/listings-all/ (server-rendered cards: title, price, monetization features, niches); detail pages
have overview fields (live since, pageview band, earning band, status) and the prose with real numbers.
Small site (~25 listings) so every under-max_price detail is fetched."""
import re, html as _html
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "nicheinvestor"
BASE = "https://nicheinvestor.com"
_CARD = re.compile(r'class="js-es-listing es-listing[^"]*" data-post-id="(\d+)"(.*?)(?=class="js-es-listing es-listing|<div class="es-pagination|</section>|$)', re.S)
_MONTHLY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(?:k)?\s*(?:/|per|a|each)\s*mo(?:nth)?", re.I)


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    seen = set()
    for page in range(1, 30):
        url = f"{BASE}/listings-all/" if page == 1 else f"{BASE}/listings-all/page/{page}/"
        try:
            h = get(http, url).text
        except Exception as e:
            log.warning("nicheinvestor %s: %s", url, e)
            break
        h = re.sub(r"<svg.*?</svg>", "", h, flags=re.S)
        new = 0
        for m in _CARD.finditer(h):
            c = _card(m.group(1), m.group(2))
            if not c or c["id"] in seen:
                continue
            seen.add(c["id"])
            new += 1
            d = {}
            if c["price"] is None or c["price"] <= max_price:
                try:
                    d = _detail(get(http, c["url"]).text)
                except Exception as e:
                    log.warning("nicheinvestor %s: %s", c["url"], e)
            yield _convert(c, d)
        if new == 0:
            break


def _card(pid, body):
    m = re.search(r'<h3 class="es-listing__title">\s*<a href="([^"]+)"[^>]*>([^<]*)</a>', body)
    if not m:
        return None
    price = re.search(r'es-price">([^<]*)', body)
    feats = re.findall(r'es_feature/[^"]*" rel="tag">([^<]*)', body)
    types = re.findall(r'listing-type/[^"]*" rel="tag">([^<]*)', body)
    labels = re.findall(r'es_label/[^"]*">([^<]*)', body)
    exc = re.search(r'es-excerpt[^>]*>([^<]*)', body)
    return {"id": pid, "url": m.group(1), "title": _html.unescape(m.group(2)).strip(),
            "price": money(price.group(1)) if price else None, "features": [_html.unescape(f) for f in feats],
            "types": [_html.unescape(x) for x in types], "labels": labels,
            "excerpt": _html.unescape(exc.group(1)).strip() if exc else ""}


def _text(page):
    t = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    return re.sub(r"\|[\s|]*", "|", t)


def _ov(t, label):
    m = re.search(r"\|" + re.escape(label) + r"\|:\|([^|]*(?:\|, \|[^|]*)*)\|", t)
    return m.group(1).replace("|, |", ", ").strip() if m else None


def _detail(page):
    t = _text(page)
    d = {"added": _ov(t, "Date added"), "category": _ov(t, "Category"), "status": _ov(t, "Status"),
         "since": _ov(t, "Website live since"), "pageviews": _ov(t, "Pageviews Per Month"),
         "earning": _ov(t, "Earning Per Month")}
    m = re.search(r"\|Description\|:\|(.*?)\|From the Seller\|", t, re.S)
    if not m:
        m = re.search(r"\|Description\|:\|(.*?)\|(?:Similar|Related|Request info)", t, re.S)
    d["description"] = m.group(1).replace("|", " ").strip() if m else ""
    m = re.search(r"\|From the Seller\|(.*?)\|(?:Similar Listings|Related Listings|Similar Properties|Request info)", t, re.S)
    d["qa"] = m.group(1).replace("|", " ").strip() if m else ""
    return d


def _convert(c, d) -> Listing:
    desc = d.get("description", "")
    text = f"{c['title']} {c['excerpt']} {desc}"
    rev = None
    m = _MONTHLY.search(c["title"]) or _MONTHLY.search(desc)
    if m:
        rev = money(m.group(1))
    if rev is None and d.get("earning"):
        e = d["earning"].lower()
        if "under" not in e and "pre" not in e:
            rev = money(e)
    paying, free = extract_customers(text)
    m2 = re.search(r"([\d,]+)\s*(?:sessions|pageviews)", desc, re.I)
    if m2:
        free = intval(m2.group(1))
    elif free is None and d.get("pageviews"):
        free = intval(d["pageviews"]) if "under" not in d["pageviews"].lower() else None
    age = None
    if d.get("since") and re.fullmatch(r"\d{4}", d["since"].strip()):
        from datetime import datetime
        age = float(max(0, (datetime.now().year - int(d["since"])) * 12 + datetime.now().month - 6))
    feats = " ".join(c["features"]).replace("Ad revenue", "display ads")
    st = (d.get("status") or "").lower()
    status = "open" if not st or "available" in st or "open" in st else "sold"
    return Listing(
        id=f"ni:{c['id']}", source=NAME, url=c["url"], title=c["title"][:200],
        category=category(feats, *c["types"], "content_site" if "site" in c["title"].lower() or "blog" in c["title"].lower() else ""),
        asking_price=c["price"], monthly_revenue=rev, monthly_profit=None,
        customers=paying, users_free=free, age_months=age, sale_method="classified", status=status,
        summary=(desc or c["excerpt"])[:2000], monetization=monetization(f"{feats} {text}"),
        raw={"features": c["features"], "niches": c["types"], "labels": c["labels"], "added": d.get("added"),
             "listing_category": d.get("category"), "since": d.get("since"), "pageview_band": d.get("pageviews"),
             "earning_band": d.get("earning"), "qa": (d.get("qa") or "")[:3000]},
    )
