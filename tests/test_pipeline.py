import json
from types import SimpleNamespace

import pytest

from jobwatch import config, dashboard, decode, notify, pipeline, scrapers, trials
from jobwatch.config import Competitor
from jobwatch.models import Job
from jobwatch.store import get_store


def _jobs(name, extra=()):
    base = [
        Job(competitor=name, title="Clinical Trial Manager", url=f"https://{name}/job/1",
            description="Contract role, W2 hourly. Oncology biotech running phase II trials with a CRO. " * 5,
            location="Boston, MA"),
        Job(competitor=name, title="Medical Science Liaison", url=f"https://{name}/job/2",
            description="Permanent role, base salary + bonus, pharma field medical team. " * 5),
        Job(competitor=name, title="Java Developer", url=f"https://{name}/job/3", description="Spring " * 60),
        Job(competitor=name, title="Recruitment Consultant", url=f"https://{name}/job/4"),
    ]
    return base + list(extra)


@pytest.fixture
def two_competitors(monkeypatch):
    comps = [Competitor(name="alpha", careers_url="https://alpha/jobs"),
             Competitor(name="beta", careers_url="https://beta/jobs")]
    monkeypatch.setattr(pipeline, "load_competitors", lambda: comps)
    return comps


def test_run_twice_only_reports_new(monkeypatch, two_competitors):
    sent = []
    monkeypatch.setattr(notify, "send_digest", lambda jobs, stats, first_run=False, watchlist=None: sent.append((jobs, first_run)))
    monkeypatch.setattr(scrapers, "scrape", lambda c: _jobs(c.name))
    monkeypatch.setattr(scrapers, "enrich", lambda j: j)

    s1 = pipeline.run()
    assert s1["new_matched"] == 4 and sent[0][1] is True
    assert {j.employment_type for j in sent[0][0]} == {"Temp / Contract", "Direct hire"}

    extra = [Job(competitor="alpha", title="Drug Safety Associate", url="https://alpha/job/9",
                 description="pharmacovigilance case processing for biotech " * 6)]
    monkeypatch.setattr(scrapers, "scrape", lambda c: _jobs(c.name, extra if c.name == "alpha" else ()))
    s2 = pipeline.run()
    assert s2["new_matched"] == 1 and [j.title for j in sent[1][0]] == ["Drug Safety Associate"]
    assert sent[1][1] is False

    store = get_store()
    assert len(store.matched_jobs()) == 5
    assert {r["competitor"] for r in store.runs()} == {"alpha", "beta"}


def test_broken_site_does_not_stop_run(monkeypatch, two_competitors):
    def scrape(c):
        if c.name == "alpha":
            raise RuntimeError("layout changed")
        return _jobs(c.name)
    monkeypatch.setattr(scrapers, "scrape", scrape)
    monkeypatch.setattr(notify, "send_digest", lambda *a, **k: None)
    s = pipeline.run()
    assert s["errors"] == ["alpha"] and s["new_matched"] == 2


def test_detail_limit_defers_rest(monkeypatch, two_competitors):
    monkeypatch.setattr(pipeline, "DETAIL_LIMIT", 1)
    monkeypatch.setattr(notify, "send_digest", lambda *a, **k: None)
    many = [Job(competitor="alpha", title=f"Clinical Research Associate {i}", url=f"https://alpha/job/c{i}")
            for i in range(3)]
    monkeypatch.setattr(scrapers, "scrape", lambda c: many if c.name == "alpha" else [])
    enriched = []
    monkeypatch.setattr(scrapers, "enrich", lambda j: enriched.append(j.url) or j)
    pipeline.run()
    run = {r["competitor"]: r for r in get_store().runs()}["alpha"]
    assert len(enriched) == 1 and run["deferred"] == 2
    pipeline.run()  # next run picks up one more
    assert len(enriched) == 2


def test_digest_renders_groups():
    jobs = [Job(competitor="a", title="CRA", url="https://a/1", employment_type="Temp / Contract", categories=["Clinical"],
                decode={"client_guess": "Moderna", "confidence": "medium", "evidence": ["mRNA, Cambridge"]}),
            Job(competitor="b", title="MSL <x>", url="https://b/2", employment_type="Direct hire", categories=["Medical Affairs"])]
    subject, body = notify.render_digest(jobs, [{"competitor": "c", "found": 0, "error": ""}])
    assert "1 Direct hire, 1 Temp / Contract" in subject
    assert body.index("Direct hire (1)") < body.index("Temp / Contract (1)")
    assert "Moderna" in body and "MSL &lt;x&gt;" in body and "No jobs could be read from: c" in body


# --- decode -----------------------------------------------------------------

def _block(**kw):
    return SimpleNamespace(**kw)


def test_decode_runs_trials_tool_then_returns_json(monkeypatch):
    final = {"client_guess": "Vertex Pharmaceuticals", "confidence": "medium", "evidence": ["Boston CF program"],
             "alternatives": [], "client_type": "Mid-size pharma", "therapeutic_area": "Cystic fibrosis",
             "modality": "Small molecule", "development_stage": "Commercial", "employment_type": "Direct hire",
             "source_urls": []}
    responses = [
        SimpleNamespace(stop_reason="tool_use", model="claude-opus-5-5", content=[
            _block(type="tool_use", id="t1", name="search_clinical_trials",
                   input={"condition": "cystic fibrosis", "intervention": "", "location": "Boston", "phase": ""})]),
        SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5", content=[_block(type="text", text=json.dumps(final))]),
    ]
    calls = []

    def create(**kw):
        calls.append(kw)
        return responses[len(calls) - 1]

    monkeypatch.setattr(decode, "_client", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    monkeypatch.setattr(trials, "search_sponsors", lambda **kw: {"industry_sponsors": [{"sponsor": "Vertex", "active_trials": 4}]})

    out = decode.decode_job(Job(competitor="x", title="Medical Director", url="u", description="CF"))
    assert out["client_guess"] == "Vertex Pharmaceuticals" and out["model"] == "claude-opus-5-5"
    tool_turn = calls[1]["messages"][2]  # user, assistant(tool_use), user(tool_result)
    assert tool_turn["content"][0]["type"] == "tool_result" and "Vertex" in tool_turn["content"][0]["content"]
    assert calls[0]["model"] == "claude-haiku-5-5"  # cheapest model is the default
    assert "fallbacks" not in calls[0]  # Haiku 5.5 has no server-side fallback
    assert calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert "Today's date:" in calls[0]["messages"][0]["content"]


def test_decode_sends_fallbacks_only_to_models_that_have_them(monkeypatch):
    monkeypatch.setattr(decode, "MODEL", "claude-sonnet-5-5")
    calls = []
    done = SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5-5",
                           content=[_block(type="text", text='{"client_guess": "X"}')])
    monkeypatch.setattr(decode, "_client", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: calls.append(kw) or done))))
    decode.decode_job(Job(competitor="x", title="t", url="u"))
    assert calls[0]["fallbacks"] == "default" and calls[0]["betas"] == ["server-side-fallback-2026-07-01"]


def test_decode_refusal_returns_none(monkeypatch):
    resp = SimpleNamespace(stop_reason="refusal", model="m", content=[])
    monkeypatch.setattr(decode, "_client", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))))
    assert decode.decode_job(Job(competitor="x", title="t", url="u")) is None


def test_only_auto_decode_postings_are_decoded(monkeypatch, two_competitors):
    """Default rule: Direct hire + (Clinical or Medical Affairs). Everything else waits for the Decode button."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(notify, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(scrapers, "scrape", lambda c: _jobs(c.name, [
        Job(competitor=c.name, title="Clinical Research Associate", url=f"https://{c.name}/j",
            description="Direct hire, permanent. Biotech CRA. " * 10)]) if c.name == "alpha" else [])
    decoded = []

    def fake(job):
        decoded.append(job.title)
        return {"client_guess": "Acme Bio", "confidence": "low", "employment_type": "Direct hire"}
    monkeypatch.setattr(decode, "decode_job", fake)
    pipeline.run()
    # CRA (direct, clinical) and MSL (direct, medical affairs) qualify; the contract CTM does not.
    assert sorted(decoded) == ["Clinical Research Associate", "Medical Science Liaison"]
    by_title = {j["title"]: j for j in get_store().matched_jobs()}
    assert by_title["Clinical Trial Manager"].get("decode") is None


def test_decode_fills_unknown_employment_type(monkeypatch):
    monkeypatch.setattr(decode, "decode_job", lambda job: {"client_guess": "X", "employment_type": "Contract-to-hire"})
    job = Job(competitor="a", title="CRA", url="u")
    decode.decode_and_apply(job)
    assert job.employment_type == "Contract-to-hire" and job.decode["client_guess"] == "X"


# --- dashboard ----------------------------------------------------------------

def _req(path, method="GET", cookies=None, body=""):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}, "cookies": cookies or [], "body": body}


def test_dashboard_requires_password(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    assert "password" in dashboard.handler(_req("/"))["body"].lower()
    assert dashboard.handler(_req("/api/data"))["statusCode"] == 401
    assert dashboard.handler(_req("/login", "POST", body="password=nope"))["statusCode"] == 401

    ok = dashboard.handler(_req("/login", "POST", body="password=s3cret"))
    assert ok["statusCode"] == 302
    cookie = ok["cookies"][0].split(";")[0]
    data = dashboard.handler(_req("/api/data", cookies=[cookie]))
    assert data["statusCode"] == 200 and json.loads(data["body"]) == {"jobs": [], "runs": [], "watchlist": []}
    assert "Competitor Job Watch" in dashboard.handler(_req("/", cookies=[cookie]))["body"]


def _login(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    ok = dashboard.handler(_req("/login", "POST", body="password=pw"))
    return [ok["cookies"][0].split(";")[0]]


def _post_json(path, payload, cookies, ctype="application/json"):
    e = _req(path, "POST", cookies=cookies, body=json.dumps(payload))
    e["headers"] = {"Content-Type": ctype}
    return dashboard.handler(e)


def test_dashboard_decode_button(monkeypatch):
    cookies = _login(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    job = Job(competitor="a", title="QA Specialist", url="https://a/1", categories=["Regulatory / Quality / PV"],
              employment_type="Temp / Contract", description="GMP", first_seen="2099-01-01")
    get_store().save(job, matched=True)
    calls = []
    monkeypatch.setattr(decode, "decode_job", lambda j: calls.append(j.description) or
                        {"client_guess": "Moderna", "confidence": "medium", "employment_type": "Temp / Contract"})

    listed = json.loads(dashboard.handler(_req("/api/data", cookies=cookies))["body"])["jobs"][0]
    assert listed["auto_decode"] is False and "description" not in listed

    assert _post_json("/api/decode", {"job_id": job.job_id}, cookies, ctype="text/plain")["statusCode"] == 415
    r = _post_json("/api/decode", {"job_id": job.job_id}, cookies)
    assert r["statusCode"] == 200 and json.loads(r["body"])["decode"]["client_guess"] == "Moderna"
    assert calls == ["GMP"]  # full description was sent to Claude
    _post_json("/api/decode", {"job_id": job.job_id}, cookies)
    assert len(calls) == 1  # second click reuses the saved result
    assert _post_json("/api/decode", {"job_id": "nope"}, cookies)["statusCode"] == 404


def test_feedback_and_redecode(monkeypatch):
    cookies = _login(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    job = Job(competitor="a", title="CRA", url="https://a/9", categories=["Clinical"], first_seen="2099-01-01",
              decode={"client_guess": "Acme", "confidence": "low", "model": "claude-haiku-5-5"})
    get_store().save(job, matched=True)

    r = _post_json("/api/feedback", {"job_id": job.job_id, "verdict": "wrong"}, cookies)
    assert json.loads(r["body"])["decode"]["feedback"] == "wrong"
    assert _post_json("/api/feedback", {"job_id": job.job_id, "verdict": "maybe"}, cookies)["statusCode"] == 400

    monkeypatch.setattr(decode, "decode_job", lambda j: {"client_guess": "Moderna", "confidence": "high",
                                                         "model": "claude-sonnet-5-5"})
    plain = json.loads(_post_json("/api/decode", {"job_id": job.job_id}, cookies)["body"])
    assert plain["decode"]["client_guess"] == "Acme"  # no force: existing answer kept, nothing spent
    forced = json.loads(_post_json("/api/decode", {"job_id": job.job_id, "force": True}, cookies)["body"])
    assert forced["decode"]["client_guess"] == "Moderna"
    assert forced["decode"]["previous"] == {"client_guess": "Acme", "confidence": "low",
                                            "model": "claude-haiku-5-5", "feedback": "wrong"}


def test_dashboard_refuses_without_configured_password():
    assert dashboard.handler(_req("/api/data", cookies=["jobwatch_session=x"]))["statusCode"] == 401


def test_shipped_config_loads():
    config.load_competitors.cache_clear()
    names = [c.name for c in config.load_competitors()]
    assert "EPM Scientific" in names and "Novaris Search" in names
    assert len(names) == len(set(names))


# --- prospects & clients watchlist ---------------------------------------------

from jobwatch import watch  # noqa: E402


def test_watch_link_matching():
    wl = [{"name": "Vertex", "status": "prospect"}, {"name": "Bristol Myers Squibb", "aliases": ["BMS"], "status": "client"},
          {"name": "Ionis", "status": "prospect"}]
    decoded = {"decode": {"client_guess": "Vertex Pharmaceuticals Inc.", "confidence": "medium",
                          "alternatives": [{"name": "Bristol-Myers Squibb Company", "why": "x"}]}}
    assert watch.link(decoded, wl) == ["Vertex", "Bristol Myers Squibb"]
    assert watch.link({"description": "Partnering with BMS on a cell therapy program"}, wl) == ["Bristol Myers Squibb"]
    assert watch.link({"decode": {"client_guess": "Ionis", "confidence": "none"}}, wl) == []  # no-confidence ignored
    assert watch.link({"description": "vertices of a graph"}, wl) == []
    assert watch.link({"direct": True, "competitor": "Ionis"}, wl) == ["Ionis"]


def test_watchlist_api_and_direct_scraping(monkeypatch, two_competitors):
    cookies = _login(monkeypatch)
    r = _post_json("/api/watchlist", {"name": "Ionis", "status": "prospect", "careers_url": "https://ionis.com/careers",
                                      "aliases": "Ionis Pharmaceuticals, Akcea"}, cookies)
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["watchlist"][0]["aliases"] == ["Ionis Pharmaceuticals", "Akcea"]
    assert _post_json("/api/watchlist", {"name": "", "status": "prospect"}, cookies)["statusCode"] == 400
    assert _post_json("/api/watchlist", {"name": "X", "status": "prospect", "careers_url": "javascript:alert(1)"},
                      cookies)["statusCode"] == 400
    _post_json("/api/watchlist", {"name": "Moderna", "status": "client"}, cookies)

    def scrape(c):
        if c.name == "Ionis":  # the prospect's own careers page
            return [Job(competitor="Ionis", title="Clinical Trial Manager", url="https://ionis.com/j/1",
                        description="Direct hire. Antisense oligonucleotide phase 3 clinical trial management. " * 4)]
        if c.name == "alpha":
            return [Job(competitor="alpha", title="Medical Science Liaison", url="https://alpha/j/2",
                        description="Our client Moderna seeks a permanent MSL, base salary + bonus, pharma. " * 4)]
        return []
    monkeypatch.setattr(scrapers, "scrape", scrape)
    sent = []
    monkeypatch.setattr(notify, "send_digest", lambda jobs, stats, first_run=False, watchlist=None:
                        sent.append(notify.render_digest(jobs, stats, first_run, watchlist)))
    pipeline.run()

    data = json.loads(dashboard.handler(_req("/api/data", cookies=cookies))["body"])
    by_title = {j["title"]: j for j in data["jobs"]}
    assert by_title["Clinical Trial Manager"]["direct"] is True and by_title["Clinical Trial Manager"]["watch"] == ["Ionis"]
    assert by_title["Clinical Trial Manager"]["auto_decode"] is False  # client already known, never decoded
    assert by_title["Medical Science Liaison"]["watch"] == ["Moderna"]
    subject, body = sent[0]
    assert subject.startswith("[JobWatch] 2 at your prospects/clients")
    assert "Their own careers site" in body and "Current client" in body
    assert "1 new matching competitor postings" in body  # Ionis' own posting isn't counted as a competitor posting

    _post_json("/api/watchlist/delete", {"name": "Ionis"}, cookies)
    assert [w["name"] for w in get_store().watchlist()] == ["Moderna"]


def test_dashboard_script_is_valid_js(tmp_path):
    """Catches quoting slips in the inline script (Python string escapes)."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = tmp_path / "d.js"
    js.write_text(dashboard.DASHBOARD_HTML.split("<script>")[1].split("</script>")[0])
    assert subprocess.run([node, "--check", str(js)], capture_output=True).returncode == 0
