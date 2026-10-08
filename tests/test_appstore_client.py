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


class TestLaunchJobPayload:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> dict:
        captured: dict = {}

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            captured["json"] = kwargs["json"]
            request = httpx.Request("POST", url)
            return httpx.Response(
                201, json={"sid": "s", "name": "n", "status": "running"}, request=request
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return captured

    async def test_optional_pod_fields_omitted_by_default(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture(monkeypatch)
        await client.launch_job(name="mism-x", identifier="abc", image="img")
        for key in (
            "service_account", "env_from", "secret_mounts",
            "ttl_seconds_after_finished", "security_context",
        ):
            assert key not in captured["json"]

    async def test_optional_pod_fields_sent_when_set(
        self, client: AppstoreClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = self._capture(monkeypatch)
        await client.launch_job(
            name="envbuild-x",
            identifier="abc",
            image="img",
            service_account="envbuild",
            env_from=[{"kind": "secret", "name": "envbuild-llm"}],
            secret_mounts=[{"secret": "s", "mount_path": "/s", "items": {}}],
            ttl_seconds_after_finished=0,
            security_context={"allow_privilege_escalation": False},
        )
        body = captured["json"]
        assert body["service_account"] == "envbuild"
        assert body["env_from"] == [{"kind": "secret", "name": "envbuild-llm"}]
        assert body["secret_mounts"][0]["secret"] == "s"
        assert body["ttl_seconds_after_finished"] == 0
        assert body["security_context"] == {"allow_privilege_escalation": False}
