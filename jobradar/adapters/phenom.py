import re
import json
import time
from .base import Job, PoliteSession, html_to_text, safe_json

def fetch_phenom(company_name: str, site: str, session: PoliteSession,
                 search_terms: list[str], detail_prefilter=None) -> list[Job]:
    """Fetch jobs from a Phenom People frontend."""
    jobs = []
    seen_ids = set()
    
    # Phenom widgets API
    base_domain = site if site.startswith("http") else f"https://{site}"
    search_url = f"{base_domain}/widgets"
    
    for term in search_terms:
        offset = 0
        limit = 50
        while True:
            payload = {
                "lang": "en_global",
                "deviceType": "desktop",
                "country": "global",
                "pageName": "search-results",
                "ddoKey": "refineSearch",
                "from": offset,
                "jobs": True,
                "counts": True,
                "size": limit,
                "clearAll": False,
                "jdsource": "facets",
                "isSliderEnable": False,
                "pageId": "page23-4qmSjC", # Common Phenom ID
                "siteType": "external",
                "keywords": term,
                "global": True,
                "selected_fields": {},
                "locationData": {}
            }
            
            r = session.post(search_url, json=payload)
            if r is None or r.status_code != 200:
                break
                
            data = safe_json(r, f"phenom:{site}")
            # Phenom wraps search results in the ddoKey requested
            results = data.get("refineSearch", {}).get("data", {}).get("jobs", [])
            print(f"DEBUG: fetched {len(results)} jobs for term {term}")
            if not results:
                print("DEBUG NO RESULTS: ", data)
                break
                
            for p in results:
                job_id = p.get("jobId") or p.get("reqId")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                
                title = p.get("title") or ""
                if detail_prefilter and not detail_prefilter(title):
                    continue
                
                # Build detail page URL
                slug = re.sub(r'[^a-zA-Z0-9]+', '-', title).strip('-')
                detail_url = f"{base_domain}/global/en/job/{job_id}/{slug}"
                
                # Fallback URL if we can't fetch detail page
                final_url = detail_url
                if "applyUrl" in p and p["applyUrl"]:
                    final_url = p["applyUrl"] # often links directly to Workday
                
                # Fetch full description from the detail page HTML
                time.sleep(0.1) # polite spacing
                dr = session.get(detail_url)
                desc = p.get("descriptionTeaser", "")
                
                if dr is not None and dr.status_code == 200:
                    # Extract the embedded phApp.ddo JSON payload from HTML
                    m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\});\s*phApp\.", dr.text, re.DOTALL)
                    if m:
                        try:
                            ddo_data = json.loads(m.group(1))
                            full_desc = ddo_data.get("jobDetail", {}).get("data", {}).get("job", {}).get("description", "")
                            if full_desc:
                                desc = html_to_text(full_desc)
                        except json.JSONDecodeError:
                            pass
                
                # Sometimes location is structured, sometimes flat
                location = p.get("cityStateCountry") or p.get("location") or ""
                if not location:
                    location = p.get("city", "")
                    if p.get("state"):
                        location += f", {p['state']}"
                        
                posted = p.get("postedDate") or p.get("dateCreated") or ""
                # "2026-06-17T00:00:00.000+0000" -> "2026-06-17"
                if len(posted) >= 10:
                    posted = posted[:10]
                
                jobs.append(Job(
                    company=company_name,
                    title=title.strip(),
                    location=location.strip(),
                    url=final_url,
                    posted=posted,
                    description=desc.strip(),
                    job_id=job_id
                ))
            
            if len(results) < limit:
                break
            offset += limit
            
    return jobs
