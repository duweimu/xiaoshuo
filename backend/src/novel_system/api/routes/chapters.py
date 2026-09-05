from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import (
    BoundedJsonObject,
    EmptyRequest,
    WriterBriefJsonInput,
)
from novel_system.api.response import ok
from novel_system.db.models import ChapterGoal, ChapterState, SceneCard
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.chapter_approval import (
    is_chapter_approved,
    require_chapter_mutation_allowed,
)
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError
from novel_system.services.text_validation import validate_user_text_payload
from novel_system.services.writer_briefs import normalize_chapter_writer_brief

router = APIRouter(tags=["chapters"])
INT64_MAX = (1 << 63) - 1


class ChapterUpsertRequest(BaseModel):
    """Whitelist the chapter fields an author is allowed to edit."""

    model_config = ConfigDict(extra="forbid", strict=True)

    chapter_id: str = Field(min_length=1, max_length=255)
    chapter_goal: str = Field(max_length=100_000)
    project_id: str | None = Field(default=None, max_length=255)
    outline_plan_id: str | None = Field(default=None, max_length=255)
    planned_scene_count: int | None = Field(default=None, ge=0, le=INT64_MAX)
    mid_aggregate_enabled: int = Field(default=0, ge=0, le=1)
    narrative_json: BoundedJsonObject | None = None
    state: str = Field(default="planned", min_length=1, max_length=64)
    words_target: int | None = Field(default=None, ge=0, le=INT64_MAX)
    display_order: int | None = Field(default=None, ge=0, le=INT64_MAX)
    main_plot_push: str | None = Field(default=None, max_length=100_000)
    emotional_target: str | None = Field(default=None, max_length=100_000)
    ending_effect: str | None = Field(default=None, max_length=100_000)
    must_not: str | None = Field(default=None, max_length=100_000)
    notes: str | None = Field(default=None, max_length=100_000)
    # Shape validation belongs to normalize_chapter_writer_brief() so chapter
    # and scene endpoints share WRITER_BRIEF_INVALID / HTTP 400 semantics.
    writer_brief_json: WriterBriefJsonInput = None


BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=255)]


class ChapterIdsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chapter_ids: list[BoundedIdentifier] = Field(max_length=10_000)


class ChapterSceneOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scene_ids: list[BoundedIdentifier] = Field(max_length=10_000)
    last_scene_id: BoundedIdentifier


@router.get("/api/v1/chapters")
def list_chapters(request: Request, session: Session = Depends(get_session)):
    return ok(
        {"items": AuthorLifecycleService(session).list_active_chapters()},
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v1/chapters")
def create_chapter(
    payload: ChapterUpsertRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(exclude_unset=True)
    # Reject/normalize the domain payload before claiming an idempotency key.
    # Invalid requests must never poison a key that the author can correct and
    # retry, while semantically equivalent briefs should hash identically.
    body["writer_brief_json"] = normalize_chapter_writer_brief(
        body.get("writer_brief_json")
    )
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters",
        payload=body,
        action=lambda: _create_chapter(session, body),
    )


@router.post("/api/v1/chapters/trash")
def trash_chapters(payload: ChapterIdsRequest, request: Request, session: Session = Depends(get_session)):
    body = payload.model_dump(mode="json")
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/trash",
        payload=body,
        action=lambda: AuthorLifecycleService(session).trash_chapters(body["chapter_ids"], actor_ref),
    )


def _create_chapter(session: Session, payload: dict) -> dict:
    validate_user_text_payload(payload, field_prefix="chapter")
    payload = {
        **payload,
        "writer_brief_json": normalize_chapter_writer_brief(payload.get("writer_brief_json")),
    }
    chapter = session.get(ChapterGoal, payload["chapter_id"])
    created = chapter is None
    _assert_chapter_display_order_available(
        session,
        chapter_id=payload["chapter_id"],
        project_id=(payload.get("project_id") if chapter is None else chapter.project_id),
        display_order=(
            payload.get("display_order")
            if "display_order" in payload
            else (chapter.display_order if chapter is not None else None)
        ),
    )
    if chapter is None:
        if str(payload.get("state") or "").strip() == "approved":
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        chapter = ChapterGoal(**payload)
        session.add(chapter)
        session.flush()
        changed = True
    else:
        if chapter.trashed_flag == 1:
            raise DomainError("CHAPTER_TRASHED", "chapter is currently in author trash")
        if (
            str(payload.get("state") or "").strip() == "approved"
            and not is_chapter_approved(session, chapter)
        ):
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        if "project_id" in payload and payload["project_id"] != chapter.project_id:
            raise DomainError(
                "CHAPTER_IDENTITY_IMMUTABLE",
                "an existing chapter cannot be moved to another project",
                status_code=409,
            )
        if "outline_plan_id" in payload and payload["outline_plan_id"] != chapter.outline_plan_id:
            raise DomainError(
                "CHAPTER_IDENTITY_IMMUTABLE",
                "an existing chapter cannot be rebound to another outline plan",
                status_code=409,
            )
        changed_fields = [
            key
            for key, value in payload.items()
            if key != "chapter_id" and getattr(chapter, key) != value
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="chapters.upsert",
        )
        if changed:
            for key, value in payload.items():
                setattr(chapter, key, value)

    state = session.get(ChapterState, payload["chapter_id"])
    # Replaying the same payload against a locked final is a true no-op: do not
    # opportunistically create runtime rows or touch update timestamps.
    should_create_state = state is None and (
        created or not is_chapter_approved(session, chapter)
    )
    if should_create_state:
        state = ChapterState(
            chapter_id=payload["chapter_id"],
            current_phase="drafting",
            mid_aggregate_enabled_effective=0,
            aggregate_block_reason="none",
        )
        session.add(state)
        changed = True
    session.flush()
    return {"chapter_id": chapter.chapter_id, "changed": changed}


def _assert_chapter_display_order_available(
    session: Session,
    *,
    chapter_id: str,
    project_id: str | None,
    display_order: int | None,
) -> None:
    if project_id is None or display_order is None:
        return
    conflict = session.execute(
        select(ChapterGoal.chapter_id).where(
            ChapterGoal.project_id == project_id,
            ChapterGoal.display_order == int(display_order),
            ChapterGoal.trashed_flag == 0,
            ChapterGoal.chapter_id != chapter_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise DomainError(
            "CHAPTER_DISPLAY_ORDER_CONFLICT",
            "another active chapter already uses this display_order",
            status_code=409,
            details={
                "project_id": project_id,
                "display_order": int(display_order),
                "conflicting_chapter_id": conflict,
            },
        )


@router.post("/api/v1/chapters/{chapter_id}/run/full")
def run_chapter_full(
    chapter_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    AuthorLifecycleService(session).require_active_chapter(chapter_id)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/run/full",
        payload={"chapter_id": chapter_id},
        action=lambda lease: ChapterRunnerService(session).run_full(chapter_id, request_lease=lease),
    )


@router.get("/api/v1/chapters/{chapter_id}/run-status")
def chapter_run_status(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    AuthorLifecycleService(session).require_active_chapter(chapter_id)
    payload = ChapterRunnerService(session).run_status(chapter_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/chapters/{chapter_id}/scene-order")
def reorder_chapter_scenes(
    chapter_id: str,
    payload: ChapterSceneOrderRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/scene-order",
        payload={"chapter_id": chapter_id, **body},
        action=lambda: _reorder_chapter_scenes(session, chapter_id, body),
    )


def _reorder_chapter_scenes(session: Session, chapter_id: str, payload: dict) -> dict:
    chapter = AuthorLifecycleService(session).require_active_chapter(chapter_id)

    scene_ids = payload.get("scene_ids")
    if not isinstance(scene_ids, list) or not scene_ids or not all(isinstance(scene_id, str) and scene_id for scene_id in scene_ids):
        raise DomainError("SCENE_ORDER_INVALID", "scene_ids must be a non-empty list", status_code=400)
    if len(scene_ids) != len(set(scene_ids)):
        raise DomainError("SCENE_ORDER_DUPLICATE", "scene_ids must not contain duplicates", status_code=400)

    last_scene_id = payload.get("last_scene_id")
    if not isinstance(last_scene_id, str) or last_scene_id not in scene_ids:
        raise DomainError("SCENE_ORDER_LAST_SCENE_INVALID", "last_scene_id must be present in scene_ids", status_code=400)

    chapter_scenes = session.execute(
        select(SceneCard).where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
    ).scalars().all()
    chapter_scene_map = {scene.scene_id: scene for scene in chapter_scenes}
    other_chapter_scenes = {
        scene.scene_id
        for scene in session.execute(select(SceneCard).where(SceneCard.scene_id.in_(scene_ids), SceneCard.trashed_flag == 0)).scalars().all()
        if scene.chapter_id != chapter_id
    }
    if other_chapter_scenes:
        raise DomainError("SCENE_ORDER_CHAPTER_MISMATCH", "scene_ids must belong to the same chapter")

    if set(scene_ids) != set(chapter_scene_map):
        raise DomainError("SCENE_ORDER_INCOMPLETE", "scene_ids must include every scene in the chapter", status_code=409)

    ordered_scenes = [chapter_scene_map[scene_id] for scene_id in scene_ids]
    changed_fields = [
        f"scene:{scene.scene_id}.order"
        for index, scene in enumerate(ordered_scenes, start=1)
        if scene.scene_seq != index
        or scene.is_chapter_last != (1 if scene.scene_id == last_scene_id else 0)
    ]
    changed = require_chapter_mutation_allowed(
        session,
        chapter,
        changed_fields=changed_fields,
        operation="chapters.reorder_scenes",
    )
    if changed:
        temporary_start = max(
            int(scene.scene_seq or 0) for scene in ordered_scenes
        ) + 1
        for offset, scene in enumerate(ordered_scenes):
            scene.scene_seq = temporary_start + offset
        session.flush()
        for index, scene in enumerate(ordered_scenes, start=1):
            scene.scene_seq = index
            scene.is_chapter_last = 1 if scene.scene_id == last_scene_id else 0
        session.flush()
    return {
        "chapter_id": chapter_id,
        "changed": changed,
        "scenes": [
            {"scene_id": scene.scene_id, "scene_seq": scene.scene_seq, "is_chapter_last": scene.is_chapter_last}
            for scene in ordered_scenes
        ],
    }
