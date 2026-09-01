"""Import site cookies from the user's real Firefox profile into the app's Playwright browser profile,
so `login <site>` in the bare Playwright window is never needed."""
import configparser, sqlite3, time
from pathlib import Path

FF_DIRS = [Path.home() / ".config/mozilla/firefox", Path.home() / ".mozilla/firefox",
           Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox"]


def _default_profile() -> Path | None:
    for base in FF_DIRS:
        ini = base / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        # prefer the [Install*] Default= entry (the profile Firefox actually launches)
        for sec in cp.sections():
            if sec.startswith("Install") and cp[sec].get("Default"):
                p = base / cp[sec]["Default"]
                if (p / "cookies.sqlite").exists():
                    return p
        for sec in cp.sections():
            if cp[sec].get("Default") == "1" and cp[sec].get("Path"):
                p = base / cp[sec]["Path"]
                if (p / "cookies.sqlite").exists():
                    return p
    return None


def read_cookies(domain: str) -> list[dict]:
    prof = _default_profile()
    if not prof:
        raise RuntimeError("no Firefox profile with cookies.sqlite found")
    uri = f"file:{prof / 'cookies.sqlite'}?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    rows = con.execute(
        "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite FROM moz_cookies WHERE host LIKE ?",
        (f"%{domain}",)).fetchall()
    con.close()
    out = []
    for host, name, value, path, expiry, sec, http, same in rows:
        out.append({"name": name, "value": value, "domain": host, "path": path or "/",
                    "expires": (float(expiry if expiry < 1e11 else expiry / 1000) if expiry and (expiry if expiry < 1e11 else expiry / 1000) > time.time() else -1),
                    "secure": bool(sec), "httpOnly": bool(http),
                    "sameSite": {0: "None", 1: "Lax", 2: "Strict"}.get(same, "Lax")})
    return out


def import_cookies(domain: str) -> int:
    """Copy <domain> cookies from real Firefox into the Playwright persistent profile."""
    from playwright.sync_api import sync_playwright
    cookies = read_cookies(domain)
    if not cookies:
        raise RuntimeError(f"no {domain} cookies in your Firefox profile — log in to https://{domain} in Firefox first")
    profile = str(Path(__file__).resolve().parents[1] / ".browser")
    with sync_playwright() as pw:
        ctx = pw.firefox.launch_persistent_context(profile, headless=True)
        ctx.add_cookies(cookies)
        ctx.close()
    return len(cookies)
