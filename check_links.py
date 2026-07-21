"""
Standalone link checker - runs on any PC, NO AWS/boto3 needed.
    pip install urllib3
    python check_links.py

Reports per company:
  OK   n  - reachable, found n data-role links right now
  OK   0  - reachable, no data roles live today (normal, not a failure)
  EMPTY   - page loaded but looks JS-rendered (no server-side links) - unreliable source
  FAIL    - 404 / connection error / blocked - fix the URL or drop the entry
Writes results.csv and a cleaned suggested_companies.json (FAILs removed).
"""
import json
import re
import csv
import urllib3

urllib3.disable_warnings()
http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=6, read=15),
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
             "Accept-Language": "en-IE,en;q=0.9"},
)

ROLE = ["data scientist", "data science", "data analyst", "data analytics",
        "machine learning", "ml engineer", "ai engineer", "analytics engineer",
        "business intelligence", "bi analyst", "data engineer",
        "research analyst", "quantitative analyst"]
LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def matches(t):
    t = t.lower()
    return any(k in t for k in ROLE)


def check_greenhouse(slug):
    r = http.request("GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if r.status != 200:
        return "FAIL", 0, f"HTTP {r.status}"
    jobs = json.loads(r.data).get("jobs", [])
    n = sum(1 for j in jobs if matches(j["title"]))
    return "OK", n, f"{len(jobs)} total jobs"


def check_lever(slug):
    r = http.request("GET", f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if r.status != 200:
        return "FAIL", 0, f"HTTP {r.status}"
    jobs = json.loads(r.data)
    n = sum(1 for j in jobs if matches(j.get("text", "")))
    return "OK", n, f"{len(jobs)} total jobs"


def check_html(url):
    try:
        r = http.request("GET", url)
    except Exception as e:
        return "FAIL", 0, str(e)[:60]
    if r.status != 200:
        return "FAIL", 0, f"HTTP {r.status}"
    page = r.data.decode("utf-8", errors="ignore")
    links = LINK_RE.findall(page)
    if len(links) < 3:
        return "EMPTY", 0, "few/no server-side links (JS-rendered?)"
    n = sum(1 for _, inner in links if matches(TAG_RE.sub(" ", inner)))
    return "OK", n, f"{len(links)} links scanned"


CHECK = {"greenhouse": lambda c: check_greenhouse(c["slug"]),
         "lever": lambda c: check_lever(c["slug"]),
         "html": lambda c: check_html(c["url"])}

companies = json.load(open("companies.json"))["companies"]
rows, keep = [], []
for i, c in enumerate(companies, 1):
    try:
        status, n, detail = CHECK[c["type"]](c)
    except Exception as e:
        status, n, detail = "FAIL", 0, str(e)[:60]
    print(f"[{i:3d}/{len(companies)}] {status:6s} {n:3d}  {c['name']:45.45s} {detail}")
    rows.append({"company": c["name"], "type": c["type"], "status": status,
                 "roles_found": n, "detail": detail})
    if status != "FAIL":
        keep.append(c)

with open("results.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["company", "type", "status", "roles_found", "detail"])
    w.writeheader()
    w.writerows(rows)
json.dump({"companies": keep}, open("suggested_companies.json", "w"), indent=2)

fails = sum(1 for r in rows if r["status"] == "FAIL")
empty = sum(1 for r in rows if r["status"] == "EMPTY")
ok = sum(1 for r in rows if r["status"] == "OK")
print(f"\n{'='*60}\nOK: {ok}   EMPTY(unreliable): {empty}   FAIL: {fails}")
print("-> results.csv written (open in Excel to review)")
print("-> suggested_companies.json written (FAILs removed; review EMPTY ones)")
