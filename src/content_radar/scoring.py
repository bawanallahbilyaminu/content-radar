"""Deterministic signal scoring and clustering. No LLM calls here.

Signals per item: recency, engagement (log-scaled, per source family), question
flag, local-angle flag. Items are then grouped into clusters by title overlap;
a cluster seen on 2+ source families gets the cross-source bonus, the strongest
single signal we have.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from content_radar.models import Item, normalize_title, source_key
from content_radar.timeutil import now_local

# Hours until the recency signal halves, by item kind. Papers and vulns age slower.
HALF_LIFE_HOURS = {"trend": 18.0, "question": 48.0, "repo": 72.0, "paper": 96.0, "vuln": 72.0}

# Engagement keys worth counting and the value at which they saturate (log scale).
ENGAGEMENT_SCALE = {
    "points": 300.0,
    "score": 300.0,
    "comments": 150.0,
    "stars": 1500.0,
    "likes": 2000.0,
    "upvotes": 60.0,
    "views": 2000.0,
    "answers": 5.0,
    "trending_score": 500.0,
    "downloads": 500_000.0,
}

WEIGHTS = {"recency": 30.0, "engagement": 30.0, "question": 15.0, "local": 15.0, "ransomware": 10.0}
CROSS_SOURCE_BONUS = 15.0  # per extra source family, capped below
CROSS_SOURCE_CAP = 45.0

_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "with",
    "is",
    "are",
    "was",
    "be",
    "by",
    "at",
    "from",
    "as",
    "it",
    "its",
    "this",
    "that",
    "how",
    "what",
    "why",
    "can",
    "do",
    "does",
    "my",
    "your",
    "new",
    "vs",
    "via",
    "into",
    "about",
    "after",
    "over",
    "using",
    "use",
    "ask",
    "hn",
    "tell",
    "show",
    "help",
    "need",
    "any",
    "anyone",
    "should",
    "could",
}
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]{2,}")


def significant_tokens(title: str) -> frozenset[str]:
    return frozenset(
        t for t in _TOKEN_RE.findall(normalize_title(title).replace(" ", " ")) if t not in _STOP
    )


def recency_signal(item: Item, now: datetime) -> float:
    age = item.age_hours(now)
    if age is None:
        return 0.5  # undated: neutral
    half_life = HALF_LIFE_HOURS.get(item.kind, 24.0)
    return 0.5 ** (age / half_life)


def engagement_signal(item: Item) -> float:
    """Best single normalized engagement metric in [0, 1], log-scaled."""
    best = 0.0
    for key, scale in ENGAGEMENT_SCALE.items():
        value = item.engagement.get(key)
        if not value or value <= 0:
            continue
        best = max(best, min(1.0, math.log1p(value) / math.log1p(scale)))
    return best


def item_score(item: Item, now: datetime | None = None) -> float:
    now = now or now_local()
    score = WEIGHTS["recency"] * recency_signal(item, now)
    score += WEIGHTS["engagement"] * engagement_signal(item)
    if item.kind == "question":
        score += WEIGHTS["question"]
    if "local" in item.niche_tags:
        score += WEIGHTS["local"]
    if item.engagement.get("ransomware_known"):
        score += WEIGHTS["ransomware"]
    return round(score, 2)


@dataclass
class Cluster:
    items: list[Item]
    score: float = 0.0
    tokens: frozenset[str] = field(default_factory=frozenset)

    @property
    def lead(self) -> Item:
        return self.items[0]

    @property
    def source_families(self) -> set[str]:
        return {source_key(i.source) for i in self.items}

    @property
    def cross_source_count(self) -> int:
        merged = max(
            (int(i.engagement.get("cross_source_count", 1)) for i in self.items), default=1
        )
        return max(len(self.source_families), merged)

    @property
    def has_question(self) -> bool:
        return any(i.kind == "question" for i in self.items)

    @property
    def has_local(self) -> bool:
        return any("local" in i.niche_tags for i in self.items)

    def urls(self) -> list[str]:
        seen: list[str] = []
        for i in self.items:
            u = str(i.url)
            if u not in seen:
                seen.append(u)
        return seen


def _similar(a: frozenset[str], b: frozenset[str], threshold: float) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    if inter < 2:
        return False
    return inter / len(a | b) >= threshold


def cluster_items(items: list[Item], threshold: float = 0.45) -> list[Cluster]:
    """Greedy clustering by significant-token Jaccard similarity on titles."""
    clusters: list[Cluster] = []
    for item in items:
        toks = significant_tokens(item.title)
        for cluster in clusters:
            if _similar(toks, cluster.tokens, threshold):
                cluster.items.append(item)
                cluster.tokens = cluster.tokens | toks
                break
        else:
            clusters.append(Cluster(items=[item], tokens=toks))
    return clusters


def rank_clusters(items: list[Item], now: datetime | None = None) -> list[Cluster]:
    """Score items, cluster them, score clusters, return best first."""
    now = now or now_local()
    scored = sorted(items, key=lambda i: item_score(i, now), reverse=True)
    clusters = cluster_items(scored)
    for cluster in clusters:
        base = max(item_score(i, now) for i in cluster.items)
        extra_sources = max(0, cluster.cross_source_count - 1)
        bonus = min(CROSS_SOURCE_CAP, CROSS_SOURCE_BONUS * extra_sources)
        cluster.score = round(min(100.0, base + bonus), 2)
    clusters.sort(key=lambda c: c.score, reverse=True)
    return clusters
