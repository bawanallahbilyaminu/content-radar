"""Orchestrates collect -> normalize -> dedupe (-> score -> synthesize -> sink in later phases)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import httpx

from content_radar.collectors import REGISTRY
from content_radar.collectors.base import client_from_settings, safe_collect
from content_radar.config import Settings
from content_radar.models import Idea, Item
from content_radar.normalize import Tagger, dedupe, within_lookback

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    items: list[Item]
    ideas: list[Idea] = field(default_factory=list)
    per_source_raw: Counter[str] = field(default_factory=Counter)
    per_source_kept: Counter[str] = field(default_factory=Counter)
    dropped_stale: int = 0
    dropped_duplicate: int = 0

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
    fresh = within_lookback(tagged, hours=settings.run.lookback_hours)
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
    transport: httpx.BaseTransport | None = None,
) -> RunResult:
    """Full daily run. Phases 2 and 3 plug scoring, synthesis and the Notion sink in here."""
    result = collect_items(settings, sources=sources, transport=transport)
    if dry_run:
        log.info("dry run: skipping synthesis and sink")
        return result
    # Phase 2: score + synthesize -> result.ideas
    # Phase 3: if sink: write result.ideas to Notion
    _ = sink
    return result
