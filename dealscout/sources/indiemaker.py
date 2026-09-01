"""IndieMaker — SSR /browse/ grid sorted price-low, walked until asking price exceeds max_price.
Cards carry price, type, and one vanity metric (MRR / customers / uniques). Revenue details are
behind login, so a capped number of detail pages ([indiemaker].max_detail, default 40) are fetched
only for the description / niche of the most promising cards."""
import re, html as _html
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "indiemaker"
BASE = "https://indiemaker.com"
_CARD = re.compile(r'<a class="ws-card" href="(/listings/[^"]+)">(.*?)</a>', re.S)
_PRICE = re.compile(r'ws-card__price">([^<]*)')
_NAME = re.compile(r'ws-card__name">([^<]*)')
_META = re.compile(r'ws-card__meta">([^<]*)')
_VANITY = re.compile(r'ws-card__vanity">([^<]*)')
_FOOT = re.compile(r'ws-card__foot">([^<]*)')

TYPE_MAP = {"micro-saas": "saas", "saas": "saas", "side-project": "saas", "domain": "domain", "website": "content_site",
            "game": "mobile_app", "mobile app": "mobile_app", "desktop app": "saas", "newsletter": "newsletter",
            "browser extension": "chrome_extension", "ecommerce": "ecommerce", "e-commerce": "ecommerce",
            "content": "content_site", "blog": "content_site", "community": "marketplace", "marketplace": "marketplace"}


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    max_detail = int(cfg.get("indiemaker", {}).get("max_detail", 40))
    cards = []
    for page in range(1, 120):
        try:
            h = get(http, f"{BASE}/browse/", params={"sort": "price_low", "page": page}).text
        except Exception as e:
            log.warning("indiemaker page %s: %s", page, e)
            break
        found = list(_CARD.finditer(h))
        if not found:
            break
        stop = False
        for m in found:
            c = _card(m.group(1), m.group(2))
            if c["price"] is not None and c["price"] > max_price:
                stop = True
                break
            cards.append(c)
        if stop:
            break
    cards.sort(key=lambda c: (c["vanity"] is None, c["age_rank"]))
    for i, c in enumerate(cards):
        detail = None
        if i < max_detail:
            try:
                detail = _detail(get(http, c["url"]).text)
            except Exception as e:
                log.warning("indiemaker %s: %s", c["url"], e)
        yield _convert(c, detail or {})


def _card(path, body):
    g = lambda rx: (rx.search(body).group(1).strip() if rx.search(body) else "")
    price = g(_PRICE)
    meta = _html.unescape(g(_META))
    typ = meta.split("·")[-1].strip() if "·" in meta else ""
    foot = g(_FOOT)
    m = re.match(r"(\d+)\s*(h|d|mo|y|w|m)", foot)
    unit = {"h": 1 / 24, "d": 1, "w": 7, "mo": 30, "m": 1 / 1440, "y": 365}
    age_days = int(m.group(1)) * unit.get(m.group(2), 1) if m else 9999
    return {"url": BASE + path, "slug": path.rsplit("/", 1)[-1], "title": _html.unescape(g(_NAME)),
            "price": money(price) if price.startswith("$") else None, "price_text": price, "type": typ,
            "vanity": g(_VANITY) or None, "age_rank": age_days, "listed_ago": foot}


def _detail(page):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    t = re.sub(r"\|[\s|]*", "|", t)
    d = {}
    m = re.search(r"\|About this listing\|(.*?)\|See the full picture\|", t, re.S)
    d["description"] = m.group(1).strip() if m else ""
    m = re.search(r"\|For sale[^|]*\|[^|]*\|([^|]*)\|(.*?)\|Listed \|([^|]*)\|Asking price", t, re.S)
    if m:
        d["tagline"] = m.group(1).strip()
        d["tags"] = [x.strip() for x in m.group(2).split("|") if x.strip()]
        d["listed"] = m.group(3).strip()
    return d


def _convert(c, d) -> Listing:
    v = (c["vanity"] or "").lower()
    mrr = rev = paying = free = None
    if "mrr" in v:
        mrr = money(v)
    elif "revenue" in v:
        rev = money(v)
    elif "customer" in v:
        paying = intval(v)
    elif "unique" in v or "session" in v or "user" in v or "visit" in v:
        free = intval(v)
    text = f"{c['title']} {d.get('tagline','')} {d.get('description','')}"
    p2, f2 = extract_customers(text)
    paying = paying if paying is not None else p2
    free = free if free is not None else f2
    tags = d.get("tags", [])
    cat = TYPE_MAP.get(c["type"].lower()) or category(c["type"], *tags, c["title"])
    mon = monetization(text)
    if mrr:
        mon = "recurring"
    return Listing(
        id=f"im:{c['slug']}", source=NAME, url=c["url"], title=c["title"][:200], category=cat,
        asking_price=c["price"], monthly_revenue=mrr if mrr is not None else rev,
        customers=paying, users_free=free, sale_method="classified" if c["price"] is not None else "contact",
        status="open", summary=(d.get("description") or d.get("tagline") or "")[:2000], monetization=mon,
        raw={"type": c["type"], "vanity": c["vanity"], "listed_ago": c["listed_ago"], "listed": d.get("listed"),
             "tags": tags, "price_text": c["price_text"]},
    )
