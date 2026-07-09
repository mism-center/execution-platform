"""Annotation service — orchestrates biomodel-annotator job lifecycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import partial

from mism_registry import ResourceRegistrationStatus

from core.settings import Settings
from schemas.annotations import AnnotateRequest, AnnotateResponse
from services.appstore_client import AppstoreClient
from services.dal_service import DALService

logger = logging.getLogger(__name__)


class AnnotationService:
    """Encapsulates annotation job lifecycle."""

    def __init__(
        self,
        dal: DALService,
        appstore: AppstoreClient,
        settings: Settings,
    ) -> None:
        self._dal = dal
        self._appstore = appstore
        self._settings = settings

    @staticmethod
    async def _in_executor(fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    async def annotate(self, request: AnnotateRequest) -> AnnotateResponse:
        """Kick off an annotation job for a model resource.

        Validates the resource exists and is in a state that allows annotation,
        launches the annotator pod via appstore, stores the job SID, and
        transitions the resource to ANNOTATING.
        """
        resource = await self._in_executor(self._dal.get_resource, request.resource_id)
        if resource is None:
            raise ValueError(f"Resource {request.resource_id} not found")

        allowed = {ResourceRegistrationStatus.DRAFT, ResourceRegistrationStatus.ANNOTATION_FAILED}
        if resource.registration_status not in allowed:
            raise ValueError(
                f"Resource {request.resource_id} cannot be annotated from "
                f"status={resource.registration_status.value}"
            )

        pvc_mounts = [{
            "pvc": self._settings.irods_pvc_name,
            "mount_path": "/workspace",
            "sub_path": resource.location_uri.strip("/"),
            "read_only": False,
        }]

        env = {
            "MODEL_INPUT": "/workspace",
            "PROMPT": request.prompt,
            "ANTHROPIC_API_KEY": request.anthropic_api_key,
        }

        try:
            result = await self._appstore.launch_job(
                name=f"mism-annotate-{request.resource_id[:8]}".lower(),
                identifier=uuid.uuid4().hex,
                image=request.image,
                cpus=request.cpus,
                memory=request.memory,
                env=env,
                pvc_mounts=pvc_mounts,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to launch annotation job: {e}") from e

        try:
            await self._in_executor(
                self._dal.update_resource_metadata,
                request.resource_id,
                {"annotation_job_sid": result.sid},
            )
            await self._in_executor(
                self._dal.set_resource_registration_status,
                request.resource_id,
                ResourceRegistrationStatus.ANNOTATING,
            )
        except Exception as e:
            logger.warning(
                f"Annotation job launched (sid={result.sid}) but failed to update "
                f"resource {request.resource_id} state",
                exc_info=True,
            )
            raise RuntimeError(f"Job launched but failed to update resource state: {e}") from e

        return AnnotateResponse(
            resource_id=request.resource_id,
            sid=result.sid,
            registration_status=ResourceRegistrationStatus.ANNOTATING,
        )

    async def poll_annotating_resources(self) -> None:
        """Poll all ANNOTATING resources and transition them on job completion."""
        resources = await self._in_executor(
            self._dal.list_resources_by_registration_status,
            ResourceRegistrationStatus.ANNOTATING,
        )
        for resource in resources:
            sid = resource.metadata.get("annotation_job_sid")
            if not sid:
                logger.warning(
                    f"Resource {resource.id} is ANNOTATING but has no annotation_job_sid"
                )
                continue
            try:
                await self._sync_annotation_status(resource.id, sid)
            except Exception:
                logger.warning(
                    f"Poller: failed to sync annotation for resource {resource.id}",
                    exc_info=True,
                )

    async def _sync_annotation_status(self, resource_id: str, sid: str) -> None:
        """Check appstore job status and transition resource registration status."""
        job_status = await self._appstore.job_status(sid)
        if job_status is None:
            return

        if job_status.status == "succeeded":
            await self._in_executor(
                self._dal.set_resource_registration_status,
                resource_id,
                ResourceRegistrationStatus.PENDING_REVIEW,
            )
            await self._in_executor(
                self._dal.update_resource_metadata,
                resource_id,
                {"annotation_job_sid": None},
            )
            logger.info(f"Resource {resource_id} annotation succeeded → pending_review")

        elif job_status.status == "failed":
            await self._in_executor(
                self._dal.set_resource_registration_status,
                resource_id,
                ResourceRegistrationStatus.ANNOTATION_FAILED,
            )
            await self._in_executor(
                self._dal.update_resource_metadata,
                resource_id,
                {"annotation_job_sid": None},
            )
            logger.info(f"Resource {resource_id} annotation failed → annotation_failed")
