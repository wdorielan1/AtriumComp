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
    monkeypatch.setattr(notify, "send_digest", lambda jobs, stats, first_run=False: sent.append((jobs, first_run)))
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
    assert calls[0]["fallbacks"] == "default" and calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_decode_refusal_returns_none(monkeypatch):
    resp = SimpleNamespace(stop_reason="refusal", model="m", content=[])
    monkeypatch.setattr(decode, "_client", SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: resp))))
    assert decode.decode_job(Job(competitor="x", title="t", url="u")) is None


def test_decode_backlog_fills_employment_type(monkeypatch, two_competitors):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(notify, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(scrapers, "scrape", lambda c: [Job(competitor=c.name, title="Clinical Research Associate",
                                                           url=f"https://{c.name}/j", description="biotech CRA " * 30)])
    monkeypatch.setattr(decode, "decode_job", lambda job: {"client_guess": "Acme Bio", "confidence": "low",
                                                           "employment_type": "Contract-to-hire"})
    pipeline.run()
    jobs = get_store().matched_jobs()
    assert all(j["decode"]["client_guess"] == "Acme Bio" for j in jobs)
    assert all(j["employment_type"] == "Contract-to-hire" for j in jobs)


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
    assert data["statusCode"] == 200 and json.loads(data["body"]) == {"jobs": [], "runs": []}
    assert "Competitor Job Watch" in dashboard.handler(_req("/", cookies=[cookie]))["body"]


def test_dashboard_refuses_without_configured_password():
    assert dashboard.handler(_req("/api/data", cookies=["jobwatch_session=x"]))["statusCode"] == 401


def test_shipped_config_loads():
    config.load_competitors.cache_clear()
    names = [c.name for c in config.load_competitors()]
    assert "EPM Scientific" in names and "Novaris Search" not in names  # disabled until URL known
    assert len(names) == len(set(names))
