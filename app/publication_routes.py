"""Private authenticated endpoints used by the Django publication workflow."""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from app.dependencies import get_publication_service
from app.models import PublicationDescriptorRequest, PublicationStatusResponse
from app.publication_service import (
    PublicationDescriptor,
    PublicationError,
    PublicationService,
    validate_descriptor_targets,
    verify_request_signature,
)

router = APIRouter(prefix="/internal/publications", tags=["publication"])


def _descriptor(model: PublicationDescriptorRequest) -> PublicationDescriptor:
    values = model.model_dump(mode="json")
    return PublicationDescriptor(**values)


def _status_response(request: Request, state: dict, *, idempotent: bool = False) -> PublicationStatusResponse:
    status_url = str(
        request.url_for(
            "publication_status",
            publication_id=state["publication_id"],
            publication_version=state["publication_version"],
        )
    )
    return PublicationStatusResponse(
        publication_id=state["publication_id"],
        publication_version=state["publication_version"],
        status=state["status"],
        status_url=status_url,
        idempotent=idempotent,
        collection_name=state["collection_name"],
        previous_collection_name=state["previous_collection_name"],
        record_count=state["record_count"],
        point_count=state["point_count"],
        error_code=state["error_code"],
        message=state["message"],
    )


async def _authenticated_body(request: Request, service: PublicationService) -> bytes:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        raise PublicationError("PUBLICATION_CONTENT_TYPE", "Content-Type must be application/json", 415)
    body = await request.body()
    if not body or len(body) > service.max_request_bytes:
        raise PublicationError("PUBLICATION_BODY_INVALID", "Publication request body is missing or too large", 413)
    verify_request_signature(request.headers, body, service.store)
    return body


def _authenticate_empty_request(request: Request, service: PublicationService) -> None:
    verify_request_signature(request.headers, b"", service.store)


@router.post("", response_model=PublicationStatusResponse, status_code=202)
async def dispatch_publication(
    request: Request,
    background_tasks: BackgroundTasks,
    service: PublicationService = Depends(get_publication_service),
) -> PublicationStatusResponse:
    """Durably queue a v2 snapshot build after authenticating its exact bytes."""
    body = await _authenticated_body(request, service)
    try:
        model = PublicationDescriptorRequest.model_validate_json(body)
    except ValueError as exc:
        raise PublicationError("PUBLICATION_DESCRIPTOR_INVALID", "Publication descriptor is invalid", 422) from exc
    descriptor = _descriptor(model)
    validate_descriptor_targets(descriptor)
    state, idempotent = service.store.enqueue(descriptor)
    if not idempotent:
        background_tasks.add_task(service.report, descriptor, state)
        background_tasks.add_task(service.run, descriptor)
    return _status_response(request, state, idempotent=idempotent)


@router.get(
    "/{publication_id}/{publication_version}", response_model=PublicationStatusResponse, name="publication_status"
)
async def publication_status(
    publication_id: UUID,
    publication_version: str,
    request: Request,
    service: PublicationService = Depends(get_publication_service),
) -> PublicationStatusResponse:
    """Return durable state for an authenticated publisher poll."""
    _authenticate_empty_request(request, service)
    state = service.store.get(str(publication_id), publication_version)
    return _status_response(request, state)


@router.post("/{publication_id}/{publication_version}/rollback", response_model=PublicationStatusResponse)
async def rollback_publication(
    publication_id: UUID,
    publication_version: str,
    request: Request,
    service: PublicationService = Depends(get_publication_service),
) -> PublicationStatusResponse:
    """Move the serving alias back to this publication's retained prior collection."""
    _authenticate_empty_request(request, service)
    state = service.store.get(str(publication_id), publication_version)
    descriptor = PublicationDescriptor(**json.loads(state["descriptor_json"]))
    state = service.rollback(descriptor)
    return _status_response(request, state)
