"""ExtensionDeal — Next.js SSR directory; cards carry users / asking / reported revenue in plain HTML.
Names are paywalled on the cards but leak in the detail page <title>."""
import re
from bs4 import BeautifulSoup
from ..models import Listing
from ..normalize import money, intval, extract_customers, monetization
from .base import get, log

NAME = "extensiondeal"
BASE = "https://extensiondeal.com"


def fetch(cfg, http):
    seen = set()
    for page in range(1, 20):
        r = get(http, f"{BASE}/extensions", params={"page": page} if page > 1 else None)
        soup = BeautifulSoup(r.text, "lxml")
        cards = [a for a in soup.select("a[href^='/extensions/ext-']") if a.select_one("dl")]
        new = [c for c in cards if c["href"] not in seen]
        if not new:
            break
        for c in new:
            seen.add(c["href"])
            yield _convert(c, http)


def _dl(card) -> dict:
    out = {}
    for div in card.select("dl > div"):
        dt, dd = div.find("dt"), div.find("dd")
        if dt and dd:
            out[dt.get_text(" ", strip=True).lower()] = dd.get_text(" ", strip=True)
    return out


def _convert(card, http) -> Listing:
    href = card["href"]
    url = BASE + href
    slug = href.rsplit("/", 1)[-1]
    fields = _dl(card)
    cat_p = card.select_one("h3 + p, p.text-neutral-500")
    niche = cat_p.get_text(strip=True) if cat_p else ""
    desc_p = card.select_one("p.line-clamp-2") or card.find("p", class_=re.compile("neutral-600"))
    desc = desc_p.get_text(" ", strip=True) if desc_p else ""
    title = ""
    try:
        d = get(http, url)
        ds = BeautifulSoup(d.text, "lxml")
        t = ds.title.get_text(strip=True) if ds.title else ""
        m = re.search(r"—\s*(.+?)\s+with\s+[\d,]+\s+users", t)
        if m:
            title = m.group(1).strip()
        full = ds.find(string=re.compile(r"^\s*Description\s*$"))
        if full and full.find_parent():
            nxt = full.find_parent().find_next_sibling()
            if nxt:
                desc = nxt.get_text(" ", strip=True) or desc
        for div in ds.select("dl > div"):
            dt, dd = div.find("dt"), div.find("dd")
            if dt and dd:
                fields.setdefault(dt.get_text(" ", strip=True).lower(), dd.get_text(" ", strip=True))
    except Exception as e:  # detail page is a nicety, not required
        log.warning("extensiondeal detail %s: %s", url, e)
    users = intval(fields.get("users") or fields.get("monthly users"))
    rev_txt = next((v for k, v in fields.items() if k.startswith("revenue")), None)
    rev = money(rev_txt)
    paying, free = extract_customers(desc)
    return Listing(
        id=f"ed:{slug}", source=NAME, url=url,
        title=(title or f"{niche or 'Chrome'} extension ({users or '?'} users)")[:200],
        category="chrome_extension", asking_price=money(fields.get("asking") or fields.get("asking price")),
        monthly_revenue=rev, customers=paying, users_free=free or users, verified_revenue=False,
        sale_method="classified", status="open", summary=desc[:2000],
        monetization=monetization(desc), raw={"niche": niche, "fields": fields},
    )
