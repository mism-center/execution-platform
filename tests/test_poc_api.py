"""Tests for the /api/v1/poc/vivarium endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from schemas.enums import VivariumStatus


class TestExecuteVivarium:
    """Headless notebook execution — main PoC goal."""

    def test_execute_success(self, client: TestClient) -> None:
        resp = client.post("/api/v1/poc/vivarium/execute", json={})
        assert resp.status_code == 201
        body = resp.json()
        assert body["sid"]
        assert body["status"] == VivariumStatus.STARTING.value
        assert body["output_path"]
        assert "Location" in resp.headers

    def test_execute_custom_resources(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/poc/vivarium/execute",
            json={"cpus": "4", "memory": "8Gi"},
        )
        assert resp.status_code == 201

    def test_get_execution(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/poc/vivarium/execute", json={})
        sid = create_resp.json()["sid"]

        resp = client.get(f"/api/v1/poc/vivarium/execute/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sid"] == sid
        assert body["status"] in [s.value for s in VivariumStatus]
        assert body["output_path"]

    def test_get_execution_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/poc/vivarium/execute/nonexistent")
        assert resp.status_code == 404

    def test_delete_execution(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/poc/vivarium/execute", json={})
        sid = create_resp.json()["sid"]

        resp = client.delete(f"/api/v1/poc/vivarium/{sid}")
        assert resp.status_code == 204

        resp = client.get(f"/api/v1/poc/vivarium/execute/{sid}")
        assert resp.status_code == 404


class TestCreateVivarium:
    """Interactive Jupyter UI — bonus."""

    def test_create_success(self, client: TestClient) -> None:
        resp = client.post("/api/v1/poc/vivarium", json={})
        assert resp.status_code == 201
        body = resp.json()
        assert body["sid"]
        assert body["status"] == VivariumStatus.STARTING.value
        assert "token=" in body["url"]
        assert body["jupyter_token"]
        assert "Location" in resp.headers

    def test_create_custom_resources(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/poc/vivarium",
            json={"cpus": "4", "memory": "8Gi"},
        )
        assert resp.status_code == 201


class TestGetVivarium:
    def test_get_instance(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/poc/vivarium", json={})
        sid = create_resp.json()["sid"]

        resp = client.get(f"/api/v1/poc/vivarium/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sid"] == sid
        assert body["is_ready"] is True
        assert body["status"] == VivariumStatus.READY.value
        assert "token=" in body["url"]

    def test_get_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/v1/poc/vivarium/nonexistent")
        assert resp.status_code == 404


class TestDeleteVivarium:
    def test_delete_instance(self, client: TestClient) -> None:
        create_resp = client.post("/api/v1/poc/vivarium", json={})
        sid = create_resp.json()["sid"]

        resp = client.delete(f"/api/v1/poc/vivarium/{sid}")
        assert resp.status_code == 204

        resp = client.get(f"/api/v1/poc/vivarium/{sid}")
        assert resp.status_code == 404

    def test_delete_not_found(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/poc/vivarium/nonexistent")
        assert resp.status_code == 404
