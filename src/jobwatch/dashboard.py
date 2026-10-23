"""Password-protected web dashboard, served from a Lambda Function URL.

Routes: GET /            dashboard (or login form)
        POST /login      password check -> session cookie
        GET /api/data    jobs + run stats as JSON
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

from .config import env
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


def _jobs_and_runs(days: int | None = None) -> tuple[list[dict], list[dict]]:
    """Matched jobs first seen in the last `days` days (keeps the response under Lambda's 6 MB cap)."""
    days = int(env("DASHBOARD_DAYS", "120")) if days is None else days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days else ""
    store = get_store()
    jobs = [j for j in store.matched_jobs() if j.get("first_seen", "") >= cutoff]
    for j in jobs:
        j.pop("description", None)  # keep the payload small; full text stays in the table
    return jobs, store.runs()


def _csv(jobs: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_seen", "last_seen", "competitor", "title", "location", "employment_type", "categories",
                "salary", "likely_client", "confidence", "client_type", "therapeutic_area", "evidence", "url"])
    for j in sorted(jobs, key=lambda j: j.get("first_seen", ""), reverse=True):
        d = j.get("decode") or {}
        w.writerow([j.get("first_seen", ""), j.get("last_seen", ""), j.get("competitor", ""), j.get("title", ""),
                    j.get("location", ""), j.get("employment_type", ""), "; ".join(j.get("categories", [])),
                    j.get("salary", ""), d.get("client_guess", ""), d.get("confidence", ""),
                    d.get("client_type", ""), d.get("therapeutic_area", ""), " | ".join(d.get("evidence", [])),
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

    if path == "/api/data":
        jobs, runs = _jobs_and_runs()
        return _resp(200, json.dumps({"jobs": jobs, "runs": runs}), "application/json")
    if path == "/export.csv":
        jobs, _ = _jobs_and_runs(days=0)
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
.bar{height:6px;border-radius:3px;background:var(--accent);display:inline-block;vertical-align:middle}
@media (max-width:700px){.hide-sm{display:none}main{padding:12px}
table,tbody,tr,td{display:block;width:100%}thead{display:none}tr{padding:10px 0;border-bottom:1px solid var(--border)}
tr:last-child{border-bottom:0}td{border:0;padding:2px 12px}td:empty{display:none}
td[data-l]::before{content:attr(data-l) ": ";color:var(--muted)}}
.stat b.sm{font-size:15px;padding-top:5px}
</style></head><body>
<header><h1>Competitor Job Watch</h1>
<nav class=tabs><button data-tab=jobs class=on>Jobs</button><button data-tab=clients>Clients</button>
<button data-tab=sources>Sources</button></nav>
<div class=small><a href=/export.csv>Export CSV</a> &middot; <a href=/logout>Sign out</a></div></header>
<main><div class=stats id=stats></div>
<div class=filters>
<input type=search id=q placeholder="Search title, client, location, therapeutic area…">
<select id=f-comp><option value="">All competitors</option></select>
<select id=f-type><option value="">All types</option><option>Direct hire</option><option>Contract-to-hire</option>
<option>Temp / Contract</option><option>Unknown</option></select>
<select id=f-cat><option value="">All categories</option></select>
<select id=f-conf><option value="">Any client confidence</option><option value=high>High</option>
<option value=medium>Medium+</option><option value=low>Low+</option></select>
<select id=f-age><option value=open>Still posted</option><option value=7>New in last 7 days</option>
<option value=30>New in last 30 days</option><option value="">All time</option></select>
</div>
<div id=view class=card></div></main>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA={jobs:[],runs:[]}, TAB='jobs', SORT={k:'first_seen',d:-1};
const typeCls=t=>({'Direct hire':'t-direct','Temp / Contract':'t-temp','Contract-to-hire':'t-c2h'}[t]||'muted');
const confRank={high:3,medium:2,low:1,none:0};
const day=s=>s?s.slice(0,10):'';
function latestRun(){return DATA.runs.reduce((m,r)=>r.ran_at>m?r.ran_at:m,'')}
function isOpen(j){const lr=latestRun();if(!lr||!j.last_seen)return true;return (new Date(lr)-new Date(j.last_seen))<3*3600e3}
function filtered(){
  const q=$('#q').value.toLowerCase(),c=$('#f-comp').value,t=$('#f-type').value,cat=$('#f-cat').value,
  conf=$('#f-conf').value,age=$('#f-age').value;
  return DATA.jobs.filter(j=>{const d=j.decode||{};
    if(c&&j.competitor!==c)return false; if(t&&j.employment_type!==t)return false;
    if(cat&&!(j.categories||[]).includes(cat))return false;
    if(conf&&(confRank[d.confidence]||0)<confRank[conf])return false;
    if(age==='open'&&!isOpen(j))return false;
    if(age&&age!=='open'&&(Date.now()-new Date(j.first_seen))>age*864e5)return false;
    if(q){const hay=[j.title,j.competitor,j.location,d.client_guess,d.therapeutic_area,d.modality,
      (d.alternatives||[]).map(a=>a.name).join(' '),(j.categories||[]).join(' ')].join(' ').toLowerCase();
      if(!hay.includes(q))return false}
    return true})}
function renderStats(rows){
  const n=t=>rows.filter(j=>j.employment_type===t).length, dec=rows.filter(j=>(j.decode||{}).client_guess&&(j.decode.client_guess!=='Unknown')).length;
  const lr=latestRun();
  $('#stats').innerHTML=[[rows.length,'postings shown'],[n('Direct hire'),'direct hire'],[n('Temp / Contract')+n('Contract-to-hire'),'temp / contract'],
   [dec,'with a likely client'],[lr?new Date(lr).toLocaleString():'—','last run']]
   .map(([v,l])=>`<div class=stat><b class="${String(v).length>8?'sm':''}">${esc(v)}</b><span>${esc(l)}</span></div>`).join('')}
function clientCell(d){if(!d||!d.client_guess)return '<span class="muted small">decoding…</span>';
  const alts=(d.alternatives||[]).map(a=>`<li><b>${esc(a.name)}</b> — ${esc(a.why)}</li>`).join('');
  const ev=(d.evidence||[]).map(e=>`<li>${esc(e)}</li>`).join('');
  const src=(d.source_urls||[]).map(u=>`<li><a href="${esc(u)}" target=_blank rel=noopener>${esc(u.slice(0,70))}</a></li>`).join('');
  return `<details><summary><b>${esc(d.client_guess)}</b> <span class="small conf-${esc(d.confidence)}">${esc(d.confidence)}</span></summary>
  <div class=small><span class=muted>${esc([d.client_type,d.therapeutic_area,d.modality,d.development_stage].filter(x=>x&&x!=='Unknown').join(' · '))}</span>
  ${ev?`<ul>${ev}</ul>`:''}${alts?`<div class=muted>Alternatives</div><ul>${alts}</ul>`:''}${src?`<div class=muted>Sources</div><ul>${src}</ul>`:''}</div></details>`}
function sortRows(rows){const k=SORT.k,dir=SORT.d;return rows.sort((a,b)=>{
  const va=k==='client'?((a.decode||{}).client_guess||''):(a[k]||''),vb=k==='client'?((b.decode||{}).client_guess||''):(b[k]||'');
  return (va>vb?1:va<vb?-1:0)*dir})}
function renderJobs(rows){
  const th=(k,l,c='')=>`<th data-k="${k}" class="${c}">${l}${SORT.k===k?(SORT.d>0?' ▲':' ▼'):''}</th>`;
  $('#view').innerHTML=`<table><thead><tr>${th('first_seen','First seen','hide-sm')}${th('title','Role')}${th('competitor','Competitor')}
  ${th('location','Location','hide-sm')}${th('employment_type','Type')}${th('client','Likely client')}</tr></thead><tbody>
  ${sortRows(rows).map(j=>`<tr class="${isOpen(j)?'':'closed'}"><td class="small muted hide-sm">${esc(day(j.first_seen))}</td>
  <td><a href="${esc(j.url)}" target=_blank rel=noopener>${esc(j.title)}</a><div>${(j.categories||[]).map(c=>`<span class=chip>${esc(c)}</span>`).join('')}</div>
  ${j.salary?`<div class="small muted">${esc(j.salary)}</div>`:''}${isOpen(j)?'':'<div class="small muted">no longer posted</div>'}</td>
  <td>${esc(j.competitor)}</td><td class="hide-sm">${esc(j.location||'—')}</td>
  <td class="${typeCls(j.employment_type)}">${esc(j.employment_type)}</td><td>${clientCell(j.decode)}</td></tr>`).join('')
  ||'<tr><td colspan=6 class=muted>No postings match these filters.</td></tr>'}</tbody></table>`;
  document.querySelectorAll('th[data-k]').forEach(h=>h.onclick=()=>{const k=h.dataset.k;SORT={k,d:SORT.k===k?-SORT.d:(k==='first_seen'?-1:1)};render()})}
function renderClients(rows){
  const m={};rows.forEach(j=>{const d=j.decode||{};if(!d.client_guess||d.client_guess==='Unknown'||d.confidence==='none')return;
    const k=d.client_guess;(m[k]=m[k]||{name:k,jobs:[],comps:new Set(),types:{},best:0,ta:new Set()});
    m[k].jobs.push(j);m[k].comps.add(j.competitor);m[k].types[j.employment_type]=(m[k].types[j.employment_type]||0)+1;
    m[k].best=Math.max(m[k].best,confRank[d.confidence]||0);if(d.therapeutic_area)m[k].ta.add(d.therapeutic_area)});
  const list=Object.values(m).sort((a,b)=>b.jobs.length-a.jobs.length||b.best-a.best), max=Math.max(1,...list.map(c=>c.jobs.length));
  const cl=['none','low','medium','high'];
  $('#view').innerHTML=`<table><thead><tr><th>Likely client</th><th>Postings</th><th>Via competitor(s)</th><th class=hide-sm>Types</th><th class=hide-sm>Areas</th></tr></thead><tbody>
  ${list.map(c=>`<tr><td><details><summary><b>${esc(c.name)}</b> <span class="small conf-${cl[c.best]}">best: ${cl[c.best]}</span></summary><ul class=small>
  ${c.jobs.map(j=>`<li><a href="${esc(j.url)}" target=_blank rel=noopener>${esc(j.title)}</a> — ${esc(j.competitor)}, ${esc(j.location||'')} <span class=muted>(${esc(day(j.first_seen))}, ${esc(j.decode.confidence)})</span></li>`).join('')}</ul></details></td>
  <td><span class=bar style="width:${Math.round(60*c.jobs.length/max)}px"></span> ${c.jobs.length}</td>
  <td>${[...c.comps].map(x=>`<span class=chip>${esc(x)}</span>`).join('')}</td>
  <td class="small hide-sm">${Object.entries(c.types).map(([t,n])=>`<span class="${typeCls(t)}">${esc(t)}: ${n}</span>`).join('<br>')}</td>
  <td class="small muted hide-sm">${esc([...c.ta].slice(0,3).join('; '))}</td></tr>`).join('')
  ||'<tr><td colspan=5 class=muted>No decoded clients yet for these filters.</td></tr>'}</tbody></table>`}
function renderSources(){
  const runs=[...DATA.runs].sort((a,b)=>a.competitor.localeCompare(b.competitor));
  const counts={};DATA.jobs.forEach(j=>{if(isOpen(j))counts[j.competitor]=(counts[j.competitor]||0)+1});
  $('#view').innerHTML=`<table><thead><tr><th>Competitor</th><th>Jobs on site</th><th>Matching (open)</th><th>New last run</th><th class=hide-sm>Last run</th><th>Status</th></tr></thead><tbody>
  ${runs.map(r=>`<tr><td><b>${esc(r.competitor)}</b></td><td data-l="Jobs on site">${r.found}</td><td data-l="Matching (open)">${counts[r.competitor]||0}</td><td data-l="New last run">${r.new_matched}${r.deferred?` <span class="small muted">(+${r.deferred} queued)</span>`:''}</td>
  <td class="small muted hide-sm">${esc(new Date(r.ran_at).toLocaleString())}</td>
  <td class=small>${r.error?`<span class=err>${esc(r.error)}</span>`:!r.found?'<span class=err>No jobs read — check URL in competitors.yaml</span>':'<span class=conf-high>OK</span>'}</td></tr>`).join('')
  ||'<tr><td colspan=6 class=muted>No runs yet.</td></tr>'}</tbody></table>`}
function render(){const rows=filtered();renderStats(rows);
  document.querySelector('.filters').style.display=TAB==='sources'?'none':'flex';
  ({jobs:renderJobs,clients:renderClients,sources:renderSources})[TAB](rows)}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{TAB=b.dataset.tab;
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.toggle('on',x===b));render()});
document.querySelectorAll('.filters input,.filters select').forEach(e=>e.addEventListener('input',render));
fetch('/api/data').then(r=>{if(r.status===401){location.reload();throw 0}return r.json()}).then(d=>{DATA=d;
  const opts=(sel,vals)=>vals.sort().forEach(v=>sel.insertAdjacentHTML('beforeend',`<option>${esc(v)}</option>`));
  opts($('#f-comp'),[...new Set(d.jobs.map(j=>j.competitor))]);opts($('#f-cat'),[...new Set(d.jobs.flatMap(j=>j.categories||[]))]);
  render()}).catch(e=>{if(e)$('#view').innerHTML='<p class=err style="padding:12px">Could not load data.</p>'});
</script></body></html>"""
