import re
import time
from bs4 import BeautifulSoup
from .base import Job, PoliteSession, html_to_text

def parse_j2w_date(dt_str: str) -> str | None:
    """Parse Jobs2Web date format like '9 Jul 2026' into ISO '2026-07-09'."""
    dt_str = dt_str.strip()
    try:
        m = re.search(r'(\d+)\s+([a-zA-Z]+)\s+(\d{4})', dt_str)
        if m:
            day, month, year = m.groups()
            day = day.zfill(2)
            months = {
                "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
                "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
            }
            m_num = months.get(month[:3].lower())
            if m_num:
                return f"{year}-{m_num}-{day}"
    except Exception:
        pass
    return None

def fetch_successfactors(company_name: str, domain: str, session: PoliteSession,
                         search_terms: list[str], detail_prefilter=None,
                         detail_cache=None) -> list[Job]:
    """Scrape jobs from a SuccessFactors Jobs2Web careers portal."""
    jobs = []
    seen_urls = set()

    for term in search_terms:
        offset = 0
        while True:
            url = f"https://{domain}/search/?q={term}&startrow={offset}"
            try:
                r = session.get(url)
                if r is None or r.status_code != 200:
                    break
                
                soup = BeautifulSoup(r.text, 'html.parser')
                tables = soup.find_all('table')
                if not tables:
                    break
                
                table = tables[0]
                rows = table.find_all('tr')
                if len(rows) <= 2: # only header rows or empty
                    break
                
                # Determine column indices from header
                headers = []
                header_row = None
                for row in rows:
                    th_cells = row.find_all(['th', 'td'])
                    texts = [th.text.strip().lower() for th in th_cells]
                    if any("title" in t or "job" in t for t in texts):
                        headers = texts
                        header_row = row
                        break
                
                title_idx = 0
                loc_idx = 1
                date_idx = 3 # fallback to published column
                
                for idx, h in enumerate(headers):
                    if "title" in h or "job" in h:
                        title_idx = idx
                    elif "location" in h:
                        loc_idx = idx
                    elif "date" in h or "published" in h:
                        date_idx = idx

                jobs_found_on_page = 0
                for row in rows:
                    if row == header_row:
                        continue
                    
                    cells = row.find_all('td')
                    if len(cells) <= max(title_idx, loc_idx):
                        continue
                    
                    link_el = cells[title_idx].find('a')
                    if not link_el or not link_el.get('href') or not link_el.get('href').startswith('/job/'):
                        continue
                    
                    href = link_el.get('href')
                    job_url = f"https://{domain}{href}"
                    
                    if job_url in seen_urls:
                        continue
                    
                    title = link_el.text.strip()
                    if detail_prefilter and not detail_prefilter(title):
                        continue
                        
                    # Extract location and date
                    location = cells[loc_idx].text.strip()
                    date_str = ""
                    if len(cells) > date_idx:
                        date_str = cells[date_idx].text.strip()
                    posted = parse_j2w_date(date_str)
                    
                    # Fetch description (respecting detail cache)
                    desc = ""
                    from .. import cache as cache_mod
                    cached = cache_mod.get_cached(detail_cache, job_url) if detail_cache is not None else None
                    if cached:
                        desc = cached.get("description", "")
                    else:
                        time.sleep(0.2) # polite spacing
                        dr = session.get(job_url)
                        if dr is not None and dr.status_code == 200:
                            dsoup = BeautifulSoup(dr.text, 'html.parser')
                            desc_div = dsoup.find(class_='jobdescription')
                            if desc_div:
                                desc = html_to_text(str(desc_div))
                            else:
                                # Fallback to body text if class not found
                                desc = html_to_text(dr.text)
                            
                            if detail_cache is not None:
                                cache_mod.put_cached(detail_cache, job_url, desc, posted, False)
                    
                    seen_urls.add(job_url)
                    jobs_found_on_page += 1
                    
                    jobs.append(Job(
                        company=company_name,
                        title=title,
                        location=location,
                        url=job_url,
                        posted=posted,
                        description=desc,
                        source="successfactors"
                    ))
                
                if jobs_found_on_page == 0:
                    break
                offset += 25
            except Exception:
                break
                
            time.sleep(0.5)
            
    return jobs
