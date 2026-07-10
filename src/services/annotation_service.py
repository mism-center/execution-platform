"""Annotation service — orchestrates biomodel-annotator job lifecycle."""

from __future__ import annotations

import asyncio
import logging
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

        The resource_id is used as the appstore job identifier so the poller
        can check status without storing a separate SID. On retry
        (ANNOTATION_FAILED), the previous failed job is deleted first to avoid
        K8s name conflicts.
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

        # On retry, the previous failed K8s Job still exists and would cause a
        # 409 conflict. Delete it first — ignore 404 if already cleaned up.
        if resource.registration_status == ResourceRegistrationStatus.ANNOTATION_FAILED:
            try:
                await self._appstore.delete_job(request.resource_id)
                logger.info(
                    f"Deleted previous failed annotation job for resource {request.resource_id}"
                )
            except Exception:
                logger.warning(
                    f"Could not delete previous annotation job for resource {request.resource_id} "
                    "— may already be gone",
                    exc_info=True,
                )

        pvc_mounts = [{
            "pvc": self._settings.irods_pvc_name,
            "mount_path": "/workspace",
            "sub_path": resource.location_uri.strip("/"),
            "read_only": False,
        }]

        env: dict[str, str] = {
            "MODEL_INPUT": "/workspace",
            "PROMPT": request.prompt,
            "LLM_API_KEY": request.api_key,
        }
        if request.base_url is not None:
            env["LLM_BASE_URL"] = request.base_url

        try:
            result = await self._appstore.launch_job(
                name=f"annotate-{request.resource_id[:8]}".lower(),
                identifier=request.resource_id,
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
                self._dal.set_resource_registration_status,
                request.resource_id,
                ResourceRegistrationStatus.ANNOTATING,
            )
        except Exception as e:
            logger.warning(
                f"Annotation job launched (sid={result.sid}) but failed to transition "
                f"resource {request.resource_id} to ANNOTATING",
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
            try:
                await self._sync_annotation_status(resource.id)
            except Exception:
                logger.warning(
                    f"Poller: failed to sync annotation for resource {resource.id}",
                    exc_info=True,
                )

    async def _sync_annotation_status(self, resource_id: str) -> None:
        """Check appstore job status and transition resource registration status.

        The job SID equals the resource_id — no metadata lookup needed.
        Deletes the K8s Job after every terminal transition to prevent stale
        jobs from blocking future retries.
        """
        job_status = await self._appstore.job_status(resource_id)
        if job_status is None:
            return

        if job_status.status == "succeeded":
            await self._in_executor(
                self._dal.set_resource_registration_status,
                resource_id,
                ResourceRegistrationStatus.PENDING_REVIEW,
            )
            logger.info(f"Resource {resource_id} annotation succeeded → pending_review")

        elif job_status.status == "failed":
            await self._in_executor(
                self._dal.set_resource_registration_status,
                resource_id,
                ResourceRegistrationStatus.ANNOTATION_FAILED,
            )
            logger.info(f"Resource {resource_id} annotation failed → annotation_failed")

        else:
            return

        # Clean up the finished K8s Job so retries aren't blocked by stale objects.
        try:
            await self._appstore.delete_job(resource_id)
        except Exception:
            logger.warning(
                f"Failed to delete annotation job for resource {resource_id} "
                "after terminal transition",
                exc_info=True,
            )
