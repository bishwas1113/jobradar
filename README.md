# JobRadar

Daily automated scan of biopharma / biotech / AgTech career sites (Workday,
Greenhouse, Lever — direct ATS endpoints, no job boards), scored against your
resume, served as a static Japandi dashboard on GitHub Pages.

## Setup (once)
1. Create a GitHub repo, push this folder
2. Replace `resume.txt` with your real Commercial Analytics resume
3. `pip install -r requirements.txt`
4. `python -m jobradar.verify_config` — fixes wrong Workday tenant IDs before first run
5. Repo Settings → Pages → deploy from branch, folder `/docs`
6. Repo Settings → Actions → General → Workflow permissions → "Read and write"

The workflow in `.github/workflows/daily.yml` runs every morning at 7:20am ET,
commits `docs/data/matches.json`, and Pages serves the dashboard.

Page link: https://bishwas1113.github.io/jobradar/

## Local run
    python -m jobradar.pipeline --max-age-days 3
    python -m jobradar.pipeline --fixtures tests/fixtures   # offline demo
    python tests/test_jobradar.py

## Tuning
- `companies.yaml`: companies, ATS tenants, Workday search terms
- `jobradar/filters.py`: level regex, exclusion titles
- `jobradar/resume_parser.py`: SKILL_LEXICON weights
- `jobradar/scoring.py`: blend weights (semantic .55 / skills .30 / level .15)
