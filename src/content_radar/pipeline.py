"""Orchestrates collect -> normalize -> dedupe (-> score -> synthesize -> sink in later phases)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import httpx

from content_radar.collectors import REGISTRY
from content_radar.collectors.base import client_from_settings, safe_collect
from content_radar.config import Settings
from content_radar.filter import FilterVerdict, filter_items
from content_radar.llm import BudgetExceeded, ModelClient, UsageTracker, make_client
from content_radar.models import Idea, Item
from content_radar.normalize import Tagger, dedupe, within_lookback
from content_radar.scoring import item_score, rank_clusters
from content_radar.synthesize import synthesize

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    items: list[Item]
    ideas: list[Idea] = field(default_factory=list)
    per_source_raw: Counter[str] = field(default_factory=Counter)
    per_source_kept: Counter[str] = field(default_factory=Counter)
    dropped_stale: int = 0
    dropped_duplicate: int = 0
    llm_summary: str = ""
    skipped_reason: str = ""

    def summary(self) -> str:
        lines = ["source            raw  kept"]
        for name in sorted(self.per_source_raw):
            lines.append(
                f"{name:<17} {self.per_source_raw[name]:>4}  {self.per_source_kept[name]:>4}"
            )
        lines.append(
            f"total kept {len(self.items)} "
            f"(stale -{self.dropped_stale}, duplicate -{self.dropped_duplicate}); "
            f"questions {sum(1 for i in self.items if i.kind == 'question')}, "
            f"local {sum(1 for i in self.items if 'local' in i.niche_tags)}"
        )
        return "\n".join(lines)


def collect_items(
    settings: Settings,
    sources: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> RunResult:
    """Run every enabled collector (optionally restricted), tag, filter and dedupe."""
    wanted = sources or settings.enabled_sources()
    unknown = [s for s in wanted if s not in REGISTRY]
    if unknown:
        log.warning("unknown sources ignored: %s", ", ".join(unknown))
    wanted = [s for s in wanted if s in REGISTRY and settings.source(s).enabled]

    tagger = Tagger(settings.niche)
    raw: list[Item] = []
    result = RunResult(items=[])
    with client_from_settings(settings, transport=transport) as client:
        for name in wanted:
            items = safe_collect(name, REGISTRY[name], settings, client)
            result.per_source_raw[name] = len(items)
            raw.extend(items)

    tagged = [tagger.apply(i) for i in raw]
    overrides = {
        name: int(cfg.get("lookback_hours"))
        for name, cfg in settings.sources.items()
        if cfg.get("lookback_hours") is not None
    }
    fresh = within_lookback(tagged, hours=settings.run.lookback_hours, per_source_hours=overrides)
    result.dropped_stale = len(tagged) - len(fresh)
    unique = dedupe(fresh)
    result.dropped_duplicate = len(fresh) - len(unique)

    for item in unique:
        result.per_source_kept[item.source.split(":")[0]] += 1
    result.items = unique
    log.info("collected %d raw -> %d unique items", len(raw), len(unique))
    return result


def run(
    settings: Settings,
    sources: list[str] | None = None,
    dry_run: bool = False,
    sink: bool = True,
    skip_llm: bool = False,
    transport: httpx.BaseTransport | None = None,
    client: ModelClient | None = None,
) -> RunResult:
    """Full daily run: collect -> score -> filter -> synthesize (-> sink in Phase 3).
    --dry-run still runs the LLM steps (that is what we want to judge) but writes nothing."""
    result = collect_items(settings, sources=sources, transport=transport)
    if skip_llm:
        log.info("--skip-llm: stopping after collection")
        return result
    if client is None and not settings.secrets.anthropic_api_key:
        result.skipped_reason = "ANTHROPIC_API_KEY not set; skipped filter and synthesis"
        log.warning(result.skipped_reason)
        return result
    result.ideas = generate_ideas(result.items, settings, client or make_client(settings), result)
    if dry_run:
        log.info("dry run: nothing written")
        return result
    # Phase 3: if sink: write result.ideas to Notion
    _ = sink
    return result


def generate_ideas(
    items: list[Item], settings: Settings, client: ModelClient, result: RunResult
) -> list[Idea]:
    """Score -> batched Haiku filter -> re-rank -> Sonnet synthesis, under the cost guard."""
    tracker = UsageTracker(settings)
    ideas: list[Idea] = []
    try:
        ranked = sorted(items, key=item_score, reverse=True)
        candidates = ranked[: settings.llm.filter_top_items]
        reserve = (
            settings.budget.estimates.synthesize_input_reserve,
            settings.budget.estimates.synthesize_output_tokens,
        )
        filtered = filter_items(candidates, settings, client, tracker, synth_reserve=reserve)
        notes: dict[str, FilterVerdict] = {
            str(f.item.url): f.verdict for f in filtered if f.verdict is not None
        }
        clusters = rank_clusters([f.item for f in filtered])
        log.info("%d items survived the filter -> %d clusters", len(filtered), len(clusters))
        ideas = synthesize(
            clusters[: settings.llm.synthesize_top_clusters], notes, settings, client, tracker
        )
    except BudgetExceeded as exc:
        result.skipped_reason = str(exc)
        log.warning("cost guard: %s", exc)
    result.llm_summary = tracker.summary()
    log.info(result.llm_summary)
    return ideas
