"""Independent verification signals for a listing (ROADMAP B1/B2/B4/B5/B6): domain age, Wayback history,
live-site + stack check, store-page counts cross-checked against the seller's claims, and strict-match
community/seller mentions. Evidence only - never touches the score. Every lookup is wrapped so a failure
becomes an item with value None; collect() never raises.
"""
import json, re, time, logging
from html import unescape as html_unescape
from datetime import datetime, timezone
from urllib.parse import urlparse, quote, quote_plus, unquote, parse_qs

import httpx
from bs4 import BeautifulSoup

from . import db
from .models import Listing
from .normalize import intval

log = logging.getLogger("dealscout")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 15
_last_hit: dict[str, float] = {}          # host -> monotonic time of last request (politeness)

_STORE_HOSTS = ("chromewebstore.google.com", "chrome.google.com", "play.google.com", "apps.apple.com",
                "wordpress.org", "apps.shopify.com", "galaxystore.samsung.com")
_MARKET_HOSTS = ("flippa", "acquire.com", "empireflippers", "motioninvest", "littleexits", "indiemaker",
                 "sideprojectors", "transferslot", "investors.club", "buymicrostartups", "indieexit",
                 "nicheinvestor", "extensiondeal", "webstoreextensions", "exitbid", "lettertrader",
                 "buysellstartups", "microns.io", "duckduckgo", "google.com", "youtube.com", "linktr.ee")
_PARKED = ("domain is for sale", "buy this domain", "this domain may be for sale", "domain parking", "parked free",
           "hugedomains", "afternic", "sedo.com", "dan.com/buy-domain", "godaddy.com/domainsearch", "is for sale!",
           "purchase this domain", "domain has expired", "account suspended", "website coming soon")
_POLICY_RISK = {
    "downloader": ("download", "downloader"), "scraper": ("scrap", "crawler", "extractor", "bulk export"),
    "vpn/proxy": ("vpn", "proxy"), "ai-wrapper": ("chatgpt", "openai", "gpt-", "ai wrapper", "powered by ai", "ai chat"),
    "crypto": ("crypto", "bitcoin", "nft", "web3", "blockchain"), "gambling": ("casino", "betting", "gambling", "forex"),
    "cheating/automation": ("undetectable", "auto-click", "autoclick", "bot for", "automation bot", "quiz answers"),
    "adult": ("adult", "nsfw", "onlyfans"),
}
_NEG = ("scam", "refund", "dead", "broken", "abandoned", "malware", "spam", "fraud", "stolen", "phishing", "banned",
        "removed from", "chargeback")
_POS = ("love", "great", "recommend", "awesome", "excellent", "works well")


# ----------------------------------------------------------------------------- helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item(source, label, value=None, url=None, at=None, level=None, **extra) -> dict:
    d = {"source": source, "label": label, "value": value, "url": url, "at": at, "level": level}
    d.update(extra)
    return d


def _polite(url: str, delay: float):
    host = urlparse(url).hostname or url
    wait = _last_hit.get(host, 0) + delay - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _get(http, url: str, delay: float = 1.0, **kw):
    """Polite GET, 15 s timeout, follows redirects. Raises on transport error (callers wrap)."""
    _polite(url, delay)
    headers = {"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    headers.update(kw.pop("headers", {}))
    try:
        return http.get(url, headers=headers, timeout=TIMEOUT, follow_redirects=True, **kw)
    except httpx.TransportError:  # one retry on timeout / connection reset, shorter budget
        time.sleep(1)
        return http.get(url, headers=headers, timeout=10, follow_redirects=True, **kw)


def _safe(fn, source: str, label: str, *a, **kw) -> list[dict]:
    """Run a lookup; on any exception return one item with value None + the error text."""
    try:
        out = fn(*a, **kw)
        return out if isinstance(out, list) else [out]
    except Exception as e:  # noqa: BLE001 - every source must degrade, never raise
        log.warning("signals %s/%s: %s", source, label, e)
        return [_item(source, label, None, level=None, error=f"{type(e).__name__}: {str(e)[:120]}")]


def _months_ago(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso)[:19].replace("Z", ""))
        return round((datetime.now() - d).days / 30.44, 1)
    except ValueError:
        return None


def _delta_pct(claim, observed) -> int | None:
    if not claim or observed is None:
        return None
    return round(100 * (observed - claim) / claim)


def _first_url(*texts: str, hosts=()) -> str | None:
    for t in texts:
        for m in re.finditer(r"https?://[^\s\"'<>)\]]+", t or ""):
            u = m.group().rstrip(".,;")
            if not hosts or any(h in u for h in hosts):
                return u
    return None


# ----------------------------------------------------------------------------- derivation
def domain_of(l: Listing) -> str | None:
    """Business hostname from raw hints, else from a URL in the summary. None for store/marketplace hosts."""
    cands = [l.raw.get(k) for k in ("hostname", "external_url", "website", "website_url", "domain", "url")]
    cands.append(_first_url(l.summary, l.title))  # the marketplace listing URL itself is never the business
    for c in cands:
        if not c:
            continue
        h = urlparse(c if "//" in str(c) else f"http://{c}").hostname or str(c)
        h = h.lower().removeprefix("www.")
        if "." not in h or any(x in h for x in _MARKET_HOSTS) or any(h.endswith(x) or x in h for x in _STORE_HOSTS):
            continue
        return h
    return None


def store_links(l: Listing) -> dict[str, str]:
    """{kind: url} for chrome/play/appstore/wordpress/shopify pages linked anywhere in the listing."""
    texts = [str(l.raw.get(k) or "") for k in ("external_url", "hostname", "website", "website_url", "url")]
    texts += [l.url or "", l.summary or "", l.title or "", json.dumps(l.raw.get("page") or {})]
    blob = "\n".join(texts)
    out = {}
    pats = {
        "chrome_web_store": r"https?://(?:chromewebstore\.google\.com/detail/[^\s\"'<>)]+|chrome\.google\.com/webstore/detail/[^\s\"'<>)]+)",
        "play": r"https?://play\.google\.com/store/apps/details\?[^\s\"'<>)]+",
        "appstore": r"https?://apps\.apple\.com/[^\s\"'<>)]+/id\d+[^\s\"'<>)]*",
        "wordpress": r"https?://(?:www\.)?wordpress\.org/plugins/[a-z0-9\-]+/?",
        "shopify": r"https?://apps\.shopify\.com/[a-z0-9\-]+/?",
    }
    for kind, pat in pats.items():
        m = re.search(pat, blob, re.I)
        if m:
            out[kind] = m.group().rstrip(".,;)")
    return out


def product_name(l: Listing, store_title: str | None = None) -> str | None:
    """A distinctive product name (>=2 words or contains a domain) or None."""
    cands = []
    if store_title:
        cands.append(store_title)
    t = (l.title or "").strip()
    m = re.match(r"^([A-Z][\w\.\-]*(?:\s+[A-Z][\w\.\-]*){0,3})\s*(?:[-:–—]|is\b|brings\b)", t)
    if m:
        cands.append(m.group(1))
    if len(t.split()) <= 4 and not re.search(r"[\d$%]", t):
        cands.append(t)
    for c in cands:
        c = re.sub(r"\s+", " ", c).strip(" -:")
        if c and (len(c.split()) >= 2 or re.search(r"\.[a-z]{2,}$", c, re.I)):
            return c[:60]
    return None


# ----------------------------------------------------------------------------- identity (B1)
def rdap(http, domain: str, delay) -> list[dict]:
    r = _get(http, f"https://rdap.org/domain/{domain}", delay)
    if r.status_code == 404:
        return [_item("rdap", "domain", domain, level=None, error="not found in RDAP")]
    r.raise_for_status()
    d = r.json()
    ev = {e.get("eventAction"): e.get("eventDate") for e in d.get("events", [])}
    reg = None
    for e in d.get("entities", []):
        if "registrar" in (e.get("roles") or []):
            for f in (e.get("vcardArray") or [None, []])[1]:
                if f[0] == "fn":
                    reg = f[3]
    url = f"https://rdap.org/domain/{domain}"
    created, expires = ev.get("registration"), ev.get("expiration")
    items = [_item("rdap", "domain created", created[:10] if created else None, url, created, months=_months_ago(created)),
             _item("rdap", "domain expires", expires[:10] if expires else None, url, expires),
             _item("rdap", "registrar", reg, url),
             _item("rdap", "status", ", ".join(d.get("status") or [])[:120] or None, url)]
    exp_m = _months_ago(expires)
    if exp_m is not None and exp_m > -2:
        items[1]["level"] = "amber"
    return items


def wayback(http, domain: str, delay) -> list[dict]:
    url = f"https://web.archive.org/cdx/search/cdx?url={domain}&output=json&fl=timestamp&collapse=timestamp:6"
    r = _get(http, url, delay)
    r.raise_for_status()
    rows = r.json()[1:] if r.text.strip() else []
    stamps = sorted(x[0] for x in rows)
    wb = f"https://web.archive.org/web/*/{domain}"
    if not stamps:
        return [_item("wayback", "first snapshot", None, wb, error="no snapshots")]
    first = stamps[0]
    first_iso = f"{first[:4]}-{first[4:6]}-{first[6:8]}"
    cutoff = (datetime.now().year - 2) * 100 + datetime.now().month
    recent = [s for s in stamps if int(s[:6]) >= cutoff]
    last = stamps[-1]
    return [_item("wayback", "first snapshot", first_iso, f"https://web.archive.org/web/{first}/{domain}", first_iso,
                  months=_months_ago(first_iso)),
            _item("wayback", "months with snapshots (last 2 y)", len(recent), wb),
            _item("wayback", "latest snapshot", f"{last[:4]}-{last[4:6]}-{last[6:8]}", f"https://web.archive.org/web/{last}/{domain}")]


def live_check(http, domain: str, delay) -> tuple[list[dict], object]:
    """GET https://<domain>; returns (items, response|None). Response is reused for stack detection."""
    t = time.monotonic()
    err = None
    r = None
    for scheme in ("https", "http"):
        try:
            r = _get(http, f"{scheme}://{domain}/", delay)
            break
        except Exception as e:  # noqa: BLE001
            err = e
    ms = int((time.monotonic() - t) * 1000)
    if r is None:
        return [_item("live", "site reachable", False, f"https://{domain}", level="red",
                      error=f"{type(err).__name__}: {str(err)[:100]}")], None
    soup = BeautifulSoup(r.text[:300000], "lxml")
    title = soup.title.get_text(" ", strip=True)[:120] if soup.title else ""
    text = soup.get_text(" ", strip=True)[:3000].lower()
    parked = any(p in (title.lower() + " " + text) for p in _PARKED) or len(text) < 40 and r.status_code == 200
    final = str(r.url)
    items = [_item("live", "HTTP status", r.status_code, final, level="green" if r.status_code < 400 else "red"),
             _item("live", "load time (ms)", ms, final, level="amber" if ms > 5000 else None),
             _item("live", "title", title or None, final),
             _item("live", "final URL", final, final)]
    if (urlparse(final).hostname or "").removeprefix("www.") != domain:
        items[3]["level"] = "amber"
        items[3]["note"] = "redirects off-domain"
    if parked:
        items.append(_item("live", "parked / for-sale page", True, final, level="red"))
    return items, r


# ----------------------------------------------------------------------------- tech (B5)
_TECH_RULES = [  # (name, kind, regex on headers+html blob)
    ("Cloudflare", "cdn", r"cf-ray|server: cloudflare|__cf_bm|cdn-cgi/"),
    ("Vercel", "hosting", r"x-vercel-id|server: vercel|\.vercel\.app"),
    ("Netlify", "hosting", r"x-nf-request-id|server: netlify|\.netlify\.app"),
    ("Firebase", "hosting/backend", r"firebaseapp\.com|firebase(?:js|-app|io)|gstatic\.com/firebasejs"),
    ("Supabase", "backend", r"supabase\.co|supabase-js"),
    ("AWS", "hosting", r"x-amz-|amazonaws\.com|server: awselb"),
    ("Google Cloud/GFE", "hosting", r"server: gse|server: google frontend|via: 1\.1 google"),
    ("Nginx", "server", r"server: nginx"), ("Apache", "server", r"server: apache"), ("LiteSpeed", "server", r"server: litespeed"),
    ("Next.js", "framework", r"_next/static|x-powered-by: next\.js|__NEXT_DATA__"),
    ("Nuxt", "framework", r"_nuxt/|__NUXT__"),
    ("React", "framework", r"react-dom|data-reactroot|/static/js/main\.[0-9a-f]+\.js"),
    ("Vue", "framework", r"vue(?:\.min)?\.js|data-v-[0-9a-f]{6,8}"),
    ("Angular", "framework", r"ng-version=|angular(?:\.min)?\.js"),
    ("Laravel", "framework", r"laravel_session|x-powered-by: php|csrf-token"),
    ("Django", "framework", r"csrfmiddlewaretoken|x-frame-options: deny.*wsgi"),
    ("Rails", "framework", r"x-powered-by: phusion|csrf-param|_rails_session"),
    ("WordPress", "cms", r"wp-content/|wp-includes/|generator\" content=\"wordpress"),
    ("Webflow", "builder", r"webflow\.(?:com|io)|data-wf-page"),
    ("Bubble", "builder", r"bubble\.io|bubbleapps\.io"),
    ("Wix", "builder", r"wixstatic\.com|wix\.com|x-wix-"),
    ("Squarespace", "builder", r"squarespace\.com|squarespace-cdn"),
    ("Framer", "builder", r"framerusercontent\.com|framer\.com"),
    ("Shopify", "ecommerce", r"cdn\.shopify\.com|x-shopify-stage|myshopify\.com"),
    ("WooCommerce", "ecommerce", r"woocommerce"),
    ("Stripe", "payments", r"js\.stripe\.com|stripe\.com/v3|checkout\.stripe\.com"),
    ("Paddle", "payments", r"cdn\.paddle\.com|paddle\.js"), ("Lemon Squeezy", "payments", r"lemonsqueezy\.com"),
    ("Gumroad", "payments", r"gumroad\.com"), ("PayPal", "payments", r"paypal\.com/sdk|paypalobjects\.com"),
    ("Razorpay", "payments", r"checkout\.razorpay\.com"),
    ("Google Analytics", "analytics", r"googletagmanager\.com|google-analytics\.com|gtag\(|UA-\d{4,}-\d|G-[A-Z0-9]{8,}"),
    ("Plausible", "analytics", r"plausible\.io"), ("Hotjar", "analytics", r"hotjar\.com"), ("Clarity", "analytics", r"clarity\.ms"),
    ("Google AdSense", "ads", r"pagead2\.googlesyndication\.com|adsbygoogle"),
    ("Ezoic", "ads", r"ezoic|ezojs\.com"), ("Mediavine", "ads", r"mediavine"), ("Raptive/AdThrive", "ads", r"adthrive|raptive"),
    ("Monumetric", "ads", r"monumetric"),
    ("OpenAI (hint)", "api", r"openai\.com|api\.openai|gpt-4|gpt-3\.5|chatgpt"),
    ("Anthropic (hint)", "api", r"anthropic\.com|claude-3|claude-sonnet|claude-opus"),
    ("Beehiiv", "newsletter", r"beehiiv\.com"), ("Substack", "newsletter", r"substack\.com|substackcdn"),
    ("Ghost", "cms", r"ghost\.io|ghost\.org|generator\" content=\"ghost"),
    ("Mailchimp", "email", r"list-manage\.com|chimpstatic"), ("ConvertKit/Kit", "email", r"convertkit\.com|kit\.com/forms"),
    ("Intercom", "support", r"intercom\.io|widget\.intercom"), ("Crisp", "support", r"crisp\.chat"), ("Tawk", "support", r"tawk\.to"),
    ("Blogger", "cms", r"blogger\.com|blogspot\.com|generator\" content=\"blogger"),
    ("jQuery", "library", r"jquery(?:\.min)?\.js"), ("Bootstrap", "library", r"bootstrap(?:\.min)?\.(?:css|js)"),
    ("Tailwind", "library", r"tailwindcss|tailwind\.css"),
    ("PHP", "language", r"x-powered-by: php"),
    ("Google Fonts", "assets", r"fonts\.googleapis\.com"),
]


def detect_stack(resp) -> list[dict]:
    """Wappalyzer-lite: header + HTML fingerprints -> tech items (name, kind)."""
    if resp is None:
        return []
    hdr = "\n".join(f"{k.lower()}: {v}" for k, v in resp.headers.items())
    html = resp.text[:400000]
    soup = BeautifulSoup(html, "lxml")
    gen = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    items = []
    if gen and gen.get("content"):
        items.append(_item("tech", "generator", gen["content"][:80], str(resp.url), kind="cms"))
    if resp.headers.get("server"):
        items.append(_item("tech", "server header", resp.headers["server"][:60], str(resp.url), kind="server"))
    if resp.headers.get("x-powered-by"):
        items.append(_item("tech", "x-powered-by", resp.headers["x-powered-by"][:60], str(resp.url), kind="server"))
    hosts = sorted({urlparse(s.get("src")).hostname for s in soup.find_all("script", src=True)
                    if urlparse(s.get("src")).hostname} - {urlparse(str(resp.url)).hostname})
    blob = hdr + "\n" + html
    found = []
    for name, kind, pat in _TECH_RULES:
        if re.search(pat, blob, re.I):
            found.append((name, kind))
    for name, kind in found:
        items.append(_item("tech", kind, name, str(resp.url), kind=kind))
    if hosts:
        items.append(_item("tech", "third-party script hosts", ", ".join(hosts[:12])[:300], str(resp.url), kind="scripts"))
    return items


def policy_risk(l: Listing) -> list[dict]:
    text = f"{l.title} {l.summary} {l.category}".lower()
    hits = []
    for k, kws in _POLICY_RISK.items():
        words = [w for w in kws if w in text]
        if words:
            hits.append(f"{k} ({', '.join(words)})")
    if not hits:
        return [_item("policy", "store-policy risk category", "none detected")]
    return [_item("policy", "store-policy risk category", "; ".join(hits), level="amber",
                  note="categories platforms police (removal / review risk); check current store policy before buying")]


# ----------------------------------------------------------------------------- stores (B2)
def _ld_app(soup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(tag.string or "")
        except ValueError:
            continue
        for n in (d if isinstance(d, list) else [d]):
            if isinstance(n, dict) and "SoftwareApplication" in str(n.get("@type")):
                return n
    return {}


def _rating_items(src, url, agg: dict, txt_rating=None, txt_count=None):
    rating = agg.get("ratingValue") if agg else None
    count = (agg.get("ratingCount") or agg.get("reviewCount")) if agg else None
    if rating in (None, 0, "0") and txt_rating is not None:
        rating = txt_rating
    if count in (None, 0, "0") and txt_count is not None:
        count = txt_count
    try:
        rating = round(float(rating), 2) if rating not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    if rating == 0 and not intval(str(count or 0)):
        rating = None  # store shows no rating yet
    return [_item(src, "rating", rating, url, level="amber" if rating is not None and 0 < rating < 3.5 else None),
            _item(src, "rating count", intval(count) if count not in (None, "") else None, url)]


def _updated_item(src, url, date_str: str | None):
    if not date_str:
        return _item(src, "last updated", None, url)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            d = datetime.strptime(date_str.strip(), fmt)
            m = round((datetime.now() - d).days / 30.44, 1)
            return _item(src, "last updated", d.date().isoformat(), url, d.date().isoformat(),
                         level="amber" if m > 12 else None, months=m)
        except ValueError:
            pass
    return _item(src, "last updated", date_str[:40], url)


def chrome_web_store(http, url: str, delay) -> list[dict]:
    r = _get(http, url, delay)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)
    if "Chrome Web Store" not in txt[:2000] and "chromewebstore" not in r.text[:5000]:
        return [_item("chrome_web_store", "page", None, url, error="not a store page")]
    og = soup.find("meta", property="og:title")
    name = (og["content"].replace(" - Chrome Web Store", "").strip() if og and og.get("content") else None)
    if not name or name.lower() in ("chrome web store", ""):
        return [_item("chrome_web_store", "listing", None, url, level="red", error="item not found / removed from store")]
    m = re.search(r"([\d,\.]+[KkMm]?\+?)\s+users", txt)
    users = intval(m.group(1)) if m else None
    m = re.search(r"\n(\d\.\d)\n\(\n([\d,]+) ratings?\n\)", txt) or re.search(r"(\d\.\d) out of 5\n([\d,]+) ratings?", txt)
    rating, count = (float(m.group(1)), intval(m.group(2))) if m else (None, None)
    m = re.search(r"\nVersion\n([\w\.\-]+)\n", txt)
    version = m.group(1) if m else None
    m = re.search(r"\nUpdated\n([A-Z][a-z]+ \d{1,2}, \d{4})\n", txt)
    updated = m.group(1) if m else None
    m = re.search(r"\nSize\n([\d\.]+\s*[KMG]iB)\n", txt)
    size = m.group(1) if m else None
    featured = "\nFeatured\n" in txt[:6000]
    m = re.search(r"\nDeveloper\n(.+?)\n(?:Website|Email|Phone|Trader|Non-trader)", txt, re.S)
    dev = m.group(1).strip().replace("\n", ", ")[:100] if m else None
    items = [_item("chrome_web_store", "name", name, url),
             _item("chrome_web_store", "users", users, url, level=None if users else "amber")]
    items += _rating_items("chrome_web_store", url, {}, rating, count)
    items += [_updated_item("chrome_web_store", url, updated),
              _item("chrome_web_store", "version", version, url),
              _item("chrome_web_store", "size", size, url),
              _item("chrome_web_store", "featured badge", featured, url, level="green" if featured else None),
              _item("chrome_web_store", "developer", dev, url)]
    return items


def play_store(http, url: str, delay) -> list[dict]:
    if "hl=" not in url:
        url += "&hl=en&gl=us"
    r = _get(http, url, delay)
    if r.status_code == 404:
        return [_item("play", "listing", None, url, level="red", error="app not found on Google Play (removed?)")]
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)
    ld = _ld_app(soup)
    name = ld.get("name") or (soup.title.get_text(strip=True).replace(" - Apps on Google Play", "") if soup.title else None)
    m = re.search(r"\n([\d,\.]+[KMB]?\+?)\nDownloads\n", txt)
    installs = m.group(1) if m else None
    m = re.search(r"\n(\d\.\d)\nstar\n([\d,\.]+[KM]?) reviews\n", txt)
    tr, tc = (float(m.group(1)), m.group(2)) if m else (None, None)
    m = re.search(r"\nUpdated on\n([A-Z][a-z]{2} \d{1,2}, \d{4})\n", txt)
    updated = m.group(1) if m else None
    dev = (ld.get("author") or {}).get("name")
    items = [_item("play", "name", name, url),
             _item("play", "installs", installs, url, level=None if installs else "amber",
                   n=intval(installs.replace("+", "")) if installs else None)]
    items += _rating_items("play", url, ld.get("aggregateRating") or {}, tr, tc)
    items += [_updated_item("play", url, updated),
              _item("play", "developer", dev, url), _item("play", "category", ld.get("applicationCategory"), url)]
    return items


def app_store(http, url: str, delay) -> list[dict]:
    r = _get(http, url, delay)
    if r.status_code == 404:
        return [_item("appstore", "listing", None, url, level="red", error="app not found on the App Store (removed?)")]
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)
    ld = _ld_app(soup)
    name = ld.get("name")
    m = re.search(r"\n(\d\.\d)\n•\n([\d,\.]+[KM]?) Ratings?\n", txt) or re.search(r"(\d\.\d) out of 5\n([\d,\.]+[KM]?) Ratings?", txt)
    tr, tc = (float(m.group(1)), m.group(2)) if m else (None, None)
    m = re.search(r"\nVersion History\n.*?\n(\d[\w\.]*)\n(\d{1,2} [A-Z][a-z]{2}(?: \d{4})?)\n", txt, re.S)
    latest_ver, latest_date = (m.group(1), m.group(2)) if m else (None, None)
    m = re.search(r"\nSize\n([\d\.]+)\n(MB|GB|KB)\n", txt)
    size = f"{m.group(1)} {m.group(2)}" if m else None
    m = re.search(r"\nCategory\n(.+?)\n", txt)
    cat = m.group(1) if m else ld.get("applicationCategory")
    dev = (ld.get("author") or {}).get("name")
    items = [_item("appstore", "name", name, url)]
    items += _rating_items("appstore", url, ld.get("aggregateRating") or {}, tr, tc)
    if items[1]["value"] in (None, 0):
        items[1]["note"] = "not enough ratings to display"
    items += [_item("appstore", "latest version", f"{latest_ver} ({latest_date})" if latest_ver else None, url),
              _item("appstore", "size", size, url), _item("appstore", "category", cat, url),
              _item("appstore", "developer", dev, url),
              _item("appstore", "price", (ld.get("offers") or {}).get("price") if isinstance(ld.get("offers"), dict) else None, url)]
    return items


def wordpress_plugin(http, url: str, delay) -> list[dict]:
    r = _get(http, url, delay)
    if r.status_code == 404:
        return [_item("wordpress", "listing", None, url, level="red", error="plugin not found on wordpress.org")]
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)
    ld = _ld_app(soup)
    m = re.search(r"\nActive installations\n([\d,\.]+\+?(?: million)?)\n", txt)
    installs = m.group(1) if m else None
    m = re.search(r"\nLast updated\n(.+?)\n(?:ago\n)?Active", txt, re.S)
    updated = m.group(1).replace("\n", " ") if m else None
    n = None
    if installs:
        n = intval(installs.replace("+", "").replace(" million", ""))
        if n is not None and "million" in installs:
            n *= 1_000_000
    items = [_item("wordpress", "name", html_unescape(ld.get("name")) if ld.get("name") else None, url),
             _item("wordpress", "active installs", installs, url, n=n)]
    items += _rating_items("wordpress", url, ld.get("aggregateRating") or {})
    items += [_item("wordpress", "last updated", updated, url, ld.get("dateModified")),
              _item("wordpress", "version", ld.get("softwareVersion"), url)]
    return items


def shopify_app(http, url: str, delay) -> list[dict]:
    r = _get(http, url, delay)
    if r.status_code == 404:
        return [_item("shopify", "listing", None, url, level="red", error="app not found on the Shopify App Store")]
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)
    ld = _ld_app(soup)
    m = re.search(r"\nLaunched\n(.+?)\n", txt)
    launched = m.group(1).strip() if m else None
    items = [_item("shopify", "name", ld.get("name"), url)]
    items += _rating_items("shopify", url, ld.get("aggregateRating") or {})
    items += [_item("shopify", "launched", launched, url)]
    return items


_STORE_FN = {"chrome_web_store": chrome_web_store, "play": play_store, "appstore": app_store,
             "wordpress": wordpress_plugin, "shopify": shopify_app}
_STORE_COUNT_LABEL = {"chrome_web_store": "users", "play": "installs", "wordpress": "active installs"}


# ----------------------------------------------------------------------------- community + seller (B4/B6)
def ddg(http, query: str, delay, max_hits: int = 8) -> tuple[list[dict], str | None]:
    """DuckDuckGo HTML results -> ([{title,url,snippet}], error|None). 202 = bot challenge."""
    r = _get(http, f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", delay,
             headers={"Referer": "https://html.duckduckgo.com/"})
    low = r.text[:5000].lower()
    if r.status_code in (202, 403, 429) or "anomaly" in low or "bots use duckduckgo" in low:
        return [], f"ddg blocked/challenged (HTTP {r.status_code})"
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    hits = []
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        href = a.get("href") or ""
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        sn = res.select_one(".result__snippet")
        hits.append({"title": a.get_text(" ", strip=True)[:160], "url": href,
                     "snippet": sn.get_text(" ", strip=True)[:300] if sn else "", "engine": "ddg"})
        if len(hits) >= max_hits:
            break
    return hits, None


def bing(http, query: str, delay, max_hits: int = 8) -> tuple[list[dict], str | None]:
    """Bing HTML results (fallback when DDG challenges). Result links are base64 redirectors - decoded here."""
    import base64
    if not http.cookies.get("MUID", domain=".bing.com") and not getattr(http, "_bing_warm", False):
        http._bing_warm = True
        _get(http, "https://www.bing.com/", delay)  # Bing serves decoy results to cookie-less clients
    r = _get(http, f"https://www.bing.com/search?q={quote(query, safe='')}&form=QBLH&setlang=en&cc=US", delay)
    if r.status_code != 200:
        return [], f"bing HTTP {r.status_code}"
    soup = BeautifulSoup(r.text, "lxml")
    hits = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a:
            continue
        href = a.get("href") or ""
        u = parse_qs(urlparse(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            try:
                href = base64.urlsafe_b64decode(u[2:] + "=" * (-len(u[2:]) % 4)).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        p = li.select_one(".b_caption p") or li.select_one("p")
        hits.append({"title": a.get_text(" ", strip=True)[:160], "url": href,
                     "snippet": p.get_text(" ", strip=True)[:300] if p else "", "engine": "bing"})
        if len(hits) >= max_hits:
            break
    if not hits and ("captcha" in r.text[:5000].lower() or "b_algo" not in r.text):
        return [], "bing returned no result list (blocked or layout change)"
    return hits, None


def google_cse(http, query: str, delay, max_hits: int = 8, api_key: str = "", cx: str = "") -> tuple[list[dict], str | None]:
    """Google Programmable Search JSON API (official; free 100 queries/day). Needs [signals].google_api_key + google_cx."""
    if not api_key or not cx:
        return [], "google: no api key / cx configured"
    try:
        r = http.get("https://www.googleapis.com/customsearch/v1",
                     params={"key": api_key, "cx": cx, "q": query, "num": min(10, max_hits)}, timeout=20)
        time.sleep(delay)
        if r.status_code == 429:
            return [], "google: daily quota exhausted (429)"
        if r.status_code != 200:
            return [], f"google: HTTP {r.status_code} {r.text[:120]}"
        items = r.json().get("items", []) or []
        hits = [{"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", ""), "engine": "google"} for i in items]
        return hits[:max_hits], None
    except Exception as e:  # noqa: BLE001
        return [], f"google: {type(e).__name__}: {e}"


def claude_search(http, query: str, delay, max_hits: int = 8, model: str = "claude-sonnet-5", timeout: int = 150) -> tuple[list[dict], str | None]:
    """Web search through the local Claude Code login (`claude -p --allowedTools WebSearch`). No key needed.
    Every returned URL is HEAD/GET-checked so a hallucinated link can never become evidence."""
    import subprocess, json as _json
    prompt = (f"Search the web for: {query}\nThen return ONLY a JSON array (no prose, no code fence) of up to {max_hits} objects "
              "{\"title\",\"url\",\"snippet\"} taken from the actual search results. Only include URLs that appeared in the results.")
    try:
        p = subprocess.run(["claude", "-p", "--output-format", "json", "--allowedTools", "WebSearch", "--model", model],
                           input=prompt, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return [], f"claude search failed: {p.stderr[:160]}"
        text = _json.loads(p.stdout).get("result", "")
        m = re.search(r"\[.*\]", text, re.S)
        arr = _json.loads(m.group()) if m else []
    except Exception as e:  # noqa: BLE001
        return [], f"claude search: {type(e).__name__}: {e}"
    hits = []
    for h in arr:
        u = str(h.get("url", ""))
        if not u.startswith("http"):
            continue
        try:
            rr = http.get(u, timeout=12, follow_redirects=True)
            alive = rr.status_code < 400
        except Exception:  # noqa: BLE001
            alive = False
        if alive:
            hits.append({"title": str(h.get("title", ""))[:200], "url": u, "snippet": str(h.get("snippet", ""))[:400], "engine": "claude-websearch"})
        time.sleep(0.3)
    return hits, None


def web_search(http, query: str, delay, max_hits: int = 8, cfg: dict | None = None) -> tuple[list[dict], str | None]:
    """Engine order from [signals].engines (default: google if configured, then claude, then ddg, then bing)."""
    sc = (cfg or {}).get("signals", {}) if cfg else {}
    engines = sc.get("engines") or ["google", "claude", "ddg", "bing"]
    errors = []
    for eng in engines:
        if eng == "google":
            hits, err = google_cse(http, query, delay, max_hits, sc.get("google_api_key", ""), sc.get("google_cx", ""))
        elif eng == "claude":
            if not sc.get("claude_search", True):
                continue
            hits, err = claude_search(http, query, delay, max_hits, sc.get("claude_model", "claude-sonnet-5"))
        elif eng == "ddg":
            hits, err = ddg(http, query, delay, max_hits)
        elif eng == "bing":
            hits, err = bing(http, query, delay, max_hits)
        else:
            continue
        if err is None:
            return hits, None
        errors.append(err)
    return [], "; ".join(errors)


def _sentiment(text: str) -> str:
    t = text.lower()
    if any(w in t for w in _NEG):
        return "neg"
    if any(w in t for w in _POS):
        return "pos"
    return "neutral"


def _relevant(hit: dict, domain: str | None, name: str | None, seller: str | None = None) -> bool:
    blob = f"{hit['title']} {hit['snippet']} {hit['url']}".lower()
    if domain and domain.lower() in blob:
        return True
    if name and name.lower() in blob:
        return True
    if seller and seller.lower() in blob:
        return True
    return False


def community(http, l: Listing, domain: str | None, name: str | None, seller: str | None, delay, max_hits=8,
              own_urls=(), max_queries=3) -> list[dict]:
    """Strict-match web mentions. Queries in priority order; DDG challenges after a handful of requests, so capped."""
    queries = []
    if domain:
        queries.append(("domain", f'"{domain}"'))
    if name:
        queries.append(("product name", f'"{name}"'))
    if seller and len(seller.split()) >= 2:
        queries.append(("seller name + flippa", f'"{seller}" flippa'))
    if domain:
        queries.append(("domain on reddit", f'"{domain}" reddit'))
    if name:
        queries.append(("product name on IH/PH/HN", f'"{name}" site:indiehackers.com OR site:producthunt.com OR site:news.ycombinator.com'))
    queries = queries[:max_queries]
    items = []
    seen = set()
    for label, q in queries:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
        try:
            hits, err = web_search(http, q, delay, max_hits=max_hits * 2, cfg=_CFG.get("cfg"))
        except Exception as e:  # noqa: BLE001
            items.append(_item("ddg", label, None, url, query=q, error=f"{type(e).__name__}: {str(e)[:100]}"))
            continue
        if err:
            items.append(_item("ddg", label, None, url, query=q, error=err))
            continue
        kept = []
        for h in hits:
            host = (urlparse(h["url"]).hostname or "").removeprefix("www.")
            if domain and (host == domain or host.endswith("." + domain)):
                continue  # the product's own pages are not community mentions
            if any(x in host for x in ("duckduckgo.com", "flippa.com/", "ipaddress.com", "turboseotools", "hypestat", "site-stats")):
                continue
            if not _relevant(h, domain, name, seller if "seller" in label else None):
                continue
            if h["url"] in seen or any(h["url"].rstrip("/") == o.split("?")[0].rstrip("/") for o in own_urls):
                continue
            seen.add(h["url"])
            h["sentiment"] = _sentiment(f"{h['title']} {h['snippet']}")
            kept.append(h)
            if len(kept) >= max_hits:
                break
        engine = hits[0]["engine"] if hits else "ddg"
        items.append(_item("ddg", label, len(kept), url, query=q, hits=kept, total_results=len(hits), engine=engine))
    return items


# ----------------------------------------------------------------------------- flags + crosschecks
def _crosschecks(l: Listing, groups: dict) -> tuple[list[dict], list[dict]]:
    checks, flags = [], []
    claimed_age = l.age_months
    # domain / wayback age vs claimed age
    for src, label, younger_lvl in (("rdap", "domain created", "red"), ("wayback", "first snapshot", "amber")):
        it = next((i for i in groups["identity"] if i["source"] == src and i["label"] == label and i.get("months") is not None), None)
        if not it or claimed_age is None:
            continue
        obs = it["months"]
        gap = obs - claimed_age
        lvl = None
        if gap < -6:
            lvl = younger_lvl
            flags.append({"level": lvl, "text": f"{'Domain registered' if src == 'rdap' else 'Wayback first snapshot'} {it['value']} "
                                              f"(~{obs:.0f} mo ago) but seller claims ~{claimed_age:.0f} months of age"})
        elif gap > 18 and src == "rdap":
            lvl = "amber"
            flags.append({"level": lvl, "text": f"Domain registered {it['value']} (~{obs:.0f} mo ago) - much older than the claimed "
                                              f"~{claimed_age:.0f} months; check for a previous business/rebrand on Wayback"})
        else:
            lvl = "green"
        if lvl == "green" and src == "rdap":
            flags.append({"level": "green", "text": f"Domain registration {it['value']} is consistent with claimed age (~{claimed_age:.0f} mo)"})
        it["level"] = lvl
        checks.append({"claim": f"age {claimed_age:.0f} months", "observed": f"{src}: {it['value']} (~{obs:.0f} mo)",
                       "delta_pct": _delta_pct(claimed_age, obs), "level": lvl})
    # store counts vs claimed users/customers
    for it in groups["audience"]:
        if it["label"] != _STORE_COUNT_LABEL.get(it["source"]) or it["value"] is None:
            continue
        obs = it.get("n") if it.get("n") is not None else (it["value"] if isinstance(it["value"], (int, float)) else intval(str(it["value"])))
        if obs is None:
            continue
        claim_field, claim = ("users_free", l.users_free) if l.users_free else ("customers", l.customers)
        if not claim:
            continue
        d = _delta_pct(claim, obs)
        approx = isinstance(it["value"], str) and "+" in it["value"] or it["source"] == "chrome_web_store"
        if d is not None and d < -25:
            lvl = "red"
        elif d is not None and d < -10 and not approx:
            lvl = "amber"
        elif d is not None and d < -10 and approx and d >= -50:
            lvl = "amber"
        else:
            lvl = "green"
        it["level"] = lvl if lvl != "green" else it.get("level") or "green"
        obs_txt = f"{it['source'].replace('_', ' ')}: {it['value']:,} {it['label']}" if isinstance(it["value"], int) else f"{it['source'].replace('_', ' ')}: {it['value']} {it['label']}"
        checks.append({"claim": f"{claim:,} {claim_field.replace('_', ' ')}", "observed": obs_txt, "delta_pct": d, "level": lvl})
        note = " (store rounds down to a bucket)" if approx else ""
        if lvl == "red":
            flags.append({"level": "red", "text": f"Seller claims {claim:,} {claim_field.replace('_', ' ')}, store shows {it['value']} ({d:+d}%){note}"})
        elif lvl == "amber":
            flags.append({"level": "amber", "text": f"Store shows {it['value']} vs claimed {claim:,} ({d:+d}%){note}"})
        else:
            flags.append({"level": "green", "text": f"Store count {it['value']} {it['label']} matches claimed {claim:,} ({d:+d}%){note}"})
    return checks, flags


def _summarize_flags(l: Listing, groups: dict, name: str | None) -> list[dict]:
    flags = []
    idn = groups["identity"]
    live = {i["label"]: i for i in idn if i["source"] == "live"}
    if live.get("site reachable") and live["site reachable"]["value"] is False:
        flags.append({"level": "red", "text": f"Site did not respond: {live['site reachable'].get('error', '')}"})
    if live.get("parked / for-sale page"):
        flags.append({"level": "red", "text": "Homepage looks like a parked / for-sale / suspended page"})
    if live.get("HTTP status") and live["HTTP status"]["value"] and live["HTTP status"]["value"] >= 400:
        flags.append({"level": "red", "text": f"Homepage returns HTTP {live['HTTP status']['value']}"})
    if live.get("final URL", {}).get("note"):
        flags.append({"level": "amber", "text": f"Homepage redirects off-domain to {live['final URL']['value']}"})
    if live.get("load time (ms)", {}).get("level") == "amber":
        flags.append({"level": "amber", "text": f"Slow homepage: {live['load time (ms)']['value']} ms"})
    exp = next((i for i in idn if i["source"] == "rdap" and i["label"] == "domain expires"), None)
    if exp and exp.get("level") == "amber":
        flags.append({"level": "amber", "text": f"Domain expires {exp['value']} (within ~2 months) - make sure renewal is part of the deal"})
    wb = next((i for i in idn if i["source"] == "wayback" and i["label"] == "months with snapshots (last 2 y)"), None)
    if wb and wb["value"] == 0:
        flags.append({"level": "amber", "text": "No Wayback snapshots in the last 2 years (low visibility / new domain)"})
    for it in groups["audience"]:
        if it.get("error") and it.get("level") == "red":
            flags.append({"level": "red", "text": f"{it['source'].replace('_', ' ')}: {it['error']}"})
        elif it["label"] == "rating" and it.get("level") == "amber":
            flags.append({"level": "amber", "text": f"{it['source'].replace('_', ' ')} rating {it['value']}/5"})
        elif it["label"] == "last updated" and it.get("level") == "amber":
            flags.append({"level": "amber", "text": f"{it['source'].replace('_', ' ')} last updated {it['value']} (>12 months ago)"})
        elif it["label"] == "featured badge" and it["value"]:
            flags.append({"level": "green", "text": "Chrome Web Store 'Featured' badge (Google-reviewed)"})
    for it in groups["tech"]:
        if it["source"] == "policy" and it.get("level") == "amber":
            flags.append({"level": "amber", "text": f"Store-policy risk category: {it['value']}"})
    api = [i["value"] for i in groups["tech"] if i.get("kind") == "api"]
    if api:
        flags.append({"level": "amber", "text": f"Third-party AI API dependency hinted on homepage: {', '.join(api)} (single-supplier + monthly cost)"})
    neg = sum(1 for i in groups["community"] for h in (i.get("hits") or []) if h.get("sentiment") == "neg")
    if neg:
        flags.append({"level": "amber", "text": f"Negative-sounding mentions found ({neg}) - read them, do not trust the keyword tag"})
    blocked = [i for i in groups["community"] if i.get("error")]
    if blocked and len(blocked) == len(groups["community"]) and blocked:
        flags.append({"level": None, "text": f"Web search unavailable ({blocked[0]['error']})"})
    if not groups["identity"] and not groups["audience"]:
        flags.append({"level": "amber", "text": "No domain or store page found in the listing - nothing could be verified independently"})
    return flags


# ----------------------------------------------------------------------------- public API
_CFG: dict = {}


def collect(listing: Listing, http, cfg: dict) -> dict:
    _CFG["cfg"] = cfg
    """Run every free lookup for one listing and return the signals dict (never raises)."""
    scfg = (cfg or {}).get("signals", {})
    delay = float(scfg.get("delay", 1.0))
    max_hits = int(scfg.get("max_hits", 8))
    groups = {"identity": [], "audience": [], "seller": [], "tech": [], "community": []}
    domain = domain_of(listing)
    stores = store_links(listing)
    seller = ((listing.raw.get("page") or {}).get("seller") or {}).get("name") or listing.raw.get("seller_name")
    resp = None

    if domain:
        groups["identity"].append(_item("derived", "domain", domain, f"https://{domain}"))
        groups["identity"] += _safe(rdap, "rdap", "domain created", http, domain, delay)
        groups["identity"] += _safe(wayback, "wayback", "first snapshot", http, domain, delay)
        try:
            live_items, resp = live_check(http, domain, delay)
        except Exception as e:  # noqa: BLE001
            live_items = [_item("live", "site reachable", None, f"https://{domain}", error=str(e)[:120])]
        groups["identity"] += live_items
        groups["tech"] += _safe(detect_stack, "tech", "stack", resp)
    else:
        groups["identity"].append(_item("derived", "domain", None, note="no business domain found in listing"))

    store_name = None
    for kind, url in stores.items():
        items = _safe(_STORE_FN[kind], kind, "listing", http, url, delay)
        groups["audience"] += items
        if not store_name:
            store_name = next((i["value"] for i in items if i["label"] == "name" and i["value"]), None)
    if not stores and listing.category in ("chrome_extension", "mobile_app"):
        groups["audience"].append(_item("derived", "store page", None, level="amber",
                                        note="listing has no store link; ask the seller for the exact store URL"))

    groups["tech"] += policy_risk(listing)

    if seller:
        groups["seller"].append(_item("marketplace", "seller name", seller, listing.url))
        pg = (listing.raw.get("page") or {}).get("seller") or {}
        for k, lab in (("positive_feedback_pct", "positive feedback %"), ("transactions", "transactions"),
                       ("transactions_total", "transactions total $"), ("verification_complete", "identity verified")):
            if pg.get(k) is not None:
                groups["seller"].append(_item("marketplace", lab, pg[k], listing.url))

    name = product_name(listing, store_name)
    if scfg.get("web_search", True):
        groups["community"] += _safe(community, "ddg", "search", http, listing, domain, name, seller,
                                     max(delay, 2.0), max_hits, tuple(stores.values()), int(scfg.get("max_queries", 3)))
        for it in groups["community"]:
            if "seller" in (it.get("label") or ""):
                groups["seller"].append(it)
        groups["community"] = [i for i in groups["community"] if "seller" not in (i.get("label") or "")]

    checks, xflags = _crosschecks(listing, groups)
    flags = xflags + _summarize_flags(listing, groups, name)
    return {"collected_at": _now(), "domain": domain, "product_name": name, "store_links": stores,
            "groups": groups, "flags": flags, "crosschecks": checks}


def collect_and_store(con, listing: Listing, http, cfg: dict) -> dict:
    """collect() and upsert into the signals table."""
    data = collect(listing, http, cfg)
    con.execute("INSERT INTO signals (listing_id, collected_at, json) VALUES (?,?,?) ON CONFLICT(listing_id) DO UPDATE SET "
                "collected_at=excluded.collected_at, json=excluded.json",
                (listing.id, data["collected_at"], json.dumps(data, default=str)))
    con.commit()
    return data


def get_signals(con, listing_id: str) -> dict | None:
    r = con.execute("SELECT json FROM signals WHERE listing_id=?", (listing_id,)).fetchone()
    return json.loads(r["json"]) if r else None


def _main(argv):
    """python -m dealscout.signals <listing id or title fragment> [--no-store] [--json]"""
    import sys
    from . import load_config
    from .scan import row_to_listing
    from .sources.base import client
    if not argv:
        print(_main.__doc__); return
    key = argv[0]
    con = db.connect()
    r = con.execute("SELECT * FROM listings WHERE id=? OR title LIKE ? ORDER BY score DESC LIMIT 1", (key, f"%{key}%")).fetchone()
    if not r:
        print("no such listing"); return
    l = row_to_listing(r)
    t = time.monotonic()
    with client() as http:
        data = collect(l, http, load_config()) if "--no-store" in argv else collect_and_store(con, l, http, load_config())
    secs = time.monotonic() - t
    if "--json" in argv:
        print(json.dumps(data, indent=1, default=str)); return
    print(f"{l.id}  {l.title[:70]}\n  domain={data['domain']} name={data['product_name']} stores={list(data['store_links'])}  ({secs:.1f}s)")
    for g, items in data["groups"].items():
        print(f"  [{g}]")
        for i in items:
            extra = f"  !{i['error']}" if i.get("error") else (f"  [{i.get('engine')} {i.get('total_results')} raw]" if i.get("query") else "")
            v = i["value"]
            print(f"    {i['source']:16s} {i['label']:32s} {str(v)[:70]:70s} {i.get('level') or '':5s}{extra}")
            for h in i.get("hits") or []:
                print(f"        - [{h['sentiment']}] {h['title'][:60]}  {h['url'][:70]}")
    print("  [crosschecks]")
    for c in data["crosschecks"]:
        print(f"    {c['level'] or '-':5s} {c['claim']} vs {c['observed']} ({c['delta_pct']}%)")
    print("  [flags]")
    for f in data["flags"]:
        print(f"    {f['level'] or '-':5s} {f['text']}")
    sys.stdout.flush()


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING)
    _main(sys.argv[1:])
