"""Registry of listing sources. Each module exposes `NAME` and `fetch(cfg, http) -> Iterable[Listing]`."""
import importlib

TIER1 = ["flippa", "empireflippers", "motioninvest"]
TIER2 = ["acquire", "littleexits", "nicheinvestor", "indiemaker", "buymicrostartups", "indieexit", "extensiondeal",
         "webstoreextensions", "exitbid", "lettertrader", "buysellstartups"]
BROWSER = ["sideprojectors", "chromestats", "transferslot", "bizbuysell", "investorsclub"]
ALL = TIER1 + TIER2 + BROWSER


def get(name: str):
    if name in BROWSER:
        mod = importlib.import_module("dealscout.sources.browser")
        return getattr(mod, f"fetch_{name}")
    return importlib.import_module(f"dealscout.sources.{name}").fetch
