"""Little Exits - public JSON API app.littleexits.com/api/firebase/searchProjects (works without a browser)."""
import re
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get

NAME = "littleexits"
API = "https://app.littleexits.com/api/firebase/searchProjects"


def fetch(cfg, http):
    page = 1
    while True:
        j = get(http, API, params={"page": page, "limit": 50, "sortBy": "has_profit", "onlyActive": "true",
                                   "onlyApproved": "true", "category": "All"}).json()
        items = j.get("projects", [])
        for d in items:
            if d.get("sold") or d.get("hidden") or not d.get("active", True):
                continue
            yield _convert(d)
        total = j.get("total") or 0
        if not items or page * 50 >= total:
            break
        page += 1


def _title(d):
    n, t = d.get("name") or "", d.get("tagline") or ""
    return (t if t.lower().startswith(n.lower()) else f"{n} - {t}" if t else n)[:200]


def _convert(d: dict) -> Listing:
    desc = re.sub(r"<[^>]+>", " ", d.get("description") or "")
    desc = re.sub(r"\s+", " ", desc).strip()
    text = f"{d.get('name','')} {d.get('tagline','')} {desc}"
    paying, free = extract_customers(text)
    ub = intval(d.get("userbase"))
    if free is None and ub:
        free = ub
    rev = money(d.get("monthly_revenue")) or None
    exp = money(d.get("monthly_expense")) or 0
    profit = (rev - exp) if rev else None
    age = d.get("age")
    return Listing(
        id=f"le:{d['id']}", source=NAME, url=f"https://app.littleexits.com/project/{d.get('slug') or d['id']}",
        title=_title(d),
        category=category(d.get("main_category"), *(d.get("category") or [])),
        asking_price=money(d.get("asking_price")), monthly_profit=profit, monthly_revenue=rev,
        margin=round(100 * profit / rev, 1) if profit and rev else None,
        customers=paying, users_free=free, age_months=round(float(age), 1) if age else None,
        verified_revenue=bool(d.get("stripe_analytics_connected_at")),
        verified_traffic=bool((d.get("googleAnalytics") or {}).get("connected")),
        sale_method="classified", status="open", summary=(d.get("tagline", "") + " " + desc)[:2000],
        monetization=monetization(text),
        raw={k: d.get(k) for k in ("stack", "business_location", "offers", "views", "premium", "created_date")},
    )
