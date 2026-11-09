"""First-pass LLM filter (Haiku), batched: ~50 items per call, never one call per item."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from content_radar.config import Settings
from content_radar.llm import (
    ModelClient,
    UsageTracker,
    check_budget,
    count_input_tokens,
    load_prompt,
    parse_or_none,
)
from content_radar.models import Item

log = logging.getLogger(__name__)


class FilterVerdict(BaseModel):
    id: int
    keep: bool
    niche: Literal["ai_ml", "data", "cyber", "other"]
    local_angle: str | None
    demand: bool
    reason: str


class FilterBatch(BaseModel):
    verdicts: list[FilterVerdict]


@dataclass
class FilteredItem:
    item: Item
    verdict: FilterVerdict | None  # None when the model skipped the id (kept, fail-open)

    @property
    def niche(self) -> str:
        if self.verdict:
            return self.verdict.niche
        for tag in self.item.niche_tags:
            if tag in {"ai_ml", "data", "cyber"}:
                return tag
        return "other"


def _batch_payload(batch: list[tuple[int, Item]]) -> str:
    rows = [
        {
            "id": idx,
            "source": item.source,
            "kind": item.kind,
            "title": item.title,
            "summary": item.summary[:300],
        }
        for idx, item in batch
    ]
    return json.dumps(rows, ensure_ascii=False)


def filter_items(
    items: list[Item],
    settings: Settings,
    client: ModelClient,
    tracker: UsageTracker,
    synth_reserve: tuple[int, int] = (0, 0),
) -> list[FilteredItem]:
    """Run the Haiku filter over `items` in batches. Raises BudgetExceeded before spending
    if the whole filter plus the reserved synthesis estimate would pass the cap."""
    if not items:
        return []
    model = settings.models.filter
    size = max(1, settings.models.filter_batch_size)
    system = load_prompt("filter")
    indexed = list(enumerate(items))
    batches = [indexed[i : i + size] for i in range(0, len(indexed), size)]
    payloads = [_batch_payload(b) for b in batches]

    # Estimate every batch up front (count_tokens is free), then guard once.
    est_in = sum(count_input_tokens(client, model, system, p) for p in payloads)
    est_out = len(items) * settings.budget.estimates.filter_output_per_item
    check_budget(settings, tracker, "filter", model, est_in, est_out)
    reserve_in, reserve_out = synth_reserve
    if reserve_in or reserve_out:
        # Make sure the filter does not eat the budget synthesis will need.
        from content_radar.llm import price_usd

        projected = (
            tracker.total_cost
            + price_usd(settings, model, est_in, est_out)
            + price_usd(settings, settings.models.synthesize, reserve_in, reserve_out)
        )
        if projected > settings.budget.max_cost_usd_per_run:
            from content_radar.llm import BudgetExceeded

            raise BudgetExceeded(
                f"skipping filter: filter (~${price_usd(settings, model, est_in, est_out):.4f}) "
                f"plus reserved synthesis (~$"
                f"{price_usd(settings, settings.models.synthesize, reserve_in, reserve_out):.4f}) "
                f"would exceed budget ${settings.budget.max_cost_usd_per_run:.2f}"
            )

    verdicts: dict[int, FilterVerdict] = {}
    for batch, payload in zip(batches, payloads, strict=True):
        response = parse_or_none(
            client,
            "filter",
            tracker,
            model=model,
            # verdicts run ~60-80 tokens each; leave generous headroom so JSON is never cut
            max_tokens=settings.budget.estimates.filter_output_per_item * len(batch) * 2 + 1000,
            system=system,
            messages=[{"role": "user", "content": payload}],
            output_format=FilterBatch,
            output_config={"effort": settings.llm.filter_effort},
        )
        if response.stop_reason == "refusal":
            log.warning("filter batch refused; keeping its %d items unfiltered", len(batch))
            continue
        parsed = response.parsed_output
        if parsed is None:
            log.warning("filter batch returned no parsable output; keeping %d items", len(batch))
            continue
        valid_ids = {idx for idx, _ in batch}
        for v in parsed.verdicts:
            if v.id in valid_ids:
                verdicts[v.id] = v

    log.info(
        "filter: %d batches, %d items in, %d verdicts, %d kept",
        len(batches),
        len(items),
        len(verdicts),
        sum(1 for v in verdicts.values() if v.keep),
    )
    out: list[FilteredItem] = []
    for idx, item in indexed:
        verdict = verdicts.get(idx)
        if verdict is None:
            out.append(FilteredItem(item, None))
        elif verdict.keep:
            tags = list(item.niche_tags)
            if verdict.niche != "other" and verdict.niche not in tags:
                tags.insert(0, verdict.niche)
            if verdict.local_angle and "local" not in tags:
                tags.append("local")
            kind = "question" if verdict.demand and item.kind == "trend" else item.kind
            out.append(
                FilteredItem(item.model_copy(update={"niche_tags": tags, "kind": kind}), verdict)
            )
    return out
