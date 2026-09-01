"""Turn messy marketplace text/numbers into the normalized Listing fields."""
import re
from datetime import datetime, timezone

_NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def money(v) -> float | None:
    """'$1.2k' / '12,000' / 12000 / '1.5M' -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(",", "").replace("$", "").replace("usd", "").strip()
    m = _NUM.search(s)
    if not m:
        return None
    n = float(m.group())
    tail = s[m.end():m.end() + 2]
    if tail.startswith("k"):
        n *= 1_000
    elif tail.startswith("m"):
        n *= 1_000_000
    return n


def intval(v) -> int | None:
    n = money(v)
    return int(n) if n is not None else None


def months_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - d).days / 30.44, 1)
    except ValueError:
        return None


CATEGORY_MAP = {
    "saas": "saas", "saas_and_software_project": "saas", "ai_apps_and_tools": "saas",
    "software": "saas", "web app": "saas", "online web tool": "saas", "tool": "saas",
    "chrome_extension": "chrome_extension", "extension": "chrome_extension", "browser extension": "chrome_extension",
    "android_app_v2": "mobile_app", "ios_app_v2": "mobile_app", "mobile app": "mobile_app", "app": "mobile_app",
    "newsletter": "newsletter", "beehiiv": "newsletter",
    "content_site": "content_site", "content": "content_site", "blog": "content_site", "display advertising": "content_site",
    "ecommerce_store": "ecommerce", "ecommerce": "ecommerce", "shopify": "ecommerce", "dropshipping": "ecommerce",
    "fba_storefront_v2": "fba", "amazon fba": "fba", "amazon": "fba",
    "service": "service", "agency": "service", "domain_v2": "domain", "domain": "domain",
    "marketplace": "marketplace",
}


def category(*hints: str) -> str:
    for h in hints:
        if not h:
            continue
        key = str(h).strip().lower()
        if key in CATEGORY_MAP:
            return CATEGORY_MAP[key]
        for k, v in CATEGORY_MAP.items():
            if k in key:
                return v
    return "other"


_CUST_PATTERNS = [
    re.compile(r"(?<![$\d.,])(\d[\d,\.]*\s*[km]?)\s*\+?\s*(?:paying|paid|premium|pro|active paying)\s+(?:customers|users|subscribers|subs|members|clients|sales)", re.I),
    re.compile(r"(?<![$\d.,])(\d[\d,\.]*\s*[km]?)\s*\+?\s*(?:[a-z]+\s)?(?:customers|subscribers|paid subscriptions|paying users|licenses|licences|clients)\b", re.I),
    re.compile(r"(?<![$\d.,])(\d[\d,\.]*\s*[km]?)\s*\+?\s*(?:sales|purchases|orders)\b", re.I),
]
_FREE_PATTERNS = [
    re.compile(r"(?<![$\d.,])(\d[\d,\.]*\s*[km]?)\s*\+?\s*(?:active users|users|installs|downloads|weekly users|members|readers)", re.I),
]


def extract_customers(text: str) -> tuple[int | None, int | None]:
    """Best-effort (paying_customers, free_users) from listing prose."""
    if not text:
        return None, None
    paying = free = None
    for p in _CUST_PATTERNS:
        m = p.search(text)
        if m:
            paying = intval(m.group(1))
            break
    for p in _FREE_PATTERNS:
        m = p.search(text)
        if m:
            free = intval(m.group(1))
            break
    return paying, free


def monetization(text: str) -> str:
    t = (text or "").lower()
    rec = any(k in t for k in ("subscription", "recurring", "mrr", "monthly plan", "saas", "membership"))
    one = any(k in t for k in ("one-time", "one time", "lifetime", "single purchase", "one-off"))
    ads = any(k in t for k in ("adsense", "display ads", "advertising", "ezoic", "mediavine", "sponsorship"))
    hits = [n for n, f in (("recurring", rec), ("one_off", one), ("ads", ads)) if f]
    if len(hits) > 1:
        return "mixed"
    return hits[0] if hits else ""
