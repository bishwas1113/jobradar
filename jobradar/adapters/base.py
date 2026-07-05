"""Shared Job model and polite HTTP session for all ATS adapters."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

import requests

USER_AGENT = "JobRadar/1.0 (personal job-search tool; low request volume)"


@dataclass
class Job:
    company: str
    title: str
    location: str
    url: str                      # direct company ATS application link
    posted: Optional[str] = None  # ISO date string when known
    description: str = ""         # plain text
    source: str = ""              # adapter name
    job_id: str = ""
    score: Optional[float] = None
    score_parts: dict = field(default_factory=dict)
    level: Optional[str] = None
    is_remote: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        # Trim description for output payloads; scoring uses full text upstream.
        d["description"] = (self.description or "")[:600]
        return d


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def html_to_text(html: str) -> str:
    """Strip HTML to newline-preserving plain text (no sentence assumptions)."""
    if not html:
        return ""
    text = re.sub(r"(?i)</(p|li|div|h[1-6]|br)>|<br\s*/?>", "\n", html)
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


class PoliteSession:
    """Sequential requests with spacing and backoff; one failure never kills a run."""

    def __init__(self, delay: float = 1.0, timeout: int = 30, max_retries: int = 2):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self._last = 0.0

    def _wait(self):
        gap = self.delay - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def request(self, method: str, url: str, **kw) -> Optional[requests.Response]:
        for attempt in range(self.max_retries + 1):
            self._wait()
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException:
                if attempt == self.max_retries:
                    return None
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            return r
        return None

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


def today_iso() -> str:
    return date.today().isoformat()
