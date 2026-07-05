"""Greenhouse and Lever adapters — both expose public read-only JSON APIs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .base import Job, PoliteSession, html_to_text

GH_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{site}?mode=json"


def fetch_greenhouse(company: str, board: str, session: PoliteSession) -> List[Job]:
    r = session.get(GH_URL.format(board=board))
    if r is None or r.status_code != 200:
        raise RuntimeError(f"greenhouse:{board} HTTP {getattr(r, 'status_code', 'ERR')}")
    jobs = []
    for j in r.json().get("jobs", []):
        # Greenhouse boards API exposes updated_at / first_published; prefer the
        # earliest-publication field when present, fall back to updated_at.
        posted_raw = j.get("first_published") or j.get("updated_at") or ""
        posted = posted_raw[:10] if posted_raw else None
        jobs.append(Job(
            company=company,
            title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", "") or "",
            url=j.get("absolute_url", ""),
            posted=posted,
            description=html_to_text(j.get("content", "")),
            source="greenhouse",
            job_id=f"gh-{board}-{j.get('id')}",
        ))
    return jobs


def fetch_lever(company: str, site: str, session: PoliteSession) -> List[Job]:
    r = session.get(LEVER_URL.format(site=site))
    if r is None or r.status_code != 200:
        raise RuntimeError(f"lever:{site} HTTP {getattr(r, 'status_code', 'ERR')}")
    jobs = []
    for j in r.json():
        created_ms = j.get("createdAt")
        posted = None
        if created_ms:
            posted = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).date().isoformat()
        cats = j.get("categories") or {}
        jobs.append(Job(
            company=company,
            title=j.get("text", "").strip(),
            location=cats.get("location", "") or "",
            url=j.get("hostedUrl", "") or j.get("applyUrl", ""),
            posted=posted,
            description=j.get("descriptionPlain") or html_to_text(j.get("description", "")),
            source="lever",
            job_id=f"lv-{site}-{j.get('id')}",
        ))
    return jobs
