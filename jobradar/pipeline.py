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
    if c.get("name") == "Eli Lilly" or ats == "lilly":
        return "https://jobsearch.lilly.com/jobs/"
    if ats == "phenom":
        return f"https://{c['site']}"
    if ats == "workday":
        host_tenant = c["tenant"].replace("_", "-")
        return f"https://{host_tenant}.{c['wd']}.myworkdayjobs.com/en-US/{c['site']}"
    if ats == "greenhouse":
        return f"https://boards.greenhouse.io/{c['board']}"
    if ats == "lever":
        return f"https://jobs.lever.co/{c['site']}"
    if ats == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{c['board']}"
    if ats == "successfactors":
        return f"https://{c['domain']}/search/"
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
    (OUT / "companies.json").write_text(json.dumps({
        "search_terms": cfg.get("search_terms", []),
        "locations": cfg.get("locations", ["Philadelphia", "Pennsylvania"]),
        "allow_remote": cfg.get("allow_remote", True),
        "weights": cfg.get("weights", {"semantic": 0.55, "skills": 0.30, "level": 0.15}),
        "level_fit": cfg.get("level_fit", {
            "Senior Manager": 1.0,
            "Associate Director": 1.0,
            "Director": 0.8,
            "Senior Director": 0.5
        }),
        "companies": rows
    }, indent=1))


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def build_payload(jobs: list[Job], profile, cfg: dict, max_age_days: int,
                   errors: list, raw_count: int, seen: dict, now: str,
                   partial: bool, company_fetch: dict | None = None) -> dict:
    """Filter/score/write whatever jobs have been collected so far. Called after
    every company during the scan so a kill at any point leaves a valid,
    useful file on disk -- never just an in-memory result that vanishes."""
    filtered = apply_filters(list(jobs), {
        "locations": cfg.get("locations", ["Philadelphia", "Pennsylvania"]),
        "allow_remote": cfg.get("allow_remote", True),
        "max_age_days": max_age_days,
    })
    filtered = score_jobs(filtered, profile, weights=cfg.get("weights"), level_fit=cfg.get("level_fit"))
    for j in filtered:
        k = job_key(j)
        j.score_parts["first_seen"] = seen.get(k, now)
        seen[k] = seen.get(k, now)

    # Per-company outcome: every configured company gets an explicit status so
    # the dashboard can render a placeholder for each -- never a silent absence.
    err_by_company = {e["company"]: e["error"] for e in errors}
    match_counts: dict[str, int] = {}
    for j in filtered:
        match_counts[j.company] = match_counts.get(j.company, 0) + 1
    company_status = []
    for c in cfg["companies"]:
        name = c["name"]
        entry = {"name": name, "ats": c.get("ats", "unknown")}
        if c.get("ats") == "unknown":
            entry.update(status="skipped", hint=c.get("note", "no adapter configured"))
        elif name in err_by_company:
            kind, hint = classify_error(err_by_company[name])
            entry.update(status="error", error_kind=kind, hint=hint,
                         error=err_by_company[name])
        elif company_fetch is not None and name not in company_fetch and partial:
            entry.update(status="pending", hint="not yet scanned in this run")
        else:
            fetched = (company_fetch or {}).get(name, 0)
            matched = match_counts.get(name, 0)
            entry.update(status="ok" if matched else "empty",
                         fetched=fetched, matches=matched)
        company_status.append(entry)

    payload = {
        "generated_at": now,
        "max_age_days": max_age_days,
        "raw_postings": raw_count,
        "matches": [j.to_dict() for j in filtered],
        "companies_status": company_status,
        "errors": errors,
        "partial": partial,  # true while the scan is still in progress / was cut short
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "matches.json").write_text(json.dumps(payload, indent=1))
    SEEN_FILE.write_text(json.dumps(seen, indent=0))
    return payload


def classify_error(err: str) -> tuple[str, str]:
    """Map a verbose error to (kind, human hint). The verbose text is kept
    alongside for copy-paste troubleshooting; this just adds orientation."""
    e = err.lower()
    if "422" in e:
        return "payload-rejected", "Endpoint is real but rejected our request format; adapter auto-retries simpler formats — if persisting, the remaining variants also failed"
    if "404" in e:
        return "not-found", "Board/tenant not found — either the slug is wrong or the board currently has zero openings"
    if "401" in e or "403" in e:
        return "blocked", "Endpoint requires auth or is blocking automated requests — may need a custom adapter"
    if "timed out" in e or "timeout" in e:
        return "slow", "Endpoint too slow this run; skipped, will retry next scan"
    if "not valid json" in e:
        return "bad-response", "Endpoint returned a non-JSON page (maintenance page or bot-block); usually transient"
    if e.startswith("http 5") or " 50" in e or " 52" in e or " 53" in e:
        return "server-error", "Their server errored; usually transient, retried automatically"
    if "no response" in e or "connection" in e:
        return "unreachable", "Network-level failure reaching the endpoint; usually transient"
    return "unknown", "Unrecognized failure — copy the verbose error into chat to troubleshoot"


def fetch_one_company(c: dict, terms: list, delay: float = 1.0) -> tuple[str, list[Job], Optional[str]]:
    """Runs in its own thread with its own PoliteSession (so the polite
    request-spacing only throttles requests to that one company, not across
    all of them). Returns (company_name, jobs, error_or_None)."""
    from .cache import load_cache  # local import: cache file is read fresh per thread start
    session = PoliteSession(delay=delay)
    detail_cache = load_cache(CACHE_FILE)
    try:
        ats = c.get("ats")
        if c.get("name") == "Eli Lilly" or ats == "lilly":
            from .adapters.lilly import fetch_lilly
            jobs = fetch_lilly(session, terms)
        elif ats == "greenhouse":
            jobs = fetch_greenhouse(c["name"], c["board"], session)
        elif ats == "lever":
            jobs = fetch_lever(c["name"], c["site"], session)
        elif ats == "workday":
            jobs = fetch_workday(
                c["name"], c["tenant"], c["wd"], c["site"], session,
                search_terms=terms, detail_prefilter=title_prefilter,
                detail_cache=detail_cache,
            )
        elif ats == "smartrecruiters":
            from .adapters.smartrecruiters import fetch_smartrecruiters
            jobs = fetch_smartrecruiters(c["name"], c["board"], session, terms, detail_prefilter=title_prefilter)
        elif ats == "successfactors":
            from .adapters.successfactors import fetch_successfactors
            jobs = fetch_successfactors(c["name"], c["domain"], session, terms, detail_prefilter=title_prefilter, detail_cache=detail_cache)
        elif ats == "phenom":
            from .adapters.phenom import fetch_phenom
            jobs = fetch_phenom(c["name"], c["site"], session, terms, detail_prefilter=title_prefilter)
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
                        retry_seconds: float = 100.0,
                        existing_matches: dict | None = None,
                        is_today: bool = False) -> tuple[list[Job], int]:
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
    company_fetch: dict[str, int] = {}   # company -> raw postings fetched

    # Automated incremental scan: skip companies that were already scanned today
    scanned_companies = set()
    if is_today and existing_matches:
        # Identify already scanned companies
        scanned_companies = {c["name"] for c in existing_matches.get("companies_status", [])
                             if c.get("status") in ("ok", "empty", "error")}
        
        # Load existing jobs for those companies
        for m in existing_matches.get("matches", []):
            if m["company"] in scanned_companies:
                jobs.append(Job(
                    company=m["company"], title=m["title"], location=m["location"],
                    url=m["url"], posted=m["posted"], description=m["description"],
                    source=m.get("source", ""), job_id=m.get("job_id", ""),
                    score=m.get("score"), score_parts=m.get("score_parts", {}),
                    level=m.get("level"), is_remote=m.get("is_remote", False)
                ))
        
        # Load existing fetch counts and copy errors
        for c in existing_matches.get("companies_status", []):
            if c["name"] in scanned_companies:
                if c.get("status") in ("ok", "empty"):
                    company_fetch[c["name"]] = c.get("fetched", 0)
                elif c.get("status") == "error":
                    errors.append({"company": c["name"], "error": c.get("error", "unknown error")})
        
        # Filter down list of companies to only those not yet scanned
        companies = [c for c in companies if c["name"] not in scanned_companies]
    write_lock = threading.Lock()

    def checkpoint(is_partial: bool):
        with write_lock:
            build_payload(jobs, profile, cfg, max_age_days, errors, len(jobs), seen, now,
                          partial=is_partial, company_fetch=company_fetch)

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
                    company_fetch[name] = len(found_jobs)
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


def run(config_path: str, resume_path: str, max_age_days: int, fixtures: str | None, force_full: bool = False) -> dict:
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
        # Load existing matches to detect if running incrementally on the same day
        matches_file = Path("docs/data/matches.json")
        existing_matches = None
        is_today = False
        if not force_full and matches_file.exists():
            try:
                existing_matches = json.loads(matches_file.read_text())
                gen_at = existing_matches.get("generated_at", "")
                if gen_at:
                    existing_date = gen_at[:10]
                    today_date = datetime.now(timezone.utc).date().isoformat()
                    if existing_date == today_date:
                        is_today = True
            except Exception:
                pass

        jobs, raw_count = fetch_all_parallel(
            cfg, errors, profile, max_age_days, seen, now,
            existing_matches=existing_matches, is_today=is_today
        )
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
    ap.add_argument("--force-full", action="store_true")
    a = ap.parse_args()
    payload = run(a.config, a.resume, a.max_age_days, a.fixtures, force_full=a.force_full)
    print(f"matches: {len(payload['matches'])}  raw: {payload['raw_postings']}  "
          f"errors: {len(payload['errors'])}")
    for e in payload["errors"]:
        print(f"  ! {e['company']}: {e['error']}", file=sys.stderr)


if __name__ == "__main__":
    main()
