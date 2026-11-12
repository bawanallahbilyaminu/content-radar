"""Merge sightings, enrich the top candidates, flag risky repos, score momentum.

Everything here is deterministic. Flags are cheap signals for the human safety
checklist, not a verdict: a flagged repo is still listed unless the flag is in
`scout.exclude_flags`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from content_radar.collectors.base import HttpClient, parse_datetime
from content_radar.config import ScoutConfig, Settings
from content_radar.scout.models import TrendingRepo
from content_radar.scout.sources import github_headers
from content_radar.timeutil import now_local

log = logging.getLogger(__name__)

REPO_API = "https://api.github.com/repos/"

FLAG_OPTIONS = ["list", "lure-words", "low-forks", "no-license", "archived", "new-repo", "fork"]


def merge(sightings: Iterable[TrendingRepo]) -> list[TrendingRepo]:
    """One record per repo (case-insensitive owner/name)."""
    merged: dict[str, TrendingRepo] = {}
    for repo in sightings:
        if repo.key in merged:
            merged[repo.key].merge(repo)
        else:
            merged[repo.key] = repo.model_copy(deep=True)
    return list(merged.values())


def preliminary_order(repos: list[TrendingRepo]) -> list[TrendingRepo]:
    """Order before enrichment: more sources first, then stars gained."""
    return sorted(
        repos,
        key=lambda r: (len(r.sources), r.stars_week or 0, r.stars_today or 0),
        reverse=True,
    )


def apply_details(repo: TrendingRepo, data: dict[str, Any]) -> None:
    """Fill a repo from GitHub REST /repos/{full_name}."""
    repo.full_name = data.get("full_name") or repo.full_name
    repo.description = repo.description or (data.get("description") or "")[:500]
    repo.language = repo.language or data.get("language")
    if data.get("stargazers_count") is not None:
        repo.stars = int(data["stargazers_count"])
    if data.get("forks_count") is not None:
        repo.forks = int(data["forks_count"])
    lic = data.get("license") or {}
    spdx = lic.get("spdx_id") if isinstance(lic, dict) else None
    repo.license = spdx if spdx and spdx != "NOASSERTION" else (lic.get("name") if lic else None)
    repo.created_at = parse_datetime(data.get("created_at"))
    repo.pushed_at = parse_datetime(data.get("pushed_at"))
    repo.archived = bool(data.get("archived"))
    repo.topics = list(data.get("topics") or [])
    if data.get("fork"):
        repo.flags = sorted(set(repo.flags) | {"fork"})
    repo.enriched = True


def enrich(settings: Settings, client: HttpClient, repos: list[TrendingRepo]) -> None:
    """GitHub REST details for the top `enrich_top` repos. Failures are per repo."""
    headers = github_headers(settings)
    for i, repo in enumerate(repos[: settings.scout.enrich_top]):
        if i:
            client.pause(0.2)
        try:
            data = client.get_json(f"{REPO_API}{repo.full_name}", headers=headers, max_retries=1)
        except Exception as exc:  # noqa: BLE001 - one missing repo must not stop the rest
            log.warning("scout: details for %s failed: %s", repo.full_name, exc)
            continue
        apply_details(repo, data)


def compute_flags(repo: TrendingRepo, cfg: ScoutConfig, today: date | None = None) -> list[str]:
    """Cheap risk and fit signals, for the human checklist."""
    today = today or now_local().date()
    flags = set(repo.flags)
    name = repo.full_name.split("/")[-1].lower()
    text = f"{repo.full_name} {repo.description}".lower()
    topics = {t.lower() for t in repo.topics}
    if name.startswith("awesome") or "awesome-list" in topics or "awesome" in topics:
        flags.add("list")
    if any(word in text for word in cfg.flags.lure_words):
        flags.add("lure-words")
    stars, forks = repo.stars or 0, repo.forks
    if (
        stars >= cfg.flags.fork_check_min_stars
        and forks is not None
        and forks / stars < cfg.flags.min_fork_ratio
    ):
        flags.add("low-forks")
    if repo.enriched:
        if not repo.license:
            flags.add("no-license")
        if repo.archived:
            flags.add("archived")
        if repo.created_at and (today - repo.created_at.date()) <= timedelta(
            days=cfg.flags.new_repo_days
        ):
            flags.add("new-repo")
    return sorted(flags)


def _percentiles(values: list[int]) -> list[float]:
    """0..1 rank of each value among all values (ties share the higher rank)."""
    if not values:
        return []
    ordered = sorted(values)
    n = len(values)
    if n == 1:
        return [1.0 if values[0] > 0 else 0.0]
    out = []
    for v in values:
        if v <= 0:
            out.append(0.0)
            continue
        below_or_equal = sum(1 for x in ordered if x <= v)
        out.append((below_or_equal - 1) / (n - 1))
    return out


def score(repos: list[TrendingRepo], cfg: ScoutConfig) -> None:
    """Momentum 0..100 = week gain rank + today gain rank + days-in-a-row streak.

    A repo has to keep trending to score high: one big day only fills the 'today'
    part, which is also how short star-farming bursts get diluted."""
    w = cfg.weights
    week = _percentiles([r.stars_week or 0 for r in repos])
    day = _percentiles([r.stars_today or 0 for r in repos])
    for repo, pw, pd in zip(repos, week, day, strict=True):
        streak_part = min(repo.streak, w.streak_cap_days) / w.streak_cap_days
        repo.momentum = round(w.week * pw + w.today * pd + w.streak * streak_part)


def next_streak(last_seen: str | None, prev_streak: int, today: str) -> tuple[int, bool]:
    """(new streak, is a new day). Same-day re-runs keep the streak unchanged."""
    if not last_seen:
        return 1, True
    if last_seen == today:
        return max(prev_streak, 1), False
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    if last_seen == yesterday:
        return prev_streak + 1, True
    return 1, True
