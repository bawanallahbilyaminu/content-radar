"""Hugging Face: trending models and daily papers via the public JSON API."""

from __future__ import annotations

from content_radar.collectors.base import HttpClient, parse_datetime
from content_radar.config import Settings
from content_radar.models import Item

MODELS_API = "https://huggingface.co/api/models"
PAPERS_API = "https://huggingface.co/api/daily_papers"


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("huggingface")
    items: list[Item] = []

    models = client.get_json(
        MODELS_API, params={"sort": "trendingScore", "direction": -1, "limit": cfg.limit}
    )
    for model in models:
        model_id = model.get("modelId") or model.get("id")
        if not model_id:
            continue
        tags = [t for t in model.get("tags", []) if ":" not in t][:8]
        pipeline = model.get("pipeline_tag") or "model"
        items.append(
            Item(
                source="huggingface",
                url=f"https://huggingface.co/{model_id}",
                title=f"{model_id} ({pipeline})",
                summary=f"Trending Hugging Face model. Task: {pipeline}. Tags: {', '.join(tags)}",
                published_at=parse_datetime(model.get("createdAt")),
                engagement={
                    "likes": float(model.get("likes") or 0),
                    "downloads": float(model.get("downloads") or 0),
                    "trending_score": float(model.get("trendingScore") or 0),
                },
                kind="trend",
                niche_tags=["ai_ml"],
            )
        )

    papers = client.get_json(PAPERS_API, params={"limit": int(cfg.get("papers_limit", 20))})
    for entry in papers:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id")
        title = entry.get("title") or paper.get("title")
        if not paper_id or not title:
            continue
        items.append(
            Item(
                source="huggingface",
                url=f"https://huggingface.co/papers/{paper_id}",
                title=title,
                summary=(entry.get("summary") or paper.get("summary") or "")[:600],
                published_at=parse_datetime(entry.get("publishedAt") or paper.get("publishedAt")),
                engagement={
                    "upvotes": float(paper.get("upvotes") or 0),
                    "comments": float(entry.get("numComments") or 0),
                },
                kind="paper",
                niche_tags=["ai_ml"],
            )
        )
    return items
