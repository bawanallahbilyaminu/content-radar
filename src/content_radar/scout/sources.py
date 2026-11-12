"""Where trending repos come from. Each fetcher fails safely and returns [].

- github.com/trending (daily and weekly): HTML, scraped with approval. There is no
  API for this page and its markup can change, so the parser is tolerant and a
  page that yields nothing is logged as a warning, not an error.
- OSS Insight trends API (past_24_hours / past_week): stars gained in the period.
- GitHub Search API: repos created in the last N days, sorted by stars.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from content_radar.collectors.base import HttpClient, clean_text
from content_radar.config import Settings
from content_radar.scout.models import TrendingRepo
from content_radar.timeutil import now_local

log = logging.getLogger(__name__)

TRENDING_URL = "https://github.com/trending"
OSSINSIGHT_URL = "https://api.ossinsight.io/v1/trends/repos/"
SEARCH_URL = "https://api.github.com/search/repositories"

_ARTICLE_RE = re.compile(r"<article\b[^>]*\bBox-row\b[^>]*>(.*?)</article>", re.S | re.I)
_REPO_HREF_RE = re.compile(r'<h[12][^>]*>.*?<a\b[^>]*href="/([\w.-]+/[\w.-]+)"', re.S | re.I)
_DESC_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
_LANG_RE = re.compile(r'itemprop="programmingLanguage"[^>]*>(.*?)<', re.S | re.I)
_PERIOD_RE = re.compile(r"([\d,]+)\s+stars?\s+(today|this week|this month)", re.I)
_NUMBER_RE = re.compile(r"[\d,]+")


def _int(value: Any) -> int | None:
    """'12,345' / '12345' / 12345 -> 12345; anything else -> None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = _NUMBER_RE.search(str(value))
    return int(match.group(0).replace(",", "")) if match else None


def _link_count(chunk: str, full_name: str, suffix: str) -> int | None:
    pattern = re.compile(
        r'<a\b[^>]*href="/' + re.escape(full_name) + "/" + suffix + r'"[^>]*>(.*?)</a>',
        re.S | re.I,
    )
    match = pattern.search(chunk)
    return _int(clean_text(match.group(1))) if match else None


def parse_trending_html(html: str, source: str) -> list[TrendingRepo]:
    """Parse github.com/trending markup. `source` is trending_daily or trending_weekly."""
    repos: list[TrendingRepo] = []
    for chunk in _ARTICLE_RE.findall(html):
        href = _REPO_HREF_RE.search(chunk)
        if not href:
            continue
        full_name = href.group(1)
        desc = _DESC_RE.search(chunk)
        lang = _LANG_RE.search(chunk)
        period = _PERIOD_RE.search(clean_text(chunk))
        gained = _int(period.group(1)) if period else None
        unit = period.group(2).lower() if period else ""
        repos.append(
            TrendingRepo(
                full_name=full_name,
                description=clean_text(desc.group(1))[:500] if desc else "",
                language=(clean_text(lang.group(1)) or None) if lang else None,
                stars=_link_count(chunk, full_name, "stargazers"),
                forks=_link_count(chunk, full_name, "forks"),
                stars_today=gained if unit == "today" else None,
                stars_week=gained if unit == "this week" else None,
                sources={source},
            )
        )
    return repos


def parse_ossinsight(data: Any, source: str) -> list[TrendingRepo]:
    """OSS Insight /v1/trends/repos: rows under data.rows, values usually strings."""
    rows: list[dict[str, Any]] = []
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict):
            rows = inner.get("rows") or []
        elif isinstance(data.get("rows"), list):
            rows = data["rows"]
    repos: list[TrendingRepo] = []
    for row in rows:
        name = (row.get("repo_name") or "").strip()
        if "/" not in name:
            continue
        gained = _int(row.get("stars"))
        repos.append(
            TrendingRepo(
                full_name=name,
                description=(row.get("description") or "")[:500],
                language=row.get("primary_language") or None,
                stars_today=gained if source == "ossinsight_day" else None,
                stars_week=gained if source == "ossinsight_week" else None,
                sources={source},
            )
        )
    return repos


def parse_search(data: Any) -> list[TrendingRepo]:
    """GitHub Search results for repos created this week: total stars ~= stars this week."""
    repos: list[TrendingRepo] = []
    for repo in (data or {}).get("items", []):
        name = repo.get("full_name")
        if not name:
            continue
        stars = _int(repo.get("stargazers_count"))
        repos.append(
            TrendingRepo(
                full_name=name,
                description=(repo.get("description") or "")[:500],
                language=repo.get("language") or None,
                stars=stars,
                forks=_int(repo.get("forks_count")),
                stars_week=stars,
                sources={"search"},
            )
        )
    return repos


def github_headers(settings: Settings) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.secrets.github_token:
        headers["Authorization"] = f"Bearer {settings.secrets.github_token}"
    return headers


def _trending(
    settings: Settings, client: HttpClient, since: str, source: str
) -> list[TrendingRepo]:
    cfg = settings.scout
    paths = [f"/{lang}" for lang in cfg.trending_languages] or [""]
    repos: list[TrendingRepo] = []
    for i, path in enumerate(paths):
        if i:
            client.pause(cfg.request_pause_seconds)
        html = client.get_text(
            f"{TRENDING_URL}{path}", params={"since": since}, headers={"Accept": "text/html"}
        )
        found = parse_trending_html(html, source)
        if not found:
            log.warning(
                "scout: %s%s parsed 0 repos; the page markup may have changed", source, path
            )
        repos.extend(found)
    return repos


def trending_daily(settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    return _trending(settings, client, "daily", "trending_daily")


def trending_weekly(settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    return _trending(settings, client, "weekly", "trending_weekly")


def ossinsight_day(settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    data = client.get_json(OSSINSIGHT_URL, params={"period": "past_24_hours", "language": "All"})
    return parse_ossinsight(data, "ossinsight_day")


def ossinsight_week(settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    data = client.get_json(OSSINSIGHT_URL, params={"period": "past_week", "language": "All"})
    return parse_ossinsight(data, "ossinsight_week")


def search(settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    cfg = settings.scout
    since = (now_local() - timedelta(days=cfg.search_created_within_days)).date().isoformat()
    data = client.get_json(
        SEARCH_URL,
        params={
            "q": f"created:>{since} stars:>{cfg.search_min_stars}",
            "sort": "stars",
            "order": "desc",
            "per_page": 50,
        },
        headers=github_headers(settings),
    )
    return parse_search(data)


Fetcher = Callable[[Settings, HttpClient], list[TrendingRepo]]

FETCHERS: dict[str, Fetcher] = {
    "trending_daily": trending_daily,
    "trending_weekly": trending_weekly,
    "ossinsight_day": ossinsight_day,
    "ossinsight_week": ossinsight_week,
    "search": search,
}


def safe_fetch(name: str, settings: Settings, client: HttpClient) -> list[TrendingRepo]:
    """One broken source never kills the scout run."""
    started = time.monotonic()
    try:
        repos = FETCHERS[name](settings, client)
    except Exception:  # noqa: BLE001 - deliberate: isolate every source
        log.exception("scout source %s failed", name)
        return []
    log.info("scout source %s: %d repos in %.1fs", name, len(repos), time.monotonic() - started)
    return repos
