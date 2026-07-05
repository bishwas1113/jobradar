"""Resume parser.

Design constraint from the owner: consulting-format resume, bullets carry NO
terminal punctuation. Tokenization is therefore strictly newline-delimited —
each non-empty line is one atomic unit. Never split on periods.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

BULLET_PREFIX = re.compile(r"^\s*[-•·▪‣o*]\s+")
SECTION_HINT = re.compile(
    r"^(experience|education|skills|summary|projects|publications|certifications)\b", re.I
)

# Domain skill lexicon — phrase-level, matched case-insensitively in both
# resume and JD text. Weighted: 2.0 = core differentiators, 1.0 = standard.
SKILL_LEXICON = {
    # data & analytics core
    "commercial analytics": 2.0, "business insights": 2.0, "customer analytics": 2.0,
    "data strategy": 2.0, "hcp targeting": 2.0, "segmentation": 1.5,
    "forecasting": 1.0, "market research": 1.0, "competitive intelligence": 1.0,
    "data visualization": 1.0, "dashboards": 1.0, "kpi": 1.0,
    "strategic insights": 1.5, "market landscaping": 1.0, "portfolio analytics": 1.5,
    "brand strategy": 1.5, "predictive analytics": 1.5, "descriptive analytics": 1.0,
    "pre-launch analytics": 1.5, "data architecture": 1.5, "operating model": 1.0,
    # pharma data assets
    "iqvia": 2.0, "symphony health": 2.0, "claims data": 1.5, "veeva": 1.5,
    "salesforce": 1.0, "crm": 1.0, "real-world data": 1.5, "real world evidence": 1.5,
    "rwd": 1.5, "digital engagement": 1.0, "npi": 1.0, "icd10": 1.0,
    "trx": 1.5, "nrx": 1.5, "patient journey": 1.5, "launch": 1.0,
    "market access": 1.0, "payer": 1.0, "oncology": 1.5, "vaccines": 1.0,
    "rare disease": 1.0, "cardiovascular": 1.0,
    # technical
    "python": 1.5, "pandas": 1.0, "sql": 1.5, "tableau": 1.5, "power bi": 1.0,
    "powerbi": 1.0, "biopython": 1.0, "machine learning": 1.5, "ai/ml": 1.5,
    "generative ai": 1.5, "clustering": 1.0, "k-means": 1.0, "regression": 1.0,
    "feature engineering": 2.0, "bioinformatics": 1.5, "genomics": 1.0,
    "nlp": 1.0, "statistics": 1.0, "statistical analysis": 1.0, "a/b testing": 1.0, "etl": 1.0,
    # R&D / pipeline
    "r&d": 1.0, "pipeline": 1.0, "clinical trial": 1.0, "drug discovery": 1.0,
    "portfolio strategy": 1.5, "due diligence": 1.0, "biotech": 1.0,
    # leadership / consulting
    "management consulting": 1.5, "stakeholder": 1.0, "cross-functional": 1.0,
    "phd": 1.5, "strategy": 1.0, "roadmap": 1.0, "product management": 1.0,
}

# Defensible synonym normalization. If any source phrase appears in a text,
# the canonical lexicon term is credited too. Applied symmetrically to resume
# and JD so genuine equivalents match. Deliberately conservative — each pairing
# is a claim you could defend in an interview, not keyword stuffing.
ALIASES = {
    "business insights": ["strategic insights", "commercial insights", "commercial analytics"],
    "customer analytics": ["hcp targeting", "hcp engagement", "patient journey", "field analytics"],
    "data strategy": ["data architecture", "data operating model", "operating model and underlying data"],
    "predictive analytics": ["predictive model", "scoring model", "engagement scoring"],
    "machine learning": ["ai/ml", "ai/ml powered", "ml powered"],
    "real-world data": ["rwd", "real world data"],
    "power bi": ["powerbi"],
}


@dataclass
class ResumeProfile:
    lines: List[str] = field(default_factory=list)   # atomic newline units
    bullets: List[str] = field(default_factory=list)  # experience bullets only
    skills: dict = field(default_factory=dict)        # matched lexicon terms -> weight
    full_text: str = ""


def extract_skills(text: str) -> dict:
    low = text.lower()
    found = {term: w for term, w in SKILL_LEXICON.items() if term in low}
    # Credit canonical terms when a defensible synonym is present.
    for canonical, sources in ALIASES.items():
        if canonical in found:
            continue
        if any(s in low for s in sources):
            found[canonical] = SKILL_LEXICON.get(canonical, 1.0)
    return found


def parse_resume(text: str) -> ResumeProfile:
    profile = ResumeProfile(full_text=text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        profile.lines.append(line)
        stripped = BULLET_PREFIX.sub("", line)
        # A "bullet" is any bulleted line, or an unbulleted line long enough
        # to be a statement rather than a header (no period reliance).
        if BULLET_PREFIX.match(line) or (len(stripped.split()) >= 6 and not SECTION_HINT.match(line)):
            profile.bullets.append(stripped)
    profile.skills = extract_skills(text)
    return profile
