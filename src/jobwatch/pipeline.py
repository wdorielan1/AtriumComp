"""One hourly run: scrape every competitor -> classify -> decode -> store -> email."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import classify, decode, notify, scrapers
from .config import Competitor, env, load_competitors
from .models import Job
from .store import get_store

log = logging.getLogger(__name__)

DETAIL_LIMIT = int(env("DETAIL_LIMIT", "60"))  # detail pages fetched per competitor per run
DECODE_LIMIT = int(env("DECODE_LIMIT", "25"))  # Claude decodes per run (backlog drains over runs)
SCRAPE_WORKERS = int(env("SCRAPE_WORKERS", "8"))
DECODE_WORKERS = int(env("DECODE_WORKERS", "4"))

# Titles worth opening even when the category keywords don't hit the title.
_AMBIGUOUS_TITLE = re.compile(r"\b(director|manager|associate|specialist|lead|head|vp|vice president|"
                              r"principal|senior|sr\.?|coordinator|analyst|consultant|officer|physician|"
                              r"nurse|pharmacist|engineer)\b", re.I)


def _worth_enriching(job: Job) -> bool:
    r = classify._rules()
    if r["exclude"].search(job.title):
        return False
    if any(t.search(job.title) for t, _, _ in r["cats"].values()) or r["industry"].search(job.title):
        return True
    return bool(_AMBIGUOUS_TITLE.search(job.title))


def _process_competitor(c: Competitor, known: set[str], matched: set[str], store, now: str) -> tuple[list[Job], dict]:
    t0 = time.monotonic()
    stats = {"competitor": c.name, "ran_at": now, "found": 0, "new": 0, "new_matched": 0,
             "deferred": 0, "error": "", "verify": c.verify}
    try:
        jobs = scrapers.scrape(c)
    except Exception as e:  # one broken site must not stop the run
        log.exception("scrape failed for %s", c.name)
        stats["error"] = f"{type(e).__name__}: {e}"[:500]
        return [], stats

    unique = {j.job_id: j for j in jobs if j.title and j.url}
    stats["found"] = len(unique)
    store.touch([jid for jid in unique if jid in matched], now)

    new_matched, enriched = [], 0
    for jid, job in unique.items():
        if jid in known:
            continue
        job.first_seen = job.last_seen = now
        needs_detail = len(job.description) < 200
        if needs_detail and not _worth_enriching(job):
            store.save(classify.classify(job), matched=False)
            stats["new"] += 1
            continue
        if needs_detail:
            if enriched >= DETAIL_LIMIT:
                stats["deferred"] += 1  # picked up next run
                continue
            try:
                scrapers.enrich(job)
            except Exception:
                log.exception("enrich failed for %s", job.url)
            enriched += 1
        classify.classify(job)
        is_match = bool(job.categories)
        store.save(job, matched=is_match)
        stats["new"] += 1
        if is_match:
            new_matched.append(job)
    stats["new_matched"] = len(new_matched)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    store.save_run(c.name, stats)
    return new_matched, stats


def _decode_backlog(store, fresh: list[Job], deadline: float) -> None:
    if not decode.enabled() or DECODE_LIMIT <= 0:
        return
    todo = [j for j in fresh if not j.decode]
    fresh_ids = {j.job_id for j in todo}
    if len(todo) < DECODE_LIMIT:  # older matches that haven't been decoded yet, newest first
        older = [Job.from_dict(d) for d in store.matched_jobs() if not d.get("decode") and d["job_id"] not in fresh_ids]
        older.sort(key=lambda j: j.first_seen, reverse=True)
        todo += older
    todo = todo[:DECODE_LIMIT]

    def work(job: Job) -> None:
        if time.monotonic() > deadline:
            return
        result = decode.decode_job(job)
        if not result:
            return
        job.decode = result
        if job.employment_type == classify.UNKNOWN and result.get("employment_type") not in (None, classify.UNKNOWN):
            job.employment_type = result["employment_type"]
        store.save(job, matched=True)

    with ThreadPoolExecutor(DECODE_WORKERS) as pool:
        list(pool.map(work, todo))


def run(time_budget_s: float = 840) -> dict:
    start = time.monotonic()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    store = get_store()
    known, matched = store.known_ids(), store.matched_ids()
    first_run = not known
    competitors = load_competitors()

    new_matched: list[Job] = []
    all_stats = []
    with ThreadPoolExecutor(SCRAPE_WORKERS) as pool:
        for jobs, stats in pool.map(lambda c: _process_competitor(c, known, matched, store, now), competitors):
            new_matched += jobs
            all_stats.append(stats)
            log.info("%(competitor)s: found=%(found)s new=%(new)s matched=%(new_matched)s err=%(error)s", stats)

    # Leave ~90s for the email after decoding.
    _decode_backlog(store, new_matched, deadline=start + time_budget_s - 90)

    if new_matched:
        notify.send_digest(new_matched, all_stats, first_run=first_run)

    summary = {
        "ran_at": now,
        "competitors": len(competitors),
        "jobs_found": sum(s["found"] for s in all_stats),
        "new_matched": len(new_matched),
        "errors": [s["competitor"] for s in all_stats if s["error"]],
        "zero_found": [s["competitor"] for s in all_stats if not s["found"] and not s["error"]],
        "seconds": round(time.monotonic() - start, 1),
    }
    log.info("run summary: %s", summary)
    return summary
