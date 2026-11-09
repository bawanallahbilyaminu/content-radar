"""Final idea synthesis (Sonnet): top clusters in, validated Idea objects out.

The model proposes; code enforces. After parsing, every idea is checked for:
- sources that exist in the collected items and carry a justification,
- the breaking rule (recent AND cross-source / KEV / big HN thread),
- angle evidence (a "sourced" angle must cite a collected local item; assumed angles
  are capped, generic ones capped harder),
- niche "other": zero local-angle points, must carry a demand signal, max one per run,
- a score recomputed from the rubric breakdown,
- the minimum score.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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
from content_radar.models import Idea, Item, canonical_url, source_key
from content_radar.scoring import Cluster
from content_radar.timeutil import now_local

log = logging.getLogger(__name__)


class SourceCitation(BaseModel):
    url: str
    supports: str  # one short phrase: what claim in the idea this URL backs


class ScoreBreakdown(BaseModel):
    timeliness: int  # 0-25
    demand: int  # 0-25
    local_angle: int  # 0-20
    cross_source: int  # 0-15
    effort: int  # 0-15, lower production effort scores higher


class IdeaDraft(BaseModel):
    """LLM-facing shape: plain strings and ints; strict validation happens in Idea."""

    title: str
    hook: str
    angle: str
    angle_evidence: Literal["sourced", "assumed", "none"]
    angle_source: str | None
    format: Literal["news", "tutorial", "explainer"]
    length: Literal["short", "long"]
    niche: Literal["ai_ml", "data", "cyber", "other"]
    why_now: str
    demand_signal: str
    sources: list[SourceCitation]
    score_breakdown: ScoreBreakdown
    effort: Literal["low", "med", "high"]
    evergreen: bool
    breaking: bool


class IdeaBatch(BaseModel):
    ideas: list[IdeaDraft]


@dataclass
class Context:
    """What code needs to check an idea: collected items by canonical URL and their clusters."""

    items: dict[str, Item]
    clusters: dict[str, Cluster]
    local_sources: set[str]
    demand_urls: set[str]  # canonical URLs the filter marked as demand evidence

    @classmethod
    def from_clusters(
        cls,
        clusters: list[Cluster],
        local_sources: list[str],
        notes: dict[str, FilterVerdict] | None = None,
    ) -> Context:
        items: dict[str, Item] = {}
        by_url: dict[str, Cluster] = {}
        for cluster in clusters:
            for item in cluster.items:
                key = canonical_url(str(item.url))
                items.setdefault(key, item)
                by_url.setdefault(key, cluster)
        demand = {canonical_url(u) for u, v in (notes or {}).items() if v.demand}
        return cls(
            items=items,
            clusters=by_url,
            local_sources={s.lower() for s in local_sources},
            demand_urls=demand,
        )

    def is_local(self, item: Item) -> bool:
        return "local" in item.niche_tags or item.source.lower() in self.local_sources

    def has_demand_signal(self, cited: list[Item]) -> bool:
        """A cited question item, a filter demand flag, or a cluster that carries a question."""
        for item in cited:
            key = canonical_url(str(item.url))
            if item.kind == "question" or key in self.demand_urls:
                return True
            cluster = self.clusters.get(key)
            if cluster and cluster.has_question:
                return True
        return False


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
                "local_item": "local" in item.niche_tags,
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


def _clamp(value: int, high: int) -> int:
    return max(0, min(int(value), high))


def is_generic_angle(angle: str, markers: list[str]) -> bool:
    text = angle.lower()
    return any(m.lower() in text for m in markers)


def qualifies_breaking(cited: list[Item], ctx: Context, window_hours: int) -> bool:
    """Published in the last `window_hours` AND (cross-source >= 2 OR CISA KEV OR HN >= 300)."""
    now = now_local()
    for item in cited:
        age = item.age_hours(now)
        if age is None or age > window_hours:
            continue
        cluster = ctx.clusters.get(canonical_url(str(item.url)))
        if cluster and cluster.cross_source_count >= 2:
            return True
        if source_key(item.source) == "cisa_kev":
            return True
        if item.source == "hackernews" and item.engagement.get("points", 0) >= 300:
            return True
    return False


def enforce(draft: IdeaDraft, ctx: Context, settings: Settings) -> tuple[Idea | None, list[str]]:
    """Apply the code-level rules to one draft. Returns (idea or None, problems)."""
    problems: list[str] = []
    run = settings.run
    short = draft.title[:50]

    # 1. Sources: must exist in the input and carry a justification.
    kept: list[str] = []
    notes: dict[str, str] = {}
    cited_items: list[Item] = []
    for cit in draft.sources:
        item = ctx.items.get(canonical_url(cit.url))
        if item is None:
            problems.append(f"'{short}': URL not in input, dropped: {cit.url}")
            continue
        if len(cit.supports.strip()) < 8:
            problems.append(f"'{short}': no justification for {cit.url}, dropped")
            continue
        url = str(item.url)
        if url not in kept:
            kept.append(url)
            notes[url] = cit.supports.strip()
            cited_items.append(item)
    if not kept:
        problems.append(f"'{short}': no justified source from the input; idea dropped")
        return None, problems

    # 2. Niche "other" must rest on a demand signal, not just a fresh article.
    if draft.niche == "other" and not ctx.has_demand_signal(cited_items):
        problems.append(f"'{short}': niche 'other' without a demand signal; idea dropped")
        return None, problems

    # 3. Angle evidence.
    angle = draft.angle.strip()
    evidence = draft.angle_evidence
    angle_source: str | None = None
    if not angle or angle.lower() == "none":
        angle, evidence = "none", "none"
    elif evidence == "sourced":
        src_item = ctx.items.get(canonical_url(draft.angle_source or ""))
        if src_item is not None and ctx.is_local(src_item):
            angle_source = str(src_item.url)
            if angle_source not in kept:
                kept.append(angle_source)
                notes[angle_source] = "local angle evidence"
                cited_items.append(src_item)
        else:
            log.info("angle for '%s' claimed sourced without a local item; assumed", short)
            evidence = "assumed"
    else:
        evidence = "assumed"

    # 4. Score from the breakdown with the local-angle caps.
    b = draft.score_breakdown
    local_points = _clamp(b.local_angle, 20)
    if evidence == "none" or draft.niche == "other":
        local_points = 0
    elif evidence == "assumed":
        cap = run.assumed_angle_max_points
        if is_generic_angle(angle, settings.niche.generic_angle_markers):
            cap = run.generic_angle_max_points
        local_points = min(local_points, cap)
    breakdown = {
        "timeliness": _clamp(b.timeliness, 25),
        "demand": _clamp(b.demand, 25),
        "local_angle": local_points,
        "cross_source": _clamp(b.cross_source, 15),
        "effort": _clamp(b.effort, 15),
    }
    score = sum(breakdown.values())

    # 5. Breaking is earned, not declared.
    breaking = draft.breaking and qualifies_breaking(cited_items, ctx, run.breaking_window_hours)
    if draft.breaking and not breaking:
        log.info("breaking flag removed from '%s': rule not met", short)

    log.info(
        "idea '%s': score %d = T%d D%d L%d(%s, model gave %d) X%d E%d; niche=%s",
        short,
        score,
        breakdown["timeliness"],
        breakdown["demand"],
        breakdown["local_angle"],
        evidence,
        b.local_angle,
        breakdown["cross_source"],
        breakdown["effort"],
        draft.niche,
    )

    try:
        idea = Idea(
            title=draft.title,
            hook=draft.hook,
            angle=angle,
            angle_evidence=evidence,
            angle_source=angle_source,
            format=draft.format,
            length=draft.length,
            niche=draft.niche,
            why_now=draft.why_now,
            demand_signal=draft.demand_signal,
            sources=kept,
            source_notes=notes,
            score=score,
            score_breakdown=breakdown,
            effort=draft.effort,
            evergreen=draft.evergreen,
            breaking=breaking,
        )
    except ValidationError as exc:
        problems.append(f"'{short}': {exc.errors()[0].get('msg', 'invalid')}")
        return None, problems
    return idea, problems


def select_final(ideas: list[Idea], settings: Settings) -> list[Idea]:
    """Min score, dedupe, at most one niche-'other' idea, cap at max_ideas_per_run."""
    run = settings.run
    seen: set[str] = set()
    others = 0
    out: list[Idea] = []
    for idea in sorted(ideas, key=lambda i: i.score, reverse=True):
        if idea.score < run.min_idea_score:
            log.info(
                "idea '%s' below min score (%d < %d)",
                idea.title[:50],
                idea.score,
                run.min_idea_score,
            )
            continue
        key = idea.dedupe_key()
        if key in seen:
            continue
        if idea.niche == "other":
            if others >= run.max_other_ideas:
                log.info(
                    "idea '%s' dropped: only %d niche-other idea(s) per run",
                    idea.title[:50],
                    run.max_other_ideas,
                )
                continue
            others += 1
        seen.add(key)
        out.append(idea)
    return out[: run.max_ideas_per_run]


def synthesize(
    clusters: list[Cluster],
    notes: dict[str, FilterVerdict],
    settings: Settings,
    client: ModelClient,
    tracker: UsageTracker,
) -> list[Idea]:
    """One Sonnet call (plus one retry when drafts were dropped). Raises BudgetExceeded
    before spending when the projected cost passes the cap."""
    if not clusters:
        return []
    model = settings.models.synthesize
    max_ideas = settings.run.max_ideas_per_run
    system = load_prompt("synthesize")
    user = clusters_payload(clusters, notes, max_ideas)
    ctx = Context.from_clusters(clusters, settings.niche.local_sources, notes)

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
        dropped: list[str] = []
        ideas = []
        if parsed is None:
            dropped = ["response was not valid JSON for the schema"]
        else:
            for draft in parsed.ideas:
                idea, problems = enforce(draft, ctx, settings)
                if idea is not None:
                    ideas.append(idea)
                elif problems:
                    dropped.append(problems[-1])
        if not dropped:
            break
        if attempt == 0:
            log.warning("synthesis: %d idea(s) dropped by validation; retrying once", len(dropped))
            feedback = (
                "Some ideas were invalid and dropped:\n- "
                + "\n- ".join(dropped[:10])
                + "\nReturn the full corrected list. Only cite URLs that appear in the input, "
                "give every source a justification, and only use niche 'other' when a cited "
                "item is a question or demand signal."
            )
            assistant_text = next(
                (b.text for b in response.content if getattr(b, "type", "") == "text"), ""
            )
            messages = [
                *messages,
                {"role": "assistant", "content": assistant_text or "{}"},
                {"role": "user", "content": feedback},
            ]

    final = select_final(ideas, settings)
    log.info("synthesize: %d ideas returned, %d kept", len(ideas), len(final))
    return final
