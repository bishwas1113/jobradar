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

from .base import Job, PoliteSession, html_to_text, safe_json

LIST_URL = "https://{host_tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = "https://{host_tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
PUBLIC_URL = "https://{host_tenant}.{wd}.myworkdayjobs.com/en-US/{site}{path}"

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


_CSRF_BODY_RES = [
    re.compile(r'CALYPSO_CSRF_TOKEN["\']?\s*[:=]\s*["\']([\w.-]+)["\']'),
    re.compile(r'["\']csrfToken["\']\s*:\s*["\']([\w.-]+)["\']'),
    re.compile(r'name=["\']csrf-token["\']\s+content=["\']([\w.-]+)["\']'),
]


def _wd_warmup(session, tenant, wd, site) -> dict:
    """Some Workday tenants reject CXS API POSTs with a bare 422 unless the
    request carries the CSRF token a browser would have. Browsers get it two
    ways: (1) a Set-Cookie header on the landing page, or (2) JavaScript that
    reads a token embedded in the page HTML and sets the cookie client-side.
    We handle both: do the warm-up GET, take the cookie if the server set one,
    otherwise extract the token straight from the HTML (no JS execution
    needed -- the token is in the page source for the JS to read).
    Harmless no-op for tenants that don't require it."""
    host_tenant = tenant.replace("_", "-")
    landing = f"https://{host_tenant}.{wd}.myworkdayjobs.com/en-US/{site}"
    r = session.get(landing)  # cookies persist on the underlying requests.Session
    token = session.s.cookies.get("CALYPSO_CSRF_TOKEN")
    source = "cookie"
    if not token and r is not None and r.text:
        for pat in _CSRF_BODY_RES:
            m = pat.search(r.text)
            if m:
                token = m.group(1)
                source = "page-html"
                # Tenants usually require the cookie AND header to match, the
                # way a browser would send them after its JS sets the cookie.
                try:
                    session.s.cookies.set("CALYPSO_CSRF_TOKEN", token,
                                          domain=f"{host_tenant}.{wd}.myworkdayjobs.com")
                except Exception:
                    pass
                break
    if token:
        return {"X-CALYPSO-CSRF-TOKEN": token, "_source": source}
    return {}


def _wd_post_resilient(session, list_url, term, offset, page_size, extra_headers=None):
    """Some Workday tenants reject certain request-body shapes with a 422 and
    an empty error message (the endpoint is correct, the body isn't to their
    liking). Try progressively simpler/safer payload variants until one is
    accepted. Returns (response, used_empty_search) where response is the first
    variant that returned 200, or the last non-200 response for error reporting."""
    variants = [
        {"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": term},
        {"appliedFacets": {}, "limit": min(page_size, 20), "offset": offset, "searchText": term},
        {"appliedFacets": {}, "limit": min(page_size, 20), "offset": offset, "searchText": ""},
        {"limit": min(page_size, 20), "offset": offset, "searchText": term},
    ]
    last = None
    for payload in variants:
        r = session.post(list_url, json=payload, headers=extra_headers or None)
        last = r
        if r is not None and r.status_code == 200 and "jobPostings" in (r.text or ""):
            return r, (payload.get("searchText", "") == "")
    return last, False


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
    max_seconds: float = 150.0,
) -> List[Job]:
    import time
    from .. import cache as cache_mod

    start = time.monotonic()
    host_tenant = tenant.replace("_", "-")
    list_url = LIST_URL.format(host_tenant=host_tenant, tenant=tenant, wd=wd, site=site)
    seen: dict[str, Job] = {}
    # Warm-up: obtain the CSRF token some tenants require before API POSTs.
    csrf_headers = _wd_warmup(session, tenant, wd, site)
    csrf_source = csrf_headers.pop("_source", None)  # diagnostics only, not a header

    for term in search_terms:
        if time.monotonic() - start > max_seconds:
            break  # time budget spent on listing; skip remaining terms
        for page in range(max_pages_per_term):
            if time.monotonic() - start > max_seconds:
                break
            r, used_empty = _wd_post_resilient(session, list_url, term, page * page_size,
                                                page_size, extra_headers=csrf_headers)
            if r is None or r.status_code != 200:
                if page == 0 and term == search_terms[0]:
                    body = (r.text[:200] if r is not None else "no response")
                    tried = (f"CSRF token from {csrf_source}" if csrf_source
                             else "no CSRF token found in cookie or page HTML")
                    raise RuntimeError(
                        f"workday:{tenant}/{site} HTTP {getattr(r, 'status_code', 'ERR')} "
                        f"(all 4 payload variants failed, {tried}) - {body}"
                    )
                break
            postings = safe_json(r, f"workday:{tenant}").get("jobPostings", [])
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
                    url=PUBLIC_URL.format(host_tenant=host_tenant, tenant=tenant, wd=wd, site=site, path=path),
                    posted=parse_posted_on(p.get("postedOn", "")),
                    description="",
                    source="workday",
                    job_id=f"wd-{tenant}-{path}",
                )
            if len(postings) < page_size:
                break
            # If we had to fall back to an unfiltered (empty searchText) query,
            # don't page deep through the whole catalog — the local level +
            # keyword filters still apply, but we cap breadth to stay polite
            # and within the time budget.
            if used_empty and page >= 1:
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
        r = session.get(DETAIL_URL.format(host_tenant=host_tenant, tenant=tenant, wd=wd, site=site, path=path))
        if r is not None and r.status_code == 200:
            try:
                info = safe_json(r, f"workday:{tenant}:detail").get("jobPostingInfo", {}) or {}
            except RuntimeError:
                info = {}  # keep the job with title/location; just no description
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
