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
                              ├─ decode (only postings you choose): Claude + web search + ClinicalTrials.gov
                              ├─ your prospects' / clients' own careers pages (optional)
                              ├─ DynamoDB (remembers every job, so nothing is processed twice)
                              └─ SES email digest of NEW matches
Dashboard Lambda (Function URL, password) ─► Jobs · Competitor clients · Prospects & Clients · Sources,
                                             on-demand "Decode" button, CSV export
```

## What you get

**Email** (only when something new appears): new postings at **your prospects and clients**
first, then competitor postings grouped into Direct hire, Contract-to-hire and Temp / Contract.
Each row shows role, competitor, location, category, **when it was posted and how long it has been
up**, and the likely client with a confidence level.

**Dashboard**
- **Jobs**: every matching competitor posting, with its posted date and how long it has been up
  ("up 12 days"; amber once a role has been open 30+ days). Filters cover competitor, employment
  type, category, client confidence, and still-posted vs. posted in the last 7/30 days. Click a likely
  client to see the evidence, alternatives and sources, or click **Decode** on any posting that
  hasn't been decoded.
- **Competitor clients**: the companies your competitors appear to recruit for, ranked by number of
  postings, with which competitors and the temp/direct split. **+ Prospect** adds one to your list.
- **Prospects & Clients**: the companies *you* track, marked Prospect or Current client. For each
  one you see the open postings that relate to it, whether found through competitors (decoded, or
  the company named in the posting) or on the company's **own careers page**, if you add it.
  Add, edit, re-mark or remove companies right on this tab.
- **Competitors**: every competitor you track, with its website and jobs page, how many jobs were read
  on the last run, and any errors. Use it to spot a site whose layout changed. This is also where you
  manage the list (see below).
- **Export CSV** of everything.

**Posting dates.** The tool uses the date the site shows ("Oct 20, 2026", "3 days ago", and so on). When
a site shows no date, it uses the date the tool first saw the posting; hovering over the age shows
which one was used. Postings that disappear show how long they *were* up.

## How decoding works

Decoding costs money, so it runs automatically only for postings matching `auto_decode` in
[`categories.yaml`](src/jobwatch/config/categories.yaml). The default is **Direct hire roles in
Clinical or Medical Affairs**. Any other posting has a **Decode** button on the dashboard; one click
decodes it in about a minute, and the result is saved, so a second click costs nothing.
Postings from your prospects' own careers pages are never decoded, since the company is already known.

Agencies hide the client's name, but postings leak clues: the suburb, the therapeutic area, the
phase, "recently raised a $150M Series C", "two approved products". For each decoded posting,
Claude:
1. Web-searches the most distinctive sentences. Clients often post the same role on their own
   careers page with near-identical wording, which is the strongest evidence.
2. Queries ClinicalTrials.gov for industry sponsors running trials in that area and location.
3. Returns a best guess, a confidence level (**high / medium / low / none**), its evidence,
   alternative candidates, the URLs it relied on, and its own read on temp vs direct.

Treat **low** guesses as leads, not facts. Results are only as good as the clues in each posting.

### Choosing the model: start cheap, measure, then decide

Decoding starts on **Claude Haiku 5.5**, the cheapest model. Each decoded posting on the dashboard
shows which model decoded it, with **✓ / ✗** buttons to mark the guess right or wrong. The Jobs tab
shows the running score per model, e.g. *"72% decodes right · Haiku 5.5 (18/25 checked)"*.

1. Mark 20–30 decodes where you know (or can find out) the real client.
2. If the score is good enough, stay on Haiku. If not, set the GitHub variable `DECODE_MODEL` to
   `claude-sonnet-5-5` (or `claude-opus-5-5`) and re-run the deploy workflow.
3. Use **Re-decode** on the ones Haiku got wrong. The new answer shows next to the old one
   ("Before: … marked wrong"), and the score tracks the new model separately, so you can compare
   the two models on the same postings.

Rough cost per 100 decodes with web search: Haiku ~$3–5, Sonnet ~$5–12, Opus ~$10–25.

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
  and `DECODE_MODEL` (default `claude-haiku-5-5`)

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

## Adding competitors, prospects and clients

You don't need to edit any files for this. Use the dashboard:

| To do this | Go to | How |
|---|---|---|
| Add one competitor | **Competitors** | Type the name and website (`kornferry.com` is fine). The jobs page is found automatically; add it only if the tool can't find it. |
| Add many competitors | **Competitors** → *Import a list* | Paste lines like `Name, website` (or copy two columns from Excel/Google Sheets), or upload a CSV. |
| Track a suggested competitor | **Competitors** | Entries marked `suggested: true` (with `enabled: false`) in `competitors.yaml` show greyed out. Click **Add**. |
| Pause or remove one | **Competitors** | **Pause** keeps it listed and stops checking it; **Remove** (only for ones you added) deletes it. |
| Add one prospect or client | **Prospects & Clients** | Type the name, choose Prospect or Current client, and optionally paste their careers page. |
| Add many prospects/clients | **Prospects & Clients** → *Import a list* | Paste `Name, careers page, prospect/client`, or copy columns from a spreadsheet. A header row is fine. Names already tracked are skipped. |

Changes apply from the next hourly run. The lists in `competitors.yaml` are just the starting list.

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

You can also fix this from the dashboard: on the **Competitors** tab, click **Edit** next to the site and
paste the jobs-page address. Also note:
- **Synectics**: your list had a LinkedIn link. LinkedIn can't be scraped (it blocks it and
  its terms forbid it), so the config uses a guess at their own site.

## Tuning

| What | Where |
|---|---|
| Keywords for each category, internal-recruiter titles to skip, temp/direct phrases | `src/jobwatch/config/categories.yaml` |
| **Which postings are auto-decoded** (employment types, categories, competitors) | `auto_decode` in `src/jobwatch/config/categories.yaml` |
| Prospects and current clients | Dashboard → Prospects & Clients |
| Competitors | `src/jobwatch/config/competitors.yaml` |
| Max auto-decodes per hour (`DecodeLimit`, 0 = only the Decode button), web searches per decode (`DecodeWebSearches`, 0 = off) | GitHub variables `DECODE_LIMIT` / `DECODE_WEB_SEARCHES`, or `sam deploy` parameters |
| Schedule | `Schedule` parameter in `template.yaml` (default `rate(1 hour)`) |
| Claude model | GitHub variable `DECODE_MODEL`: `claude-haiku-5-5` (default), `claude-sonnet-5-5` or `claude-opus-5-5` |
| Decoding effort | `DECODE_EFFORT` env var (default `medium`) |

## Cost (rough)

| Item | Estimate |
|---|---|
| Lambda, DynamoDB, SES | usually a few $/month (mostly within the free tier) |
| Claude decoding, with web search | roughly **$0.03–0.05** per decode on Haiku (default), $0.05–0.12 on Sonnet, $0.10–0.25 on Opus |

You pay only for postings that are decoded: the ones matching `auto_decode`, plus any you click
**Decode** on. Scraping frequency barely matters, because each posting is decoded at most once. To spend
less, narrow `auto_decode`, set `DECODE_WEB_SEARCHES` to `0` (much cheaper, less accurate), or set
`DECODE_LIMIT` to `0` and decode only by clicking.

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
