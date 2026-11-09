"""Stack Exchange API 2.3: recent high-vote questions. Primary demand-signal source.

Quota: 300 requests/day per IP without a key, 10,000 with STACKEXCHANGE_KEY.
Rules honoured here: the `backoff` field (sleep that many seconds before the next
call), throttle errors (stop immediately), and a minimum remaining quota floor.
"""

from __future__ import annotations

import logging
import time

import httpx

from content_radar.collectors.base import HttpClient, clean_text
from content_radar.config import Settings
from content_radar.models import Item
from content_radar.timeutil import from_timestamp, now_local

log = logging.getLogger(__name__)

API = "https://api.stackexchange.com/2.3/questions"
SITE_NICHE = {"stackoverflow": None, "datascience": "data", "security": "cyber", "ai": "ai_ml"}
TAG_NICHE = {
    "python": "data",
    "pandas": "data",
    "machine-learning": "ai_ml",
    "llm": "ai_ml",
    "docker": "other",
}


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("stackexchange")
    lookback_hours = int(cfg.get("lookback_hours", 168))
    pagesize = int(cfg.get("pagesize", 30))
    min_score = int(cfg.get("min_score", 1))
    quota_floor = int(cfg.get("min_quota_remaining", 20))
    fromdate = int(now_local().timestamp()) - lookback_hours * 3600

    queries: list[tuple[str, str | None]] = []
    for site_cfg in cfg.get("sites", []):
        site = site_cfg["site"]
        tags = site_cfg.get("tags") or [None]
        queries.extend((site, tag) for tag in tags)

    items: list[Item] = []
    seen: set[str] = set()
    for site, tag in queries:
        params: dict[str, str | int] = {
            "site": site,
            "sort": "votes",
            "order": "desc",
            "fromdate": fromdate,
            "pagesize": pagesize,
            "filter": "default",
        }
        if tag:
            params["tagged"] = tag
        if settings.secrets.stackexchange_key:
            params["key"] = settings.secrets.stackexchange_key
        try:
            data = client.get_json(API, params=params)
        except httpx.HTTPStatusError as exc:
            # 400 with error_id 502 is a throttle violation: stop for this run.
            log.warning(
                "stackexchange %s/%s refused (%s); stopping", site, tag, exc.response.status_code
            )
            break

        for q in data.get("items", []):
            if int(q.get("score") or 0) < min_score:
                continue
            item = _question_to_item(q, site, tag)
            if item is None or item.canonical in seen:
                continue
            seen.add(item.canonical)
            items.append(item)

        backoff = data.get("backoff")
        if backoff:
            log.info("stackexchange asked for backoff %ss", backoff)
            time.sleep(float(backoff))
        remaining = data.get("quota_remaining")
        if remaining is not None and int(remaining) < quota_floor:
            log.warning(
                "stackexchange quota_remaining=%s below floor %s; stopping", remaining, quota_floor
            )
            break

    items.sort(key=lambda i: i.engagement.get("score", 0), reverse=True)
    return items[: cfg.limit]


def _question_to_item(q: dict, site: str, tag: str | None) -> Item | None:
    link = q.get("link")
    title = q.get("title")
    if not link or not title:
        return None
    tags = [t for t in q.get("tags", []) if isinstance(t, str)]
    niche = SITE_NICHE.get(site) or (TAG_NICHE.get(tag or "") if tag else None)
    answered = "answered" if q.get("is_answered") else "unanswered"
    summary = (
        f"{site}.stackexchange question · tags: {', '.join(tags[:6])} · "
        f"{q.get('score', 0)} votes, {q.get('view_count', 0)} views, "
        f"{q.get('answer_count', 0)} answers ({answered})"
    )
    return Item(
        source=f"stackexchange:{site}",
        url=link,
        title=clean_text(title),
        summary=summary,
        published_at=from_timestamp(q.get("creation_date")),
        engagement={
            "score": float(q.get("score") or 0),
            "views": float(q.get("view_count") or 0),
            "answers": float(q.get("answer_count") or 0),
        },
        kind="question",
        niche_tags=[niche] if niche and niche != "other" else [],
    )
