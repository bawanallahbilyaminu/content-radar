from datetime import UTC, datetime, timedelta

from content_radar.config import NicheConfig
from content_radar.models import Item
from content_radar.normalize import Tagger, dedupe, within_lookback

NICHE = NicheConfig(
    keywords=["llm", "ransomware", "pandas", "ai"],
    local_keywords=["nigeria", "hausa", "ndpa"],
    question_markers=["?", "how do i", "help"],
)


def _item(source="hn", url="https://a.com/x", title="t", **kw) -> Item:
    return Item(source=source, url=url, title=title, **kw)


def test_tagger_buckets_and_local():
    tagger = Tagger(NICHE)
    assert tagger.tags_for("New LLM speaks Hausa") == ["ai_ml", "local"]
    assert tagger.tags_for("Ransomware hits Lagos bank") == ["cyber"]
    assert tagger.tags_for("pandas 3.0 released") == ["data"]
    assert tagger.tags_for("Gardening tips") == []


def test_tagger_word_boundaries():
    tagger = Tagger(NICHE)
    assert tagger.matched_keywords("retail chains") == []  # 'ai' inside 'chains' must not match
    assert tagger.matched_keywords("AI is here") == ["ai"]


def test_tagger_promotes_questions():
    tagger = Tagger(NICHE)
    q = tagger.apply(_item(title="How do I fine-tune an LLM on a laptop?"))
    assert q.kind == "question"
    assert "ai_ml" in q.niche_tags
    paper = tagger.apply(_item(title="Why do LLMs hallucinate?", kind="paper"))
    assert paper.kind == "paper"  # only trends get promoted


def test_dedupe_by_url_and_title_merges_engagement():
    a = _item(
        "hackernews", "https://www.a.com/x?utm_source=1", "Big story", engagement={"points": 10}
    )
    b = _item("rss:verge", "https://a.com/x", "Big story", engagement={"points": 50, "comments": 3})
    c = _item("rss:techcrunch", "https://other.com/y", "BIG STORY!")
    d = _item("hackernews", "https://a.com/x", "Big story again")  # same source family, same url
    out = dedupe([a, b, c, d])
    assert len(out) == 1
    kept = out[0]
    assert kept.engagement["points"] == 50
    assert kept.engagement["comments"] == 3
    assert kept.engagement["cross_source_count"] == 3  # hn + verge + techcrunch


def test_dedupe_keeps_distinct():
    out = dedupe(
        [_item(url="https://a.com/1", title="one"), _item(url="https://a.com/2", title="two")]
    )
    assert len(out) == 2


def test_within_lookback_keeps_undated():
    now = datetime(2026, 1, 10, tzinfo=UTC)
    fresh = _item(published_at=now - timedelta(hours=1))
    stale = _item(url="https://a.com/s", title="s", published_at=now - timedelta(hours=100))
    undated = _item(url="https://a.com/u", title="u")
    assert within_lookback([fresh, stale, undated], hours=48, now=now) == [fresh, undated]
