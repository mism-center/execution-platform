"""Tests for the /api/v1/runs endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient
from mism_registry import RunStatus

from services.dal_service import DALService
from tests.conftest import create_test_run


class TestCreateRun:
    def test_create_run_success(self, client: TestClient, dal: DALService) -> None:
        run_id = create_test_run(dal)
        resp = client.post("/api/v1/runs", json={"run_id": run_id})
        assert resp.status_code == 201
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["sid"]
        assert body["status"] == RunStatus.RUNNING.value
        assert "Location" in resp.headers

    def test_create_run_not_found(self, client: TestClient) -> None:
        resp = client.post("/api/v1/runs", json={"run_id": "nonexistent"})
        assert resp.status_code == 400
        assert "not found" in resp.json()["error"]["detail"].lower()

    def test_create_run_missing_run_id(self, client: TestClient) -> None:
        resp = client.post("/api/v1/runs", json={})
        assert resp.status_code == 422

    def test_create_run_model_no_execution_ref(
        self, client: TestClient, dal: DALService
    ) -> None:
        """Model without execution_ref should fail with 400."""
        model = dal.register_model(
            name="no-image-model",
            location_uri="irods:///mism/models/no-image",
            execution_ref="",
        )
        run = dal.create_run(model_id=model.id)
        resp = client.post("/api/v1/runs", json={"run_id": run.id})
        assert resp.status_code == 400
        assert "execution_ref" in resp.json()["error"]["detail"]


class TestGetRun:
    def test_get_run(self, client: TestClient, dal: DALService) -> None:
        run_id = create_test_run(dal)
        client.post("/api/v1/runs", json={"run_id": run_id})

        resp = client.get(f"/api/v1/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["status"] in [s.value for s in RunStatus]

    def test_get_run_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/runs/nonexistent")
        assert resp.status_code == 404


class TestListRuns:
    def test_list_empty(self, client: TestClient) -> None:
        resp = client.get("/api/v1/runs")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []

    def test_list_after_create(self, client: TestClient, dal: DALService) -> None:
        run_id = create_test_run(dal)
        client.post("/api/v1/runs", json={"run_id": run_id})
        resp = client.get("/api/v1/runs")
        assert resp.status_code == 200
        assert len(resp.json()["runs"]) >= 1


class TestDeleteRun:
    def test_delete_run(self, client: TestClient, dal: DALService) -> None:
        run_id = create_test_run(dal)
        client.post("/api/v1/runs", json={"run_id": run_id})

        resp = client.delete(f"/api/v1/runs/{run_id}")
        assert resp.status_code == 204

    def test_delete_run_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/runs/nonexistent")
        assert resp.status_code == 404


