"""Match Confidence Score (0-100).

Blend of three signals:
  semantic (55%) — cosine similarity between resume text and JD text.
      Uses sentence-transformers embeddings when the model is available
      (GitHub Actions runner); falls back transparently to TF-IDF cosine
      so the pipeline never hard-fails on the ML dependency.
  skills   (30%) — weighted overlap of lexicon terms present in BOTH
      resume and JD, normalized by terms the JD asks for.
  level    (15%) — how well the title's seniority matches the target band.

Semantic scores are re-mapped from their practical range into 0-1 so the
final number spreads usefully instead of clustering at 40-60.
"""
from __future__ import annotations

from typing import List, Optional

from .adapters.base import Job
from .resume_parser import ResumeProfile, extract_skills

_LEVEL_FIT = {
    "Senior Manager": 1.0,
    "Associate Director": 1.0,
    "Director": 0.8,        # stretch band per two-year plan
    "Senior Director": 0.5,  # visible but honestly flagged as a reach
}

_EMBEDDER = None
_EMBED_TRIED = False


def _get_embedder():
    global _EMBEDDER, _EMBED_TRIED
    if not _EMBED_TRIED:
        _EMBED_TRIED = True
        try:
            from sentence_transformers import SentenceTransformer
            _EMBEDDER = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            _EMBEDDER = None
    return _EMBEDDER


def _semantic_batch(resume_text: str, jd_texts: List[str]) -> List[float]:
    model = _get_embedder()
    if model is not None:
        import numpy as np
        vecs = model.encode([resume_text] + jd_texts, normalize_embeddings=True)
        sims = (vecs[1:] @ vecs[0]).tolist()
        # MiniLM resume-vs-JD sims live roughly in [0.2, 0.75]; stretch to 0-1.
        return [max(0.0, min(1.0, (s - 0.2) / 0.55)) for s in sims]
    # TF-IDF fallback
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=20000)
    m = vec.fit_transform([resume_text] + jd_texts)
    sims = cosine_similarity(m[0], m[1:]).ravel().tolist()
    # TF-IDF sims for this task live roughly in [0.02, 0.35]; stretch to 0-1.
    return [max(0.0, min(1.0, (s - 0.02) / 0.33)) for s in sims]


def _skill_score(resume_skills: dict, jd_text: str) -> tuple[float, list]:
    jd_skills = extract_skills(jd_text)
    if not jd_skills:
        return 0.5, []  # JD mentions nothing in lexicon: neutral, not zero
    overlap = {t: w for t, w in jd_skills.items() if t in resume_skills}
    score = sum(overlap.values()) / sum(jd_skills.values())
    return score, sorted(overlap, key=overlap.get, reverse=True)


def score_jobs(jobs: List[Job], profile: ResumeProfile, weights: dict | None = None, level_fit: dict | None = None) -> List[Job]:
    if not jobs:
        return jobs
    if weights is None:
        weights = {"semantic": 0.55, "skills": 0.30, "level": 0.15}
    if level_fit is None:
        level_fit = _LEVEL_FIT

    jd_texts = [f"{j.title}\n{j.description}" for j in jobs]
    semantic = _semantic_batch(profile.full_text, jd_texts)
    for j, jd_text, sem in zip(jobs, jd_texts, semantic):
        skill, matched = _skill_score(profile.skills, jd_text)
        level = level_fit.get(j.level or "", 0.4)
        final = 100 * (weights.get("semantic", 0.55) * sem + 
                       weights.get("skills", 0.30) * skill + 
                       weights.get("level", 0.15) * level)
        j.score = round(final, 1)
        j.score_parts = {
            "semantic": round(sem, 3),
            "skills": round(skill, 3),
            "level_fit": level,
            "matched_skills": matched[:8],
        }
    jobs.sort(key=lambda x: x.score or 0, reverse=True)
    return jobs
