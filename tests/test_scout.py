"""Worth the Clone? repo scout: parsing, flags, momentum, Notion upsert, full run.
No network: see conftest. The trending HTML fixtures are hand-made to mirror
github.com/trending markup; the first live dry run confirms the real page parses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.scout.models import TrendingRepo
from content_radar.scout.rank import (
    apply_details,
    compute_flags,
    merge,
    next_streak,
    score,
)
from content_radar.scout.run import run_scout
from content_radar.scout.sink import HUMAN_PROPERTIES, RepoSink
from content_radar.scout.sources import parse_ossinsight, parse_search, parse_trending_html
from tests.conftest import load_fixture, load_json_fixture, mock_transport


@pytest.fixture
def settings(tmp_path):
    return load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")


# --- sources ---------------------------------------------------------------------


def test_parse_trending_weekly():
    repos = parse_trending_html(load_fixture("scout_trending_weekly.html"), "trending_weekly")
    by_name = {r.full_name: r for r in repos}
    assert list(by_name) == ["acme/quickvec", "lists-r-us/awesome-agents", "freestuff99/game-tools"]
    q = by_name["acme/quickvec"]
    assert q.stars == 12480
    assert q.forks == 640
    assert q.stars_week == 3210
    assert q.stars_today is None
    assert q.language == "Python"
    assert q.description == "A tiny vector database that runs on a laptop & needs no GPU"
    assert q.sources == {"trending_weekly"}
    assert by_name["lists-r-us/awesome-agents"].language is None


def test_parse_trending_daily_sets_stars_today():
    repos = parse_trending_html(load_fixture("scout_trending_daily.html"), "trending_daily")
    assert [(r.full_name, r.stars_today, r.stars_week) for r in repos] == [
        ("acme/quickvec", 402, None),
        ("devtools/fastlint", 350, None),
    ]


def test_parse_trending_html_tolerates_unknown_markup():
    assert parse_trending_html("<html><body><p>Nothing here</p></body></html>", "x") == []


def test_parse_ossinsight_rows_and_bad_rows():
    repos = parse_ossinsight(load_json_fixture("scout_ossinsight_week.json"), "ossinsight_week")
    assert [(r.full_name, r.stars_week, r.language) for r in repos] == [
        ("acme/quickvec", 3350, "Python"),
        ("orbit/termdash", 1440, "Go"),
    ]
    flat = parse_ossinsight({"rows": [{"repo_name": "a/b", "stars": 7}]}, "ossinsight_day")
    assert flat[0].stars_today == 7
    assert parse_ossinsight({"unexpected": True}, "ossinsight_day") == []


def test_parse_search_counts_total_stars_as_this_week():
    repos = parse_search(load_json_fixture("scout_github_search.json"))
    assert repos[0].full_name == "newco/agent-kit"
    assert repos[0].stars == repos[0].stars_week == 2100
    assert repos[0].sources == {"search"}


# --- merge, flags, score -----------------------------------------------------------


def test_merge_unions_sources_and_keeps_largest_numbers():
    weekly = parse_trending_html(load_fixture("scout_trending_weekly.html"), "trending_weekly")
    daily = parse_trending_html(load_fixture("scout_trending_daily.html"), "trending_daily")
    oss = parse_ossinsight(load_json_fixture("scout_ossinsight_week.json"), "ossinsight_week")
    merged = {r.full_name: r for r in merge(weekly + daily + oss)}
    q = merged["acme/quickvec"]
    assert q.sources == {"trending_weekly", "trending_daily", "ossinsight_week"}
    assert q.stars_week == 3350  # OSS Insight saw more than the trending page
    assert q.stars_today == 402
    assert q.stars == 12511
    assert len(merged) == 5


def test_merge_is_case_insensitive_and_does_not_mutate_input():
    a = TrendingRepo(full_name="Acme/QuickVec", stars_week=5, sources={"search"})
    b = TrendingRepo(full_name="acme/quickvec", stars_week=9, sources={"trending_weekly"})
    merged = merge([a, b])
    assert len(merged) == 1 and merged[0].stars_week == 9
    assert a.stars_week == 5 and a.sources == {"search"}


def test_compute_flags(settings):
    cfg = settings.scout
    today = datetime(2026, 11, 20, tzinfo=UTC).date()
    awesome = TrendingRepo(full_name="lists-r-us/awesome-agents")
    lure = TrendingRepo(
        full_name="freestuff99/game-tools",
        description="Undetected aimbot and license key generator",
        stars=4100,
        forks=2,
    )
    fresh = TrendingRepo(full_name="newco/agent-kit", stars=2100, forks=90)
    apply_details(
        fresh,
        {
            "full_name": "newco/agent-kit",
            "license": None,
            "created_at": "2026-11-15T10:00:00Z",
            "pushed_at": "2026-11-19T10:00:00Z",
            "archived": False,
            "topics": ["agents"],
        },
    )
    assert compute_flags(awesome, cfg, today) == ["list"]
    assert compute_flags(lure, cfg, today) == ["low-forks", "lure-words"]
    assert compute_flags(fresh, cfg, today) == ["new-repo", "no-license"]


def test_apply_details_reads_licence_dates_and_fork():
    repo = TrendingRepo(full_name="acme/quickvec")
    apply_details(
        repo,
        {
            "full_name": "acme/quickvec",
            "stargazers_count": 12600,
            "forks_count": 650,
            "license": {"spdx_id": "MIT", "name": "MIT License"},
            "created_at": "2025-01-02T03:04:05Z",
            "pushed_at": "2026-11-19T08:00:00Z",
            "archived": False,
            "fork": True,
            "topics": ["vector-database"],
        },
    )
    assert (repo.stars, repo.forks, repo.license) == (12600, 650, "MIT")
    assert repo.created_at.year == 2025 and repo.enriched
    assert repo.flags == ["fork"]


def test_score_rewards_sustained_trending(settings):
    cfg = settings.scout
    steady = TrendingRepo(full_name="a/steady", stars_week=1000, stars_today=100, streak=7)
    spike = TrendingRepo(full_name="b/spike", stars_week=1000, stars_today=100, streak=1)
    quiet = TrendingRepo(full_name="c/quiet", stars_week=10, stars_today=1, streak=1)
    score([steady, spike, quiet], cfg)
    assert steady.momentum == 100
    assert spike.momentum < steady.momentum
    assert quiet.momentum < spike.momentum
    assert all(0 <= r.momentum <= 100 for r in (steady, spike, quiet))


def test_next_streak():
    assert next_streak(None, 0, "2026-11-20") == (1, True)
    assert next_streak("2026-11-19", 3, "2026-11-20") == (4, True)
    assert next_streak("2026-11-20", 4, "2026-11-20") == (4, False)  # same-day re-run
    assert next_streak("2026-11-10", 6, "2026-11-20") == (1, True)


# --- Notion sink -------------------------------------------------------------------


class FakeRepoNotion:
    def __init__(self, rows):
        self.rows = rows
        self.created, self.page_updates, self.schema_updates, self.queries = [], [], [], []
        self.databases = SimpleNamespace(
            retrieve=lambda database_id: {"id": database_id, "data_sources": [{"id": "ds_r"}]}
        )
        self.data_sources = SimpleNamespace(
            query=self._query,
            retrieve=lambda data_source_id: {"id": data_source_id, "properties": {"Repo": {}}},
            update=lambda data_source_id, properties: self.schema_updates.append(properties),
        )
        self.pages = SimpleNamespace(create=self._create, update=self._update)

    def _query(self, data_source_id, filter, page_size):  # noqa: A002 - Notion's kwarg name
        assert data_source_id == "ds_r"
        wanted = filter["title"]["equals"].lower()
        self.queries.append(wanted)
        hits = [r for r in self.rows if _title(r).lower() == wanted]
        return {"results": hits[:page_size], "has_more": False}

    def _create(self, parent, properties):
        assert parent == {"type": "data_source_id", "data_source_id": "ds_r"}
        self.created.append(properties)
        return {"id": f"page_{len(self.created)}"}

    def _update(self, page_id, properties):
        self.page_updates.append((page_id, properties))
        return {"id": page_id}


def _title(row):
    return "".join(t["plain_text"] for t in row["properties"]["Repo"]["title"])


def _existing(name, last_seen, streak, days_seen, first_seen="2026-11-01"):
    return {
        "id": f"row-{name}",
        "properties": {
            "Repo": {"type": "title", "title": [{"plain_text": name}]},
            "Last Seen": {"type": "date", "date": {"start": last_seen}},
            "First Seen": {"type": "date", "date": {"start": first_seen}},
            "Streak": {"type": "number", "number": streak},
            "Days Seen": {"type": "number", "number": days_seen},
            "Pick": {"type": "select", "select": {"name": "Deep"}},
        },
    }


def test_sink_creates_new_rows_and_updates_existing_without_touching_human_columns():
    fake = FakeRepoNotion([_existing("acme/quickvec", "2026-11-19", 2, 5)])
    sink = RepoSink(fake, "db_r")
    old = TrendingRepo(full_name="acme/quickvec", stars=12600, stars_week=3350, sources={"search"})
    new = TrendingRepo(full_name="devtools/fastlint", stars=980, stars_today=350)
    sink.load_history([old, new], today="2026-11-20")
    assert (old.streak, old.days_seen) == (3, 6)
    assert (new.streak, new.days_seen) == (1, 1)

    created, updated = sink.write([old, new], today="2026-11-20")
    assert (created, updated) == (1, 1)
    page_id, props = fake.page_updates[0]
    assert page_id == "row-acme/quickvec"
    assert props["First Seen"] == {"date": {"start": "2026-11-01"}}
    assert props["Last Seen"] == {"date": {"start": "2026-11-20"}}
    assert props["Streak"] == {"number": 3}
    new_props = fake.created[0]
    assert new_props["First Seen"] == new_props["Last Seen"] == {"date": {"start": "2026-11-20"}}
    for written in (props, new_props):
        assert not set(written) & set(HUMAN_PROPERTIES)
    assert fake.schema_updates  # missing columns were added, human columns included
    assert set(HUMAN_PROPERTIES) <= set(fake.schema_updates[0])
    assert fake.queries.count("acme/quickvec") == 1  # cached between history and write


def test_sink_same_day_rerun_does_not_double_count():
    fake = FakeRepoNotion([_existing("acme/quickvec", "2026-11-20", 3, 6)])
    repo = TrendingRepo(full_name="acme/quickvec")
    RepoSink(fake, "db_r").load_history([repo], today="2026-11-20")
    assert (repo.streak, repo.days_seen) == (3, 6)


# --- full run ----------------------------------------------------------------------


def _details(name, created="2024-05-01T00:00:00Z", license_id="MIT"):
    return {
        "full_name": name,
        "license": {"spdx_id": license_id} if license_id else None,
        "created_at": created,
        "pushed_at": "2026-11-19T00:00:00Z",
        "archived": False,
        "topics": [],
    }


def _transport():
    recent = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return mock_transport(
        {
            "trending?since=daily": load_fixture("scout_trending_daily.html"),
            "trending?since=weekly": load_fixture("scout_trending_weekly.html"),
            "period=past_week": load_json_fixture("scout_ossinsight_week.json"),
            "period=past_24_hours": httpx.Response(503),  # a broken source must not stop the run
            "search/repositories": load_json_fixture("scout_github_search.json"),
            "repos/acme/quickvec": _details("acme/quickvec"),
            "repos/devtools/fastlint": _details("devtools/fastlint"),
            "repos/newco/agent-kit": _details("newco/agent-kit", created=recent, license_id=None),
            "repos/orbit/termdash": _details("orbit/termdash"),
            "repos/freestuff99/game-tools": _details("freestuff99/game-tools"),
        }
    )


def test_run_scout_end_to_end_writes_ranked_rows(settings):
    settings.scout.request_pause_seconds = 0
    fake = FakeRepoNotion([_existing("acme/quickvec", "2026-11-19", 4, 9)])
    result = run_scout(settings, transport=_transport(), notion=RepoSink(fake, "db_r"))

    names = [r.full_name for r in result.repos]
    assert "lists-r-us/awesome-agents" not in names  # excluded: list
    assert result.excluded == 1
    assert names[0] == "acme/quickvec"  # most sources, biggest week, longest streak
    assert set(names) == {
        "acme/quickvec",
        "devtools/fastlint",
        "newco/agent-kit",
        "orbit/termdash",
        "freestuff99/game-tools",
    }
    flagged = {r.full_name: r.flags for r in result.repos}
    assert "lure-words" in flagged["freestuff99/game-tools"]
    assert {"new-repo", "no-license"} <= set(flagged["newco/agent-kit"])
    assert result.per_source["ossinsight_day"] == 0
    assert (result.created, result.updated) == (4, 1)


def test_run_scout_dry_run_writes_nothing(settings):
    settings.scout.request_pause_seconds = 0
    fake = FakeRepoNotion([])
    result = run_scout(
        settings, dry_run=True, transport=_transport(), notion=RepoSink(fake, "db_r")
    )
    assert result.repos and not fake.created and not fake.page_updates and not fake.queries


def test_run_scout_without_notion_ids_skips_writing(settings):
    settings.scout.request_pause_seconds = 0
    result = run_scout(settings, transport=_transport())
    assert "NOTION_REPOS_DB_ID" in result.skipped_reason
    assert result.repos


def test_config_has_scout_section(settings):
    assert settings.scout.enabled
    assert "trending_weekly" in settings.scout.sources
    assert (
        settings.scout.weights.week + settings.scout.weights.today + (settings.scout.weights.streak)
        == 100
    )
