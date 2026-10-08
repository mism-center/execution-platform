"""Tests for BuildService and /api/v1/builds — the envagent builder launch."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mism_registry import ResourceRegistrationStatus

from core.settings import Settings
from dependencies import get_build_service
from main import create_app
from schemas.builds import BuildRequest
from services.appstore_client import AppstoreClient, JobResult, JobStatus
from services.build_service import BuildInProgressError, BuildService
from services.dal_service import DALService
from tests.conftest import approve_resource

MODEL_REL = "mism/models/m1"


@pytest.fixture
def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=None,
        irods_mount_path=str(tmp_path),
        irods_pvc_name="irods-pvc",
    )


@pytest.fixture
def build_service(
    dal: DALService, mock_appstore: AppstoreClient, build_settings: Settings
) -> BuildService:
    # Default: no previous build Job for the model.
    mock_appstore.job_status = AsyncMock(return_value=None)
    return BuildService(dal=dal, appstore=mock_appstore, settings=build_settings)


def _job(model_id: str, status: str) -> JobStatus:
    return JobStatus(
        sid=f"build-{model_id}", name=f"envbuild-build-{model_id}",
        status=status, phase=status.capitalize(),
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
        approve_resource(dal, model.id)
    return model.id


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
        assert kw["name"] == "envbuild"
        assert len(f"{kw['name']}-{kw['identifier']}") <= 63
        assert resp.resource_id == model_id
        assert re.fullmatch(r"[0-9a-f]{32}", resp.run_id)
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
            "pvc": "irods-pvc", "mount_path": "/models", "sub_path": "", "read_only": True,
        }
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

    @pytest.mark.parametrize("bad_id", ["a,executor=x", "UPPER", "x" * 49, "-lead"])
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
        mock_appstore.job_status = AsyncMock(return_value=JobStatus(
            sid=f"build-{self.MODEL_ID}", name=f"envbuild-build-{self.MODEL_ID}",
            status="succeeded", phase="Succeeded", exit_code=0,
        ))
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
        assert resp.json()["job_name"] == f"envbuild-build-{model_id}"

    def test_get_404_for_unknown(self, api: TestClient) -> None:
        resp = api.get("/api/v1/builds/b2aaaaec-d6a5-4b33-bfa5-f0eb530c5265")
        assert resp.status_code == 404
