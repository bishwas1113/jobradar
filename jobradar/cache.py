"""Persistent cache of job posting details (description, posted date, remote
flag), keyed by a stable per-posting key. Once a posting has been fetched,
subsequent runs reuse the cached copy instead of re-hitting the ATS detail
endpoint for it -- this is what "archiving daily searches" buys us: real
network savings and faster runs, not just a historical record.

TTL is intentionally a bit over 24h so one daily run always finds yesterday's
entries still valid, but stale entries older than that (e.g. a job that
disappeared and came back weeks later) get treated as fresh unknowns again.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

CACHE_TTL_HOURS = 40
_LOCK = threading.Lock()


def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        path.write_text(json.dumps(cache, indent=0))


def get_cached(cache: dict, key: str) -> dict | None:
    with _LOCK:
        entry = cache.get(key)
    if not entry:
        return None
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except Exception:
        return None
    age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    if age_hours > CACHE_TTL_HOURS:
        return None
    return entry


def put_cached(cache: dict, key: str, description: str, posted, is_remote: bool) -> None:
    with _LOCK:
        cache[key] = {
            "description": description,
            "posted": posted,
            "is_remote": is_remote,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
