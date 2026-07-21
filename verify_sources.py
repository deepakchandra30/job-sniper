"""
Run locally (python verify_sources.py) before deploying.
Tests every entry in companies.json and reports:
  OK n     - fetched, n data-role links/jobs found right now
  OK 0     - fetched fine, just no data roles live today (normal)
  FAIL     - URL/slug broken; fix or remove the entry
Requires: pip install urllib3
"""
import json
import monitor  # reuses the same fetchers

with open("companies.json") as f:
    companies = json.load(f)["companies"]

broken = []
for c in companies:
    name, jobs, err = monitor.scan_company(c)
    if err:
        print(f"FAIL  {name:40s} {err[:80]}")
        broken.append(name)
    else:
        print(f"OK {len(jobs):3d}  {name}")

print(f"\n{len(broken)} broken sources" + (": fix these in companies.json" if broken else " - all good ✅"))
