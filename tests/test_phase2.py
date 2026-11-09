"""Scoring, clustering, cost guard, batched filter and synthesis with a fake Claude client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace

import pytest

from content_radar import pipeline
from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.filter import FilterBatch, FilterVerdict, filter_items
from content_radar.llm import BudgetExceeded, UsageTracker, check_budget, price_usd
from content_radar.models import Item
from content_radar.scoring import cluster_items, item_score, rank_clusters
from content_radar.synthesize import (
    IdeaBatch,
    IdeaDraft,
    ScoreBreakdown,
    SourceCitation,
    synthesize,
)
from content_radar.timeutil import now_local


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")


def _item(title, source="hackernews", url=None, hours_old=2.0, **kw) -> Item:
    url = url or f"https://example.com/{abs(hash(title + source)) % 10_000_000}"
    return Item(
        source=source,
        url=url,
        title=title,
        published_at=now_local() - timedelta(hours=hours_old),
        **kw,
    )


# --- scoring --------------------------------------------------------------------


def test_item_score_prefers_fresh_engaged_local_questions():
    fresh = _item("A", engagement={"points": 300}, hours_old=1)
    stale = _item("B", engagement={"points": 300}, hours_old=60)
    quiet = _item("C", hours_old=1)
    question = _item("D", kind="question", hours_old=1)
    local = _item("E", niche_tags=["local"], hours_old=1)
    assert item_score(fresh) > item_score(stale)
    assert item_score(fresh) > item_score(quiet)
    assert item_score(question) > item_score(quiet)
    assert item_score(local) > item_score(quiet)
    assert 0 <= item_score(fresh) <= 100


def test_cluster_items_groups_same_story_across_sources():
    a = _item("OpenAI releases GPT-6 with Hausa support", source="hackernews")
    b = _item("GPT-6 released by OpenAI, adds Hausa and Yoruba support", source="rss:verge")
    c = _item("CISA adds SonicWall flaw to KEV catalog", source="cisa_kev")
    clusters = cluster_items([a, b, c])
    assert len(clusters) == 2
    big = max(clusters, key=lambda c: len(c.items))
    assert {i.source for i in big.items} == {"hackernews", "rss:verge"}
    assert big.cross_source_count == 2


def test_rank_clusters_rewards_cross_source():
    solo = _item("Quantum chip breaks record", engagement={"points": 150}, hours_old=1)
    a = _item("Ransomware hits Lagos bank", source="rss:bleeping", hours_old=1)
    b = _item("Lagos bank hit by ransomware attack", source="rss:techcabal", hours_old=1)
    c = _item("Lagos bank ransomware attack confirmed", source="hackernews", hours_old=1)
    ranked = rank_clusters([solo, a, b, c])
    assert ranked[0].cross_source_count == 3
    assert ranked[0].score > ranked[1].score


# --- cost guard -----------------------------------------------------------------


def test_price_usd_uses_config_pricing(settings):
    assert price_usd(settings, "claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert price_usd(settings, "claude-sonnet-5-5", 0, 1_000_000) == pytest.approx(10.0)
    assert price_usd(settings, "unknown-model", 1000, 1000) == 0.0


def test_check_budget_raises_when_projected_cost_exceeds_cap(settings):
    tracker = UsageTracker(settings)
    settings.budget.max_cost_usd_per_run = 0.05
    check_budget(settings, tracker, "filter", "claude-haiku-5-5", 10_000, 2_000)  # ~$0.002
    with pytest.raises(BudgetExceeded, match="exceeds budget"):
        check_budget(settings, tracker, "synthesize", "claude-sonnet-5-5", 20_000, 7_000)  # $0.11


# --- fake Claude client ---------------------------------------------------------


@dataclass
class FakeMessages:
    parse_responses: list = field(default_factory=list)
    parse_calls: list = field(default_factory=list)
    count_calls: int = 0
    tokens_per_count: int = 3000

    def count_tokens(self, **kw):
        self.count_calls += 1
        return SimpleNamespace(input_tokens=self.tokens_per_count)

    def parse(self, **kw):
        self.parse_calls.append(kw)
        parsed = self.parse_responses.pop(0)
        return SimpleNamespace(
            parsed_output=parsed,
            usage=SimpleNamespace(input_tokens=3000, output_tokens=400),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=parsed.model_dump_json() if parsed else "")],
        )


def _fake(responses) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(parse_responses=list(responses)))


def _verdicts(ids, keep=True, niche="ai_ml", local=None, demand=False):
    return FilterBatch(
        verdicts=[
            FilterVerdict(
                id=i, keep=keep, niche=niche, local_angle=local, demand=demand, reason="r"
            )
            for i in ids
        ]
    )


# --- filter ---------------------------------------------------------------------


def test_filter_batches_about_fifty_items_per_call(settings):
    settings.models.filter_batch_size = 50
    items = [_item(f"Topic {i}") for i in range(120)]
    responses = [
        _verdicts(range(0, 50)),
        _verdicts(range(50, 100)),
        _verdicts(range(100, 120), keep=False),
    ]
    client = _fake(responses)
    tracker = UsageTracker(settings)
    out = filter_items(items, settings, client, tracker)
    assert len(client.messages.parse_calls) == 3
    sizes = [len(json.loads(c["messages"][0]["content"])) for c in client.messages.parse_calls]
    assert sizes == [50, 50, 20]
    assert len(out) == 100  # last batch all dropped
    assert tracker.calls_for("filter") == 3
    assert client.messages.count_calls == 3  # one estimate per batch, before any spend
    assert "filter: 3 call(s)" in tracker.summary()


def test_filter_applies_verdict_tags_and_fails_open_on_missing_ids(settings):
    items = [_item("Does GPT-6 speak Hausa?"), _item("Pandas 4.0 released"), _item("Mystery")]
    client = _fake(
        [
            FilterBatch(
                verdicts=[
                    FilterVerdict(
                        id=0,
                        keep=True,
                        niche="ai_ml",
                        local_angle="Hausa support",
                        demand=True,
                        reason="r",
                    ),
                    FilterVerdict(
                        id=1, keep=True, niche="data", local_angle=None, demand=False, reason="r"
                    ),
                    # id 2 omitted by the model
                ]
            )
        ]
    )
    out = filter_items(items, settings, client, UsageTracker(settings))
    assert len(out) == 3
    assert out[0].item.kind == "question" and "local" in out[0].item.niche_tags
    assert "data" in out[1].item.niche_tags
    assert out[2].verdict is None  # kept, fail-open


def test_filter_fails_open_when_output_is_truncated(settings):
    items = [_item("a"), _item("b")]
    client = _fake([])

    def broken_parse(**kw):
        FilterBatch.model_validate_json('{"verdicts": [{"id": 0, "ke')  # raises

    client.messages.parse = broken_parse
    tracker = UsageTracker(settings)
    out = filter_items(items, settings, client, tracker)  # must not raise
    assert len(out) == 2 and all(f.verdict is None for f in out)
    assert tracker.calls_for("filter") == 1  # still accounted for


def test_filter_skips_everything_when_budget_too_small(settings):
    settings.budget.max_cost_usd_per_run = 0.0001
    client = _fake([])
    with pytest.raises(BudgetExceeded):
        filter_items([_item("x")], settings, client, UsageTracker(settings))
    assert client.messages.parse_calls == []  # nothing spent


# --- synthesize -----------------------------------------------------------------


def _breakdown(score: int) -> ScoreBreakdown:
    # Spread a target score over the rubric with the local angle at 0 (angle 'none').
    t = min(25, score)
    d = min(25, score - t)
    c = min(15, score - t - d)
    e = min(15, score - t - d - c)
    return ScoreBreakdown(timeliness=t, demand=d, local_angle=0, cross_source=c, effort=e)


def _draft(title, sources, score=80, **kw) -> IdeaDraft:
    cits = [
        s
        if isinstance(s, SourceCitation)
        else SourceCitation(url=s, supports="backs the main claim")
        for s in sources
    ]
    base = dict(
        title=title,
        hook="Hook.",
        angle="none",
        angle_evidence="none",
        angle_source=None,
        format="news",
        length="short",
        niche="ai_ml",
        why_now="now",
        demand_signal="signal",
        sources=cits,
        score_breakdown=_breakdown(score),
        effort="low",
        evergreen=False,
        breaking=False,
    )
    base.update(kw)
    return IdeaDraft(**base)


def test_synthesize_drops_invented_urls_and_retries_once(settings):
    a = _item("GPT-6 speaks Hausa", url="https://real.example/a")
    b = _item("KEV adds SonicWall", source="cisa_kev", url="https://real.example/b")
    clusters = rank_clusters([a, b])
    first = IdeaBatch(
        ideas=[
            _draft("Good idea", ["https://real.example/a?utm_source=x"]),
            _draft("Fabricated", ["https://made.up/url"]),
        ]
    )
    second = IdeaBatch(
        ideas=[
            _draft("Good idea", ["https://real.example/a"]),
            _draft("Fixed idea", ["https://real.example/b"], score=70),
        ]
    )
    client = _fake([first, second])
    tracker = UsageTracker(settings)
    ideas = synthesize(clusters, {}, settings, client, tracker)
    assert len(client.messages.parse_calls) == 2
    retry_messages = client.messages.parse_calls[1]["messages"]
    assert retry_messages[-1]["role"] == "user" and "invalid" in retry_messages[-1]["content"]
    assert [i.title for i in ideas] == ["Good idea", "Fixed idea"]
    assert all(str(u).startswith("https://real.example/") for i in ideas for u in i.sources)
    assert tracker.calls_for("synthesize") == 2


def test_synthesize_caps_and_orders_by_score(settings):
    settings.run.max_ideas_per_run = 2
    items = [_item(f"T{i}", url=f"https://real.example/{i}") for i in range(3)]
    clusters = rank_clusters(items)
    batch = IdeaBatch(
        ideas=[
            _draft("Low", ["https://real.example/0"], score=40),
            _draft("High", ["https://real.example/1"], score=90),
            _draft("Mid", ["https://real.example/2"], score=60),
        ]
    )
    ideas = synthesize(clusters, {}, settings, _fake([batch]), UsageTracker(settings))
    # Low (40) is dropped by min_idea_score=55; the cap of 2 keeps High and Mid, best first.
    assert [i.title for i in ideas] == ["High", "Mid"]


def test_synthesize_budget_guard_blocks_before_spend(settings):
    settings.budget.max_cost_usd_per_run = 0.01
    clusters = rank_clusters([_item("x", url="https://real.example/x")])
    client = _fake([])
    with pytest.raises(BudgetExceeded, match="synthesize"):
        synthesize(clusters, {}, settings, client, UsageTracker(settings))
    assert client.messages.parse_calls == []


# --- pipeline wiring ------------------------------------------------------------


def test_generate_ideas_records_skip_reason_when_over_budget(settings):
    settings.budget.max_cost_usd_per_run = 0.0001
    result = pipeline.RunResult(items=[])
    ideas = pipeline.generate_ideas([_item("x")], settings, _fake([]), result)
    assert ideas == []
    assert (
        "exceeds budget" in result.skipped_reason or "would exceed budget" in result.skipped_reason
    )
    assert "no calls" in result.llm_summary


def test_run_without_api_key_skips_llm(settings, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "collect_items",
        lambda s, sources=None, transport=None: pipeline.RunResult(items=[_item("x")]),
    )
    result = pipeline.run(settings, dry_run=True)
    assert result.ideas == []
    assert "ANTHROPIC_API_KEY" in result.skipped_reason
