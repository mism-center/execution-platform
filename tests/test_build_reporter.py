"""Tests for the discovery build-callback payload and HttpBuildReporter's
delivery contract (2xx accepted, 4xx dropped, 5xx/network retried)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, Field

from core.settings import Settings
from dependencies import _create_build_reporter
from services.build_reporter import (
    BuildResult,
    HttpBuildReporter,
    LoggingBuildReporter,
    build_callback_payload,
)

URL = "http://discovery-gateway:8000/api/internal/builds"

VERDICT = {
    "model_id": "mism:model/m1", "job_id": "job-x", "status": "failed",
    "ladder_reached": "L2", "image_ref": "docker.io/mismplatform/envbuild@sha256:8e05",
    "image_digest": "sha256:8e05", "reason": "StochasticSystem missing from arrow",
    "failure_class": "ABI_MISMATCH", "routes_to": "retry", "lockfile_sha256": "sha256:lock",
    "entrypoint_used": "python chemotaxis/composites/chemotaxis_flagella.py",
    "run": {"run_id": "98d9da1c66"},
}


def _result(**overrides) -> BuildResult:
    fields = dict(
        resource_id="m1",
        run_id="98d9da1c66",
        job_name="envbuild-98d9da1c66-build-m1",
        job_status="succeeded",
        exit_code=None,
        verdict=VERDICT,
        attempts=[
            {"attempt": 3, "ladder_reached": "L2", "failure_class": "MISSING_DEPENDENCY",
             "action_taken": {"type": "ADD_PKG", "arg": "arrow", "why": "..."},
             "duration_s": 120.4, "error_raw": "x" * 5000},
            {"attempt": 5, "ladder_reached": "L2", "failure_class": "ABI_MISMATCH",
             "action_taken": None, "duration_s": 99.0},
        ],
        spec={
            "install_mode": "mounted", "env_vars": {"MPLBACKEND": "Agg"},
            "entrypoint": ["python", "declared.py"],
            "mount": {"code_path": "/model", "workdir": "/model",
                      "input_path": "/inputs", "output_path": "/outputs"},
        },
        dockerfile="FROM python:3.11-slim\n",
        lockfile="numpy==1.26.4\n",
        annotation_patch=None,
    )
    fields.update(overrides)
    return BuildResult(**fields)


class TestPayload:
    def test_matches_agreed_shape(self) -> None:
        p = build_callback_payload(_result())
        assert set(p) == {
            "resource_id", "run_id", "job", "outcome", "image", "ladder_reached", "reason",
            "failure_class", "routes_to", "runtime", "artifacts", "attempts", "verdict",
        }
        assert p["job"] == {
            "name": "envbuild-98d9da1c66-build-m1", "status": "succeeded", "exit_code": None,
        }
        assert p["outcome"] == "failed"
        assert p["image"] == {
            "ref": "docker.io/mismplatform/envbuild@sha256:8e05", "digest": "sha256:8e05",
        }
        assert (p["ladder_reached"], p["failure_class"], p["routes_to"]) == (
            "L2", "ABI_MISMATCH", "retry",
        )
        assert p["artifacts"] == {
            "dockerfile": "FROM python:3.11-slim\n", "lockfile": "numpy==1.26.4\n",
            "lockfile_sha256": "sha256:lock", "annotation_patch": None,
        }
        assert p["verdict"] == VERDICT

    def test_runtime_uses_the_entrypoint_l3_actually_ran(self) -> None:
        runtime = build_callback_payload(_result())["runtime"]
        assert runtime["entrypoint"] == ["python", "chemotaxis/composites/chemotaxis_flagella.py"]
        assert runtime["install_mode"] == "mounted"
        assert runtime["mount"]["output_path"] == "/outputs"
        assert runtime["env_vars"] == {"MPLBACKEND": "Agg"}

    def test_runtime_falls_back_to_spec_entrypoint(self) -> None:
        verdict = {k: v for k, v in VERDICT.items() if k != "entrypoint_used"}
        runtime = build_callback_payload(_result(verdict=verdict))["runtime"]
        assert runtime["entrypoint"] == ["python", "declared.py"]

    def test_attempts_are_summarised_without_raw_errors(self) -> None:
        attempts = build_callback_payload(_result())["attempts"]
        assert attempts == [
            {"attempt": 3, "ladder_reached": "L2", "failure_class": "MISSING_DEPENDENCY",
             "action": {"type": "ADD_PKG", "arg": "arrow"}, "duration_s": 120.4},
            {"attempt": 5, "ladder_reached": "L2", "failure_class": "ABI_MISMATCH",
             "action": None, "duration_s": 99.0},
        ]

    @pytest.mark.parametrize(
        ("job_status", "outcome"), [("missing", "job_missing"), ("failed", "no_verdict")]
    )
    def test_no_verdict_payload(self, job_status: str, outcome: str) -> None:
        p = build_callback_payload(_result(
            job_status=job_status, verdict=None, attempts=[], spec=None,
            dockerfile=None, lockfile=None,
        ))
        assert p["outcome"] == outcome
        assert p["image"] is None
        assert p["runtime"] == {
            "entrypoint": [], "install_mode": None, "mount": None, "env_vars": {},
        }
        assert p["verdict"] == {}
        assert p["ladder_reached"] == ""
        assert p["reason"]

    def test_unknown_run_id_sent_as_empty_string(self) -> None:
        assert build_callback_payload(_result(run_id=None))["run_id"] == ""


# Copy of discovery's request model (model-discovery, branch api-add-build-callback,
# api/src/mismapi/schemas/registry.py). Keep in sync: if discovery changes it,
# this is the test that should fail.
class _BuildImage(BaseModel):
    ref: str = ""
    digest: str = ""


class _BuildMount(BaseModel):
    output_path: str = ""


class _BuildRuntime(BaseModel):
    entrypoint: list[str] = Field(default_factory=list)
    mount: _BuildMount | None = None


class _BuildResultCallback(BaseModel):
    resource_id: str
    outcome: str
    run_id: str = ""
    ladder_reached: str = ""
    failure_class: str | None = None
    reason: str = ""
    image: _BuildImage | None = None
    runtime: _BuildRuntime | None = None
    verdict: dict[str, Any] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class TestDiscoveryContract:
    """Every payload variant must validate against discovery's model — a 422
    there is a 4xx here, which the reporter does not retry."""

    @pytest.mark.parametrize("overrides", [
        {},  # failed at L2 with an image
        {"verdict": {**VERDICT, "status": "verified", "ladder_reached": "L3",
                     "failure_class": None}},
        {"verdict": {**VERDICT, "ladder_reached": "L0", "image_ref": None,
                     "image_digest": None}},  # failed before anything was pushed
        {"job_status": "failed", "verdict": None, "attempts": [], "spec": None},
        {"job_status": "missing", "run_id": None, "verdict": None, "attempts": [],
         "spec": None, "dockerfile": None, "lockfile": None},
    ])
    def test_validates(self, overrides: dict) -> None:
        payload = build_callback_payload(_result(**overrides))
        parsed = _BuildResultCallback.model_validate(payload)
        assert parsed.resource_id == "m1"
        assert parsed.outcome == payload["outcome"]

    def test_verified_build_meets_discoverys_success_rule(self) -> None:
        verdict = {**VERDICT, "status": "verified", "ladder_reached": "L3", "failure_class": None}
        parsed = _BuildResultCallback.model_validate(
            build_callback_payload(_result(verdict=verdict))
        )
        # Mirrors BuildResultCallback.succeeded on the discovery side.
        assert parsed.outcome == "verified" and parsed.ladder_reached == "L3"
        assert not parsed.failure_class and parsed.image and parsed.image.ref
        assert parsed.verdict["entrypoint_used"] == VERDICT["entrypoint_used"]


class TestHttpBuildReporter:
    @staticmethod
    def _respond(monkeypatch: pytest.MonkeyPatch, status: int) -> dict:
        sent: dict = {}

        async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            sent["url"] = url
            sent["json"] = kwargs["json"]
            return httpx.Response(status, text="body", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return sent

    async def test_posts_payload_to_callback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._respond(monkeypatch, 200)
        await HttpBuildReporter(URL).report(_result())
        assert sent["url"] == URL
        assert sent["json"] == build_callback_payload(_result())

    async def test_5xx_raises_so_the_poller_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._respond(monkeypatch, 503)
        with pytest.raises(httpx.HTTPStatusError):
            await HttpBuildReporter(URL).report(_result())

    async def test_4xx_is_logged_and_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._respond(monkeypatch, 422)
        with caplog.at_level(logging.ERROR):
            await HttpBuildReporter(URL).report(_result())  # does not raise
        assert any("not retrying" in r.message for r in caplog.records)

    async def test_409_replay_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._respond(monkeypatch, 409)
        with caplog.at_level(logging.INFO):
            await HttpBuildReporter(URL).report(_result())  # does not raise
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    async def test_connection_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(self: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx.AsyncClient, "post", boom)
        with pytest.raises(httpx.ConnectError):
            await HttpBuildReporter(URL).report(_result())


class TestReporterSelection:
    def test_http_when_callback_url_set(self) -> None:
        settings = Settings(database_url=None, envbuild_callback_url=URL)
        assert isinstance(_create_build_reporter(settings), HttpBuildReporter)

    def test_logging_when_unset(self) -> None:
        settings = Settings(database_url=None, envbuild_callback_url=None)
        assert isinstance(_create_build_reporter(settings), LoggingBuildReporter)
