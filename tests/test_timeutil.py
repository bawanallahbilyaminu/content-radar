"""All dates and windows must be Africa/Lagos."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from content_radar import timeutil
from content_radar.config import DEFAULT_CONFIG_PATH, load_settings
from content_radar.models import Item
from content_radar.normalize import within_lookback

LAGOS = ZoneInfo("Africa/Lagos")


def test_config_sets_lagos(tmp_path):
    load_settings(DEFAULT_CONFIG_PATH, env_file=tmp_path / "none.env")
    assert str(timeutil.local_tz()) == "Africa/Lagos"
    assert timeutil.now_local().utcoffset() == timedelta(hours=1)  # WAT, no DST


def test_item_dates_are_stored_in_lagos_time():
    utc_item = Item(
        source="x",
        url="https://a.com",
        title="t",
        published_at=datetime(2026, 11, 9, 5, 0, tzinfo=UTC),
    )
    assert utc_item.published_at == datetime(2026, 11, 9, 6, 0, tzinfo=LAGOS)
    assert utc_item.published_at.utcoffset() == timedelta(hours=1)
    assert utc_item.model_dump(mode="json")["published_at"].endswith("+01:00")

    naive_item = Item(
        source="x", url="https://a.com", title="t", published_at=datetime(2026, 11, 9, 5, 0)
    )
    assert naive_item.published_at == utc_item.published_at  # naive means UTC


def test_from_timestamp_is_lagos():
    dt = timeutil.from_timestamp(0)
    assert dt == datetime(1970, 1, 1, 1, 0, tzinfo=LAGOS)


def test_lookback_window_uses_lagos_clock_and_same_instant():
    # 05:00 UTC daily run == 06:00 Lagos. An item from 47h before must survive a 48h window
    # regardless of which zone it was reported in.
    run_at = datetime(2026, 11, 9, 6, 0, tzinfo=LAGOS)
    reported_utc = Item(
        source="x",
        url="https://a.com/1",
        title="a",
        published_at=run_at.astimezone(UTC) - timedelta(hours=47),
    )
    reported_lagos = Item(
        source="x", url="https://a.com/2", title="b", published_at=run_at - timedelta(hours=47)
    )
    too_old = Item(
        source="x", url="https://a.com/3", title="c", published_at=run_at - timedelta(hours=49)
    )
    kept = within_lookback([reported_utc, reported_lagos, too_old], hours=48, now=run_at)
    assert kept == [reported_utc, reported_lagos]


def test_set_timezone_rejects_garbage():
    import zoneinfo

    import pytest

    with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
        timeutil.set_timezone("Mars/Olympus")
    timeutil.set_timezone("Africa/Lagos")
