"""Per-source scrape health: record each run into source_health, report status, raise alarms."""
import json
from statistics import median
from .db import now

DEGRADED_SHARE = 0.30   # seen < 30% of the median → degraded
MIN_MEDIAN = 5          # ignore sources that never yield much anyway
WINDOW = 10             # runs used for the median


def record(con, source: str, seen: int, passed: int, error, secs) -> None:
    """Insert one run row (call once per source per scan)."""
    con.execute("INSERT INTO source_health (source, at, seen, passed, error, secs) VALUES (?,?,?,?,?,?)",
                (source, now(), int(seen or 0), int(passed or 0), (str(error) if error else None),
                 int(round(secs or 0))))
    con.commit()


def _status(last, med):
    if last is None:
        return "never", False
    if last["error"]:
        return "error", False
    if med >= MIN_MEDIAN and (last["seen"] or 0) == 0:
        return "degraded", True
    if med >= MIN_MEDIAN and (last["seen"] or 0) < DEGRADED_SHARE * med:
        return "degraded", False
    return "ok", False


def report(con) -> list[dict]:
    """One dict per source: last run, last/prev seen, median over the last WINDOW runs, status."""
    out = []
    names = [r[0] for r in con.execute("SELECT DISTINCT source FROM source_health ORDER BY source")]
    for name in names:
        rows = con.execute("SELECT * FROM source_health WHERE source=? ORDER BY id DESC LIMIT ?",
                           (name, WINDOW)).fetchall()
        last = rows[0] if rows else None
        prev = rows[1] if len(rows) > 1 else None
        med = float(median([r["seen"] or 0 for r in rows])) if rows else 0.0
        status, layout = _status(last, med)
        out.append({"source": name, "last_run_at": last["at"] if last else None,
                    "last_seen": last["seen"] if last else None, "prev_seen": prev["seen"] if prev else None,
                    "median_seen": med, "runs": len(rows), "last_error": last["error"] if last else None,
                    "last_secs": last["secs"] if last else None, "status": status,
                    "layout_change_suspected": layout})
    return out


def alarms(con) -> list[str]:
    """Human sentences for every source that is degraded / erroring / possibly broken by a layout change."""
    out = []
    for r in report(con):
        s = r["source"]
        if r["layout_change_suspected"]:
            out.append(f"{s}: returned 0 listings with no error (median {r['median_seen']:.0f}) — "
                       f"the site layout or API probably changed.")
        elif r["status"] == "error":
            out.append(f"{s}: last run failed — {r['last_error']}")
        elif r["status"] == "degraded":
            out.append(f"{s}: only {r['last_seen']} listings vs a median of {r['median_seen']:.0f} — "
                       f"partial scrape or blocked.")
    return out


def backfill_from_scans(con) -> int:
    """One-time import of the per-source summaries stored in `scans` (no-op if source_health has rows)."""
    if con.execute("SELECT 1 FROM source_health LIMIT 1").fetchone():
        return 0
    n = 0
    for sc in con.execute("SELECT started, finished, summary FROM scans ORDER BY id"):
        try:
            summary = json.loads(sc["summary"] or "{}")
        except json.JSONDecodeError:
            continue
        at = sc["finished"] or sc["started"]
        for name, s in summary.items():
            if not isinstance(s, dict):
                continue
            con.execute("INSERT INTO source_health (source, at, seen, passed, error, secs) VALUES (?,?,?,?,?,?)",
                        (name, at, int(s.get("seen") or 0), int(s.get("pass") or 0), s.get("error"),
                         int(s.get("secs") or 0)))
            n += 1
        con.commit()
    return n


if __name__ == "__main__":
    from .db import connect
    con = connect()
    print("backfilled", backfill_from_scans(con), "rows")
    for r in report(con):
        print(f"{r['source']:20s} {r['status']:9s} last={r['last_seen']!s:>5} prev={r['prev_seen']!s:>5} "
              f"med={r['median_seen']:6.1f} runs={r['runs']:2d} at={r['last_run_at']} err={r['last_error'] or ''}")
    for a in alarms(con):
        print("ALARM:", a)
