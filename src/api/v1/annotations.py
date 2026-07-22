"""Annotation endpoints — trigger model annotation jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from core.errors import OrchestrationError, ValidationError
from dependencies import get_annotation_service
from schemas.annotations import AnnotateRequest, AnnotateResponse
from services.annotation_service import AnnotationService

router = APIRouter(prefix="/annotations", tags=["annotations"])


@router.post("", response_model=AnnotateResponse, status_code=200)
async def annotate(
    body: AnnotateRequest,
    service: Annotated[AnnotationService, Depends(get_annotation_service)],
) -> AnnotateResponse:
    """Kick off a biomodel-annotator job for a model resource.

    The resource must be in DRAFT or ANNOTATION_FAILED status.
    Transitions the resource to ANNOTATING and returns the appstore job SID.
    """
    try:
        return await service.annotate(body)
    except ValueError as e:
        raise ValidationError(detail=str(e)) from e
    except RuntimeError as e:
        raise OrchestrationError(detail=str(e)) from e
