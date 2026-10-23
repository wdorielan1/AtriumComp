"""Password-protected web dashboard, served from a Lambda Function URL.

Routes: GET /            dashboard (or login form)
        POST /login      password check -> session cookie
        GET /api/data    jobs + run stats as JSON
        POST /api/decode decode one posting on demand ({"job_id": ..., "force": true to re-decode})
        POST /api/feedback mark a decode right/wrong ({"job_id": ..., "verdict": "right"|"wrong"|""})
        POST /api/watchlist         add/update a prospect or current client
        POST /api/watchlist/import  add many at once ({"items": [{"name", "careers_url", "status"}], "default_status"})
        POST /api/watchlist/delete  remove one ({"name": ...})
        POST /api/competitors          add / edit / pause one ({"name", "homepage", "careers_url", "enabled"})
        POST /api/competitors/import   add many ({"items": [{"name", "homepage"}]}); paused ones are switched on
        POST /api/competitors/delete   remove one that was added on the dashboard ({"name": ...})
        GET /export.csv  matched jobs as CSV
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from . import classify, config, decode, watch
from .config import env
from .models import Job
from .store import get_store

COOKIE = "jobwatch_session"


def _password() -> str:
    if not os.environ.get("DASHBOARD_PASSWORD") and env("DASHBOARD_PASSWORD_PARAM"):
        import boto3
        p = boto3.client("ssm").get_parameter(Name=env("DASHBOARD_PASSWORD_PARAM"), WithDecryption=True)
        os.environ["DASHBOARD_PASSWORD"] = p["Parameter"]["Value"]
    return os.environ.get("DASHBOARD_PASSWORD", "")


def _token() -> str:
    return hmac.new(_password().encode(), b"jobwatch-session-v1", hashlib.sha256).hexdigest()


def _authed(event: dict) -> bool:
    if not _password():
        return False  # refuse to serve without a password configured
    for c in event.get("cookies") or []:
        name, _, value = c.partition("=")
        if name.strip() == COOKIE and hmac.compare_digest(value.strip(), _token()):
            return True
    return False


def _resp(status: int, body: str, ctype: str = "text/html; charset=utf-8", **extra) -> dict:
    headers = {"Content-Type": ctype, "Cache-Control": "no-store", "X-Frame-Options": "DENY",
               "Referrer-Policy": "no-referrer", **extra.pop("headers", {})}
    return {"statusCode": status, "headers": headers, "body": body, **extra}


def _prepare(j: dict, watchlist: list[dict]) -> dict:
    j["auto_decode"] = classify.auto_decode(Job.from_dict(j))
    j["watch"] = watch.link(j, watchlist)
    j.pop("description", None)  # keep the payload small; full text stays in the table
    return j


def _competitor_rows(store) -> list[dict]:
    rows = [{"name": c.name, "homepage": c.homepage, "careers_url": c.careers_url, "enabled": c.enabled, "source": src}
            for c, src in config.merged_competitors(store.competitors())]
    order = {"built-in": 0, "added": 0, "suggested": 1}
    return sorted(rows, key=lambda r: (not r["enabled"], order[r["source"]], r["name"].lower()))


def _load(days: int | None = None) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Matched jobs first seen in the last `days` days (keeps the response under Lambda's 6 MB cap),
    run stats, the watchlist, and the competitor list."""
    days = int(env("DASHBOARD_DAYS", "120")) if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    store = get_store()
    wl = sorted(store.watchlist(), key=lambda w: w["name"].lower())
    jobs = [_prepare(j, wl) for j in store.matched_jobs() if j.get("first_seen", "") >= cutoff]
    return jobs, store.runs(), wl, _competitor_rows(store)


def _json_body(event: dict) -> tuple[dict | None, dict | None]:
    """(payload, None) or (None, error response). Requiring JSON blocks form-based CSRF."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if not headers.get("content-type", "").startswith("application/json"):
        return None, _resp(415, '{"error":"expected application/json"}', "application/json")
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return None, _resp(400, '{"error":"invalid JSON"}', "application/json")
    return data, None


def _save_watch(event: dict) -> dict:
    data, err = _json_body(event)
    if err:
        return err
    name = str(data.get("name", "")).strip()
    status = data.get("status", watch.PROSPECT)
    url = str(data.get("careers_url", "")).strip()
    aliases = data.get("aliases", [])
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    aliases = [str(a).strip() for a in aliases if str(a).strip()][:10]
    if not name or len(name) > 100:
        return _resp(400, '{"error":"Company name is required (max 100 characters)."}', "application/json")
    if status not in (watch.PROSPECT, watch.CLIENT):
        return _resp(400, '{"error":"status must be prospect or client"}', "application/json")
    if url and not url.startswith(("http://", "https://")):
        return _resp(400, '{"error":"Careers page must be a full URL starting with https://"}', "application/json")
    store = get_store()
    existing = {w["name"].lower(): w for w in store.watchlist()}.get(name.lower(), {})
    if existing.get("name") and existing["name"] != name:
        store.delete_watch(existing["name"])  # renamed (case change)
    today = datetime.now(timezone.utc).date().isoformat()
    store.save_watch({"name": name, "status": status, "careers_url": url, "aliases": aliases,
                      "notes": str(data.get("notes", ""))[:500], "added": existing.get("added", today)})
    return _resp(200, json.dumps({"watchlist": sorted(store.watchlist(), key=lambda w: w["name"].lower())}),
                 "application/json")


def _import_watch(event: dict) -> dict:
    """Add many prospects/clients at once. Names already tracked are left alone (a blank careers URL is filled in)."""
    data, err = _json_body(event)
    if err:
        return err
    items, default = data.get("items"), data.get("default_status", watch.PROSPECT)
    if not isinstance(items, list) or not items or len(items) > 500:
        return _resp(400, '{"error":"Provide between 1 and 500 companies."}', "application/json")
    if default not in (watch.PROSPECT, watch.CLIENT):
        return _resp(400, '{"error":"default_status must be prospect or client"}', "application/json")
    store = get_store()
    existing = {w["name"].lower(): w for w in store.watchlist()}
    today = datetime.now(timezone.utc).date().isoformat()
    added = updated = 0
    skipped: list[dict] = []
    for raw in items:
        raw = raw if isinstance(raw, dict) else {}
        name = str(raw.get("name", "")).strip()
        url = str(raw.get("careers_url", "")).strip()
        status = raw.get("status") or default
        if not name or len(name) > 100:
            skipped.append({"name": name[:40], "reason": "missing or too-long name"})
        elif status not in (watch.PROSPECT, watch.CLIENT):
            skipped.append({"name": name, "reason": f"unknown status '{status}'"})
        elif url and not url.startswith(("http://", "https://")):
            skipped.append({"name": name, "reason": "careers page must start with https://"})
        elif name.lower() in existing:
            cur = existing[name.lower()]
            if url and not cur.get("careers_url"):
                cur = {**cur, "careers_url": url}
                store.save_watch(cur)
                existing[name.lower()] = cur
                updated += 1
            else:
                skipped.append({"name": name, "reason": "already tracked"})
        else:
            item = {"name": name, "status": status, "careers_url": url, "aliases": [], "notes": "", "added": today}
            store.save_watch(item)
            existing[name.lower()] = item
            added += 1
    return _resp(200, json.dumps({"added": added, "updated": updated, "skipped": skipped,
                                  "watchlist": sorted(store.watchlist(), key=lambda w: w["name"].lower())}),
                 "application/json")


def _norm_url(u: str) -> str:
    """"kennedybond.com" -> "https://kennedybond.com"; "" stays "". Returns None if it isn't a usable address."""
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    host = u.split("//", 1)[1].split("/", 1)[0]
    return u if "." in host and " " not in u and len(u) < 500 else None


def _competitor_reply(store, **extra) -> dict:
    return _resp(200, json.dumps({"competitors": _competitor_rows(store), **extra}), "application/json")


def _save_competitor(event: dict) -> dict:
    data, err = _json_body(event)
    if err:
        return err
    name = str(data.get("name", "")).strip()
    home, jobs_url = _norm_url(data.get("homepage")), _norm_url(data.get("careers_url"))
    if not name or len(name) > 100:
        return _resp(400, '{"error":"Competitor name is required (max 100 characters)."}', "application/json")
    if home is None or jobs_url is None:
        return _resp(400, '{"error":"That web address doesn\'t look right. Example: https://www.example.com"}',
                     "application/json")
    store = get_store()
    current = {c.name.lower(): c for c, _ in config.merged_competitors(store.competitors())}.get(name.lower())
    if not (home or jobs_url or (current and (current.homepage or current.careers_url))):
        return _resp(400, '{"error":"Add the competitor\'s website or its jobs page."}', "application/json")
    store.save_competitor({"name": current.name if current else name, "homepage": home, "careers_url": jobs_url,
                           "enabled": bool(data.get("enabled", True))})
    return _competitor_reply(store)


def _import_competitors(event: dict) -> dict:
    """Add many competitors at once. Ones already listed are skipped, except paused/suggested ones, which are switched on."""
    data, err = _json_body(event)
    if err:
        return err
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 500:
        return _resp(400, '{"error":"Provide between 1 and 500 competitors."}', "application/json")
    store = get_store()
    known = {c.name.lower(): c for c, _ in config.merged_competitors(store.competitors())}
    added = resumed = 0
    skipped: list[dict] = []
    for raw in items:
        raw = raw if isinstance(raw, dict) else {}
        name = str(raw.get("name", "")).strip()
        home, jobs_url = _norm_url(raw.get("homepage")), _norm_url(raw.get("careers_url"))
        cur = known.get(name.lower())
        if not name or len(name) > 100:
            skipped.append({"name": name[:40], "reason": "missing or too-long name"})
        elif home is None or jobs_url is None:
            skipped.append({"name": name, "reason": "web address doesn't look right"})
        elif cur and cur.enabled:
            skipped.append({"name": name, "reason": "already tracked"})
        elif cur:  # paused or suggested: switch it on (and take a better address if one was given)
            store.save_competitor({"name": cur.name, "homepage": home, "careers_url": jobs_url, "enabled": True})
            resumed += 1
        elif not (home or jobs_url):
            skipped.append({"name": name, "reason": "no website given"})
        else:
            store.save_competitor({"name": name, "homepage": home, "careers_url": jobs_url, "enabled": True})
            known[name.lower()] = config.Competitor(name=name, homepage=home, careers_url=jobs_url)
            added += 1
    return _competitor_reply(store, added=added, resumed=resumed, skipped=skipped)


def _delete_competitor(event: dict) -> dict:
    data, err = _json_body(event)
    if err:
        return err
    name = str(data.get("name", "")).strip()
    store = get_store()
    source = {c.name.lower(): s for c, s in config.merged_competitors(store.competitors())}.get(name.lower())
    if source is None:
        return _resp(404, '{"error":"competitor not found"}', "application/json")
    if source != "added":
        return _resp(400, '{"error":"Built-in competitors can be paused, not removed."}', "application/json")
    store.delete_competitor(name)
    return _competitor_reply(store)


def _delete_watch(event: dict) -> dict:
    data, err = _json_body(event)
    if err:
        return err
    store = get_store()
    store.delete_watch(str(data.get("name", "")))
    return _resp(200, json.dumps({"watchlist": sorted(store.watchlist(), key=lambda w: w["name"].lower())}),
                 "application/json")


def _decode_now(event: dict) -> dict:
    """Decode one posting on demand. Runs synchronously (Function URLs allow long requests)."""
    body, err = _json_body(event)
    if err:
        return err
    store, job = _load_job(body)
    if job is None:
        return _resp(404, '{"error":"job not found"}', "application/json")
    # Without force, an existing decode (e.g. from a double click) is returned without paying again.
    if not job.decode or body.get("force"):
        if not decode.enabled():
            return _resp(503, '{"error":"Decoding is not configured (no Anthropic API key)."}', "application/json")
        if not decode.decode_and_apply(job):
            return _resp(502, '{"error":"Decoding failed. Try again in a minute."}', "application/json")
        store.save(job, matched=True)
    return _resp(200, json.dumps(_prepare(job.to_dict(), store.watchlist())), "application/json")


def _load_job(body: dict):
    store = get_store()
    data = store.get_job(str(body.get("job_id", "")))
    return store, (Job.from_dict(data) if data and data.get("matched") else None)


def _feedback(event: dict) -> dict:
    """Mark a decode right or wrong, to measure accuracy per model."""
    body, err = _json_body(event)
    if err:
        return err
    verdict = body.get("verdict", "")
    if verdict not in ("right", "wrong", ""):
        return _resp(400, '{"error":"verdict must be right, wrong or empty"}', "application/json")
    store, job = _load_job(body)
    if job is None or not job.decode:
        return _resp(404, '{"error":"no decode to rate"}', "application/json")
    job.decode["feedback"] = verdict
    store.save(job, matched=True)
    return _resp(200, json.dumps(_prepare(job.to_dict(), store.watchlist())), "application/json")


def _csv(jobs: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_seen", "last_seen", "competitor", "title", "location", "employment_type", "categories",
                "salary", "likely_client", "confidence", "client_type", "therapeutic_area", "evidence",
                "your_prospect_or_client", "posted_by", "url"])
    for j in sorted(jobs, key=lambda j: j.get("first_seen", ""), reverse=True):
        d = j.get("decode") or {}
        w.writerow([j.get("first_seen", ""), j.get("last_seen", ""), j.get("competitor", ""), j.get("title", ""),
                    j.get("location", ""), j.get("employment_type", ""), "; ".join(j.get("categories", [])),
                    j.get("salary", ""), d.get("client_guess", ""), d.get("confidence", ""),
                    d.get("client_type", ""), d.get("therapeutic_area", ""), " | ".join(d.get("evidence", [])),
                    "; ".join(j.get("watch", [])), "company site" if j.get("direct") else "competitor",
                    j.get("url", "")])
    return buf.getvalue()


def handler(event: dict, context=None) -> dict:
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")

    if path == "/login" and method == "POST":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        pw = parse_qs(body).get("password", [""])[0]
        if _password() and hmac.compare_digest(pw, _password()):
            return _resp(302, "", headers={"Location": "/"},
                         cookies=[f"{COOKIE}={_token()}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=2592000"])
        return _resp(401, LOGIN_HTML.replace("<!--ERR-->", "<p class=err>Wrong password.</p>"))

    if not _authed(event):
        if path.startswith("/api") or path.endswith(".csv"):
            return _resp(401, '{"error":"unauthorized"}', "application/json")
        msg = "" if _password() else "<p class=err>DASHBOARD_PASSWORD is not configured.</p>"
        return _resp(200, LOGIN_HTML.replace("<!--ERR-->", msg))

    if path == "/api/decode" and method == "POST":
        return _decode_now(event)
    if path == "/api/feedback" and method == "POST":
        return _feedback(event)
    if path == "/api/watchlist" and method == "POST":
        return _save_watch(event)
    if path == "/api/competitors" and method == "POST":
        return _save_competitor(event)
    if path == "/api/competitors/import" and method == "POST":
        return _import_competitors(event)
    if path == "/api/competitors/delete" and method == "POST":
        return _delete_competitor(event)
    if path == "/api/watchlist/import" and method == "POST":
        return _import_watch(event)
    if path == "/api/watchlist/delete" and method == "POST":
        return _delete_watch(event)
    if path == "/api/data":
        jobs, runs, wl, comps = _load()
        return _resp(200, json.dumps({"jobs": jobs, "runs": runs, "watchlist": wl, "competitors": comps}),
                     "application/json")
    if path == "/export.csv":
        jobs, _, _, _ = _load(days=0)
        return _resp(200, _csv(jobs), "text/csv; charset=utf-8",
                     headers={"Content-Disposition": 'attachment; filename="competitor-jobs.csv"'})
    if path == "/logout":
        return _resp(302, "", headers={"Location": "/"}, cookies=[f"{COOKIE}=; Path=/; Max-Age=0"])
    return _resp(200, DASHBOARD_HTML)


_BASE_CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--text:#1f2328;--muted:#59636e;--border:#d1d9e0;--accent:#0b6bcb;
--chip:#eef2f6;--hi:#1a7f37;--med:#9a6700;--low:#6e7781;--direct:#0b6bcb;--temp:#8250df;--c2h:#bf3989}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--panel:#171b21;--text:#e6e9ee;--muted:#9aa4b0;
--border:#2c333c;--accent:#4c9bf0;--chip:#222831;--hi:#3fb950;--med:#d29922;--low:#8b949e;--direct:#4c9bf0;
--temp:#a371f7;--c2h:#db61a2}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
input,select,button{font:inherit;color:var(--text);background:var(--panel);border:1px solid var(--border);
border-radius:6px;padding:6px 8px}button{cursor:pointer}.err{color:#cf222e}
"""

LOGIN_HTML = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Competitor Job Watch</title>
<style>{_BASE_CSS}.box{{max-width:340px;margin:15vh auto;padding:24px;background:var(--panel);
border:1px solid var(--border);border-radius:10px}}input{{width:100%;margin:10px 0}}button{{width:100%;
background:var(--accent);color:#fff;border:0}}</style></head><body><form class=box method=post action=/login>
<h2 style="margin-top:0">Competitor Job Watch</h2><!--ERR--><input type=password name=password
placeholder=Password autofocus required><button>Sign in</button></form></body></html>"""

DASHBOARD_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Competitor Job Watch</title>
<style>""" + _BASE_CSS + """
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
padding:14px 16px;background:var(--panel);border-bottom:1px solid var(--border)}
h1{font-size:17px;margin:0}.tabs{display:flex;gap:4px}.tabs button{border:0;background:none;
padding:6px 12px;border-radius:6px;color:var(--muted)}.tabs button.on{background:var(--chip);color:var(--text);
font-weight:600}main{padding:16px;max-width:1400px;margin:0 auto}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:12px}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}.filters input[type=search]{flex:1;min-width:200px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow-x:auto}
table{width:100%;border-collapse:collapse}th,td{padding:8px 10px;text-align:left;vertical-align:top;
border-bottom:1px solid var(--border)}th{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap;
cursor:pointer;user-select:none}tr:last-child td{border-bottom:0}
.chip{display:inline-block;padding:1px 7px;border-radius:10px;background:var(--chip);font-size:12px;margin:1px 2px 1px 0;
white-space:nowrap}.t-direct{color:var(--direct)}.t-temp{color:var(--temp)}.t-c2h{color:var(--c2h)}
.conf-high{color:var(--hi)}.conf-medium{color:var(--med)}.conf-low,.conf-none{color:var(--low)}
.muted{color:var(--muted)}.small{font-size:12px}details summary{cursor:pointer}.closed{opacity:.55}
button.decode{padding:3px 10px;font-size:12px;border-color:var(--accent);color:var(--accent)}
button.decode:hover{background:var(--accent);color:#fff}
.imp-box{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-top:6px}
#imp-text{width:100%;font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px}
.nowrap{white-space:nowrap}.rate{margin-top:4px;color:var(--muted)}
button.fb{padding:0 7px;font-size:12px;line-height:18px;margin-left:2px}
button.fb.on-right{background:var(--hi);color:#fff;border-color:var(--hi)}button.fb.on-wrong{background:#cf222e;color:#fff;border-color:#cf222e}.w-client{background:color-mix(in srgb,var(--hi) 18%,transparent);color:var(--hi)}
.w-prospect{background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.seg{display:flex;gap:4px;margin-bottom:10px}.seg button{border:0;background:none;color:var(--muted);padding:4px 10px;border-radius:6px}
.seg button.on{background:var(--chip);color:var(--text);font-weight:600}
table.inner{margin-top:8px}table.inner td{padding:6px 8px 6px 0;border-bottom:1px solid var(--border)}
.bar{height:6px;border-radius:3px;background:var(--accent);display:inline-block;vertical-align:middle}
@media (max-width:700px){.hide-sm{display:none}main{padding:12px}
table,tbody,tr,td{display:block;width:100%}thead{display:none}tr{padding:10px 0;border-bottom:1px solid var(--border)}
tr:last-child{border-bottom:0}td{border:0;padding:2px 12px}td:empty{display:none}
td[data-l]::before{content:attr(data-l) ": ";color:var(--muted)}}
.stat b.sm{font-size:15px;padding-top:5px}
</style></head><body>
<header><h1>Competitor Job Watch</h1>
<nav class=tabs><button data-tab=jobs class=on>Jobs</button><button data-tab=clients>Competitor clients</button>
<button data-tab=accounts>Prospects &amp; Clients</button><button data-tab=sources>Competitors</button></nav>
<div class=small><a href=/export.csv>Export CSV</a> &middot; <a href=/logout>Sign out</a></div></header>
<main><div class=stats id=stats></div>
<div class=filters id=filters>
<input type=search id=q placeholder="Search title, client, location, therapeutic area…">
<select id=f-comp><option value="">All competitors</option></select>
<select id=f-type><option value="">All types</option><option>Direct hire</option><option>Contract-to-hire</option>
<option>Temp / Contract</option><option>Unknown</option></select>
<select id=f-cat><option value="">All categories</option></select>
<select id=f-conf><option value="">Any client confidence</option><option value=high>High</option>
<option value=medium>Medium+</option><option value=low>Low+</option></select>
<select id=f-age><option value=open>Still posted</option><option value=7>Posted in last 7 days</option>
<option value=30>Posted in last 30 days</option><option value="">All time</option></select>
</div>
<form id=acct-form class="filters" autocomplete=off style="display:none">
<input id=a-name placeholder="Company name (e.g. Vertex Pharmaceuticals)" required maxlength=100 style="flex:2;min-width:200px">
<select id=a-status><option value=prospect>Prospect</option><option value=client>Current client</option></select>
<input id=a-url placeholder="Their careers page URL (optional)" style="flex:2;min-width:200px">
<input id=a-alias placeholder="Other names, comma-separated (optional)" style="flex:1;min-width:160px">
<button class=primary id=a-save>Add</button><span id=a-msg class="small err"></span>
</form>
<form id=comp-form class="filters" autocomplete=off style="display:none">
<input id=c-name placeholder="Competitor name (e.g. Korn Ferry)" required maxlength=100 style="flex:2;min-width:200px">
<input id=c-home placeholder="Website (e.g. kornferry.com)" style="flex:2;min-width:200px">
<input id=c-jobs placeholder="Jobs page (optional; found automatically if blank)" style="flex:2;min-width:200px">
<button class=primary id=c-save>Add</button><span id=c-msg class="small err"></span>
</form>
<details id=acct-import style="display:none;margin-bottom:10px"><summary id=imp-title class=small style="cursor:pointer;color:var(--accent)">Import a list of companies (paste from a spreadsheet, or upload a CSV)</summary>
<div class=imp-box>
<p id=imp-help class="small muted" style="margin:6px 0"></p>
<textarea id=imp-text rows=7></textarea>
<div class=filters style="margin:8px 0 0"><input type=file id=imp-file accept=".csv,.tsv,.txt">
<select id=imp-status><option value=prospect>Add as prospects</option><option value=client>Add as current clients</option></select>
<button class=primary id=imp-go type=button>Import</button><span id=imp-msg class=small></span></div>
<div id=imp-detail class="small muted"></div>
</div></details>
<div id=acct-tabs class=seg style="display:none"><button data-s="" class=on>All</button><button data-s=prospect>Prospects</button><button data-s=client>Current clients</button></div>
<div id=view class=card></div></main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA={jobs:[],runs:[],watchlist:[]}, TAB='jobs', SORT={k:'posted',d:-1}, BUSY=new Set(), ERRS={}, ACCT='';
const post=async(url,body)=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.status===401){location.reload();throw new Error('Signed out')}const out=await r.json();if(!r.ok)throw new Error(out.error||'Request failed');return out};
const replaceJob=out=>{const i=DATA.jobs.findIndex(j=>j.job_id===out.job_id);if(i>=0)DATA.jobs[i]=out};
async function decodeNow(id,force=false){BUSY.add(id);delete ERRS[id];render();
  try{replaceJob(await post('/api/decode',{job_id:id,force}))}
  catch(e){ERRS[id]=e.message}finally{BUSY.delete(id);render()}}
async function rate(id,verdict){try{replaceJob(await post('/api/feedback',{job_id:id,verdict}));render()}catch(e){alert(e.message)}}
const modelName=m=>{m=String(m||'').replace(/^claude-/,'').replace(/-(\\d+)-(\\d+)$/,' $1.$2').replace(/-(\\d+)$/,' $1');return m?m[0].toUpperCase()+m.slice(1):'?'};
// Accuracy of decodes you've marked right/wrong, per model: the evidence for keeping Haiku or moving to Sonnet.
function accuracy(){const by={};DATA.jobs.forEach(j=>{const d=j.decode||{};if(d.feedback!=='right'&&d.feedback!=='wrong')return;
  const m=modelName(d.model);(by[m]=by[m]||{r:0,n:0});by[m].n++;if(d.feedback==='right')by[m].r++});return by}
const typeCls=t=>({'Direct hire':'t-direct','Temp / Contract':'t-temp','Contract-to-hire':'t-c2h'}[t]||'muted');
const confRank={high:3,medium:2,low:1,none:0};
const day=s=>s?s.slice(0,10):'';
function latestRun(){return DATA.runs.reduce((m,r)=>r.ran_at>m?r.ran_at:m,'')}
function isOpen(j){const lr=latestRun();if(!lr||!j.last_seen)return true;return (new Date(lr)-new Date(j.last_seen))<3*3600e3}
// How long a posting has been up: from the site's posted date (or when we first saw it) to now, or to when it disappeared.
const postedOn=j=>j.posted_date||day(j.first_seen);
function ageDays(j){const p=postedOn(j);if(!p)return null;const end=isOpen(j)?new Date():new Date(j.last_seen);
  return Math.max(0,Math.floor((end-new Date(p+'T00:00:00'))/864e5))}
const plural=n=>n===1?'1 day':n+' days';
function ageText(j){const n=ageDays(j);if(n===null)return '';return isOpen(j)?(n===0?'posted today':'up '+plural(n)):'was up '+plural(n)}
const fmtDate=s=>s?new Date(s+'T00:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'—';
function ageCell(j){const n=ageDays(j),cls=isOpen(j)&&n>=30?'conf-medium':'muted';
  return `<div>${esc(fmtDate(postedOn(j)))}</div><div class="small ${cls}" title="${j.posted_date&&j.posted?'Date from the posting':'Date we first saw it'}">${esc(ageText(j))}</div>`}
const SUFFIX=new Set('inc incorporated llc ltd limited corp corporation co company plc sa ag gmbh nv the group holdings pharmaceuticals pharmaceutical pharma therapeutics biosciences bioscience biotherapeutics biopharma biopharmaceuticals biotech biotechnology bio sciences labs laboratories medical us usa'.split(' '));
const norm=n=>{const w=(n||'').toLowerCase().replace(/&/g,' and ').match(/[a-z0-9]+/g)||[];const c=w.filter(x=>!SUFFIX.has(x));return (c.length?c:w).join(' ')};
const watchFor=name=>DATA.watchlist.find(w=>[w.name,...(w.aliases||[])].some(n=>norm(n)===norm(name)));
const compJobs=()=>DATA.jobs.filter(j=>!j.direct);
function filtered(){
  const q=$('#q').value.toLowerCase(),c=$('#f-comp').value,t=$('#f-type').value,cat=$('#f-cat').value,
  conf=$('#f-conf').value,age=$('#f-age').value;
  return compJobs().filter(j=>{const d=j.decode||{};
    if(c&&j.competitor!==c)return false; if(t&&j.employment_type!==t)return false;
    if(cat&&!(j.categories||[]).includes(cat))return false;
    if(conf&&(confRank[d.confidence]||0)<confRank[conf])return false;
    if(age==='open'&&!isOpen(j))return false;
    if(age&&age!=='open'&&(Date.now()-new Date(postedOn(j)))>age*864e5)return false;
    if(q){const hay=[j.title,j.competitor,j.location,d.client_guess,d.therapeutic_area,d.modality,
      (d.alternatives||[]).map(a=>a.name).join(' '),(j.categories||[]).join(' '),(j.watch||[]).join(' ')].join(' ').toLowerCase();
      if(!hay.includes(q))return false}
    return true})}
const statHtml=items=>items.map(([v,l])=>`<div class=stat><b class="${String(v).length>8?'sm':''}">${esc(v)}</b><span>${esc(l)}</span></div>`).join('');
function renderStats(rows){
  const n=t=>rows.filter(j=>j.employment_type===t).length, dec=rows.filter(j=>(j.decode||{}).client_guess&&(j.decode.client_guess!=='Unknown')).length;
  const lr=latestRun();
  const acc=Object.entries(accuracy()).sort((a,b)=>b[1].n-a[1].n).slice(0,2)
    .map(([m,a])=>[Math.round(100*a.r/a.n)+'%',`decodes right · ${m} (${a.r}/${a.n} checked)`]);
  $('#stats').innerHTML=statHtml([[rows.length,'postings shown'],[n('Direct hire'),'direct hire'],[n('Temp / Contract')+n('Contract-to-hire'),'temp / contract'],
   [dec,'with a likely client'],...(acc.length?acc:[['—','decode accuracy: mark decodes ✓ / ✗']]),[lr?new Date(lr).toLocaleString():'—','last run']])}
const watchChips=j=>(j.watch||[]).map(n=>{const w=watchFor(n)||{};return `<span class="chip ${w.status==='client'?'w-client':'w-prospect'}">${w.status==='client'?'Client':'Prospect'}: ${esc(n)}</span>`}).join('');
function clientCell(j){const d=j.decode;
  if(BUSY.has(j.job_id))return '<span class="muted small">Decoding… usually under a minute</span>';
  if(!d||!d.client_guess)return `<button class=decode data-id="${esc(j.job_id)}">Decode</button>`+
    (j.auto_decode?' <span class="muted small">queued for auto-decode</span>':'')+
    (ERRS[j.job_id]?`<div class="err small">${esc(ERRS[j.job_id])}</div>`:'');
  const alts=(d.alternatives||[]).map(a=>`<li><b>${esc(a.name)}</b> — ${esc(a.why)}</li>`).join('');
  const ev=(d.evidence||[]).map(e=>`<li>${esc(e)}</li>`).join('');
  const src=(d.source_urls||[]).map(u=>`<li><a href="${esc(u)}" target=_blank rel=noopener>${esc(u.slice(0,70))}</a></li>`).join('');
  const fb=d.feedback, prev=d.previous;
  const rateHtml=`<div class="small rate">${esc(modelName(d.model))} &middot; Right?
    <button class="fb ${fb==='right'?'on-right':''}" data-id="${esc(j.job_id)}" data-v="${fb==='right'?'':'right'}" title="Correct">✓</button><button
    class="fb ${fb==='wrong'?'on-wrong':''}" data-id="${esc(j.job_id)}" data-v="${fb==='wrong'?'':'wrong'}" title="Wrong">✗</button>
    &middot; <a href=# class=redecode data-id="${esc(j.job_id)}">Re-decode</a>
    ${prev?`<div class=muted>Before: ${esc(prev.client_guess)} (${esc(prev.confidence)}, ${esc(modelName(prev.model))}${prev.feedback?', marked '+esc(prev.feedback):''})</div>`:''}
    ${ERRS[j.job_id]?`<div class=err>${esc(ERRS[j.job_id])}</div>`:''}</div>`;
  return `<details><summary><b>${esc(d.client_guess)}</b> <span class="small conf-${esc(d.confidence)}">${esc(d.confidence)}</span></summary>
  <div class=small><span class=muted>${esc([d.client_type,d.therapeutic_area,d.modality,d.development_stage].filter(x=>x&&x!=='Unknown').join(' · '))}</span>
  ${ev?`<ul>${ev}</ul>`:''}${alts?`<div class=muted>Alternatives</div><ul>${alts}</ul>`:''}${src?`<div class=muted>Sources</div><ul>${src}</ul>`:''}</div></details>${rateHtml}`}
function sortRows(rows){const k=SORT.k,dir=SORT.d;const val=j=>k==='client'?((j.decode||{}).client_guess||''):k==='posted'?postedOn(j):(j[k]||'');
  return rows.sort((a,b)=>{const va=val(a),vb=val(b);return (va>vb?1:va<vb?-1:0)*dir})}
function renderJobs(rows){
  const th=(k,l,c='')=>`<th data-k="${k}" class="${c}">${l}${SORT.k===k?(SORT.d>0?' ▲':' ▼'):''}</th>`;
  $('#view').innerHTML=`<table><thead><tr>${th('posted','Posted')}${th('title','Role')}${th('competitor','Competitor')}
  ${th('location','Location','hide-sm')}${th('employment_type','Type')}${th('client','Likely client')}</tr></thead><tbody>
  ${sortRows(rows).map(j=>`<tr class="${isOpen(j)?'':'closed'}"><td class=nowrap>${ageCell(j)}</td>
  <td><a href="${esc(j.url)}" target=_blank rel=noopener>${esc(j.title)}</a><div>${(j.categories||[]).map(c=>`<span class=chip>${esc(c)}</span>`).join('')}${watchChips(j)}</div>
  ${j.salary?`<div class="small muted">${esc(j.salary)}</div>`:''}${isOpen(j)?'':'<div class="small muted">no longer posted</div>'}</td>
  <td>${esc(j.competitor)}</td><td class="hide-sm">${esc(j.location||'—')}</td>
  <td class="${typeCls(j.employment_type)}">${esc(j.employment_type)}</td><td>${clientCell(j)}</td></tr>`).join('')
  ||'<tr><td colspan=6 class=muted>No postings match these filters.</td></tr>'}</tbody></table>`;
  document.querySelectorAll('th[data-k]').forEach(h=>h.onclick=()=>{const k=h.dataset.k;SORT={k,d:SORT.k===k?-SORT.d:(k==='posted'?-1:1)};render()})}
function renderClients(rows){
  const m={};rows.forEach(j=>{const d=j.decode||{};if(!d.client_guess||d.client_guess==='Unknown'||d.confidence==='none')return;
    const k=d.client_guess;(m[k]=m[k]||{name:k,jobs:[],comps:new Set(),types:{},best:0,ta:new Set()});
    m[k].jobs.push(j);m[k].comps.add(j.competitor);m[k].types[j.employment_type]=(m[k].types[j.employment_type]||0)+1;
    m[k].best=Math.max(m[k].best,confRank[d.confidence]||0);if(d.therapeutic_area)m[k].ta.add(d.therapeutic_area)});
  const list=Object.values(m).sort((a,b)=>b.jobs.length-a.jobs.length||b.best-a.best), max=Math.max(1,...list.map(c=>c.jobs.length));
  const cl=['none','low','medium','high'];
  const tag=name=>{const w=watchFor(name);return w?`<span class="chip ${w.status==='client'?'w-client':'w-prospect'}">${w.status==='client'?'Your client':'Your prospect'}</span>`
    :`<button class="decode add-prospect" data-name="${esc(name)}" title="Add to your prospects">+ Prospect</button>`};
  $('#view').innerHTML=`<table><thead><tr><th>Likely client</th><th>Postings</th><th>Via competitor(s)</th><th class=hide-sm>Types</th><th class=hide-sm>Areas</th></tr></thead><tbody>
  ${list.map(c=>`<tr><td><details><summary><b>${esc(c.name)}</b> <span class="small conf-${cl[c.best]}">best: ${cl[c.best]}</span></summary><ul class=small>
  ${c.jobs.map(j=>`<li><a href="${esc(j.url)}" target=_blank rel=noopener>${esc(j.title)}</a> — ${esc(j.competitor)}, ${esc(j.location||'')} <span class=muted>(${esc(fmtDate(postedOn(j)))}, ${esc(ageText(j))}; ${esc(j.decode.confidence)})</span></li>`).join('')}</ul></details>
  <div style="margin-top:4px">${tag(c.name)}</div></td>
  <td><span class=bar style="width:${Math.round(60*c.jobs.length/max)}px"></span> ${c.jobs.length}</td>
  <td>${[...c.comps].map(x=>`<span class=chip>${esc(x)}</span>`).join('')}</td>
  <td class="small hide-sm">${Object.entries(c.types).map(([t,n])=>`<span class="${typeCls(t)}">${esc(t)}: ${n}</span>`).join('<br>')}</td>
  <td class="small muted hide-sm">${esc([...c.ta].slice(0,3).join('; '))}</td></tr>`).join('')
  ||'<tr><td colspan=5 class=muted>No decoded clients yet for these filters.</td></tr>'}</tbody></table>`}
function renderAccounts(){
  const wl=DATA.watchlist.filter(w=>!ACCT||w.status===ACCT);
  const jobsFor=w=>DATA.jobs.filter(j=>(j.watch||[]).includes(w.name)).sort((a,b)=>postedOn(b)>postedOn(a)?1:-1);
  const all=DATA.watchlist.map(w=>({w,open:jobsFor(w).filter(isOpen)}));
  const cnt=s=>all.filter(a=>a.w.status===s);
  $('#stats').innerHTML=statHtml([[cnt('prospect').length,'prospects'],[cnt('prospect').reduce((n,a)=>n+a.open.length,0),'open postings at prospects'],
    [cnt('client').length,'current clients'],[cnt('client').reduce((n,a)=>n+a.open.length,0),'open postings at clients']]);
  if(!DATA.watchlist.length){$('#view').innerHTML=`<p style="padding:14px" class=muted>Add the companies you want to track above. For each one you'll see
    competitor postings that look like they're for that company, and, if you add its careers page, the company's own life-science postings, checked hourly.</p>`;return}
  $('#view').innerHTML=`<table><thead><tr><th>Company</th><th>Open postings</th><th class=hide-sm>Via</th><th>Newest</th><th></th></tr></thead><tbody>
  ${wl.map(w=>{const js=jobsFor(w),open=js.filter(isOpen),newest=open[0];
    const via=[...new Set(open.map(j=>j.direct?'Own site':j.competitor))];
    return `<tr><td><details><summary><b>${esc(w.name)}</b> <span class="chip ${w.status==='client'?'w-client':'w-prospect'}">${w.status==='client'?'Current client':'Prospect'}</span></summary>
    <div class=small>${w.careers_url?`Careers page: <a href="${esc(w.careers_url)}" target=_blank rel=noopener>${esc(w.careers_url.slice(0,60))}</a>`:'<span class=muted>No careers page added: only competitor postings are tracked.</span>'}
    ${(w.aliases||[]).length?`<br><span class=muted>Also matches:</span> ${esc(w.aliases.join(', '))}`:''}
    ${js.length?`<table class=inner><tbody>${js.map(j=>`<tr class="${isOpen(j)?'':'closed'}"><td class=nowrap>${ageCell(j)}</td><td><a href="${esc(j.url)}" target=_blank rel=noopener>${esc(j.title)}</a>
      <div class=muted>${esc(j.location||'')}</div></td><td>${j.direct?'Their own site':esc(j.competitor)+(j.decode&&j.decode.client_guess?` <span class="muted">(${esc(j.decode.confidence)} match)</span>`:' <span class=muted>(named in posting)</span>')}</td>
      <td class="${typeCls(j.employment_type)}">${esc(j.employment_type)}</td></tr>`).join('')}</tbody></table>`:'<p class=muted>No matching postings yet.</p>'}</div></details></td>
    <td data-l="Open postings"><b>${open.length}</b>${js.length>open.length?` <span class="small muted">(+${js.length-open.length} closed)</span>`:''}</td>
    <td class=hide-sm>${via.map(v=>`<span class=chip>${esc(v)}</span>`).join('')}</td>
    <td data-l="Newest" class=small>${newest?`${esc(newest.title)}<div class=muted>${esc(ageText(newest))}</div>`:'<span class=muted>—</span>'}</td>
    <td class="small nowrap"><a href=# class=a-edit data-name="${esc(w.name)}">Edit</a> &middot;
      <a href=# class=a-toggle data-name="${esc(w.name)}">${w.status==='client'?'Mark prospect':'Mark client'}</a> &middot;
      <a href=# class=a-del data-name="${esc(w.name)}">Remove</a></td></tr>`}).join('')
  ||'<tr><td colspan=5 class=muted>None in this group.</td></tr>'}</tbody></table>`}
function renderSources(){
  const runs={};DATA.runs.forEach(r=>runs[r.competitor]=r);
  const counts={};DATA.jobs.forEach(j=>{if(isOpen(j)&&!j.direct)counts[j.competitor]=(counts[j.competitor]||0)+1});
  const link=(u,t)=>u?`<a href="${esc(u)}" target=_blank rel=noopener>${t}</a>`:'';
  const status=(c,r)=>!c.enabled?(c.source==='suggested'?'<span class=muted>Suggested: not tracking yet</span>':'<span class=muted>Paused</span>')
    :!r?'<span class=muted>Waiting for the next hourly run</span>'
    :r.error?`<span class=err>${esc(r.error)}</span>`
    :!r.found?'<span class=err>No jobs read: add the jobs page link (Edit)</span>':'<span class=conf-high>OK</span>';
  const comps=DATA.competitors||[], active=comps.filter(c=>c.enabled).length;
  const direct=DATA.runs.filter(r=>r.direct).sort((a,b)=>a.competitor.localeCompare(b.competitor));
  $('#stats').innerHTML=statHtml([[active,'competitors tracked'],[comps.filter(c=>!c.enabled).length,'paused or suggested'],
    [Object.values(runs).filter(r=>!r.direct&&(r.error||!r.found)).length,'sites with problems'],[direct.length,'prospect/client careers pages']]);
  $('#view').innerHTML=`<table><thead><tr><th>Competitor</th><th>Jobs on site</th><th>Matching (open)</th><th>New last run</th><th class=hide-sm>Last run</th><th>Status</th><th></th></tr></thead><tbody>
  ${comps.map(c=>{const r=runs[c.name];const sug=c.source==='suggested';
    return `<tr class="${c.enabled?'':'closed'}"><td><b>${esc(c.name)}</b> ${sug?'<span class=chip>Suggested</span>':c.source==='added'?'<span class=chip>Added by you</span>':''}
    <div class="small">${link(c.homepage,'Website')}${c.homepage&&c.careers_url?' &middot; ':''}${link(c.careers_url,'Jobs page')}</div></td>
    <td data-l="Jobs on site">${r?r.found:'—'}</td><td data-l="Matching (open)">${c.enabled?(counts[c.name]||0):'—'}</td>
    <td data-l="New last run">${r?r.new_matched+(r.deferred?` <span class="small muted">(+${r.deferred} queued)</span>`:''):'—'}</td>
    <td class="small muted hide-sm">${r?esc(new Date(r.ran_at).toLocaleString()):'—'}</td><td class=small>${status(c,r)}</td>
    <td class="small nowrap">${c.enabled?'':`<a href=# class=c-resume data-name="${esc(c.name)}">${sug?'Add':'Resume'}</a> &middot; `}<a href=# class=c-edit data-name="${esc(c.name)}">Edit</a>${c.enabled?` &middot; <a href=# class=c-pause data-name="${esc(c.name)}">Pause</a>`:''}${c.source==='added'?` &middot; <a href=# class=c-del data-name="${esc(c.name)}">Remove</a>`:''}</td></tr>`}).join('')
  ||'<tr><td colspan=7 class=muted>No competitors yet.</td></tr>'}</tbody></table>
  ${direct.length?`<div style="padding:10px 12px 4px" class="small muted">Careers pages of your prospects and clients</div><table><thead><tr><th>Company</th><th>Jobs on site</th><th>New last run</th><th>Status</th></tr></thead><tbody>
  ${direct.map(r=>`<tr><td><b>${esc(r.competitor)}</b></td><td data-l="Jobs on site">${r.found}</td><td data-l="New last run">${r.new_matched}</td>
  <td class=small>${r.error?`<span class=err>${esc(r.error)}</span>`:!r.found?'<span class=err>No jobs read: check the careers page URL</span>':'<span class=conf-high>OK</span>'}</td></tr>`).join('')}</tbody></table>`:''}`}
function render(){
  $('#filters').style.display=TAB==='jobs'||TAB==='clients'?'flex':'none';
  $('#acct-form').style.display=$('#acct-tabs').style.display=TAB==='accounts'?'flex':'none';
  $('#acct-import').style.display=TAB==='accounts'||TAB==='sources'?'block':'none';
  $('#comp-form').style.display=TAB==='sources'?'flex':'none';
  $('#imp-status').style.display=TAB==='accounts'?'':'none';
  $('#imp-text').placeholder=TAB==='sources'
    ?['Korn Ferry, kornferry.com','Planet Pharma\\tplanetpharma.com\\tplanetpharma.com/jobs','Slone Partners, https://www.slonepartners.com'].join('\\n')
    :['Vertex Pharmaceuticals, https://careers.vrtx.com','Moderna\\thttps://www.modernatx.com/careers\\tclient','Ionis Pharmaceuticals'].join('\\n');
  $('#imp-title').textContent=TAB==='sources'?'Import a list of competitors (paste from a spreadsheet, or upload a CSV)':'Import a list of companies (paste from a spreadsheet, or upload a CSV)';
  $('#imp-help').innerHTML=TAB==='sources'
    ?'One competitor per line: <b>name</b>, then its <b>website</b> and optionally its <b>jobs page</b>, separated by commas or tabs. Copying columns straight from Excel or Google Sheets works. Competitors already tracked are skipped; paused or suggested ones are switched on.'
    :'One company per line: <b>name</b>, then optionally a <b>careers page URL</b> and <b>prospect</b> or <b>client</b>, separated by commas or tabs. Copying two or three columns straight from Excel or Google Sheets works. A header row is fine. Companies you already track are left alone.';
  if(TAB==='accounts')return renderAccounts();
  const rows=filtered();renderStats(rows);
  ({jobs:renderJobs,clients:renderClients,sources:renderSources})[TAB](rows)}
async function saveWatch(body){const out=await post('/api/watchlist',body);DATA.watchlist=out.watchlist;await load()}
async function load(){const r=await fetch('/api/data');if(r.status===401){location.reload();return}
  const d=await r.json();DATA=d;
  const fill=(sel,first,vals)=>{const cur=sel.value;sel.innerHTML=`<option value="">${first}</option>`+vals.sort().map(v=>`<option>${esc(v)}</option>`).join('');sel.value=cur};
  fill($('#f-comp'),'All competitors',[...new Set(compJobs().map(j=>j.competitor))]);
  fill($('#f-cat'),'All categories',[...new Set(compJobs().flatMap(j=>j.categories||[]))]);
  render()}
document.addEventListener('click',async e=>{const t=e.target;
  const b=t.closest('button.decode:not(.add-prospect)');if(b)return decodeNow(b.dataset.id);
  const fbb=t.closest('button.fb');if(fbb)return rate(fbb.dataset.id,fbb.dataset.v);
  const rd=t.closest('a.redecode');if(rd){e.preventDefault();
    if(confirm('Decode this posting again with the current model? This costs one more decode.'))decodeNow(rd.dataset.id,true);return}
  const add=t.closest('.add-prospect');if(add){add.disabled=true;add.textContent='Adding…';
    try{await saveWatch({name:add.dataset.name,status:'prospect'})}catch(err){alert(err.message)}return}
  const a=t.closest('a.a-edit,a.a-toggle,a.a-del');if(!a)return;e.preventDefault();
  const w=DATA.watchlist.find(x=>x.name===a.dataset.name);if(!w)return;
  if(a.classList.contains('a-del')){if(!confirm(`Stop tracking ${w.name}?`))return;
    const out=await post('/api/watchlist/delete',{name:w.name});DATA.watchlist=out.watchlist;return load()}
  if(a.classList.contains('a-toggle'))return saveWatch({...w,status:w.status==='client'?'prospect':'client'}).catch(err=>alert(err.message));
  $('#a-name').value=w.name;$('#a-status').value=w.status;$('#a-url').value=w.careers_url||'';$('#a-alias').value=(w.aliases||[]).join(', ');
  $('#a-save').textContent='Save';$('#a-name').focus()});
// Parse pasted/uploaded text: name [, careers URL] [, prospect|client]; commas or tabs; quotes allowed; header row skipped.
function splitRow(line){const sep=line.includes('\\t')?'\\t':',',out=[];let cur='',q=false;
  for(const ch of line){if(ch==='"')q=!q;else if(ch===sep&&!q){out.push(cur);cur=''}else cur+=ch}out.push(cur);return out.map(s=>s.trim())}
function parseList(text,defStatus){const seen=new Set(),items=[];
  text.split(/\\r?\\n/).forEach((line,i)=>{if(!line.trim())return;const f=splitRow(line);
    if(i===0&&/^(company|name|account)\\b/i.test(f[0]))return;
    const name=f[0].replace(/^"|"$/g,'').trim();if(!name||seen.has(name.toLowerCase()))return;seen.add(name.toLowerCase());
    const rest=f.slice(1);const isUrl=x=>/^(https?:\\/\\/)?[\\w-]+(\\.[\\w-]+)+(\\/\\S*)?$/i.test(x)&&!/^(prospect|client)$/i.test(x);
    const urls=rest.filter(isUrl);
    const st=(rest.find(x=>/^(prospect|(current )?client)s?$/i.test(x))||'').toLowerCase();
    items.push({name,urls,status:st?(st.includes('client')?'client':'prospect'):defStatus})});
  return items}
$('#imp-file').addEventListener('change',async e=>{const f=e.target.files[0];if(f)$('#imp-text').value=await f.text()});
$('#imp-go').addEventListener('click',async()=>{const msg=$('#imp-msg'),det=$('#imp-detail');msg.className='small';det.textContent='';
  const parsed=parseList($('#imp-text').value,$('#imp-status').value), comps=TAB==='sources';
  if(!parsed.length){msg.className='small err';msg.textContent='Nothing to import: paste at least one name.';return}
  msg.textContent=`Importing ${parsed.length}…`;
  try{let out;
    if(comps){out=await post('/api/competitors/import',{items:parsed.map(p=>({name:p.name,homepage:p.urls[0]||'',careers_url:p.urls[1]||''}))});DATA.competitors=out.competitors}
    else{out=await post('/api/watchlist/import',{items:parsed.map(p=>({name:p.name,careers_url:p.urls[0]||'',status:p.status})),default_status:$('#imp-status').value});DATA.watchlist=out.watchlist}
    msg.textContent=`Added ${out.added}`+(out.resumed?`, switched on ${out.resumed}`:'')+(out.updated?`, updated ${out.updated}`:'')+(out.skipped.length?`, skipped ${out.skipped.length}`:'');
    det.textContent=out.skipped.map(s=>`${s.name}: ${s.reason}`).join(' · ');
    $('#imp-text').value='';$('#imp-file').value='';await load()}catch(err){msg.className='small err';msg.textContent=err.message}});
async function saveComp(body){const out=await post('/api/competitors',body);DATA.competitors=out.competitors;render()}
document.addEventListener('click',async e=>{const a=e.target.closest('a.c-resume,a.c-pause,a.c-edit,a.c-del');if(!a)return;e.preventDefault();
  const c=(DATA.competitors||[]).find(x=>x.name===a.dataset.name);if(!c)return;
  try{if(a.classList.contains('c-del')){if(!confirm(`Remove ${c.name}? Its postings stay in the list.`))return;
      const out=await post('/api/competitors/delete',{name:c.name});DATA.competitors=out.competitors;return render()}
    if(a.classList.contains('c-pause'))return saveComp({...c,enabled:false});
    if(a.classList.contains('c-resume'))return saveComp({...c,enabled:true});
    $('#c-name').value=c.name;$('#c-home').value=c.homepage||'';$('#c-jobs').value=c.careers_url||'';$('#c-save').textContent='Save';$('#c-name').focus()}
  catch(err){alert(err.message)}});
$('#comp-form').addEventListener('submit',async e=>{e.preventDefault();$('#c-msg').textContent='';
  const cur=(DATA.competitors||[]).find(x=>x.name.toLowerCase()===$('#c-name').value.trim().toLowerCase());
  try{await saveComp({name:$('#c-name').value,homepage:$('#c-home').value,careers_url:$('#c-jobs').value,enabled:true});
    e.target.reset();$('#c-save').textContent='Add'}catch(err){$('#c-msg').textContent=err.message}});
$('#acct-form').addEventListener('submit',async e=>{e.preventDefault();$('#a-msg').textContent='';
  const existing=DATA.watchlist.find(x=>x.name.toLowerCase()===$('#a-name').value.trim().toLowerCase())||{};
  try{await saveWatch({name:$('#a-name').value,status:$('#a-status').value,careers_url:$('#a-url').value,aliases:$('#a-alias').value,notes:existing.notes||''});
    e.target.reset();$('#a-save').textContent='Add'}catch(err){$('#a-msg').textContent=err.message}});
document.querySelectorAll('#acct-tabs button').forEach(b=>b.onclick=()=>{ACCT=b.dataset.s;
  document.querySelectorAll('#acct-tabs button').forEach(x=>x.classList.toggle('on',x===b));render()});
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{TAB=b.dataset.tab;
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));render()});
document.querySelectorAll('#filters input,#filters select').forEach(e=>e.addEventListener('input',render));
load().catch(()=>{$('#view').innerHTML='<p class=err style="padding:12px">Could not load data.</p>'});
</script></body></html>"""
