"""Your watchlist: prospects and current clients.

Managed from the dashboard and stored alongside the jobs. A posting is linked to a
watched company when:
  - it was collected from that company's own careers page (`direct` postings), or
  - Claude's decoded client (or an alternative it named) is that company, or
  - the company's name or an alias appears in the posting itself.
"""

from __future__ import annotations

import re
from typing import Iterable

from .config import Competitor
from .models import Job

PROSPECT, CLIENT = "prospect", "client"

_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc", "sa",
             "ag", "gmbh", "nv", "the", "group", "holdings", "pharmaceuticals", "pharmaceutical", "pharma",
             "therapeutics", "biosciences", "bioscience", "biotherapeutics", "biopharma", "biopharmaceuticals",
             "biotech", "biotechnology", "bio", "sciences", "labs", "laboratories", "medical", "us", "usa"}


def normalize(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    core = [w for w in words if w not in _SUFFIXES]
    return " ".join(core or words)


def names_for(item: dict) -> list[str]:
    aliases = item.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",")]
    return [n for n in [item["name"], *aliases] if n]


def _same_company(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 4 and (long_.startswith(short + " ") or long_.endswith(" " + short))


def _mentioned(name: str, text: str) -> bool:
    """Name appears as a whole word. Short names (<= 4 chars, e.g. "BMS") must match case exactly."""
    if len(name) < 3:
        return False
    flags = 0 if len(name) <= 4 else re.I
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, flags))


def link(job: dict | Job, watchlist: Iterable[dict]) -> list[str]:
    """Names of watched companies this posting relates to."""
    j = job.to_dict() if isinstance(job, Job) else job
    if j.get("direct"):
        return [j.get("competitor", "")]
    d = j.get("decode") or {}
    guesses = []
    if d.get("client_guess") and d.get("confidence") != "none":
        guesses.append(d["client_guess"])
    guesses += [a.get("name", "") for a in d.get("alternatives") or []]
    text = f"{j.get('title', '')}\n{j.get('description', '')}"
    out = []
    for item in watchlist:
        names = names_for(item)
        hit = any(_same_company(g, n) for g in guesses for n in names) or any(_mentioned(n, text) for n in names)
        if hit:
            out.append(item["name"])
    return out


def as_competitors(watchlist: Iterable[dict]) -> list[Competitor]:
    """Watched companies with a careers page become extra scrape targets (their own postings)."""
    return [Competitor(name=w["name"], careers_url=w["careers_url"], max_pages=5, extra={"direct": True})
            for w in watchlist if w.get("careers_url")]
