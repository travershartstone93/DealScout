"""Comparables: category multiples (asking / monthly profit), sold multiples from `outcomes`, and a
one-line position for a listing. Multiples use monthly profit; outliers (<=0 or >600x) are dropped."""
import math
from statistics import median

MAX_MULT = 600.0
MIN_PROFIT = 20.0        # $/mo below this a multiple is noise


def _g(row, k, default=None):
    """Field access for sqlite Row / dict / Listing."""
    if row is None:
        return default
    if hasattr(row, "keys"):
        return row[k] if k in row.keys() else default
    return getattr(row, k, default)


def _mult(price, profit):
    if price and profit and profit >= MIN_PROFIT:
        m = price / profit
        if 0 < m <= MAX_MULT:
            return round(m, 1)
    return None


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = (len(s) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return round(s[lo] + (s[hi] - s[lo]) * (i - lo), 1)


def _open_rows(con, category=None):
    sql = ("SELECT id, title, url, source, category, asking_price, monthly_profit, status FROM listings "
           "WHERE status='open' AND hidden=0 AND asking_price>0 AND monthly_profit>0")
    args = ()
    if category:
        sql += " AND category=?"
        args = (category,)
    return con.execute(sql, args).fetchall()


def _sold_rows(con, category=None):
    sql = ("SELECT l.id, l.title, l.url, l.source, l.category, l.asking_price, o.final_price, o.first_price, "
           "o.days_listed, o.sold_at, COALESCE(o.monthly_profit, l.monthly_profit) AS monthly_profit "
           "FROM outcomes o JOIN listings l ON l.id=o.listing_id WHERE o.outcome='sold'")
    args = ()
    if category:
        sql += " AND l.category=?"
        args = (category,)
    return con.execute(sql, args).fetchall()


def category_benchmarks(con) -> dict:
    """category -> {n_open, n_sold, median_multiple_ask, p25, p75, median_multiple_sold, median_days_to_sell, sold_to_ask_ratio}."""
    out = {}
    ask, sold, days, ratio, n_open, n_sold = {}, {}, {}, {}, {}, {}
    for r in _open_rows(con):
        c = r["category"]
        n_open[c] = n_open.get(c, 0) + 1
        m = _mult(r["asking_price"], r["monthly_profit"])
        if m:
            ask.setdefault(c, []).append(m)
    for r in _sold_rows(con):
        c = r["category"]
        n_sold[c] = n_sold.get(c, 0) + 1
        m = _mult(r["final_price"], r["monthly_profit"])
        if m:
            sold.setdefault(c, []).append(m)
        # rows first seen already sold (days 0, final == first) carry no timing / discount information
        informative = (r["days_listed"] or 0) > 0 or (r["final_price"] and r["first_price"] and r["final_price"] != r["first_price"])
        if informative and r["days_listed"] is not None:
            days.setdefault(c, []).append(r["days_listed"])
        if informative and r["final_price"] and r["first_price"]:
            ratio.setdefault(c, []).append(r["final_price"] / r["first_price"])
    for c in set(n_open) | set(n_sold):
        a = ask.get(c, [])
        out[c] = {"n_open": n_open.get(c, 0), "n_sold": n_sold.get(c, 0), "n_ask_multiples": len(a),
                  "median_multiple_ask": round(median(a), 1) if a else None,
                  "p25": _pct(a, 0.25), "p75": _pct(a, 0.75),
                  "median_multiple_sold": round(median(sold[c]), 1) if sold.get(c) else None,
                  "median_days_to_sell": round(median(days[c])) if days.get(c) else None,
                  "sold_to_ask_ratio": round(median(ratio[c]), 2) if ratio.get(c) else None}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n_open"]))


def comps_for(con, listing_row, k: int = 8) -> list:
    """k most similar open/sold listings: same category, monthly_profit 0.5-2x (asking 0.5-2x if no profit); sold first."""
    lid, cat = _g(listing_row, "id"), _g(listing_row, "category")
    profit, price = _g(listing_row, "monthly_profit"), _g(listing_row, "asking_price")
    key = "monthly_profit" if profit and profit > 0 else "asking_price"
    ref = profit if key == "monthly_profit" else price
    if not ref or not cat:
        return []
    cands = []
    for r in _sold_rows(con, cat):
        v = r[key] if key == "monthly_profit" else (r["final_price"] or r["asking_price"])
        if r["id"] != lid and v and 0.5 * ref <= v <= 2 * ref:
            cands.append((0, abs(math.log(v / ref)), r, "sold"))
    for r in _open_rows(con, cat):
        v = r[key]
        if r["id"] != lid and v and 0.5 * ref <= v <= 2 * ref:
            cands.append((1, abs(math.log(v / ref)), r, "open"))
    cands.sort(key=lambda t: (t[0], t[1]))
    out = []
    for _, _, r, st in cands[:k]:
        final = r["final_price"] if st == "sold" else None
        out.append({"id": r["id"], "title": (r["title"] or "")[:90], "url": r["url"], "source": r["source"],
                    "asking": r["asking_price"], "final_price": final, "monthly_profit": r["monthly_profit"],
                    "multiple": _mult(final or r["asking_price"], r["monthly_profit"]), "status": st,
                    "days_listed": r["days_listed"] if st == "sold" else None})
    return out


def position(con, listing_row) -> dict:
    """{multiple, category_median, percentile, n, note} - e.g. 'asks 27x monthly profit; category median 19x (n=41)'."""
    cat = _g(listing_row, "category")
    m = _mult(_g(listing_row, "asking_price"), _g(listing_row, "monthly_profit"))
    bm = category_benchmarks(con).get(cat) or {}
    med, n = bm.get("median_multiple_ask"), bm.get("n_ask_multiples", 0)
    if m is None:
        note = "no usable profit figure - cannot compute a multiple"
        if med:
            note += f"; category median asks {med:g}x monthly profit (n={n})"
        return {"multiple": None, "category_median": med, "percentile": None, "n": n, "note": note}
    ask = [_mult(r["asking_price"], r["monthly_profit"]) for r in _open_rows(con, cat)]
    ask = [x for x in ask if x]
    pctl = round(100 * sum(1 for x in ask if x < m) / len(ask)) if ask else None
    note = f"asks {m:g}x monthly profit"
    if med:
        note += f"; category median {med:g}x (n={n})"
        if pctl is not None:
            note += f" - pricier than {pctl}% of open {cat} listings"
    else:
        note += "; no category benchmark yet"
    if bm.get("median_multiple_sold"):
        note += f"; sold comps closed at {bm['median_multiple_sold']:g}x (n={bm['n_sold']})"
    if bm.get("sold_to_ask_ratio"):
        note += f", ~{round(100 * bm['sold_to_ask_ratio'])}% of first ask"
    return {"multiple": m, "category_median": med, "percentile": pctl, "n": n, "note": note}


if __name__ == "__main__":  # python -m dealscout.comps [listing id]
    import sys, json
    from . import db
    con = db.connect()
    print(json.dumps(category_benchmarks(con), indent=1))
    if len(sys.argv) > 1:
        r = con.execute("SELECT * FROM listings WHERE id=? OR id LIKE ?", (sys.argv[1], f"%:{sys.argv[1]}")).fetchone()
        print(json.dumps(position(con, r), indent=1))
        print(json.dumps(comps_for(con, r), indent=1))
