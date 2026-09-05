from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.project_requests import ProjectCreateRequest
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.services.projects import ProjectChapterFlowService, ProjectService, start_project_chapter_run_job_worker

router = APIRouter(tags=["projects"])


class ProjectChapterRunJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    offline_demo: StrictBool = False


class ProjectChapterReadConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    note: str | None = Field(default=None, max_length=1000)


class ProjectChapterApproveFinalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision_notes: str | None = Field(default=None, max_length=2000)


class ProjectChapterReopenFinalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("reason must not be blank")
        return reason


@router.post("/api/v1/projects")
def create_project(payload: ProjectCreateRequest, request: Request, session: Session = Depends(get_session)):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects",
        payload=body,
        action=lambda: ProjectService(session).create(body),
    )


@router.get("/api/v1/projects")
def list_projects(request: Request, session: Session = Depends(get_session)):
    return ok(
        ProjectService(session).list(),
        req_id=getattr(request.state, "request_id", None),
    )


@router.get("/api/v1/projects/{project_id}/dashboard")
def project_dashboard(project_id: str, request: Request, session: Session = Depends(get_session)):
    return ok(ProjectService(session).dashboard(project_id), req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/projects/{project_id}/outline-plan/{plan_id}/approve")
def approve_outline_plan(
    project_id: str,
    plan_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/outline-plan/{plan_id}/approve",
        payload={"project_id": project_id, "plan_id": plan_id, **body},
        action=lambda: ProjectService(session).approve_outline_plan(project_id, plan_id),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/run")
def run_project_chapter(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/chapters/{chapter_id}/run",
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).run_chapter(project_id, chapter_id),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job")
def run_project_chapter_job(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterRunJobRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {"offline_demo": False}
    job_to_start: str | None = None

    def prepare() -> dict[str, Any]:
        nonlocal job_to_start
        result = ProjectChapterFlowService(session).prepare_chapter_run_job(
            project_id,
            chapter_id,
            offline_demo=bool(body["offline_demo"]),
        )
        if bool(result.pop("_start_worker", False)):
            job_to_start = result["run"]["job_id"]
        return result

    response = optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=prepare,
    )
    # The closure is populated only when this request executed the action. A
    # durable replay returns the cached response without launching another worker.
    if job_to_start is not None:
        start_project_chapter_run_job_worker(project_id, chapter_id, job_to_start)
    return response


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final")
def approve_project_chapter_final(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterApproveFinalRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/chapters/{chapter_id}/approve-final",
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).approve_final(project_id, chapter_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/reopen-final")
def reopen_project_chapter_final(
    project_id: str,
    chapter_id: str,
    payload: ProjectChapterReopenFinalRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/chapters/{chapter_id}/reopen-final",
        payload={"project_id": project_id, "chapter_id": chapter_id, **body},
        action=lambda: ProjectChapterFlowService(session).reopen_final(
            project_id,
            chapter_id,
            reason=body["reason"],
            actor_ref=actor_ref,
        ),
    )


@router.post("/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm")
def confirm_project_chapter_read(
    project_id: str,
    chapter_id: str,
    request: Request,
    payload: ProjectChapterReadConfirmRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/chapters/{chapter_id}/read-confirm",
        payload={"project_id": project_id, "chapter_id": chapter_id, "body": body},
        action=lambda: ProjectChapterFlowService(session).confirm_read(
            project_id, chapter_id, body, actor_ref=actor_ref
        ),
    )


