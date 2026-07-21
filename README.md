# Job Sniper — 24/7 Entry-Level Data Role Monitor (AWS)

Scans ~110 curated Irish employers every 5 minutes for Data Scientist / Data Analyst / data-related entry-level roles, scores each against your CV skills (Python, SQL, PySpark, Power BI, ML, ETL, Azure/AWS), and emails you instantly when something new goes live — so you can apply within 5–10 minutes of posting.

## Architecture

EventBridge (rate: 5 min) → Lambda (parallel scan, ~30–60s) → S3 (seen-jobs state) → SES (email alert)

Cost: effectively €0. ~260k GB-seconds/month of Lambda compute vs 400k free-tier allowance; S3 and SES usage is negligible.

## Deploy (one command)

```bash
# 1. Make sure AWS CLI is configured
aws configure   # region: eu-west-1

# 2. Deploy
chmod +x deploy.sh
./deploy.sh deepakchandra3012@gmail.com

# 3. Click the SES verification link AWS emails you (one-time)
```

That's it. The first run is a silent baseline (it records everything currently live so you don't get 200 emails). Every run after that emails you only genuinely NEW postings.

## Before deploying (recommended, 5 min)

Careers URLs rot. Test the sources locally and prune any that fail:

```bash
pip install urllib3 boto3
python verify_sources.py
```

Fix or delete any `FAIL` entries in `companies.json`, then deploy. Re-run this monthly.

## How matching works

A job alert fires when a title contains a role keyword (`data scientist`, `data analyst`, `machine learning`, `analytics engineer`, `business intelligence`, `data engineer`, ...) AND is not senior (`senior`, `staff`, `principal`, `lead`, `manager`, ... are excluded). Titles with explicit entry signals (`graduate`, `junior`, `intern`, `associate`, `early career`) always pass; plain untitled-seniority roles (e.g. just "Data Analyst") also pass, because those are frequently open to <1 year experience — better a false positive you skim in 5 seconds than a missed role.

The **CV fit %** in the email is a weighted keyword match against your core stack. It's a fast-triage signal, not a verdict — anything 40%+ with the right title is worth opening.

## Tuning

- **Add/remove companies**: edit `companies.json`, then `./deploy.sh your@email.com` again (it updates in place).
  - `greenhouse` / `lever` entries use official JSON APIs — most reliable, add any company that uses those ATSs by slug.
  - `html` entries scrape anchor tags — works broadly, breaks silently if a site is fully JS-rendered. For JS-heavy sites (Workday especially), the search-URL versions listed usually still expose links server-side, but verify.
- **Stricter matching**: remove the final `return True` in `looks_entry_level()` to require an explicit entry-level keyword.
- **Change frequency**: `aws events put-rule --name job-sniper-5min --schedule-expression "rate(10 minutes)"`.

## Known limitations (honest list)

1. **JS-only careers pages** (some Workday/Eightfold sites) may return 0 jobs via plain HTML scraping. The verify script will show `OK 0` permanently for these — for such companies, find their Greenhouse/Lever slug if one exists, or swap in their RSS/API endpoint.
2. **Some sites block datacenter IPs** (403s). Options: remove them, or route those few through a scraping-friendly endpoint.
3. Keyword title matching can't read the job description's "0–1 years experience" line. Ambiguous titles are included by design; skim and skip.

## Monitoring the monitor

```bash
aws logs tail /aws/lambda/job-sniper --follow --region eu-west-1
```

Each run logs `{"status":"ok","new":N,"errors":[...]}`. If a company keeps erroring, fix or drop it.
