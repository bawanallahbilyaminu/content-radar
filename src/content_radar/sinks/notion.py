"""Notion sink: create one page per new Idea in the Ideas database. Create-only.

Dedupe against existing rows by normalized title + primary source URL. Never
updates or deletes rows: once a human touches a row it is theirs.

Uses Notion API 2025-09-03 (notion-client 3.x): a database owns one or more data
sources; pages are created under the data source and queries run against it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol

from content_radar.models import Idea, canonical_url, normalize_title
from content_radar.timeutil import today_local

log = logging.getLogger(__name__)

STATUS_OPTIONS = ["New", "Shortlisted", "Scripting", "Produced", "Posted", "Rejected"]
ANGLE_EVIDENCE_OPTIONS = ["sourced", "assumed", "none"]

# Property schema shared by the creation script and the sink.
PROPERTIES: dict[str, dict[str, Any]] = {
    "Title": {"title": {}},
    "Hook": {"rich_text": {}},
    "Angle": {"rich_text": {}},
    "Angle Evidence": {"select": {"options": [{"name": o} for o in ANGLE_EVIDENCE_OPTIONS]}},
    "Angle Source": {"url": {}},
    "Format": {"select": {"options": [{"name": o} for o in ("news", "tutorial", "explainer")]}},
    "Length": {"select": {"options": [{"name": o} for o in ("short", "long")]}},
    "Niche": {"select": {"options": [{"name": o} for o in ("ai_ml", "data", "cyber", "other")]}},
    "Why Now": {"rich_text": {}},
    "Demand Signal": {"rich_text": {}},
    "Sources": {"rich_text": {}},
    "Primary Source": {"url": {}},
    "Score": {"number": {"format": "number"}},
    "Effort": {"select": {"options": [{"name": o} for o in ("low", "med", "high")]}},
    "Evergreen": {"checkbox": {}},
    "Breaking": {"checkbox": {}},
    "Status": {"select": {"options": [{"name": o} for o in STATUS_OPTIONS]}},
    "Date Found": {"date": {}},
}


class NotionLike(Protocol):
    """The slice of notion_client.Client we use. Tests supply a fake."""

    databases: Any
    data_sources: Any
    pages: Any


def _rt(text: str) -> dict[str, Any]:
    text = (text or "")[:1900]  # Notion caps a rich_text block at 2000 chars
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _select(name: str) -> dict[str, Any]:
    return {"select": {"name": name}}


def idea_to_properties(idea: Idea, found_on: str) -> dict[str, Any]:
    sources_text = "\n".join(
        f"{url} — {idea.source_notes.get(str(url), '')}".rstrip(" —") for url in idea.sources
    )
    props: dict[str, Any] = {
        "Title": {"title": [{"type": "text", "text": {"content": idea.title[:200]}}]},
        "Hook": _rt(idea.hook),
        "Angle": _rt(idea.angle),
        "Angle Evidence": _select(idea.angle_evidence),
        "Angle Source": {"url": str(idea.angle_source) if idea.angle_source else None},
        "Format": _select(idea.format),
        "Length": _select(idea.length),
        "Niche": _select(idea.niche),
        "Why Now": _rt(idea.why_now),
        "Demand Signal": _rt(idea.demand_signal),
        "Sources": _rt(sources_text),
        "Primary Source": {"url": str(idea.sources[0])},
        "Score": {"number": idea.score},
        "Effort": _select(idea.effort),
        "Evergreen": {"checkbox": idea.evergreen},
        "Breaking": {"checkbox": idea.breaking},
        "Status": _select("New"),
        "Date Found": {"date": {"start": found_on}},
    }
    return props


def _plain(prop: dict[str, Any]) -> str:
    kind = prop.get("type")
    if kind == "title":
        return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    if kind == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))
    if kind == "url":
        return prop.get("url") or ""
    return ""


def row_key(title: str, primary_url: str) -> str:
    return f"{normalize_title(title)}|{canonical_url(primary_url) if primary_url else ''}"


class NotionSink:
    def __init__(self, client: NotionLike, database_id: str) -> None:
        self.client = client
        self.database_id = database_id
        self._data_source_id: str | None = None

    @property
    def data_source_id(self) -> str:
        if self._data_source_id is None:
            db = self.client.databases.retrieve(database_id=self.database_id)
            sources = db.get("data_sources") or []
            if not sources:
                raise RuntimeError(f"Notion database {self.database_id} has no data sources")
            self._data_source_id = sources[0]["id"]
        return self._data_source_id

    def iter_rows(self) -> Iterable[dict[str, Any]]:
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {"data_source_id": self.data_source_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            page = self.client.data_sources.query(**kwargs)
            yield from page.get("results", [])
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")

    def existing_keys(self) -> set[str]:
        keys: set[str] = set()
        for row in self.iter_rows():
            props = row.get("properties", {})
            title = _plain(props.get("Title", {}))
            primary = _plain(props.get("Primary Source", {}))
            if not primary:
                # Fall back to the first URL in the Sources text.
                primary = _plain(props.get("Sources", {})).split()[0:1] or [""]
                primary = primary[0]
            keys.add(row_key(title, primary))
        return keys

    def write(self, ideas: list[Idea]) -> list[Idea]:
        """Create a page for every idea not already present. Returns the ideas created."""
        existing = self.existing_keys()
        found_on = today_local()
        created: list[Idea] = []
        for idea in ideas:
            key = row_key(idea.title, str(idea.sources[0]))
            if key in existing:
                log.info("notion: skip duplicate '%s'", idea.title[:60])
                continue
            self.client.pages.create(
                parent={"type": "data_source_id", "data_source_id": self.data_source_id},
                properties=idea_to_properties(idea, found_on),
            )
            existing.add(key)
            created.append(idea)
            log.info("notion: created '%s' (score %d)", idea.title[:60], idea.score)
        log.info(
            "notion: %d created, %d skipped as duplicates", len(created), len(ideas) - len(created)
        )
        return created


def make_sink(token: str, database_id: str) -> NotionSink:
    from notion_client import Client

    return NotionSink(Client(auth=token), database_id)
