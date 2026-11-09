"""Reddit via app-only (client_credentials) OAuth, read-only, post-level fields only.

Stored per post: title, url, score, num_comments, created_utc, flair, subreddit.
Never stored: author names, post bodies, comments.

Falls back to the public per-subreddit Atom feed when credentials are missing or
the OAuth call is refused (Data API approval can take weeks).
"""

from __future__ import annotations

import logging

import feedparser
import httpx

from content_radar.collectors.base import HttpClient, clean_text, struct_time_to_datetime
from content_radar.config import Settings
from content_radar.models import Item
from content_radar.timeutil import from_timestamp, to_local

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_API = "https://oauth.reddit.com"
PUBLIC_RSS = "https://www.reddit.com/r/{sub}/new/.rss"
QUESTION_FLAIRS = ("question", "help", "discussion", "beginner", "advice")


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("reddit")
    subs: list[str] = cfg.get("subreddits", [])
    secrets = settings.secrets
    user_agent = secrets.reddit_user_agent or settings.run.user_agent

    if secrets.reddit_client_id and secrets.reddit_client_secret:
        try:
            token = _app_token(
                client, secrets.reddit_client_id, secrets.reddit_client_secret, user_agent
            )
            return _collect_oauth(client, subs, token, user_agent, cfg.limit)
        except httpx.HTTPStatusError as exc:
            log.warning("reddit OAuth refused (%s); falling back to RSS", exc.response.status_code)
    else:
        log.info("reddit credentials missing; using public RSS fallback")

    if not cfg.get("rss_fallback", True):
        return []
    return _collect_rss(
        client, subs, user_agent, cfg.limit, delay=float(cfg.get("rss_delay_seconds", 6))
    )


# --- OAuth path -----------------------------------------------------------------


def _app_token(client: HttpClient, client_id: str, client_secret: str, user_agent: str) -> str:
    response = client.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        headers={"User-Agent": user_agent},
    )
    return response.json()["access_token"]


def _collect_oauth(
    client: HttpClient, subs: list[str], token: str, user_agent: str, limit: int
) -> list[Item]:
    headers = {"Authorization": f"bearer {token}", "User-Agent": user_agent}
    items: list[Item] = []
    seen: set[str] = set()
    for sub in subs:
        for listing in ("hot", "new"):
            data = client.get_json(
                f"{OAUTH_API}/r/{sub}/{listing}",
                params={"limit": limit, "raw_json": 1},
                headers=headers,
            )
            for child in data.get("data", {}).get("children", []):
                post = child.get("data", {})
                item = _post_to_item(post)
                if item is None or item.canonical in seen:
                    continue
                seen.add(item.canonical)
                items.append(item)
    return items


def _post_to_item(post: dict) -> Item | None:
    title = post.get("title")
    permalink = post.get("permalink")
    if not title or not permalink or post.get("stickied"):
        return None
    sub = post.get("subreddit", "")
    flair = (post.get("link_flair_text") or "").strip()
    discussion_url = f"https://www.reddit.com{permalink}"
    url = discussion_url if post.get("is_self") else (post.get("url") or discussion_url)
    kind = "question" if any(k in flair.lower() for k in QUESTION_FLAIRS) else "trend"
    summary = (
        f"r/{sub}" + (f" · flair: {flair}" if flair else "") + f" · discussion: {discussion_url}"
    )
    return Item(
        source=f"reddit:{sub}",
        url=url,
        title=title,
        summary=summary,
        published_at=from_timestamp(post.get("created_utc")),
        engagement={
            "score": float(post.get("score") or 0),
            "comments": float(post.get("num_comments") or 0),
        },
        kind=kind,
    )


# --- RSS fallback ---------------------------------------------------------------


def _collect_rss(
    client: HttpClient, subs: list[str], user_agent: str, limit: int, delay: float = 6.0
) -> list[Item]:
    """Public feeds are rate-limited for anonymous clients: space requests out and,
    on a 429, stop for this run rather than retry. We never work around the limit."""
    items: list[Item] = []
    for index, sub in enumerate(subs):
        if index:
            client.pause(delay)
        try:
            text = client.get_text(
                PUBLIC_RSS.format(sub=sub), headers={"User-Agent": user_agent}, max_retries=0
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                log.warning("reddit rss rate-limited at r/%s; stopping for this run", sub)
                break
            log.warning("reddit rss r/%s failed: %s", sub, exc)
            continue
        except httpx.HTTPError as exc:
            log.warning("reddit rss r/%s failed: %s", sub, exc)
            continue
        items.extend(parse_subreddit_feed(text, sub, limit))
    return items


def parse_subreddit_feed(text: str, sub: str, limit: int) -> list[Item]:
    """Title, link and date only. Author and body are present in the feed and discarded."""
    feed = feedparser.parse(text)
    items: list[Item] = []
    for entry in feed.entries[:limit]:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        published = struct_time_to_datetime(
            entry.get("updated_parsed") or entry.get("published_parsed")
        )
        items.append(
            Item(
                source=f"reddit:{sub}",
                url=link,
                title=clean_text(title),
                summary=f"r/{sub} · discussion: {link}",
                published_at=to_local(published) if published else None,
                engagement={},
                kind="trend",
            )
        )
    return items
