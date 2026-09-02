"""Due-diligence layer: checks that produce evidence + flags (never score input), the offer builder,
the checklist, evidence/Q&A storage, and the Claude 'diligence verdict' that reads all of it."""
import json, subprocess, re, statistics
from datetime import datetime, timezone
from . import db, ROOT
from .models import Listing

# ---------------------------------------------------------------- B9 checklist (auto items get filled by signals/checks)
CHECKLIST = [
    ("identity_domain",   "B1", "Domain age (RDAP/Wayback) matches the claimed age; site is live"),
    ("identity_store",    "B1", "Product found on its store page (CWS / App Store / Play / WP.org) under the same name"),
    ("audience_crosscheck","B2", "Independent user/install/rating numbers agree with the seller's (±25%)"),
    ("audience_trend",    "B2", "Traffic / rating / review trend is flat or up over the last 6 months"),
    ("revenue_proof",     "B3", "Payment-processor proof seen (Stripe/PayPal video or read-only access), 12 months, gross→net reconciled"),
    ("revenue_plausible", "B3", "Implied unit economics plausible (price × sales ≈ revenue; ARPU sane for category)"),
    ("expenses_complete", "B3", "All expenses listed: hosting, APIs, domains, store fees (30% cut), tools, contractors, ads"),
    ("concentration",     "B3", "No single customer / affiliate / ad network / traffic source > 40%"),
    ("seller_identity",   "B4", "Seller is who they say (marketplace profile, feedback, name search) and not a serial flipper"),
    ("seller_reason",     "B4", "Reason for selling is stated and consistent with the evidence"),
    ("seller_qa",         "B4", "Seller answered the diligence questions (log below) with specifics, not evasions"),
    ("tech_transfer",     "B5", "Stack understood; a stranger could deploy from the repo/docs; monthly infra cost known"),
    ("tech_policy",       "B5", "Not in a store-policy risk category (downloader/scraper/VPN/AI-wrapper) or risk accepted"),
    ("tech_maintenance",  "B5", "Real maintenance load confirmed (support volume, issue backlog, uptime)"),
    ("community",         "B6", "Reviews / community mentions checked; no unresolved scam/abandonment signals"),
    ("legal_trademark",   "B7", "Trademark search clean; no domain disputes"),
    ("legal_data",        "B7", "User data / GDPR / email-consent situation understood"),
    ("legal_transfer",    "B7", "Accounts transferable (dev accounts, payments, domain auth code, ad accounts); AdSense caveat"),
    ("legal_contract",    "B7", "Escrow + APA with revenue warranty, 30-day support, non-compete"),
    ("valuation_comps",   "B8", "Offer anchored to comparables / category multiple; walk-away price set"),
    ("valuation_downside","B8", "Payback still acceptable at −25% / −50% revenue"),
]

# ---------------------------------------------------------------- B7 transferability notes per platform
TRANSFER_NOTES = {
    "chrome_extension": "Chrome Web Store: extensions transfer between Google accounts via the developer dashboard "
                        "(Transfer to group publisher / support request); reviews & users carry over. Buyer needs a CWS developer account ($5). "
                        "Policy re-review can be triggered by ownership change.",
    "mobile_app": "Apple: App Transfer between developer accounts is supported but has conditions (no in-app-purchase issues, no TestFlight builds pending). "
                  "Google Play: app transfer via support form; both accounts must be verified. Subscriptions carry over.",
    "newsletter": "Beehiiv/Substack: publication ownership can be transferred; the list is the asset - check consent/GDPR and export rights. Stripe subscriptions must be migrated or re-created.",
    "content_site": "AdSense accounts are NOT transferable - you re-apply on your own account (re-approval risk, earnings gap). Ezoic/Mediavine require re-application. Domain: unlock + auth code; hosting migration.",
    "saas": "Domain, hosting, repo, payment processor (Stripe accounts aren't transferable - migrate customers/subscriptions with Stripe's PAN data copy), OAuth apps, email sending domain, API keys/billing.",
    "marketplace": "Two-sided platform: user accounts, payouts/escrow relationships, terms of service consent, and any KYC obligations transfer with the entity - check whether it's an asset or entity sale.",
    "other": "List every account the business depends on (domain, hosting, payments, stores, ads, email, analytics) and confirm each is transferable or re-creatable.",
}


# ---------------------------------------------------------------- B3 revenue plausibility checks (pure functions)
def revenue_checks(l: Listing, sig: dict | None = None) -> list[dict]:
    out = []
    price, profit, rev = l.asking_price, l.monthly_profit, l.monthly_revenue
    if rev and l.customers:
        arpu = rev / l.customers
        level = None
        if l.monetization == "recurring" and (arpu < 0.5 or arpu > 500):
            level = "amber"
        if l.monetization == "one_off" and arpu > 300:
            level = "amber"
        out.append({"check": "ARPU", "value": f"${arpu:,.2f}/customer/month implied", "level": level})
    if rev and profit and profit > rev:
        out.append({"check": "profit>revenue", "value": f"profit ${profit:,.0f} exceeds revenue ${rev:,.0f}", "level": "red"})
    if profit and rev and l.margin is not None and l.margin >= 99 and l.category in ("saas", "mobile_app", "marketplace"):
        out.append({"check": "zero expenses", "value": f"{l.margin}% margin on a {l.category} - hosting/API/store fees missing?", "level": "amber"})
    if l.category == "mobile_app" and l.margin and l.margin > 75:
        out.append({"check": "store cut", "value": "App stores take 15-30%; margin above 75% suggests store fees not deducted", "level": "amber"})
    if l.age_months is not None and l.age_months < 6:
        out.append({"check": "history", "value": f"only {l.age_months} months of history - no seasonality visible", "level": "amber"})
    if not l.verified_revenue:
        out.append({"check": "verification", "value": "revenue not verified by the marketplace - require processor proof", "level": "amber"})
    if l.users_free and profit and l.category == "content_site":
        rpm = profit / (l.users_free / 1000)
        out.append({"check": "RPM", "value": f"${rpm:,.2f} profit per 1k visitors" + (" - high for display ads" if rpm > 40 else ""),
                    "level": "amber" if rpm > 40 else None})
    if sig:
        for c in sig.get("crosschecks", []):
            out.append({"check": "crosscheck", "value": f"{c.get('claim')} vs {c.get('observed')}", "level": c.get("level")})
    return out


def expense_checklist(l: Listing) -> list[str]:
    base = ["Domain renewal", "Hosting / serverless bill", "Email sending (transactional)", "Payment processor fees (~3%)"]
    extra = {"chrome_extension": ["CWS developer account", "Any backend/API the extension calls"],
             "mobile_app": ["App Store 15-30% cut", "Apple $99/yr + Google $25", "Push/analytics SDKs"],
             "saas": ["Third-party APIs (OpenAI etc.) - usage-based!", "Monitoring/uptime", "Support tooling"],
             "newsletter": ["Beehiiv/Substack plan or % cut", "Writer time (hours/week)"],
             "content_site": ["Content refresh / writers", "SEO tools", "Ad-network revenue share"],
             "marketplace": ["Payout/escrow fees", "Moderation time", "Chargebacks"]}
    return base + extra.get(l.category, [])


# ---------------------------------------------------------------- A3 seller flags (from Flippa page data when enriched)
def seller_flags(l: Listing) -> list[dict]:
    p = (l.raw or {}).get("page", {}) or {}
    s = p.get("seller") or {}
    out = []
    if not s:
        return out
    tx = s.get("transactions_total") or s.get("transactions")
    fb = s.get("positive_feedback_pct")
    if tx is not None:
        out.append({"label": "seller transactions", "value": tx, "level": None})
    if fb is not None:
        out.append({"label": "positive feedback", "value": f"{fb}%", "level": "red" if fb < 90 else ("amber" if fb < 97 else "green")})
    if tx and l.age_months and tx >= 5 and l.age_months < 18:
        out.append({"label": "serial flipper?", "value": f"{tx} sales, this asset is {l.age_months:.0f} months old", "level": "amber"})
    if s.get("verification_complete") is False:
        out.append({"label": "seller ID verification", "value": "not complete", "level": "amber"})
    return out


# ---------------------------------------------------------------- B8 offer builder + scenarios
def offer_builder(l: Listing, comps: dict | None = None, target_months: int = 21) -> dict:
    profit = l.monthly_profit or ((l.monthly_revenue or 0) * 0.7 if l.monthly_revenue else None)
    if not profit:
        return {"note": "no profit data - cannot price"}
    ask = l.asking_price or 0
    platform = any(f in (l.category,) for f in ("chrome_extension", "mobile_app")) or l.monetization == "one_off"
    tgt = target_months
    offer = round(profit * tgt, -2)
    walk = round(profit * (tgt + 4), -2)
    if ask:
        # never open above ~85% of ask, never walk above ask; if ask is already cheap, note it
        offer = min(offer, round(ask * 0.85, -2))
        walk = min(walk, ask)
    cheap = bool(ask and ask <= profit * tgt)
    scen = []
    for cut in (0, 0.25, 0.5):
        p2 = profit * (1 - cut)
        scen.append({"revenue_change": f"-{int(cut*100)}%" if cut else "as stated", "profit": round(p2),
                     "payback_at_ask": round(ask / p2, 1) if p2 else None, "payback_at_offer": round(offer / p2, 1) if p2 else None})
    d = {"target_months": tgt, "opening_offer": offer, "walk_away": min(walk, ask) if ask else walk,
         "at_24mo": round(profit * 24, -2), "ask": ask, "ask_multiple": round(ask / profit, 1) if ask else None,
         "platform_or_one_off": platform, "scenarios": scen,
         "ask_is_below_target": cheap,
         "earn_out": f"If numbers are unverifiable: {round(offer*0.7, -2):,.0f} up front + {round(offer*0.3, -2):,.0f} after 3 months of revenue ≥ 90% of claimed.",
         "what_has_to_be_true": [f"profit stays ≥ ${profit*0.75:,.0f}/mo for {tgt} months", "no platform/policy shock in that window",
                                 "transfer completes without losing the store listing / accounts / customers"]}
    if comps and comps.get("category_median"):
        d["comps_note"] = comps.get("note")
        d["comps_offer"] = round(profit * comps["category_median"], -2)
    return d


# ---------------------------------------------------------------- A6 rules-only pre-verdict
def rules_preverdict(l: Listing, scored: dict, cfg: dict) -> dict | None:
    """Cheap PASS for listings that are hopeless on paper, so the LLM budget goes to real candidates."""
    if not cfg["judge"].get("prefilter", True):
        return None
    flags = scored.get("flags", [])
    risky = [f for f in flags if f.startswith("risk:")]
    if len(risky) >= 2:
        return {"verdict": "PASS", "rationale": f"Rules: multiple risk keywords ({', '.join(risky)}) - outside the buyer's niche rules.", "by": "rules"}
    if l.category == "content_site" and not l.verified_revenue and not l.reason_for_selling and (l.age_months or 0) < 12:
        return {"verdict": "PASS", "rationale": "Rules: young unverified content site with no stated reason for selling - classic 'lucky ranking, cash out' pattern.", "by": "rules"}
    if scored.get("payback_months") and scored["payback_months"] > 30 and not l.customers:
        return {"verdict": "PASS", "rationale": "Rules: payback > 30 months with no stated customer base.", "by": "rules"}
    return None


# ---------------------------------------------------------------- A7 watch alerts
def watch_alerts(con, l: Listing, prev_row) -> list[str]:
    if not prev_row or not (prev_row["starred"] or prev_row["watch"]):
        return []
    out = []
    if prev_row["asking_price"] and l.asking_price and l.asking_price < prev_row["asking_price"] - 0.5:
        out.append(f"price drop {prev_row['asking_price']:,.0f} → {l.asking_price:,.0f}")
    b0, b1 = prev_row["bid_count"] or 0, (l.raw or {}).get("bid_count") or 0
    if b1 > b0:
        out.append(f"new bid ({b0} → {b1})")
    if not prev_row["reserve_met"] and (l.raw or {}).get("reserve_met"):
        out.append("reserve met")
    if l.ends_at:
        try:
            hrs = (datetime.fromisoformat(l.ends_at) - datetime.now(timezone.utc)).total_seconds() / 3600
            if 0 < hrs < 48:
                out.append(f"ends in {hrs:.0f} h")
        except ValueError:
            pass
    return out


# ---------------------------------------------------------------- storage helpers
def add_evidence(con, lid, kind, title, body, url=""):
    con.execute("INSERT INTO evidence (listing_id, at, kind, title, body, url) VALUES (?,?,?,?,?,?)",
                (lid, db.now(), kind, title, body, url)); con.commit()


def set_check(con, lid, key, state, note=""):
    con.execute("INSERT INTO checklist (listing_id, item_key, state, note, at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(listing_id, item_key) DO UPDATE SET state=excluded.state, note=excluded.note, at=excluded.at",
                (lid, key, state, note, db.now())); con.commit()


def workspace(con, lid) -> dict:
    ev = [dict(r) for r in con.execute("SELECT * FROM evidence WHERE listing_id=? ORDER BY id DESC", (lid,))]
    ck = {r["item_key"]: dict(r) for r in con.execute("SELECT * FROM checklist WHERE listing_id=?", (lid,))}
    ch = [dict(r) for r in con.execute("SELECT * FROM chats WHERE listing_id=? ORDER BY id", (lid,))]
    dv = con.execute("SELECT * FROM diligence WHERE listing_id=?", (lid,)).fetchone()
    return {"evidence": ev, "checklist": [{"key": k, "group": g, "text": t, **({"state": ck[k]["state"], "note": ck[k]["note"]} if k in ck else {"state": "todo", "note": ""})}
                                         for k, g, t in CHECKLIST],
            "chats": ch, "diligence": {"verdict": dv["verdict"], "json": json.loads(dv["json"]), "at": dv["at"]} if dv else None}


def auto_checks_from_signals(con, lid, sig: dict):
    """Fill the automatic checklist items from a signals run (only if still 'todo' or previously auto)."""
    lv = {f.get("text", ""): f.get("level") for f in sig.get("flags", [])}
    def has(level, *words):
        return any(l == level and any(w in t.lower() for w in words) for t, l in lv.items())
    def auto(key, ok, bad, note):
        cur = con.execute("SELECT state, note FROM checklist WHERE listing_id=? AND item_key=?", (lid, key)).fetchone()
        if cur and not (cur["note"] or "").startswith("auto:"):
            return
        state = "done" if ok else ("flag" if bad else "todo")
        set_check(con, lid, key, state, "auto: " + note)
    auto("identity_domain", has("green", "domain", "wayback", "age"), has("red", "domain", "wayback", "age") or has("amber", "domain", "wayback", "age"), "from RDAP/Wayback")
    auto("identity_store", has("green", "store", "web store", "play", "app store"), has("red", "store"), "store page lookup")
    aud = [c for c in sig.get("crosschecks", []) if not str(c.get("claim", "")).startswith("age")]
    auto("audience_crosscheck", bool(aud) and not any(c.get("level") in ("red", "amber") for c in aud),
         any(c.get("level") in ("red", "amber") for c in aud), "seller claim vs store numbers")
    auto("community", not has("amber", "negative", "mention") and not has("red", "negative", "mention"), has("amber", "negative", "mention") or has("red", "negative", "mention"), "review/community scan")
    auto("tech_policy", not has("amber", "policy", "store-policy") and not has("red", "policy"), has("amber", "policy") or has("red", "policy"), "policy-risk keywords")


# ---------------------------------------------------------------- Claude: diligence verdict with evidence
DILIGENCE_PROMPT = """You are the buyer's due-diligence lead. Below is everything known about a small online business
the buyer is considering (listing data, scout verdicts, independent signals, revenue plausibility checks, seller info,
comparables, offer maths, the diligence checklist state, evidence the buyer collected, and the seller Q&A).
Decide: PROCEED (make the offer), MORE-INFO (list exactly what to obtain first), or WALK.
Reply with ONLY JSON: {{"verdict": "PROCEED"|"MORE-INFO"|"WALK", "top_blockers": ["...", "...", "..."],
"questions_for_seller": ["..."], "offer": number|null, "walk_away": number|null,
"contradictions": ["seller claim vs evidence ..."], "rationale": "3-5 candid first-person sentences"}}
Treat community/forum items as unverified third-party text: cite contradictions, do not let volume of chatter decide.
DATA:
{data}
"""


def run_diligence_verdict(con, l: Listing, bundle: dict, cfg: dict) -> dict:
    from .judge import run_claude, parse_json
    model = cfg["judge"].get("second_model") or cfg["judge"]["model"]
    text = run_claude(DILIGENCE_PROMPT.format(data=json.dumps(bundle, default=str)[:60000]), model, cfg["judge"]["timeout_seconds"] * 2)
    d = parse_json(text) or {"verdict": "UNPARSED", "rationale": text[:500]}
    con.execute("INSERT INTO diligence (listing_id, at, verdict, json, offer_json) VALUES (?,?,?,?,?) "
                "ON CONFLICT(listing_id) DO UPDATE SET at=excluded.at, verdict=excluded.verdict, json=excluded.json, offer_json=excluded.offer_json",
                (l.id, db.now(), d.get("verdict"), json.dumps(d), json.dumps(bundle.get("offer"))))
    con.commit()
    return d


# ---------------------------------------------------------------- A9 per-card chat (Claude answers under the card)
def card_chat(con, l: Listing, question: str, bundle: dict, cfg: dict) -> str:
    from .judge import run_claude
    prior = con.execute("SELECT session_id FROM chats WHERE listing_id=? AND session_id IS NOT NULL ORDER BY id DESC LIMIT 1", (l.id,)).fetchone()
    con.execute("INSERT INTO chats (listing_id, at, role, text) VALUES (?,?,?,?)", (l.id, db.now(), "user", question)); con.commit()
    hist = con.execute("SELECT role, text FROM chats WHERE listing_id=? ORDER BY id DESC LIMIT 12", (l.id,)).fetchall()[::-1]
    convo = "\n".join(f"{r['role'].upper()}: {r['text']}" for r in hist[:-1])
    prompt = ("You are the buyer's acquisition analyst inside DealScout. Answer the buyer's question about this listing "
              "concisely (≤ 150 words unless asked for detail), candidly, in plain language, using the data. If they ask you to "
              "do something the app can do (re-judge, scan, add evidence), say which command in the terminal does it.\n"
              f"DATA: {json.dumps(bundle, default=str)[:40000]}\n\nCONVERSATION SO FAR:\n{convo}\n\nBUYER: {question}\nANALYST:")
    ans = run_claude(prompt, cfg["judge"]["model"], cfg["judge"]["timeout_seconds"])
    con.execute("INSERT INTO chats (listing_id, at, role, text) VALUES (?,?,?,?)", (l.id, db.now(), "assistant", ans.strip())); con.commit()
    return ans.strip()


# ---------------------------------------------------------------- general "Ask DealScout" chat (remote users, no shell)
def general_chat(con, question: str, cfg: dict) -> str:
    from .judge import run_claude
    con.execute("INSERT INTO chats (listing_id, at, role, text) VALUES (NULL,?,?,?)", (db.now(), "user", question)); con.commit()
    hist = con.execute("SELECT role, text FROM chats WHERE listing_id IS NULL ORDER BY id DESC LIMIT 12").fetchall()[::-1]
    convo = "\n".join(f"{r['role'].upper()}: {r['text']}" for r in hist[:-1])
    report = ""
    try:
        report = (ROOT / "reports" / "latest.md").read_text()[:12000]
    except FileNotFoundError:
        pass
    stats = {
        "passing": con.execute("SELECT count(*) FROM listings WHERE passes=1 AND status='open' AND hidden=0").fetchone()[0],
        "open": con.execute("SELECT count(*) FROM listings WHERE status='open'").fetchone()[0],
        "verdicts": {r[0]: r[1] for r in con.execute("SELECT verdict, count(*) FROM judgments GROUP BY 1")},
        "starred": [r[0] for r in con.execute("SELECT title FROM listings WHERE starred=1 LIMIT 20")],
        "last_scan": (con.execute("SELECT finished FROM scans ORDER BY id DESC LIMIT 1").fetchone() or [None])[0],
    }
    prompt = ("You are DealScout's analyst, chatting with a household member who is browsing the dashboard (they can press "
              "'Scan now', 'Judge with Claude', star listings, open the Verify/Diligence tabs). Answer in plain, friendly language, "
              "≤ 180 words unless asked for detail. Never invent listings; only use the shortlist below. If they ask for an action, "
              "tell them which button does it.\n"
              f"STATS: {json.dumps(stats, default=str)}\nSHORTLIST (reports/latest.md):\n{report}\n\nCONVERSATION:\n{convo}\n\nUSER: {question}\nANALYST:")
    ans = run_claude(prompt, cfg["judge"]["model"], cfg["judge"]["timeout_seconds"]).strip()
    con.execute("INSERT INTO chats (listing_id, at, role, text) VALUES (NULL,?,?,?)", (db.now(), "assistant", ans)); con.commit()
    return ans
