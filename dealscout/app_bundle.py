"""Assemble everything known about one listing (used by the diligence tab, the per-card chat and the diligence verdict)."""
import json
from . import db
from .scan import row_to_listing
from .diligence import revenue_checks, expense_checklist, seller_flags, offer_builder, workspace, TRANSFER_NOTES


def bundle_for(con, row, cfg) -> dict:
    l = row_to_listing(row)
    j = db.get_judgment(con, l.id)
    sig_row = con.execute("SELECT * FROM signals WHERE listing_id=?", (l.id,)).fetchone()
    sig = json.loads(sig_row["json"]) if sig_row else None
    comps = pos = bench = None
    try:
        from .comps import comps_for, position
        comps = comps_for(con, row)
        pos = position(con, row)
    except Exception:
        pass
    ph = [dict(r) for r in con.execute("SELECT seen_at, asking_price, status, bid_count, reserve_met FROM price_history WHERE listing_id=? ORDER BY seen_at", (l.id,))]
    ws = workspace(con, l.id)
    scored = {"payback_months": row["payback_months"], "payback_75": row["payback_75"], "payback_65": row["payback_65"],
              "flags": json.loads(row["flags"] or "[]"), "score": row["score"]}
    listing = {k: getattr(l, k) for k in ("id", "source", "url", "title", "category", "asking_price", "monthly_profit", "monthly_revenue",
                                          "margin", "customers", "users_free", "age_months", "churn_pct", "verified_revenue", "verified_traffic",
                                          "sale_method", "ends_at", "status", "reason_for_selling", "summary", "hours_per_week", "monetization")}
    listing["raw_extra"] = {k: v for k, v in (l.raw or {}).items() if k in ("page", "gated", "risks", "opportunities", "mrr", "seller_location", "hostname")}
    return {
        "listing": listing, "scored": scored,
        "scout_verdict": {"verdict": j["verdict"], "first_pass": j["verdict1"], "first": json.loads(j["json"] or "{}"),
                          "skeptic": json.loads(j["json2"] or "null")} if j else None,
        "signals": sig,
        "revenue_checks": revenue_checks(l, sig), "expense_checklist": expense_checklist(l), "seller_flags": seller_flags(l),
        "transfer_notes": TRANSFER_NOTES.get(l.category, TRANSFER_NOTES["other"]),
        "comps": comps, "position": pos, "offer": offer_builder(l, pos), "price_history": ph,
        **ws,
    }
