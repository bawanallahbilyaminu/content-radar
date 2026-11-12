import json

import httpx
import pytest

from content_radar import __main__ as cli
from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.pipeline import collect_items
from content_radar.pipeline import run as pipeline_run
from tests.conftest import load_fixture, load_json_fixture, mock_transport


@pytest.fixture
def settings(tmp_path):
    s = load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")
    s.run.lookback_hours = 24 * 365 * 10  # fixtures age; keep everything
    return s


@pytest.fixture
def transport():
    return mock_transport(
        {
            "tags=ask_hn": load_json_fixture("hn_ask.json"),
            "hn.algolia.com": load_json_fixture("hn_search.json"),
            "reddit.com": load_fixture("reddit_rss.xml"),
            "api.stackexchange.com": load_json_fixture("se_questions.json"),
            "api.github.com": load_json_fixture("github_search.json"),
            "api/models": load_json_fixture("hf_models.json"),
            "api/daily_papers": load_json_fixture("hf_papers.json"),
            "export.arxiv.org": load_fixture("arxiv.xml"),
            "cisa.gov": httpx.Response(503, text="down"),  # one broken source
            "bleepingcomputer": load_fixture("rss_bleeping.xml"),
            "techcabal": load_fixture("rss_techcabal.xml"),
        }
    )


def test_collect_items_survives_broken_source(settings, transport):
    result = collect_items(settings, transport=transport)
    assert result.per_source_raw["cisa_kev"] == 0
    assert result.per_source_raw["hackernews"] > 0
    assert result.items
    assert len({i.fingerprint() for i in result.items}) == len(result.items)
    assert "total kept" in result.summary()


def test_collect_items_respects_sources_filter(settings, transport):
    result = collect_items(settings, sources=["arxiv", "bogus"], transport=transport)
    assert set(result.per_source_raw) == {"arxiv"}
    assert all(i.source == "arxiv" for i in result.items)


def test_cli_dry_run_prints_json(settings, transport, monkeypatch, capsys):
    # The arXiv fixture has fixed dates, so keep it inside the lookback window
    # whatever day the suite runs; this test is about the CLI output, not freshness.
    settings.sources["arxiv"].model_extra["lookback_hours"] = 10**6
    monkeypatch.setattr(cli, "load_settings", lambda _p: settings)
    monkeypatch.setattr(cli, "run", lambda s, **kw: pipeline_run(s, transport=transport, **kw))
    rc = cli.main(["run", "--dry-run", "--skip-llm", "--sources", "arxiv", "--limit", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 2
    assert data[0]["source"] == "arxiv"
