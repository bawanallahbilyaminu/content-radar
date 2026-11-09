"""arXiv Atom API: newest submissions in the configured categories (title + abstract only)."""

from __future__ import annotations

import feedparser

from content_radar.collectors.base import HttpClient, clean_text, struct_time_to_datetime
from content_radar.config import Settings
from content_radar.models import Item

API = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL", "cs.CR"]


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("arxiv")
    categories = cfg.get("categories") or DEFAULT_CATEGORIES
    query = " OR ".join(f"cat:{c}" for c in categories)
    text = client.get_text(
        API,
        params={
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": cfg.limit,
        },
    )
    feed = feedparser.parse(text)
    items: list[Item] = []
    for entry in feed.entries:
        link = entry.get("link") or entry.get("id")
        if not link or not entry.get("title"):
            continue
        cats = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
        items.append(
            Item(
                source="arxiv",
                url=link,
                title=clean_text(entry["title"]),
                summary=clean_text(entry.get("summary", ""))[:600],
                published_at=struct_time_to_datetime(entry.get("published_parsed")),
                engagement={},
                kind="paper",
                niche_tags=_tags_from_categories(cats),
            )
        )
    return items


def _tags_from_categories(cats: list[str]) -> list[str]:
    tags: list[str] = []
    if any(c.startswith("cs.CR") for c in cats):
        tags.append("cyber")
    if any(c in {"cs.AI", "cs.LG", "cs.CL", "cs.CV", "stat.ML"} for c in cats):
        tags.append("ai_ml")
    return tags
