"""Data model for one trending repo, merged across sources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

SOURCE_NAMES = ("trending_daily", "trending_weekly", "ossinsight_day", "ossinsight_week", "search")


class TrendingRepo(BaseModel):
    """A repo seen trending by one or more sources. Numbers are None when unknown."""

    full_name: str = Field(description="owner/name, as GitHub spells it")
    description: str = ""
    language: str | None = None
    stars: int | None = None
    forks: int | None = None
    stars_today: int | None = Field(default=None, description="stars gained in the last day")
    stars_week: int | None = Field(default=None, description="stars gained in the last 7 days")
    sources: set[str] = Field(default_factory=set)

    # Filled in by enrichment (GitHub REST /repos/{full_name}).
    license: str | None = None
    created_at: datetime | None = None
    pushed_at: datetime | None = None
    archived: bool = False
    topics: list[str] = Field(default_factory=list)
    enriched: bool = False

    # Filled in by scoring and the sink.
    flags: list[str] = Field(default_factory=list)
    streak: int = 1
    days_seen: int = 1
    momentum: int = 0

    @property
    def key(self) -> str:
        return self.full_name.lower()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    def merge(self, other: TrendingRepo) -> None:
        """Fold another sighting of the same repo into this one (in place)."""
        self.sources |= other.sources
        if not self.description and other.description:
            self.description = other.description
        if not self.language and other.language:
            self.language = other.language
        for field in ("stars", "forks", "stars_today", "stars_week"):
            mine, theirs = getattr(self, field), getattr(other, field)
            if theirs is not None and (mine is None or theirs > mine):
                setattr(self, field, theirs)
