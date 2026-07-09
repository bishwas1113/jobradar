import time
from .base import Job, PoliteSession, html_to_text

def fetch_lilly(session: PoliteSession, search_terms: list[str]) -> list[Job]:
    """Fetch jobs from Eli Lilly's JobSync Solr API endpoint."""
    jobs = []
    base_url = "https://prod-search-api.jobsyn.org/api/v1/solr/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-origin": "jobsearch.lilly.com"
    }

    # Query all terms
    for term in search_terms:
        page = 1
        while True:
            params = {
                "page": str(page),
                "q": term
            }
            try:
                r = session.get(base_url, params=params, headers=headers)
                if r is None or r.status_code != 200:
                    break
                data = r.json()
                results = data.get("jobs", [])
                if not results:
                    break
                
                for j in results:
                    title_exact = j.get("title_exact")
                    title_slug = j.get("title_slug")
                    location = j.get("location_exact") or ""
                    posted_date = j.get("date_added")  # e.g., "2026-07-08T06:18:02.492Z"
                    desc = html_to_text(j.get("description") or "")
                    
                    if not title_exact or not title_slug:
                        continue
                        
                    # Format date: 2026-07-08T06:18:02.492Z -> 2026-07-08
                    posted = None
                    if posted_date:
                        posted = posted_date[:10]
                        
                    url = f"https://jobsearch.lilly.com/jobs/{title_slug}/"
                    
                    jobs.append(Job(
                        company="Eli Lilly",
                        title=title_exact.strip(),
                        location=location.strip(),
                        url=url,
                        posted=posted,
                        description=desc,
                        source="lilly"
                    ))
                
                pagination = data.get("pagination", {})
                if not pagination.get("has_more_pages") or page >= pagination.get("total_pages", 1):
                    break
                page += 1
            except Exception:
                break
            
            # Rate limit politely
            time.sleep(0.5)
            
    return jobs
