# DealScout roadmap

Two lists. **A** = improvements to the scout (find + rank). **B** = due-diligence additions (verify a candidate
before money moves). Rule kept throughout: nothing in B changes the numeric score; B produces evidence,
red/green flags and a diligence verdict shown *next to* the scout verdict.

## A. Scout improvements
1. Logged-in enrichment for Flippa / Acquire / Investors.Club (P&L, traffic charts, seller Q&A).
2. Independent verification signals pulled automatically (store user counts/ratings, Wayback age, DNS alive).
3. Seller history & "serial flipper" flag from marketplace seller pages.
4. Outcome tracking: final sale price + time-to-sell per listing → calibrate suggested offers on real data.
5. Comparables: category payback-multiple benchmarks from price history.
6. Rules-only pre-verdict to save judge quota on obvious PASSes.
7. Watchlist alerts (in-app): price drop, reserve met, new bid, ending in 48 h.
8. Diligence workspace per starred listing (evidence + notes → re-judge with evidence).
9. Two-way terminal ↔ cards (Claude can star/hide/open; answers stream under the card).
10. Source health page + layout-change alarm.
11. Test fixture per scraper.

## B. Due-diligence additions ("Signals" + "Diligence" tabs on a starred/candidate listing)

### B1. Identity & existence
- Domain: WHOIS/RDAP creation date vs claimed age; registrar; expiry date; DNS resolves; TLS cert age.
- Wayback Machine: first snapshot date, snapshot count/yr (activity), design/ownership changes, was it ever a
  different business (rebrand/PBN history).
- Live check: site returns 200, load time, "site for sale"/parking pages, broken checkout.
- Product identity: exact-name + domain match to store pages (Chrome Web Store, App Store, Google Play,
  Shopify App Store, WordPress.org plugin page). No fuzzy matches.

### B2. Traffic & audience (independent of seller)
- Store pages: user/install count, rating, review count, review trend (last 90 d vs prior), last update date,
  category rank, permission changes; **cross-check against seller's "active users"** (>25% gap → red flag).
- SimilarWeb/Semrush free tier or Cloudflare Radar rank; Tranco rank; trend over 6 months.
- Search: brand-name search volume trend (Google Trends), indexed page count, manual-action signs
  (site: results collapse), backlink profile smell test (spammy anchors = PBN).
- Social: follower counts + last-post dates on any linked accounts.
- Newsletter (Beehiiv/Substack): public subscriber badge, archive cadence, open-rate claims vs industry.

### B3. Revenue plausibility
- Implied unit economics: profit ÷ price ÷ users; flag if ARPU is implausible for the category
  (e.g. $9.99 one-off × claimed sales ≠ stated revenue ±15%).
- Payment-processor screenshots: request Stripe/PayPal/Gumroad dashboard *video* or read-only Stripe access;
  reconcile 12 months of gross → net; refunds/chargebacks rate.
- Marketplace verification badges parsed (Flippa "Data Verified" integrations list) and *which* metrics they
  actually cover (revenue verified ≠ profit verified).
- Expense audit list: hosting, APIs (OpenAI etc.), domains, store fees (30% app-store cut!), tools,
  contractors, ad spend, anything the seller "forgot".
- Seasonality: monthly series (if provided) vs same months prior year; flag <6-month history.
- Concentration: top customer/affiliate/ad-network share; single traffic source share.

### B4. Seller
- Marketplace profile: feedback %, transactions, account age, other current/past listings (serial flipper),
  location vs claimed.
- Name/handle search: IndieHackers, Product Hunt maker page, Twitter/X, LinkedIn, GitHub, do they exist,
  do they own the product, did they announce shutdown/pivot, do they sell many "6-month-old passive sites".
- Reason for selling stated vs evidence (e.g. "no time" but launched 3 new products last month).
- Communication log: questions asked, answers received (stored per listing), response latency.

### B5. Product & tech
- Stack detection (Wappalyzer-style headers/HTML): hosting, framework, third-party APIs → transfer difficulty
  and monthly cost; single-supplier risk (one API = whole product).
- Repo/code review checklist if source is provided: license of dependencies, secrets in repo, deploy docs,
  test presence; time-to-first-deploy by a stranger.
- Store-policy risk: category on any platform's "under review" lists (downloaders, scrapers, VPNs, AI wrappers);
  permission scope; last policy change dates.
- Maintenance reality: issue tracker/backlog, support email volume claimed vs review complaints, uptime.

### B6. Community & sentiment (the "forums" question: evidence only, never score input)
- Product-scoped sources: store reviews, GitHub issues, Product Hunt comments, seller's own IH/Reddit posts,
  Trustpilot/G2 if any.
- Marketplace-community scam checks: r/Flippa, r/EntrepreneurRideAlong, Acquire community, by seller name/domain.
- Strict matching (domain or unique product name + category), each hit stored with source/date/link, shown as
  a list; the skeptic pass may cite contradictions with seller claims, nothing more.

### B7. Legal & transfer
- Trademark search (USPTO/EUIPO free search) on the brand; domain disputes.
- Data/GDPR: does it store user data; privacy policy present; email list consent.
- Transferability: store developer account transfer rules (Apple/Google/Chrome), payment account migration,
  domain unlock/auth code, ad-network account transfer (AdSense can't be transferred, new account = re-approval).
- Escrow + APA template with 30-day support and rep/warranty on revenue; non-compete.

### B8. Valuation & offer
- Comparable closed sales for the category (from our price history + marketplace "sold" data).
- Multiple bands: monthly-profit multiple vs category median; adjust for verified/unverified, age, platform.
- Offer builder: target payback (18/21/24 mo) → price; earn-out option if numbers unverifiable; walk-away price.
- Scenario table: revenue −25% / −50% cases → payback; "what has to be true" list.

### B9. Process & scoring of diligence itself
- Diligence checklist state per listing (todo/done/n-a), evidence attachments (screenshots, exports, chat logs).
- Diligence verdict separate from scout verdict: PROCEED / MORE INFO / WALK, with the top-3 blockers.
- Claude re-judge with evidence: skeptic pass reads B1-B8 findings and the seller's answers.
- Timeline: auction end, seller response deadlines, escrow milestones.

## Suggested build order
B1 + B2 (automatic, free, high signal) → B8 (uses data we already have) → B6 (contained) → B9 workspace →
B3/B4 helpers (mostly checklists + storage) → B5/B7 (checklists + a few lookups).
