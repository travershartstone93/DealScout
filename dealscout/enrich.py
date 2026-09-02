"""Second-pass enrichment: fetch a listing's public HTML page and fill fields the list API leaves blank.

Flippa (verified 2026-08-17): listing pages are server-rendered Rails HTML, no challenge on plain GET.
Logged-out, the page exposes: a "key metrics" strip (label/value pairs such as Site Age, Monthly Profit,
Profit Margin, Page Views, Total Active Subscribers, Overall Churn / Churn Rate, Monthly Downloads,
Store Rating, ...), the bid box (starting bid, bid count, reserve, days left, Buy It Now / asking price,
"Reduced NN%"), the "Verified Listing" badge, the "Data Verified Listing" integrations list (Google
Analytics, Stripe, Google AdSense, ...), the "About the Business" description, the seller card (name,
location, feedback %, transactions) and payment methods. ld+json carries Product/Offer with
availability (InStock vs SoldOut). The detailed sections (revenue_and_expenses, traffic_insights,
monetization_methods, reason for sale, ...) are behind login and NOT in the HTML.
"""
import json, re, logging
from dataclasses import replace
from bs4 import BeautifulSoup

from . import db
from .models import Listing
from .normalize import money, intval, extract_customers, monetization as _monetization
from .score import evaluate
from .sources.base import get, is_challenge

log = logging.getLogger("dealscout")

# key-metric labels (lowercased) -> Listing field
_CUSTOMER_LABELS = ("total active subscribers", "active subscribers", "paying customers", "paying users",
                    "customers", "subscribers", "paid subscribers", "active customers", "clients")
_FREE_LABELS = ("monthly active users", "active users", "total users", "registered users", "users",
                "total downloads", "monthly downloads", "installs", "total installs")
_CHURN_LABELS = ("overall churn", "churn rate", "churn")
_REV_INTEGRATIONS = ("stripe", "paypal", "shopify", "app store", "google play", "woocommerce", "paddle",
                     "chargebee", "quickbooks", "xero", "amazon", "adsense")
_TRAFFIC_INTEGRATIONS = ("google analytics", "google search console", "semrush", "similarweb")

_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)?\s*(?:\d+(?:\.\d+)?)?\s*(?:hours?|hrs?)\s*(?:per|a|/|each)\s*week", re.I)
_REASON = re.compile(
    r"(?:reason(?:s)? for (?:selling|sale|the sale)|why (?:am i|are we|i am|we are) selling|why sell(?:ing)?)"
    r"\s*[:\--?]*\s*(.{20,400}?)(?:\n|$|(?<=[.!])\s)", re.I | re.S)
_REASON_SENTENCE = re.compile(
    r"([^.\n]{0,160}\b(?:(?:i'?m|i am|we'?re|we are|owner is|seller is|it is|is being|being) sold|"
    r"selling (?:this|it|because|as|since|due|to)|reason for (?:the )?sale|decided to sell|moving on to|"
    r"focus(?:ing)? on (?:other|new|my)|no longer have (?:the )?time|lack of time)\b[^.\n]{0,220}[.!]?)", re.I)
_TEMPLATE_DESC = ("this business overview should emphasize", "information about customer demographics is crucial")


def _age_months(s: str) -> float | None:
    s = s.lower()
    y = re.search(r"(\d+(?:\.\d+)?)\s*(?:year|yr)", s)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:month|mo)\b", s)
    d = re.search(r"(\d+)\s*(?:day|week)", s)
    if not (y or m or d):
        return None
    n = 0.0
    if y:
        n += float(y.group(1)) * 12
    if m:
        n += float(m.group(1))
    if d and not (y or m):
        n = 0.5
    return round(n, 1)


def _signed_money(s: str) -> float | None:
    v = money(s)
    if v is not None and s.strip().startswith("-"):
        v = -v
    return v


def _pct(s: str) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*\s*%", s or "")
    return money(m.group()) if m else None


def parse_flippa_page(html: str) -> dict:
    """Pull everything useful out of a Flippa listing page. Returns {} if the page isn't a listing."""
    soup = BeautifulSoup(html, "lxml")
    out: dict = {}

    # --- ld+json (Product / Offer) ---
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except ValueError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for n in nodes or []:
            if isinstance(n, dict) and n.get("@type") == "Product":
                offer = n.get("offers") or {}
                out["ld"] = {"name": n.get("name"), "category": n.get("category"), "price": money(offer.get("price")),
                             "availability": (offer.get("availability") or "").rsplit("/", 1)[-1],
                             "description": n.get("description")}
    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""
    out["page_title"] = title_tag

    # --- key metrics strip: <div rel="tooltip"><span>Label</span><div>Value</div></div> ---
    metrics = {}
    for box in soup.select('div[rel="tooltip"]'):
        span = box.find("span")
        val = box.find("div")
        if span and val:
            label = span.get_text(" ", strip=True)
            value = val.get_text(" ", strip=True)
            if label and value and len(label) < 40 and label not in metrics:
                metrics[label] = value
    if not metrics and "flippa" not in html[:3000].lower():
        return {}
    out["metrics"] = metrics

    # --- description ("About the Business") ---
    desc = ""
    for hdr in soup.find_all(string=re.compile(r"^\s*About the Business\s*$")):
        card = hdr.find_parent(attrs={"data-controller": "toggle-class"})
        if card:
            bodies = [b for b in card.select('[data-toggle-class-target="element"]') if b.name != "hr"]
            if bodies:
                desc = bodies[-1].get_text("\n", strip=True)
                break
    desc = re.sub(r"\n{3,}", "\n\n", desc).strip()
    if any(t in desc.lower() for t in _TEMPLATE_DESC):
        out["description_is_template"] = True
        desc = ""
    out["description"] = desc

    # --- text of the main listing region (between "SELLER GUIDE" and the buying-advice / similar section) ---
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    start = text.find("SELLER GUIDE")
    end = -1
    for marker in ("Similar businesses to this", "Buying Advice", "Community Watch"):
        j = text.find(marker, max(start, 0))
        if j != -1 and (end == -1 or j < end):
            end = j
    main = text[start if start != -1 else 0: end if end != -1 else None]
    out["main_text"] = main

    # --- header line & badges ---
    out["verified_listing"] = "Verified Listing" in main
    out["data_verified"] = "Data Verified Listing" in main
    ints = []
    m = re.search(r"connected the following data integrations:\n(.*?)\n(?:Learn More|About the seller)", main, re.S)
    if m:
        ints = [x.strip() for x in m.group(1).split("\n") if x.strip()]
    out["integrations"] = ints
    m = re.search(r"^Business Location\n(.+)$", main, re.M)
    if m:
        out["location"] = m.group(1).strip()

    # --- bid box / price ---
    bid = {}
    m = re.search(r"Starting Bid Price\n(USD \$[\d,\.]+)", main)
    if m:
        bid["starting_bid"] = money(m.group(1))
    m = re.search(r"(?:Highest Bid|Current Bid)[^\n]*\n(USD \$[\d,\.]+)", main)
    if m:
        bid["highest_bid"] = money(m.group(1))
    m = re.search(r"\n(No Bids|(\d+) bids?)\n", main)
    if m:
        bid["bid_count"] = int(m.group(2)) if m.group(2) else 0
    if "Reserve not met" in main:
        bid["reserve_met"] = False
    elif "Reserve Met" in main or "Reserve met" in main:
        bid["reserve_met"] = True
    m = re.search(r"Reserve:\s*(USD \$[\d,\.]+)", main)
    if m:
        bid["reserve"] = money(m.group(1))
    m = re.search(r"(\d+)\s+(day|hour|minute)s? left", main)
    if m:
        bid["time_left"] = f"{m.group(1)} {m.group(2)}s"
    m = re.search(r"Buy It Now for (USD \$[\d,\.]+)", main)
    if m:
        bid["buy_it_now"] = money(m.group(1))
    m = re.search(r"Asking Price[^\n]*\n(USD \$[\d,\.]+)(?:\n(USD \$[\d,\.]+))?", main)
    if m:
        prices = [money(x) for x in m.groups() if x]
        bid["asking_price"] = prices[-1]
        if len(prices) == 2:
            bid["original_price"] = prices[0]
    m = re.search(r"Reduced (\d+)%", main)
    if m:
        bid["reduced_pct"] = int(m.group(1))
    out["bid"] = bid
    m = re.search(r"\n(\d[\d,]*)\nComments\n(\d[\d,]*)\nViews\n(\d[\d,]*)\nWatchers", main)
    if m:
        out["engagement"] = {"comments": intval(m.group(1)), "views": intval(m.group(2)), "watchers": intval(m.group(3))}

    # --- seller ---
    seller = {}
    m = re.search(r"About the seller\n(.+?)\n", main)
    if m:
        seller["name"] = m.group(1).strip()
    m = re.search(r"([\d\.]+)%\npositive feedback", main)
    if m:
        seller["positive_feedback_pct"] = float(m.group(1))
    m = re.search(r"(\d+) transactions? totalling (USD \$[\d,\.]+)", main)
    if m:
        seller["transactions"] = int(m.group(1))
        seller["transactions_total"] = money(m.group(2))
    seller["verification_complete"] = "Verification Complete" in main
    m = re.search(r"Payment Methods\n(.*?)(?:\n\n|\nSimilar|\nBuying|$)", main, re.S)
    if m:
        seller["payment_methods"] = [x for x in m.group(1).split("\n") if x and len(x) < 30][:4]
    out["seller"] = seller

    # --- status ---
    avail = (out.get("ld") or {}).get("availability", "")
    if avail == "SoldOut" or "Sold on Flippa" in title_tag:
        out["status"] = "sold"
    elif avail and avail != "InStock":
        out["status"] = "ended"
    elif re.search(r"(?:this )?(?:listing|auction) (?:has )?(?:ended|expired|closed)|no longer (?:available|for sale)", main, re.I):
        out["status"] = "ended"
    return out


def _apply_flippa(l: Listing, page: dict) -> Listing:
    metrics = page.get("metrics", {})
    lm = {k.lower(): v for k, v in metrics.items()}
    upd: dict = {}
    raw = dict(l.raw)

    def metric(labels):
        for lab in labels:
            if lab in lm:
                return lm[lab]
        return None

    desc = page.get("description") or ""
    text_for_counts = f"{l.title}\n{desc}"

    # customers / free users
    v = metric(_CUSTOMER_LABELS)
    cust = intval(v) if v else None
    v = metric(_FREE_LABELS)
    free = intval(v) if v else None
    p_cust, p_free = extract_customers(text_for_counts)
    if cust is None:
        cust = p_cust
    if free is None:
        free = p_free
    if cust is not None and (l.customers is None or cust > l.customers):
        upd["customers"] = cust
    if free is not None and l.users_free is None:
        upd["users_free"] = free

    # financials (fill only)
    if l.monthly_profit is None and "monthly profit" in lm:
        v = _signed_money(lm["monthly profit"])
        if v is not None:
            upd["monthly_profit"] = v
    if l.monthly_revenue is None and "monthly revenue" in lm:
        v = _signed_money(lm["monthly revenue"])
        if v is not None:
            upd["monthly_revenue"] = v
    if l.margin is None and "profit margin" in lm:
        v = _pct(lm["profit margin"])
        if v is not None:
            upd["margin"] = v
    if l.asking_price is None:
        b = page.get("bid", {})
        p = b.get("asking_price") or b.get("buy_it_now") or (page.get("ld") or {}).get("price")
        if p:
            upd["asking_price"] = p

    # age / churn
    if l.age_months is None and "site age" in lm:
        a = _age_months(lm["site age"])
        if a is not None:
            upd["age_months"] = a
    v = metric(_CHURN_LABELS)
    if v is not None and l.churn_pct is None:
        c = _pct(v)
        if c is not None:
            upd["churn_pct"] = c

    # verified flags (never unset)
    ints = [i.lower() for i in page.get("integrations", [])]
    if not l.verified_revenue and any(k in i for i in ints for k in _REV_INTEGRATIONS):
        upd["verified_revenue"] = True
    if not l.verified_traffic and any(k in i for i in ints for k in _TRAFFIC_INTEGRATIONS):
        upd["verified_traffic"] = True

    # monetization
    if not l.monetization:
        mon = _monetization(text_for_counts)
        if not mon:
            if metric(("total active subscribers", "active subscribers")) or "monthly recurring revenue" in lm:
                mon = "recurring"
            elif any("adsense" in i or "ezoic" in i or "mediavine" in i for i in ints):
                mon = "ads"
        if mon:
            upd["monetization"] = mon

    # hours / reason
    if l.hours_per_week is None:
        m = _HOURS.search(desc)
        if m:
            upd["hours_per_week"] = float(m.group(1))
    if not l.reason_for_selling and desc:
        m = _REASON.search(desc) or _REASON_SENTENCE.search(desc)
        if m:
            upd["reason_for_selling"] = m.group(1).strip()[:400]

    # summary: seller's full description if longer than what we have
    if desc and len(desc) > len(l.summary or ""):
        upd["summary"] = desc[:3000]

    # status
    st = page.get("status")
    if st in ("sold", "ended") and l.status == "open":
        upd["status"] = st

    raw["page"] = {k: page.get(k) for k in ("metrics", "integrations", "verified_listing", "data_verified", "location",
                                            "bid", "seller", "engagement", "description_is_template", "status")}
    raw["page"]["ld_availability"] = (page.get("ld") or {}).get("availability")
    raw["enriched"] = db.now()
    upd["raw"] = raw
    return replace(l, **upd)


def enrich(listing: Listing, http) -> Listing | None:
    """Fetch the listing's page and return an updated copy, or None if unsupported / failed / empty."""
    if listing.source != "flippa" or not listing.url:
        return None
    try:
        r = get(http, listing.url, headers={"Accept": "text/html,*/*;q=0.8"})
    except Exception as e:
        log.warning("enrich %s: %s", listing.id, e)
        return None
    if is_challenge(r.text):
        log.warning("enrich %s: challenge page", listing.id)
        return None
    page = parse_flippa_page(r.text)
    if not page:
        return None
    return _apply_flippa(listing, page)


def enrich_batch(con, cfg: dict, http, limit: int | None = None) -> dict:
    """Enrich passing, open flippa rows that were never enriched (or changed since). Writes back via db.upsert."""
    from .scan import row_to_listing
    if limit is None:
        limit = cfg.get("enrich", {}).get("max_per_scan", 60)
    rows = con.execute(
        "SELECT * FROM listings WHERE source='flippa' AND status='open' AND passes=1 AND hidden=0 "
        "AND (json_extract(raw_json,'$.enriched') IS NULL OR json_extract(raw_json,'$.enriched_hash') IS NOT content_hash) "
        "ORDER BY score DESC LIMIT ?", (limit,)).fetchall()
    n_ok = n_fail = 0
    for r in rows:
        l = row_to_listing(r)
        new = enrich(l, http)
        if new is None:
            n_fail += 1
            raw = dict(l.raw)
            raw["enriched"] = db.now()
            raw["enrich_error"] = True
            new = replace(l, raw=raw)
        else:
            n_ok += 1
        new.raw["enriched_hash"] = db.content_hash(new)
        db.upsert(con, new, evaluate(new, cfg))
        con.commit()
    return {"enriched": n_ok, "failed": n_fail}
