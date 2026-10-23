"""Generic careers-page scraper.

Works on most staffing-firm sites without per-site code, trying in order:
  1. an embedded job-board vendor (Greenhouse, Lever, Bullhorn, ...) -> its JSON API
  2. schema.org JobPosting JSON-LD on the listing page
  3. an RSS/Atom job feed linked from the page
  4. links that look like job-detail pages
and following "next page" links up to `max_pages`.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from .. import fetch
from ..config import Competitor
from ..models import Job
from . import ats
from .parse import clean, job_from_jsonld, jsonld_postings, main_text, parse_rss, soup

log = logging.getLogger(__name__)

CAREERS_LINK = re.compile(r"\b(jobs?|careers?|opportunit(y|ies)|openings|positions|vacancies|"
                          r"job seekers?|find (a )?job|search jobs|browse jobs|current openings)\b", re.I)
CAREERS_HREF = re.compile(r"/(jobs?|careers?|job-search|search-jobs|find-a-job|opportunities|openings|"
                          r"positions|vacancies|job-board)(/|$|\?)", re.I)
# Singular "/job/..." detail links are the strongest signal on most staffing sites.
DETAIL_HREF_STRONG = re.compile(r"/(job|job-details?|jobdetails?|jobs/view|job-posting|position|vacancy|requisition)"
                                r"/[^?#]*[A-Za-z0-9-]{4,}", re.I)
DETAIL_HREF_WEAK = re.compile(r"/(jobs|positions|openings|vacancies|careers|opportunities)/[^?#/]+[A-Za-z0-9-]{6,}"
                              r"|[?&](job_?id|jobid|jid|reqid|req|posting_?id|id)=[\w-]+", re.I)
NAV_TEXT = re.compile(r"^(view|see|search|browse|all|apply|read more|learn more|more|details|jobs?|careers?|"
                      r"next|previous|prev|login|sign in|register|submit|upload|home|contact|about|share|save)\b", re.I)
NEXT_TEXT = re.compile(r"^(next|next page|›|»|>|older)$", re.I)
TEAMTAILOR = re.compile(r"https?://([A-Za-z0-9-]+)\.teamtailor\.com")


def _same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    root = lambda h: ".".join(h.split(".")[-2:])
    return root(ha) == root(hb)


def discover_careers_url(homepage: str) -> str | None:
    r = fetch.get(homepage)
    if r is None:
        return None
    best, best_score = None, 0
    for a in soup(r.text).find_all("a", href=True):
        href, text = urljoin(r.url, a["href"]), clean(a.get_text())
        score = 0
        if CAREERS_HREF.search(urlparse(href).path + ("?" + urlparse(href).query if urlparse(href).query else "")):
            score += 2
        if CAREERS_LINK.search(text):
            score += 2
        if re.search(r"\b(search|find|browse|current|open)\b", text, re.I):
            score += 1
        if re.search(r"\b(join our team|work (for|with) us|internal|our careers)\b", text, re.I):
            score -= 3  # the firm's own internal hiring page
        if not _same_site(href, homepage) and not ats.detect(href) and "teamtailor" not in href:
            score -= 1
        if score > best_score:
            best, best_score = href, score
    return best if best_score >= 2 else None


def _job_links(markup: str, page_url: str, competitor: str) -> list[Job]:
    strong, weak = {}, {}
    for a in soup(markup).find_all("a", href=True):
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not href.startswith("http"):
            continue
        text = clean(a.get_text(" "))
        if not text:
            heading = a.find(["h2", "h3", "h4"]) or a.find_parent(["h2", "h3", "h4"])
            text = clean(heading.get_text(" ")) if heading else ""
        if not text or NAV_TEXT.match(text) or not (2 <= len(text.split()) <= 16) or len(text) > 140:
            continue
        if re.search(r"\bjobs\b", text, re.I) and not re.search(r"\b(manager|director|scientist|associate)\b", text, re.I):
            continue  # "Market Access Jobs" style filter links
        if not (_same_site(href, page_url) or ats.detect(href) or TEAMTAILOR.match(href)):
            continue
        path = urlparse(href).path + ("?" + urlparse(href).query if urlparse(href).query else "")
        if DETAIL_HREF_STRONG.search(path):
            strong.setdefault(href, text)
        elif DETAIL_HREF_WEAK.search(path):
            weak.setdefault(href, text)
    links = strong if len(strong) >= 2 else {**weak, **strong}
    return [Job(competitor=competitor, title=t, url=u, source="links") for u, t in links.items()]


def _next_page(markup: str, page_url: str, page_no: int) -> str | None:
    s = soup(markup)
    link = s.find("link", rel="next") or s.find("a", rel="next")
    if link and link.get("href"):
        return urljoin(page_url, link["href"])
    for a in s.find_all("a", href=True):
        label = clean(a.get_text()) or a.get("aria-label", "")
        if NEXT_TEXT.match(label) or re.fullmatch(r"next( page)?", a.get("aria-label", ""), re.I):
            return urljoin(page_url, a["href"])
    for a in s.find_all("a", href=True):
        if re.search(rf"[?&/](page|p|pg)[=/]{page_no + 1}\b", a["href"]):
            return urljoin(page_url, a["href"])
    return None


def _bullhorn_portal(markup: str, page_url: str) -> str | None:
    """Bullhorn's open-source career portal keeps its API settings in app.json."""
    if "career-portal" not in markup and "bullhorn" not in markup.lower():
        return None
    cfg = fetch.get_json(urljoin(page_url.split("#")[0].rstrip("/") + "/", "app.json")) or {}
    svc = cfg.get("service") or {}
    if svc.get("corpToken") and svc.get("swimlane"):
        return f"{svc['swimlane']}:{svc['corpToken']}"
    return None


def scrape_page(competitor: Competitor, start_url: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    url, seen = start_url, set()
    for page_no in range(1, max(1, competitor.max_pages) + 1):
        if not url or url in seen:
            break
        seen.add(url)
        r = fetch.get(url)
        if r is None:
            break
        markup = r.text

        if page_no == 1:
            found = ats.detect(markup)
            bh = None if found else _bullhorn_portal(markup, r.url)
            if bh:
                found = ("bullhorn", bh)
            if found:
                vendor, board = found
                log.info("%s: %s board %s", competitor.name, vendor, board)
                vendor_jobs = ats.ADAPTERS[vendor](competitor.name, board, r.url)
                if vendor_jobs:
                    return vendor_jobs
            tt = TEAMTAILOR.search(markup)
            if tt and "teamtailor.com" not in urlparse(r.url).netloc:
                return scrape_page(competitor, f"https://{tt.group(1)}.teamtailor.com/jobs")

        page_jobs = [j for d in jsonld_postings(markup) if (j := job_from_jsonld(d, competitor.name, r.url))]

        if not page_jobs and page_no == 1:
            feed = soup(markup).find("link", type=re.compile(r"(rss|atom)\+xml"))
            if feed and feed.get("href") and re.search(r"job|career|vacanc", feed["href"] + feed.get("title", ""), re.I):
                fr = fetch.get(urljoin(r.url, feed["href"]))
                if fr is not None:
                    page_jobs = parse_rss(fr.text, competitor.name, r.url)
                    if page_jobs:
                        return page_jobs

        if not page_jobs:
            page_jobs = _job_links(markup, r.url, competitor.name)

        new = 0
        for j in page_jobs:
            if j.url not in jobs:
                jobs[j.url] = j
                new += 1
        if new == 0:
            break
        url = _next_page(markup, r.url, page_no)
    return list(jobs.values())


def scrape(competitor: Competitor) -> list[Job]:
    tried = []
    if competitor.careers_url:
        tried.append(competitor.careers_url)
        jobs = scrape_page(competitor, competitor.careers_url)
        if jobs:
            return jobs
    if competitor.homepage:
        found = discover_careers_url(competitor.homepage)
        if found and found not in tried:
            log.info("%s: discovered careers page %s", competitor.name, found)
            jobs = scrape_page(competitor, found)
            if jobs:
                return jobs
    return []


def enrich(job: Job) -> Job:
    """Fetch the detail page to fill in description/location/type/date."""
    r = fetch.get(job.url)
    if r is None:
        return job
    for d in jsonld_postings(r.text):
        full = job_from_jsonld(d, job.competitor, r.url)
        if full:
            job.description = full.description or job.description
            job.location = job.location or full.location
            job.posted = job.posted or full.posted
            job.salary = job.salary or full.salary
            job.employment_type_raw = job.employment_type_raw or full.employment_type_raw
            job.ref = job.ref or full.ref
            break
    if len(job.description) < 200:
        text = main_text(r.text)
        if len(text) > len(job.description):
            job.description = text
    return job
