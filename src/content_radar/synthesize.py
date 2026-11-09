"""Final idea synthesis (Sonnet): top clusters in, validated Idea objects out."""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ValidationError

from content_radar.config import Settings
from content_radar.filter import FilterVerdict
from content_radar.llm import (
    ModelClient,
    UsageTracker,
    check_budget,
    count_input_tokens,
    load_prompt,
    parse_or_none,
)
from content_radar.models import Idea, canonical_url
from content_radar.scoring import Cluster

log = logging.getLogger(__name__)


class IdeaDraft(BaseModel):
    """LLM-facing shape: plain strings and ints; strict validation happens in Idea."""

    title: str
    hook: str
    angle: str
    format: Literal["news", "tutorial", "explainer"]
    length: Literal["short", "long"]
    niche: Literal["ai_ml", "data", "cyber", "other"]
    why_now: str
    demand_signal: str
    sources: list[str]
    score: int
    effort: Literal["low", "med", "high"]
    evergreen: bool
    breaking: bool


class IdeaBatch(BaseModel):
    ideas: list[IdeaDraft]


def clusters_payload(
    clusters: list[Cluster], notes: dict[str, FilterVerdict], max_ideas: int
) -> str:
    rows = []
    for rank, cluster in enumerate(clusters, start=1):
        items = []
        for item in cluster.items[:5]:
            note = notes.get(str(item.url))
            row: dict = {
                "source": item.source,
                "kind": item.kind,
                "title": item.title,
                "url": str(item.url),
                "summary": item.summary[:400],
                "engagement": {k: int(v) for k, v in item.engagement.items() if v},
                "published_at": item.published_at.isoformat() if item.published_at else None,
            }
            if note:
                row["filter_notes"] = {
                    "niche": note.niche,
                    "local_angle": note.local_angle,
                    "demand": note.demand,
                }
            items.append(row)
        rows.append(
            {
                "rank": rank,
                "pre_score": cluster.score,
                "cross_source_count": cluster.cross_source_count,
                "has_question": cluster.has_question,
                "has_local": cluster.has_local,
                "items": items,
            }
        )
    header = f"Produce at most {max_ideas} ideas as JSON. Today's clusters, best first:\n"
    return header + json.dumps(rows, ensure_ascii=False)


def _to_ideas(drafts: list[IdeaDraft], allowed: dict[str, str]) -> tuple[list[Idea], list[str]]:
    """Convert drafts to validated Ideas. Unknown URLs are dropped; ideas with no real
    source are dropped. Returns (ideas, error messages for a retry)."""
    ideas: list[Idea] = []
    errors: list[str] = []
    for d in drafts:
        kept_sources = []
        for url in d.sources:
            real = allowed.get(canonical_url(url))
            if real and real not in kept_sources:
                kept_sources.append(real)
        if not kept_sources:
            errors.append(f"idea '{d.title[:60]}' cited no URL from the input; dropped")
            continue
        try:
            ideas.append(Idea(**{**d.model_dump(), "sources": kept_sources}))
        except ValidationError as exc:
            errors.append(f"idea '{d.title[:60]}': {exc.errors()[0].get('msg', 'invalid')}")
    return ideas, errors


def synthesize(
    clusters: list[Cluster],
    notes: dict[str, FilterVerdict],
    settings: Settings,
    client: ModelClient,
    tracker: UsageTracker,
) -> list[Idea]:
    """One Sonnet call (plus one retry on validation problems). Raises BudgetExceeded
    before spending when the projected cost passes the cap."""
    if not clusters:
        return []
    model = settings.models.synthesize
    max_ideas = settings.run.max_ideas_per_run
    system = load_prompt("synthesize")
    user = clusters_payload(clusters, notes, max_ideas)
    allowed = {canonical_url(u): u for c in clusters for u in c.urls()}

    est_in = count_input_tokens(client, model, system, user)
    est_out = settings.budget.estimates.synthesize_output_tokens
    check_budget(settings, tracker, "synthesize", model, est_in, est_out)

    messages = [{"role": "user", "content": user}]
    ideas: list[Idea] = []
    for attempt in range(2):
        response = parse_or_none(
            client,
            "synthesize",
            tracker,
            model=model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_format=IdeaBatch,
            output_config={"effort": settings.llm.synthesize_effort},
        )
        if response.stop_reason == "refusal":
            log.warning("synthesis refused by the model; no ideas this run")
            return []
        parsed = response.parsed_output
        if parsed is None:
            errors = ["response was not valid JSON for the schema"]
        else:
            ideas, errors = _to_ideas(parsed.ideas, allowed)
        if ideas and not errors:
            break
        if attempt == 0 and errors:
            log.warning("synthesis validation issues (%d); retrying once", len(errors))
            feedback = (
                "Some ideas were invalid:\n- "
                + "\n- ".join(errors[:10])
                + "\nReturn the full corrected list. Only cite URLs that appear in the input."
            )
            assistant_text = next(
                (b.text for b in response.content if getattr(b, "type", "") == "text"), ""
            )
            messages = [
                *messages,
                {"role": "assistant", "content": assistant_text or "{}"},
                {"role": "user", "content": feedback},
            ]
        else:
            break

    seen: set[str] = set()
    unique: list[Idea] = []
    for idea in sorted(ideas, key=lambda i: i.score, reverse=True):
        key = idea.dedupe_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(idea)
    log.info("synthesize: %d ideas returned, %d kept", len(ideas), len(unique[:max_ideas]))
    return unique[:max_ideas]
