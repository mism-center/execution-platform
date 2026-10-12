"""Tests for BuildService and /api/v1/builds — the envagent builder launch."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mism_registry import EnvBuildStatus, ResourceRegistrationStatus

from core.settings import Settings
from dependencies import get_build_service
from main import create_app
from schemas.builds import BuildRequest
from services.appstore_client import AppstoreClient, JobResult, JobStatus
from services.build_reporter import BuildResult
from services.build_service import BuildInProgressError, BuildService
from services.dal_service import DALService
from tests.conftest import approve_resource

MODEL_REL = "mism/models/m1"
RUN_ID = "0123456789"


class RecordingReporter:
    def __init__(self) -> None:
        self.results: list[BuildResult] = []

    async def report(self, result: BuildResult) -> None:
        self.results.append(result)


@pytest.fixture
def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=None,
        irods_mount_path=str(tmp_path),
        irods_pvc_name="irods-pvc",
        envbuild_work_mount_path=str(tmp_path / "envbuild-work"),
    )


@pytest.fixture
def reporter() -> RecordingReporter:
    return RecordingReporter()


@pytest.fixture
def build_service(
    dal: DALService,
    mock_appstore: AppstoreClient,
    build_settings: Settings,
    reporter: RecordingReporter,
) -> BuildService:
    # Default: no previous build Job for the model.
    mock_appstore.job_status = AsyncMock(return_value=None)
    return BuildService(
        dal=dal, appstore=mock_appstore, settings=build_settings, reporter=reporter
    )


def _job(
    model_id: str, status: str, run_id: str = RUN_ID, exit_code: int | None = None
) -> JobStatus:
    return JobStatus(
        sid=f"build-{model_id}", name=f"envbuild-{run_id}-build-{model_id}",
        status=status, phase=status.capitalize(), exit_code=exit_code,
    )


def _model(
    dal: DALService,
    root: Path,
    *,
    approved: bool = True,
    annotated: bool = True,
    on_disk: bool = True,
) -> str:
    model_dir = root / MODEL_REL
    if on_disk:
        model_dir.mkdir(parents=True)
        if annotated:
            (model_dir / "metadata-package").mkdir()
            (model_dir / "metadata-package" / "execution.yaml").write_text("execution: {}\n")
    model = dal.register_model(name="m1", location_uri=f"irods:///{MODEL_REL}")
    if approved:
        # Approval puts env_build_status at READY_FOR_BUILD, as discovery would.
        approve_resource(dal, model.id, runnable=False)
    return model.id


def _env_status(dal: DALService, model_id: str) -> EnvBuildStatus:
    return dal.get_resource(model_id).env_build_status


def _launch_kwargs(mock_appstore: AppstoreClient) -> dict:
    mock_appstore.launch_job.assert_awaited_once()
    return mock_appstore.launch_job.call_args.kwargs


class TestLaunch:
    async def test_job_matches_envagent_template(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)

        resp = await build_service.launch(BuildRequest(resource_id=model_id))

        kw = _launch_kwargs(mock_appstore)
        # Prefixed so the mism-guid label never matches the annotation Job's.
        assert kw["identifier"] == f"build-{model_id}"
        # run_id rides in the Job name so the poller can find this build's verdict.
        assert re.fullmatch(r"[0-9a-f]{10}", resp.run_id)
        assert kw["name"] == f"envbuild-{resp.run_id}"
        assert len(f"{kw['name']}-{kw['identifier']}") <= 63
        assert resp.resource_id == model_id
        assert _env_status(dal, model_id) == EnvBuildStatus.BUILDING
        mock_appstore.job_status.assert_awaited_once_with(f"build-{model_id}")
        mock_appstore.delete_job.assert_not_awaited()
        assert kw["image"] == "mismplatform/pi-envagent:k8s-test"
        assert resp.model_repo == f"/models/{MODEL_REL}"

        env = kw["env"]
        assert env["MODEL_ID"] == f"mism:model/{model_id}"
        assert env["MODEL_REPO"] == f"/models/{MODEL_REL}"
        assert env["ANNOTATION"] == f"/models/{MODEL_REL}/metadata-package"
        assert env["ENVBUILD_RUN_ID"] == resp.run_id
        assert env["ENVBUILD_MODELS_PVC"] == "irods-pvc"
        assert env["ENVBUILD_WORK_PVC"] == "envbuild-work"
        assert "PROMPT" not in env
        # appstore's env serializer rejects blank values.
        assert all(v for v in env.values())

        mounts = {m["mount_path"]: m for m in kw["pvc_mounts"]}
        assert mounts["/models"] == {
            "pvc": "irods-pvc", "mount_path": "/models", "read_only": True,
        }
        # appstore's PvcMountSerializer rejects an explicit blank sub_path.
        assert all(m.get("sub_path", "x") != "" for m in kw["pvc_mounts"])
        assert mounts["/work"]["pvc"] == "envbuild-work"
        assert mounts["/work"]["read_only"] is False

        assert kw["service_account"] == "envbuild"
        assert [e["kind"] for e in kw["env_from"]] == ["configmap", "secret"]
        assert kw["env_from"][0]["optional"] is True
        assert kw["env_from"][1]["name"] == "envbuild-llm"
        assert kw["secret_mounts"] == [{
            "secret": "envbuild-registry-auth",
            "mount_path": "/home/node/.docker",
            "items": {".dockerconfigjson": "config.json"},
        }]
        assert kw["ttl_seconds_after_finished"] == 86400
        assert kw["security_context"] == {
            "allow_privilege_escalation": False, "drop_capabilities": ["ALL"],
        }

    async def test_prompt_and_annotation_subpath_passed_through(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path, annotated=False)
        alt = tmp_path / MODEL_REL / "metadata-package-na1"
        alt.mkdir()
        (alt / "execution.yaml").write_text("execution: {}\n")

        await build_service.launch(BuildRequest(
            resource_id=model_id,
            annotation_subpath="metadata-package-na1/",
            prompt="prefer python 3.10",
        ))

        env = _launch_kwargs(mock_appstore)["env"]
        assert env["ANNOTATION"] == f"/models/{MODEL_REL}/metadata-package-na1"
        assert env["PROMPT"] == "prefer python 3.10"

    async def test_unknown_resource(self, build_service: BuildService) -> None:
        with pytest.raises(LookupError):
            await build_service.launch(BuildRequest(resource_id="nope"))

    @pytest.mark.parametrize("bad_id", ["a,executor=x", "UPPER", "x" * 38, "-lead"])
    async def test_unsafe_resource_id_refused_before_lookup(
        self, build_service: BuildService, mock_appstore: AppstoreClient, bad_id: str
    ) -> None:
        with pytest.raises(LookupError):
            await build_service.launch(BuildRequest(resource_id=bad_id))
        mock_appstore.job_status.assert_not_awaited()

    @pytest.mark.parametrize("active", ["running", "pending"])
    async def test_active_build_blocks_relaunch(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
        active: str,
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.job_status = AsyncMock(return_value=_job(model_id, active))
        with pytest.raises(BuildInProgressError):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.delete_job.assert_not_awaited()
        mock_appstore.launch_job.assert_not_awaited()

    @pytest.mark.parametrize("finished", ["succeeded", "failed"])
    async def test_finished_build_replaced(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
        finished: str,
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.job_status = AsyncMock(return_value=_job(model_id, finished))
        await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.delete_job.assert_awaited_once_with(f"build-{model_id}")
        mock_appstore.launch_job.assert_awaited_once()

    async def test_failed_cleanup_of_previous_build_is_runtime_error(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.job_status = AsyncMock(return_value=_job(model_id, "failed"))
        mock_appstore.delete_job = AsyncMock(side_effect=Exception("500"))
        with pytest.raises(RuntimeError, match="previous envbuild job"):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_not_awaited()

    async def test_requires_approval(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path, approved=False)
        dal.set_resource_registration_status(model_id, ResourceRegistrationStatus.ANNOTATING)
        dal.set_resource_registration_status(model_id, ResourceRegistrationStatus.PENDING_REVIEW)

        with pytest.raises(ValueError, match="approved"):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_not_awaited()

    async def test_requires_ready_for_build(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)
        dal.set_env_build_status(model_id, EnvBuildStatus.BUILDING)
        dal.set_env_build_status(model_id, EnvBuildStatus.RUNNABLE)

        with pytest.raises(ValueError, match="expected ready_for_build"):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_not_awaited()

    async def test_building_is_in_progress(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)
        dal.set_env_build_status(model_id, EnvBuildStatus.BUILDING)

        with pytest.raises(BuildInProgressError):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_not_awaited()

    async def test_failed_launch_leaves_ready_for_build(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.launch_job = AsyncMock(side_effect=Exception("boom"))
        with pytest.raises(RuntimeError):
            await build_service.launch(BuildRequest(resource_id=model_id))
        assert _env_status(dal, model_id) == EnvBuildStatus.READY_FOR_BUILD

    async def test_failed_building_transition_deletes_the_job(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        model_id = _model(dal, tmp_path)

        def boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(dal, "set_env_build_status", boom)
        with pytest.raises(RuntimeError, match="BUILDING failed"):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_awaited_once()
        mock_appstore.delete_job.assert_awaited_once_with(f"build-{model_id}")

    async def test_rejects_non_model(
        self, build_service: BuildService, dal: DALService
    ) -> None:
        dal.register_dataset(resource_id="ds-1", name="ds", location_uri="mism/datasets/ds")
        with pytest.raises(ValueError, match="not a model"):
            await build_service.launch(BuildRequest(resource_id="ds-1"))

    async def test_missing_model_dir(
        self, build_service: BuildService, dal: DALService, tmp_path: Path
    ) -> None:
        model_id = _model(dal, tmp_path, on_disk=False)
        with pytest.raises(ValueError, match="Model directory not found"):
            await build_service.launch(BuildRequest(resource_id=model_id))

    async def test_missing_annotation(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path, annotated=False)
        with pytest.raises(ValueError, match="Annotation not found"):
            await build_service.launch(BuildRequest(resource_id=model_id))
        mock_appstore.launch_job.assert_not_awaited()

    @pytest.mark.parametrize("subpath", ["../other", "/etc", "a/../..", "."])
    async def test_annotation_subpath_must_stay_inside_model_dir(
        self, build_service: BuildService, dal: DALService, tmp_path: Path, subpath: str
    ) -> None:
        model_id = _model(dal, tmp_path)
        with pytest.raises(ValueError, match="annotation_subpath"):
            await build_service.launch(
                BuildRequest(resource_id=model_id, annotation_subpath=subpath)
            )

    async def test_appstore_failure_is_runtime_error(
        self,
        build_service: BuildService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        tmp_path: Path,
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.launch_job = AsyncMock(side_effect=Exception("400 service_account"))
        with pytest.raises(RuntimeError, match="Failed to launch envbuild job"):
            await build_service.launch(BuildRequest(resource_id=model_id))


class TestGetStatus:
    MODEL_ID = "b2aaaaec-d6a5-4b33-bfa5-f0eb530c5265"

    async def test_returns_live_status(
        self, build_service: BuildService, mock_appstore: AppstoreClient
    ) -> None:
        mock_appstore.job_status = AsyncMock(
            return_value=_job(self.MODEL_ID, "succeeded", exit_code=0)
        )
        status = await build_service.get_status(self.MODEL_ID)
        assert status is not None
        assert status.resource_id == self.MODEL_ID
        assert status.status == "succeeded"
        assert status.exit_code == 0
        # Looks up the prefixed identifier, never the annotation Job's bare id.
        mock_appstore.job_status.assert_awaited_once_with(f"build-{self.MODEL_ID}")

    async def test_unsafe_id_skips_appstore(
        self, build_service: BuildService, mock_appstore: AppstoreClient
    ) -> None:
        assert await build_service.get_status("x,executor=mism-exec") is None
        mock_appstore.job_status.assert_not_awaited()

    async def test_no_build(self, build_service: BuildService) -> None:
        assert await build_service.get_status(self.MODEL_ID) is None


class TestBuildsApi:
    @pytest.fixture
    def api(self, build_service: BuildService) -> TestClient:
        app = create_app()
        app.dependency_overrides[get_build_service] = lambda: build_service
        return TestClient(app)

    def test_post_201(
        self, api: TestClient, dal: DALService, mock_appstore: AppstoreClient, tmp_path: Path
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.launch_job = AsyncMock(return_value=JobResult(
            sid="x", name="envbuild-job", status="running",
        ))
        resp = api.post("/api/v1/builds", json={"resource_id": model_id})
        assert resp.status_code == 201
        body = resp.json()
        assert body["resource_id"] == model_id
        assert body["run_id"]
        assert body["job_name"] == "envbuild-job"
        assert body["status"] == "running"

    def test_post_404(self, api: TestClient) -> None:
        resp = api.post("/api/v1/builds", json={"resource_id": "nope"})
        assert resp.status_code == 404

    def test_post_400_when_not_approved(
        self, api: TestClient, dal: DALService, tmp_path: Path
    ) -> None:
        model_id = _model(dal, tmp_path, approved=False)
        resp = api.post("/api/v1/builds", json={"resource_id": model_id})
        assert resp.status_code == 400

    def test_post_502_on_appstore_failure(
        self, api: TestClient, dal: DALService, mock_appstore: AppstoreClient, tmp_path: Path
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.launch_job = AsyncMock(side_effect=Exception("boom"))
        resp = api.post("/api/v1/builds", json={"resource_id": model_id})
        assert resp.status_code == 502

    def test_post_409_while_build_active(
        self, api: TestClient, dal: DALService, mock_appstore: AppstoreClient, tmp_path: Path
    ) -> None:
        model_id = _model(dal, tmp_path)
        mock_appstore.job_status = AsyncMock(return_value=_job(model_id, "running"))
        resp = api.post("/api/v1/builds", json={"resource_id": model_id})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "conflict"

    def test_get_200(self, api: TestClient, mock_appstore: AppstoreClient) -> None:
        model_id = "b2aaaaec-d6a5-4b33-bfa5-f0eb530c5265"
        mock_appstore.job_status = AsyncMock(return_value=_job(model_id, "running"))
        resp = api.get(f"/api/v1/builds/{model_id}")
        assert resp.status_code == 200
        assert resp.json()["job_name"] == f"envbuild-{RUN_ID}-build-{model_id}"

    def test_get_404_for_unknown(self, api: TestClient) -> None:
        resp = api.get("/api/v1/builds/b2aaaaec-d6a5-4b33-bfa5-f0eb530c5265")
        assert resp.status_code == 404


def _write_records(
    work: Path,
    model_id: str,
    *,
    run_id: str = RUN_ID,
    status: str = "verified",
    job_id: str = "job-2026-10-12-abcd",
    with_state: bool = True,
    annotation_patch: bool = False,
) -> None:
    """Lay out envbuild-work the way envagent leaves it after a verdict:
    two attempts, the second one producing the verdict's (best) image."""
    records = work / "records"
    job_dir = records / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for n in (1, 2):
        (job_dir / f"Dockerfile.a{n}").write_text(f"FROM python:3.11-slim # a{n}\n")
        (job_dir / f"lockfile.a{n}.txt").write_text(f"numpy==1.26.4 # a{n}\n")
    if with_state:
        (job_dir / "state.json").write_text(json.dumps({"best": {"spec": {
            "install_mode": "mounted",
            "env_vars": {"MPLBACKEND": "Agg"},
            "entrypoint": ["python", "declared.py"],
            "mount": {"code_path": "/model", "workdir": "/model",
                      "input_path": "/inputs", "output_path": "/outputs"},
        }}}))
    if annotation_patch:
        (job_dir / "annotation-patch.yaml").write_text("schema: envbuild-annotation-patch/1\n")
    verdict = {
        "model_id": f"mism:model/{model_id}", "job_id": job_id, "status": status,
        "image_ref": "docker.io/mismplatform/envbuild@sha256:bbb",
        "image_digest": "sha256:bbb", "reason": "ok", "run": {"run_id": run_id},
        "ladder_reached": "L3", "failure_class": None, "routes_to": "retry",
        "lockfile_sha256": "sha256:lock", "entrypoint_used": "python chemo/run.py --steps 5",
    }
    attempts = [
        {"model_id": f"mism:model/{model_id}", "job_id": job_id, "attempt": 1,
         "ladder_reached": "L0", "failure_class": "ABI_MISMATCH", "image_digest": "sha256:aaa",
         "action_taken": {"type": "PIN_PKG", "arg": "numpy<2", "why": "..."}, "duration_s": 60.0},
        {"model_id": f"mism:model/{model_id}", "job_id": job_id, "attempt": 2,
         "ladder_reached": "L3", "failure_class": None, "image_digest": "sha256:bbb",
         "action_taken": None, "duration_s": 90.5},
    ]
    with (records / "verdicts.jsonl").open("a") as f:
        f.write(json.dumps(verdict) + "\n")
    with (records / "attempts.jsonl").open("a") as f:
        for row in attempts:
            f.write(json.dumps(row) + "\n")


class TestPoller:
    @pytest.fixture
    def building(self, dal: DALService, tmp_path: Path) -> str:
        model_id = _model(dal, tmp_path)
        dal.set_env_build_status(model_id, EnvBuildStatus.BUILDING)
        return model_id

    async def test_verified_build_reported_with_outputs(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        dal: DALService,
        tmp_path: Path,
    ) -> None:
        work = tmp_path / "envbuild-work"
        # An older build of the same model, then this one.
        _write_records(work, building, run_id="aaaaaaaaaa", status="failed", job_id="job-old")
        _write_records(work, building)
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded", exit_code=0))

        await build_service.poll_building_resources()

        [result] = reporter.results
        assert result.resource_id == building
        assert result.run_id == RUN_ID
        assert result.outcome == "verified"
        assert result.image_ref == "docker.io/mismplatform/envbuild@sha256:bbb"
        assert [a["attempt"] for a in result.attempts] == [1, 2]
        # The best attempt is the one whose image the verdict names (a2).
        assert result.dockerfile == "FROM python:3.11-slim # a2\n"
        assert result.lockfile == "numpy==1.26.4 # a2\n"
        assert result.spec["mount"]["workdir"] == "/model"
        assert result.annotation_patch is None
        assert "records/jobs/job-2026-10-12-abcd/state.json" in result.files
        # ExecAPI doesn't make the terminal transition — the callback owns it.
        assert _env_status(dal, building) == EnvBuildStatus.BUILDING

    async def test_reported_once_per_job(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        tmp_path: Path,
    ) -> None:
        _write_records(tmp_path / "envbuild-work", building)
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded"))
        await build_service.poll_building_resources()
        await build_service.poll_building_resources()
        assert len(reporter.results) == 1

    @pytest.mark.parametrize("active", ["pending", "running"])
    async def test_active_job_not_reported(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        active: str,
    ) -> None:
        mock_appstore.job_status = AsyncMock(return_value=_job(building, active))
        await build_service.poll_building_resources()
        assert reporter.results == []

    async def test_stale_verdict_from_previous_build_not_used(
        self,
        build_service: BuildService,
        build_settings: Settings,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        tmp_path: Path,
    ) -> None:
        _write_records(tmp_path / "envbuild-work", building, run_id="aaaaaaaaaa")
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded"))

        # Within the grace window: wait for the verdict to appear.
        await build_service.poll_building_resources()
        assert reporter.results == []

        # Past it: report verdict-less rather than borrow the old build's verdict.
        build_settings.envbuild_verdict_grace_seconds = 0
        await build_service.poll_building_resources()
        [result] = reporter.results
        assert result.verdict is None
        assert result.outcome == "no_verdict"
        assert "without a verdict" in result.reason

    async def test_failed_agent_reported_without_waiting(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
    ) -> None:
        mock_appstore.job_status = AsyncMock(
            return_value=_job(building, "failed", exit_code=1)
        )
        await build_service.poll_building_resources()
        [result] = reporter.results
        assert result.job_status == "failed"
        assert result.exit_code == 1
        assert result.verdict is None

    async def test_missing_job_reported(
        self,
        build_service: BuildService,
        reporter: RecordingReporter,
        building: str,
    ) -> None:
        await build_service.poll_building_resources()
        [result] = reporter.results
        assert result.job_status == "missing"
        assert result.outcome == "job_missing"
        assert "disappeared" in result.reason

    async def test_only_building_models_polled(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        dal: DALService,
        tmp_path: Path,
    ) -> None:
        _model(dal, tmp_path)  # READY_FOR_BUILD, never launched
        await build_service.poll_building_resources()
        mock_appstore.job_status.assert_not_awaited()
        assert reporter.results == []

    async def test_one_bad_model_does_not_stop_the_rest(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
    ) -> None:
        mock_appstore.job_status = AsyncMock(side_effect=Exception("appstore down"))
        await build_service.poll_building_resources()  # must not raise
        assert reporter.results == []

    async def test_annotation_patch_and_missing_state_tolerated(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        tmp_path: Path,
    ) -> None:
        _write_records(
            tmp_path / "envbuild-work", building, with_state=False, annotation_patch=True
        )
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded"))
        await build_service.poll_building_resources()
        [result] = reporter.results
        assert result.spec is None
        assert result.annotation_patch.startswith("schema: envbuild-annotation-patch/1")

    async def test_no_second_report_when_ttl_removes_the_job(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        tmp_path: Path,
    ) -> None:
        _write_records(tmp_path / "envbuild-work", building)
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded"))
        await build_service.poll_building_resources()
        # Model still BUILDING (callback not applied yet); the Job's TTL expires.
        mock_appstore.job_status = AsyncMock(return_value=None)
        await build_service.poll_building_resources()
        assert [r.job_status for r in reporter.results] == ["succeeded"]

    async def test_failed_delivery_is_retried_next_poll(
        self,
        build_service: BuildService,
        mock_appstore: AppstoreClient,
        reporter: RecordingReporter,
        building: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_records(tmp_path / "envbuild-work", building)
        mock_appstore.job_status = AsyncMock(return_value=_job(building, "succeeded"))
        calls = {"n": 0}
        original = reporter.report

        async def flaky(result: BuildResult) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("discovery 503")
            await original(result)

        monkeypatch.setattr(reporter, "report", flaky)
        await build_service.poll_building_resources()  # delivery fails, swallowed by poller
        assert reporter.results == []
        await build_service.poll_building_resources()  # retried
        assert len(reporter.results) == 1
        await build_service.poll_building_resources()  # then not again
        assert calls["n"] == 2
