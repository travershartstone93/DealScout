"""Motion Invest - the site's own Supabase PostgREST endpoint (anon key is public in their JS bundle)."""
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get

NAME = "motioninvest"


def fetch(cfg, http):
    mi = cfg["motioninvest"]
    url = f"{mi['supabase_url']}/rest/v1/marketplace_listings"
    hdr = {"apikey": mi["anon_key"], "Authorization": f"Bearer {mi['anon_key']}"}
    rows = get(http, url, params={"select": "*", "status": "eq.active", "order": "created_at.desc", "limit": 500},
               headers=hdr).json()
    for d in rows:
        yield _convert(d)


def _convert(d: dict) -> Listing:
    text = f"{d.get('title','')} {d.get('description','')}"
    paying, free = extract_customers(text)
    subs = intval(d.get("subscribers"))
    if paying is None and subs:
        paying = subs
    if free is None:
        free = intval(d.get("monthly_visitors")) or intval(d.get("page_views"))
    age = d.get("age")
    age_m = None
    if age is not None:
        a = money(age)
        if a is not None:
            age_m = a * 12 if a < 40 else a  # heuristic: small numbers are years
    slug = d.get("slug") or d.get("id")
    return Listing(
        id=f"mi:{d.get('id')}", source=NAME, url=f"https://motioninvest.com/marketplace/{slug}",
        title=(d.get("title") or "")[:200],
        category=category(d.get("category"), d.get("type"), d.get("business_model"), d.get("monetization_method")),
        asking_price=money(d.get("price")), monthly_profit=money(d.get("monthly_profit")),
        monthly_revenue=money(d.get("monthly_revenue")), margin=money(d.get("profit_margin")),
        customers=paying, users_free=free, age_months=age_m, verified_revenue=True, verified_traffic=True,
        sale_method="classified", status="open" if d.get("status") == "active" else "sold",
        reason_for_selling=(d.get("reason_for_selling") or "")[:500], summary=(d.get("description") or "")[:2000],
        monetization=monetization(f"{d.get('monetization_method','')} {text}"),
        raw={k: d.get(k) for k in ("multiple", "industry", "growth", "created_at", "type", "category")},
    )
