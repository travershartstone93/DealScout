"""LetterTrader (ex-Duuce) — Next.js SSR. Public surface is the per-category pages /newsletters/<slug>?page=N
(cards: badge, tagline, list size, asking price). Detail pages (/newsletter/<id>) expose revenue, expenses,
subscriber count, founded year, churn and write time without login."""
import re
from datetime import date
from bs4 import BeautifulSoup
from ..models import Listing
from ..normalize import money, intval, monetization
from .base import get, log

NAME = "lettertrader"
BASE = "https://lettertrader.com"
SEED = ["ai", "finance", "food", "health", "jobs", "local", "self-improvement", "travel", "business", "tech",
        "sports", "media", "crypto", "news", "psychology", "marketing", "education", "lifestyle", "entertainment",
        "real-estate", "gaming", "science", "politics", "productivity", "parenting", "startups", "design",
        "ecommerce", "career", "culture", "religion", "music", "fashion", "environment"]


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    queue, done, seen = list(SEED), set(), set()
    while queue:
        slug = queue.pop(0)
        if slug in done:
            continue
        done.add(slug)
        for page in range(1, 30):
            url = f"{BASE}/newsletters/{slug}"
            r = get(http, url, params={"page": page} if page > 1 else None)
            soup = BeautifulSoup(r.text, "lxml")
            cards = soup.select("article a[href^='/newsletter/']")
            if not cards:
                break
            new = 0
            for a in cards:
                card = _card(a)
                lab = _slug(card["niche"])
                if lab and lab not in done and lab not in queue:
                    queue.append(lab)
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                new += 1
                detail = {}
                if card["status"] == "open" and (card["price"] is None or card["price"] <= max_price * 3):
                    try:
                        detail = _detail(http, card["url"])
                    except Exception as e:
                        log.warning("lettertrader detail %s: %s", card["url"], e)
                yield _convert(card, detail)
            if new == 0 or not soup.select_one(f"a[href='/newsletters/{slug}?page={page + 1}']"):
                break


def _card(a) -> dict:
    href = a["href"].split("?")[0]
    txt = a.get_text(" | ", strip=True)
    badge = a.select_one("span")
    badge_t = badge.get_text(strip=True) if badge else ""
    h3 = a.select_one("h3")
    niche_el = h3.find_next_sibling("span") if h3 else None
    tagline_el = a.select_one("p")
    kv = {}
    for div in a.select("div"):
        spans = div.find_all("span", recursive=False)
        if len(spans) == 2:
            kv[spans[0].get_text(strip=True).lower()] = spans[1].get_text("", strip=True)
    status = "sold" if "sold" in badge_t.lower() else "open"
    return {
        "id": href.rsplit("/", 1)[-1], "url": BASE + href, "badge": badge_t,
        "title": h3.get_text(strip=True) if h3 else "", "niche": niche_el.get_text(strip=True) if niche_el else "",
        "tagline": tagline_el.get_text(" ", strip=True) if tagline_el else "",
        "list_size": intval(kv.get("list size")), "price": money(kv.get("asking price")), "status": status,
        "text": txt,
    }


def _detail(http, url) -> dict:
    soup = BeautifulSoup(get(http, url).text, "lxml")
    out = {}
    for h3 in soup.select("section h3, div h3"):
        val = h3.find_next_sibling(["p", "div"])
        if val is None:
            continue
        v = val.get_text(" ", strip=True)
        if v and len(v) < 80:
            out.setdefault(h3.get_text(strip=True).lower(), v)
    d = soup.select_one("#description-details p")
    if d:
        out["description"] = d.get_text(" ", strip=True)
    for h in soup.select("h3"):
        if h.get_text(strip=True) in ("Strengths", "Opportunities"):
            nxt = h.find_next(["p", "ul", "div"])
            if nxt:
                out[h.get_text(strip=True).lower()] = nxt.get_text(" ", strip=True)[:1500]
    out["verified"] = bool(soup.find(string=re.compile("Seller Verified")))
    lo = soup.find(string=re.compile(r"Listed on"))
    if lo:
        out["listed_on"] = lo.find_parent().get_text(" ", strip=True).replace("Listed on", "").strip()
    return out


def _convert(c: dict, d: dict) -> Listing:
    rev = money(d.get("av. monthly revenue"))
    exp = money(d.get("av. monthly expenses"))
    profit = round(rev - exp, 2) if rev is not None and exp is not None else None
    margin = round(100 * profit / rev, 1) if profit is not None and rev else None
    subs = intval(d.get("subscriber count")) or c["list_size"]
    founded = intval(d.get("founded"))
    age = None
    if founded and 1990 < founded <= date.today().year:
        age = (date.today().year - founded) * 12 + date.today().month - 6
        age = max(age, 1)
    hrs = money(d.get("write time/issue"))
    freq = (d.get("frequency") or "").lower()
    per_week = {"daily": 5, "weekly": 1, "bi-weekly": 0.5, "biweekly": 0.5, "monthly": 0.25}.get(freq)
    hours = round(hrs * per_week, 1) if hrs and per_week else None
    desc = " ".join(x for x in (c["tagline"], d.get("description"), d.get("strengths"), d.get("opportunities")) if x)
    return Listing(
        id=f"lt:{c['id']}", source=NAME, url=c["url"],
        title=f"{c['title']} — {c['tagline']}"[:200] if c["tagline"] else c["title"][:200],
        category="newsletter", asking_price=c["price"], monthly_profit=profit, monthly_revenue=rev, margin=margin,
        customers=None, users_free=subs, age_months=age, churn_pct=money(d.get("churn rate")),
        verified_revenue=bool(d.get("verified")), sale_method="classified", status=c["status"],
        summary=desc[:2000], hours_per_week=hours, monetization=monetization(desc) or ("ads" if rev else ""),
        raw={"badge": c["badge"], "niche": c["niche"], "list_size": c["list_size"], "listed_on": d.get("listed_on"),
             "open_rate": d.get("open rate"), "click_rate": d.get("click rate"), "frequency": d.get("frequency"),
             "acquisition_via": d.get("acquisition via"), "hosting_platform": d.get("hosting platform"),
             "issues_written": d.get("no. of issues written"), "founded": founded, "expenses": exp},
    )
