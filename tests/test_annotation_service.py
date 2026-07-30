"""Tests for AnnotationService — in particular that a failed appstore
``delete_job`` cleanup never blocks the resource state machine, and that
the failure is logged (not silently dropped)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from mism_registry import ResourceRegistrationStatus

from schemas.annotations import AnnotateRequest
from services.annotation_service import AnnotationService
from services.appstore_client import AppstoreClient, JobResult, JobStatus
from services.dal_service import DALService


def _make_delete_500_error() -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", "http://mism-appstore:8000/api/v1/jobs/fake-sid/")
    response = httpx.Response(500, text="boom", request=request)
    return httpx.HTTPStatusError("Server error '500'", request=request, response=response)


class TestSyncAnnotationStatus:
    async def test_succeeded_transition_survives_delete_job_failure(
        self,
        annotation_service: AnnotationService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        model = dal.register_model(
            name="race-model",
            location_uri="irods:///mism/models/race",
            execution_ref="docker.io/org/model:v1",
        )
        dal.set_resource_registration_status(model.id, ResourceRegistrationStatus.ANNOTATING)

        mock_appstore.job_status = AsyncMock(
            return_value=JobStatus(
                sid=model.id, name="fake-job", status="succeeded", phase="Succeeded"
            )
        )
        mock_appstore.delete_job = AsyncMock(side_effect=_make_delete_500_error())

        with caplog.at_level(logging.WARNING):
            await annotation_service._sync_annotation_status(model.id)

        resource = dal.get_resource(model.id)
        assert resource is not None
        assert resource.registration_status == ResourceRegistrationStatus.PENDING_REVIEW
        assert any("orphaned" in record.message for record in caplog.records)

    async def test_failed_transition_survives_delete_job_failure(
        self,
        annotation_service: AnnotationService,
        dal: DALService,
        mock_appstore: AppstoreClient,
    ) -> None:
        model = dal.register_model(
            name="race-model-2",
            location_uri="irods:///mism/models/race2",
            execution_ref="docker.io/org/model:v1",
        )
        dal.set_resource_registration_status(model.id, ResourceRegistrationStatus.ANNOTATING)

        mock_appstore.job_status = AsyncMock(
            return_value=JobStatus(sid=model.id, name="fake-job", status="failed", phase="Failed")
        )
        mock_appstore.delete_job = AsyncMock(side_effect=_make_delete_500_error())

        await annotation_service._sync_annotation_status(model.id)

        resource = dal.get_resource(model.id)
        assert resource is not None
        assert resource.registration_status == ResourceRegistrationStatus.ANNOTATION_FAILED


class TestReannotateDespiteStaleJob:
    async def test_relaunch_proceeds_even_if_previous_job_delete_fails(
        self,
        annotation_service: AnnotationService,
        dal: DALService,
        mock_appstore: AppstoreClient,
    ) -> None:
        model = dal.register_model(
            name="retry-model",
            location_uri="irods:///mism/models/retry",
            execution_ref="docker.io/org/model:v1",
        )
        dal.set_resource_registration_status(model.id, ResourceRegistrationStatus.ANNOTATING)
        dal.set_resource_registration_status(model.id, ResourceRegistrationStatus.PENDING_REVIEW)

        mock_appstore.delete_job = AsyncMock(side_effect=_make_delete_500_error())
        mock_appstore.launch_job = AsyncMock(
            return_value=JobResult(sid=model.id, name="fake-job", status="running")
        )

        response = await annotation_service.annotate(
            AnnotateRequest(
                resource_id=model.id, image="annotator:latest", prompt="describe this model"
            )
        )

        assert response.registration_status == ResourceRegistrationStatus.ANNOTATING
        mock_appstore.launch_job.assert_awaited_once()
