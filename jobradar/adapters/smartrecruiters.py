import time
from .base import Job, PoliteSession, html_to_text

def fetch_smartrecruiters(company_name: str, board: str, session: PoliteSession,
                           search_terms: list[str], detail_prefilter=None) -> list[Job]:
    """Fetch jobs from a company's SmartRecruiters portal."""
    jobs = []
    base_url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
    offset = 0
    limit = 100

    while True:
        params = {
            "offset": str(offset),
            "limit": str(limit)
        }
        try:
            r = session.get(base_url, params=params)
            if r is None or r.status_code != 200:
                break
            
            data = r.json()
            postings = data.get("content", [])
            if not postings:
                break
                
            for p in postings:
                job_id = p.get("id")
                title = p.get("name") or ""
                
                # Check prefilter if available
                if detail_prefilter and not detail_prefilter(title):
                    continue
                
                # Fetch details for description
                detail_url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{job_id}"
                time.sleep(0.1) # polite spacing
                
                dr = session.get(detail_url)
                desc = ""
                if dr is not None and dr.status_code == 200:
                    detail_data = dr.json()
                    sections = detail_data.get("jobAd", {}).get("sections", {}) or {}
                    parts = []
                    for key in ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]:
                        text = sections.get(key, {}).get("text")
                        if text:
                            parts.append(text)
                    desc = html_to_text("\n".join(parts))
                
                loc_data = p.get("location", {}) or {}
                location = loc_data.get("fullLocation") or loc_data.get("city") or ""
                released = p.get("releasedDate")
                posted = released[:10] if released else None
                url = f"https://jobs.smartrecruiters.com/{board}/{job_id}"

                jobs.append(Job(
                    company=company_name,
                    title=title.strip(),
                    location=location.strip(),
                    url=url,
                    posted=posted,
                    description=desc,
                    source="smartrecruiters"
                ))
            
            total = data.get("totalFound", 0)
            offset += limit
            if offset >= total:
                break
        except Exception:
            break
            
        time.sleep(0.5)

    return jobs
