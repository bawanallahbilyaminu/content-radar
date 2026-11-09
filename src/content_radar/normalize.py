"""Keyword tagging, question detection and in-run dedupe."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from content_radar.config import NicheConfig
from content_radar.models import Item, normalize_title

log = logging.getLogger(__name__)

# Keyword -> niche bucket. Anything unmatched but still containing a niche keyword is "other".
_NICHE_BUCKETS: dict[str, tuple[str, ...]] = {
    "cyber": (
        "cybersecurity",
        "infosec",
        "ransomware",
        "phishing",
        "malware",
        "vulnerability",
        "cve",
        "zero-day",
        "exploit",
        "breach",
        "data leak",
        "hacked",
        "scam",
        "security",
    ),
    "data": (
        "data science",
        "data engineering",
        "pandas",
        "sql",
        "dbt",
        "spark",
        "airflow",
        "analytics",
        "dataset",
        "warehouse",
    ),
    "ai_ml": (
        "ai",
        "artificial intelligence",
        "llm",
        "large language model",
        "gpt",
        "claude",
        "gemini",
        "llama",
        "openai",
        "anthropic",
        "deepseek",
        "mistral",
        "machine learning",
        "deep learning",
        "neural network",
        "transformer",
        "rag",
        "agents",
        "agentic",
        "mcp",
        "fine-tuning",
        "prompt",
        "hugging face",
        "pytorch",
        "model",
    ),
}


def _word_pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])")


class Tagger:
    """Compiles the niche / local keyword lists once per run."""

    def __init__(self, niche: NicheConfig) -> None:
        self.keywords = [(k.lower(), _word_pattern(k)) for k in niche.keywords]
        self.local = [(k.lower(), _word_pattern(k)) for k in niche.local_keywords]
        self.question_markers = [m.lower() for m in niche.question_markers]

    def matched_keywords(self, text: str) -> list[str]:
        text = text.lower()
        return [k for k, pat in self.keywords if pat.search(text)]

    def is_local(self, text: str) -> bool:
        text = text.lower()
        return any(pat.search(text) for _, pat in self.local)

    def is_question(self, title: str) -> bool:
        t = title.lower()
        return any(m in t for m in self.question_markers)

    def tags_for(self, text: str) -> list[str]:
        """Return niche buckets hit plus 'local' when a Nigeria/Africa keyword appears."""
        hits = set(self.matched_keywords(text))
        tags: list[str] = []
        for bucket, words in _NICHE_BUCKETS.items():
            if hits.intersection(words) or any(
                _word_pattern(w).search(text.lower()) for w in words
            ):
                tags.append(bucket)
        if self.is_local(text):
            tags.append("local")
        return tags

    def apply(self, item: Item) -> Item:
        """Fill niche_tags and promote to kind='question' when title phrasing asks for help."""
        text = f"{item.title} {item.summary}"
        tags = self.tags_for(text)
        merged = list(dict.fromkeys([*item.niche_tags, *tags]))
        kind = item.kind
        if kind == "trend" and self.is_question(item.title):
            kind = "question"
        return item.model_copy(update={"niche_tags": merged, "kind": kind})


def dedupe(items: Iterable[Item]) -> list[Item]:
    """Drop repeats by canonical URL or normalized title. The first occurrence wins,
    but engagement from later duplicates is merged in and the source list recorded."""
    by_url: dict[str, Item] = {}
    by_title: dict[str, Item] = {}
    out: list[Item] = []
    for item in items:
        url_key = item.canonical
        title_key = normalize_title(item.title)
        existing = by_url.get(url_key) or by_title.get(title_key)
        if existing is None:
            by_url[url_key] = item
            by_title[title_key] = item
            out.append(item)
            continue
        _merge_duplicate(existing, item)
    return out


def _merge_duplicate(keep: Item, dup: Item) -> None:
    for key, value in dup.engagement.items():
        keep.engagement[key] = max(keep.engagement.get(key, 0.0), value)
    sources = keep.engagement.setdefault("cross_source_count", 1.0)
    if dup.source.split(":")[0] != keep.source.split(":")[0]:
        keep.engagement["cross_source_count"] = sources + 1
    for tag in dup.niche_tags:
        if tag not in keep.niche_tags:
            keep.niche_tags.append(tag)
    if dup.kind == "question" and keep.kind == "trend":
        keep.kind = "question"


def within_lookback(items: Iterable[Item], hours: int, now: datetime | None = None) -> list[Item]:
    """Keep items published inside the window. Items with no date are kept (feeds vary)."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=hours)
    return [i for i in items if i.published_at is None or i.published_at >= cutoff]
