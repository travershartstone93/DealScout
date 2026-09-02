"""DealScout dashboard - http://127.0.0.1:5006"""
import json, subprocess, sys, shlex, os, secrets, time, ipaddress
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from flask import Flask, jsonify, render_template, request, send_file, session, redirect, url_for, g, abort
from werkzeug.security import check_password_hash
import tomli_w
from dealscout import load_config, db, ROOT, CONFIG_PATH

app = Flask(__name__)
_secret = ROOT / ".secret"
if not _secret.exists():
    _secret.write_text(secrets.token_hex(32)); os.chmod(_secret, 0o600)
app.secret_key = _secret.read_text().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=30))
if load_config()["server"].get("password_hash"):
    app.config["SESSION_COOKIE_SECURE"] = True
_FAILS: dict[str, list[float]] = {}          # ip -> timestamps of failed logins
_OPEN_PATHS = {"/login", "/logout", "/favicon.ico"}


def _is_local() -> bool:
    """True only for a direct request from this machine (not through the tunnel/proxy)."""
    if request.headers.get("Cf-Connecting-Ip") or request.headers.get("X-Forwarded-For"):
        return False
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _tailscale_user() -> str | None:
    """Identity injected by `tailscale serve` (only trusted when the hop came from tailscaled on loopback)."""
    login = request.headers.get("Tailscale-User-Login")
    if not login:
        return None
    if request.headers.get("Cf-Connecting-Ip"):
        return None                         # cloudflared hop forwards client headers - not from tailscaled
    xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if xff:
        try:
            peer = ipaddress.ip_address(xff)
        except ValueError:
            return None
        if peer not in ipaddress.ip_network("100.64.0.0/10") and peer not in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
            return None                     # tailscale serve reports a tailnet (CGNAT) peer; anything else is another proxy
    try:
        if not ipaddress.ip_address(request.remote_addr or "").is_loopback:
            return None                     # header could be spoofed on a non-loopback path
    except ValueError:
        return None
    return login


@app.before_request
def _auth():
    cfg = load_config()
    g.remote = not (_is_local() and cfg["server"].get("trust_localhost", True))
    g.cfg = cfg
    g.user = None
    if not g.remote or request.path in _OPEN_PATHS or request.path.startswith("/static/"):
        return None
    ts = _tailscale_user()
    if ts and cfg["server"].get("trust_tailscale", True):
        allowed = cfg["server"].get("allowed_tailscale_users") or []
        if not allowed or ts.lower() in [a.lower() for a in allowed]:
            g.user = ts
            session["auth"] = True
            session["user"] = ts
    if session.get("auth"):
        g.user = g.user or session.get("user")
        if request.path in ("/api/ask",):
            abort(403)                       # never let a remote user type into the local shell
        return None
    if request.path.startswith("/api/"):
        return jsonify(error="login required"), 401
    return redirect(url_for("login", next=request.path))


@app.before_request
def _csrf_guard():
    """Block cross-site writes: a web page in the local browser can fetch 127.0.0.1:5006."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        abort(403)
    if request.path in _OPEN_PATHS:
        return None
    port = g.cfg["server"].get("port", 5006)
    if request.host not in (f"127.0.0.1:{port}", f"localhost:{port}") and not session.get("auth"):
        abort(403)


@app.route("/login", methods=["GET", "POST"])
def login():
    behind_cf = g.cfg["server"].get("behind_cloudflare", False)
    ip = (request.headers.get("Cf-Connecting-Ip") if behind_cf else None) or request.remote_addr or "?"
    now = time.time()
    _FAILS[ip] = [t for t in _FAILS.get(ip, []) if now - t < 600]
    err = None
    if request.method == "POST":
        if len(_FAILS[ip]) >= 5:
            return render_template("login.html", error="Too many attempts - wait 10 minutes."), 429
        h = g.cfg["server"].get("password_hash") or ""
        if h and check_password_hash(h, request.form.get("password", "")):
            session.permanent = True
            session["auth"] = True
            nxt = request.args.get("next") or "/"
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        _FAILS[ip].append(now)
        err = "Wrong password." if h else "No shared password set yet - run ./dealscout.sh set-password on the host."
    return render_template("login.html", error=err), (401 if err else 200)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")
VERDICT_ORDER = "CASE j.verdict WHEN 'BUY-CANDIDATE' THEN 0 WHEN 'NEGOTIATE' THEN 1 WHEN 'PASS' THEN 3 ELSE 2 END"


def query(args):
    con = db.connect()
    where, params = ["1=1"], []
    mode = args.get("mode", "pass")
    if mode == "pass":
        where.append("l.passes=1 AND l.status='open'")
    elif mode == "starred":
        where.append("l.starred=1")
    elif mode == "open":
        where.append("l.status='open'")
    if args.get("hidden") != "1" and mode != "starred":
        where.append("l.hidden=0")
    if args.get("source"):
        where.append("l.source=?"); params.append(args["source"])
    if args.get("category"):
        where.append("l.category=?"); params.append(args["category"])
    if args.get("verdict") == "none":
        where.append("j.verdict IS NULL")
    elif args.get("verdict"):
        where.append("j.verdict=?"); params.append(args["verdict"])
    if args.get("max_price"):
        where.append("l.asking_price<=?"); params.append(float(args["max_price"]))
    if args.get("max_payback"):
        where.append("l.payback_months<=?"); params.append(float(args["max_payback"]))
    if args.get("customers_known") == "1":
        where.append("l.customers IS NOT NULL")
    if args.get("no_ads") == "1":
        where.append("l.flags NOT LIKE '%traffic_ads_dependent%'")
    if args.get("software") == "1":
        where.append("l.category IN ('saas','chrome_extension','mobile_app','newsletter')")
    if args.get("hide_ending") == "1":
        where.append("(l.ends_at IS NULL OR l.ends_at > ? OR l.starred=1)")
        params.append((datetime.now(timezone.utc) + timedelta(hours=24)).isoformat())
    if args.get("q"):
        where.append("(l.title LIKE ? OR l.summary LIKE ?)"); params += [f"%{args['q']}%"] * 2
    sort = {"score": "l.score DESC", "payback": "l.payback_months ASC", "price": "l.asking_price ASC",
            "profit": "l.monthly_profit DESC", "customers": "l.customers DESC", "verdict": VERDICT_ORDER + ", l.score DESC",
            "seen": "l.first_seen DESC"}.get(args.get("sort", "verdict"), VERDICT_ORDER + ", l.score DESC")
    rows = con.execute(
        f"SELECT l.*, j.verdict, j.verdict1, j.json AS jjson, j.json2 AS jjson2, j.model2, j.judged_at "
        f"FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id "
        f"WHERE {' AND '.join(where)} ORDER BY {sort} LIMIT 500", params).fetchall()
    keys = [r["dupe_key"] for r in rows if r["dupe_key"]]
    dupes = {}
    if keys:
        q = ",".join("?" * len(keys))
        for d in con.execute(f"SELECT id, dupe_key, source, url FROM listings WHERE dupe_key IN ({q}) AND status='open'", keys):
            dupes.setdefault(d["dupe_key"], []).append({"id": d["id"], "source": d["source"], "url": d["url"]})
    fb = {r["listing_id"]: dict(r) for r in con.execute(
        "SELECT listing_id, agree, comment FROM feedback WHERE id IN (SELECT max(id) FROM feedback GROUP BY listing_id)")}
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(hours=24)).isoformat()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d.get("flags") or "[]")
        d["fail_reasons"] = json.loads(d.get("fail_reasons") or "[]")
        d["judgment"] = json.loads(d.pop("jjson") or "null")
        d["second"] = json.loads(d.pop("jjson2") or "null")
        d.pop("raw_json", None)
        d["also_on"] = [x for x in dupes.get(d["dupe_key"], []) if x["id"] != d["id"]]
        d["feedback"] = fb.get(d["id"])
        d["ends_soon"] = bool(d["ends_at"] and d["ends_at"] < soon)
        try:
            d["days_on_market"] = (now - datetime.fromisoformat(d["first_seen"])).days if d.get("first_seen") else None
        except ValueError:
            d["days_on_market"] = None
        out.append(d)
    return out


def _read_status(path):
    try:
        return json.loads((ROOT / path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@app.route("/api/status")
def api_status():
    con = db.connect()
    cfg = load_config()
    since = int(request.args.get("since_event", 0))
    ev = [dict(r) for r in con.execute("SELECT * FROM ui_events WHERE id>? ORDER BY id LIMIT 20", (since,))]
    for e in ev:
        e["payload"] = json.loads(e["payload"])
    last_ev = con.execute("SELECT max(id) FROM ui_events").fetchone()[0] or 0
    scan_running = bool(subprocess.run(["pgrep", "-f", "dealscout.scan scan"], capture_output=True).stdout.strip())
    scan = _read_status("scan.status.json")
    scan["running"] = scan_running and scan.get("running", False)
    judge = _read_status("judge.status.json")
    judge_running = bool(subprocess.run(["pgrep", "-f", "dealscout.scan judge"], capture_output=True).stdout.strip()) or scan_running
    if not judge_running:
        judge["running"] = False
    dil = _read_status("diligence.status.json")
    return jsonify(scan=scan, judge=judge, diligence=dil, remote=g.remote, events=ev, last_event=last_ev,
                   judged_today=db.judged_today(con), max_per_day=cfg["judge"].get("max_per_day"),
                   passing=con.execute("SELECT count(*) FROM listings WHERE passes=1 AND status='open' AND hidden=0").fetchone()[0],
                   judged=con.execute("SELECT count(*) FROM judgments").fetchone()[0])


@app.route("/api/ask", methods=["POST"])
def api_ask():
    """Type a question into the Claude terminal (tmux session 'dealscout')."""
    text = (request.get_json(force=True).get("text") or "").strip().replace("\n", " ")
    if not text:
        return jsonify(ok=False, error="empty")
    r = subprocess.run(["tmux", "send-keys", "-t", "dealscout", "-l", text], capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify(ok=False, error="terminal session not running - open the terminal pane first")
    subprocess.run(["tmux", "send-keys", "-t", "dealscout", "Enter"], capture_output=True)
    return jsonify(ok=True)


@app.route("/api/listing/<path:lid>/feedback", methods=["POST"])
def api_feedback(lid):
    b = request.get_json(force=True)
    db.add_feedback(db.connect(), lid, int(b.get("agree", 1)), b.get("comment", ""))
    return jsonify(ok=True)


EDITABLE = {"filters": ["max_price", "max_payback_months", "min_customers", "min_free_users", "revenue_to_profit_ratio", "exclude_categories"],
            "score": ["payback", "margin", "customers", "age", "verified", "recurring", "hours", "software_bonus",
                      "paying_customers_bonus", "ads_penalty"],
            "judge": ["enabled", "model", "max_per_scan", "max_per_day", "second_pass", "second_model", "second_max_per_scan"]}


def _toml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) for x in v) + "]"
    return json.dumps(v)


def _write_config_preserving_comments(cfg):
    """Rewrite only `key = value` lines in place so the comments in config.toml survive."""
    import re
    lines = CONFIG_PATH.read_text().splitlines()
    sec = None
    drop = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            sec = m.group(1); continue
        m = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
        if m and sec in cfg and m.group(1) in cfg[sec]:
            k = m.group(1)
            # multi-line array: swallow continuation lines until brackets balance
            depth = line.count("[") - line.count("]")
            j = i
            while depth > 0 and j + 1 < len(lines):
                j += 1
                drop.add(j)
                depth += lines[j].count("[") - lines[j].count("]")
            comment = ""
            if "#" in line and not line.strip().startswith("#"):
                # keep an inline comment only for scalar lines (arrays with # inside strings are rare here)
                idx = line.find("#")
                if line[:idx].count('"') % 2 == 0:
                    comment = "   " + line[idx:].rstrip()
            lines[i] = f"{k} = {_toml_scalar(cfg[sec][k])}{comment}"
    text = "\n".join(l for n, l in enumerate(lines) if n not in drop) + "\n"
    # sanity: must still parse and contain every key; else fall back to a full dump
    import tomllib
    try:
        chk = tomllib.loads(text)
        ok = all(k in chk.get(sec, {}) for sec, d in cfg.items() if isinstance(d, dict) for k in d)
    except tomllib.TOMLDecodeError:
        ok = False
    CONFIG_PATH.write_text(text if ok else tomli_w.dumps(cfg))


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    cfg = load_config()
    if request.method == "POST":
        body = request.get_json(force=True)
        for sec, keys in EDITABLE.items():
            for k in keys:
                if sec in body and k in body[sec]:
                    v = body[sec][k]
                    cur = cfg[sec].get(k)
                    if isinstance(cur, bool):
                        v = bool(v)
                    elif isinstance(cur, int) and not isinstance(cur, bool):
                        v = int(float(v))
                    elif isinstance(cur, float):
                        v = float(v)
                    elif isinstance(cur, list):
                        v = [x.strip() for x in (v if isinstance(v, list) else str(v).split(",")) if x.strip()]
                    cfg[sec][k] = v
        if "sources" in body:
            for k, v in body["sources"].items():
                if k in cfg["sources"]:
                    cfg["sources"][k] = bool(v)
        _write_config_preserving_comments(cfg)
    return jsonify({sec: {k: cfg[sec].get(k) for k in keys} for sec, keys in EDITABLE.items()} | {"sources": cfg["sources"]})


@app.route("/api/rescore", methods=["POST"])
def api_rescore():
    p = subprocess.run([sys.executable, "-m", "dealscout.scan", "rescore"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    return jsonify(ok=p.returncode == 0, log=(p.stdout + p.stderr)[-600:])


@app.route("/api/export/starred")
def api_export_starred():
    con = db.connect()
    rows = con.execute("SELECT l.*, j.verdict, j.json AS jjson, j.json2 FROM listings l LEFT JOIN judgments j ON j.listing_id=l.id "
                       "WHERE l.starred=1 ORDER BY l.score DESC").fetchall()
    out = ["# DealScout - diligence checklist for starred listings", f"_generated {db.now()[:16]}_", ""]
    for r in rows:
        j = json.loads(r["jjson"] or "{}"); j2 = json.loads(r["json2"] or "{}")
        out += [f"## {r['title']}", f"{r['url']}", "",
                f"- Asking ${r['asking_price'] or 0:,.0f} · profit ${r['monthly_profit'] or 0:,.0f}/mo · payback {r['payback_months']} mo "
                f"(75%: {r['payback_75']}, 65%: {r['payback_65']}) · customers {r['customers']} · age {r['age_months']} mo",
                f"- Verdict: **{r['verdict']}** - {j.get('rationale','')}"]
        if j2:
            out.append(f"- Skeptic: **{j2.get('verdict')}** - {j2.get('strongest_objection','')}")
        out += ["- Suggested offer: " + (f"${j.get('suggested_offer'):,.0f}" if j.get("suggested_offer") else "-"),
                f"- My notes: {r['note'] or ''}", "", "**Checklist**",
                "- [ ] Verify revenue (Stripe/PayPal/ad-network screenshots or Flippa verification, 12 months)",
                "- [ ] Verify traffic / installs / active users (GA / Search Console / store dashboard)",
                "- [ ] Confirm paying-customer count and churn (last 6 months)",
                "- [ ] Ask the seller's real reason for selling; check story vs. numbers",
                "- [ ] Hours/week actually required; support-email volume; any servers/API bills",
                "- [ ] Platform risk: store policies, account transferability, single supplier/API",
                "- [ ] Assets included: code, domain, accounts, customer list, brand; transfer plan",
                "- [ ] Legal: trademarks, licenses, GDPR/data, refunds owed",
                "- [ ] Escrow + written APA; 30-day support clause",
                *[f"- [ ] Red flag to clear: {x}" for x in (j.get("red_flags") or [])], ""]
    path = ROOT / "reports" / "diligence.md"
    path.write_text("\n".join(out))
    return send_file(path, mimetype="text/markdown", as_attachment=request.args.get("dl") == "1", download_name="diligence.md")


@app.route("/")
def index():
    con = db.connect()
    cfg = load_config()
    stats = {
        "total": con.execute("SELECT count(*) FROM listings").fetchone()[0],
        "open": con.execute("SELECT count(*) FROM listings WHERE status='open'").fetchone()[0],
        "passing": con.execute("SELECT count(*) FROM listings WHERE passes=1 AND status='open' AND hidden=0").fetchone()[0],
        "judged": con.execute("SELECT count(*) FROM judgments").fetchone()[0],
        "last_scan": (con.execute("SELECT finished, summary FROM scans ORDER BY id DESC LIMIT 1").fetchone() or [None, "{}"]),
    }
    sources = [r[0] for r in con.execute("SELECT DISTINCT source FROM listings ORDER BY 1")]
    cats = [r[0] for r in con.execute("SELECT DISTINCT category FROM listings ORDER BY 1")]
    return render_template("index.html", stats=stats, sources=sources, cats=cats, cfg=cfg, remote=g.remote,
                           public_url=cfg["server"].get("public_url", ""), user=g.user)


@app.route("/api/listings")
def api_listings():
    return jsonify(query(request.args))


@app.route("/api/new")
def api_new():
    """In-app notifications: BUY/NEGOTIATE verdicts judged since `since` (ISO)."""
    since = request.args.get("since", "")
    con = db.connect()
    rows = con.execute(
        "SELECT l.id, l.title, l.asking_price, l.payback_months, j.verdict, j.judged_at FROM judgments j "
        "JOIN listings l ON l.id=j.listing_id WHERE j.judged_at>? AND j.verdict IN ('BUY-CANDIDATE','NEGOTIATE') "
        "AND l.hidden=0 ORDER BY j.judged_at", (since,)).fetchall()
    return jsonify(items=[dict(r) for r in rows], now=db.now())


def _row(lid):
    con = db.connect()
    r = con.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    return con, r


@app.route("/api/listing/<path:lid>/workspace")
def api_workspace(lid):
    from dealscout.app_bundle import bundle_for
    con, r = _row(lid)
    if not r:
        return jsonify(error="not found"), 404
    b = bundle_for(con, r, load_config())
    return jsonify(b)


@app.route("/api/listing/<path:lid>/signals", methods=["POST"])
def api_signals(lid):
    p = subprocess.run([sys.executable, "-m", "dealscout.scan", "signals", lid], cwd=ROOT, capture_output=True, text=True, timeout=90)
    return jsonify(ok=p.returncode == 0, log=(p.stdout + p.stderr)[-1500:])


@app.route("/api/listing/<path:lid>/diligence", methods=["POST"])
def api_diligence(lid):
    """Async: returns immediately; the card polls /api/status (diligence.running) and reloads its workspace."""
    (ROOT / "diligence.status.json").write_text(json.dumps({"running": True, "id": lid}))
    subprocess.Popen([sys.executable, "-m", "dealscout.scan", "diligence", lid], cwd=ROOT,
                     stdout=open(ROOT / "diligence.log", "a"), stderr=subprocess.STDOUT)
    return jsonify(ok=True, async_=True)


@app.route("/api/listing/<path:lid>/evidence", methods=["POST"])
def api_evidence(lid):
    from dealscout.diligence import add_evidence
    b = request.get_json(force=True)
    add_evidence(db.connect(), lid, b.get("kind", "note"), b.get("title", "")[:200], b.get("body", ""), b.get("url", ""))
    return jsonify(ok=True)


@app.route("/api/listing/<path:lid>/check", methods=["POST"])
def api_check(lid):
    from dealscout.diligence import set_check
    b = request.get_json(force=True)
    set_check(db.connect(), lid, b["key"], b.get("state", "todo"), b.get("note", ""))
    return jsonify(ok=True)


@app.route("/api/listing/<path:lid>/chat", methods=["POST"])
def api_chat(lid):
    from dealscout.app_bundle import bundle_for
    from dealscout.diligence import card_chat
    from dealscout.scan import row_to_listing
    con, r = _row(lid)
    q = (request.get_json(force=True).get("text") or "").strip()
    if not r or not q:
        return jsonify(ok=False, error="missing")
    cfg = load_config()
    try:
        ans = card_chat(con, row_to_listing(r), q, bundle_for(con, r, cfg), cfg)
        return jsonify(ok=True, answer=ans)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:400])


@app.route("/api/chat", methods=["POST"])
def api_general_chat():
    from dealscout.diligence import general_chat
    q = (request.get_json(force=True).get("text") or "").strip()
    if not q:
        return jsonify(ok=False, error="empty")
    try:
        return jsonify(ok=True, answer=general_chat(db.connect(), q, load_config()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300])


@app.route("/api/chat/history")
def api_general_chat_history():
    con = db.connect()
    rows = con.execute("SELECT role, text, at FROM chats WHERE listing_id IS NULL ORDER BY id DESC LIMIT 30").fetchall()[::-1]
    return jsonify([dict(r) for r in rows])


@app.route("/api/health")
def api_health():
    try:
        from dealscout.health import report, alarms
        con = db.connect()
        return jsonify(sources=report(con), alarms=alarms(con))
    except ImportError:
        return jsonify(sources=[], alarms=["health module not installed yet"])


@app.route("/health")
def health_page():
    return render_template("health.html")


@app.route("/api/comps")
def api_comps():
    try:
        from dealscout.comps import category_benchmarks
        return jsonify(category_benchmarks(db.connect()))
    except ImportError:
        return jsonify({})


@app.route("/api/listing/<path:lid>")
def api_listing(lid):
    con = db.connect()
    r = con.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
    j = db.get_judgment(con, lid)
    return jsonify({"listing": dict(r) if r else None, "judgment": dict(j) if j else None})


@app.route("/api/listing/<path:lid>/flag", methods=["POST"])
def api_flag(lid):
    body = request.get_json(force=True)
    con = db.connect()
    for k in ("starred", "hidden", "note", "watch"):
        if k in body:
            con.execute(f"UPDATE listings SET {k}=? WHERE id=?", (body[k], lid))
    con.commit()
    return jsonify(ok=True)


@app.route("/api/listing/<path:lid>/judge", methods=["POST"])
def api_judge(lid):
    """Async (Cloudflare cuts requests at 100 s): the card shows a spinner via judge.status.json and reloads."""
    from dealscout.judge import set_status
    set_status(running=True, phase="first", done=0, total=1, current=lid, ids=[lid])
    subprocess.Popen([sys.executable, "-m", "dealscout.scan", "judge", "--force", "--id", lid], cwd=ROOT,
                     stdout=open(ROOT / "judge.log", "a"), stderr=subprocess.STDOUT)
    return jsonify(ok=True, async_=True)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    subprocess.Popen([sys.executable, "-m", "dealscout.scan", "scan"], cwd=ROOT,
                     stdout=open(ROOT / "scan.log", "a"), stderr=subprocess.STDOUT)
    return jsonify(ok=True, msg="scan started in background; see scan.log")


@app.route("/api/scanlog")
def api_scanlog():
    p = ROOT / "scan.log"
    return (p.read_text()[-6000:] if p.exists() else ""), 200, {"Content-Type": "text/plain"}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=load_config()["server"]["port"], debug=False)
