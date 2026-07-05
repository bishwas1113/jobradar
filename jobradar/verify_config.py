"""Verify every companies.yaml entry against its live ATS endpoint.

Run locally (or as a one-off Actions job) BEFORE trusting the daily pipeline:

    python -m jobradar.verify_config

For each company it reports OK / FAIL with the HTTP status, so wrong tenant
or board IDs are caught immediately. For Workday failures it also probes the
other common wd shards (wd1/wd3/wd5/wd12) and suggests a correction if one
responds. Common fix for remaining failures: open the company careers site in
a browser, note the myworkdayjobs.com URL — the tenant, shard, and site name
are all visible in it — and update companies.yaml.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .adapters.base import PoliteSession

ROOT = Path(__file__).resolve().parent.parent
SHARDS = ["wd1", "wd3", "wd5", "wd12"]


def check_workday(c, session) -> tuple[bool, str]:
    url = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    r = session.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if r is not None and r.status_code == 200 and "jobPostings" in r.text:
        return True, f"OK ({r.json().get('total', '?')} postings)"
    status = getattr(r, "status_code", "no-response")
    for shard in SHARDS:
        if shard == c["wd"]:
            continue
        alt = f"https://{c['tenant']}.{shard}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}/jobs"
        r2 = session.post(alt, json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        if r2 is not None and r2.status_code == 200 and "jobPostings" in r2.text:
            return False, f"FAIL {status} -> try wd: {shard}"
    return False, f"FAIL {status} (check tenant/site in careers-page URL)"


def check_greenhouse(c, session) -> tuple[bool, str]:
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    if r is not None and r.status_code == 200:
        return True, f"OK ({len(r.json().get('jobs', []))} postings)"
    return False, f"FAIL {getattr(r, 'status_code', 'no-response')} (check board slug)"


def check_lever(c, session) -> tuple[bool, str]:
    r = session.get(f"https://api.lever.co/v0/postings/{c['site']}?mode=json&limit=1")
    if r is not None and r.status_code == 200:
        return True, "OK"
    return False, f"FAIL {getattr(r, 'status_code', 'no-response')} (check site slug)"


def main():
    cfg = yaml.safe_load((ROOT / "companies.yaml").read_text())
    session = PoliteSession(delay=0.5)
    ok = bad = skipped = 0
    for c in cfg["companies"]:
        ats = c.get("ats")
        if ats == "workday":
            good, msg = check_workday(c, session)
        elif ats == "greenhouse":
            good, msg = check_greenhouse(c, session)
        elif ats == "lever":
            good, msg = check_lever(c, session)
        else:
            skipped += 1
            print(f"  -    {c['name']}: skipped ({c.get('note', 'no adapter')})")
            continue
        ok += good
        bad += not good
        print(f"  {'OK  ' if good else 'FAIL'} {c['name']}: {msg}")
    print(f"\n{ok} verified, {bad} need attention, {skipped} skipped (custom sites)")


if __name__ == "__main__":
    main()
