"""Adapters for job-board vendors that expose public JSON feeds.

Each adapter takes (competitor_name, board, page_url) and returns a list of Jobs.
`detect()` finds a vendor and board id inside any careers-page HTML, which is how
most staffing sites embed these boards.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import fetch
from ..models import Job
from .parse import clean, html_to_text


def greenhouse(competitor: str, board: str, page_url: str = "") -> list[Job]:
    data = fetch.get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true") or {}
    return [
        Job(competitor=competitor, title=clean(j.get("title")), url=j.get("absolute_url", ""),
            location=clean((j.get("location") or {}).get("name")), description=html_to_text(j.get("content")),
            posted=j.get("updated_at", ""), ref=str(j.get("id", "")), source="greenhouse")
        for j in data.get("jobs", [])
    ]


def lever(competitor: str, board: str, page_url: str = "") -> list[Job]:
    data = fetch.get_json(f"https://api.lever.co/v0/postings/{board}?mode=json") or []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        posted = datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else ""
        jobs.append(Job(competitor=competitor, title=clean(j.get("text")), url=j.get("hostedUrl", ""),
                        location=clean(cats.get("location")), description=clean(j.get("descriptionPlain"))[:20000],
                        employment_type_raw=clean(cats.get("commitment")), posted=posted,
                        ref=j.get("id", ""), source="lever"))
    return jobs


def workable(competitor: str, board: str, page_url: str = "") -> list[Job]:
    data = fetch.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{board}") or {}
    return [
        Job(competitor=competitor, title=clean(j.get("title")), url=j.get("url") or j.get("shortlink", ""),
            location=", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")])),
            employment_type_raw=clean(j.get("employment_type")), posted=j.get("published_on", ""),
            ref=j.get("shortcode", ""), source="workable")
        for j in data.get("jobs", [])
    ]


def smartrecruiters(competitor: str, board: str, page_url: str = "") -> list[Job]:
    jobs, offset = [], 0
    while offset < 1000:
        data = fetch.get_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset={offset}")
        if not data or not data.get("content"):
            break
        for j in data["content"]:
            loc = j.get("location") or {}
            jobs.append(Job(competitor=competitor, title=clean(j.get("name")),
                            url=f"https://jobs.smartrecruiters.com/{board}/{j.get('id')}",
                            location=", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")])),
                            employment_type_raw=clean((j.get("typeOfEmployment") or {}).get("label")),
                            posted=j.get("releasedDate", ""), ref=str(j.get("id", "")), source="smartrecruiters"))
        offset += 100
        if offset >= data.get("totalFound", 0):
            break
    return jobs


def bullhorn(competitor: str, board: str, page_url: str = "") -> list[Job]:
    """Bullhorn open-source career portal. board = "<swimlane>:<corpToken>"."""
    swimlane, corp = board.split(":", 1)
    base = f"https://public-rest{swimlane}.bullhornstaffing.com/rest-services/{corp}"
    fields = "id,title,address,employmentType,dateLastPublished,publicDescription,publishedCategory"
    jobs, start = [], 0
    portal = page_url.split("#")[0].rstrip("/")
    while start < 2000:
        data = fetch.get_json(f"{base}/search/JobOrder?query=(isOpen:1)&fields={fields}"
                              f"&count=200&start={start}&sort=-dateLastPublished")
        rows = (data or {}).get("data") or []
        for j in rows:
            a = j.get("address") or {}
            ts = j.get("dateLastPublished")
            posted = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else ""
            jobs.append(Job(competitor=competitor, title=clean(j.get("title")),
                            url=f"{portal}/#/jobs/{j.get('id')}" if portal else str(j.get("id")),
                            location=", ".join(filter(None, [a.get("city"), a.get("state")])),
                            description=html_to_text(j.get("publicDescription")),
                            employment_type_raw=clean(j.get("employmentType")), posted=posted,
                            ref=str(j.get("id", "")), source="bullhorn"))
        start += 200
        if len(rows) < 200:
            break
    return jobs


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
    "bullhorn": bullhorn,
}

_SIGNATURES = [
    ("greenhouse", re.compile(r"boards(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("bullhorn", re.compile(r"public-rest(\d+)\.bullhornstaffing\.com(?::443)?/rest-services/([A-Za-z0-9]+)")),
]
_IGNORE_BOARDS = {"embed", "js", "v1", "api", "static", "assets", "widget"}


def detect(markup: str) -> tuple[str, str] | None:
    for name, rx in _SIGNATURES:
        for m in rx.finditer(markup):
            board = ":".join(m.groups())
            if board.lower() not in _IGNORE_BOARDS:
                return name, board
    return None
