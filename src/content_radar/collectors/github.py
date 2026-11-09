"""GitHub Search API: repos created in the last N days, ranked by stars, filtered to the niche."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from content_radar.collectors.base import HttpClient, parse_datetime
from content_radar.config import Settings
from content_radar.models import Item
from content_radar.normalize import Tagger

API = "https://api.github.com/search/repositories"


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("github")
    tagger = Tagger(settings.niche)
    days = int(cfg.get("created_within_days", 7))
    min_stars = int(cfg.get("min_stars", 30))
    min_forks = int(cfg.get("min_forks", 3))
    topics = {t.lower() for t in cfg.get("topics", [])}
    since = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()

    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if settings.secrets.github_token:
        headers["Authorization"] = f"Bearer {settings.secrets.github_token}"

    # One request: GitHub search has no OR across topic qualifiers, so pull the
    # most-starred new repos and filter locally by topic or niche keyword.
    data = client.get_json(
        API,
        params={
            "q": f"created:>{since} stars:>{min_stars}",
            "sort": "stars",
            "order": "desc",
            "per_page": 100,
        },
        headers=headers,
    )

    items: list[Item] = []
    for repo in data.get("items", []):
        if int(repo.get("forks_count") or 0) < min_forks:
            continue  # star-farmed spam: hundreds of stars, zero forks
        repo_topics = {t.lower() for t in repo.get("topics", [])}
        text = (
            f"{repo.get('full_name', '')} {repo.get('description') or ''} {' '.join(repo_topics)}"
        )
        if not (repo_topics & topics) and not tagger.matched_keywords(text):
            continue
        items.append(
            Item(
                source="github",
                url=repo["html_url"],
                title=repo["full_name"],
                summary=(repo.get("description") or "")[:500],
                published_at=parse_datetime(repo.get("created_at")),
                engagement={
                    "stars": float(repo.get("stargazers_count") or 0),
                    "forks": float(repo.get("forks_count") or 0),
                },
                kind="repo",
                niche_tags=sorted(repo_topics & topics),
            )
        )
    return items[: cfg.limit]
