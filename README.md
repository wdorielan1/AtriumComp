# Competitor Job Watch

Every hour, this tool checks the job boards of competing staffing firms. It keeps the
**life-science roles** (Clinical, Medical Affairs, Regulatory/Quality/PV, Lab/R&D,
life-science Commercial), tags each one **Direct hire / Contract-to-hire / Temp-Contract**,
and uses Claude to **decode which client company is probably behind the posting**.
New matches arrive by **email**, and everything is browsable on a password-protected
**web dashboard**.

```
EventBridge (hourly) ─► Scrape Lambda ─► competitor sites (public pages, robots.txt respected)
                              │
                              ├─ classify: category + temp vs direct (keyword rules you can edit)
                              ├─ decode: Claude + web search + ClinicalTrials.gov ─► likely client
                              ├─ DynamoDB (remembers every job, so nothing is processed twice)
                              └─ SES email digest of NEW matches
Dashboard Lambda (Function URL, password) ─► Jobs · Clients · Sources tabs, CSV export
```

## What you get

**Email** (only when something new appears): grouped into Direct hire, Contract-to-hire and
Temp / Contract, with role, competitor, location, category, and the likely client with a
confidence level.

**Dashboard**
- **Jobs**: every matching posting, with filters for competitor, employment type, category,
  client confidence, and "still posted" vs. new in the last 7/30 days. Click a likely client to
  see the evidence, alternative candidates, and source links.
- **Clients**: the decoded companies ranked by number of postings, showing which competitors
  recruit for each one and the temp/direct split. This tab answers "who is each competitor working with?"
- **Sources**: per-competitor health: jobs found on the site, matches, and errors. Use it to spot a
  competitor whose site changed.
- **Export CSV** of everything.

## How decoding works

Agencies hide the client's name, but postings leak clues: the suburb, the therapeutic area, the
phase, "recently raised a $150M Series C", "two approved products". For each new matching posting,
Claude:
1. Web-searches the most distinctive sentences. Clients often post the same role on their own
   careers page with near-identical wording, which is the strongest evidence.
2. Queries ClinicalTrials.gov for industry sponsors running trials in that area and location.
3. Returns a best guess, a confidence level (**high / medium / low / none**), its evidence,
   alternative candidates, the URLs it relied on, and its own read on temp vs direct.

Treat **low** guesses as leads, not facts. Results are only as good as the clues in each posting.

## Setup (about 30 minutes)

You need an AWS account, an Anthropic API key, and this GitHub repo.

### 1. Allow AWS to send email (SES)
In the AWS console, open **Amazon SES** in your region (e.g. us-east-1) and go to **Identities → Create identity**.
Verify the **sender** address (or your whole domain). A new SES account starts in "sandbox"
mode, where you must also verify each **recipient** address, or request production access.

### 2. Store the two secrets in AWS
In the console, open **Systems Manager → Parameter Store → Create parameter** and create both as type **SecureString**:

| Name | Value |
|---|---|
| `/jobwatch/anthropic-api-key` | your Anthropic API key |
| `/jobwatch/dashboard-password` | a password for the dashboard |

(CLI equivalent: `aws ssm put-parameter --type SecureString --name /jobwatch/anthropic-api-key --value sk-ant-...`)

### 3. Deploy

**Option A: from GitHub (no installs on your PC).**
Create an IAM user with deploy rights (CloudFormation, Lambda, IAM, DynamoDB, S3, EventBridge)
and an access key, then in the repo go to **Settings → Secrets and variables → Actions**:
- Secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- Variables: `EMAIL_FROM`, `EMAIL_TO` (comma-separated), optionally `AWS_REGION` (default `us-east-1`)

Then open **Actions → Test and deploy → Run workflow** (or push to `main`). The log ends with your
dashboard URL.

**Option B: from your machine.** Install the AWS SAM CLI and Python 3.12, then:
```bash
sam build
sam deploy --guided --stack-name jobwatch --capabilities CAPABILITY_IAM
```

### 4. First run
It runs automatically within the hour. To run it immediately: in the AWS console, open **Lambda → the
`jobwatch-ScrapeFunction-…` function → Test**. Then open the dashboard and check the
**Sources** tab.

The first email lists everything currently posted, so it is long. After that you only get
new postings. Decoding works through the backlog 25 postings per hour.

## Fix any competitors that show "No jobs read"

I couldn't open the competitor sites while building this, so some URLs in
[`src/jobwatch/config/competitors.yaml`](src/jobwatch/config/competitors.yaml) are best
guesses (marked `verify: true`). The scraper also looks for a "Jobs/Careers" link on each
homepage by itself. For any competitor showing 0 jobs:

1. Open their site in a browser, find the page that lists jobs, and paste that URL as `careers_url`.
2. If their jobs page is hosted by Greenhouse, Lever, Workable, SmartRecruiters or Bullhorn, the
   scraper detects it automatically. Pages rendered entirely by JavaScript (some JobDiva/Ceipal
   portals) may need a small custom adapter; ask and one can be added.
3. Check locally with `python -m jobwatch.cli discover "Firm Name" --enrich`.

Edit the file on GitHub and commit; it redeploys automatically. Also still needed:
- **Novaris Search**: I couldn't find its website. Add its `homepage` and set `enabled: true`.
- **Synectics**: your list had a LinkedIn link. LinkedIn can't be scraped (it blocks it and
  its terms forbid it), so the config uses a guess at their own site.

## Tuning

| What | Where |
|---|---|
| Keywords for each category, internal-recruiter titles to skip, temp/direct phrases | `src/jobwatch/config/categories.yaml` |
| Competitors | `src/jobwatch/config/competitors.yaml` |
| Decodes per hour (`DecodeLimit`, 0 = off), web searches per decode (`DecodeWebSearches`, 0 = off) | GitHub variables `DECODE_LIMIT` / `DECODE_WEB_SEARCHES`, or `sam deploy` parameters |
| Schedule | `Schedule` parameter in `template.yaml` (default `rate(1 hour)`) |
| Claude model / effort | `DECODE_MODEL` (default `claude-opus-5-5`), `DECODE_EFFORT` (default `medium`) env vars on the scrape function |

## Cost (rough)

| Item | Estimate |
|---|---|
| Lambda, DynamoDB, SES | usually a few $/month (mostly within the free tier) |
| Claude decoding, with web search | roughly **$0.10–0.25 per decoded posting** |

At 30 new matching postings a day, decoding is about $3–8/day. The first-run backlog is capped by
`DecodeLimit` (25 per hour, so at most about 600 decodes/day). Set `DECODE_WEB_SEARCHES` to `0` to cut
the cost a lot (lower accuracy), or `DECODE_LIMIT` to `0` to turn decoding off.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cd src
python -m jobwatch.cli discover                 # what the scraper finds on each site
python -m jobwatch.cli run                      # full run into data/jobs.json (decodes if ANTHROPIC_API_KEY is set)
python -m jobwatch.cli decode <job-url>         # decode a single posting
DASHBOARD_PASSWORD=pick-one python -m jobwatch.cli serve   # http://localhost:8000
cd .. && python -m pytest -q                    # tests
```

## Good-citizen scraping

The scraper reads only public job pages, obeys each site's `robots.txt`, waits at least one
second between requests to the same site, opens each job's detail page only once, and identifies
itself in its User-Agent.
