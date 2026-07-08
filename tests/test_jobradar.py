import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobradar.adapters.base import Job, html_to_text
from jobradar.adapters.workday import parse_posted_on
from jobradar.filters import apply_filters, detect_level, title_prefilter
from jobradar.resume_parser import parse_resume
from jobradar.scoring import score_jobs


def test_level_detection():
    # Senior Manager — all common spellings
    for t in ["Senior Manager, Commercial Analytics", "Sr. Manager, Insights",
              "Sr Manager Data Strategy", "Snr Manager, Analytics",
              "Senior Mgr, Business Insights", "Sr. Mgr. Customer Analytics"]:
        assert detect_level(t) == "Senior Manager", t
    # Associate Director — all common spellings
    for t in ["Associate Director, Oncology Insights", "Assoc. Director, Analytics",
              "Assoc Director Commercial", "Associate Dir, Data Strategy",
              "Assoc. Dir. Insights", "AD, Business Insights"]:
        assert detect_level(t) == "Associate Director", t
    # Director / Senior Director
    assert detect_level("Director, HCP Analytics") == "Director"
    assert detect_level("Sr. Director Data Strategy") == "Senior Director"
    assert detect_level("Snr Director, Insights") == "Senior Director"
    # Above band — excluded even though they contain "Director"
    for t in ["Executive Director, Analytics", "Exec. Director, Insights",
              "VP, Commercial Analytics", "Vice President, Data Strategy",
              "Head of Analytics", "Chief Data Officer"]:
        assert detect_level(t) is None, t
    # Too junior — not matched
    for t in ["Senior Data Analyst", "Manager, Insights", "Analytics Manager"]:
        assert detect_level(t) is None, t


def test_prefilter_excludes_out_of_scope():
    assert title_prefilter("Associate Director, Business Insights")
    assert not title_prefilter("Account Director, Key Hospitals")
    assert not title_prefilter("Director, Human Resources")


def test_posted_on_parsing():
    assert parse_posted_on("Posted Today") == date.today().isoformat()
    assert parse_posted_on("Posted Yesterday") == (date.today() - timedelta(days=1)).isoformat()
    assert parse_posted_on("Posted 3 Days Ago") == (date.today() - timedelta(days=3)).isoformat()
    assert parse_posted_on("Posted 30+ Days Ago").endswith("+")
    assert parse_posted_on("") is None


def test_recency_and_location_filters():
    old = (date.today() - timedelta(days=10)).isoformat()
    fresh = date.today().isoformat()
    jobs = [
        Job("A", "Associate Director, Analytics", "Philadelphia, PA", "u1", posted=fresh),
        Job("B", "Associate Director, Analytics", "San Diego, CA", "u2", posted=fresh),
        Job("C", "Senior Manager, Insights", "Remote - US", "u3", posted=fresh),
        Job("D", "Associate Director, Analytics", "Philadelphia, PA", "u4", posted=old),
        Job("E", "Senior Analyst", "Philadelphia, PA", "u5", posted=fresh),
    ]
    out = apply_filters(jobs, {"locations": ["Philadelphia"], "allow_remote": True, "max_age_days": 3})
    assert [j.company for j in out] == ["A", "C"]
    assert out[0].level == "Associate Director"


def test_resume_parser_no_period_dependence():
    text = "EXPERIENCE\n- Built HCP segmentation using k-means clustering\n- Engineered features from IQVIA and Symphony Health data without any periods\nSKILLS\nPython, Tableau, SQL"
    p = parse_resume(text)
    assert len(p.bullets) == 2                      # newline-delimited, not period-split
    assert "iqvia" in p.skills and "k-means" in p.skills
    assert all("." not in b[-1] for b in p.bullets)


def test_scoring_orders_relevant_jobs_higher():
    profile = parse_resume(Path(__file__).parent.parent.joinpath("resume.txt").read_text())
    strong = Job("X", "Associate Director, Commercial Analytics", "Remote", "u1",
                 description="Lead commercial analytics using IQVIA claims data, HCP targeting, "
                             "segmentation, Python and Tableau dashboards for oncology launch")
    weak = Job("Y", "Director, Facilities Engineering Projects", "Remote", "u2",
               description="Oversee HVAC construction, building maintenance vendors and site safety")
    weak.level = "Director"; strong.level = "Associate Director"
    ranked = score_jobs([weak, strong], profile)
    assert ranked[0].title.startswith("Associate Director")
    assert ranked[0].score > ranked[1].score
    assert 0 <= ranked[1].score <= 100 and ranked[0].score <= 100
    assert "iqvia" in ranked[0].score_parts["matched_skills"]


def test_html_to_text_preserves_lines():
    txt = html_to_text("<p>Lead analytics</p><ul><li>Build models</li><li>Own KPIs</li></ul>")
    assert txt.splitlines() == ["Lead analytics", "Build models", "Own KPIs"]


def test_detail_cache_respects_ttl_and_freshness():
    from jobradar.cache import get_cached, put_cached, CACHE_TTL_HOURS
    from datetime import datetime, timezone, timedelta
    cache = {}
    put_cached(cache, "job-1", "a real description", "2026-07-01", False)
    assert get_cached(cache, "job-1")["description"] == "a real description"
    # Simulate a stale entry older than the TTL
    cache["job-2"] = {
        "description": "stale", "posted": "2026-06-01", "is_remote": False,
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS + 1)).isoformat(),
    }
    assert get_cached(cache, "job-2") is None
    assert get_cached(cache, "does-not-exist") is None


def test_slow_company_does_not_block_others():
    import time
    import jobradar.pipeline as pl
    from jobradar.adapters.base import Job

    def fake_fetch(c, terms, delay=1.0):
        if c["name"] == "SlowCorp":
            time.sleep(2.0)
            return c["name"], [], None
        return c["name"], [Job(c["name"], "Associate Director, Analytics", "Remote",
                                f"u-{c['name']}", posted="2026-07-05", description="analytics")], None

    original = pl.fetch_one_company
    pl.fetch_one_company = fake_fetch
    try:
        cfg = {"search_terms": ["analytics"], "companies": [
            {"name": "FastA", "ats": "greenhouse", "board": "x"},
            {"name": "SlowCorp", "ats": "greenhouse", "board": "x"},
            {"name": "FastB", "ats": "greenhouse", "board": "x"},
        ]}
        errors = []
        t0 = time.monotonic()
        jobs, _ = pl.fetch_all_parallel(cfg, errors, parse_resume(Path(__file__).parent.parent.joinpath("resume.txt").read_text()),
                                          3, {}, "2026-07-06T12:00:00+00:00",
                                          max_workers=4, first_pass_seconds=0.8, retry_seconds=0.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.8, f"Should not wait for SlowCorp's full 2s, took {elapsed:.2f}s"
        assert sorted(j.company for j in jobs) == ["FastA", "FastB"]
        assert any("SlowCorp" in e["company"] for e in errors)
    finally:
        pl.fetch_one_company = original


def test_workday_recovers_from_422_on_oversized_limit():
    import json as _json
    from jobradar.adapters import workday as W

    class FakeResp:
        def __init__(self, status, postings=None):
            self.status_code = status
            self._p = postings or []
            self.text = _json.dumps({"jobPostings": self._p}) if status == 200 else '{"httpStatus":422,"message":""}'
        def json(self):
            return {"jobPostings": self._p}

    class FakeSession:
        # Rejects limit>20 with 422 (real Jazz/Vertex behavior), accepts <=20
        def post(self, url, json=None, headers=None, **kw):
            if json.get("limit", 0) > 20:
                return FakeResp(422)
            return FakeResp(200, postings=[])
        def get(self, url, **kw):
            return FakeResp(200)

    r, used_empty = W._wd_post_resilient(FakeSession(), "http://x", "analytics", 0, 100)
    assert r is not None and r.status_code == 200, "should recover from oversized-limit 422"


def test_workday_csrf_warmup_recovers_422():
    import json as _json
    from jobradar.adapters import workday as W

    class FakeResp:
        def __init__(self, status, postings=None):
            self.status_code = status
            self._p = postings if postings is not None else []
            self.text = _json.dumps({"jobPostings": self._p}) if status == 200 else '{"httpStatus":422}'
        def json(self): return {"jobPostings": self._p, "jobPostingInfo": {}}

    class FakeCookies(dict):
        def get(self, k): return dict.get(self, k)
    class FakeInner:
        def __init__(self): self.cookies = FakeCookies()
    class FakeSession:
        def __init__(self): self.s = FakeInner()
        def get(self, url, **kw):
            if "/en-US/" in url and "/job/" not in url:
                self.s.cookies["CALYPSO_CSRF_TOKEN"] = "t"
            return FakeResp(200)
        def post(self, url, headers=None, json=None, **kw):
            if not headers or headers.get("X-CALYPSO-CSRF-TOKEN") != "t":
                return FakeResp(422)
            return FakeResp(200, postings=[{"externalPath": "/job/X",
                "title": "Associate Director, Analytics", "locationsText": "Remote",
                "postedOn": "Posted Today"}] if json.get("offset", 0) == 0 else [])

    jobs = W.fetch_workday("X", "t1", "wd1", "Site", FakeSession(),
                            search_terms=["analytics"], detail_prefilter=None, detail_cache={})
    assert len(jobs) == 1 and jobs[0].title.startswith("Associate Director")


def test_workday_csrf_token_extracted_from_page_html():
    """Tenants that set the CSRF cookie via JavaScript send no Set-Cookie header;
    the token is embedded in the landing page HTML. Verify we extract it and
    satisfy a tenant requiring BOTH matching cookie and header."""
    import json as _json
    from jobradar.adapters import workday as W

    class FakeResp:
        def __init__(self, status, postings=None, text=None):
            self.status_code = status
            self._p = postings if postings is not None else []
            self.text = text if text is not None else (
                _json.dumps({"jobPostings": self._p}) if status == 200 else '{"httpStatus":422}')
        def json(self): return {"jobPostings": self._p, "jobPostingInfo": {}}

    class FakeCookies(dict):
        def get(self, k): return dict.get(self, k)
        def set(self, k, v, domain=None): self[k] = v
    class FakeInner:
        def __init__(self): self.cookies = FakeCookies()
    class FakeSession:
        def __init__(self): self.s = FakeInner()
        def get(self, url, **kw):
            if "/en-US/" in url and "/job/" not in url:
                return FakeResp(200, text='<script>{"csrfToken":"tok-x"}</script>')
            return FakeResp(200)
        def post(self, url, headers=None, json=None, **kw):
            ok = (self.s.cookies.get("CALYPSO_CSRF_TOKEN") == "tok-x"
                  and headers and headers.get("X-CALYPSO-CSRF-TOKEN") == "tok-x")
            if not ok:
                return FakeResp(422)
            return FakeResp(200, postings=[{"externalPath": "/job/Y",
                "title": "Senior Manager, Analytics", "locationsText": "Remote",
                "postedOn": "Posted Today"}] if json.get("offset", 0) == 0 else [])

    jobs = W.fetch_workday("X", "t2", "wd5", "Site", FakeSession(),
                            search_terms=["analytics"], detail_prefilter=None, detail_cache={})
    assert len(jobs) == 1 and "Senior Manager" in jobs[0].title


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
