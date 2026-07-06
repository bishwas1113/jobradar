"""Daily pipeline: fetch -> filter -> score -> dedup -> write docs/data/matches.json.

Run:  python -m jobradar.pipeline [--config companies.yaml] [--resume resume.txt]
      [--max-age-days 3] [--fixtures tests/fixtures]  (fixtures = offline mode)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .adapters.base import Job, PoliteSession
from .adapters.greenhouse_lever import fetch_greenhouse, fetch_lever
from .adapters.workday import fetch_workday
from .filters import apply_filters, title_prefilter
from .resume_parser import parse_resume
from .scoring import score_jobs

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
SEEN_FILE = OUT / "seen.json"
CACHE_FILE = OUT / "detail_cache.json"
ARCHIVE_DIR = OUT / "archive"


def job_key(j: Job) -> str:
    return hashlib.sha1(f"{j.url}|{j.title}".encode()).hexdigest()[:16]


def company_url(c: dict) -> str:
    """Best-effort public career-site link for the Companies tab."""
    ats = c.get("ats")
    if ats == "workday":
        return f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/en-US/{c['site']}"
    if ats == "greenhouse":
        return f"https://boards.greenhouse.io/{c['board']}"
    if ats == "lever":
        return f"https://jobs.lever.co/{c['site']}"
    return ""


def export_companies(cfg: dict) -> None:
    """Write docs/data/companies.json so the dashboard's Companies tab can
    list every configured company with a link, without parsing YAML in JS."""
    rows = []
    for c in cfg["companies"]:
        rows.append({
            "name": c["name"],
            "ats": c.get("ats", "unknown"),
            "tenant": c.get("tenant"), "wd": c.get("wd"), "site": c.get("site"),
            "board": c.get("board"),
            "verified": bool(c.get("verified")),
            "note": c.get("note", ""),
            "url": company_url(c),
        })
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "companies.json").write_text(json.dumps(
        {"search_terms": cfg.get("search_terms", []), "companies": rows}, indent=1))


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def build_payload(jobs: list[Job], profile, cfg: dict, max_age_days: int,
                   errors: list, raw_count: int, seen: dict, now: str,
                   partial: bool) -> dict:
    """Filter/score/write whatever jobs have been collected so far. Called after
    every company during the scan so a kill at any point leaves a valid,
    useful file on disk -- never just an in-memory result that vanishes."""
    filtered = apply_filters(list(jobs), {
        "locations": cfg.get("locations", ["Philadelphia", "Pennsylvania"]),
        "allow_remote": cfg.get("allow_remote", True),
        "max_age_days": max_age_days,
    })
    filtered = score_jobs(filtered, profile)
    for j in filtered:
        k = job_key(j)
        j.score_parts["first_seen"] = seen.get(k, now)
        seen[k] = seen.get(k, now)
    payload = {
        "generated_at": now,
        "max_age_days": max_age_days,
        "raw_postings": raw_count,
        "matches": [j.to_dict() for j in filtered],
        "errors": errors,
        "partial": partial,  # true while the scan is still in progress / was cut short
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "matches.json").write_text(json.dumps(payload, indent=1))
    SEEN_FILE.write_text(json.dumps(seen, indent=0))
    return payload


def fetch_one_company(c: dict, terms: list, delay: float = 1.0) -> tuple[str, list[Job], Optional[str]]:
    """Runs in its own thread with its own PoliteSession (so the polite
    request-spacing only throttles requests to that one company, not across
    all of them). Returns (company_name, jobs, error_or_None)."""
    from .cache import load_cache  # local import: cache file is read fresh per thread start
    session = PoliteSession(delay=delay)
    detail_cache = load_cache(CACHE_FILE)
    try:
        ats = c.get("ats")
        if ats == "greenhouse":
            jobs = fetch_greenhouse(c["name"], c["board"], session)
        elif ats == "lever":
            jobs = fetch_lever(c["name"], c["site"], session)
        elif ats == "workday":
            jobs = fetch_workday(
                c["name"], c["tenant"], c["wd"], c["site"], session,
                search_terms=terms, detail_prefilter=title_prefilter,
                detail_cache=detail_cache,
            )
        else:
            jobs = []
        from .cache import save_cache
        save_cache(CACHE_FILE, detail_cache)
        return c["name"], jobs, None
    except Exception as e:
        return c["name"], [], str(e)


def fetch_all_parallel(cfg: dict, errors: list, profile, max_age_days: int,
                        seen: dict, now: str, max_workers: int = 6,
                        first_pass_seconds: float = 150.0,
                        retry_seconds: float = 100.0) -> tuple[list[Job], int]:
    """Scans every company concurrently instead of one at a time, so a single
    slow ATS endpoint never blocks the rest of the scan. Companies still
    in flight after the first pass get one retry window; anything still
    unfinished after that is skipped for this run (logged, not silently
    dropped) rather than holding up everything else indefinitely.

    Checkpoints (a full write of everything collected so far) happen after
    every company finishes, same guarantee as the old sequential version --
    just now driven by whichever company finishes next, in any order.
    """
    import concurrent.futures as cf

    terms = cfg.get("search_terms", ["analytics"])
    companies = cfg["companies"]
    jobs: list[Job] = []
    write_lock = threading.Lock()

    def checkpoint(is_partial: bool):
        with write_lock:
            build_payload(jobs, profile, cfg, max_age_days, errors, len(jobs), seen, now,
                          partial=is_partial)

    pool = cf.ThreadPoolExecutor(max_workers=max_workers)
    pending = {pool.submit(fetch_one_company, c, terms): c for c in companies}

    def drain(timeout_budget: float, remaining: dict) -> dict:
        """Collect whatever finishes within the budget; return the still-pending map."""
        deadline = time.monotonic() + timeout_budget
        still_pending = dict(remaining)
        try:
            for fut in cf.as_completed(list(remaining.keys()),
                                        timeout=max(0.1, deadline - time.monotonic())):
                c = still_pending.pop(fut, None)
                name, found_jobs, err = fut.result()
                if err:
                    errors.append({"company": name, "error": err})
                else:
                    jobs.extend(found_jobs)
                checkpoint(is_partial=True)
        except cf.TimeoutError:
            pass
        return still_pending

    still_pending = drain(first_pass_seconds, pending)
    if still_pending:
        still_pending = drain(retry_seconds, still_pending)
    if still_pending:
        # Genuinely too slow even with the retry window: log and move on.
        # Note: Python cannot forcibly kill an in-flight request, so that
        # company's own thread keeps running in the background and its
        # result (if any) is simply discarded when it eventually finishes --
        # but critically, we do NOT wait for it, and it never delayed any
        # other company's result above.
        for fut, c in still_pending.items():
            errors.append({"company": c["name"],
                           "error": "timed out (slow endpoint) - skipped this run, will retry tomorrow"})
    pool.shutdown(wait=False, cancel_futures=True)  # never block our own return on stragglers

    checkpoint(is_partial=False)
    return jobs, len(jobs)


def write_archive(payload: dict) -> None:
    """Dated snapshot of each completed run, so past scans aren't lost when
    matches.json gets overwritten tomorrow. Keeps the most recent 30 days."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = (payload.get("generated_at") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    (ARCHIVE_DIR / f"{date_str}.json").write_text(json.dumps(payload, indent=1))
    snapshots = sorted(ARCHIVE_DIR.glob("*.json"))
    for old in snapshots[:-30]:
        old.unlink(missing_ok=True)


def run(config_path: str, resume_path: str, max_age_days: int, fixtures: str | None) -> dict:
    cfg = yaml.safe_load(Path(config_path).read_text())
    profile = parse_resume(Path(resume_path).read_text())
    errors: list = []
    export_companies(cfg)

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if fixtures:
        jobs = load_fixture_jobs(Path(fixtures))
        raw_count = len(jobs)
        payload = build_payload(jobs, profile, cfg, max_age_days, errors, raw_count,
                                 seen, now, partial=False)
    else:
        jobs, raw_count = fetch_all_parallel(cfg, errors, profile, max_age_days, seen, now)
        payload = build_payload(jobs, profile, cfg, max_age_days, errors, raw_count,
                                 seen, now, partial=False)
        write_archive(payload)
    return payload


def load_fixture_jobs(fixture_dir: Path) -> list[Job]:
    """Offline mode: build Jobs from stored API fixture files (used in tests/CI)."""
    from .adapters.base import html_to_text
    jobs: list[Job] = []
    for f in sorted(fixture_dir.glob("gh_*.json")):
        data = json.loads(f.read_text())
        for j in data.get("jobs", []):
            jobs.append(Job(
                company=data.get("_company", f.stem), title=j["title"],
                location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""), posted=(j.get("first_published") or "")[:10] or None,
                description=html_to_text(j.get("content", "")), source="greenhouse",
                job_id=f"gh-fix-{j.get('id')}",
            ))
    for f in sorted(fixture_dir.glob("wd_*.json")):
        data = json.loads(f.read_text())
        for j in data.get("jobs", []):
            jobs.append(Job(**j))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "companies.yaml"))
    ap.add_argument("--resume", default=str(ROOT / "resume.txt"))
    ap.add_argument("--max-age-days", type=int, default=3)
    ap.add_argument("--fixtures", default=None)
    a = ap.parse_args()
    payload = run(a.config, a.resume, a.max_age_days, a.fixtures)
    print(f"matches: {len(payload['matches'])}  raw: {payload['raw_postings']}  "
          f"errors: {len(payload['errors'])}")
    for e in payload["errors"]:
        print(f"  ! {e['company']}: {e['error']}", file=sys.stderr)


if __name__ == "__main__":
    main()
