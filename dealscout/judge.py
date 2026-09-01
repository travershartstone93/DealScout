"""Qualitative verdicts via the local Claude Code login (`claude -p`), no API key needed.
Pass 1: cheap model, rubric verdict.  Pass 2 (BUY/NEGOTIATE only): stronger model tries to refute it."""
import json, subprocess, re
from . import db, ROOT
from .models import Listing

STATUS = ROOT / "judge.status.json"

RUBRIC = """You are helping a buyer pick a small, passive, turnkey online business to acquire.
Buyer rules: price <= $20k, payback <= 36 months (prefers <= 24), wants a broad customer base (a few
customers leaving must not kill it), essentially zero maintenance ("own it, keep it running, answer emails"),
avoids regulatory/trust-risky niches (e.g. trading signals, gambling, adult, crypto), tolerates but flags
platform dependency (Chrome Web Store, App Store, etc.), likes one-off software purchases as much as subscriptions.
Content/AdSense sites are acceptable but the buyer prefers software products with paying customers.

Assess the listing below. Reply with ONLY a JSON object, no prose, with keys:
verdict: "BUY-CANDIDATE" | "NEGOTIATE" | "PASS"
turnkey_score: 0-10 (10 = fully automated, no servers/inventory/support burden)
maintenance_estimate: short phrase (e.g. "1-2 h/month: email support, occasional store policy update")
niche_risk: 0-10 (10 = very risky: regulation, trust, platform bans, fads)
platform_dependency: short phrase or "none"
customer_concentration: short phrase (is the base broad enough?)
seller_reason_plausible: true|false|"unknown"
red_flags: [short strings]
green_flags: [short strings]
max_price_for_24mo_payback: number (monthly_profit * 24) or null
suggested_offer: number or null (what you'd realistically offer — for listings with a flagged platform dependency or non-recurring revenue target ~18-21 months payback rather than 24, and never exceed max_price_for_24mo_payback)
rationale: 2-4 sentences in a candid, first-person advisory tone.

IMPORTANT — baseline missing data: the DATA CONTEXT line tells you which fields are missing for most listings
on this marketplace's public feed (e.g. reason for selling, paying-customer counts, hours/week are absent ~90%
of the time on Flippa until a buyer contacts the seller). Treat that baseline missingness as the NORM, not a
listing-specific red flag: do not spend the rationale re-stating it, do not downgrade a listing for gaps every
peer shares, and do not repeat "unverified/blank X" boilerplate. Judge what is DISTINCTIVE about this listing
versus its peers (its numbers, category, platform, seller story where present). Put standard data requests in
red_flags only when they are decision-critical for THIS listing (e.g. revenue unverified AND the price is high).
A listing may only be marked down for opacity when it is unusually opaque FOR ITS SOURCE.
"""


def _data_context(con, source: str) -> str:
    """Per-source field-availability stats so the judge knows what 'normal missing' looks like."""
    r = con.execute(
        "SELECT count(*) n, sum(reason_for_selling='') no_reason, sum(customers IS NULL) no_cust, "
        "sum(hours_per_week IS NULL) no_hours, sum(monetization='') no_monet, sum(verified_revenue=0) no_verif "
        "FROM listings WHERE source=? AND passes=1", (source,)).fetchone()
    if not r or not r["n"]:
        return ""
    n = r["n"]
    pct = lambda x: round(100 * (x or 0) / n)
    return (f"DATA CONTEXT for {source} (n={n} passing listings): reason-for-selling missing {pct(r['no_reason'])}%, "
            f"paying-customer count missing {pct(r['no_cust'])}%, hours/week missing {pct(r['no_hours'])}%, "
            f"monetization unstated {pct(r['no_monet'])}%, revenue unverified {pct(r['no_verif'])}%. "
            "Gaps at or above these rates are marketplace baseline, not listing-specific flags.")

SKEPTIC = """You are a skeptical acquisitions advisor giving a SECOND OPINION. A first-pass analyst rated the
listing below as {verdict}. Your job is to try to talk the buyer OUT of it: look for the strongest reasons this
is a worse deal than it looks (fragile traffic/platform, unverifiable numbers, hidden maintenance, dying niche,
seller-story inconsistencies, payback that only works if nothing decays). Then decide honestly.
Respect the DATA CONTEXT line: fields missing at the marketplace-baseline rate are the norm, not an objection —
object only to gaps or contradictions specific to THIS listing.
Buyer rules: price <= $20k, payback <= 36 months (prefers <= 24), broad customer base, near-zero maintenance,
no regulatory/trust-risky niches, prefers software with paying customers over ad-supported content.
Reply with ONLY a JSON object: {{"verdict": "BUY-CANDIDATE"|"NEGOTIATE"|"PASS", "agrees_with_first_pass": true|false,
"strongest_objection": "...", "what_would_change_my_mind": "...", "suggested_offer": number|null,
"rationale": "2-4 candid first-person sentences"}}
FIRST-PASS ANALYSIS: {first}
"""


def _payload(l: Listing, scored: dict) -> str:
    return json.dumps({
        "source": l.source, "url": l.url, "title": l.title, "category": l.category,
        "asking_price": l.asking_price, "monthly_profit": l.monthly_profit, "monthly_revenue": l.monthly_revenue,
        "margin_pct": l.margin, "paying_customers": l.customers, "free_users": l.users_free,
        "age_months": l.age_months, "churn_pct": l.churn_pct, "verified_revenue": l.verified_revenue,
        "verified_traffic": l.verified_traffic, "sale_method": l.sale_method, "monetization": l.monetization,
        "hours_per_week": l.hours_per_week, "reason_for_selling": l.reason_for_selling, "summary": l.summary,
        "extra": {k: v for k, v in (l.raw or {}).items() if k in ("risks", "opportunities", "mrr", "churn", "tech_stack",
                                                                    "traffic_sources", "expenses", "hours", "stripe_connected",
                                                                    "google_analytics", "bid_count", "reserve_met")},
        "computed": {"payback_at_asking": scored.get("payback_months"), "payback_at_75pct": scored.get("payback_75"),
                     "payback_at_65pct": scored.get("payback_65"), "rule_flags": scored.get("flags")},
    }, default=str)


def _feedback_block(con) -> str:
    rows = db.recent_disagreements(con, 10)
    if not rows:
        return ""
    lines = [f'- On "{r["title"]}" (rated {r["verdict"]}) the buyer disagreed: {r["comment"] or "no comment"}' for r in rows]
    return "\nThe buyer has previously disagreed with verdicts like these — learn their taste from it:\n" + "\n".join(lines) + "\n"


def run_claude(prompt: str, model: str, timeout: int) -> str:
    cmd = ["claude", "-p", "--output-format", "json", "--model", model,
           "--disallowedTools", "Bash Edit Write NotebookEdit WebFetch WebSearch"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"claude -p failed: {p.stderr[:500]}")
    out = json.loads(p.stdout)
    return out.get("result", "") if isinstance(out, dict) else str(out)


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group()) if m else {}
    except json.JSONDecodeError:
        return {}


def set_status(**kw):
    try:
        cur = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    except json.JSONDecodeError:
        cur = {}
    cur.update(kw)
    STATUS.write_text(json.dumps(cur))


def judge(con, l: Listing, scored: dict, cfg: dict, force: bool = False) -> dict | None:
    """First pass (+ optional second pass). Returns the first-pass dict with 'second' attached if run."""
    jc = cfg["judge"]
    chash = db.content_hash(l)
    existing = db.get_judgment(con, l.id)
    if existing and existing["content_hash"] == chash and not force:
        d = json.loads(existing["json"])
        if existing["json2"]:
            d["second"] = json.loads(existing["json2"])
        return d
    if not force and db.judged_today(con) >= jc.get("max_per_day", 10**9):
        raise RuntimeError(f"daily judge cap reached ({jc['max_per_day']}) — raise [judge].max_per_day or use --force")
    prompt = RUBRIC + _feedback_block(con) + "\n" + _data_context(con, l.source) + "\nLISTING:\n" + _payload(l, scored)
    text = run_claude(prompt, jc["model"], jc["timeout_seconds"])
    data = parse_json(text)
    verdict = data.get("verdict", "UNPARSED")
    db.save_judgment(con, l.id, chash, jc["model"], verdict, data, text)
    con.execute("UPDATE judgments SET verdict1=?, json2=NULL, model2=NULL WHERE listing_id=?", (verdict, l.id))
    con.commit()
    return data


def second_opinion(con, l: Listing, scored: dict, cfg: dict) -> dict | None:
    jc = cfg["judge"]
    ex = db.get_judgment(con, l.id)
    if not ex or ex["verdict1"] not in ("BUY-CANDIDATE", "NEGOTIATE") or ex["json2"]:
        return None
    first = json.loads(ex["json"])
    prompt = SKEPTIC.format(verdict=ex["verdict1"], first=json.dumps(first)) + _feedback_block(con) + "\n" + _data_context(con, l.source) + "\nLISTING:\n" + _payload(l, scored)
    text = run_claude(prompt, jc.get("second_model", jc["model"]), jc["timeout_seconds"])
    d = parse_json(text)
    if not d:
        return None
    final = d.get("verdict") or ex["verdict1"]
    con.execute("UPDATE judgments SET json2=?, model2=?, verdict=? WHERE listing_id=?",
                (json.dumps(d), jc.get("second_model"), final, l.id))
    con.commit()
    return d
