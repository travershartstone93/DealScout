from dataclasses import dataclass, field, asdict
from typing import Optional
import json

CATEGORIES = ("saas", "chrome_extension", "mobile_app", "newsletter", "content_site",
              "ecommerce", "fba", "service", "domain", "marketplace", "other")


@dataclass
class Listing:
    id: str                       # "source:native_id"
    source: str
    url: str
    title: str
    category: str = "other"
    asking_price: Optional[float] = None
    monthly_profit: Optional[float] = None
    monthly_revenue: Optional[float] = None
    margin: Optional[float] = None          # percent
    customers: Optional[int] = None         # paying users / subscribers / sales count
    users_free: Optional[int] = None
    age_months: Optional[float] = None
    churn_pct: Optional[float] = None
    verified_revenue: bool = False
    verified_traffic: bool = False
    sale_method: str = ""
    ends_at: Optional[str] = None
    status: str = "open"                    # open | sold | ended | stale
    reason_for_selling: str = ""
    summary: str = ""
    hours_per_week: Optional[float] = None
    monetization: str = ""                  # recurring | one_off | ads | mixed | ""
    raw: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        d = asdict(self)
        d["raw_json"] = json.dumps(d.pop("raw"), default=str)[:20000]
        return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id TEXT PRIMARY KEY, source TEXT, url TEXT, title TEXT, category TEXT,
  asking_price REAL, monthly_profit REAL, monthly_revenue REAL, margin REAL,
  customers INTEGER, users_free INTEGER, age_months REAL, churn_pct REAL,
  verified_revenue INTEGER, verified_traffic INTEGER, sale_method TEXT, ends_at TEXT,
  status TEXT, reason_for_selling TEXT, summary TEXT, hours_per_week REAL, monetization TEXT,
  raw_json TEXT, first_seen TEXT, last_seen TEXT, miss_count INTEGER DEFAULT 0,
  passes INTEGER DEFAULT 0, score REAL, payback_months REAL, payback_75 REAL, payback_65 REAL,
  flags TEXT, fail_reasons TEXT, content_hash TEXT,
  starred INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS judgments (
  listing_id TEXT PRIMARY KEY, content_hash TEXT, model TEXT, judged_at TEXT,
  verdict TEXT, json TEXT, raw_text TEXT
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
  listing_id TEXT, seen_at TEXT, asking_price REAL, status TEXT, bid_count INTEGER, reserve_met INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ph ON price_history(listing_id, seen_at);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, at TEXT, agree INTEGER, comment TEXT,
  verdict TEXT, title TEXT
);
CREATE TABLE IF NOT EXISTS ui_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS signals (
  listing_id TEXT PRIMARY KEY, collected_at TEXT, json TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  listing_id TEXT PRIMARY KEY, checked_at TEXT, outcome TEXT, final_price REAL, sold_at TEXT,
  first_price REAL, days_listed INTEGER, category TEXT, monthly_profit REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, at TEXT, kind TEXT, title TEXT, body TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS checklist (
  listing_id TEXT, item_key TEXT, state TEXT, note TEXT, at TEXT, PRIMARY KEY (listing_id, item_key)
);
CREATE TABLE IF NOT EXISTS chats (
  id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT, at TEXT, role TEXT, text TEXT, session_id TEXT
);
CREATE TABLE IF NOT EXISTS diligence (
  listing_id TEXT PRIMARY KEY, at TEXT, verdict TEXT, json TEXT, offer_json TEXT
);
CREATE TABLE IF NOT EXISTS source_health (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, at TEXT, seen INTEGER, passed INTEGER, error TEXT, secs INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
"""

# columns added after v1 (ALTER TABLE ... is applied by db.migrate)
MIGRATIONS = [
    "ALTER TABLE listings ADD COLUMN dupe_key TEXT",
    "ALTER TABLE listings ADD COLUMN price_prev REAL",
    "ALTER TABLE listings ADD COLUMN relisted INTEGER DEFAULT 0",
    "ALTER TABLE judgments ADD COLUMN verdict1 TEXT",
    "ALTER TABLE judgments ADD COLUMN json2 TEXT",
    "ALTER TABLE judgments ADD COLUMN model2 TEXT",
    "ALTER TABLE listings ADD COLUMN watch INTEGER DEFAULT 0",
    "ALTER TABLE listings ADD COLUMN bid_count INTEGER",
    "ALTER TABLE listings ADD COLUMN reserve_met INTEGER",
]
