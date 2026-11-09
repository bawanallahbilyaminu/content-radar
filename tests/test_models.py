from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from content_radar.models import Idea, Item, canonical_url, normalize_title


def test_canonical_url_strips_tracking_and_www():
    url = "https://www.Example.com/path/?utm_source=x&id=5&fbclid=abc#frag"
    assert canonical_url(url) == "https://example.com/path?id=5"


def test_canonical_url_keeps_root_path():
    assert canonical_url("https://example.com/") == "https://example.com/"


def test_normalize_title():
    assert normalize_title("  GPT-5: What's NEW?! ") == "gpt 5 what s new"


def test_item_fingerprint_matches_for_same_story():
    a = Item(source="hn", url="https://www.a.com/x?utm_source=hn", title="Big News!")
    b = Item(source="rss", url="https://a.com/x", title="big news")
    assert a.fingerprint() == b.fingerprint()


def test_item_published_at_becomes_aware():
    item = Item(source="hn", url="https://a.com", title="t", published_at=datetime(2026, 1, 1))
    assert item.published_at is not None
    assert item.published_at.tzinfo is not None
    assert item.published_at == datetime(2026, 1, 1, tzinfo=UTC)  # same instant


def test_item_age_hours():
    now = datetime(2026, 1, 2, tzinfo=UTC)
    item = Item(source="hn", url="https://a.com", title="t", published_at=now - timedelta(hours=6))
    assert item.age_hours(now) == pytest.approx(6.0)
    assert Item(source="hn", url="https://a.com", title="t").age_hours(now) is None


def test_item_title_whitespace_collapsed():
    assert Item(source="hn", url="https://a.com", title="  a \n b  ").title == "a b"


def _idea(**overrides):
    base = dict(
        title="Does GPT-5 speak Hausa?",
        hook="I asked GPT-5 to translate a Hausa proverb.",
        angle="Tests Hausa and Yoruba support",
        format="explainer",
        length="short",
        niche="ai_ml",
        why_now="Released yesterday",
        demand_signal="Top of r/artificial",
        sources=["https://example.com/gpt5"],
        score=80,
        effort="low",
    )
    base.update(overrides)
    return Idea(**base)


def test_idea_valid():
    idea = _idea()
    assert idea.primary_source == "https://example.com/gpt5"
    assert idea.dedupe_key().startswith("does gpt 5 speak hausa|")


def test_idea_requires_source():
    with pytest.raises(ValidationError):
        _idea(sources=[])


def test_idea_score_bounds():
    with pytest.raises(ValidationError):
        _idea(score=101)


def test_idea_rejects_unknown_format():
    with pytest.raises(ValidationError):
        _idea(format="vlog")
