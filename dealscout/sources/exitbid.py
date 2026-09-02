"""ExitBid - 5-day auctions. Live lots come from the site's own JSON snapshot (/auctions.json, CC BY 4.0);
descriptions/expenses/age come from the public Supabase `listings` table (publishable key is in the page JS).
Numbers are self-reported *ranges* ("$1k-$5k", "100-1K"): revenue takes the low bound, users the midpoint."""
import re
from ..models import Listing
from ..normalize import money, category, extract_customers, monetization
from .base import get, log

NAME = "exitbid"
FEED = "https://exitbid.io/auctions.json"
SB_URL = "https://kiyozvewqavgshnhzbad.supabase.co/rest/v1/listings"
SB_KEY = "sb_publishable_yrejgexp2OsVb8zRvLx0Lg_2IaqZG6N"

_AGE = {"<6mo": 3, "6mo-1yr": 9, "1-2yr": 18, "2+yr": 30}


def _range(s):
    """'$1k-$5k' -> (1000, 5000); '$5k+' -> (5000, None); '0-100' -> (0, 100)."""
    if not s:
        return None, None
    parts = re.split(r"\s*[--]\s*", str(s), maxsplit=1)
    lo = money(parts[0])
    hi = money(parts[1]) if len(parts) > 1 else None
    return lo, hi


def fetch(cfg, http):
    items = get(http, FEED).json().get("items", [])
    ids = [i["url"].split("id=")[-1] for i in items if "id=" in i.get("url", "")]
    details = {}
    if ids:
        try:
            hdr = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
            rows = get(http, SB_URL, params={"select": "*", "id": f"in.({','.join(ids)})"}, headers=hdr).json()
            details = {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}
        except Exception as e:  # feed alone is still useful
            log.warning("exitbid supabase detail fetch failed: %s", e)
    for it in items:
        lid = it["url"].split("id=")[-1]
        yield _convert(it, details.get(lid, {}))


def _convert(it: dict, d: dict) -> Listing:
    lid = it["url"].split("id=")[-1]
    rev_lo, rev_hi = _range(it.get("monthly_revenue") or d.get("monthly_revenue"))
    usr_lo, usr_hi = _range(it.get("users") or d.get("users_count"))
    users = None
    if usr_lo is not None:
        users = int((usr_lo + usr_hi) / 2) if usr_hi is not None else int(usr_lo)
    exp_lo, exp_hi = _range(d.get("expenses_percent"))
    profit = margin = None
    if rev_lo is not None and exp_lo is not None:
        exp = (exp_lo + exp_hi) / 2 if exp_hi is not None else exp_lo
        margin = round(100 - exp, 1)
        profit = round(rev_lo * margin / 100, 2)
    price = it.get("current_bid_usd") or it.get("starting_price_usd")
    reserve = it.get("reserve_price_usd")
    if reserve and not it.get("reserve_met") and price and price < reserve:
        price = reserve  # you won't get it below the reserve anyway
    desc = " ".join(x for x in (it.get("tagline"), d.get("full_description"), d.get("growth_opportunities")) if x)
    paying, free = extract_customers(desc)
    cat = category(it.get("category"), d.get("business_type"), it.get("industry"))
    if cat == "other" and re.search(r"bot|telegram|discord", f"{it.get('category','')}", re.I):
        cat = "saas"
    reason = d.get("selling_reason") or ""
    if d.get("reason_details"):
        reason += f": {d['reason_details']}"
    mon = monetization(desc)
    return Listing(
        id=f"xb:{lid}", source=NAME, url=it["url"], title=(it.get("name") or d.get("business_name") or "")[:200],
        category=cat, asking_price=price, monthly_profit=profit, monthly_revenue=rev_lo, margin=margin,
        customers=paying if paying is not None else (users if rev_lo else None),
        users_free=free if free is not None else users,
        age_months=_AGE.get(d.get("business_age") or "", None) or (money(d.get("business_age")) or None),
        verified_revenue=bool(d.get("revenue_proof_url") or d.get("trustmrr_url")), sale_method="auction",
        ends_at=it.get("ends_at"), status="open", reason_for_selling=reason[:500], summary=desc[:2000],
        hours_per_week=None, monetization=mon,
        raw={"revenue_range": it.get("monthly_revenue"), "users_range": it.get("users"),
             "expenses_pct": d.get("expenses_percent"), "business_age": d.get("business_age"),
             "starting_price_usd": it.get("starting_price_usd"), "current_bid_usd": it.get("current_bid_usd"),
             "reserve_price_usd": reserve, "reserve_met": it.get("reserve_met"), "bid_count": it.get("bid_count"),
             "growth_trend": d.get("growth_trend"), "traffic_sources": d.get("traffic_sources"),
             "tech_stack": d.get("tech_stack"), "website_url": it.get("website_url"), "industry": it.get("industry"),
             "business_stage": d.get("business_stage"), "hosting_costs": d.get("hosting_costs")},
    )
