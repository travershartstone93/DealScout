"""Flippa v3 JSON:API. One paginated query per property_type (combining a price range with a
property_type[] array returns 0 results — verified 2026-08-16)."""
from ..models import Listing
from ..normalize import months_since, extract_customers, monetization, category
from .base import get, log

NAME = "flippa"
API = "https://flippa.com/v3/listings"


def fetch(cfg, http):
    types = cfg.get("flippa", {}).get("property_types", ["saas"])
    max_price = cfg["filters"]["max_price"]
    for pt in types:
        page = 1
        while True:
            params = {"filter[property_type]": pt, "filter[status]": "open",
                      "filter[price][max]": max_price, "page[size]": 100, "page[number]": page}
            data = get(http, API, params=params).json()
            items = data.get("data", [])
            for d in items:
                yield _convert(d)
            total = data.get("meta", {}).get("total_results", 0)
            if page * 100 >= total or not items:
                break
            page += 1
        log.info("flippa %s: done", pt)


def _convert(d: dict) -> Listing:
    text = f"{d.get('title','')} {d.get('summary','')}"
    paying, free = extract_customers(text)
    if free is None and d.get("app_downloads_per_month"):
        free = int(d["app_downloads_per_month"])
    if free is None and d.get("uniques_per_month"):
        free = int(d["uniques_per_month"])
    price = d.get("display_price") or d.get("buy_it_now_price") or d.get("current_price")
    profit, rev = d.get("profit_per_month"), d.get("revenue_per_month")
    margin = round(100 * profit / rev, 1) if profit and rev else None
    return Listing(
        id=f"flippa:{d['id']}", source=NAME, url=d.get("html_url") or f"https://flippa.com/{d['id']}",
        title=(d.get("title") or d.get("property_name") or "")[:200],
        category=category(d.get("property_type"), d.get("industry")),
        asking_price=price, monthly_profit=profit, monthly_revenue=rev, margin=margin,
        customers=paying, users_free=free, age_months=months_since(d.get("established_at")),
        verified_revenue=bool(d.get("has_verified_revenue")), verified_traffic=bool(d.get("has_verified_traffic")),
        sale_method=d.get("sale_method") or "", ends_at=d.get("ends_at"),
        status="open" if d.get("status") == "open" else "ended",
        summary=(d.get("summary") or "")[:2000], monetization=monetization(text),
        raw={k: v for k, v in d.items() if k not in ("images", "relationships", "links")},
    )
