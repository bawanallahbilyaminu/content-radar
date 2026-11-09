"""Generic RSS / Atom collector for the feeds listed in config.yaml."""

from __future__ import annotations

import logging

import feedparser

from content_radar.collectors.base import HttpClient, clean_text, struct_time_to_datetime
from content_radar.config import Settings
from content_radar.models import Item

log = logging.getLogger(__name__)


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("rss")
    items: list[Item] = []
    for feed_cfg in cfg.get("feeds", []):
        name = feed_cfg.get("name") or feed_cfg.get("url")
        try:
            items.extend(collect_feed(client, feed_cfg, limit=cfg.limit))
        except Exception:  # noqa: BLE001 - one dead feed must not drop the others
            log.exception("rss feed %s failed", name)
    return items


def collect_feed(client: HttpClient, feed_cfg: dict, limit: int) -> list[Item]:
    name = feed_cfg.get("name") or feed_cfg["url"]
    text = client.get_text(feed_cfg["url"])
    return parse_feed(text, source=f"rss:{name}", feed_cfg=feed_cfg, limit=limit)


def parse_feed(text: str, source: str, feed_cfg: dict, limit: int) -> list[Item]:
    feed = feedparser.parse(text)
    base_tags: list[str] = []
    if feed_cfg.get("niche") and feed_cfg["niche"] != "other":
        base_tags.append(feed_cfg["niche"])
    if feed_cfg.get("local"):
        base_tags.append("local")

    items: list[Item] = []
    for entry in feed.entries[:limit]:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        summary = entry.get("summary") or ""
        if not summary and entry.get("content"):
            summary = entry["content"][0].get("value", "")
        items.append(
            Item(
                source=source,
                url=link,
                title=clean_text(title),
                summary=clean_text(summary)[:600],
                published_at=struct_time_to_datetime(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                engagement={},
                kind="trend",
                niche_tags=list(base_tags),
            )
        )
    return items
