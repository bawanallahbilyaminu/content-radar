"""Code-enforced idea rules (breaking, angle evidence, source justification, min score)
and the Notion sink with a fake client."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.models import Idea, Item
from content_radar.scoring import rank_clusters
from content_radar.sinks.notion import PROPERTIES, NotionSink, idea_to_properties, row_key
from content_radar.synthesize import Context, IdeaDraft, ScoreBreakdown, SourceCitation, enforce
from content_radar.timeutil import now_local


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for v in ("NOTION_TOKEN", "NOTION_IDEAS_DB_ID"):
        monkeypatch.delenv(v, raising=False)
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


def _ctx(items, settings) -> Context:
    return Context.from_clusters(rank_clusters(items), settings.niche.local_sources)


def _draft(sources, **kw) -> IdeaDraft:
    base = dict(
        title="Idea",
        hook="Hook.",
        angle="none",
        angle_evidence="none",
        angle_source=None,
        format="news",
        length="short",
        niche="cyber",
        why_now="now",
        demand_signal="signal",
        sources=[SourceCitation(url=u, supports="backs the main claim here") for u in sources],
        score_breakdown=ScoreBreakdown(
            timeliness=20, demand=20, local_angle=0, cross_source=10, effort=10
        ),
        effort="low",
        evergreen=False,
        breaking=False,
    )
    base.update(kw)
    return IdeaDraft(**base)


# --- breaking -------------------------------------------------------------------


def test_breaking_requires_recent_and_strong_signal(settings):
    lone = _item("Quiet story", url="https://a.com/lone", hours_old=3, engagement={"points": 50})
    big_hn = _item("Huge thread", url="https://a.com/big", hours_old=3, engagement={"points": 450})
    kev = _item(
        "CVE-2026-1: X",
        source="cisa_kev",
        url="https://nvd.nist.gov/vuln/detail/CVE-2026-1",
        hours_old=10,
    )
    old_kev = _item(
        "CVE-2020-9: Y",
        source="cisa_kev",
        url="https://nvd.nist.gov/vuln/detail/CVE-2020-9",
        hours_old=100,
    )
    x1 = _item(
        "Bank breach exposes data", source="rss:bleeping", url="https://a.com/x1", hours_old=5
    )
    x2 = _item(
        "Bank breach exposes customer data",
        source="rss:thehackernews",
        url="https://a.com/x2",
        hours_old=6,
    )
    ctx = _ctx([lone, big_hn, kev, old_kev, x1, x2], settings)

    def flag(urls):
        idea, _ = enforce(_draft(urls, breaking=True), ctx, settings)
        return idea.breaking

    assert flag(["https://a.com/lone"]) is False  # recent but weak
    assert flag(["https://a.com/big"]) is True  # HN >= 300
    assert flag(["https://nvd.nist.gov/vuln/detail/CVE-2026-1"]) is True  # KEV, recent
    assert flag(["https://nvd.nist.gov/vuln/detail/CVE-2020-9"]) is False  # KEV but old
    assert flag(["https://a.com/x1"]) is True  # cross-source cluster
    idea, _ = enforce(_draft(["https://a.com/big"], breaking=False), ctx, settings)
    assert idea.breaking is False  # never added, only removed


# --- angle evidence -------------------------------------------------------------


def test_sourced_angle_must_cite_a_local_item(settings):
    story = _item("New LLM release", url="https://a.com/llm")
    local = _item(
        "Nigeria fintechs adopt AI",
        source="rss:techcabal",
        url="https://techcabal.com/ai",
        niche_tags=["local"],
    )
    ctx = _ctx([story, local], settings)
    bd = ScoreBreakdown(timeliness=20, demand=20, local_angle=18, cross_source=5, effort=10)

    sourced, _ = enforce(
        _draft(
            ["https://a.com/llm"],
            angle="Ties to TechCabal's fintech AI report",
            angle_evidence="sourced",
            angle_source="https://techcabal.com/ai",
            score_breakdown=bd,
        ),
        ctx,
        settings,
    )
    assert sourced.angle_evidence == "sourced"
    assert str(sourced.angle_source) == "https://techcabal.com/ai"
    assert "https://techcabal.com/ai" in [str(u) for u in sourced.sources]
    assert sourced.score == 20 + 20 + 18 + 5 + 10

    # Claims "sourced" but points at a non-local item -> assumed, local points capped at 6
    fake, _ = enforce(
        _draft(
            ["https://a.com/llm"],
            angle="Many Nigerians use LLMs",
            angle_evidence="sourced",
            angle_source="https://a.com/llm",
            score_breakdown=bd,
        ),
        ctx,
        settings,
    )
    assert fake.angle_evidence == "assumed"
    assert fake.angle_source is None
    assert fake.score == 20 + 20 + 6 + 5 + 10

    # angle "none" -> zero local points whatever the model claimed
    none, _ = enforce(
        _draft(["https://a.com/llm"], angle="none", angle_evidence="sourced", score_breakdown=bd),
        ctx,
        settings,
    )
    assert none.angle_evidence == "none" and none.score == 20 + 20 + 0 + 5 + 10


# --- source justification -------------------------------------------------------


def test_sources_without_justification_are_dropped(settings):
    a = _item("A", url="https://a.com/a")
    b = _item("B", url="https://a.com/b")
    ctx = _ctx([a, b], settings)
    draft = _draft([])
    draft.sources = [
        SourceCitation(url="https://a.com/a", supports="the HN thread that is the demand signal"),
        SourceCitation(url="https://a.com/b", supports=""),
        SourceCitation(url="https://nope.example/x", supports="a perfectly fine justification"),
    ]
    idea, problems = enforce(draft, ctx, settings)
    assert [str(u) for u in idea.sources] == ["https://a.com/a"]
    assert idea.source_notes["https://a.com/a"].startswith("the HN thread")
    assert len(problems) == 2
    draft.sources = [SourceCitation(url="https://a.com/b", supports="")]
    idea, problems = enforce(draft, ctx, settings)
    assert idea is None and "no justified source" in problems[-1]


def test_score_breakdown_is_clamped(settings):
    a = _item("A", url="https://a.com/a")
    ctx = _ctx([a], settings)
    bd = ScoreBreakdown(timeliness=99, demand=-5, local_angle=0, cross_source=15, effort=15)
    idea, _ = enforce(_draft(["https://a.com/a"], score_breakdown=bd), ctx, settings)
    assert idea.score == 25 + 0 + 0 + 15 + 15


# --- Notion sink ----------------------------------------------------------------


class FakeNotion:
    def __init__(self, rows):
        self.rows = rows
        self.created = []
        self.databases = SimpleNamespace(
            retrieve=lambda database_id: {"id": database_id, "data_sources": [{"id": "ds_1"}]}
        )
        self.data_sources = SimpleNamespace(query=self._query)
        self.pages = SimpleNamespace(create=self._create)

    def _query(self, data_source_id, page_size, start_cursor=None):
        assert data_source_id == "ds_1"
        if start_cursor is None:
            return {"results": self.rows[:1], "has_more": len(self.rows) > 1, "next_cursor": "c2"}
        return {"results": self.rows[1:], "has_more": False, "next_cursor": None}

    def _create(self, parent, properties):
        assert parent == {"type": "data_source_id", "data_source_id": "ds_1"}
        self.created.append(properties)
        return {"id": f"page_{len(self.created)}"}


def _row(title, url):
    return {
        "properties": {
            "Title": {"type": "title", "title": [{"plain_text": title}]},
            "Primary Source": {"type": "url", "url": url},
        }
    }


def _idea(title, url, **kw) -> Idea:
    base = dict(
        title=title,
        hook="h",
        angle="none",
        format="news",
        length="short",
        niche="cyber",
        why_now="w",
        demand_signal="d",
        sources=[url],
        score=70,
        effort="low",
    )
    base.update(kw)
    return Idea(**base)


def test_notion_sink_creates_only_new_rows_with_status_new():
    fake = FakeNotion([_row("Existing Idea", "https://a.com/1"), _row("Other", "https://a.com/2")])
    sink = NotionSink(fake, "db_1")
    created = sink.write(
        [
            _idea("existing idea!", "https://www.a.com/1?utm_source=x"),  # dup by normalized key
            _idea(
                "Brand New",
                "https://a.com/3",
                breaking=True,
                angle="Lagos banks",
                angle_evidence="sourced",
                angle_source="https://techcabal.com/x",
                source_notes={"https://a.com/3": "the report"},
            ),
        ]
    )
    assert [i.title for i in created] == ["Brand New"]
    assert len(fake.created) == 1
    props = fake.created[0]
    assert props["Status"] == {"select": {"name": "New"}}
    assert props["Breaking"] == {"checkbox": True}
    assert props["Angle Evidence"] == {"select": {"name": "sourced"}}
    assert props["Angle Source"] == {"url": "https://techcabal.com/x"}
    assert props["Primary Source"] == {"url": "https://a.com/3"}
    assert "the report" in props["Sources"]["rich_text"][0]["text"]["content"]
    assert props["Date Found"]["date"]["start"] == now_local().date().isoformat()
    assert set(props) == set(PROPERTIES)


def test_row_key_matches_idea_dedupe_key():
    idea = _idea("Hello World", "https://www.a.com/x/?ref=hn")
    assert row_key("hello world", "https://a.com/x") == idea.dedupe_key()


def test_idea_to_properties_truncates_long_text():
    idea = _idea("T", "https://a.com/1", hook="x" * 5000)
    props = idea_to_properties(idea, "2026-11-09")
    assert len(props["Hook"]["rich_text"][0]["text"]["content"]) <= 2000
