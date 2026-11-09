"""Load config.yaml and environment variables into one typed Settings object."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class RunConfig(BaseModel):
    max_ideas_per_run: int = 7
    lookback_hours: int = 48
    timezone: str = "Africa/Lagos"
    user_agent: str = "content-radar/0.1 (+https://github.com/bawanallahbilyaminu/content-radar)"
    http_timeout_seconds: float = 20.0


class ModelsConfig(BaseModel):
    filter: str = "claude-haiku-5-5"
    synthesize: str = "claude-sonnet-5-5"
    filter_batch_size: int = 50


class NicheConfig(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    local_keywords: list[str] = Field(default_factory=list)
    question_markers: list[str] = Field(default_factory=list)


class SourceConfig(BaseModel):
    """Per-source settings. Extra keys are kept so each collector can read its own."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    limit: int = 50

    def get(self, key: str, default: Any = None) -> Any:
        extra = self.model_extra or {}
        return extra.get(key, default)


class Secrets(BaseModel):
    anthropic_api_key: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str | None = None
    github_token: str | None = None
    notion_token: str | None = None
    notion_ideas_db_id: str | None = None
    youtube_api_key: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        def get(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        return cls(
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            reddit_client_id=get("REDDIT_CLIENT_ID"),
            reddit_client_secret=get("REDDIT_CLIENT_SECRET"),
            reddit_user_agent=get("REDDIT_USER_AGENT"),
            github_token=get("GITHUB_TOKEN"),
            notion_token=get("NOTION_TOKEN"),
            notion_ideas_db_id=get("NOTION_IDEAS_DB_ID"),
            youtube_api_key=get("YOUTUBE_API_KEY"),
        )


class Settings(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    niche: NicheConfig = Field(default_factory=NicheConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    secrets: Secrets = Field(default_factory=Secrets)

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name, SourceConfig(enabled=False))

    def enabled_sources(self) -> list[str]:
        return [name for name, cfg in self.sources.items() if cfg.enabled]


def load_settings(config_path: Path | None = None, env_file: Path | None = None) -> Settings:
    """Read config.yaml and .env. Missing files fall back to defaults."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    path = config_path or DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.pop("secrets", None)  # secrets never come from yaml
    settings = Settings.model_validate(raw)
    settings.secrets = Secrets.from_env()
    return settings
