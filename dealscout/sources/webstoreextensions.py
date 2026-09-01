"""WebStoreExtensions — the acquisition marketplace lives at /sell ("Live listings"); cards are SSR HTML
with installs / upvotes / geo and "Price: $X" (the site's asking price; many sellers put "Free")."""
from bs4 import BeautifulSoup
from ..models import Listing
from ..normalize import money, intval, extract_customers, monetization
from .base import get

NAME = "webstoreextensions"
BASE = "https://webstoreextensions.com"


def fetch(cfg, http):
    r = get(http, f"{BASE}/sell")
    soup = BeautifulSoup(r.text, "lxml")
    seen = set()
    for li in soup.select("li"):
        a = li.select_one("a[href^='/extensions/']")
        if not a or not li.select_one("h3") or "Price:" not in li.get_text():
            continue
        href = a["href"].split("#")[0]
        if href in seen:
            continue
        seen.add(href)
        yield _convert(li, href)


def _convert(li, href) -> Listing:
    slug = href.rsplit("/", 1)[-1]
    title = li.select_one("h3").get_text(" ", strip=True)
    niche_el = li.select_one("p.uppercase")
    niche = niche_el.get_text(strip=True) if niche_el else ""
    desc_el = li.select_one("p.line-clamp-2")
    desc = " ".join(desc_el.get_text(" ", strip=True).split()) if desc_el else ""
    stats = {}
    for cell in li.select("div.text-center"):
        ps = cell.find_all("p")
        if len(ps) >= 2:
            stats[ps[0].get_text(strip=True).lower()] = ps[1].get_text(strip=True)
    price_txt = ""
    for p in li.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t.startswith("Price:"):
            price_txt = t.split(":", 1)[1].strip()
            break
    price = None if price_txt.lower() in ("", "free", "n/a") else money(price_txt)
    installs = intval(stats.get("installs"))
    paying, free = extract_customers(desc)
    store = li.select_one("a[href*='chromewebstore.google.com'], a[href*='chrome.google.com/webstore']")
    return Listing(
        id=f"wse:{slug}", source=NAME, url=BASE + href, title=title[:200], category="chrome_extension",
        asking_price=price, customers=paying, users_free=installs or free, verified_revenue=False,
        sale_method="classified", status="open", summary=desc[:2000], monetization=monetization(desc),
        raw={"niche": niche, "stats": stats, "price_text": price_txt,
             "store_url": store["href"] if store else None},
    )
