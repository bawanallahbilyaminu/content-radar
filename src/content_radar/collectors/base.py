"""Shared HTTP client and the fail-safe wrapper every collector runs under."""

from __future__ import annotations

import calendar
import html
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from content_radar.config import Settings
from content_radar.models import Item

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class HttpClient:
    """Thin httpx wrapper: descriptive User-Agent, timeout, backoff on 429/5xx."""

    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json, text/xml, */*"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self.max_retries = max_retries
        self._sleep = sleep or (lambda d: time.sleep(d))

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        attempt = 0
        while True:
            response = self._client.get(url, **kwargs)
            if response.status_code not in RETRY_STATUSES or attempt >= self.max_retries:
                response.raise_for_status()
                return response
            attempt += 1
            delay = _retry_delay(response, attempt)
            log.warning(
                "GET %s -> %s, retry %d in %.1fs", url, response.status_code, attempt, delay
            )
            self._sleep(delay)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        response = self._client.post(url, **kwargs)
        response.raise_for_status()
        return response

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.get(url, **kwargs).text

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 60.0)
    return min(2.0**attempt + random.uniform(0, 0.5), 30.0)


Collector = Callable[[Settings, HttpClient], list[Item]]


def safe_collect(
    name: str, collector: Collector, settings: Settings, client: HttpClient
) -> list[Item]:
    """Run one collector. Any exception is logged and yields [] so one broken
    source never kills the run."""
    started = time.monotonic()
    try:
        items = collector(settings, client)
    except Exception:  # noqa: BLE001 - deliberate: isolate every source
        log.exception("collector %s failed", name)
        return []
    log.info("collector %s: %d items in %.1fs", name, len(items), time.monotonic() - started)
    return items


def client_from_settings(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> HttpClient:
    return HttpClient(
        user_agent=settings.run.user_agent,
        timeout=settings.run.http_timeout_seconds,
        transport=transport,
    )


_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", value or "")).split())


def parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO-8601 strings (with Z or offset). Returns UTC-aware datetime or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def struct_time_to_datetime(value: time.struct_time | None) -> datetime | None:
    """feedparser gives UTC struct_time for *_parsed fields."""
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
