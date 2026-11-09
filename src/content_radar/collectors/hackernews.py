"""Hacker News via the Algolia search API: front page, Ask HN, and niche keyword searches."""

from __future__ import annotations

from content_radar.collectors.base import HttpClient, parse_datetime
from content_radar.config import Settings
from content_radar.models import Item
from content_radar.normalize import Tagger
from content_radar.timeutil import now_local, to_local

API = "https://hn.algolia.com/api/v1"
DEFAULT_QUERIES = ["AI", "LLM", "machine learning", "cybersecurity", "vulnerability", "data breach"]


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("hackernews")
    tagger = Tagger(settings.niche)
    min_points = int(cfg.get("min_points", 20))
    ask_min_points = int(cfg.get("ask_min_points", 5))
    since = int(now_local().timestamp()) - settings.run.lookback_hours * 3600
    queries = cfg.get("queries") or DEFAULT_QUERIES

    hits: dict[str, dict] = {}
    front = client.get_json(f"{API}/search", params={"tags": "front_page", "hitsPerPage": 30})
    for hit in front.get("hits", []):
        hits[hit["objectID"]] = hit

    # Ask HN posts are demand signals: lower threshold, always kind="question".
    ask = client.get_json(
        f"{API}/search_by_date",
        params={
            "tags": "ask_hn",
            "numericFilters": f"created_at_i>{since},points>{ask_min_points}",
            "hitsPerPage": 50,
        },
    )
    for hit in ask.get("hits", []):
        hits.setdefault(hit["objectID"], hit)

    for query in queries:
        page = client.get_json(
            f"{API}/search_by_date",
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"created_at_i>{since},points>{min_points}",
                "hitsPerPage": 30,
            },
        )
        for hit in page.get("hits", []):
            hits.setdefault(hit["objectID"], hit)

    items: list[Item] = []
    for hit in hits.values():
        item = _to_item(hit)
        if item is None:
            continue
        text = f"{item.title} {item.summary}"
        if not tagger.matched_keywords(text):
            continue  # front page and Ask HN carry plenty of off-niche stories
        items.append(item)

    items.sort(key=lambda i: i.engagement.get("points", 0), reverse=True)
    return items[: cfg.limit]


def _to_item(hit: dict) -> Item | None:
    title = hit.get("title") or hit.get("story_title")
    if not title:
        return None
    hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
    url = hit.get("url") or hn_url
    is_ask = "ask_hn" in (hit.get("_tags") or []) or title.lower().startswith("ask hn")
    created = parse_datetime(hit.get("created_at"))
    return Item(
        source="hackernews",
        url=url,
        title=title,
        summary=(hit.get("story_text") or "")[:500] or f"HN discussion: {hn_url}",
        published_at=to_local(created) if created else None,
        engagement={
            "points": float(hit.get("points") or 0),
            "comments": float(hit.get("num_comments") or 0),
        },
        kind="question" if is_ask else "trend",
    )
