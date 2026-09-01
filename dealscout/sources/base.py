import time, logging, httpx

log = logging.getLogger("dealscout")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 DealScout/0.1"


def client(**kw) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9,*/*;q=0.8"},
                        timeout=30, follow_redirects=True, **kw)


def get(http: httpx.Client, url: str, retries: int = 3, delay: float = 0.6, **kw) -> httpx.Response:
    """Polite GET with backoff. Raises on final failure."""
    for i in range(retries):
        try:
            r = http.get(url, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            time.sleep(delay)
            return r
        except (httpx.HTTPError, httpx.TransportError) as e:
            if i == retries - 1:
                raise
            log.warning("retry %s (%s)", url, e)
            time.sleep(2 ** i * 1.5)
    raise RuntimeError("unreachable")


def is_challenge(html: str) -> bool:
    h = html[:4000].lower()
    return ("just a moment" in h and "cloudflare" in h) or "cf-turnstile" in h or "access denied" in h[:600]
