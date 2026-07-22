"""Tests for the service layer (RunService)."""

from __future__ import annotations

import pytest
from mism_registry import RunStatus

from services.dal_service import DALService
from services.run_service import RunService
from tests.conftest import create_test_run


class TestRunService:
    async def test_create_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        result = await run_service.create_run(run_id)
        assert result.run_id == run_id
        assert result.sid
        assert result.status == RunStatus.RUNNING

    async def test_create_run_not_found(self, run_service: RunService) -> None:
        with pytest.raises(ValueError, match="not found"):
            await run_service.create_run("nonexistent")

    async def test_get_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        await run_service.create_run(run_id)
        fetched = await run_service.get_run(run_id)
        assert fetched is not None
        assert fetched.run_id == run_id

    async def test_get_run_not_found(self, run_service: RunService) -> None:
        assert await run_service.get_run("nonexistent") is None

    async def test_get_run_shows_error_after_failure(
        self, run_service: RunService, dal: DALService
    ) -> None:
        run_id = create_test_run(dal)
        await run_service.create_run(run_id)
        dal.mark_failed(run_id, "OOM killed")
        fetched = await run_service.get_run(run_id)
        assert fetched is not None
        assert fetched.status == RunStatus.FAILED
        assert fetched.error == "OOM killed"

    async def test_delete_run(self, run_service: RunService, dal: DALService) -> None:
        run_id = create_test_run(dal)
        await run_service.create_run(run_id)
        assert await run_service.delete_run(run_id) is True
        assert await run_service.delete_run("nonexistent") is False

    async def test_uses_model_compute(
        self, run_service: RunService, dal: DALService, mock_appstore
    ) -> None:
        """Compute on the Resource maps to launch_job's cpus/memory args."""
        run_id = create_test_run(dal)
        await run_service.create_run(run_id)
        launch_call = mock_appstore.launch_job.await_args
        # create_test_run stamps Compute(cpu_cores=2, memory_gb=4.0) on the model
        assert launch_call.kwargs["cpus"] == "2"
        assert launch_call.kwargs["memory"] == "4.0Gi"


# from services.appstore_client import JobStatus
# class TestPollBatchRuns:
#     async def test_poll_completes_succeeded_run(
#         self, run_service: RunService, dal: DALService, mock_appstore
#     ) -> None:
#         run_id = create_test_run(dal)
#         await run_service.create_run(run_id)
#         mock_appstore.job_status.return_value = JobStatus(
#             sid="fake-sid", name="fake-job", status="succeeded", phase="Succeeded"
#         )
#         await run_service.poll_batch_runs()
#         run = dal.get_run(run_id)
#         assert RunStatus(run.status.value) == RunStatus.COMPLETED
#
#     async def test_poll_fails_failed_run(
#         self, run_service: RunService, dal: DALService, mock_appstore
#     ) -> None:
#         run_id = create_test_run(dal)
#         await run_service.create_run(run_id)
#         mock_appstore.job_status.return_value = JobStatus(
#             sid="fake-sid", name="fake-job", status="failed", phase="Failed"
#         )
#         await run_service.poll_batch_runs()
#         run = dal.get_run(run_id)
#         assert RunStatus(run.status.value) == RunStatus.FAILED
#
#     async def test_poll_skips_non_batch_runs(
#         self, run_service: RunService, dal: DALService, mock_appstore
#     ) -> None:
#         run_id = create_test_run(dal)
#         await run_service.create_interactive(run_id)
#         mock_appstore.job_status.return_value = JobStatus(
#             sid="fake-sid", name="fake-job", status="succeeded", phase="Succeeded"
#         )
#         await run_service.poll_batch_runs()
#         run = dal.get_run(run_id)
#         # Interactive run should not be touched by the batch poller
#         assert RunStatus(run.status.value) == RunStatus.RUNNING
#
#     async def test_poll_handles_appstore_error_gracefully(
#         self, run_service: RunService, dal: DALService, mock_appstore
#     ) -> None:
#         from unittest.mock import AsyncMock
#         run_id = create_test_run(dal)
#         await run_service.create_run(run_id)
#         mock_appstore.job_status = AsyncMock(side_effect=Exception("appstore down"))
#         # Should not raise — errors are swallowed per-run
#         await run_service.poll_batch_runs()
#         run = dal.get_run(run_id)
#         assert RunStatus(run.status.value) == RunStatus.RUNNING
