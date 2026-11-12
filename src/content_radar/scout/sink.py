"""Notion sink for the repo scout: one row per repo in its own database.

Unlike the Ideas sink this one upserts, but only the bot's own columns. The human
columns (Pick, Episode, Verdict, Notes) are never written, not even on create, so
anything typed there is safe.

Rows are found by an exact title match on Repo (owner/name), one query per repo.
"""

from __future__ import annotations

import logging
from typing import Any

from content_radar.scout.models import SOURCE_NAMES, TrendingRepo
from content_radar.scout.rank import FLAG_OPTIONS, next_streak
from content_radar.sinks.notion import NotionLike
from content_radar.timeutil import today_local

log = logging.getLogger(__name__)

PICK_OPTIONS = ["Deep", "Quick", "Radar", "Skip"]
VERDICT_OPTIONS = ["Worth it", "Wait", "Skip", "Not yet tested", "Needs GPU, not run"]

BOT_PROPERTIES: dict[str, dict[str, Any]] = {
    "Repo": {"title": {}},
    "URL": {"url": {}},
    "Description": {"rich_text": {}},
    "Language": {"rich_text": {}},
    "Stars": {"number": {"format": "number_with_commas"}},
    "Forks": {"number": {"format": "number_with_commas"}},
    "Stars This Week": {"number": {"format": "number_with_commas"}},
    "Stars Today": {"number": {"format": "number_with_commas"}},
    "Momentum": {"number": {"format": "number"}},
    "Streak": {"number": {"format": "number"}},
    "Days Seen": {"number": {"format": "number"}},
    "First Seen": {"date": {}},
    "Last Seen": {"date": {}},
    "Sources": {"multi_select": {"options": [{"name": s} for s in SOURCE_NAMES]}},
    "Flags": {"multi_select": {"options": [{"name": f} for f in FLAG_OPTIONS]}},
    "License": {"rich_text": {}},
    "Created": {"date": {}},
    "Last Push": {"date": {}},
}

HUMAN_PROPERTIES: dict[str, dict[str, Any]] = {
    "Pick": {"select": {"options": [{"name": o} for o in PICK_OPTIONS]}},
    "Episode": {"number": {"format": "number"}},
    "Verdict": {"select": {"options": [{"name": o} for o in VERDICT_OPTIONS]}},
    "Notes": {"rich_text": {}},
}

PROPERTIES: dict[str, dict[str, Any]] = {**BOT_PROPERTIES, **HUMAN_PROPERTIES}


def _rt(text: str | None) -> dict[str, Any]:
    text = (text or "")[:1900]
    return {"rich_text": [{"type": "text", "text": {"content": text}}] if text else []}


def _num(value: int | None) -> dict[str, Any]:
    return {"number": value}


def _date(value: str | None) -> dict[str, Any]:
    return {"date": {"start": value} if value else None}


def repo_to_properties(repo: TrendingRepo, first_seen: str, last_seen: str) -> dict[str, Any]:
    """Bot-owned columns only."""
    return {
        "Repo": {"title": [{"type": "text", "text": {"content": repo.full_name[:200]}}]},
        "URL": {"url": repo.url},
        "Description": _rt(repo.description),
        "Language": _rt(repo.language),
        "Stars": _num(repo.stars),
        "Forks": _num(repo.forks),
        "Stars This Week": _num(repo.stars_week),
        "Stars Today": _num(repo.stars_today),
        "Momentum": _num(repo.momentum),
        "Streak": _num(repo.streak),
        "Days Seen": _num(repo.days_seen),
        "First Seen": _date(first_seen),
        "Last Seen": _date(last_seen),
        "Sources": {"multi_select": [{"name": s} for s in sorted(repo.sources)]},
        "Flags": {"multi_select": [{"name": f} for f in repo.flags]},
        "License": _rt(repo.license),
        "Created": _date(repo.created_at.date().isoformat() if repo.created_at else None),
        "Last Push": _date(repo.pushed_at.date().isoformat() if repo.pushed_at else None),
    }


def _date_of(props: dict[str, Any], key: str) -> str | None:
    value = (props.get(key) or {}).get("date") or {}
    start = value.get("start") if isinstance(value, dict) else None
    return start[:10] if start else None


def _number_of(props: dict[str, Any], key: str) -> int:
    value = (props.get(key) or {}).get("number")
    return int(value) if value is not None else 0


class RepoSink:
    def __init__(self, client: NotionLike, database_id: str) -> None:
        self.client = client
        self.database_id = database_id
        self._data_source_id: str | None = None
        self._rows: dict[str, dict[str, Any] | None] = {}

    @property
    def data_source_id(self) -> str:
        if self._data_source_id is None:
            db = self.client.databases.retrieve(database_id=self.database_id)
            sources = db.get("data_sources") or []
            if not sources:
                raise RuntimeError(f"Notion database {self.database_id} has no data sources")
            self._data_source_id = sources[0]["id"]
        return self._data_source_id

    def ensure_schema(self) -> list[str]:
        """Add any missing column. Never removes or renames."""
        ds = self.client.data_sources.retrieve(data_source_id=self.data_source_id)
        have = set((ds.get("properties") or {}).keys())
        missing = {k: v for k, v in PROPERTIES.items() if k not in have}
        if missing:
            self.client.data_sources.update(data_source_id=self.data_source_id, properties=missing)
            log.info("notion repos: added missing properties %s", sorted(missing))
        return sorted(missing)

    def find(self, full_name: str) -> dict[str, Any] | None:
        """The existing row for owner/name, or None. Cached per run."""
        key = full_name.lower()
        if key not in self._rows:
            page = self.client.data_sources.query(
                data_source_id=self.data_source_id,
                filter={"property": "Repo", "title": {"equals": full_name}},
                page_size=5,
            )
            match = None
            for row in page.get("results", []):
                title = (row.get("properties", {}).get("Repo") or {}).get("title") or []
                text = "".join(t.get("plain_text", "") for t in title)
                if text.lower() == key:
                    match = row
                    break
            self._rows[key] = match
        return self._rows[key]

    def load_history(self, repos: list[TrendingRepo], today: str | None = None) -> None:
        """Set streak and days_seen on each repo from its existing row."""
        today = today or today_local()
        for repo in repos:
            row = self.find(repo.full_name)
            if row is None:
                repo.streak, repo.days_seen = 1, 1
                continue
            props = row.get("properties", {})
            streak, new_day = next_streak(
                _date_of(props, "Last Seen"), _number_of(props, "Streak"), today
            )
            repo.streak = streak
            repo.days_seen = _number_of(props, "Days Seen") + (1 if new_day else 0) or 1

    def write(self, repos: list[TrendingRepo], today: str | None = None) -> tuple[int, int]:
        """Create or update one row per repo. Returns (created, updated)."""
        today = today or today_local()
        self.ensure_schema()
        created = updated = 0
        for repo in repos:
            row = self.find(repo.full_name)
            if row is None:
                self.client.pages.create(
                    parent={"type": "data_source_id", "data_source_id": self.data_source_id},
                    properties=repo_to_properties(repo, first_seen=today, last_seen=today),
                )
                created += 1
            else:
                first = _date_of(row.get("properties", {}), "First Seen") or today
                self.client.pages.update(
                    page_id=row["id"],
                    properties=repo_to_properties(repo, first_seen=first, last_seen=today),
                )
                updated += 1
        log.info("notion repos: %d created, %d updated", created, updated)
        return created, updated


def make_repo_sink(token: str, database_id: str) -> RepoSink:
    from notion_client import Client

    return RepoSink(Client(auth=token), database_id)
