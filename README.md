# DealScout

DealScout hunts for small, turnkey online businesses to buy — Chrome extensions, SaaS tools,
newsletters, apps — across every marketplace it can reach, filters them against your rules,
scores them, and asks Claude for a candid verdict on each one that passes.

## How it works (plain language)

1. **Scan** — every 6 hours (systemd timer) it pulls the current for-sale listings from each
   marketplace. Some sites give clean JSON (Flippa, Empire Flippers, Motion Invest, Little Exits),
   some are read from their HTML pages, and a few need a real browser (SideProjectors, Transferslot,
   Investors.Club) so a headless Firefox does those.
2. **Normalize** — each listing becomes the same shape: asking price, profit/month, revenue/month,
   paying customers, free users, age, verified flags, category, summary.
3. **Hard filters** (`config.toml → [filters]`): price ≤ $20k, payback ≤ 36 months, ≥ 100 paying
   customers (or ≥ 2000 free users if that's all the seller says), category not in the excluded list,
   still open. A listing that fails any of these is kept in the database but not shown by default.
4. **Score 0–100** — payback (also computed at 75% and 65% of asking, so you can see the negotiation
   target), margin, customer count, age, verified data, recurring vs one-off, hours/week. Risk keywords
   (forex, gambling, adult, crypto…) subtract points; platform dependency (Chrome Web Store, App Store,
   Beehiiv…) is flagged, not punished.
5. **Claude judge** — the passing listings are sent, one at a time, to your local Claude Code login
   (`claude -p`, no API key) with a fixed rubric. Claude returns BUY-CANDIDATE / NEGOTIATE / PASS,
   a turnkey score, niche risk, a suggested offer and a short rationale — the same style as the
   Bulk-File-Downloader-vs-ForexNotifier reasoning. Verdicts are cached; a listing is only re-judged
   if its numbers change or you press "Re-judge".
6. **Dead listings** — anything not seen in two consecutive scans is marked stale and hidden.

## Using it

**Desktop shortcut:** *DealScout* on your Desktop and in the app menu (`launch.sh` — starts the services
if needed and opens the dashboard in a Firefox window).

**Layout:** listings on the left as cards — expand *"Why Claude says …"* under any card for the full
reasoning, numbers, red/green flags, offer suggestion, notes and Re-judge/Hide. On the right is a live
terminal running Claude Code inside `~/dealscout` (ttyd + tmux on :5007, service `dealscout-term`; the
conversation survives page reloads). Ask it things like *"why is the AdSense site a buy?"*, *"scan again"*,
*"judge the SaaS listings"*, *"compare the top three"* — it can run `./dealscout.sh` and read the DB.
Notifications are in-app only (the 🔔 bell): new BUY-CANDIDATE / NEGOTIATE verdicts since your last visit.


- Dashboard: <http://127.0.0.1:5006> (systemd user service `dealscout`).
  Filters at the top, click a row for the detail drawer, ★ to star, Hide to bury, notes are saved.
- Shortlist as text: `reports/latest.md` (rewritten after every scan).
- Talk to Claude about the shortlist interactively:
  `cd ~/dealscout && claude` then e.g. *"read reports/latest.md and compare the top 5"* — or point it
  at `curl -s 'http://127.0.0.1:5006/api/listings?mode=pass'` for the raw JSON.

### What the cards show
Verdict (final — if the skeptic second pass disagreed, the first-pass verdict is shown as a badge), score,
days on market, auction end, price drops (`was $X`), relisted, "also on <site>" when the same business is
listed elsewhere, verified badges, risk/platform flags. Expand *Why Claude says …* for the rationale, the
**skeptic second opinion** (a stronger model told to argue against the deal), 👍/👎 feedback (disagreements
are fed into future judgments so it learns your taste), notes, *Ask Claude about this* (types the question
into the terminal), Re-judge, Hide.

**Settings (⚙)** edits filters, score weights, judge models/caps and sources; *Save + re-score* re-applies
without a network scan. **Export starred** writes a diligence checklist (`reports/diligence.md`).
Judging is capped per scan and per day (`[judge].max_per_day`) to protect your Claude quota; the header
shows *judged today N/M* and live scan/judge progress. `dealscout.db` is backed up daily to `backups/`.

### Due diligence (per card: Why · Verify · Diligence · Chat)
- **Verify** — *Collect signals*: domain age (RDAP) and Wayback history vs the claimed age, store pages
  (Chrome Web Store / App Store / Play / WP.org) user counts & ratings cross-checked against the seller's numbers,
  tech stack + policy-risk category, community mentions (strict name/domain match, evidence only). Plus revenue
  plausibility checks (ARPU, store cut, missing expenses, RPM), seller flags, expenses to confirm, transfer notes.
- **Diligence** — offer builder (opening offer at target payback, walk-away, 24-mo price, category-multiple price,
  −25% / −50% scenarios, earn-out wording), comparables, the 21-item checklist (auto-filled where signals can),
  evidence & seller Q&A log, and the **Diligence verdict**: Claude reads everything → PROCEED / MORE-INFO / WALK
  with top blockers, questions for the seller, and contradictions between claims and evidence.
- **Chat** — ask the analyst about this listing; it answers under the card (→ terminal sends it to the big pane).
- 👁 **watch** a listing to get in-app alerts on price drops, new bids, reserve met, ending soon.
- **♥ Sources** — scraper health page (per-source counts, degraded/error/layout-change alarms).
- Rules pre-verdict: obviously hopeless listings get a PASS without spending Claude quota.

### Commands
```
./dealscout.sh scan                 # everything enabled in config.toml, then judge, then report
./dealscout.sh scan --source flippa --no-judge
./dealscout.sh judge --limit 20     # judge unjudged passing listings (software + known-customer listings first)
./dealscout.sh judge --category saas --limit 10
./dealscout.sh enrich               # fetch Flippa detail pages for passing listings (customers, churn, description)
./dealscout.sh rescore              # re-apply config.toml filters/weights to stored listings (no network)
./dealscout.sh show <id|title>      # scroll the dashboard to a listing (Claude uses this)
./dealscout.sh feedback <id> --disagree --comment "…"
./dealscout.sh signals <id>         # independent verification signals
./dealscout.sh diligence <id>       # PROCEED / MORE-INFO / WALK verdict from all evidence
./dealscout.sh evidence <id> --kind qa --title "Q… A…" [--body] [--url]
./dealscout.sh star|hide|watch <id> # (and unstar/unhide/unwatch)
./dealscout.sh outcomes | health
./dealscout.sh judge --force --id flippa:13714786
./dealscout.sh report               # rewrite reports/latest.md
./dealscout.sh sources              # what's on/off
./dealscout.sh login investorsclub  # opens a Firefox window; log in, close it; session is kept in .browser/
```
Service control: `systemctl --user restart dealscout` · `systemctl --user start dealscout-scan` (run a scan now)
· `journalctl --user -u dealscout-scan -n 50` or `tail scan.log`.

## Sharing with family
See **SHARING.md** — Tailscale (`tailscale serve`) with automatic tailnet identity; remote users get an
"Ask DealScout" chat instead of the terminal, and the layout works on phones.

## Tuning
Everything lives in `config.toml`: thresholds, score weights, risk/platform keyword lists, which
sources are on, the judge model (`claude-sonnet-5` by default; `claude-fable-5` for the hardest calls)
and how many listings get judged per scan (`max_per_scan`, to keep it cheap).

## Sources
| status | sites |
|---|---|
| JSON API, no login | Flippa, Empire Flippers, Motion Invest, Little Exits |
| HTML pages, no login | Acquire (public part), IndieMaker, BuyMicroStartups, IndieExit, Niche Investor, ExtensionDeal, WebStoreExtensions, ExitBid, LetterTrader, BuySellStartups |
| headless Firefox | SideProjectors, Transferslot |
| Firefox + your login | Investors.Club (`dealscout login investorsclub`) |
| off — blocked | Chrome-Stats marketplace (Turnstile), BizBuySell (Akamai) — the modules exist and explain why |

## Adding a site
Create `dealscout/sources/<name>.py` with `NAME` and `fetch(cfg, http)` yielding `Listing` objects
(see `motioninvest.py` for a 40-line example), add the name to `ALL` in `sources/__init__.py` and
`[sources]` in `config.toml`. One broken site never stops a scan — its error shows in the scan summary.
