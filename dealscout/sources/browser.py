"""Playwright/Firefox sources for sites behind bot-protection or JS-only rendering.
Persistent profile lives in ~/dealscout/.browser; `dealscout login <site>` opens a headed window once."""
import json, re
from pathlib import Path
from ..models import Listing
from ..normalize import money, intval, category, extract_customers, monetization, months_since
from .base import log

PROFILE = str(Path(__file__).resolve().parents[2] / ".browser")
LOGIN_URLS = {"flippa": "https://flippa.com/login", "investorsclub": "https://investors.club/login/", "acquire": "https://app.acquire.com/login",
              "sideprojectors": "https://www.sideprojectors.com/login", "littleexits": "https://app.littleexits.com/login",
              "chromestats": "https://chrome-stats.com/marketplace/forsale", "bizbuysell": "https://www.bizbuysell.com/"}
FETCH_JS = 'u => fetch(u,{headers:{"X-Requested-With":"XMLHttpRequest","Accept":"application/json"}}).then(r=>r.text())'


def _ctx(pw, headless=True):
    return pw.firefox.launch_persistent_context(PROFILE, headless=headless, viewport={"width": 1400, "height": 900})


def login(site: str):
    from playwright.sync_api import sync_playwright
    url = LOGIN_URLS.get(site, f"https://{site}")
    print(f"Opening {url} - log in, then close the window. Session is saved to {PROFILE}.")
    with sync_playwright() as pw:
        ctx = _ctx(pw, headless=False)
        pg = ctx.new_page()
        pg.goto(url)
        try:
            pg.wait_for_event("close", timeout=0)
        except Exception:
            pass
        ctx.close()


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


# ---------------------------------------------------------------- SideProjectors (JSON behind Cloudflare)
def fetch_sideprojectors(cfg, http):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _ctx(pw)
        pg = ctx.new_page()
        cap = []
        pg.on("response", lambda r: cap.append(r.url) if "sideprojectors.com/project/data" in r.url else None)
        pg.goto("https://www.sideprojectors.com/", wait_until="domcontentloaded")
        pg.wait_for_timeout(6000)
        if not cap:
            ctx.close()
            raise RuntimeError("blocked by bot protection (no data XHR captured)")
        q = re.search(r"query=([^&]+)", cap[0]).group(1)
        offset, seen = 0, 0
        while offset < 600:
            url = ("https://www.sideprojectors.com/project/data?savedSearchId=all&query=" + q +
                   "&postTypes=sell&projectTypes=SaaS%2CShop%2CBlog%2CWebsite%2CMobile%2CDesktop%2CBrowser%2CDomain%2COther"
                   f"&projectPrice=all&revenue=all&projectDate=all&marketId=all&orderBy=created_at&orderType=desc&limit=50&offset={offset}")
            try:
                j = json.loads(pg.evaluate(FETCH_JS, url))
            except Exception as e:
                raise RuntimeError(f"sideprojectors data fetch failed: {e}")
            items = j.get("projects", [])
            for d in items:
                if d.get("post_type") != "sell" or d.get("item_status") in ("sold", "closed"):
                    continue
                yield _sp_convert(d)
                seen += 1
            if len(items) < 50:
                break
            offset += 50
        ctx.close()
        log.info("sideprojectors: %d", seen)


def _sp_convert(d: dict) -> Listing:
    desc = _strip(d.get("description"))
    rev = d.get("revenue") or {}
    text = f"{d.get('name','')} {d.get('pitch','')} {desc} {_strip(rev.get('explanation'))}"
    paying, free = extract_customers(text)
    monthly_rev = None
    rr = rev.get("revenue_range")
    if rr:
        nums = [money(x) for x in re.findall(r"\$?\d[\d,\.]*[kK]?", str(rr))]
        nums = [n for n in nums if n]
        if nums:
            monthly_rev = sum(nums) / len(nums)
    for m in d.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        k = str(m.get("name") or m.get("key") or m.get("label") or "").lower()
        v = money(m.get("value"))
        if v is None:
            continue
        if "mrr" in k or ("revenue" in k and "month" in k):
            monthly_rev = v
        elif "customer" in k or "subscriber" in k or "paying" in k:
            paying = paying or int(v)
        elif "user" in k:
            free = free or int(v)
    return Listing(
        id=f"sp:{d['id']}", source="sideprojectors", url=f"https://www.sideprojectors.com/project/{d['id']}",
        title=(d.get("name") or "")[:200], category=category(d.get("project_type")),
        asking_price=money(d.get("offer_price")) if d.get("offer_price") else None, monthly_revenue=monthly_rev,
        customers=paying, users_free=free, age_months=months_since(d.get("approved_at") or d.get("created_at_proper")),
        verified_revenue=False, sale_method="auction" if d.get("auction_ends_at") else "classified",
        ends_at=d.get("auction_ends_at"), status="open", summary=(d.get("pitch", "") + " " + desc)[:2000],
        monetization=monetization(text),
        raw={"has_revenue": rev.get("has_revenue"), "revenue_range": rr, "price_note": d.get("price"),
             "num_bids": d.get("num_bids"), "identity_verified": d.get("is_identity_verified")},
    )


# ---------------------------------------------------------------- Transferslot (SSR HTML)
def fetch_transferslot(cfg, http):
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup
    with sync_playwright() as pw:
        ctx = _ctx(pw)
        pg = ctx.new_page()
        pg.goto("https://transferslot.com/", wait_until="domcontentloaded")
        pg.wait_for_timeout(4000)
        html = pg.content()
        ctx.close()
    soup = BeautifulSoup(html, "lxml")
    items = soup.select("li.product")
    if not items:
        raise RuntimeError("blocked or layout changed (no li.product)")
    for li in items:
        state = li.select_one(".product__state")
        if state and "still on sale" not in state.get_text(" ", strip=True).lower():
            continue
        a = li.select_one("a[href]")
        slug = a["href"].rstrip("/").split("/")[-1]
        title = li.select_one("h2").get_text(strip=True) if li.select_one("h2") else slug
        pitch = li.select_one("p").get_text(" ", strip=True) if li.select_one("p") else ""
        metrics = {m.select_one(".product__label").get_text(strip=True).lower(): money(m.select_one(".product__metric").get_text())
                   for m in li.select("ul li") if m.select_one(".product__label")}
        t = li.select_one("time")
        yield Listing(
            id=f"ts:{slug}", source="transferslot", url=f"https://transferslot.com/products/{slug}", title=title[:200],
            category=category(li.get("data-category"), pitch) if li.get("data-category") else category(pitch, "saas"),
            asking_price=money(li.get("data-price")) or metrics.get("asking price"),
            monthly_revenue=money(li.get("data-mrr")) or metrics.get("mrr"), monthly_profit=metrics.get("profits"),
            customers=extract_customers(title + " " + pitch)[0], users_free=extract_customers(title + " " + pitch)[1],
            age_months=months_since(t.get("datetime")) if t else None, sale_method="classified", status="open",
            summary=pitch[:2000], monetization="recurring" if li.get("data-mrr") not in (None, "", "0") else monetization(pitch),
            raw={"listed_at": t.get("datetime") if t else None},
        )


# ---------------------------------------------------------------- Investors.Club (login required)
def fetch_investorsclub(cfg, http):
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup
    with sync_playwright() as pw:
        ctx = _ctx(pw)
        pg = ctx.new_page()
        pg.goto("https://investors.club/listings/", wait_until="domcontentloaded")
        pg.wait_for_timeout(5000)
        body = pg.inner_text("body").lower()
        if "sign in" in body[:3000] and "asking" not in body:
            ctx.close()
            raise RuntimeError("not logged in - run `dealscout login investorsclub`")
        cards = pg.eval_on_selector_all("a[href*='/listing/']", "els => [...new Set(els.map(e => e.href))]")
        out = []
        for u in cards[:60]:
            pg.goto(u, wait_until="domcontentloaded")
            pg.wait_for_timeout(1500)
            out.append((u, pg.content()))
        ctx.close()
    for u, html in out:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        if re.search(r"\b(sold|under offer)\b", text[:2000], re.I):
            continue
        def grab(label):
            m = re.search(label + r"[^$\d]{0,40}\$?\s?([\d,\.]+\s?[kK]?)", text, re.I)
            return money(m.group(1)) if m else None
        title = soup.title.get_text(strip=True) if soup.title else u
        paying, free = extract_customers(text[:5000])
        yield Listing(
            id=f"ic:{u.rstrip('/').split('/')[-1]}", source="investorsclub", url=u, title=title[:200],
            category=category(text[:3000]), asking_price=grab(r"asking price"),
            monthly_profit=grab(r"(?:monthly|avg\.? monthly|net) profit"), monthly_revenue=grab(r"(?:monthly|avg\.? monthly) revenue"),
            customers=paying, users_free=free, verified_revenue=True, sale_method="classified", status="open",
            summary=text[:2000], monetization=monetization(text[:5000]), raw={},
        )


# ---------------------------------------------------------------- Not reachable headlessly (verified 2026-08-17)
def fetch_chromestats(cfg, http):
    raise RuntimeError("chrome-stats.com marketplace sits behind Cloudflare Turnstile - headless Firefox is challenged; "
                       "run `dealscout login chromestats`, solve the challenge once, then re-enable")


def fetch_bizbuysell(cfg, http):
    raise RuntimeError("bizbuysell.com (Akamai) times out for automated browsers; left disabled")
