"""Local command line.

  python -m jobwatch.cli discover [NAME]   check what the scraper finds per competitor
  python -m jobwatch.cli run               one full run (local JSON store unless TABLE_NAME set)
  python -m jobwatch.cli decode URL        decode a single posting
  python -m jobwatch.cli serve [PORT]      dashboard at http://localhost:8000
  python -m jobwatch.cli preview-email     write the digest for current matches to data/digest.html
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import classify, dashboard, decode, notify, pipeline, scrapers
from .config import load_competitors
from .models import Job
from .store import get_store


def cmd_discover(args) -> None:
    comps = [c for c in load_competitors() if not args.name or args.name.lower() in c.name.lower()]
    for c in comps:
        print(f"\n=== {c.name} ({c.careers_url or c.homepage})" + ("  [verify]" if c.verify else ""))
        try:
            jobs = scrapers.scrape(c)
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            continue
        sources = sorted({j.source for j in jobs})
        print(f"  {len(jobs)} jobs found via {', '.join(sources) or '-'}")
        for j in jobs[: args.sample]:
            if args.enrich:
                scrapers.enrich(j)
            classify.classify(j)
            tag = ", ".join(j.categories) or "no match"
            print(f"  - {j.title[:70]:70} | {j.location[:25]:25} | {j.employment_type:16} | {tag}")
            print(f"    {j.url}")
        if not jobs:
            print("  Nothing found. Open the site, find the job-search page, and set careers_url "
                  "(or type/board if it uses Greenhouse, Lever, Bullhorn, ...).")


def cmd_run(args) -> None:
    print(json.dumps(pipeline.run(), indent=2))


def cmd_decode(args) -> None:
    job = Job(competitor=args.competitor, title="", url=args.url)
    scrapers.generic.enrich(job)
    if not job.title:
        from .scrapers.parse import soup
        from . import fetch
        r = fetch.get(args.url)
        if r is not None:
            h = soup(r.text).find(["h1", "title"])
            job.title = h.get_text(strip=True) if h else ""
    classify.classify(job)
    print(f"{job.title} | {job.location} | {job.employment_type} | {job.categories}")
    if not decode.enabled():
        sys.exit("Set ANTHROPIC_API_KEY to decode.")
    print(json.dumps(decode.decode_job(job), indent=2))


def cmd_preview_email(args) -> None:
    store = get_store()
    jobs = [Job.from_dict(d) for d in store.matched_jobs()]
    subject, body = notify.render_digest(jobs, store.runs())
    out = Path("data/digest.html")
    out.parent.mkdir(exist_ok=True)
    out.write_text(body)
    print(f"{subject}\nwrote {out}")


def cmd_serve(args) -> None:
    class H(BaseHTTPRequestHandler):
        def _go(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            event = {
                "rawPath": urlparse(self.path).path,
                "requestContext": {"http": {"method": method}},
                "cookies": [c.strip() for c in (self.headers.get("Cookie") or "").split(";") if c.strip()],
                "body": self.rfile.read(n).decode() if n else "",
            }
            r = dashboard.handler(event)
            self.send_response(r["statusCode"])
            for k, v in r["headers"].items():
                self.send_header(k, v)
            for c in r.get("cookies", []):
                self.send_header("Set-Cookie", c.replace("; Secure", ""))  # plain http locally
            self.end_headers()
            self.wfile.write(r["body"].encode())

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    print(f"Dashboard on http://localhost:{args.port}")
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="jobwatch")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("name", nargs="?")
    d.add_argument("--sample", type=int, default=8)
    d.add_argument("--enrich", action="store_true", help="fetch detail pages for the sample")
    d.set_defaults(fn=cmd_discover)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    dc = sub.add_parser("decode")
    dc.add_argument("url")
    dc.add_argument("--competitor", default="(manual)")
    dc.set_defaults(fn=cmd_decode)
    s = sub.add_parser("serve")
    s.add_argument("port", nargs="?", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("preview-email").set_defaults(fn=cmd_preview_email)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
