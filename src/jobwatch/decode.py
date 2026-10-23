"""Decode an anonymised staffing-firm posting: which client company is hiring?

Claude reads the posting and can (a) web-search distinctive phrases, since the
client often posts the same role on its own careers page, and (b) query
ClinicalTrials.gov for sponsors matching the therapeutic area and location.
Returns a dict stored on the job as `decode`.
"""

from __future__ import annotations

import json
import logging
import os

import anthropic

from . import trials
from .classify import C2H, DIRECT, TEMP, UNKNOWN
from .config import env, env_bool
from .models import Job

log = logging.getLogger(__name__)

MODEL = env("DECODE_MODEL", "claude-opus-5-5")
EFFORT = env("DECODE_EFFORT", "medium")
WEB_SEARCHES = int(env("DECODE_WEB_SEARCHES", "4"))
MAX_TURNS = 8

SYSTEM = """You are a market-intelligence analyst for a life-sciences recruiting firm.
Competitor staffing agencies post jobs on behalf of client companies (pharma,
biotech, CROs, med-device) but usually hide the client's name. Your job is to work
out which company is most likely behind a posting.

How to investigate:
- Look for identifying details: city/suburb, therapeutic area, modality, named
  drugs or targets, development phase, company size or headcount, funding stage
  ("recently closed a $150M Series C"), number of approved products, recent
  milestones, site names, unusual tools or systems.
- Use web_search on the most distinctive sentence or phrase from the description
  (in quotes), and on title + city + clue combinations. Clients often publish the
  same role on their own careers page, LinkedIn, or Indeed with near-identical
  wording; finding that is the strongest evidence.
- Use search_clinical_trials when the posting names a disease area, drug class,
  or phase, together with the location, to see which industry sponsors fit.
- Prefer a short, decisive investigation. Stop once you have strong evidence.

Be honest about uncertainty. Confidence levels:
- high: you found the client's own posting or an unambiguous identifying detail.
- medium: several independent clues point to the same company.
- low: a plausible guess from one or two weak clues.
- none: nothing points anywhere; set client_guess to "Unknown".
Never invent evidence. Cite URLs you actually saw in source_urls.

Also classify the employment type from the posting: "Temp / Contract",
"Contract-to-hire", "Direct hire", or "Unknown"."""

TRIALS_TOOL = {
    "name": "search_clinical_trials",
    "description": (
        "Search ClinicalTrials.gov for active industry-sponsored trials and return the lead sponsors "
        "ranked by number of matching trials. Use to narrow down which companies work in a therapeutic "
        "area / drug class / phase, optionally near a location. Pass at least one of condition or intervention."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "condition": {"type": "string", "description": "Disease or therapeutic area, e.g. 'IgA nephropathy' or 'NSCLC'. Empty string if none."},
            "intervention": {"type": "string", "description": "Drug, modality, or target, e.g. 'CAR-T', 'KRAS G12C'. Empty string if none."},
            "location": {"type": "string", "description": "City/state, e.g. 'Boston, Massachusetts'. Empty string if none."},
            "phase": {"type": "string", "enum": ["", "PHASE1", "PHASE2", "PHASE3", "PHASE4"]},
        },
        "required": ["condition", "intervention", "location", "phase"],
        "additionalProperties": False,
    },
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "client_guess": {"type": "string", "description": "Most likely hiring company, or 'Unknown'."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Short bullet reasons."},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "why": {"type": "string"}},
                "required": ["name", "why"],
                "additionalProperties": False,
            },
        },
        "client_type": {
            "type": "string",
            "enum": ["Big pharma", "Mid-size pharma", "Biotech (private)", "Biotech (public)", "CRO",
                     "CDMO", "Medical device", "Diagnostics", "Other", "Unknown"],
        },
        "therapeutic_area": {"type": "string"},
        "modality": {"type": "string"},
        "development_stage": {"type": "string"},
        "employment_type": {"type": "string", "enum": [TEMP, C2H, DIRECT, UNKNOWN]},
        "source_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["client_guess", "confidence", "evidence", "alternatives", "client_type",
                 "therapeutic_area", "modality", "development_stage", "employment_type", "source_urls"],
    "additionalProperties": False,
}

_client: anthropic.Anthropic | None = None


def _anthropic() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY") and env("ANTHROPIC_API_KEY_PARAM"):
            import boto3  # available in Lambda
            p = boto3.client("ssm").get_parameter(Name=env("ANTHROPIC_API_KEY_PARAM"), WithDecryption=True)
            os.environ["ANTHROPIC_API_KEY"] = p["Parameter"]["Value"]
        _client = anthropic.Anthropic()
    return _client


def enabled() -> bool:
    return env_bool("DECODE_ENABLED", True) and bool(os.environ.get("ANTHROPIC_API_KEY") or env("ANTHROPIC_API_KEY_PARAM"))


def _prompt(job: Job) -> str:
    return (
        f"Competitor agency: {job.competitor}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location or 'not stated'}\n"
        f"Salary/rate: {job.salary or 'not stated'}\n"
        f"Posted: {job.posted or 'unknown'}\n"
        f"Site's employment type field: {job.employment_type_raw or 'none'}\n"
        f"URL: {job.url}\n\n"
        f"Description:\n{job.description[:15000] or '(no description available)'}"
    )


def _run_tool(name: str, args: dict) -> str:
    if name == "search_clinical_trials":
        return json.dumps(trials.search_sponsors(**args))
    return json.dumps({"error": f"unknown tool {name}"})


def decode_job(job: Job) -> dict | None:
    tools: list[dict] = [TRIALS_TOOL]
    if WEB_SEARCHES > 0:
        tools.insert(0, {"type": "web_search_20260209", "name": "web_search", "max_uses": WEB_SEARCHES})
    messages: list[dict] = [{"role": "user", "content": _prompt(job)}]
    client = _anthropic()

    for _ in range(MAX_TURNS):
        try:
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                messages=messages,
                tools=tools,
                output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as e:
            log.warning("decode rate-limited for %s: %s", job.url, e)
            return None
        except anthropic.APIStatusError as e:
            log.error("decode API error %s for %s: %s", e.status_code, job.url, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("decode connection error for %s: %s", job.url, e)
            return None

        if resp.stop_reason == "refusal":
            log.warning("decode refused for %s", job.url)
            return None
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "pause_turn":
            continue  # server-side web search loop paused; resend to resume
        if resp.stop_reason == "tool_use":
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        out, err = _run_tool(block.name, dict(block.input)), False
                    except Exception as e:  # tool failures go back to Claude, not up the stack
                        out, err = json.dumps({"error": str(e)}), True
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": err})
            messages.append({"role": "user", "content": results})
            continue

        text = next((b.text for b in reversed(resp.content) if b.type == "text"), "")
        try:
            result = json.loads(text)
        except ValueError:
            log.error("decode returned non-JSON for %s (stop_reason=%s)", job.url, resp.stop_reason)
            return None
        result["model"] = resp.model
        return result

    log.warning("decode hit turn limit for %s", job.url)
    return None
