"""Acquire.com public listing pages (/public/<slug>) discovered via sitemap.xml. No login: only the
public headline metrics (asking price, TTM revenue/profit, last-month figures, paying users). Detail
fetches capped (cfg [acquire].max_detail, default 60), newest by sitemap lastmod."""
import re, html as _html, json
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "acquire"
SITEMAP = "https://app.acquire.com/sitemap.xml"
_URL = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]*)</lastmod>)?", re.S)


def fetch(cfg, http):
    cap = int(cfg.get("acquire", {}).get("max_detail", 60))
    xml = get(http, SITEMAP).text
    urls = [(m.group(2) or "", _html.unescape(m.group(1))) for m in _URL.finditer(xml) if "/public/" in m.group(1)]
    urls.sort(key=lambda t: t[0], reverse=True)  # stable: keeps sitemap order within equal lastmod
    for _, url in urls[:cap]:
        try:
            page = get(http, url).text
        except Exception as e:
            log.warning("acquire %s: %s", url, e)
            continue
        l = _convert(url, page)
        if l:
            yield l


def _text(page: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = _html.unescape(t)
    return re.sub(r"\|[\s|]*", "|", t)


def _field(t: str, label: str):
    """Value following '|label|' — skipping the tooltip sentence Acquire inserts on the metrics grid."""
    m = re.search(r"\|" + re.escape(label) + r"\|((?:[^|]*\.\|)?)([^|]*)\|", t)
    if not m:
        return None
    v = m.group(2).strip()
    return None if v in ("", "-", "—") else v


def _convert(url: str, page: str) -> Listing | None:
    ld = {}
    m = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S)
    if m:
        try:
            ld = json.loads(m.group(1))
        except json.JSONDecodeError:
            ld = {}
    slug = url.rsplit("/public/", 1)[-1]
    nid = slug.split("-", 1)[0]
    t = _text(page)
    title = ld.get("name") or _field(t, "P&L Documents (1)") or slug
    if not ld and "Asking Price" not in t:
        return None
    price = money((ld.get("offers") or {}).get("price")) or money(_field(t, "Asking Price"))
    ttm_rev, ttm_prof = money(_field(t, "TTM Revenue")), money(_field(t, "TTM Profit"))
    lm_rev, lm_prof = money(_field(t, "Last Months Revenue")), money(_field(t, "Last Months Profit"))
    m_rev = lm_rev if lm_rev is not None else (ttm_rev / 12 if ttm_rev else None)
    m_prof = lm_prof if lm_prof is not None else (ttm_prof / 12 if ttm_prof else None)
    margin = round(100 * ttm_prof / ttm_rev, 1) if ttm_prof and ttm_rev else None
    desc = ld.get("description") or ""
    text = f"{title} {desc}"
    paying, free = extract_customers(text)
    pu = intval(_field(t, "Paying Users") or _field(t, "Customers"))
    if pu:
        paying = pu
    mau = intval(_field(t, "Monthly Active Users") or _field(t, "Total Downloads"))
    if free is None and mau:
        free = mau
    founded = _field(t, "Date Founded")
    age = None
    if founded:
        fm = re.search(r"([A-Za-z]+)?\s*(\d{4})", founded)
        if fm:
            from datetime import datetime
            months = ["january", "february", "march", "april", "may", "june", "july", "august",
                      "september", "october", "november", "december"]
            mo = months.index(fm.group(1).lower()) + 1 if fm.group(1) and fm.group(1).lower() in months else 6
            now = datetime.now()
            age = max(0.0, (now.year - int(fm.group(2))) * 12 + now.month - mo)
    biz = _field(t, "Business Model") or ""
    cat = category(ld.get("category"), title, biz)
    mon = monetization(f"{biz} {text}")
    if "subscription" in biz.lower():
        mon = "recurring"
    growth = _field(t, "Annual Growth Rate")
    return Listing(
        id=f"acq:{nid}", source=NAME, url=url, title=str(title)[:200], category=cat,
        asking_price=price, monthly_profit=m_prof, monthly_revenue=m_rev, margin=margin,
        customers=paying, users_free=free, age_months=age, verified_revenue=False, verified_traffic=False,
        sale_method="classified", status="open",
        reason_for_selling=(_field(t, "Selling Reasoning") or "")[:500], summary=desc[:2000], monetization=mon,
        raw={"ttm_revenue": ttm_rev, "ttm_profit": ttm_prof, "last_month_revenue": lm_rev, "last_month_profit": lm_prof,
             "growth": growth, "multiples": _field(t, "Multiples"), "team": _field(t, "Team Size"),
             "founded": founded, "business_model": biz, "ld_category": ld.get("category")},
    )
