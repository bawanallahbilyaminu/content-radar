"""Calibration: hand-labelled good vs weak ideas must keep their relative order.

The fixture holds realistic model outputs (rubric breakdowns included) for ideas the
owner judged good or weak. Only code-side rules run here, so this guards the scoring
logic; prompt changes are checked by re-running the pipeline and comparing to this set.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.models import Item
from content_radar.scoring import rank_clusters
from content_radar.synthesize import Context, IdeaDraft, enforce, select_final
from content_radar.timeutil import now_local

FIXTURE = Path(__file__).parent / "fixtures" / "calibration_ideas.json"


@pytest.fixture
def settings(tmp_path):
    return load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")


def _load():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _items(spec):
    now = now_local()
    return [
        Item(
            source=i["source"],
            url=i["url"],
            title=i["title"],
            kind=i.get("kind", "trend"),
            published_at=now - timedelta(hours=i.get("hours_old", 2)),
            engagement=i.get("engagement", {}),
            niche_tags=i.get("niche_tags", []),
        )
        for i in spec
    ]


def _draft(spec) -> IdeaDraft:
    fields = {k: v for k, v in spec.items() if not k.startswith("_")}
    fields.setdefault("evergreen", False)
    return IdeaDraft(
        hook="Hook.",
        why_now="now",
        demand_signal="signal",
        breaking=False,
        **fields,
    )


def _score(spec, ctx, settings) -> int:
    idea, _ = enforce(_draft(spec), ctx, settings)
    return idea.score if idea is not None else -1  # dropped ideas rank below everything


def test_every_good_idea_outranks_every_weak_idea(settings):
    data = _load()
    ctx = Context.from_clusters(rank_clusters(_items(data["items"])), settings.niche.local_sources)
    good = {g["title"]: _score(g, ctx, settings) for g in data["good"]}
    weak = {w["title"]: _score(w, ctx, settings) for w in data["weak"]}
    worst_good = min(good.values())
    best_weak = max(weak.values())
    assert worst_good > best_weak, f"good={good} weak={weak}"


def test_good_ideas_pass_min_score_and_weak_ones_do_not(settings):
    data = _load()
    ctx = Context.from_clusters(rank_clusters(_items(data["items"])), settings.niche.local_sources)
    for g in data["good"]:
        assert _score(g, ctx, settings) >= settings.run.min_idea_score, g["title"]
    for w in data["weak"]:
        assert _score(w, ctx, settings) < settings.run.min_idea_score, w["title"]


def test_other_niche_needs_demand_and_is_capped_to_one(settings):
    data = _load()
    items = _items(data["items"])
    # Give the phone-settings cluster a question so it passes the demand gate...
    items.append(
        Item(
            source="reddit:Nigeria",
            url="https://www.reddit.com/r/Nigeria/comments/x/how_do_i_cut_my_data_usage/",
            title="How do I cut data use? Smartphone settings influence mobile data consumption",
            kind="question",
            published_at=now_local(),
        )
    )
    ctx = Context.from_clusters(rank_clusters(items), settings.niche.local_sources)
    weak = data["weak"]
    phone, _ = enforce(_draft(weak[0]), ctx, settings)
    fees, _ = enforce(_draft(weak[1]), ctx, settings)
    assert (
        phone is not None and phone.score_breakdown["local_angle"] == 0
    )  # other -> 0 local points
    assert fees is None  # still no demand signal -> dropped
    # ...and even with demand, at most one 'other' idea survives selection.
    settings.run.min_idea_score = 0
    twin = phone.model_copy(update={"title": "Another other idea", "sources": [phone.sources[0]]})
    twin = twin.model_copy(update={"sources": ["https://techeconomy.ng/other-2"]})
    final = select_final([phone, twin], settings)
    assert [i.niche for i in final] == ["other"]
