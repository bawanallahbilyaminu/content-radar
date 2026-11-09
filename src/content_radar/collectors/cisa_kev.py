"""CISA Known Exploited Vulnerabilities catalog: entries added recently."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from content_radar.collectors.base import HttpClient
from content_radar.config import Settings
from content_radar.models import Item
from content_radar.timeutil import now_local

FEED = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def collect(settings: Settings, client: HttpClient) -> list[Item]:
    cfg = settings.source("cisa_kev")
    # KEV additions are sparse, so look back further than the general window by default.
    lookback_days = int(cfg.get("lookback_days", 7))
    cutoff = (now_local() - timedelta(days=lookback_days)).date()

    data = client.get_json(FEED)
    vulns = sorted(
        data.get("vulnerabilities", []), key=lambda v: v.get("dateAdded", ""), reverse=True
    )

    items: list[Item] = []
    for v in vulns:
        added = _parse_date(v.get("dateAdded"))
        if added is None or added < cutoff:
            continue
        cve = v.get("cveID", "").strip()
        if not cve:
            continue
        ransomware = str(v.get("knownRansomwareCampaignUse", "")).lower() == "known"
        items.append(
            Item(
                source="cisa_kev",
                url=f"https://nvd.nist.gov/vuln/detail/{cve}",
                title=f"{cve}: {v.get('vulnerabilityName') or v.get('product', 'Unknown')}",
                summary=(
                    f"{v.get('vendorProject', '')} {v.get('product', '')}. "
                    f"{v.get('shortDescription', '')} "
                    f"Required action: {v.get('requiredAction', '')} "
                    f"Due: {v.get('dueDate', '')}."
                ).strip()[:600],
                published_at=datetime(added.year, added.month, added.day, tzinfo=UTC),
                engagement={"ransomware_known": 1.0 if ransomware else 0.0},
                kind="vuln",
                niche_tags=["cyber"],
            )
        )
        if len(items) >= cfg.limit:
            break
    return items


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
