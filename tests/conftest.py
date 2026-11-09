"""Shared test setup. Tests must never hit the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any test tries a real HTTP request through httpx."""

    def _blocked(self, request, *args, **kwargs):  # noqa: ARG001
        raise RuntimeError(f"Network access blocked in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str):
    return json.loads(load_fixture(name))


def mock_transport(responses: dict[str, httpx.Response | str | dict]) -> httpx.MockTransport:
    """Build a MockTransport that matches on URL substring.

    Values may be an httpx.Response, a str body (200 text), or a dict (200 JSON).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, value in responses.items():
            if needle in url:
                if isinstance(value, httpx.Response):
                    return value
                if isinstance(value, dict | list):
                    return httpx.Response(200, json=value)
                return httpx.Response(200, text=value)
        return httpx.Response(404, text=f"no mock for {url}")

    return httpx.MockTransport(handler)
