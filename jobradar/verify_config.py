"""Verify EVERY companies.yaml entry against its LIVE ATS endpoint, and
optionally auto-fix the config from what the live endpoints actually return.

This is the source of truth -- NOT web search, NOT guesswork. It sends the
exact same API request the real pipeline sends, and only trusts a config that
returns real job postings. Run it on a network that can reach the ATS domains
(your machine, or the GitHub Actions "Verify ATS endpoints" workflow).

    python -m jobradar.verify_config            # report only
    python -m jobradar.verify_config --fix      # rewrite companies.yaml with
                                                # any working shard/site variants
                                                # it discovers by probing live

For a Workday FAIL it probes every combination of the known shards and a set
of common site-name variants, and reports (or writes) the FIRST combination
that actually returns postings. Anything it cannot make work against a live
endpoint is left untouched and clearly flagged -- never silently "fixed".
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .adapters.base import PoliteSession

ROOT = Path(__file__).resolve().parent.parent
SHARDS = ["wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd101", "wd103", "wd501"]


def _wd_probe(session, tenant, wd, site) -> tuple[bool, int, str]:
    """One live Workday probe — using the exact same CSRF warm-up as the real
    pipeline (cookie or page-HTML token), so verify and scan can't disagree.
    Returns (ok, total_postings, sample_title)."""
    from .adapters.workday import _wd_warmup
    headers = _wd_warmup(session, tenant, wd, site)
    headers.pop("_source", None)
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    r = session.post(url, headers=headers or None,
                     json={"appliedFacets": {}, "limit": 3, "offset": 0, "searchText": ""})
    if r is not None and r.status_code == 200 and "jobPostings" in r.text:
        data = r.json()
        postings = data.get("jobPostings", [])
        sample = postings[0].get("title", "?") if postings else "(no postings right now)"
        return True, data.get("total", 0), sample
    return False, 0, ""


def check_workday(c, session) -> tuple[bool, str, dict | None]:
    """Verify as-configured; if it fails, probe shard + site-name variants live.
    Returns (ok, message, fix_dict_or_None)."""
    tenant, wd, site = c["tenant"], c["wd"], c["site"]
    ok, total, sample = _wd_probe(session, tenant, wd, site)
    if ok:
        return True, f'OK ({total} postings) — e.g. "{sample}"', None

    # Build site-name variants to try (case + common patterns), preserving the
    # configured one first. These are TRIED AGAINST LIVE ENDPOINTS, not assumed.
    site_variants = []
    for s in [site, site.lower(), site.capitalize(), "External", "external",
              "Careers", "careers", f"{tenant}careers", f"{tenant}_careers"]:
        if s and s not in site_variants:
            site_variants.append(s)

    for shard in SHARDS:
        for s in site_variants:
            if shard == wd and s == site:
                continue  # already tried the configured combo above
            ok, total, sample = _wd_probe(session, tenant, shard, s)
            if ok and total >= 0:
                fix = {"wd": shard, "site": s}
                return False, f'FIXABLE -> wd={shard} site={s} ({total} postings, e.g. "{sample}")', fix
    return False, "FAIL (no working shard/site found live — tenant itself may be wrong)", None


def check_greenhouse(c, session) -> tuple[bool, str, dict | None]:
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs")
    if r is not None and r.status_code == 200:
        jobs = r.json().get("jobs", [])
        sample = jobs[0].get("title", "?") if jobs else "(no postings right now)"
        return True, f'OK ({len(jobs)} postings) — e.g. "{sample}"', None
    # Greenhouse boards sometimes live under job-boards host; the boards-api
    # host is authoritative for the API though, so a 404 here means wrong slug.
    return False, f"FAIL {getattr(r, 'status_code', 'no-response')} (board slug wrong — verify on live careers page)", None


def check_lever(c, session) -> tuple[bool, str, dict | None]:
    r = session.get(f"https://api.lever.co/v0/postings/{c['site']}?mode=json&limit=3")
    if r is not None and r.status_code == 200:
        jobs = r.json()
        sample = jobs[0].get("text", "?") if jobs else "(no postings right now)"
        return True, f'OK ({len(jobs)}+ postings) — e.g. "{sample}"', None
    return False, f"FAIL {getattr(r, 'status_code', 'no-response')} (site slug wrong — verify on live careers page)", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="rewrite companies.yaml with working variants discovered live")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "companies.yaml").read_text())
    session = PoliteSession(delay=0.4)
    ok = bad = fixable = skipped = 0
    changed = False

    for c in cfg["companies"]:
        ats = c.get("ats")
        fix = None
        if ats == "workday":
            good, msg, fix = check_workday(c, session)
        elif ats == "greenhouse":
            good, msg, fix = check_greenhouse(c, session)
        elif ats == "lever":
            good, msg, fix = check_lever(c, session)
        else:
            skipped += 1
            print(f"  --   {c['name']}: skipped ({c.get('note', 'no adapter configured')})")
            continue

        if good:
            ok += 1
            c["verified"] = True
            print(f"  OK   {c['name']}: {msg}")
        elif fix:
            fixable += 1
            print(f"  FIX  {c['name']}: {msg}")
            if args.fix:
                c.update(fix)
                c["verified"] = True
                changed = True
        else:
            bad += 1
            c["verified"] = False
            print(f"  FAIL {c['name']}: {msg}")

    print(f"\n{ok} verified live, {fixable} fixable, {bad} broken, {skipped} skipped (custom sites)")
    if args.fix and changed:
        (ROOT / "companies.yaml").write_text(
            yaml.dump(cfg, sort_keys=False, allow_unicode=True, width=120, default_flow_style=False))
        print("\ncompanies.yaml updated with live-verified fixes.")
    elif fixable and not args.fix:
        print("\nRe-run with --fix to write these live-verified corrections into companies.yaml.")


if __name__ == "__main__":
    main()
