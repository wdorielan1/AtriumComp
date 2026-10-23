"""Scraper entry points: `scrape(competitor)` and `enrich(job)`."""

from __future__ import annotations

from ..config import Competitor
from ..models import Job
from . import ats, generic
from .parse import parse_rss


def scrape(competitor: Competitor) -> list[Job]:
    kind = (competitor.type or "generic").lower()
    if kind in ats.ADAPTERS:
        return ats.ADAPTERS[kind](competitor.name, competitor.board, competitor.careers_url)
    if kind == "teamtailor":
        board = competitor.board or competitor.careers_url
        url = board if board.startswith("http") else f"https://{board}.teamtailor.com/jobs"
        return generic.scrape_page(competitor, url)
    if kind == "rss":
        from .. import fetch
        r = fetch.get(competitor.careers_url)
        return parse_rss(r.text, competitor.name, competitor.careers_url) if r is not None else []
    return generic.scrape(competitor)


def enrich(job: Job) -> Job:
    if len(job.description) >= 200 and job.location:
        return job
    return generic.enrich(job)


__all__ = ["scrape", "enrich"]
