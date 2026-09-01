"""BuySellStartups — Next.js RSC; /startups is login-gated but /browse/<category> renders every card in HTML.
Named listings hide the asking price ("Sign in →") but show MRR + multiple, so asking ≈ ARR × multiple
(flagged in raw as derived). Confidential listings show asking but hide the name/MRR.
Detail pages (/listings/<slug>) add ARR, profit, founded year, team size and the description."""
import re
from datetime import date
from bs4 import BeautifulSoup
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization
from .base import get, log

NAME = "buysellstartups"
BASE = "https://buysellstartups.com"
CATS = ["saas", "developer-tools", "mobile-app", "productivity", "content", "e-commerce", "marketplace", "other"]


def fetch(cfg, http):
    max_price = cfg["filters"]["max_price"]
    seen = set()
    cats = list(CATS)
    while cats:
        c = cats.pop(0)
        r = get(http, f"{BASE}/browse/{c}")
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select("a[href^='/browse/']"):
            slug = a["href"].split("/browse/")[-1].split("?")[0]
            if slug and slug not in cats and slug not in CATS:
                cats.append(slug)
        for art in soup.select("article"):
            a = art.find_parent("a")
            if not a or not a.get("href", "").startswith("/listings/"):
                continue
            card = _card(art, a["href"], c)
            if card["slug"] in seen:
                continue
            seen.add(card["slug"])
            detail = {}
            est = card["asking"] or card["asking_est"]
            if est is None or est <= max_price * 3:
                try:
                    detail = _detail(http, card["url"])
                except Exception as e:
                    log.warning("buysellstartups detail %s: %s", card["url"], e)
            yield _convert(card, detail)


def _kv(root) -> dict:
    out = {}
    for div in root.select("div"):
        kids = div.find_all(["p", "span"], recursive=False)
        if len(kids) == 2:
            out.setdefault(kids[0].get_text(strip=True).lower(), kids[1].get_text(" ", strip=True))
    return out


def _num(v):
    if v is None or "•" in v or v.strip() in ("—", "-", "") or "sign in" in v.lower():
        return None
    return money(v)


def _card(art, href, cat_slug) -> dict:
    href = href.split("?")[0]
    kv = _kv(art)
    h2 = art.select_one("h2")
    title = h2.get_text(strip=True) if h2 else ""
    tag = art.select_one("p.line-clamp-2") or art.select_one("h2 ~ p")
    tagline = tag.get_text(" ", strip=True) if tag else ""
    if tagline.lower().startswith("seller has chosen"):
        tagline = ""
    badges = [s.get_text(strip=True) for s in art.select("span")]
    mrr = _num(kv.get("mrr"))
    annual = _num(kv.get("annual rev"))
    mult = _num(kv.get("multiple"))
    asking = _num(kv.get("asking"))
    est = None
    if asking is None and mult:
        arr = annual if annual is not None else (mrr * 12 if mrr is not None else None)
        if arr:
            est = round(arr * mult)
    trend = kv.get("revenue trend", "")
    return {
        "slug": href.rsplit("/", 1)[-1], "url": BASE + href, "title": title, "cat_slug": cat_slug,
        "tagline": tagline, "asking": asking, "asking_est": est,
        "mrr": mrr, "annual_rev": annual, "alltime_rev": _num(kv.get("all-time rev")), "multiple": mult,
        "confidential": "Confidential" in badges or title.startswith("[Name"), "featured": "Featured" in badges,
        "trend": trend, "kv": kv,
    }


def _detail(http, url) -> dict:
    soup = BeautifulSoup(get(http, url).text, "lxml")
    out = {}
    for lab in ("MRR", "ARR", "Multiple", "Profit", "Founded", "Team", "Asking price", "Asking"):
        el = soup.find(string=re.compile(rf"^\s*{re.escape(lab)}\s*$"))
        if el and el.find_parent() and el.find_parent().find_next_sibling():
            out[lab.lower()] = el.find_parent().find_next_sibling().get_text(" ", strip=True)[:120]
    body = soup.find("body")
    ps = sorted((p for p in (body or soup).find_all("p")), key=lambda p: len(p.get_text(strip=True)), reverse=True)
    if ps and len(ps[0].get_text(strip=True)) > 80:
        out["description"] = ps[0].get_text(" ", strip=True)
    out["verified"] = bool(soup.find(string=re.compile("Verified")))
    sc = soup.find(string=re.compile(r"^\s*Score\s*$"))
    if sc:
        m = re.search(r"Score\s*(\d+)\s*/\s*10", sc.find_parent().parent.get_text(" ", strip=True))
        if m:
            out["score"] = int(m.group(1))
    tags = [s.get_text(strip=True) for s in soup.select("h1 ~ * span, section span") if s.get_text(strip=True).islower()]
    out["tags"] = sorted(set(tags))[:12]
    return out


def _convert(c: dict, d: dict) -> Listing:
    mrr = c["mrr"] if c["mrr"] is not None else _num(d.get("mrr"))
    arr = _num(d.get("arr"))
    if mrr is None and arr:
        mrr = round(arr / 12, 2)
    if mrr is None and c["annual_rev"]:
        mrr = round(c["annual_rev"] / 12, 2)
    if mrr is not None and mrr < 0:
        mrr = None
    profit = _num(d.get("profit"))
    asking = c["asking"] if c["asking"] is not None else c["asking_est"]
    if asking is None:
        asking = _num(d.get("asking price"))
    founded = intval(d.get("founded"))
    age = None
    if founded and 1990 < founded <= date.today().year:
        age = max((date.today().year - founded) * 12 + date.today().month - 6, 1)
    desc = " ".join(x for x in (c["tagline"], d.get("description")) if x)
    paying, free = extract_customers(desc)
    mon = monetization(desc)
    if mrr and not mon:
        mon = "recurring"
    cat = category(c["cat_slug"], *d.get("tags", []))
    if cat == "other" and re.search(r"extension", desc, re.I):
        cat = "chrome_extension"
    if cat == "other" and c["cat_slug"] in ("developer-tools", "productivity"):
        cat = "saas"
    if c["cat_slug"] == "content":
        cat = "content_site"
    return Listing(
        id=f"bss:{c['slug']}", source=NAME, url=c["url"],
        title=(c["title"] if not c["confidential"]
               else f"[Confidential] {(c['tagline'] or d.get('description', ''))[:90] or c['cat_slug']}")[:200],
        category=cat, asking_price=asking, monthly_profit=profit, monthly_revenue=mrr,
        margin=round(100 * profit / mrr, 1) if profit and mrr else None,
        customers=paying, users_free=free, age_months=age, verified_revenue=bool(d.get("verified")),
        sale_method="classified", status="open", summary=desc[:2000], monetization=mon,
        raw={"asking_derived": c["asking"] is None and c["asking_est"] is not None, "multiple": c["multiple"],
             "annual_rev": c["annual_rev"], "alltime_rev": c["alltime_rev"], "trend": c["trend"],
             "confidential": c["confidential"], "featured": c["featured"], "team": d.get("team"),
             "score": d.get("score"), "founded": founded, "tags": d.get("tags"), "browse_category": c["cat_slug"]},
    )
