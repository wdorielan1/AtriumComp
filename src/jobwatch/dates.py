"""Turn the many ways sites show a posting date into an ISO date.

Handles ISO timestamps, RSS dates ("Tue, 20 Oct 2026 14:00:00 GMT"), US dates
("10/20/2026"), month names ("Oct 20, 2026", "20 October 2026") and relative
text ("Posted today", "3 days ago", "30+ days ago", "2 weeks ago").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

_MONTH_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y")
_REL = re.compile(r"(\d+)\+?\s*(minute|hour|day|week|month)s?\s+ago", re.I)


def normalize_date(raw: str, today: date) -> str:
    """ISO date (YYYY-MM-DD), or "" if unparseable or implausible (future / >2 years old)."""
    s = (raw or "").strip()
    if not s:
        return ""
    d = _parse(s, today)
    if d is None or d > today + timedelta(days=1) or d < today - timedelta(days=730):
        return ""
    return d.isoformat()


def _parse(s: str, today: date) -> date | None:
    low = s.lower()
    m = _REL.search(low)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return today - timedelta(days=days)
    if re.search(r"\b(just posted|today|new)\b", low):
        return today
    if "yesterday" in low:
        return today - timedelta(days=1)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return date.fromisoformat(m.group(1))
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError, IndexError):
        pass
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        mo, dy, yr = (int(x) for x in m.groups())
        yr += 2000 if yr < 100 else 0
        try:
            return date(yr, mo, dy)
        except ValueError:
            return None
    cleaned = re.sub(r"(?i)^(posted|date posted|published|listed)\s*(on)?[:\s]*", "", s)
    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", cleaned).replace(".", "").strip()
    for fmt in _MONTH_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def age_label(posted_iso: str, today: date | None = None) -> str:
    """e.g. "Posted Oct 20, 2026 · up 3 days"."""
    if not posted_iso:
        return ""
    d = date.fromisoformat(posted_iso[:10])
    n = max(0, ((today or date.today()) - d).days)
    up = "posted today" if n == 0 else f"up {n} day{'s' if n != 1 else ''}"
    return f"Posted {d.strftime('%b')} {d.day}, {d.year} · {up}"


def posted_date(posted_raw: str, first_seen: str) -> str:
    """Best estimate of when the posting went up: the site's date if it makes sense, else when we first saw it."""
    seen = first_seen[:10] if first_seen else ""
    today = date.fromisoformat(seen) if seen else date.today()
    site = normalize_date(posted_raw, today)
    if site and (not seen or site <= seen):
        return site
    return seen
