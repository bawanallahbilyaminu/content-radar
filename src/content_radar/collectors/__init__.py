"""Collector registry. Each collector is `collect(settings, client) -> list[Item]`."""

from __future__ import annotations

from content_radar.collectors import (
    arxiv,
    cisa_kev,
    github,
    hackernews,
    huggingface,
    reddit,
    rss,
    stackexchange,
)
from content_radar.collectors.base import Collector

REGISTRY: dict[str, Collector] = {
    "hackernews": hackernews.collect,
    "github": github.collect,
    "huggingface": huggingface.collect,
    "arxiv": arxiv.collect,
    "cisa_kev": cisa_kev.collect,
    "rss": rss.collect,
    "reddit": reddit.collect,
    "stackexchange": stackexchange.collect,
}

__all__ = ["REGISTRY", "Collector"]
