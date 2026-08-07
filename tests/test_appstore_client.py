"""Tests for AppstoreClient.delete_job — bounded retry on 5xx and that the
appstore response body surfaces in the final error instead of being lost."""

from __future__ import annotations

import httpx
import pytest

from core.settings import Settings
from services.appstore_client import AppstoreClient


def _response(status_code: int, text: str) -> httpx.Response:
    request = httpx.Request("DELETE", "http://mism-appstore:8000/api/v1/jobs/fake-sid/")
    return httpx.Response(status_code, text=text, request=request)


@pytest.fixture
def client() -> AppstoreClient:
    settings = Settings(
        database_url=None,
        appstore_delete_retry_max_attempts=3,
        appstore_delete_retry_backoff_seconds=0.0,
    )
    return AppstoreClient(settings)


class TestDeleteJobRetry:
    async def test_retries_on_500_then_succeeds(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        responses = [
            _response(500, "boom 1"),
            _response(500, "boom 2"),
            _response(200, "{}"),
        ]
        calls = {"count": 0}

        async def fake_delete(self: httpx.AsyncClient, url: str, **_: object) -> httpx.Response:
            resp = responses[calls["count"]]
            calls["count"] += 1
            return resp

        monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

        await client.delete_job("fake-sid")

        assert calls["count"] == 3

    async def test_gives_up_after_max_attempts_with_body_in_error(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"count": 0}

        async def fake_delete(self: httpx.AsyncClient, url: str, **_: object) -> httpx.Response:
            calls["count"] += 1
            return _response(500, "appstore internal error: job still terminating")

        monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.delete_job("fake-sid")

        assert calls["count"] == 3
        assert "job still terminating" in str(exc_info.value)

    async def test_404_returns_immediately_without_retry(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"count": 0}

        async def fake_delete(self: httpx.AsyncClient, url: str, **_: object) -> httpx.Response:
            calls["count"] += 1
            return _response(404, "not found")

        monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

        await client.delete_job("fake-sid")

        assert calls["count"] == 1

    async def test_4xx_error_raises_immediately_without_retry(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = {"count": 0}

        async def fake_delete(self: httpx.AsyncClient, url: str, **_: object) -> httpx.Response:
            calls["count"] += 1
            return _response(400, "bad request")

        monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

        with pytest.raises(httpx.HTTPStatusError):
            await client.delete_job("fake-sid")

        assert calls["count"] == 1
