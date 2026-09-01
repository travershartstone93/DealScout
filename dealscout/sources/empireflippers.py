"""Empire Flippers public listings API (financials are public)."""
from ..models import Listing
from ..normalize import months_since, category, extract_customers, monetization
from .base import get

NAME = "empireflippers"
API = "https://api.empireflippers.com/api/v1/listings/list"


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    page = 1
    while True:
        data = get(http, API, params={"limit": 100, "page": page, "listing_status": "For Sale"}).json()["data"]
        for d in data.get("listings", []):
            if d.get("listing_price") and d["listing_price"] > max_price * 3:
                continue  # price_max param is ignored server-side; keep a margin for negotiation view
            yield _convert(d)
        if page >= int(data.get("pages", 1)):
            break
        page += 1


def _convert(d: dict) -> Listing:
    mons = [m.get("monetization", "") for m in d.get("monetizations", [])]
    niches = [n.get("niche", "") for n in d.get("niches", [])]
    text = f"{d.get('public_title','')} {d.get('summary','')} {' '.join(mons)}"
    paying, free = extract_customers(text)
    sites = d.get("sites") or []
    if free is None and sites and sites[0].get("average_monthly_unique_users"):
        free = int(sites[0]["average_monthly_unique_users"])
    mon = monetization(" ".join(mons) + " " + text)
    if d.get("mrr"):
        mon = "recurring"
    return Listing(
        id=f"ef:{d.get('listing_number')}", source=NAME,
        url=f"https://empireflippers.com/listing/{d.get('listing_number')}/",
        title=(d.get("public_title") or "")[:200], category=category(*mons, *niches),
        asking_price=d.get("listing_price"), monthly_profit=d.get("average_monthly_net_profit"),
        monthly_revenue=d.get("average_monthly_gross_revenue"), margin=d.get("profit_margin"),
        customers=paying, users_free=free, age_months=months_since(d.get("first_made_money_at")),
        churn_pct=d.get("churn_percent"), verified_revenue=True, verified_traffic=bool(sites and sites[0].get("uses_google_analytics")),
        sale_method="classified", status="open" if d.get("listing_status") == "For Sale" else "sold",
        summary=(d.get("summary") or "")[:2000], hours_per_week=d.get("hours_worked_per_week"), monetization=mon,
        raw={k: d.get(k) for k in ("listing_number", "listing_multiple", "mrr", "ltv", "growth_percent",
                                   "days_on_marketplace", "opportunities", "risks", "customer_type")},
    )
