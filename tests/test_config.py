from pathlib import Path

from content_radar.config import DEFAULT_CONFIG_PATH, Settings, load_settings


def test_repo_config_loads():
    settings = load_settings(DEFAULT_CONFIG_PATH, env_file=Path("nonexistent.env"))
    assert settings.run.max_ideas_per_run == 7
    assert settings.models.filter_batch_size == 50
    assert "hackernews" in settings.enabled_sources()
    assert "youtube" not in settings.enabled_sources()
    assert settings.niche.keywords
    assert settings.niche.local_keywords


def test_source_extra_keys_accessible():
    settings = load_settings(DEFAULT_CONFIG_PATH, env_file=Path("nonexistent.env"))
    feeds = settings.source("rss").get("feeds")
    assert isinstance(feeds, list) and feeds
    assert settings.source("arxiv").get("categories") == ["cs.AI", "cs.LG", "cs.CL", "cs.CR"]


def test_unknown_source_is_disabled():
    assert Settings().source("nope").enabled is False


def test_secrets_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  test-key ")
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    settings = load_settings(tmp_path / "missing.yaml", env_file=tmp_path / "missing.env")
    assert settings.secrets.anthropic_api_key == "test-key"
    assert settings.secrets.notion_token is None


def test_secrets_in_yaml_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text("secrets:\n  anthropic_api_key: leaked\n", encoding="utf-8")
    settings = load_settings(cfg, env_file=tmp_path / "missing.env")
    assert settings.secrets.anthropic_api_key is None
