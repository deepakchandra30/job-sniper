"""
Job Sniper - AWS Lambda job monitor
Scans careers pages/APIs for entry-level data roles, diffs against S3 state,
scores against Deepak's resume profile, emails new matches via SES instantly.

Env vars required:
  STATE_BUCKET   - S3 bucket for seen_jobs.json
  ALERT_EMAIL    - your email (SES-verified)
  SENDER_EMAIL   - SES-verified sender (can be same as ALERT_EMAIL)
"""

import json
import os
import re
import hashlib
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import urllib3

http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=5, read=12),
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept-Language": "en-IE,en;q=0.9",
    },
)

s3 = boto3.client("s3")
ses = boto3.client("ses", region_name=os.environ.get("SES_REGION", "eu-west-1"))

STATE_BUCKET = os.environ["STATE_BUCKET"]
STATE_KEY = "seen_jobs.json"
ALERT_EMAIL = os.environ["ALERT_EMAIL"]
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", ALERT_EMAIL)

# ---------------------------------------------------------------- matching ---

ROLE_KEYWORDS = [
    "data scientist", "data science", "data analyst", "data analytics",
    "machine learning", "ml engineer", "ai engineer", "analytics engineer",
    "business intelligence", "bi analyst", "insights analyst",
    "data engineer", "research analyst", "quantitative analyst",
]

# Entry-level signals. A job matches if it has a role keyword AND
# (an entry signal OR no seniority signal at all).
ENTRY_KEYWORDS = [
    "graduate", "grad ", "entry level", "entry-level", "junior", "intern",
    "internship", "associate", "trainee", "early career", "campus", "level 1",
    " i ", " 1 ", "assistant",
]

SENIOR_EXCLUDE = [
    "senior", "sr.", "sr ", "staff", "principal", "lead ", "manager", "head of",
    "director", "vp ", "vice president", "architect", "expert", "specialist iii",
    "iii", " iv", "distinguished", "chief",
]

# Resume-fit scoring: weighted keywords from Deepak's CV.
RESUME_SKILLS = {
    "python": 3, "sql": 3, "pyspark": 3, "spark": 2, "power bi": 3,
    "machine learning": 3, "predictive": 2, "etl": 3, "pipeline": 2,
    "azure": 2, "aws": 2, "gcp": 1, "cloudera": 2, "hive": 2, "oracle": 1,
    "generative ai": 2, "genai": 2, "llm": 2, "nlp": 1, "deep learning": 1,
    "tableau": 1, "statistics": 2, "statistical": 2, "forecasting": 2,
    "pandas": 1, "scikit": 1, "dashboards": 1, "visualisation": 1,
    "visualization": 1, "dbt": 1, "airflow": 1, "agile": 1,
}


def looks_entry_level(title, description=""):
    t = f" {title.lower()} "
    d = description.lower()
    if any(x in t for x in SENIOR_EXCLUDE):
        return False
    if any(x in t or x in d for x in ENTRY_KEYWORDS):
        return True
    # No seniority marker either way -> plain "Data Analyst" etc. Keep it:
    # generic titles are often open to <1yr experience.
    return True


def matches_role(title):
    t = title.lower()
    return any(k in t for k in ROLE_KEYWORDS)


def resume_fit_score(text):
    t = text.lower()
    score = sum(w for kw, w in RESUME_SKILLS.items() if kw in t)
    max_score = sum(RESUME_SKILLS.values())
    return round(100 * score / max_score)


def job_id(company, title, url):
    return hashlib.sha256(f"{company}|{title}|{url}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- fetchers ---

def fetch_greenhouse(company):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['slug']}/jobs"
    r = http.request("GET", url)
    if r.status != 200:
        return []
    jobs = []
    for j in json.loads(r.data).get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if "dublin" in loc.lower() or "ireland" in loc.lower() or "remote" in loc.lower():
            jobs.append({"title": j["title"], "url": j["absolute_url"], "location": loc})
    return jobs


def fetch_lever(company):
    url = f"https://api.lever.co/v0/postings/{company['slug']}?mode=json"
    r = http.request("GET", url)
    if r.status != 200:
        return []
    jobs = []
    for j in json.loads(r.data):
        loc = (j.get("categories") or {}).get("location", "") or ""
        if "dublin" in loc.lower() or "ireland" in loc.lower():
            jobs.append({"title": j["text"], "url": j["hostedUrl"], "location": loc})
    return jobs


LINK_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S
)
TAG_RE = re.compile(r"<[^>]+>")


def fetch_html(company):
    """Generic fallback: pull anchors whose text looks like a data job title."""
    r = http.request("GET", company["url"])
    if r.status != 200:
        return []
    page = r.data.decode("utf-8", errors="ignore")
    jobs, seen = [], set()
    for href, inner in LINK_RE.findall(page):
        title = TAG_RE.sub(" ", inner)
        title = re.sub(r"\s+", " ", title).strip()
        if not (5 < len(title) < 120):
            continue
        if not matches_role(title):
            continue
        if href.startswith("/"):
            base = re.match(r"(https?://[^/]+)", company["url"]).group(1)
            href = base + href
        elif not href.startswith("http"):
            continue
        key = (title, href)
        if key in seen:
            continue
        seen.add(key)
        jobs.append({"title": title, "url": href, "location": ""})
    return jobs


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "html": fetch_html}


def scan_company(company):
    try:
        raw = FETCHERS[company["type"]](company)
    except Exception as e:
        return company["name"], [], str(e)
    matched = [
        j for j in raw
        if matches_role(j["title"]) and looks_entry_level(j["title"])
    ]
    return company["name"], matched, None


# ------------------------------------------------------------------- state ---

def load_state():
    try:
        obj = s3.get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {"seen": {}, "first_run_done": False}
    except Exception:
        return {"seen": {}, "first_run_done": False}


def save_state(state):
    s3.put_object(
        Bucket=STATE_BUCKET, Key=STATE_KEY,
        Body=json.dumps(state).encode(), ContentType="application/json",
    )


# ------------------------------------------------------------------- email ---

def send_alert(new_jobs):
    rows = ""
    for j in sorted(new_jobs, key=lambda x: -x["fit"]):
        rows += (
            f'<tr><td style="padding:8px"><b>{j["company"]}</b></td>'
            f'<td style="padding:8px"><a href="{j["url"]}">{j["title"]}</a></td>'
            f'<td style="padding:8px">{j.get("location","")}</td>'
            f'<td style="padding:8px">{j["fit"]}%</td></tr>'
        )
    html = f"""
    <h2>&#128640; {len(new_jobs)} new data role(s) just posted</h2>
    <p>Apply fast - these were detected within minutes of going live.</p>
    <table border="1" cellspacing="0" style="border-collapse:collapse">
      <tr><th style="padding:8px">Company</th><th style="padding:8px">Role</th>
          <th style="padding:8px">Location</th><th style="padding:8px">CV fit</th></tr>
      {rows}
    </table>
    <p style="color:#888">Job Sniper · {datetime.datetime.utcnow().isoformat()}Z</p>
    """
    top = max(new_jobs, key=lambda x: x["fit"])
    ses.send_email(
        Source=SENDER_EMAIL,
        Destination={"ToAddresses": [ALERT_EMAIL]},
        Message={
            "Subject": {"Data": f"🚨 NEW: {top['title']} @ {top['company']} (+{len(new_jobs)-1} more)"
                        if len(new_jobs) > 1 else f"🚨 NEW: {top['title']} @ {top['company']}"},
            "Body": {"Html": {"Data": html}},
        },
    )


# ----------------------------------------------------------------- handler ---

def lambda_handler(event, context):
    with open(os.path.join(os.path.dirname(__file__), "companies.json")) as f:
        companies = json.load(f)["companies"]

    state = load_state()
    seen = state["seen"]
    first_run = not state.get("first_run_done", False)

    new_jobs, errors = [], []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(scan_company, c) for c in companies]
        for fut in as_completed(futures):
            name, jobs, err = fut.result()
            if err:
                errors.append(f"{name}: {err}")
                continue
            for j in jobs:
                jid = job_id(name, j["title"], j["url"])
                if jid in seen:
                    continue
                seen[jid] = datetime.datetime.utcnow().isoformat()
                fit = resume_fit_score(j["title"])
                new_jobs.append({**j, "company": name, "fit": max(fit, 30)})

    # Prune state > 90 days old to keep the file small
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=90)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v > cutoff}

    if first_run:
        # Baseline run: record everything currently live, don't spam email.
        state["first_run_done"] = True
        save_state(state)
        return {"status": "baseline", "recorded": len(new_jobs), "errors": errors}

    if new_jobs:
        send_alert(new_jobs)
    save_state(state)
    return {"status": "ok", "new": len(new_jobs), "errors": errors}
