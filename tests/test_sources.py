"""Replay recorded fixtures (tests/fixtures/<source>.json) through each HTTP source's fetch().
Run: .venv/bin/python -m unittest tests/test_sources.py"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx  # noqa: E402
from dealscout import load_config  # noqa: E402
from dealscout.models import Listing  # noqa: E402
from dealscout.sources import TIER1, TIER2, get as get_source  # noqa: E402
from dealscout.sources import base  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAX_LISTINGS = 15
# sites where most sellers genuinely list no price ("Free" / "make an offer"): lower the priced-share bar
PRICED_SHARE = {"webstoreextensions": 0.25, "buysellstartups": 0.35}


class FakeResponse:
    def __init__(self, url, rec):
        self.url = httpx.URL(url)
        self.status_code = rec["status"]
        self.text = rec["text"]
        self.headers = httpx.Headers(rec.get("headers") or {})
        self.request = httpx.Request("GET", self.url)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code), request=self.request, response=self)
        return self


class FakeHttp:
    """Serves recorded responses; unknown URLs raise so the source's own error handling kicks in."""

    def __init__(self, requests: dict):
        self.requests = requests
        self.misses = []

    def get(self, url, params=None, headers=None, **kw):
        u = httpx.URL(url)
        if params:
            u = u.copy_merge_params(params)
        key = str(u)
        rec = self.requests.get(key)
        if rec is None:
            self.misses.append(key)
            raise httpx.ConnectError(f"not in fixture: {key}")
        return FakeResponse(key, rec)


def load_fixture(name):
    p = FIXTURES / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def run_source(name, fx, cfg):
    http = FakeHttp(fx["requests"])
    out = []
    gen = get_source(name)(cfg, http)
    for l in gen:
        out.append(l)
        if len(out) >= MAX_LISTINGS:
            gen.close()
            break
    return out, http


class SourceFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls._sleep = base.time.sleep
        base.time.sleep = lambda s: None  # skip politeness delay / backoff

    @classmethod
    def tearDownClass(cls):
        base.time.sleep = cls._sleep

    def check(self, name):
        fx = load_fixture(name)
        if fx is None:
            self.skipTest(f"no fixture for {name}: run tests/record_fixtures.py {name}")
        listings, http = run_source(name, fx, self.cfg)
        self.assertGreaterEqual(len(listings), 1, f"{name}: no listings from fixture (misses={http.misses[:3]})")
        for l in listings:
            self.assertIsInstance(l, Listing)
            self.assertTrue(l.id and l.url and l.title, f"{name}: missing id/url/title in {l.id!r}")
            self.assertTrue(l.id.count(":") >= 1, f"{name}: id not source:native ({l.id})")
            for f in ("asking_price", "monthly_profit", "monthly_revenue"):
                v = getattr(l, f)
                self.assertTrue(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)),
                                f"{name}: {f}={v!r} ({type(v).__name__}) in {l.id}")
        priced = sum(1 for l in listings if l.asking_price is not None)
        need = PRICED_SHARE.get(name, 0.5) * len(listings)
        self.assertGreaterEqual(priced, need, f"{name}: only {priced}/{len(listings)} have asking_price")


def _make(name):
    def test(self):
        self.check(name)
    test.__name__ = f"test_{name}"
    return test


for _n in TIER1 + TIER2:
    setattr(SourceFixtureTests, f"test_{_n}", _make(_n))

if __name__ == "__main__":
    unittest.main()
