"""Hard runtime filters: level, location, recency. Applied before scoring."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import List, Optional

from .adapters.base import Job

# Level taxonomy — order matters (most specific first).
# Covers common real-world spellings: senior/sr/snr, manager/mgr, director/dir,
# associate/assoc, with or without periods.
_SR = r"(?:senior|sr|snr)\.?"
_MGR = r"(?:manager|mgr)\.?"
_DIR = r"(?:director|dir)\.?"
_ASSOC = r"(?:associate|assoc)\.?"

LEVEL_PATTERNS = [
    ("Senior Director", re.compile(rf"\b{_SR}\s+{_DIR}", re.I)),
    ("Associate Director",
     re.compile(rf"\b{_ASSOC}\s+{_DIR}|\bAD\b(?=[,\s]|$)", re.I)),
    ("Director", re.compile(r"\b(director|dir)\.?\b", re.I)),
    ("Senior Manager", re.compile(rf"\b{_SR}\s+{_MGR}", re.I)),
]

# Titles at or above these bands are out of the target range — excluded even
# though they contain "Director". Checked BEFORE level detection.
_ABOVE_BAND = re.compile(
    r"\b(executive|exec\.?)\s+(director|dir\.?)\b"
    r"|\b(vice\s+president|vp|svp|evp)\b"
    r"|\bchief\b|\bhead\s+of\b", re.I,
)

# Titles that pass level but are out of scope (sales-force, HR, facilities...).
EXCLUDE_TITLE = re.compile(
    r"\b(sales\s+rep|account\s+(manager|executive|director)|human\s+resources|"
    r"facilities|paralegal|counsel|nurse|physician|veterinar)\b", re.I,
)


def detect_level(title: str) -> Optional[str]:
    if _ABOVE_BAND.search(title):
        return None  # Executive Director, VP, Chief, Head of — above target band
    for name, pat in LEVEL_PATTERNS:
        if pat.search(title):
            return name
    return None


def title_prefilter(title: str) -> bool:
    """Cheap check used by adapters before fetching full descriptions."""
    return detect_level(title) is not None and not EXCLUDE_TITLE.search(title)


def location_ok(job: Job, allowed: List[str], allow_remote: bool = True) -> bool:
    loc = (job.location or "").lower()
    if allow_remote and (job.is_remote or "remote" in loc):
        return True
    return any(a.lower() in loc for a in allowed) if allowed else True


def recent_enough(job: Job, max_age_days: int) -> bool:
    """Unknown dates pass (better to surface than silently drop); '+' suffix
    marks a Workday lower bound like '30+ days' and fails strict windows."""
    if not job.posted:
        return True
    p = job.posted
    lower_bound = p.endswith("+")
    p = p.rstrip("+")
    try:
        posted = date.fromisoformat(p)
    except ValueError:
        return True
    if lower_bound and (date.today() - posted).days >= max_age_days:
        return False
    return posted >= date.today() - timedelta(days=max_age_days)


def apply_filters(jobs: List[Job], cfg: dict) -> List[Job]:
    out = []
    for j in jobs:
        j.level = detect_level(j.title)
        if j.level is None or EXCLUDE_TITLE.search(j.title):
            continue
        if not location_ok(j, cfg.get("locations", []), cfg.get("allow_remote", True)):
            continue
        if not recent_enough(j, cfg.get("max_age_days", 3)):
            continue
        out.append(j)
    return out
