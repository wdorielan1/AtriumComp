"""Keyword matching: life-science categories and employment type."""

from __future__ import annotations

import re
from functools import lru_cache

from .config import load_categories
from .models import Job

TEMP, C2H, DIRECT, UNKNOWN = "Temp / Contract", "Contract-to-hire", "Direct hire", "Unknown"

# Phrases that contain "contract"/"temp" but say nothing about the job's terms.
_NOISE = re.compile(r"contract (research|manufacturing|development|development and manufacturing)"
                    r"( organi[sz]ations?)?|contract lab\w*|temperature\w*|contemp\w*", re.I)

# JSON-LD employmentType / ATS values. FULL_TIME is deliberately weak: staffing
# firms label plenty of full-time-hours contracts FULL_TIME.
_STRUCTURED = {
    "CONTRACTOR": TEMP, "TEMPORARY": TEMP, "CONTRACT": TEMP, "TEMP": TEMP, "PER DIEM": TEMP,
    "INTERN": TEMP, "CONTRACT TO HIRE": C2H, "CONTRACT-TO-HIRE": C2H, "TEMP TO PERM": C2H,
    "PERMANENT": DIRECT, "DIRECT HIRE": DIRECT, "PERM": DIRECT,
}
_WEAK = {"CONTRACT": TEMP, "TEMP": TEMP, "PERMANENT": DIRECT, "FULL TIME": DIRECT}


def _phrase_rx(phrases: list[str]) -> re.Pattern:
    parts = []
    for p in phrases:
        p = str(p).strip()  # YAML turns bare numbers like 1099 into ints
        e = re.escape(p)
        # Allow prefixes like "immunolog" to match "immunology"/"immunologist".
        lead = r"(?<![A-Za-z0-9])" if p[:1].isalnum() else ""
        tail = r"(?![A-Za-z0-9])" if p[-1:].isalnum() and len(p) <= 4 else ""
        parts.append(lead + e + tail)
    return re.compile("|".join(parts), re.I) if parts else re.compile(r"(?!x)x")


@lru_cache
def _rules():
    cfg = load_categories()
    cats = {name: (_phrase_rx(r.get("title", [])), _phrase_rx(r.get("description", [])), bool(r.get("fallback")))
            for name, r in cfg["categories"].items()}
    return {
        "cats": cats,
        "min_desc": int(cfg.get("min_description_hits", 3)),
        "industry": _phrase_rx(cfg.get("industry_signals", [])),
        "exclude": _phrase_rx(cfg.get("exclude_titles", [])),
        "emp": {name: _phrase_rx(words) for name, words in cfg.get("employment_types", {}).items()},
    }


def categorize(job: Job) -> list[str]:
    r = _rules()
    if r["exclude"].search(job.title):
        return []
    text = f"{job.title}\n{job.description}"
    if not r["industry"].search(text):
        return []
    specific, fallback = [], []
    for name, (title_rx, desc_rx, is_fallback) in r["cats"].items():
        hit = bool(title_rx.search(job.title)) or \
            len({m.group(0).lower() for m in desc_rx.finditer(job.description)}) >= r["min_desc"]
        if hit:
            (fallback if is_fallback else specific).append(name)
    return specific or fallback


def employment_type(job: Job) -> str:
    raw = (job.employment_type_raw or "").upper().replace("_", " ").strip()
    if raw in _STRUCTURED:
        return _STRUCTURED[raw]
    if "CONTRACT" in raw and ("HIRE" in raw or "PERM" in raw):
        return C2H

    emp = _rules()["emp"]
    title = _NOISE.sub(" ", job.title)
    body = _NOISE.sub(" ", f"{job.salary}\n{job.description}")
    if C2H in emp and (emp[C2H].search(title) or emp[C2H].search(body)):
        return C2H
    scores = {}
    for name, rx in emp.items():
        if name == C2H:
            continue
        scores[name] = 3 * len(rx.findall(title)) + len(rx.findall(body))
    best = max(scores, key=scores.get, default=None)
    if best and scores[best] > 0 and list(scores.values()).count(scores[best]) == 1:
        return best
    for key, val in _WEAK.items():  # weaker hints like "FULL_TIME, CONTRACTOR"
        if key in raw:
            return val
    return UNKNOWN


def classify(job: Job) -> Job:
    job.categories = categorize(job)
    job.employment_type = employment_type(job)
    return job
