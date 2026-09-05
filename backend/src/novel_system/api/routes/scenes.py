from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import EmptyRequest, WriterBriefJsonInput
from novel_system.api.response import ok
from novel_system.db.models import (
    AttemptTracker,
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RevisionCandidate,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.archiver import Archiver
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.author_state import compute_author_state
from novel_system.services.chapter_state import chapter_state_snapshot
from novel_system.services.chapter_approval import (
    is_chapter_approved,
    require_chapter_mutation_allowed,
)
from novel_system.services.canonical_manuscripts import CanonicalSceneService
from novel_system.services.errors import DomainError
from novel_system.services.literary_quality import LiteraryQualityService
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.near_final import (
    NEAR_FINAL_REWRITE_TYPE,
    NEAR_FINAL_RUBRIC_ID,
)
from novel_system.services.pagination import paginate_select, resolve_pagination_request
from novel_system.services.projects import ProjectService
from novel_system.services.reference_safety import ReferenceSafetyService
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_notes import SceneNotesService
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService
from novel_system.services.scene_run_jobs import (
    SceneRunJobService,
    remember_committed_cancellation,
    start_scene_run_job_worker,
)
from novel_system.services.scene_run_preflight import SceneRunPreflightService
from novel_system.services.source_safety import source_profile_ids_from_snapshot
from novel_system.services.text_validation import clean_backfill_markers, validate_user_text_payload
from novel_system.services.writer_briefs import normalize_scene_writer_brief
from novel_system.services.writer_review import WriterReviewService

router = APIRouter(tags=["scenes"])
_LOGGER = logging.getLogger(__name__)
INT64_MAX = (1 << 63) - 1


class SceneUpsertRequest(BaseModel):
    """Whitelist author-editable scene-card fields.

    Run state, trash state, word rollups, and timestamps remain server-owned.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    scene_id: str = Field(min_length=1, max_length=255)
    chapter_id: str = Field(min_length=1, max_length=255)
    scene_goal: str = Field(max_length=100_000)
    project_id: str | None = Field(default=None, max_length=255)
    outline_plan_id: str | None = Field(default=None, max_length=255)
    scene_seq: int | None = Field(default=None, ge=1, le=INT64_MAX)
    pov_character_id: str | None = Field(default=None, max_length=255)
    onstage_chars_json: list[Annotated[str, Field(min_length=1, max_length=255)]] = (
        Field(default_factory=list, max_length=256)
    )
    resolved_relation_id: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=10_000)
    beats_json: list[Annotated[str, Field(max_length=20_000)]] = Field(
        default_factory=list, max_length=256
    )
    must_include_text: str | None = Field(default=None, max_length=100_000)
    forbidden_text: str | None = Field(default=None, max_length=100_000)
    exit_change: str | None = Field(default=None, max_length=20_000)
    hook: str | None = Field(default=None, max_length=20_000)
    # Keep the established domain-error contract for malformed writer briefs:
    # normalize_scene_writer_brief() validates the JSON shape and returns the
    # stable WRITER_BRIEF_INVALID / HTTP 400 response used by API clients.
    writer_brief_json: WriterBriefJsonInput = None
    target_length_band: str | None = Field(default=None, max_length=64)
    scene_type: str | None = Field(default=None, max_length=64)
    is_chapter_last: int = Field(default=0, ge=0, le=1)
    state: str = Field(default="todo", min_length=1, max_length=64)
    constraint_intensity: float | None = Field(default=None, ge=0.0, le=1.0)


class ExactAuthorDraftAdoptionRequest(BaseModel):
    """One exact browser manuscript revision to save and publish atomically."""

    model_config = ConfigDict(extra="forbid", strict=True)

    draft_id: str = Field(min_length=1, max_length=255)
    base_revision_no: int = Field(ge=1, le=INT64_MAX)
    # Required but nullable: null is the CAS value when no canonical scene exists.
    expected_current_final_scene_row_id: str | None = Field(max_length=255)
    content: str = Field(max_length=2_000_000)


class AdoptCurrentRequest(BaseModel):
    """Only exact server-issued content-safety finding codes may be acknowledged."""

    model_config = ConfigDict(extra="forbid", strict=True)

    accepted_warning_codes: list[
        Annotated[str, Field(min_length=1, max_length=128)]
    ] = Field(default_factory=list, max_length=64)
    exact_author_draft: ExactAuthorDraftAdoptionRequest | None = None


BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=255)]


class SceneIdsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scene_ids: list[BoundedIdentifier] = Field(max_length=10_000)


class SceneRunCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # These values retain their domain validators and stable error codes.
    author_note: Any | None = None
    run_policy: Any | None = None
    from_step: Any | None = None
    resume: Any | None = None


class SceneRunJobRequest(SceneRunCommandRequest):
    resume_budget: bool | None = None


class SceneAutoRewriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: str | None = Field(default=None, max_length=64)


class SceneRunCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: Any | None = None


class StyleCandidateSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    no_clear_difference: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=INT64_MAX)
    preference_tags: list[
        Literal[
            "style_match",
            "rhythm",
            "voice",
            "imagery",
            "dialogue",
            "overall_quality",
            "plot_fidelity",
        ]
    ] = Field(default_factory=list, max_length=7)


class StyleCandidateReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str | None = Field(default=None, max_length=300)


class SceneBudgetTopupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # The endpoint deliberately reports all invalid dimensions together through
    # INVALID_BUDGET_TOPUP, so retain raw scalar types for that domain check.
    extra_tokens: Any = 0
    extra_attempts: Any = 0
    extra_provider_attempts: Any = 0
    reason: str | None = Field(default=None, max_length=300)


class SceneAuthorNotesSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    notes: str = Field(max_length=100_000)
    base_revision_no: int = Field(ge=0, le=INT64_MAX)


@router.get("/api/v1/scenes/{scene_id}/author-notes")
def get_scene_author_notes(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    result = SceneNotesService(session).get(scene_id)
    return ok(result, req_id=getattr(request.state, "request_id", None))


@router.patch("/api/v1/scenes/{scene_id}/author-notes")
def save_scene_author_notes(
    scene_id: str,
    payload: SceneAuthorNotesSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/scenes/{scene_id}/author-notes",
        payload={"scene_id": scene_id, "body": body},
        action=lambda: SceneNotesService(session).save(
            scene_id,
            body["notes"],
            base_revision_no=body["base_revision_no"],
        ),
    )


@router.post("/api/v1/scenes/trash")
def trash_scenes(
    payload: SceneIdsRequest, request: Request, session: Session = Depends(get_session)
):
    body = payload.model_dump(mode="json")
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/trash",
        payload=body,
        action=lambda: AuthorLifecycleService(session).trash_scenes(
            body["scene_ids"], actor_ref
        ),
    )


@router.post("/api/v1/scenes")
def create_scene(
    payload: SceneUpsertRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(exclude_unset=True)
    # Domain validation precedes the durable idempotency claim.  A malformed
    # brief therefore cannot reserve a key needed by the corrected request.
    body["writer_brief_json"] = normalize_scene_writer_brief(
        body.get("writer_brief_json")
    )
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes",
        payload=body,
        action=lambda: _create_scene(session, body),
    )


def _create_scene(session: Session, payload: dict) -> dict:
    validate_user_text_payload(payload, field_prefix="scene")
    payload = {
        **payload,
        "writer_brief_json": normalize_scene_writer_brief(
            payload.get("writer_brief_json")
        ),
    }
    lifecycle = AuthorLifecycleService(session)
    chapter_id = payload.get("chapter_id")
    if not isinstance(chapter_id, str) or not chapter_id:
        raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)

    chapter = lifecycle.require_active_chapter(chapter_id)

    scene = session.get(SceneCard, payload["scene_id"])
    created = scene is None
    effective_scene_seq = (
        payload.get("scene_seq")
        if payload.get("scene_seq") is not None
        else (
            scene.scene_seq
            if scene is not None
            else _next_scene_seq(session, chapter_id)
        )
    )
    _assert_scene_seq_available(
        session,
        scene_id=payload["scene_id"],
        chapter_id=chapter_id,
        scene_seq=int(effective_scene_seq),
    )
    if scene is None:
        require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=["scenes.create"],
            operation="scenes.upsert_create",
        )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": _next_scene_seq(session, chapter_id),
            }
        scene = SceneCard(**payload)
        session.add(scene)
        session.flush()
        changed = True
    else:
        if scene.trashed_flag == 1:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash")
        if payload["chapter_id"] != scene.chapter_id:
            raise DomainError(
                "SCENE_IDENTITY_IMMUTABLE",
                "an existing scene cannot be moved to another chapter",
                status_code=409,
            )
        if "project_id" in payload:
            requested_project_id = payload["project_id"]
            may_bind_from_chapter = (
                scene.project_id is None
                and requested_project_id is not None
                and requested_project_id == chapter.project_id
            )
            if requested_project_id != scene.project_id and not may_bind_from_chapter:
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be moved to another project",
                    status_code=409,
                )
        if "outline_plan_id" in payload:
            requested_outline_id = payload["outline_plan_id"]
            may_bind_from_chapter = (
                scene.outline_plan_id is None
                and requested_outline_id is not None
                and requested_outline_id == chapter.outline_plan_id
            )
            if (
                requested_outline_id != scene.outline_plan_id
                and not may_bind_from_chapter
            ):
                raise DomainError(
                    "SCENE_IDENTITY_IMMUTABLE",
                    "an existing scene cannot be rebound to another outline plan",
                    status_code=409,
                )
        if payload.get("scene_seq") is None:
            payload = {
                **payload,
                "scene_seq": scene.scene_seq,
            }
        changed_fields = [
            key
            for key, value in payload.items()
            if key not in {"scene_id", "chapter_id"} and getattr(scene, key) != value
        ]
        changed = require_chapter_mutation_allowed(
            session,
            chapter,
            changed_fields=changed_fields,
            operation="scenes.upsert_update",
        )
        if changed:
            for key, value in payload.items():
                setattr(scene, key, value)

    state = session.get(SceneRunState, payload["scene_id"])
    should_create_state = state is None and (
        created or not is_chapter_approved(session, chapter)
    )
    if should_create_state:
        state = SceneRunState(scene_id=payload["scene_id"], scene_status="ready")
        session.add(state)
        changed = True
    session.flush()
    return {"scene_id": scene.scene_id, "changed": changed}


def _assert_scene_seq_available(
    session: Session,
    *,
    scene_id: str,
    chapter_id: str,
    scene_seq: int,
) -> None:
    conflict = session.execute(
        select(SceneCard.scene_id).where(
            SceneCard.chapter_id == chapter_id,
            SceneCard.scene_seq == scene_seq,
            SceneCard.trashed_flag == 0,
            SceneCard.scene_id != scene_id,
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise DomainError(
            "SCENE_SEQUENCE_CONFLICT",
            "another active scene already uses this scene_seq",
            status_code=409,
            details={
                "chapter_id": chapter_id,
                "scene_seq": scene_seq,
                "conflicting_scene_id": conflict,
            },
        )


def _parse_run_policy(payload: dict | None) -> str:
    run_policy = (
        str((payload or {}).get("run_policy") or "reliable").strip() or "reliable"
    )
    if run_policy not in {"reliable", "strict", "auto"}:
        raise DomainError(
            "INVALID_RUN_POLICY",
            "run_policy must be one of reliable|strict|auto",
            status_code=422,
            details={"run_policy": run_policy},
        )
    return run_policy


def _reject_manual_checkpoint_controls(payload: dict | None) -> None:
    supplied = sorted({"from_step", "resume"}.intersection((payload or {}).keys()))
    if supplied:
        raise DomainError(
            "RUN_CHECKPOINT_CONTROL_FORBIDDEN",
            "scene runs resume only from the server-owned durable checkpoint",
            status_code=422,
            details={"unsupported_fields": supplied},
        )


@router.post("/api/v1/scenes/{scene_id}/run/full")
def run_scene(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneRunCommandRequest | None = Body(default=None),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    AuthorLifecycleService(session).require_active_scene(scene_id)
    _reject_manual_checkpoint_controls(body)
    # FE-ALIGN G3：作者改写指令随请求下发（注入风格生成提示词；幂等键随 note 变化）
    author_note = normalize_author_note(body.get("author_note"))
    # Wave 2（治理 §6.3）：run_policy 请求级参数（reliable|strict|auto；列属 Wave 3）
    run_policy = _parse_run_policy(body)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/run/full",
        payload={
            "scene_id": scene_id,
            **({"author_note": author_note} if author_note else {}),
            **({"run_policy": run_policy} if run_policy != "reliable" else {}),
        },
        action=lambda lease: Orchestrator(session).run_scene(
            scene_id,
            author_note=author_note,
            run_policy=run_policy,
            execution_id=lease.execution_id,
            lease_renewer=lease.renew,
        ),
    )


@router.post("/api/v1/scenes/{scene_id}/preflight/create-cards")
def create_scene_preflight_cards(
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """确定性建出当前场景缺失的最小 voice/relation 卡(active)，解阻 run 预检。

    这是 create_minimal_voice_card / create_minimal_relation_card 预检动作的真实执行落点
    （此前该动作只是提示、无可执行端点，是死胡同）。幂等：已有 active 卡则跳过。
    """

    def create_cards() -> dict:
        scene = AuthorLifecycleService(session).require_active_scene(scene_id)
        return SceneRunPreflightService(session).create_missing_cards(scene)

    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/preflight/create-cards",
        payload={"scene_id": scene_id},
        action=create_cards,
    )


@router.post("/api/v1/scenes/{scene_id}/run/jobs")
def create_scene_run_job(
    scene_id: str,
    request: Request,
    start: bool = True,
    session: Session = Depends(get_session),
    payload: SceneRunJobRequest | None = Body(default=None),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    _reject_manual_checkpoint_controls(body)
    job_to_start: str | None = None

    def create_job() -> dict:
        nonlocal job_to_start
        service = SceneRunJobService(session)
        budget_resume_parent_execution_id = (
            service.resolve_budget_resume_execution_id(scene_id)
            if body.get("resume_budget") is True
            else None
        )
        job = service.create_job(
            scene_id,
            actor_ref=actor_ref,
            author_note=body.get("author_note"),
            run_policy=_parse_run_policy(body),
            budget_resume_parent_execution_id=budget_resume_parent_execution_id,
        )
        if start and job.status == "queued":
            job_to_start = job.job_id
        return service.serialize_job(job)

    response = optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/run/jobs",
        payload={"scene_id": scene_id, "start": start, "body": body},
        action=create_job,
    )
    # 闭包只在本请求真正执行动作时填充;持久重放直接返回缓存响应,不再拉起 worker。
    if job_to_start is not None:
        start_scene_run_job_worker(job_to_start)
    return response


@router.get("/api/v1/run-jobs/{job_id}")
def get_run_job(job_id: str, request: Request, session: Session = Depends(get_session)):
    service = SceneRunJobService(session)
    job = service.get_job(job_id)
    return ok(
        service.serialize_job(job), req_id=getattr(request.state, "request_id", None)
    )


@router.post("/api/v1/run-jobs/{job_id}/cancel")
def cancel_run_job(
    job_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneRunCancelRequest | None = Body(default=None),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}

    def cancel() -> dict:
        service = SceneRunJobService(session)
        job = service.request_cancel(
            job_id, actor_ref=actor_ref, reason=body.get("reason")
        )
        return service.serialize_job(job)

    response = optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/run-jobs/{job_id}/cancel",
        payload={"job_id": job_id, "body": body},
        action=cancel,
    )
    # Reasserting the process-local signal is safe and is useful after a replay
    # served by a process that did not execute the original cancellation.
    remember_committed_cancellation(job_id)
    return response


@router.get("/api/v1/scenes/{scene_id}/run/jobs/latest")
def get_latest_scene_run_job(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    AuthorLifecycleService(session).require_active_scene(scene_id)
    service = SceneRunJobService(session)
    return ok(
        service.serialize_job(service.latest_job(scene_id)),
        req_id=getattr(request.state, "request_id", None),
    )


@router.get("/api/v1/scene-run-states")
def list_scene_run_states(
    project_id: str, request: Request, session: Session = Depends(get_session)
):
    """项目内全部场景运行态（管线真相）。

    起草台队列成员的后端派生源：换浏览器后 FE 据此恢复「哪些场进过管线」，
    localStorage 队列退化为这份真相的读缓存（贯通轮遗留项 ①）。
    只返回有运行态行且离开过 ready 的场——ready/无行 = 从未进管线，不参与恢复。
    """
    ProjectService(session).require_project(project_id)
    rows = session.execute(
        select(SceneRunState, SceneCard)
        .join(SceneCard, SceneCard.scene_id == SceneRunState.scene_id)
        .where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        .order_by(SceneRunState.updated_at.desc())
    ).all()
    items = [
        {
            "scene_id": state.scene_id,
            "chapter_id": card.chapter_id,
            "scene_status": state.scene_status,
            # 治理 §5.3：列表恢复面也带作者可见态（枚举），FE 不再从 scene_status 猜
            "author_state": compute_author_state(session, state.scene_id, state)[
                "author_state"
            ],
            "total_attempt_count": state.total_attempt_count,
            "updated_at": state.updated_at,
        }
        for state, card in rows
        if state.scene_status != "ready"
    ]
    return ok(
        {"items": items, "count": len(items)},
        req_id=getattr(request.state, "request_id", None),
    )


@router.get("/api/v1/scenes/{scene_id}/status")
def scene_status(
    scene_id: str, request: Request, session: Session = Depends(get_session)
):
    AuthorLifecycleService(session).require_active_scene(scene_id)
    state = session.get(SceneRunState, scene_id)
    if state is None:
        # 经目录新建、从未 run 的有效场景没有运行态行——返回 ready 空态投影，
        # 与只读 workbench 一致；GET 不为查看动作补建持久行。
        return ok(
            {
                "scene_status": "ready",
                "current_bundle_id": None,
                "current_bundle_hash": None,
                "current_neutral_draft_row_id": None,
                "current_style_draft_row_id": None,
                "current_final_scene_row_id": None,
                "repeat_issue_key": None,
                "repeat_issue_count": 0,
                # 治理 §5.3：作者可见状态投影（React 只消费这层字段）
                **compute_author_state(session, scene_id, None),
            },
            req_id=getattr(request.state, "request_id", None),
        )
    return ok(
        {
            "scene_status": state.scene_status,
            "current_bundle_id": state.current_bundle_id,
            "current_bundle_hash": state.current_bundle_hash,
            "current_neutral_draft_row_id": state.current_neutral_draft_row_id,
            "current_style_draft_row_id": state.current_style_draft_row_id,
            "current_final_scene_row_id": state.current_final_scene_row_id,
            "repeat_issue_key": state.repeat_issue_key,
            "repeat_issue_count": state.repeat_issue_count,
            # 治理 §5.3：作者可见状态投影（React 只消费这层字段）
            **compute_author_state(session, scene_id, state),
        },
        req_id=getattr(request.state, "request_id", None),
    )


def _latest_selection_gate_event(
    session: Session, scene_id: str
) -> HumanReviewEvent | None:
    events = (
        session.execute(
            select(HumanReviewEvent)
            .where(
                HumanReviewEvent.scene_id == scene_id,
                HumanReviewEvent.event_source == "candidate_selection",
            )
            .order_by(
                HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc()
            )
        )
        .scalars()
        .all()
    )
    for event in events:
        if (event.details_json or {}).get("gate_type") == "style_candidate_selection":
            return event
    return None


@router.get("/api/v1/scenes/{scene_id}/style-candidates")
def get_scene_style_candidates(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    include_scores: bool = False,
    diagnostic: bool = False,
):
    """Wave 3（治理 §5.5/§6.3）：候选终选取数——默认盲化视图。

    存在终选 gate 时：按 gate 的 blinded_order 输出**完整正文**，默认剥离
    机器分数与预选标记（按分排序展示本身就是泄漏）；`include_scores=true`
    为作者主动展开——附分数但不改顺序。无 gate（标准场/历史诊断）保留旧的
    按分降序形状（`diagnostic` 用途），并标 `blinded:false`。
    """
    AuthorLifecycleService(session).require_active_scene(scene_id)
    from novel_system.services.literary_quality import adversarial_rank_score

    state = session.get(SceneRunState, scene_id)
    gate = _latest_selection_gate_event(session, scene_id)
    dispersion_score = state.candidate_dispersion_score if state else None
    criticality_info = None
    if state and state.criticality_level:
        criticality_info = {
            "level": state.criticality_level,
            "reasons": state.criticality_reasons_json or [],
        }

    if gate is not None and not diagnostic:
        details = gate.details_json or {}
        blinded_order = [
            str(r)
            for r in (
                details.get("blinded_order") or details.get("candidate_row_ids") or []
            )
        ]
        candidates = []
        for row_id in blinded_order:
            draft = session.get(SceneDraft, row_id)
            if draft is None:
                continue
            entry: dict[str, Any] = {
                "row_id": draft.row_id,
                "content": draft.content,
                "created_at": str(draft.created_at) if draft.created_at else None,
            }
            if include_scores:
                # 主动展开：分数只做标注，不重排（§5.5）
                entry["adversarial_score"] = round(
                    adversarial_rank_score(draft.content) if draft.content else 0.0, 3
                )
            candidates.append(entry)
        return ok(
            {
                "scene_id": scene_id,
                "blinded": True,
                "candidates": candidates,
                "total": len(candidates),
                "selection": {
                    "decision_status": details.get("decision_status"),
                    "selected_row_id": (
                        details.get("selected_row_id")
                        if details.get("decision_status") == "selected"
                        else None
                    ),
                    "event_id": gate.event_id,
                },
                "dispersion_score": dispersion_score,
                "criticality": criticality_info,
            },
            req_id=getattr(request.state, "request_id", None),
        )

    # 无终选 gate：旧诊断形状（按分降序、带分数）——仅限非盲化诊断用途
    drafts = list(
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "style_draft",
            )
            .order_by(SceneDraft.created_at.desc())
        )
        .scalars()
        .all()
    )
    selected_row_id = state.current_style_draft_row_id if state else None
    candidates = []
    for d in drafts:
        score = adversarial_rank_score(d.content) if d.content else 0.0
        candidates.append(
            {
                "row_id": d.row_id,
                "adversarial_score": round(score, 3),
                "content_preview": (d.content or "")[:500],
                "content": d.content,
                "selected": d.row_id == selected_row_id,
                "created_at": str(d.created_at) if d.created_at else None,
            }
        )
    candidates.sort(key=lambda c: c["adversarial_score"], reverse=True)
    return ok(
        {
            "scene_id": scene_id,
            "blinded": False,
            "candidates": candidates,
            "total": len(candidates),
            "dispersion_score": dispersion_score,
            "dispersion_signal": (
                "low"
                if dispersion_score is not None and dispersion_score < 0.15
                else "adequate" if dispersion_score is not None else None
            ),
            "criticality": criticality_info,
        },
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v1/scenes/{scene_id}/style-candidates/{row_id}/select")
def select_style_candidate(
    scene_id: str,
    row_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: StyleCandidateSelectRequest | None = Body(default=None),
):
    """Wave 3（治理 §5.5/§6.3）：作者终选——一次写入 + 锁定。

    相同选择重复提交幂等返回；已存在不同终选记录时新的 select 返回
    409 SELECTION_LOCKED；变更选择需先显式 reopen（留审计）。记录
    选择耗时/无明显差异标记（§5.5 记录选择、放弃、无明显差异和选择耗时）。
    """
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}

    def _select(session: Session) -> dict[str, Any]:
        from novel_system.db.models import utcnow as now_iso
        from novel_system.services.style_reference.style_feedback import (
            build_candidate_selection_feedback,
            normalize_preference_tags,
        )

        AuthorLifecycleService(session).require_active_scene(scene_id)
        draft = session.get(SceneDraft, row_id)
        if draft is None or draft.scene_id != scene_id:
            raise DomainError(
                "CANDIDATE_NOT_FOUND",
                f"Style draft candidate {row_id} not found for scene {scene_id}",
                status_code=404,
            )
        state = session.get(SceneRunState, scene_id)
        if state is None:
            raise DomainError(
                "SCENE_STATE_NOT_FOUND", "Scene run state not found", status_code=404
            )

        gate = _latest_selection_gate_event(session, scene_id)
        preference_tags = normalize_preference_tags(body.get("preference_tags"))
        feedback_recorded = False
        if gate is not None:
            details = dict(gate.details_json or {})
            decision_status = details.get("decision_status")
            if decision_status == "selected":
                if details.get("selected_row_id") == row_id:
                    # 相同选择重复提交：幂等返回（§7.4）
                    return {
                        "scene_id": scene_id,
                        "selected_row_id": row_id,
                        "decision_status": "selected",
                        "message": "Candidate already selected",
                    }
                raise DomainError(
                    "SELECTION_LOCKED",
                    "terminal selection is locked — reopen explicitly before changing the choice",
                    status_code=409,
                    details={
                        "scene_id": scene_id,
                        "selected_row_id": details.get("selected_row_id"),
                    },
                )
            candidate_row_ids = [
                str(r) for r in (details.get("candidate_row_ids") or [])
            ]
            if candidate_row_ids and row_id not in candidate_row_ids:
                raise DomainError(
                    "CANDIDATE_NOT_IN_GATE",
                    "candidate is not part of the terminal-selection gate",
                    status_code=409,
                    details={"scene_id": scene_id, "row_id": row_id},
                )
            # A reopened decision gets a new current feedback record; immutable
            # prior observations remain in style_feedback_history.
            details.pop("style_feedback", None)
            details.pop("style_feedback_error_code", None)
            decided_at = now_iso()
            feedback = None
            feedback_error_code = None
            try:
                feedback = build_candidate_selection_feedback(
                    details,
                    scene_id=scene_id,
                    selected_row_id=row_id,
                    preference_tags=preference_tags,
                    no_clear_difference=bool(body.get("no_clear_difference")),
                    observed_at=decided_at,
                )
            except Exception as exc:  # feedback must not block the author's choice
                feedback_error_code = exc.__class__.__name__
                _LOGGER.warning(
                    "style candidate feedback degraded for scene %s",
                    scene_id,
                    exc_info=True,
                )
            history = list(details.get("decision_history") or [])
            history.append(
                {
                    "action": "select",
                    "row_id": row_id,
                    "actor_ref": actor_ref,
                    "at": decided_at,
                    "no_clear_difference": bool(body.get("no_clear_difference")),
                    "preference_tags": preference_tags,
                    **(
                        {"duration_ms": int(body["duration_ms"])}
                        if isinstance(body.get("duration_ms"), (int, float))
                        else {}
                    ),
                    **(
                        {"style_feedback_id": feedback["feedback_id"]}
                        if feedback is not None
                        else {}
                    ),
                }
            )
            feedback_history = list(details.get("style_feedback_history") or [])
            if feedback is not None:
                feedback_history.append(feedback)
                feedback_recorded = True
            gate.details_json = {
                **details,
                "decision_status": "selected",
                "selected_row_id": row_id,
                "decided_at": decided_at,
                "no_clear_difference": bool(body.get("no_clear_difference")),
                "preference_tags": preference_tags,
                "decision_history": history,
                "style_feedback_history": feedback_history,
                **({"style_feedback": feedback} if feedback is not None else {}),
                **(
                    {"style_feedback_error_code": feedback_error_code}
                    if feedback_error_code is not None
                    else {}
                ),
            }
            gate.status = "resolved"
        else:
            # 无 gate 的旧路径（标准场直接 select）：首次 select 补建已决 gate，
            # 使终选锁定语义对所有场景生效（§6.3 补充契约）。
            from uuid import uuid4

            gate = HumanReviewEvent(
                event_id=f"hre_sel_{uuid4().hex[:12]}",
                scene_id=scene_id,
                chapter_id=draft.chapter_id,
                object_ref=f"candidate_selection:{scene_id}",
                event_source="candidate_selection",
                priority="high",
                status="resolved",
                allowed_actions_json=["select", "reopen"],
                details_json={
                    "gate_type": "style_candidate_selection",
                    "candidate_row_ids": [row_id],
                    "blinded_order": [row_id],
                    "decision_status": "selected",
                    "selected_row_id": row_id,
                    "decided_at": now_iso(),
                    "no_clear_difference": bool(body.get("no_clear_difference")),
                    "preference_tags": preference_tags,
                    "decision_history": [
                        {
                            "action": "select",
                            "row_id": row_id,
                            "actor_ref": actor_ref,
                            "at": now_iso(),
                            "no_clear_difference": bool(
                                body.get("no_clear_difference")
                            ),
                            "preference_tags": preference_tags,
                        }
                    ],
                    "tokens_used": int(getattr(state, "scene_tokens_used", 0) or 0),
                    "style_feedback_history": [],
                },
                default_action="select",
            )
            session.add(gate)

        state.current_style_draft_row_id = row_id
        # 治理 §4.3：候选选择也是「最近有效正文」的维护点
        state.latest_valid_draft_row_id = row_id
        session.flush()
        return {
            "scene_id": scene_id,
            "selected_row_id": row_id,
            "decision_status": "selected",
            "style_feedback_recorded": feedback_recorded,
            "message": "Candidate selected for human terminal review",
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/style-candidates/{row_id}/select",
        payload={"scene_id": scene_id, "row_id": row_id, **body},
        action=lambda: _select(session),
    )


@router.post("/api/v1/scenes/{scene_id}/resume-after-selection")
def resume_after_selection(
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """Wave 3（§5.5/§6.3）：作者终选后从批判修订/QC 续跑到归档。"""
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    AuthorLifecycleService(session).require_active_scene(scene_id)
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/resume-after-selection",
        payload={"scene_id": scene_id},
        action=lambda lease: Orchestrator(session).resume_after_selection(
            scene_id,
            execution_id=lease.execution_id,
            lease_renewer=lease.renew,
        ),
    )


@router.post("/api/v1/scenes/{scene_id}/budget/topup")
def topup_scene_budget(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: SceneBudgetTopupRequest | None = Body(default=None),
):
    """作者显式追加 token/业务尝试/provider 尝试预算；唯一扩容入口，留审计。"""
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json") if payload else {}
    raw_extras = {
        "extra_tokens": body.get("extra_tokens", 0),
        "extra_attempts": body.get("extra_attempts", 0),
        "extra_provider_attempts": body.get("extra_provider_attempts", 0),
    }
    invalid_fields = {
        field: value
        for field, value in raw_extras.items()
        if type(value) is not int or value < 0 or value > INT64_MAX
    }
    if invalid_fields or not any(
        value > 0 for value in raw_extras.values() if type(value) is int
    ):
        raise DomainError(
            "INVALID_BUDGET_TOPUP",
            "topup values must be non-negative integers and at least one must be positive",
            status_code=422,
            details={**raw_extras, "max_lifecycle_budget": INT64_MAX},
        )
    extra_tokens = raw_extras["extra_tokens"]
    extra_attempts = raw_extras["extra_attempts"]
    extra_provider_attempts = raw_extras["extra_provider_attempts"]
    reason = str(body.get("reason") or "").strip()[:300]

    def _topup(session: Session) -> dict[str, Any]:
        from novel_system.db.models import OperationLog
        from novel_system.services.scene_budget import ensure_scene_budget_initialized

        AuthorLifecycleService(session).require_active_scene(scene_id)
        ensure_scene_budget_initialized(session, scene_id)
        updated_budgets = session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.scene_token_budget.is_not(None),
                SceneRunState.scene_token_budget <= INT64_MAX - extra_tokens,
                SceneRunState.attempt_budget <= INT64_MAX - extra_attempts,
                SceneRunState.provider_attempt_budget
                <= INT64_MAX - extra_provider_attempts,
            )
            .values(
                scene_token_budget=SceneRunState.scene_token_budget + extra_tokens,
                attempt_budget=SceneRunState.attempt_budget + extra_attempts,
                provider_attempt_budget=(
                    SceneRunState.provider_attempt_budget + extra_provider_attempts
                ),
            )
            .returning(
                SceneRunState.scene_token_budget,
                SceneRunState.attempt_budget,
                SceneRunState.provider_attempt_budget,
            )
            .execution_options(synchronize_session=False)
        ).one_or_none()
        if updated_budgets is None:
            current = session.execute(
                select(
                    SceneRunState.scene_token_budget,
                    SceneRunState.attempt_budget,
                    SceneRunState.provider_attempt_budget,
                ).where(SceneRunState.scene_id == scene_id)
            ).one()
            details = {
                "extra_tokens": extra_tokens,
                "scene_token_budget": current.scene_token_budget,
                "max_scene_token_budget": INT64_MAX,
            }
            if extra_attempts or extra_provider_attempts:
                details.update(
                    {
                        "extra_attempts": extra_attempts,
                        "attempt_budget": current.attempt_budget,
                        "extra_provider_attempts": extra_provider_attempts,
                        "provider_attempt_budget": current.provider_attempt_budget,
                        "max_lifecycle_budget": INT64_MAX,
                    }
                )
            raise DomainError(
                "INVALID_BUDGET_TOPUP",
                "lifecycle budget topup exceeds the signed 64-bit limit",
                status_code=422,
                details=details,
            )
        new_budget, new_attempt_budget, new_provider_attempt_budget = map(
            int, updated_budgets
        )
        state = session.get(SceneRunState, scene_id)
        assert state is not None
        session.refresh(state)
        session.add(
            OperationLog(
                event_type="scene_budget_topup",
                object_type="scene",
                object_ref=scene_id,
                payload_json={
                    "extra_tokens": extra_tokens,
                    "extra_attempts": extra_attempts,
                    "extra_provider_attempts": extra_provider_attempts,
                    "reason": reason,
                    "actor_ref": actor_ref,
                    "scene_token_budget": new_budget,
                    "scene_tokens_used": int(state.scene_tokens_used or 0),
                    "scene_tokens_reserved": int(state.scene_tokens_reserved or 0),
                    "attempt_budget": new_attempt_budget,
                    "total_attempt_count": int(state.total_attempt_count or 0),
                    "provider_attempt_budget": new_provider_attempt_budget,
                    "provider_attempts_used": int(state.provider_attempts_used or 0),
                },
            )
        )
        session.flush()
        return {
            "scene_id": scene_id,
            "scene_token_budget": new_budget,
            "scene_tokens_used": int(state.scene_tokens_used or 0),
            "scene_tokens_reserved": int(state.scene_tokens_reserved or 0),
            "attempt_budget": new_attempt_budget,
            "total_attempt_count": int(state.total_attempt_count or 0),
            "provider_attempt_budget": new_provider_attempt_budget,
            "provider_attempts_used": int(state.provider_attempts_used or 0),
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/budget/topup",
        payload={
            "scene_id": scene_id,
            "extra_tokens": extra_tokens,
            "extra_attempts": extra_attempts,
            "extra_provider_attempts": extra_provider_attempts,
            "reason": reason,
        },
        action=lambda: _topup(session),
    )


def _author_draft_plain_text(html: str | None) -> str:
    """author-draft 存 HTML（<p> 分段）；归档正文按段落还原为纯文本。"""
    import re

    if not html:
        return ""
    text = re.sub(r"</p\s*>|<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _scene_lifecycle_budget_payload(state: SceneRunState) -> dict[str, int] | None:
    """Author-safe lifecycle counters used by the explicit topup UI.

    The immutable basis remains server-owned; only the single-call unit needed
    for an informed author topup is projected. No routing or credential data is
    exposed.
    """
    if state.scene_token_budget is None:
        return None
    budget = int(state.scene_token_budget)
    used = int(state.scene_tokens_used or 0)
    reserved = int(state.scene_tokens_reserved or 0)
    basis = (
        state.scene_budget_basis_json
        if isinstance(state.scene_budget_basis_json, dict)
        else {}
    )
    raw_baseline = basis.get("baseline_tokens")
    baseline = (
        int(raw_baseline)
        if type(raw_baseline) is int and raw_baseline > 0
        else max(1, budget // 5)
    )
    return {
        "scene_token_budget": budget,
        "scene_tokens_used": used,
        "scene_tokens_reserved": reserved,
        "scene_tokens_remaining": max(0, budget - used - reserved),
        "baseline_tokens": baseline,
        "recommended_topup_tokens": baseline,
        "attempt_budget": int(state.attempt_budget),
        "total_attempt_count": int(state.total_attempt_count or 0),
        "provider_attempt_budget": int(state.provider_attempt_budget),
        "provider_attempts_used": int(state.provider_attempts_used or 0),
    }


@router.post("/api/v1/scenes/{scene_id}/adopt-current")
def adopt_current_scene(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
    payload: AdoptCurrentRequest | None = Body(default=None),
):
    """治理 §5.2：作者采纳归档的单一服务入口。

    前端「归档/置 done」动作必须打到这里——携带 exact_author_draft 时，
    作者稿 CAS 保存与 CanonicalScene 提升在同一个幂等事务中完成，浏览器正文
    不再与 FinalScene 分裂。兼容调用未携带 exact_author_draft 时，内容源优先级
    仍为未归档 current_final_scene → 管线草稿（latest_valid > style > neutral）→
    author-draft 人工稿兜底。守卫：无任何有效稿 409 NO_VALID_DRAFT；
    确定性来源安全扫描命中 409 SOURCE_SAFETY_BLOCKED（草稿保留可重试，
    设计红线 8：来源安全未通过可保存草稿但不能标记为已安全归档）。
    """
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json") if payload is not None else {}
    accepted_warning_codes = body.get("accepted_warning_codes") or []
    exact_author_draft = body.get("exact_author_draft")

    def _adopt(session: Session) -> dict[str, Any]:
        from uuid import uuid4

        scene = AuthorLifecycleService(session).require_active_scene(scene_id)
        state = session.get(SceneRunState, scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            session.add(state)
            session.flush()

        # 兼容旧调用的已归档幂等返回。精确作者稿可能是在已归档版本之上的
        # 新修订，必须继续走 revision + FinalScene 双 CAS，不能在这里吞掉。
        # （如 C2 真实库中 failed@soft_qc_ready 的历史残留，作者重点一次即自愈）
        if (
            exact_author_draft is None
            and state.scene_status == "archived"
            and state.current_final_scene_row_id
        ):
            current_final = session.get(FinalScene, state.current_final_scene_row_id)
            if current_final is None or current_final.scene_id != scene_id:
                raise DomainError(
                    "FINAL_SCENE_NOT_FOUND",
                    "archived scene points to a missing final manuscript",
                    status_code=409,
                    details={"scene_id": scene_id},
                )
            current_memory = (
                session.execute(
                    select(SceneMemory).where(
                        SceneMemory.scene_id == scene_id,
                        SceneMemory.final_scene_row_id == current_final.row_id,
                        SceneMemory.active_flag == 1,
                    )
                )
                .scalars()
                .first()
            )
            confirmation = Archiver(session).archive_final_scene(
                scene_id,
                current_final.row_id,
                carry_notes_json=(
                    list(current_memory.carry_notes_json or [])
                    if current_memory is not None
                    else []
                ),
                author_confirmed_final=True,
                accepted_warning_codes=accepted_warning_codes,
            )
            residue_finalized = SceneRunCheckpointService(
                session
            ).finalize_after_author_archive(scene_id)
            return {
                "scene_id": scene_id,
                "scene_status": "archived",
                "final_scene_row_id": state.current_final_scene_row_id,
                "already_archived": True,
                "safe_to_archive": confirmation["safe_to_archive"],
                "literary_warnings_unresolved": confirmation[
                    "literary_warnings_unresolved"
                ],
                "author_confirmed_final": confirmation["author_confirmed_final"],
                "finality": confirmation["finality"],
                "run_residue_finalized": residue_finalized,
                "author_state": compute_author_state(session, scene_id, state),
            }

        # Wave 2（治理 §5.3/§5.4）：只有真实 Q0/Q1 能阻断归档——投影为 hard_blocked
        # （当前 QC 报告存在 verified Q0/Q1 分级条目）时拒绝采纳，正文保留（§7.2）。
        projection = compute_author_state(session, scene_id, state)
        if projection["author_state"] == "hard_blocked":
            raise DomainError(
                "HARD_BLOCKED",
                "verified Q0/Q1 findings block adoption — resolve or revise before archiving",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "blocking_findings": projection["blocking_findings"],
                },
            )
        # Wave 3（§5.5 完成门）：关键场景未终选前不可归档——adopt 旁路同样封死
        if projection["author_state"] == "awaiting_author_choice":
            raise DomainError(
                "SELECTION_REQUIRED",
                "author terminal selection is required before archiving this critical scene",
                status_code=409,
                details={"scene_id": scene_id},
            )

        # 浏览器精确稿路径：先以 base_revision_no 保存请求中的确定正文，再把
        # 保存后的同一修订提升为 FinalScene。两个动作共享当前数据库事务；保存、
        # 安全门、聚合或归档任一步失败都会整体回滚。
        if exact_author_draft is not None:
            draft_id = exact_author_draft["draft_id"]
            draft = session.get(AuthorDraft, draft_id)
            if draft is None:
                raise DomainError(
                    "AUTHOR_DRAFT_NOT_FOUND",
                    "author draft not found",
                    status_code=404,
                    details={"draft_id": draft_id},
                )
            if draft.object_type != "scene" or draft.object_id != scene_id:
                raise DomainError(
                    "AUTHOR_DRAFT_SCENE_MISMATCH",
                    "author draft does not belong to the scene being adopted",
                    status_code=409,
                    details={
                        "draft_id": draft_id,
                        "draft_object_type": draft.object_type,
                        "draft_object_id": draft.object_id,
                        "scene_id": scene_id,
                    },
                )
            saved = AuthorDraftService(session).save(
                draft_id,
                {
                    "content": exact_author_draft["content"],
                    "base_revision_no": exact_author_draft["base_revision_no"],
                    "note": "atomic scene adoption",
                },
                actor_ref=actor_ref,
            )
            saved_draft = saved.get("draft") or {}
            saved_revision_no = saved_draft.get("revision_no")
            if not isinstance(saved_revision_no, int):
                raise DomainError(
                    "AUTHOR_DRAFT_SAVE_INCOMPLETE",
                    "saved author draft did not return a revision number",
                    status_code=500,
                    details={"draft_id": draft_id},
                )
            promoted = CanonicalSceneService(session).promote_author_draft(
                draft_id,
                {
                    "base_revision_no": saved_revision_no,
                    "expected_current_final_scene_row_id": exact_author_draft[
                        "expected_current_final_scene_row_id"
                    ],
                    # Saving exact author text proves which revision was chosen;
                    # it does not prove that story facts stayed unchanged.
                    "narrative_effect": "requires_reconcile",
                    "accepted_warning_codes": accepted_warning_codes,
                },
                actor_ref=actor_ref,
            )
            session.flush()
            session.refresh(draft)
            promoted["author_draft"] = AuthorDraftService.serialize_draft(
                draft,
                current_final_scene_row_id=promoted["final_scene_row_id"],
            )
            promoted["exact_author_draft"] = True
            promoted["author_state"] = compute_author_state(session, scene_id, state)
            return promoted

        # 1) 内容源解析
        final: FinalScene | None = None
        if state.current_final_scene_row_id:
            row = session.get(FinalScene, state.current_final_scene_row_id)
            if row is not None and (row.content or "").strip():
                final = row
        source_draft_row_id: str | None = None
        content: str | None = None
        source_bundle_id: str | None = None
        source_bundle_hash: str | None = None
        if final is None:
            for row_id in (
                state.latest_valid_draft_row_id,
                state.current_style_draft_row_id,
                state.current_neutral_draft_row_id,
            ):
                if not row_id:
                    continue
                draft = session.get(SceneDraft, row_id)
                if draft is not None and (draft.content or "").strip():
                    source_draft_row_id = row_id
                    content = draft.content
                    source_bundle_id = draft.source_bundle_id
                    source_bundle_hash = draft.source_bundle_hash
                    break
            if content is None:
                author_draft = (
                    session.execute(
                        select(AuthorDraft).where(
                            AuthorDraft.object_type == "scene",
                            AuthorDraft.object_id == scene_id,
                            AuthorDraft.status == "current",
                        )
                    )
                    .scalars()
                    .first()
                )
                text = (
                    _author_draft_plain_text(author_draft.content)
                    if author_draft
                    else ""
                )
                if text.strip():
                    content = text
                    source_bundle_id = f"author_draft:{author_draft.draft_id}"
                    source_bundle_hash = f"author_draft_rev_{author_draft.revision_no}"
            if content is None:
                raise DomainError(
                    "NO_VALID_DRAFT",
                    "no valid draft content to adopt — generate or write the scene first",
                    status_code=409,
                    details={"scene_id": scene_id},
                )

        # 2) 确定性来源安全守卫（Q0 红线；Q0–Q3 分级阻断策略随 Wave 2 落地）
        target_content = final.content if final is not None else (content or "")
        bundle = (
            session.get(SceneBundle, state.current_bundle_id)
            if state.current_bundle_id
            else None
        )
        scan = ReferenceSafetyService(session).scan_runtime_text(
            target_content,
            source_profile_ids=source_profile_ids_from_snapshot(
                bundle.frozen_snapshot_json if bundle else None
            ),
        )
        if not scan.get("safe", True):
            raise DomainError(
                "SOURCE_SAFETY_BLOCKED",
                "source-safety scan blocked adoption — draft is kept and can be revised",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "blocked_terms": scan.get("blocked_terms") or [],
                },
            )

        # 3) FinalScene 建行或提升，经归档事务统一置权威态
        if final is None:
            final = FinalScene(
                row_id=f"final_scene_{scene_id}_adopt_{uuid4().hex[:10]}",
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                content=content or "",
                source_bundle_id=source_bundle_id or "author_adopt",
                source_bundle_hash=source_bundle_hash or "author_adopt",
            )
            session.add(final)
            session.flush()
        state.current_final_scene_row_id = final.row_id
        if source_draft_row_id:
            state.latest_valid_draft_row_id = source_draft_row_id

        carry_notes: list[dict[str, Any]] = [
            {"kind": "author_adoption", "actor_ref": actor_ref}
        ]
        quality_warnings = [
            item
            for item in projection.get("quality_warnings") or []
            if isinstance(item, dict)
        ]
        if quality_warnings:
            # Wave 2（Wave 2 项 7）：采纳带 Q2/Q3 警告的稿 = 作者显式接受，留审计
            carry_notes.append(
                {
                    "kind": "quality_warning_acceptance",
                    "actor_ref": actor_ref,
                    "accepted": [
                        {
                            "issue_key": item.get("issue_key") or item.get("kind"),
                            "quality_level": item.get("quality_level"),
                        }
                        for item in quality_warnings[:10]
                    ],
                }
            )
        archive_result = Archiver(session).archive_final_scene(
            scene_id,
            final.row_id,
            carry_notes_json=carry_notes,
            author_confirmed_final=True,
            accepted_warning_codes=accepted_warning_codes,
        )
        # C2 状态一致性债务：归档后无主执行残留（failed@soft_qc_ready 等）
        # 在同一事务内收敛为 completed/archived，运维/展示不再被误导
        run_residue_finalized = SceneRunCheckpointService(
            session
        ).finalize_after_author_archive(scene_id)
        return {
            "scene_id": scene_id,
            "scene_status": archive_result["scene_status"],
            "final_scene_row_id": final.row_id,
            "scene_memory_row_id": archive_result["scene_memory_row_id"],
            "safe_to_archive": archive_result["safe_to_archive"],
            "literary_warnings_unresolved": archive_result[
                "literary_warnings_unresolved"
            ],
            "author_confirmed_final": archive_result["author_confirmed_final"],
            "finality": archive_result["finality"],
            "source_safety_scan": scan,
            "run_residue_finalized": run_residue_finalized,
            "author_state": compute_author_state(session, scene_id, state),
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/adopt-current",
        payload={"scene_id": scene_id, **body},
        action=lambda: _adopt(session),
    )


@router.get("/api/v1/scenes/{scene_id}/workbench")
def scene_workbench(
    scene_id: str, request: Request, session: Session = Depends(get_session)
):
    scene = AuthorLifecycleService(session).require_active_scene(scene_id)
    chapter = session.get(ChapterGoal, scene.chapter_id)
    state = session.get(SceneRunState, scene_id)
    chapter_state = chapter_state_snapshot(session, scene.chapter_id)
    run_preflight = SceneRunPreflightService(session).build(scene, chapter_state)
    bundle = (
        session.get(SceneBundle, state.current_bundle_id)
        if state is not None and state.current_bundle_id
        else None
    )
    neutral = (
        session.get(SceneDraft, state.current_neutral_draft_row_id)
        if state is not None and state.current_neutral_draft_row_id
        else None
    )
    style = (
        session.get(SceneDraft, state.current_style_draft_row_id)
        if state is not None and state.current_style_draft_row_id
        else None
    )
    final = (
        session.get(FinalScene, state.current_final_scene_row_id)
        if state is not None and state.current_final_scene_row_id
        else None
    )
    source_safety_scan = ReferenceSafetyService(session).scan_runtime_text(
        final.content if final else "",
        source_profile_ids=source_profile_ids_from_snapshot(
            bundle.frozen_snapshot_json if bundle else None
        ),
    )
    memory = (
        session.execute(
            select(SceneMemory).where(
                SceneMemory.scene_id == scene_id, SceneMemory.active_flag == 1
            )
        )
        .scalars()
        .first()
    )
    attempts = (
        session.execute(
            select(AttemptTracker)
            .where(AttemptTracker.scene_id == scene_id)
            .order_by(AttemptTracker.attempt_id.asc())
        )
        .scalars()
        .all()
    )
    blueprint_service = SceneBlueprintService(session)
    contract_service = SceneExecutionContractService(session)
    execution_contract = contract_service.latest(scene_id)
    response = ok(
        {
            "chapter_goal": {
                "chapter_id": chapter.chapter_id,
                "chapter_goal": chapter.chapter_goal,
                "main_plot_push": chapter.main_plot_push,
                "emotional_target": chapter.emotional_target,
                "ending_effect": chapter.ending_effect,
            },
            "scene_card": {
                "scene_id": scene.scene_id,
                "scene_goal": scene.scene_goal,
                "beats_json": scene.beats_json,
                "must_include_text": clean_backfill_markers(scene.must_include_text),
                "location": scene.location,
            },
            "scene_run_state": {
                "scene_status": state.scene_status if state is not None else "ready",
                "current_bundle_id": (
                    state.current_bundle_id if state is not None else None
                ),
                "current_bundle_hash": (
                    state.current_bundle_hash if state is not None else None
                ),
                "current_final_scene_row_id": (
                    state.current_final_scene_row_id if state is not None else None
                ),
                "lifecycle_budget": (
                    _scene_lifecycle_budget_payload(state)
                    if state is not None
                    else None
                ),
            },
            # 治理 §5.3：作者可见状态投影块（完整契约字段）
            "author_state": compute_author_state(session, scene_id, state),
            "chapter_state": chapter_state,
            "run_preflight": run_preflight,
            "bundle": {
                "bundle_id": bundle.bundle_id if bundle else None,
                "bundle_snapshot_hash": bundle.bundle_snapshot_hash if bundle else None,
                "snapshot": bundle.frozen_snapshot_json if bundle else None,
            },
            "neutral_draft": (
                {"row_id": neutral.row_id, "content": neutral.content}
                if neutral
                else None
            ),
            "style_draft": (
                {"row_id": style.row_id, "content": style.content} if style else None
            ),
            "final_scene": (
                {"row_id": final.row_id, "content": final.content} if final else None
            ),
            "source_safety_scan": source_safety_scan,
            "anti_template_quality_summary": _serialize_anti_template_quality_summary(
                session, final
            ),
            "literary_blueprint": blueprint_service.latest_payload(scene_id),
            "execution_contract": contract_service.serialize(execution_contract),
            "scene_memory": (
                {"row_id": memory.row_id, "content": memory.content} if memory else None
            ),
            "generation_summary": (
                _serialize_generation_summary(session, scene_id, state)
                if state is not None
                else None
            ),
            "near_final_summary": _serialize_near_final_summary(session, scene_id),
            "hard_qc_summary": (
                _serialize_qc_summary(
                    _latest_qc_report(session, scene_id, state, "hard_qc")
                )
                if state is not None
                else None
            ),
            "soft_qc_summary": (
                _serialize_qc_summary(
                    _latest_qc_report(session, scene_id, state, "soft_qc")
                )
                if state is not None
                else None
            ),
            "rewrite_counters": {
                "hard_partial_rewrite_count": (
                    state.hard_partial_rewrite_count if state is not None else 0
                ),
                "hard_full_rewrite_count": (
                    state.hard_full_rewrite_count if state is not None else 0
                ),
                "soft_patch_count": state.soft_patch_count if state is not None else 0,
                "repeat_issue_key": (
                    state.repeat_issue_key if state is not None else None
                ),
                "repeat_issue_count": (
                    state.repeat_issue_count if state is not None else 0
                ),
            },
            "human_review_summary": (
                _serialize_human_review_summary(
                    _resolve_human_review_event(session, scene_id, state)
                )
                if state is not None
                else None
            ),
            "writer_review_summary": WriterReviewService(session).scene_summary(
                scene_id
            ),
            "attempts": [_serialize_attempt(item) for item in attempts],
        },
        req_id=getattr(request.state, "request_id", None),
    )
    return response


def _serialize_anti_template_quality_summary(
    session: Session, final: FinalScene | None
) -> dict | None:
    if final is None or not (final.content or "").strip():
        return None
    return LiteraryQualityService(session).analyze_text(
        {
            "content": final.content or "",
            "object_type": "scene",
            "object_id": final.scene_id,
            "chapter_id": final.chapter_id,
            "scene_id": final.scene_id,
            "source_ref": f"final_scene:{final.row_id}",
        }
    )


def _serialize_generation_summary(
    session: Session, scene_id: str, state: SceneRunState
) -> dict | None:
    llm_call = _resolve_generation_llm_call(session, scene_id, state)
    if llm_call is None:
        return None
    summary = {
        "llm_call_id": llm_call.llm_call_id,
        "step": _display_generation_step(llm_call.step),
        "raw_step": llm_call.step,
        "provider": llm_call.provider,
        "model": llm_call.model,
        "prompt_hash": llm_call.prompt_hash,
        "prompt_tokens": llm_call.prompt_tokens,
        "completion_tokens": llm_call.completion_tokens,
        "total_tokens": llm_call.total_tokens,
        "latency_ms": llm_call.latency_ms,
        "finish_reason": llm_call.finish_reason,
        "error_code": llm_call.error_code,
        "created_at": llm_call.created_at,
    }
    return summary


def _resolve_generation_llm_call(
    session: Session, scene_id: str, state: SceneRunState
) -> LlmCall | None:
    if state.current_final_scene_row_id:
        final_scene = session.get(FinalScene, state.current_final_scene_row_id)
        if (
            final_scene is not None
            and final_scene.scene_id == scene_id
            and final_scene.generation_llm_call_id
        ):
            llm_call = session.get(LlmCall, final_scene.generation_llm_call_id)
            if llm_call is not None:
                return llm_call

    for row_id in (
        state.current_style_draft_row_id,
        state.current_neutral_draft_row_id,
    ):
        if not row_id:
            continue
        draft = session.get(SceneDraft, row_id)
        if (
            draft is None
            or draft.scene_id != scene_id
            or not draft.generation_llm_call_id
        ):
            continue
        llm_call = session.get(LlmCall, draft.generation_llm_call_id)
        if llm_call is not None:
            return llm_call
    return None


def _display_generation_step(raw_step: str | None) -> str | None:
    return {
        "scene_literary_rewrite": "literary_rewrite",
        "soft_patch": "style_patch",
        "style_draft": "style_draft",
        "neutral_draft": "neutral_draft",
    }.get(raw_step, raw_step)


def _serialize_near_final_summary(session: Session, scene_id: str) -> dict | None:
    latest_attempt = (
        session.execute(
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "near_final_acceptance_review",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        .scalars()
        .first()
    )
    latest_evaluation = (
        session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == "scene",
                WriterEvaluation.object_id == scene_id,
                WriterEvaluation.rubric_id == NEAR_FINAL_RUBRIC_ID,
            )
            .order_by(
                WriterEvaluation.created_at.desc(),
                WriterEvaluation.evaluation_id.desc(),
            )
        )
        .scalars()
        .first()
    )
    if latest_attempt is None and latest_evaluation is None:
        return None
    details = (
        dict(latest_attempt.details_json or {}) if latest_attempt is not None else {}
    )
    revision_candidate = None
    candidate_id = details.get("revision_candidate_id")
    if isinstance(candidate_id, str) and candidate_id.strip():
        revision_candidate = session.get(RevisionCandidate, candidate_id)
    if revision_candidate is None:
        revision_candidate = (
            session.execute(
                select(RevisionCandidate)
                .where(
                    RevisionCandidate.object_type == "scene",
                    RevisionCandidate.object_id == scene_id,
                    RevisionCandidate.revision_type == NEAR_FINAL_REWRITE_TYPE,
                )
                .order_by(
                    RevisionCandidate.created_at.desc(),
                    RevisionCandidate.revision_id.desc(),
                )
            )
            .scalars()
            .first()
        )
    near_final_status = latest_attempt.status if latest_attempt is not None else None
    if near_final_status is None and latest_evaluation is not None:
        near_final_status = (
            "human_review_required"
            if latest_evaluation.requires_human_review
            else "revision_required"
        )
    failure_class = details.get("failure_class") or (
        latest_evaluation.failure_class if latest_evaluation is not None else None
    )
    archive_attempt = (
        session.execute(
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "archive",
                AttemptTracker.status == "completed",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        .scalars()
        .first()
    )
    archive_gate = (
        (archive_attempt.details_json or {}).get("final_text_gate")
        if archive_attempt is not None
        else {}
    )
    archived_safe = archive_gate.get("safe_to_archive", archive_gate.get("archivable"))
    author_confirmed_final = bool(archive_gate.get("author_confirmed_final"))
    literary_warnings_unresolved = bool(
        not author_confirmed_final
        and (
            archive_gate.get("literary_warnings_unresolved")
            or near_final_status != "near_final_ready"
            or (latest_evaluation is not None and latest_evaluation.findings_json)
        )
    )
    return {
        "rubric_id": NEAR_FINAL_RUBRIC_ID,
        "near_final_status": near_final_status,
        "pipeline_stage": _near_final_pipeline_stage(near_final_status),
        "failure_class": failure_class,
        "failure_reason": _near_final_failure_label(failure_class),
        "auto_rewrite_eligible": (
            bool(latest_evaluation.auto_rewrite_eligible)
            if latest_evaluation is not None
            and latest_evaluation.auto_rewrite_eligible is not None
            else None
        ),
        "contract_field_refs": (
            latest_evaluation.contract_field_refs_json
            if latest_evaluation is not None
            else {}
        ),
        "promotion_blockers": (
            latest_evaluation.promotion_blockers_json
            if latest_evaluation is not None
            else []
        ),
        "evaluation_id": (
            latest_evaluation.evaluation_id
            if latest_evaluation is not None
            else details.get("evaluation_id")
        ),
        "revision_candidate_id": (
            revision_candidate.revision_id if revision_candidate is not None else None
        ),
        "revision_candidate_status": (
            revision_candidate.status if revision_candidate is not None else None
        ),
        "overall_score": (
            latest_evaluation.overall_score if latest_evaluation is not None else None
        ),
        "requires_human_review": (
            bool(latest_evaluation.requires_human_review)
            if latest_evaluation is not None
            else False
        ),
        "safe_to_archive": bool(archived_safe) if archived_safe is not None else None,
        "literary_warnings_unresolved": literary_warnings_unresolved,
        "author_confirmed_final": author_confirmed_final,
        "finality": {
            "safe_to_archive": (
                bool(archived_safe) if archived_safe is not None else None
            ),
            "literary_warnings_unresolved": literary_warnings_unresolved,
            "author_confirmed_final": author_confirmed_final,
        },
        "findings": (
            latest_evaluation.findings_json if latest_evaluation is not None else []
        ),
        "revision_brief": (
            latest_evaluation.revision_brief_json
            if latest_evaluation is not None
            else []
        ),
        "stage_order": [
            "Planning",
            "Drafting",
            "Rewriting",
            "Acceptance Review",
            "Near-final",
        ],
        "created_at": (
            latest_evaluation.created_at
            if latest_evaluation is not None
            else latest_attempt.created_at
        ),
    }


def _near_final_pipeline_stage(status: str | None) -> str:
    return {
        "near_final_ready": "Near-final",
        "revision_required": "Acceptance Review",
        "human_review_required": "Acceptance Review",
    }.get(status or "", "Planning")


def _near_final_failure_label(failure_class: Any) -> str | None:
    if not isinstance(failure_class, str) or not failure_class:
        return None
    return {
        "fact_blocker": "fact",
        "scene_structure_failure": "structure",
        "character_flatness": "character",
        "prose_model_voice": "prose",
        "ending_weakness": "prose",
        "chapter_payoff_gap": "chapter",
        "reference_safety": "safety",
    }.get(failure_class, failure_class)


def _latest_qc_report(
    session: Session, scene_id: str, state: SceneRunState, qc_type: str
) -> QcReport | None:
    if state.current_qc_report_id:
        current_report = session.get(QcReport, state.current_qc_report_id)
        if current_report is not None and current_report.scene_id == scene_id:
            if current_report.qc_type == qc_type:
                return current_report
            if current_report.source_bundle_id:
                return _latest_qc_report_for_bundle(
                    session, scene_id, current_report.source_bundle_id, qc_type
                )

    current_bundle_id = _resolve_current_run_bundle_id(session, scene_id, state)
    if not current_bundle_id:
        return None
    return _latest_qc_report_for_bundle(session, scene_id, current_bundle_id, qc_type)


def _latest_qc_report_for_bundle(
    session: Session, scene_id: str, bundle_id: str, qc_type: str
) -> QcReport | None:
    return (
        session.execute(
            select(QcReport)
            .where(
                QcReport.scene_id == scene_id,
                QcReport.qc_type == qc_type,
                QcReport.source_bundle_id == bundle_id,
            )
            .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        )
        .scalars()
        .first()
    )


def _resolve_current_run_bundle_id(
    session: Session, scene_id: str, state: SceneRunState
) -> str | None:
    if state.current_bundle_id:
        return state.current_bundle_id

    if state.current_final_scene_row_id:
        final_scene = session.get(FinalScene, state.current_final_scene_row_id)
        if (
            final_scene is not None
            and final_scene.scene_id == scene_id
            and final_scene.source_bundle_id
        ):
            return final_scene.source_bundle_id

    for row_id in (
        state.current_style_draft_row_id,
        state.current_neutral_draft_row_id,
    ):
        if not row_id:
            continue
        draft = session.get(SceneDraft, row_id)
        if draft is not None and draft.scene_id == scene_id and draft.source_bundle_id:
            return draft.source_bundle_id

    return None


def _serialize_qc_summary(report: QcReport | None) -> dict | None:
    if report is None:
        return None
    summary = {
        "qc_report_id": report.qc_report_id,
        "qc_type": report.qc_type,
        "pass_flag": None if report.pass_flag is None else bool(report.pass_flag),
        "resolution_code": report.resolution_code,
        "issue_keys": _extract_issue_keys(report.issues_json or []),
        "next_action": report.next_action,
        "rewrite_brief": _extract_rewrite_brief(report.rewrite_brief_json or []),
        "created_at": report.created_at,
    }
    if report.issues_json:
        summary["issues"] = report.issues_json
    evidence_spans = _extract_evidence_spans(report.issues_json or [])
    if evidence_spans:
        summary["evidence_spans"] = evidence_spans
    return summary


def _extract_issue_keys(entries: list[dict]) -> list[str]:
    issue_keys: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        issue_key = entry.get("issue_key")
        if isinstance(issue_key, str) and issue_key.strip():
            issue_keys.append(issue_key.strip())
    return issue_keys


def _extract_evidence_spans(entries: list[dict]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_spans = entry.get("evidence_spans")
        if isinstance(entry_spans, list):
            spans.extend(span for span in entry_spans if isinstance(span, dict))
    return spans


def _extract_rewrite_brief(entries: list[dict]) -> list[str]:
    rewrite_brief: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        instruction = entry.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            rewrite_brief.append(instruction.strip())
            continue
        carry_note_text = entry.get("carry_note_text")
        if isinstance(carry_note_text, str) and carry_note_text.strip():
            rewrite_brief.append(carry_note_text.strip())
    return rewrite_brief


def _resolve_human_review_event(
    session: Session, scene_id: str, state: SceneRunState
) -> HumanReviewEvent | None:
    if not state.current_human_review_event_id:
        return None
    event = session.get(HumanReviewEvent, state.current_human_review_event_id)
    if event is not None and event.scene_id == scene_id:
        return event
    return None


def _serialize_human_review_summary(event: HumanReviewEvent | None) -> dict | None:
    if event is None:
        return None
    details = dict(event.details_json or {})
    return {
        "event_id": event.event_id,
        "status": event.status,
        "event_source": event.event_source,
        "priority": event.priority,
        "trigger_reason": details.get("trigger_reason"),
        "failure_reason": details.get("failure_reason"),
        "recommended_action": details.get("recommended_action"),
        "linked_target_ref": details.get("linked_target_ref"),
        "created_at": event.created_at,
    }


def _load_generation_history_lookups(
    session: Session,
    attempts: list[AttemptTracker],
) -> dict[str, dict[str, Any]]:
    details_list = [dict(item.details_json or {}) for item in attempts]
    draft_ids = {
        value
        for details in details_list
        for key in ("row_id", "source_draft_row_id", "source_style_draft_row_id")
        if (value := _detail_str(details, key)) is not None
    }
    final_scene_ids = {
        value
        for details in details_list
        if (value := _detail_str(details, "final_scene_row_id")) is not None
    }
    qc_report_ids = {
        value
        for details in details_list
        for key in ("qc_report_id", "source_qc_report_id")
        if (value := _detail_str(details, key)) is not None
    }
    event_ids = {
        value
        for details in details_list
        if (value := _detail_str(details, "human_review_event_id")) is not None
    }
    direct_llm_call_ids = {
        value
        for details in details_list
        for key in ("llm_call_id", "final_generation_llm_call_id")
        if (value := _detail_str(details, key)) is not None
    }

    def _load(model, key_column, ids: set[str]) -> dict[str, Any]:
        if not ids:
            return {}
        rows = session.execute(select(model).where(key_column.in_(ids))).scalars().all()
        return {str(getattr(row, key_column.key)): row for row in rows}

    drafts = _load(SceneDraft, SceneDraft.row_id, draft_ids)
    final_scenes = _load(FinalScene, FinalScene.row_id, final_scene_ids)
    qc_reports = _load(QcReport, QcReport.qc_report_id, qc_report_ids)
    review_events = _load(HumanReviewEvent, HumanReviewEvent.event_id, event_ids)
    linked_llm_call_ids = {
        str(row.generation_llm_call_id)
        for row in [*drafts.values(), *final_scenes.values()]
        if row.generation_llm_call_id
    }
    llm_calls = _load(
        LlmCall,
        LlmCall.llm_call_id,
        direct_llm_call_ids | linked_llm_call_ids,
    )
    return {
        "drafts": drafts,
        "final_scenes": final_scenes,
        "qc_reports": qc_reports,
        "review_events": review_events,
        "llm_calls": llm_calls,
    }


def _serialize_generation_history_item(
    item: AttemptTracker,
    *,
    attempt_order: int,
    lookups: dict[str, dict[str, Any]],
) -> dict:
    details = dict(item.details_json or {})
    llm_call = _resolve_attempt_llm_call(item, details, lookups)
    qc_report = _resolve_attempt_qc_report(item, details, lookups)
    source_qc_report = _resolve_scene_scoped_qc_report(
        lookups["qc_reports"],
        item.scene_id,
        _detail_str(details, "source_qc_report_id"),
    )
    human_review_event = _resolve_attempt_human_review_event(item, details, lookups)
    return {
        "attempt_order": attempt_order,
        "attempt": _serialize_attempt(item),
        "reference_ids": {
            "source_bundle_id": item.source_bundle_id,
            "row_id": _detail_str(details, "row_id"),
            "source_draft_row_id": _detail_str(details, "source_draft_row_id"),
            "source_style_draft_row_id": _detail_str(
                details, "source_style_draft_row_id"
            ),
            "final_scene_row_id": _detail_str(details, "final_scene_row_id"),
            "llm_call_id": (
                llm_call.llm_call_id
                if llm_call is not None
                else _detail_str(details, "llm_call_id")
            ),
            "qc_report_id": qc_report.qc_report_id if qc_report is not None else None,
            "source_qc_report_id": (
                source_qc_report.qc_report_id if source_qc_report is not None else None
            ),
            "human_review_event_id": (
                human_review_event.event_id if human_review_event is not None else None
            ),
            "final_generation_llm_call_id": _detail_str(
                details, "final_generation_llm_call_id"
            ),
        },
        "llm_call": _serialize_llm_call_detail(llm_call),
        "qc_report": _serialize_qc_report_detail(qc_report),
        "human_review_event": _serialize_human_review_event_detail(human_review_event),
    }


def _detail_str(details: dict, key: str) -> str | None:
    value = details.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _resolve_attempt_llm_call(
    item: AttemptTracker,
    details: dict,
    lookups: dict[str, dict[str, Any]],
) -> LlmCall | None:
    llm_calls = lookups["llm_calls"]
    for llm_call_id in (
        _detail_str(details, "llm_call_id"),
        _detail_str(details, "final_generation_llm_call_id"),
    ):
        if not llm_call_id:
            continue
        llm_call = llm_calls.get(llm_call_id)
        if llm_call is not None and (
            item.scene_id is None or llm_call.scene_id == item.scene_id
        ):
            return llm_call

    for row_key in ("row_id", "source_draft_row_id", "source_style_draft_row_id"):
        row_id = _detail_str(details, row_key)
        if not row_id:
            continue
        draft = lookups["drafts"].get(row_id)
        if draft is None or (
            item.scene_id is not None and draft.scene_id != item.scene_id
        ):
            continue
        if draft.generation_llm_call_id:
            llm_call = llm_calls.get(draft.generation_llm_call_id)
            if llm_call is not None:
                return llm_call

    final_scene_row_id = _detail_str(details, "final_scene_row_id")
    if final_scene_row_id:
        final_scene = lookups["final_scenes"].get(final_scene_row_id)
        if final_scene is not None and (
            item.scene_id is None or final_scene.scene_id == item.scene_id
        ):
            if final_scene.generation_llm_call_id:
                llm_call = llm_calls.get(final_scene.generation_llm_call_id)
                if llm_call is not None:
                    return llm_call

    return None


def _resolve_scene_scoped_qc_report(
    qc_reports: dict[str, Any],
    scene_id: str | None,
    qc_report_id: str | None,
) -> QcReport | None:
    if not qc_report_id:
        return None
    qc_report = qc_reports.get(qc_report_id)
    if qc_report is None:
        return None
    if scene_id is not None and qc_report.scene_id != scene_id:
        return None
    return qc_report


def _resolve_attempt_qc_report(
    item: AttemptTracker,
    details: dict,
    lookups: dict[str, dict[str, Any]],
) -> QcReport | None:
    for qc_report_id in (
        _detail_str(details, "qc_report_id"),
        _detail_str(details, "source_qc_report_id"),
    ):
        qc_report = _resolve_scene_scoped_qc_report(
            lookups["qc_reports"],
            item.scene_id,
            qc_report_id,
        )
        if qc_report is not None:
            return qc_report
    return None


def _resolve_attempt_human_review_event(
    item: AttemptTracker,
    details: dict,
    lookups: dict[str, dict[str, Any]],
) -> HumanReviewEvent | None:
    event_id = _detail_str(details, "human_review_event_id")
    if not event_id:
        return None
    event = lookups["review_events"].get(event_id)
    if event is None:
        return None
    if item.scene_id is not None and event.scene_id != item.scene_id:
        return None
    return event


def _serialize_llm_call_detail(llm_call: LlmCall | None) -> dict | None:
    if llm_call is None:
        return None
    return {
        "llm_call_id": llm_call.llm_call_id,
        "step": _display_generation_step(llm_call.step),
        "raw_step": llm_call.step,
        "provider": llm_call.provider,
        "model": llm_call.model,
        "prompt_hash": llm_call.prompt_hash,
        "request_payload_summary": llm_call.request_payload_summary,
        "response_payload_summary": llm_call.response_payload_summary,
        "prompt_tokens": llm_call.prompt_tokens,
        "completion_tokens": llm_call.completion_tokens,
        "total_tokens": llm_call.total_tokens,
        "latency_ms": llm_call.latency_ms,
        "finish_reason": llm_call.finish_reason,
        "error_code": llm_call.error_code,
        "created_at": llm_call.created_at,
    }


def _serialize_qc_report_detail(report: QcReport | None) -> dict | None:
    if report is None:
        return None
    return {
        "qc_report_id": report.qc_report_id,
        "qc_type": report.qc_type,
        "source_draft_row_id": report.source_draft_row_id,
        "source_bundle_id": report.source_bundle_id,
        "pass_flag": None if report.pass_flag is None else bool(report.pass_flag),
        "resolution_code": report.resolution_code,
        "next_action": report.next_action,
        "issues_json": report.issues_json or [],
        "rewrite_brief_json": report.rewrite_brief_json or [],
        "issue_keys": _extract_issue_keys(report.issues_json or []),
        "rewrite_brief": _extract_rewrite_brief(report.rewrite_brief_json or []),
        "created_at": report.created_at,
    }


def _serialize_human_review_event_detail(event: HumanReviewEvent | None) -> dict | None:
    if event is None:
        return None
    return {
        "event_id": event.event_id,
        "status": event.status,
        "event_source": event.event_source,
        "priority": event.priority,
        "owner": event.owner,
        "object_ref": event.object_ref,
        "allowed_actions_json": event.allowed_actions_json or [],
        "result_status_map_json": event.result_status_map_json or {},
        "default_action": event.default_action,
        "details_json": dict(event.details_json or {}),
        "created_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _serialize_attempt(item: AttemptTracker) -> dict:
    return {
        "attempt_id": item.attempt_id,
        "step": item.step,
        "status": item.status,
        "source_bundle_id": item.source_bundle_id,
        "details_json": item.details_json,
        "created_at": item.created_at,
    }


def _next_scene_seq(session: Session, chapter_id: str) -> int:
    return AuthorLifecycleService(session).next_scene_append_seq(chapter_id)
