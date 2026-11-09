"""Reddit (OAuth + RSS fallback), Stack Exchange, and Ask HN demand-signal tests."""

from __future__ import annotations

import json

import httpx
import pytest

from content_radar.collectors import REGISTRY, hackernews, reddit, stackexchange
from content_radar.collectors.base import HttpClient
from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from tests.conftest import load_fixture, load_json_fixture, mock_transport


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for var in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "STACKEXCHANGE_KEY"):
        monkeypatch.delenv(var, raising=False)
    return load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")


def _client(responses) -> HttpClient:
    return HttpClient(user_agent="test", transport=mock_transport(responses), sleep=lambda _: None)


def test_registry_includes_demand_sources():
    assert {"reddit", "stackexchange"} <= set(REGISTRY)


# --- Reddit ---------------------------------------------------------------------


def test_reddit_oauth_path_uses_client_credentials_and_drops_authors(settings):
    settings.secrets.reddit_client_id = "cid"
    settings.secrets.reddit_client_secret = "csecret"
    settings.secrets.reddit_user_agent = "content-radar-test/0.1"
    settings.sources["reddit"].model_extra["subreddits"] = ["LocalLLaMA"]
    listing = load_json_fixture("reddit_listing.json")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.reddit.com" and request.url.path == "/api/v1/access_token":
            assert request.method == "POST"
            assert b"grant_type=client_credentials" in request.content
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        assert request.url.host == "oauth.reddit.com"
        assert request.headers["Authorization"] == "bearer tok"
        assert request.headers["User-Agent"] == "content-radar-test/0.1"
        return httpx.Response(200, json=listing)

    client = HttpClient(user_agent="x", transport=httpx.MockTransport(handler))
    items = reddit.collect(settings, client)

    # hot + new for one subreddit, deduped by URL -> 2 real posts, sticky dropped
    assert len(items) == 2
    titles = {i.title for i in items}
    assert "Weekly discussion thread" not in titles
    question = next(i for i in items if i.title.startswith("How do I"))
    assert question.kind == "question"
    assert question.engagement == {"score": 212.0, "comments": 87.0}
    assert question.published_at is not None and question.published_at.utcoffset() is not None
    link_post = next(i for i in items if "DeepSeek" in i.title)
    assert str(link_post.url).startswith("https://github.com/")
    dumped = json.dumps([i.model_dump(mode="json") for i in items])
    assert "should_not_be_stored" not in dumped
    assert "someone_else" not in dumped
    assert "Body text" not in dumped
    # token request + hot + new
    assert len(seen) == 3


def test_reddit_falls_back_to_rss_when_credentials_missing(settings):
    settings.sources["reddit"].model_extra["subreddits"] = ["MachineLearning", "Nigeria"]
    client = _client(
        {
            "r/MachineLearning/new/.rss": load_fixture("reddit_rss.xml"),
            "r/Nigeria/new/.rss": httpx.Response(429, text="slow down"),
        }
    )
    items = reddit.collect(settings, client)
    assert items and all(i.source == "reddit:MachineLearning" for i in items)
    assert all(i.published_at is not None for i in items)
    dumped = json.dumps([i.model_dump(mode="json") for i in items])
    assert "/u/" not in dumped  # author handles never stored


def test_reddit_falls_back_to_rss_when_oauth_refused(settings):
    settings.secrets.reddit_client_id = "cid"
    settings.secrets.reddit_client_secret = "csecret"
    settings.sources["reddit"].model_extra["subreddits"] = ["MachineLearning"]
    client = _client(
        {
            "api/v1/access_token": httpx.Response(401, json={"error": "unauthorized"}),
            "r/MachineLearning/new/.rss": load_fixture("reddit_rss.xml"),
        }
    )
    items = reddit.collect(settings, client)
    assert items and items[0].source == "reddit:MachineLearning"


def test_reddit_rss_fallback_can_be_disabled(settings):
    settings.sources["reddit"].model_extra["rss_fallback"] = False
    assert reddit.collect(settings, _client({})) == []


# --- Stack Exchange -------------------------------------------------------------


def test_stackexchange_questions(settings):
    data = load_json_fixture("se_questions.json")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.params["sort"] == "votes"
        assert "fromdate" in request.url.params
        return httpx.Response(200, json=data)

    client = HttpClient(user_agent="t", transport=httpx.MockTransport(handler))
    items = stackexchange.collect(settings, client)
    assert items
    assert all(i.kind == "question" for i in items)
    assert all(i.source.startswith("stackexchange:") for i in items)
    assert "&quot;" not in items[0].title
    assert items[0].engagement["views"] > 0
    assert items == sorted(items, key=lambda i: i.engagement["score"], reverse=True)
    # 5 stackoverflow tags + datascience + security + ai
    assert len(calls) == 8
    assert sum("tagged=" in c for c in calls) == 5


def test_stackexchange_honours_backoff_and_quota_floor(settings):
    data = load_json_fixture("se_questions.json")
    slept: list[float] = []
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        body = dict(data)
        if n["calls"] == 1:
            body["backoff"] = 7
        if n["calls"] == 2:
            body["quota_remaining"] = 3  # below the floor of 20
        return httpx.Response(200, json=body)

    import time

    client = HttpClient(user_agent="t", transport=httpx.MockTransport(handler))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(time, "sleep", slept.append)
        stackexchange.collect(settings, client)
    assert slept == [7.0]
    assert n["calls"] == 2  # stopped after quota dropped below the floor


def test_stackexchange_stops_on_throttle_error(settings):
    n = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["calls"] += 1
        return httpx.Response(400, json={"error_id": 502, "error_name": "throttle_violation"})

    client = HttpClient(
        user_agent="t", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    assert stackexchange.collect(settings, client) == []
    assert n["calls"] == 1


def test_stackexchange_sends_key_when_present(settings):
    settings.secrets.stackexchange_key = "abc"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("key", ""))
        return httpx.Response(200, json={"items": [], "quota_remaining": 9000})

    stackexchange.collect(
        settings, HttpClient(user_agent="t", transport=httpx.MockTransport(handler))
    )
    assert seen and all(k == "abc" for k in seen)


# --- Ask HN ---------------------------------------------------------------------


def test_hackernews_ask_hn_marked_as_question(settings):
    ask = load_json_fixture("hn_ask.json")
    ask["hits"].append(
        {
            "objectID": "999",
            "title": "Ask HN: How do I run an LLM offline on a cheap laptop?",
            "url": None,
            "points": 40,
            "num_comments": 12,
            "created_at": "2026-11-09T10:00:00Z",
            "_tags": ["story", "ask_hn"],
        }
    )
    client = _client(
        {
            "tags=ask_hn": ask,
            "hn.algolia.com": load_json_fixture("hn_search.json"),
        }
    )
    items = hackernews.collect(settings, client)
    laptop = next(i for i in items if i.title.startswith("Ask HN: How do I run an LLM"))
    assert laptop.kind == "question"
    assert str(laptop.url) == "https://news.ycombinator.com/item?id=999"
    # off-niche Ask HN posts (C++ enjoyment, 'what are you working on') are dropped
    assert all("C++" not in i.title for i in items)
    # anything Algolia tagged ask_hn is a question; keyword-search stories stay trends
    assert all(i.kind == "question" for i in items if "ask_hn" in _tags_of(i, ask))
    assert any(i.kind == "trend" for i in items)


def _tags_of(item, ask_payload) -> list[str]:
    for hit in ask_payload["hits"]:
        if hit["title"] == item.title:
            return hit.get("_tags", [])
    return []
