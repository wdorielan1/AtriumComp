"""ClinicalTrials.gov (API v2) lookup: which industry sponsors run trials matching
a therapeutic area / drug / location. Used as a decode tool."""

from __future__ import annotations

from collections import Counter
from urllib.parse import urlencode

from . import fetch

API = "https://clinicaltrials.gov/api/v2/studies"


def search_sponsors(condition: str = "", intervention: str = "", location: str = "",
                    phase: str = "", limit: int = 100) -> dict:
    params = {
        "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,ACTIVE_NOT_RECRUITING,ENROLLING_BY_INVITATION",
        "fields": "NCTId,BriefTitle,LeadSponsorName,LeadSponsorClass,Phase,Condition",
        "pageSize": str(min(limit, 200)),
    }
    if condition:
        params["query.cond"] = condition
    if intervention:
        params["query.intr"] = intervention
    if location:
        params["query.locn"] = location
    if phase:
        params["query.term"] = f"AREA[Phase]{phase.upper().replace(' ', '')}"
    data = fetch.get_json(f"{API}?{urlencode(params)}")
    if data is None:
        return {"error": "ClinicalTrials.gov request failed"}

    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for s in data.get("studies", []):
        p = s.get("protocolSection", {})
        sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        if sponsor.get("class") != "INDUSTRY":
            continue
        name = sponsor.get("name", "")
        counts[name] += 1
        ident = p.get("identificationModule", {})
        phases = ",".join(p.get("designModule", {}).get("phases", []) or [])
        if len(examples.setdefault(name, [])) < 2:
            examples[name].append(f"{ident.get('nctId')} [{phases}] {ident.get('briefTitle', '')[:120]}")
    return {
        "studies_scanned": len(data.get("studies", [])),
        "industry_sponsors": [
            {"sponsor": n, "active_trials": c, "examples": examples[n]} for n, c in counts.most_common(25)
        ],
    }
