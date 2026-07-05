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
    assert detect_level("Associate Director, Oncology Insights") == "Associate Director"
    assert detect_level("Senior Manager, Commercial Analytics") == "Senior Manager"
    assert detect_level("Sr. Director Data Strategy") == "Senior Director"
    assert detect_level("Director, HCP Analytics") == "Director"
    assert detect_level("Senior Data Analyst") is None
    assert detect_level("Manager, Insights") is None


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
