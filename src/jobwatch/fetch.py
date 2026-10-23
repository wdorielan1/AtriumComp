"""Polite HTTP fetching: retries, timeouts, robots.txt, per-host pacing."""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; JobWatch/1.0; competitor job-posting monitor) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
TIMEOUT = 20
MIN_INTERVAL = 1.0  # seconds between requests to the same host

_local = threading.local()
_robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_last_hit: dict[str, float] = {}
_lock = threading.Lock()


def session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
        _local.session = s
    return s


def _allowed(url: str) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    with _lock:
        if base not in _robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = session().get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            _robots[base] = rp
        rp = _robots[base]
    return rp is None or rp.can_fetch("JobWatch", url)


def _pace(url: str) -> None:
    host = urlparse(url).netloc
    with _lock:
        wait = _last_hit.get(host, 0) + MIN_INTERVAL - time.monotonic()
        _last_hit[host] = time.monotonic() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


def get(url: str, *, respect_robots: bool = True, **kw) -> requests.Response | None:
    """GET a URL; returns None on network error, robots disallow, or non-2xx."""
    if respect_robots and not _allowed(url):
        log.info("robots.txt disallows %s", url)
        return None
    _pace(url)
    try:
        r = session().get(url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        log.warning("GET %s failed: %s", url, e)
        return None
    if not r.ok:
        log.warning("GET %s -> HTTP %s", url, r.status_code)
        return None
    return r


def get_json(url: str, **kw):
    r = get(url, respect_robots=False, **kw)  # public JSON APIs of job-board vendors
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        return None
