"""Record HTTP fixtures per scraper: run each HTTP source live, capture every response, keep ~15 listings.
Usage: .venv/bin/python tests/record_fixtures.py [source ...]"""
import sys, json, logging, time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
from dealscout import load_config  # noqa: E402
from dealscout.sources import TIER1, TIER2, get as get_source  # noqa: E402
from dealscout.sources import base  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAX_LISTINGS = 15
MAX_BODY = 2 * 1024 * 1024


class RecordingClient(httpx.Client):
    """httpx.Client that stores every GET response keyed by its fully-resolved URL."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.requests = {}

    def get(self, url, **kw):
        r = super().get(url, **kw)
        text = r.text
        if len(text) > MAX_BODY:
            text = text[:MAX_BODY]
        self.requests[str(r.url)] = {"status": r.status_code, "content_type": r.headers.get("content-type", ""),
                                     "headers": {k: v for k, v in r.headers.items()
                                                 if k.lower().startswith("x-wp-") or k.lower() == "content-type"},
                                     "text": text}
        return r


def record(name: str, cfg: dict) -> dict:
    hdr = {"User-Agent": base.UA, "Accept": "application/json, text/html;q=0.9,*/*;q=0.8"}
    with RecordingClient(headers=hdr, timeout=30, follow_redirects=True) as http:
        listings, err = [], None
        t0 = time.time()
        try:
            gen = get_source(name)(cfg, http)
            for l in gen:
                listings.append(l)
                if len(listings) >= MAX_LISTINGS:
                    gen.close()
                    break
        except Exception as e:  # keep whatever was captured; report the error
            err = f"{type(e).__name__}: {e}"
        out = {"source": name, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "n_listings": len(listings), "error": err, "secs": round(time.time() - t0, 1),
               "requests": http.requests, "sample": [asdict(l) for l in listings[:3]]}
    (FIXTURES / f"{name}.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config()
    names = sys.argv[1:] or TIER1 + TIER2
    FIXTURES.mkdir(exist_ok=True)
    for name in names:
        out = record(name, cfg)
        size = (FIXTURES / f"{name}.json").stat().st_size
        print(f"{name:20s} listings={out['n_listings']:3d} requests={len(out['requests']):3d} "
              f"{size/1024:7.0f}KB {out['secs']}s {out['error'] or ''}")


if __name__ == "__main__":
    main()
