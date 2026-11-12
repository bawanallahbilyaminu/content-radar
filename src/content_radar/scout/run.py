"""Scout run: fetch -> merge -> order -> enrich -> flag -> history -> score -> sink."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import httpx

from content_radar.collectors.base import client_from_settings
from content_radar.config import Settings
from content_radar.scout.models import TrendingRepo
from content_radar.scout.rank import compute_flags, enrich, merge, preliminary_order, score
from content_radar.scout.sink import RepoSink, make_repo_sink
from content_radar.scout.sources import FETCHERS, safe_fetch

log = logging.getLogger(__name__)


@dataclass
class ScoutResult:
    repos: list[TrendingRepo] = field(default_factory=list)
    per_source: Counter[str] = field(default_factory=Counter)
    excluded: int = 0
    created: int = 0
    updated: int = 0
    skipped_reason: str = ""

    def summary(self) -> str:
        lines = ["scout source       repos"]
        for name in sorted(self.per_source):
            lines.append(f"{name:<18} {self.per_source[name]:>5}")
        top = ", ".join(f"{r.full_name} ({r.momentum})" for r in self.repos[:5])
        lines.append(f"kept {len(self.repos)} (excluded by flag -{self.excluded}); top: {top}")
        return "\n".join(lines)


def run_scout(
    settings: Settings,
    dry_run: bool = False,
    sink: bool = True,
    sources: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    notion: RepoSink | None = None,
) -> ScoutResult:
    cfg = settings.scout
    result = ScoutResult()
    if not cfg.enabled:
        result.skipped_reason = "scout.enabled is false in config.yaml"
        log.info(result.skipped_reason)
        return result
    wanted = [s for s in (sources or cfg.sources) if s in FETCHERS]

    sightings: list[TrendingRepo] = []
    with client_from_settings(settings, transport=transport) as client:
        for name in wanted:
            found = safe_fetch(name, settings, client)
            result.per_source[name] = len(found)
            sightings.extend(found)
        repos = preliminary_order(merge(sightings))
        enrich(settings, client, repos)

    kept: list[TrendingRepo] = []
    for repo in repos:
        repo.flags = compute_flags(repo, cfg)
        if set(repo.flags) & set(cfg.exclude_flags):
            result.excluded += 1
            continue
        kept.append(repo)

    write = sink and not dry_run
    if write and notion is None:
        token, db_id = settings.secrets.notion_token, settings.secrets.notion_repos_db_id
        if token and db_id:
            notion = make_repo_sink(token, db_id)
        else:
            result.skipped_reason = (
                "NOTION_TOKEN / NOTION_REPOS_DB_ID not set; repos were not written "
                "(run scripts/create_notion_db.py --kind repos first)"
            )
            log.warning(result.skipped_reason)
            write = False

    # History (streak, days seen) only exists in Notion, so dry runs score with streak 1.
    if write and notion is not None:
        notion.load_history(kept)
    score(kept, cfg)
    kept.sort(key=lambda r: (r.momentum, r.stars_week or 0), reverse=True)
    result.repos = kept[: cfg.max_rows_per_run]

    if write and notion is not None:
        result.created, result.updated = notion.write(result.repos)
    elif dry_run:
        log.info("scout dry run: nothing written")
    return result
