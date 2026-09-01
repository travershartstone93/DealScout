"""Hard filters + 0-100 score + deterministic risk/platform flags."""
import math
from .models import Listing


def payback(price, profit):
    if price and profit and profit > 0:
        return round(price / profit, 1)
    return None


def evaluate(l: Listing, cfg: dict) -> dict:
    f, w, r = cfg["filters"], cfg["score"], cfg["risk"]
    text = f"{l.title} {l.summary} {l.reason_for_selling}".lower()
    flags, fails = [], []

    profit = l.monthly_profit
    if (profit is None or profit <= 0) and l.monthly_revenue:
        profit = l.monthly_revenue * f["revenue_to_profit_ratio"]
        flags.append("profit_estimated_from_revenue")
    price = l.asking_price
    pb = payback(price, profit)
    pb75 = payback(price * 0.75 if price else None, profit)
    pb65 = payback(price * 0.65 if price else None, profit)

    if price is None:
        fails.append("no_price")
    elif price > f["max_price"]:
        fails.append("price>max")
    if pb is None:
        fails.append("no_profit_data")
    elif pb > f["max_payback_months"]:
        fails.append("payback>max")
    if l.customers is not None:
        if l.customers < f["min_customers"]:
            fails.append("customers<min")
    elif l.users_free is not None:
        if l.users_free < f["min_free_users"]:
            fails.append("free_users<min")
        else:
            flags.append("only_free_user_count_known")
    else:
        flags.append("customer_count_unknown")
    if l.category in f["exclude_categories"]:
        fails.append(f"category:{l.category}")
    if l.status != "open":
        fails.append(f"status:{l.status}")

    for k in r["keywords"]:
        if k in text:
            flags.append(f"risk:{k}")
    for k in r["platform_keywords"]:
        if k in text or (k == "chrome extension" and l.category == "chrome_extension"):
            flags.append(f"platform:{k}")
    if l.monetization == "one_off":
        flags.append("non_recurring_revenue")
    if l.category == "content_site" or l.monetization == "ads":
        flags.append("traffic_ads_dependent")
    if l.churn_pct and l.churn_pct > 15:
        flags.append("high_churn")

    # score
    s = 0.0
    if pb is not None:
        s += w["payback"] * max(0.0, min(1.0, (48 - pb) / 42))          # 6mo→1.0, 48mo→0
    if l.margin is not None:
        s += w["margin"] * max(0.0, min(1.0, l.margin / 100))
    n = l.customers if l.customers is not None else (l.users_free or 0) / 10
    if n:
        s += w["customers"] * min(1.0, math.log10(max(n, 1)) / 4)          # 10k→1.0
    if l.age_months:
        s += w["age"] * min(1.0, l.age_months / 48)
    s += w["verified"] * (0.6 * l.verified_revenue + 0.4 * l.verified_traffic)
    s += w["recurring"] * (1.0 if l.monetization == "recurring" else 0.6 if l.monetization in ("", "mixed", "one_off") else 0.3)
    if l.hours_per_week is not None:
        s += w["hours"] * max(0.0, 1 - l.hours_per_week / 20)
    else:
        s += w["hours"] * 0.5
    s -= 8 * sum(1 for x in flags if x.startswith("risk:"))
    if l.category in w.get("software_categories", []):
        s += w.get("software_bonus", 0)
    if l.customers:
        s += w.get("paying_customers_bonus", 0) * min(1.0, math.log10(max(l.customers, 1)) / 3)
    if "traffic_ads_dependent" in flags:
        s -= w.get("ads_penalty", 6)
    if any(x.startswith("price_drop") for x in flags):
        s += 3
    s = round(max(0.0, min(100.0, s)), 1)

    return {"passes": int(not fails), "score": s, "payback_months": pb, "payback_75": pb75,
            "payback_65": pb65, "flags": flags, "fail_reasons": fails}
