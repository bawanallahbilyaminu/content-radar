"""Fixture-driven tests for each keyless collector. No network: see conftest."""

from __future__ import annotations

import httpx
import pytest

from content_radar.collectors import REGISTRY, arxiv, cisa_kev, github, hackernews, huggingface, rss
from content_radar.collectors.base import HttpClient, safe_collect
from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from tests.conftest import load_fixture, load_json_fixture, mock_transport


@pytest.fixture
def settings(tmp_path):
    return load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")


def _client(responses) -> HttpClient:
    return HttpClient(user_agent="test", transport=mock_transport(responses), sleep=lambda _: None)


def test_registry_has_all_keyless_sources():
    assert set(REGISTRY) >= {"hackernews", "github", "huggingface", "arxiv", "cisa_kev", "rss"}


def test_hackernews(settings):
    hn = load_json_fixture("hn_search.json")
    client = _client({"tags=ask_hn": {"hits": []}, "hn.algolia.com": hn})
    items = hackernews.collect(settings, client)
    assert items
    first = items[0]
    assert first.source == "hackernews"
    assert first.engagement["points"] >= 20
    assert first.published_at is not None and first.published_at.tzinfo is not None
    # fixture hits are returned for every request; dedupe by objectID keeps them unique
    assert len({str(i.url) for i in items}) == len(items)


def test_hackernews_keeps_only_niche_stories(settings):
    hn = load_json_fixture("hn_search.json")
    hn["hits"].append(
        {"objectID": "1", "title": "Best sourdough starter", "url": "https://b.com", "points": 500}
    )
    items = hackernews.collect(
        settings, _client({"tags=ask_hn": {"hits": []}, "hn.algolia.com": hn})
    )
    assert all("sourdough" not in i.title.lower() for i in items)


def test_github(settings):
    data = load_json_fixture("github_search.json")
    items = github.collect(settings, _client({"api.github.com": data}))
    assert items
    assert all(i.kind == "repo" for i in items)
    assert items[0].title == data["items"][0]["full_name"]
    assert items[0].engagement["stars"] == data["items"][0]["stargazers_count"]


def test_github_drops_star_farm_repos(settings):
    data = load_json_fixture("github_search.json")
    spam = dict(data["items"][0])
    spam.update(
        {
            "full_name": "x/ai-trainer-tool",
            "html_url": "https://github.com/x/ai-trainer-tool",
            "stargazers_count": 400,
            "forks_count": 0,
        }
    )
    data["items"].insert(0, spam)
    items = github.collect(settings, _client({"api.github.com": data}))
    assert all(i.title != "x/ai-trainer-tool" for i in items)


def test_github_sends_token_when_present(settings):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"items": []})

    settings.secrets.github_token = "tok"
    client = HttpClient(user_agent="t", transport=httpx.MockTransport(handler))
    assert github.collect(settings, client) == []
    assert seen.get("authorization") == "Bearer tok"


def test_huggingface(settings):
    client = _client(
        {
            "api/models": load_json_fixture("hf_models.json"),
            "api/daily_papers": load_json_fixture("hf_papers.json"),
        }
    )
    items = huggingface.collect(settings, client)
    kinds = {i.kind for i in items}
    assert kinds == {"trend", "paper"}
    model = next(i for i in items if i.kind == "trend")
    assert str(model.url).startswith("https://huggingface.co/")
    assert model.engagement["likes"] > 0
    paper = next(i for i in items if i.kind == "paper")
    assert "/papers/" in str(paper.url)
    assert paper.summary


def test_arxiv(settings):
    items = arxiv.collect(settings, _client({"export.arxiv.org": load_fixture("arxiv.xml")}))
    assert len(items) == 4
    assert all(i.kind == "paper" for i in items)
    assert all(i.published_at is not None for i in items)
    assert "arxiv.org/abs/" in str(items[0].url)
    assert "\n" not in items[0].title


def test_cisa_kev(settings, monkeypatch):
    data = load_json_fixture("cisa_kev.json")
    # Freeze "now" relative to the fixture so the lookback window always includes it.
    newest = max(v["dateAdded"] for v in data["vulnerabilities"])
    from datetime import UTC, datetime

    frozen = datetime.fromisoformat(newest).replace(tzinfo=UTC)
    monkeypatch.setattr(cisa_kev, "now_local", lambda: frozen)
    items = cisa_kev.collect(settings, _client({"cisa.gov": data}))
    assert items
    assert all(i.kind == "vuln" for i in items)
    assert items[0].title.startswith("CVE-")
    assert "nvd.nist.gov" in str(items[0].url)
    assert items[0].published_at >= items[-1].published_at


def test_rss_two_feeds(settings):
    settings.sources["rss"].model_extra["feeds"] = [
        {"name": "bleeping", "url": "https://www.bleepingcomputer.com/feed/", "niche": "cyber"},
        {
            "name": "techcabal",
            "url": "https://techcabal.com/feed/",
            "niche": "other",
            "local": True,
        },
        {"name": "dead", "url": "https://dead.example/feed"},
    ]
    client = _client(
        {
            "bleepingcomputer": load_fixture("rss_bleeping.xml"),
            "techcabal": load_fixture("rss_techcabal.xml"),
            "dead.example": httpx.Response(500, text="boom"),
        }
    )
    items = rss.collect(settings, client)
    sources = {i.source for i in items}
    assert sources == {"rss:bleeping", "rss:techcabal"}  # dead feed skipped, others survive
    bleeping = [i for i in items if i.source == "rss:bleeping"]
    assert bleeping and "cyber" in bleeping[0].niche_tags
    cabal = [i for i in items if i.source == "rss:techcabal"]
    assert cabal and "local" in cabal[0].niche_tags
    assert all("<" not in i.summary for i in items)
    assert all(i.published_at is not None for i in bleeping)


def test_safe_collect_swallows_errors(settings):
    def boom(_s, _c):
        raise RuntimeError("source down")

    assert safe_collect("boom", boom, settings, _client({})) == []


def test_http_client_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    slept: list[float] = []
    client = HttpClient(user_agent="t", transport=httpx.MockTransport(handler), sleep=slept.append)
    assert client.get_json("https://x.test/") == {"ok": True}
    assert calls["n"] == 2
    assert slept == [1.0]


def test_http_client_gives_up_after_max_retries():
    client = HttpClient(
        user_agent="t",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
        max_retries=2,
        sleep=lambda _: None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://x.test/")
