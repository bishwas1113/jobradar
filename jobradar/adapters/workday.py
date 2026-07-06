"""Workday adapter — uses the unofficial-but-stable CXS JSON endpoint.

Strategy: instead of paging a tenant's entire catalog (thousands of postings),
run each configured search term through Workday's server-side searchText and
merge results. Detail pages are fetched only for postings that survive the
cheap title/level pre-filter, keeping request counts low and polite.

postedOn arrives as relative text ("Posted Today", "Posted 3 Days Ago",
"Posted 30+ Days Ago") in list results; the detail endpoint returns a real
date under jobPostingInfo.startDate when available.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Callable, List, Optional

from .base import Job, PoliteSession, html_to_text

LIST_URL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = "https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
PUBLIC_URL = "https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{path}"

_REL_RE = re.compile(r"(\d+)\+?\s*day", re.I)


def parse_posted_on(text: str) -> Optional[str]:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' -> ISO date."""
    if not text:
        return None
    t = text.lower()
    if "today" in t:
        return date.today().isoformat()
    if "yesterday" in t:
        return (date.today() - timedelta(days=1)).isoformat()
    m = _REL_RE.search(t)
    if m:
        days = int(m.group(1))
        d = (date.today() - timedelta(days=days)).isoformat()
        return d + "+" if "+" in text else d  # '30+' marks a lower bound
    return None


def fetch_workday(
    company: str,
    tenant: str,
    wd: str,
    site: str,
    session: PoliteSession,
    search_terms: List[str],
    detail_prefilter: Optional[Callable[[str], bool]] = None,
    max_pages_per_term: int = 3,
    page_size: int = 20,
    detail_cache: Optional[dict] = None,
    max_seconds: float = 90.0,
) -> List[Job]:
    import time
    from .. import cache as cache_mod

    start = time.monotonic()
    list_url = LIST_URL.format(tenant=tenant, wd=wd, site=site)
    seen: dict[str, Job] = {}

    for term in search_terms:
        if time.monotonic() - start > max_seconds:
            break  # time budget spent on listing; skip remaining terms
        for page in range(max_pages_per_term):
            if time.monotonic() - start > max_seconds:
                break
            payload = {
                "appliedFacets": {},
                "limit": page_size,
                "offset": page * page_size,
                "searchText": term,
            }
            r = session.post(list_url, json=payload)
            if r is None or r.status_code != 200:
                if page == 0 and term == search_terms[0]:
                    raise RuntimeError(
                        f"workday:{tenant}/{site} HTTP {getattr(r, 'status_code', 'ERR')}"
                    )
                break
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                path = p.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen[path] = Job(
                    company=company,
                    title=(p.get("title") or "").strip(),
                    location=p.get("locationsText", "") or "",
                    url=PUBLIC_URL.format(tenant=tenant, wd=wd, site=site, path=path),
                    posted=parse_posted_on(p.get("postedOn", "")),
                    description="",
                    source="workday",
                    job_id=f"wd-{tenant}-{path}",
                )
            if len(postings) < page_size:
                break

    # Fetch descriptions only for postings that pass the cheap pre-filter,
    # reusing the cache when a posting was already fetched recently.
    jobs: List[Job] = []
    for path, job in seen.items():
        if detail_prefilter and not detail_prefilter(job.title):
            continue
        if time.monotonic() - start > max_seconds:
            # Out of time budget for detail fetches: keep the posting with
            # whatever we have (title/location/date), just without a
            # description. It'll still show up, just score lower on skills.
            jobs.append(job)
            continue
        cache_key = job.job_id
        cached = cache_mod.get_cached(detail_cache, cache_key) if detail_cache is not None else None
        if cached:
            job.description = cached["description"]
            if cached.get("posted"):
                job.posted = cached["posted"]
            job.is_remote = bool(cached.get("is_remote"))
            jobs.append(job)
            continue
        r = session.get(DETAIL_URL.format(tenant=tenant, wd=wd, site=site, path=path))
        if r is not None and r.status_code == 200:
            info = r.json().get("jobPostingInfo", {}) or {}
            job.description = html_to_text(info.get("jobDescription", ""))
            start_date = info.get("startDate")
            if start_date:
                job.posted = str(start_date)[:10]
            if info.get("remoteType", "").lower().startswith("fully"):
                job.is_remote = True
            if detail_cache is not None:
                cache_mod.put_cached(detail_cache, cache_key, job.description, job.posted, job.is_remote)
        jobs.append(job)
    return jobs
