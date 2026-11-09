"""Core data models: Item (a collected signal) and Idea (a ranked video idea)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, HttpUrl, field_validator

ItemKind = Literal["trend", "question", "paper", "vuln", "repo", "video"]
IdeaFormat = Literal["news", "tutorial", "explainer"]
IdeaLength = Literal["short", "long"]
Niche = Literal["ai_ml", "data", "cyber", "other"]
Effort = Literal["low", "med", "high"]

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "ref_src",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "source",
}
_WORD_RE = re.compile(r"[^a-z0-9]+")


def canonical_url(url: str) -> str:
    """Lower-case host, drop fragment and tracking params, strip trailing slash."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace for dedupe."""
    return _WORD_RE.sub(" ", title.lower()).strip()


class Item(BaseModel):
    """One collected signal from a source, before scoring."""

    source: str = Field(description="Collector name, e.g. 'hackernews', 'rss:techcabal'")
    url: HttpUrl
    title: str = Field(min_length=1)
    summary: str = ""
    published_at: datetime | None = None
    engagement: dict[str, float] = Field(default_factory=dict)
    kind: ItemKind = "trend"
    niche_tags: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        return " ".join(str(v).split())

    @field_validator("published_at")
    @classmethod
    def _ensure_tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    @property
    def canonical(self) -> str:
        return canonical_url(str(self.url))

    def fingerprint(self) -> str:
        """Key for in-run dedupe: normalized title plus canonical URL."""
        return f"{normalize_title(self.title)}|{self.canonical}"

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.published_at is None:
            return None
        now = now or datetime.now(UTC)
        return max(0.0, (now - self.published_at).total_seconds() / 3600)


class Idea(BaseModel):
    """A ranked video idea produced by the synthesis step."""

    title: str = Field(min_length=1, max_length=200)
    hook: str = Field(description="First 3 seconds of the video")
    angle: str = Field(description="Nigeria / Africa angle, or 'none'")
    format: IdeaFormat
    length: IdeaLength
    niche: Niche
    why_now: str
    demand_signal: str
    sources: list[HttpUrl] = Field(min_length=1)
    score: int = Field(ge=0, le=100)
    effort: Effort
    evergreen: bool = False
    breaking: bool = False

    @property
    def primary_source(self) -> str:
        return canonical_url(str(self.sources[0]))

    def dedupe_key(self) -> str:
        return f"{normalize_title(self.title)}|{self.primary_source}"
