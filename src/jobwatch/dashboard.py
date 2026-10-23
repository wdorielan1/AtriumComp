"""Password-protected web dashboard, served from a Lambda Function URL.

Routes: GET /            dashboard (or login form)
        POST /login      password check -> session cookie
        GET /api/data    jobs + run stats as JSON
        POST /api/decode decode one posting on demand ({"job_id": ...})
        POST /api/watchlist         add/update a prospect or current client
        POST /api/watchlist/delete  remove one ({"name": ...})
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

from . import classify, decode, watch
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


def _load(days: int | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Matched jobs first seen in the last `days` days (keeps the response under Lambda's 6 MB cap),
    run stats, and the watchlist."""
    days = int(env("DASHBOARD_DAYS", "120")) if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    store = get_store()
    wl = sorted(store.watchlist(), key=lambda w: w["name"].lower())
    jobs = [_prepare(j, wl) for j in store.matched_jobs() if j.get("first_seen", "") >= cutoff]
    return jobs, store.runs(), wl


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
    data, err = _json_body(event)
    if err:
        return err
    if not data.get("job_id"):
        return _resp(400, '{"error":"job_id required"}', "application/json")
    job_id = str(data["job_id"])

    store = get_store()
    data = store.get_job(job_id)
    if not data or not data.get("matched"):
        return _resp(404, '{"error":"job not found"}', "application/json")
    job = Job.from_dict(data)
    if not job.decode:  # already decoded (e.g. a double click): return it without paying again
        if not decode.enabled():
            return _resp(503, '{"error":"Decoding is not configured (no Anthropic API key)."}', "application/json")
        if not decode.decode_and_apply(job):
            return _resp(502, '{"error":"Decoding failed. Try again in a minute."}', "application/json")
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
    if path == "/api/watchlist" and method == "POST":
        return _save_watch(event)
    if path == "/api/watchlist/delete" and method == "POST":
        return _delete_watch(event)
    if path == "/api/data":
        jobs, runs, wl = _load()
        return _resp(200, json.dumps({"jobs": jobs, "runs": runs, "watchlist": wl}), "application/json")
    if path == "/export.csv":
        jobs, _, _ = _load(days=0)
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
.nowrap{white-space:nowrap}.w-client{background:color-mix(in srgb,var(--hi) 18%,transparent);color:var(--hi)}
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
<button data-tab=accounts>Prospects &amp; Clients</button><button data-tab=sources>Sources</button></nav>
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
<div id=acct-tabs class=seg style="display:none"><button data-s="" class=on>All</button><button data-s=prospect>Prospects</button><button data-s=client>Current clients</button></div>
<div id=view class=card></div></main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA={jobs:[],runs:[],watchlist:[]}, TAB='jobs', SORT={k:'posted',d:-1}, BUSY=new Set(), ERRS={}, ACCT='';
const post=async(url,body)=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.status===401){location.reload();throw new Error('Signed out')}const out=await r.json();if(!r.ok)throw new Error(out.error||'Request failed');return out};
async function decodeNow(id){BUSY.add(id);delete ERRS[id];render();
  try{const out=await post('/api/decode',{job_id:id});const i=DATA.jobs.findIndex(j=>j.job_id===id);if(i>=0)DATA.jobs[i]=out}
  catch(e){ERRS[id]=e.message}finally{BUSY.delete(id);render()}}
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
  $('#stats').innerHTML=statHtml([[rows.length,'postings shown'],[n('Direct hire'),'direct hire'],[n('Temp / Contract')+n('Contract-to-hire'),'temp / contract'],
   [dec,'with a likely client'],[lr?new Date(lr).toLocaleString():'—','last run']])}
const watchChips=j=>(j.watch||[]).map(n=>{const w=watchFor(n)||{};return `<span class="chip ${w.status==='client'?'w-client':'w-prospect'}">${w.status==='client'?'Client':'Prospect'}: ${esc(n)}</span>`}).join('');
function clientCell(j){const d=j.decode;
  if(BUSY.has(j.job_id))return '<span class="muted small">Decoding… usually under a minute</span>';
  if(!d||!d.client_guess)return `<button class=decode data-id="${esc(j.job_id)}">Decode</button>`+
    (j.auto_decode?' <span class="muted small">queued for auto-decode</span>':'')+
    (ERRS[j.job_id]?`<div class="err small">${esc(ERRS[j.job_id])}</div>`:'');
  const alts=(d.alternatives||[]).map(a=>`<li><b>${esc(a.name)}</b> — ${esc(a.why)}</li>`).join('');
  const ev=(d.evidence||[]).map(e=>`<li>${esc(e)}</li>`).join('');
  const src=(d.source_urls||[]).map(u=>`<li><a href="${esc(u)}" target=_blank rel=noopener>${esc(u.slice(0,70))}</a></li>`).join('');
  return `<details><summary><b>${esc(d.client_guess)}</b> <span class="small conf-${esc(d.confidence)}">${esc(d.confidence)}</span></summary>
  <div class=small><span class=muted>${esc([d.client_type,d.therapeutic_area,d.modality,d.development_stage].filter(x=>x&&x!=='Unknown').join(' · '))}</span>
  ${ev?`<ul>${ev}</ul>`:''}${alts?`<div class=muted>Alternatives</div><ul>${alts}</ul>`:''}${src?`<div class=muted>Sources</div><ul>${src}</ul>`:''}</div></details>`}
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
  const runs=[...DATA.runs].sort((a,b)=>(a.direct-b.direct)||a.competitor.localeCompare(b.competitor));
  const counts={};DATA.jobs.forEach(j=>{if(isOpen(j))counts[j.competitor]=(counts[j.competitor]||0)+1});
  $('#view').innerHTML=`<table><thead><tr><th>Site</th><th>Jobs on site</th><th>Matching (open)</th><th>New last run</th><th class=hide-sm>Last run</th><th>Status</th></tr></thead><tbody>
  ${runs.map(r=>`<tr><td><b>${esc(r.competitor)}</b>${r.direct?' <span class="small muted">(careers page of a prospect/client)</span>':''}</td><td data-l="Jobs on site">${r.found}</td><td data-l="Matching (open)">${counts[r.competitor]||0}</td><td data-l="New last run">${r.new_matched}${r.deferred?` <span class="small muted">(+${r.deferred} queued)</span>`:''}</td>
  <td class="small muted hide-sm">${esc(new Date(r.ran_at).toLocaleString())}</td>
  <td class=small>${r.error?`<span class=err>${esc(r.error)}</span>`:!r.found?`<span class=err>No jobs read — check the ${r.direct?'careers page URL':'URL in competitors.yaml'}</span>`:'<span class=conf-high>OK</span>'}</td></tr>`).join('')
  ||'<tr><td colspan=6 class=muted>No runs yet.</td></tr>'}</tbody></table>`}
function render(){
  $('#filters').style.display=TAB==='jobs'||TAB==='clients'?'flex':'none';
  $('#acct-form').style.display=$('#acct-tabs').style.display=TAB==='accounts'?'flex':'none';
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
  const add=t.closest('.add-prospect');if(add){add.disabled=true;add.textContent='Adding…';
    try{await saveWatch({name:add.dataset.name,status:'prospect'})}catch(err){alert(err.message)}return}
  const a=t.closest('a.a-edit,a.a-toggle,a.a-del');if(!a)return;e.preventDefault();
  const w=DATA.watchlist.find(x=>x.name===a.dataset.name);if(!w)return;
  if(a.classList.contains('a-del')){if(!confirm(`Stop tracking ${w.name}?`))return;
    const out=await post('/api/watchlist/delete',{name:w.name});DATA.watchlist=out.watchlist;return load()}
  if(a.classList.contains('a-toggle'))return saveWatch({...w,status:w.status==='client'?'prospect':'client'}).catch(err=>alert(err.message));
  $('#a-name').value=w.name;$('#a-status').value=w.status;$('#a-url').value=w.careers_url||'';$('#a-alias').value=(w.aliases||[]).join(', ');
  $('#a-save').textContent='Save';$('#a-name').focus()});
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
