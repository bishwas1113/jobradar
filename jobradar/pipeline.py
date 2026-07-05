"""Daily pipeline: fetch -> filter -> score -> dedup -> write docs/data/matches.json.

Run:  python -m jobradar.pipeline [--config companies.yaml] [--resume resume.txt]
      [--max-age-days 3] [--fixtures tests/fixtures]  (fixtures = offline mode)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def job_key(j: Job) -> str:
    return hashlib.sha1(f"{j.url}|{j.title}".encode()).hexdigest()[:16]


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def fetch_all(cfg: dict, session: PoliteSession, errors: list) -> list[Job]:
    jobs: list[Job] = []
    terms = cfg.get("search_terms", ["analytics"])
    for c in cfg["companies"]:
        ats = c.get("ats")
        try:
            if ats == "greenhouse":
                jobs += fetch_greenhouse(c["name"], c["board"], session)
            elif ats == "lever":
                jobs += fetch_lever(c["name"], c["site"], session)
            elif ats == "workday":
                jobs += fetch_workday(
                    c["name"], c["tenant"], c["wd"], c["site"], session,
                    search_terms=terms, detail_prefilter=title_prefilter,
                )
            # ats == "unknown": skipped until an adapter is configured
        except Exception as e:  # fault isolation: one company never kills the run
            errors.append({"company": c["name"], "error": str(e)})
    return jobs


def run(config_path: str, resume_path: str, max_age_days: int, fixtures: str | None) -> dict:
    cfg = yaml.safe_load(Path(config_path).read_text())
    profile = parse_resume(Path(resume_path).read_text())
    errors: list = []

    if fixtures:
        jobs = load_fixture_jobs(Path(fixtures))
    else:
        session = PoliteSession(delay=1.0)
        jobs = fetch_all(cfg, session, errors)

    raw_count = len(jobs)
    jobs = apply_filters(jobs, {
        "locations": cfg.get("locations", ["Philadelphia", "Pennsylvania"]),
        "allow_remote": cfg.get("allow_remote", True),
        "max_age_days": max_age_days,
    })
    jobs = score_jobs(jobs, profile)

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for j in jobs:
        k = job_key(j)
        j.score_parts["first_seen"] = seen.get(k, now)
        seen[k] = seen.get(k, now)

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now,
        "max_age_days": max_age_days,
        "raw_postings": raw_count,
        "matches": [j.to_dict() for j in jobs],
        "errors": errors,
    }
    (OUT / "matches.json").write_text(json.dumps(payload, indent=1))
    SEEN_FILE.write_text(json.dumps(seen, indent=0))
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
