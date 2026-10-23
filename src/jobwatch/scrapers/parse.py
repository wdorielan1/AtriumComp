"""HTML helpers: JSON-LD JobPosting extraction, text cleanup, RSS parsing."""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Job

WS = re.compile(r"\s+")


def soup(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


def clean(text: str | None) -> str:
    return WS.sub(" ", html.unescape(text or "")).strip()


def html_to_text(markup: str | None, limit: int = 20000) -> str:
    if not markup:
        return ""
    s = soup(html.unescape(markup) if "&lt;" in markup else markup)
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    text = s.get_text("\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:limit]


def main_text(markup: str, limit: int = 20000) -> str:
    """Best-effort main content of a job detail page."""
    s = soup(markup)
    for t in s(["script", "style", "noscript", "nav", "header", "footer", "form", "aside", "svg"]):
        t.decompose()
    candidates = s.select("main, article, [class*=job-description], [class*=jobDescription], "
                          "[class*=job-detail], [class*=job_description], [id*=job], [class*=description]")
    best = max(candidates, key=lambda t: len(t.get_text(" ", strip=True)), default=None)
    node = best if best is not None and len(best.get_text(strip=True)) > 300 else (s.body or s)
    text = node.get_text("\n")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:limit]


def _walk_jsonld(obj: Any) -> Iterable[dict]:
    if isinstance(obj, list):
        for o in obj:
            yield from _walk_jsonld(o)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types:
            yield obj
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in obj:
                yield from _walk_jsonld(obj[key])


def jsonld_postings(markup: str) -> list[dict]:
    out = []
    for tag in soup(markup).find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except ValueError:
            # Some sites put several objects or trailing commas in one tag.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()))
            except ValueError:
                continue
        out.extend(_walk_jsonld(data))
    return out


def _location(loc: Any) -> str:
    if isinstance(loc, list):
        return "; ".join(filter(None, (_location(l) for l in loc)))
    if isinstance(loc, dict):
        addr = loc.get("address", loc)
        if isinstance(addr, str):
            return clean(addr)
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            return ", ".join(clean(p) for p in parts if p)
        return clean(loc.get("name"))
    return clean(str(loc)) if loc else ""


def _salary(sal: Any) -> str:
    if not isinstance(sal, dict):
        return clean(str(sal)) if sal else ""
    cur = sal.get("currency", "")
    val = sal.get("value", sal)
    if isinstance(val, dict):
        lo, hi, unit = val.get("minValue"), val.get("maxValue"), val.get("unitText", "")
        amt = val.get("value")
        rng = f"{lo}-{hi}" if lo and hi else str(amt or lo or hi or "")
        return clean(f"{cur} {rng} {unit}".strip()) if rng else ""
    return clean(f"{cur} {val}")


def job_from_jsonld(d: dict, competitor: str, base_url: str, source: str = "jsonld") -> Job | None:
    title = clean(d.get("title") or d.get("name"))
    if not title:
        return None
    url = d.get("url") or d.get("sameAs") or base_url
    ident = d.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    emp = d.get("employmentType", "")
    if isinstance(emp, list):
        emp = ", ".join(emp)
    loc = _location(d.get("jobLocation"))
    if not loc and str(d.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        loc = "Remote"
    return Job(
        competitor=competitor,
        title=title,
        url=urljoin(base_url, url),
        location=loc,
        description=html_to_text(d.get("description")),
        posted=clean(str(d.get("datePosted", ""))),
        salary=_salary(d.get("baseSalary")),
        employment_type_raw=clean(str(emp)),
        ref=clean(str(ident or "")),
        source=source,
    )


def parse_rss(xml_text: str, competitor: str, base_url: str) -> list[Job]:
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    jobs = []
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.iter("item")
    for it in items:
        title = clean(it.findtext("title"))
        link = clean(it.findtext("link"))
        if title and link:
            jobs.append(Job(competitor=competitor, title=title, url=urljoin(base_url, link),
                            description=html_to_text(it.findtext("description")),
                            posted=clean(it.findtext("pubDate")), ref=clean(it.findtext("guid")),
                            source="rss"))
    for entry in root.iter(f"{atom}entry"):
        title = clean(entry.findtext(f"{atom}title"))
        link_el = entry.find(f"{atom}link")
        link = link_el.get("href") if link_el is not None else ""
        if title and link:
            jobs.append(Job(competitor=competitor, title=title, url=urljoin(base_url, link),
                            description=html_to_text(entry.findtext(f"{atom}content") or entry.findtext(f"{atom}summary")),
                            posted=clean(entry.findtext(f"{atom}updated")), ref=clean(entry.findtext(f"{atom}id")),
                            source="rss"))
    return jobs
