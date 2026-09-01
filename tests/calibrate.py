"""Calibration harness: run two reference listings through the judge (no DB), check verdicts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dealscout import load_config, judge, score  # noqa: E402
from dealscout.models import Listing  # noqa: E402

CASES = [
    (Listing(
        id="cal:bulk-file-downloader", source="flippa", url="https://flippa.com/example-bulk-file-downloader",
        title="Bulk File Downloader — Chrome extension, 96% margin, fully automated",
        category="chrome_extension", asking_price=10000, monthly_profit=361, monthly_revenue=376, margin=96,
        customers=322, users_free=10500, age_months=None, churn_pct=None,
        verified_revenue=True, verified_traffic=True, sale_method="asking_price",
        reason_for_selling="Personal / wedding expenses",
        summary=("Chrome extension with 10,500 active users and ~30,000 installs in the last year. "
                 "Monetised via a $9.99 one-time upgrade (322 paid sales in period), software auto-delivers "
                 "the upgrade. Zero operating costs, fully automated set-and-forget. Stripe and Google Analytics "
                 "connected; Flippa-verified revenue, expenses and traffic."),
        hours_per_week=0, monetization="one_off",
    ), {"NEGOTIATE", "BUY-CANDIDATE"}),
    (Listing(
        id="cal:forexnotifier", source="flippa", url="https://flippa.com/example-forexnotifier",
        title="ForexNotifier — trading-signal SaaS, 260 subscribers, 4 years old",
        category="saas", asking_price=15000, monthly_profit=875, monthly_revenue=1136, margin=77,
        customers=260, users_free=None, age_months=48, churn_pct=10,
        verified_revenue=True, verified_traffic=True, sale_method="asking_price",
        reason_for_selling="Moving on to other projects",
        summary=("Forex trading-signal SaaS delivering signals to 260 active subscribers via WhatsApp, "
                 "Telegram and SMS. 4 years old, 10% churn, data-verified listing, 11,700 monthly page views."),
        hours_per_week=2, monetization="recurring",
    ), {"PASS"}),
]


def main() -> int:
    cfg = load_config()
    jc = cfg["judge"]
    ok = True
    for l, expected in CASES:
        scored = score.evaluate(l, cfg)
        text = judge.run_claude(judge.RUBRIC + "\nLISTING:\n" + judge._payload(l, scored), jc["model"], jc["timeout_seconds"])
        data = judge.parse_json(text)
        verdict = data.get("verdict", "UNPARSED")
        status = "OK" if verdict in expected else "FAIL"
        print(f"=== {l.title}")
        print(f"rule score={scored['score']} payback={scored['payback_months']} flags={scored['flags']}")
        print(f"[{status}] verdict={verdict} (expected one of {sorted(expected)})")
        print(f"suggested_offer={data.get('suggested_offer')}  niche_risk={data.get('niche_risk')}  platform={data.get('platform_dependency')}")
        print(f"rationale: {data.get('rationale')}")
        print()
        ok = ok and status == "OK"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
