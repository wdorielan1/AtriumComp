"""Email digest of newly found matching jobs (Amazon SES)."""

from __future__ import annotations

import html
import logging
from collections import defaultdict

from .classify import C2H, DIRECT, TEMP, UNKNOWN, auto_decode
from .config import env
from .dates import age_label
from .models import Job
from .watch import CLIENT, link

log = logging.getLogger(__name__)

MAX_ROWS = 150
TYPE_ORDER = [DIRECT, C2H, TEMP, UNKNOWN]
CONF_BADGE = {"high": "#1a7f37", "medium": "#9a6700", "low": "#6e7781", "none": "#6e7781"}


def _sub(text: str) -> str:
    return f"<br><span style='color:#57606a;font-size:12px'>{html.escape(text)}</span>" if text else ""


def _client_cell(job: Job) -> str:
    d = job.decode or {}
    guess = d.get("client_guess")
    if not guess:
        label = "pending" if auto_decode(job) else "not auto-decoded &middot; Decode on dashboard"
        return f'<span style="color:#6e7781">{label}</span>'
    conf = d.get("confidence", "none")
    reason = html.escape("; ".join(d.get("evidence", [])[:2]))
    return (f'<b>{html.escape(guess)}</b> <span style="color:{CONF_BADGE.get(conf, "#6e7781")};'
            f'font-size:12px">({conf})</span><br><span style="color:#57606a;font-size:12px">{reason}</span>')


def _watch_section(jobs: list[Job], watchlist: list[dict]) -> tuple[int, str]:
    """New postings tied to your prospects / current clients, shown first in the email."""
    esc = html.escape
    status = {w["name"]: w.get("status") for w in watchlist}
    rows = [(name, j) for j in jobs for name in link(j, watchlist)]
    if not rows:
        return 0, ""
    rows.sort(key=lambda r: (status.get(r[0]) != CLIENT, r[0], r[1].title))
    out = [f"<h3 style='margin:20px 0 6px'>Your prospects &amp; clients ({len(rows)})</h3>",
           "<table cellpadding='6' style='border-collapse:collapse;width:100%'>"
           "<tr style='background:#f6f8fa;text-align:left'><th>Company</th><th>Role</th><th>Posted by</th>"
           "<th>Location</th><th>Type</th></tr>"]
    for name, j in rows[:MAX_ROWS]:
        tag = "Current client" if status.get(name) == CLIENT else "Prospect"
        via = "Their own careers site" if j.direct else esc(j.competitor)
        out.append("<tr style='border-top:1px solid #d0d7de;vertical-align:top'>"
                   f"<td><b>{esc(name)}</b><br><span style='color:#57606a;font-size:12px'>{tag}</span></td>"
                   f"<td><a href='{esc(j.url)}'>{esc(j.title)}</a>{_sub(age_label(j.posted_date or j.first_seen[:10]))}</td>"
                   f"<td>{via}</td>"
                   f"<td>{esc(j.location or '—')}</td><td>{esc(j.employment_type)}</td></tr>")
    out.append("</table>")
    return len(rows), "".join(out)


def render_digest(jobs: list[Job], stats: list[dict], first_run: bool = False,
                  watchlist: list[dict] | None = None) -> tuple[str, str]:
    esc = html.escape
    watch_count, watch_html = _watch_section(jobs, watchlist or [])
    jobs = [j for j in jobs if not j.direct]  # own-site postings of watched companies appear only in the watch section
    by_type: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_type[j.employment_type].append(j)

    counts = ", ".join(f"{len(by_type[t])} {t}" for t in TYPE_ORDER if by_type[t])
    subject = f"[JobWatch] {len(jobs)} new life-science jobs at competitors ({counts or 'none'})"
    if watch_count:
        subject = f"[JobWatch] {watch_count} at your prospects/clients · " + subject.removeprefix("[JobWatch] ")
    dash = env("DASHBOARD_URL")
    parts = [
        '<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;color:#1f2328">',
        f"<h2 style='margin:0 0 4px'>{len(jobs)} new matching competitor postings</h2>",
        f"<p style='margin:0 0 16px;color:#57606a'>{esc(counts)}"
        + (f" &middot; <a href='{esc(dash)}'>Open dashboard</a>" if dash else "") + "</p>",
        watch_html,
    ]
    if first_run:
        parts.append("<p><b>First run:</b> this lists every matching job currently posted, so it is long. "
                     "From now on you will only get new postings. Client decoding runs in batches, so "
                     "many rows show <i>pending</i>; check the dashboard over the next few hours.</p>")

    shown = 0
    for t in TYPE_ORDER:
        group = sorted(by_type.get(t, []), key=lambda j: (j.competitor, j.title))
        if not group or shown >= MAX_ROWS:
            continue
        parts.append(f"<h3 style='margin:20px 0 6px'>{esc(t)} ({len(group)})</h3>")
        parts.append("<table cellpadding='6' style='border-collapse:collapse;width:100%'>"
                     "<tr style='background:#f6f8fa;text-align:left'><th>Role</th><th>Competitor</th>"
                     "<th>Location</th><th>Category</th><th>Likely client</th></tr>")
        for j in group:
            if shown >= MAX_ROWS:
                break
            parts.append(
                "<tr style='border-top:1px solid #d0d7de;vertical-align:top'>"
                f"<td><a href='{esc(j.url)}'>{esc(j.title)}</a>"
                + _sub(age_label(j.posted_date or j.first_seen[:10]))
                + (_sub(j.salary) if j.salary else "")
                + f"</td><td>{esc(j.competitor)}</td><td>{esc(j.location or '—')}</td>"
                f"<td>{esc(', '.join(j.categories))}</td><td>{_client_cell(j)}</td></tr>")
            shown += 1
        parts.append("</table>")
    if len(jobs) > shown:
        parts.append(f"<p>…and {len(jobs) - shown} more on the dashboard.</p>")

    broken = [s["competitor"] for s in stats if s.get("error") or not s.get("found")]
    if broken:
        parts.append("<p style='color:#57606a;font-size:12px;margin-top:20px'>No jobs could be read from: "
                     f"{esc(', '.join(sorted(broken)))}. Their site may have changed or the URL in "
                     "competitors.yaml needs fixing (see the dashboard's Sources tab).</p>")
    parts.append("</div>")
    return subject, "".join(parts)


def send_digest(jobs: list[Job], stats: list[dict], first_run: bool = False,
                watchlist: list[dict] | None = None) -> None:
    sender, to = env("EMAIL_FROM"), [a.strip() for a in env("EMAIL_TO").split(",") if a.strip()]
    subject, body = render_digest(jobs, stats, first_run, watchlist)
    if not sender or not to:
        log.info("EMAIL_FROM/EMAIL_TO not set; skipping email: %s", subject)
        return
    import boto3
    boto3.client("ses").send_email(
        Source=sender,
        Destination={"ToAddresses": to},
        Message={"Subject": {"Data": subject[:200]}, "Body": {"Html": {"Data": body}}},
    )
    log.info("sent digest to %s", to)
