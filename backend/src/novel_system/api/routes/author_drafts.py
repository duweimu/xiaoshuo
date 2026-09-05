from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest
from novel_system.api.response import ok
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.canonical_manuscripts import CanonicalSceneService

router = APIRouter(tags=["author-drafts"])

INT64_MAX = (1 << 63) - 1
MAX_DRAFT_CONTENT_CHARS = 2_000_000
MAX_INSTRUCTION_CHARS = 8_000
MAX_NOTE_CHARS = 4_000
MAX_WARNING_CODES = 64

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
OptionalIdentifier = Annotated[str, Field(max_length=255)]
NoteText = Annotated[str, Field(max_length=MAX_NOTE_CHARS)]
WarningCode = Annotated[str, Field(min_length=1, max_length=128)]


class StrictAuthorDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AuthorDraftSaveRequest(StrictAuthorDraftRequest):
    content: str = Field(max_length=MAX_DRAFT_CONTENT_CHARS)
    base_revision_no: int = Field(ge=1, le=INT64_MAX)
    patch_id: OptionalIdentifier | None = None
    revision_id: OptionalIdentifier | None = None
    option_id: OptionalIdentifier | None = None
    note: NoteText | None = None


class CanonicalPromotionRequest(StrictAuthorDraftRequest):
    # Keep these optional at the transport boundary so the domain service can
    # apply the fail-closed ``requires_reconcile`` default.
    base_revision_no: int | None = Field(default=None, ge=1, le=INT64_MAX)
    expected_current_final_scene_row_id: Identifier | None = None
    narrative_effect: str | None = Field(default=None, max_length=64)
    accepted_warning_codes: list[WarningCode] = Field(
        default_factory=list,
        max_length=MAX_WARNING_CODES,
    )


class ProposalTargetRangeRequest(StrictAuthorDraftRequest):
    unit: str | None = Field(default=None, max_length=32)
    start: int | None = Field(default=None, ge=0, le=MAX_DRAFT_CONTENT_CHARS)
    end: int | None = Field(default=None, ge=0, le=MAX_DRAFT_CONTENT_CHARS)
    source_excerpt: str | None = Field(default=None, max_length=100_000)
    before_text: str | None = Field(default=None, max_length=100_000)
    excerpt: str | None = Field(default=None, max_length=100_000)


class ProposalGenerateRequest(StrictAuthorDraftRequest):
    proposal_type: OptionalIdentifier | None = None
    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION_CHARS)
    target_range: ProposalTargetRangeRequest | None = None
    replacement_text: str | None = Field(default=None, max_length=MAX_DRAFT_CONTENT_CHARS)
    proposal_kind: OptionalIdentifier | None = None
    source_evaluation_id: OptionalIdentifier | None = None
    proposal_source: OptionalIdentifier | None = None


class ProposalGenerateSetRequest(StrictAuthorDraftRequest):
    mode: str | None = Field(default=None, max_length=64)
    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION_CHARS)
    target_range: ProposalTargetRangeRequest | None = None
    source_evaluation_id: OptionalIdentifier | None = None


class ScopedProposalApplyRequest(StrictAuthorDraftRequest):
    proposal_id: OptionalIdentifier | None = None
    apply_mode: str | None = Field(default=None, max_length=64)
    note: NoteText | None = None
    decision_reason: NoteText | None = None


class ProposalApplyRequest(StrictAuthorDraftRequest):
    apply_mode: str | None = Field(default=None, max_length=64)
    note: NoteText | None = None
    decision_reason: NoteText | None = None
    affected_excerpt: str | None = Field(default=None, max_length=100_000)


class ProposalRejectRequest(StrictAuthorDraftRequest):
    note: NoteText | None = None
    decision_reason: NoteText | None = None
    rejected_ai_trace: str | None = Field(default=None, max_length=100_000)


@router.get("/api/v1/author-drafts/{object_type}/{object_id}/current")
def get_current_author_draft(object_type: str, object_id: str, request: Request, session: Session = Depends(get_session)):
    payload = AuthorDraftService(session).current(object_type, object_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/author-drafts/{object_type}/{object_id}/ensure")
def ensure_author_draft(
    object_type: str,
    object_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{object_type}/{object_id}/ensure",
        payload={"object_type": object_type, "object_id": object_id},
        action=lambda: AuthorDraftService(session).ensure(object_type, object_id, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-drafts/{object_type}/{object_id}/ensure-blank")
def ensure_blank_author_draft(
    object_type: str,
    object_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{object_type}/{object_id}/ensure-blank",
        payload={"object_type": object_type, "object_id": object_id},
        action=lambda: AuthorDraftService(session).ensure_blank(object_type, object_id, actor_ref=actor_ref),
    )


@router.patch("/api/v1/author-drafts/{draft_id}")
def save_author_draft(
    draft_id: str,
    payload: AuthorDraftSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/author-drafts/{draft_id}",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).save(draft_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-drafts/{draft_id}/promote-canonical")
def promote_author_draft_canonical(
    draft_id: str,
    request: Request,
    payload: CanonicalPromotionRequest | None = None,
    session: Session = Depends(get_session),
):
    """Promote one saved scene AuthorDraft revision into canonical FinalScene.

    ``requires_reconcile`` publishes the exact author revision while keeping its
    canon ledger pending. ``facts_unchanged`` is accepted only when the previous
    final already has a complete, hash-matched canon commit.
    """

    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/promote-canonical",
        payload={"draft_id": draft_id, **body},
        action=lambda: CanonicalSceneService(session).promote_author_draft(
            draft_id,
            body,
            actor_ref=actor_ref,
        ),
    )


@router.get("/api/v1/author-drafts/{draft_id}/revisions")
def list_author_draft_revisions(draft_id: str, request: Request, session: Session = Depends(get_session)):
    result = AuthorDraftService(session).revisions(draft_id)
    return ok(result, req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/author-drafts/{draft_id}/revisions/{revision_no}")
def get_author_draft_revision(draft_id: str, revision_no: int, request: Request, session: Session = Depends(get_session)):
    result = AuthorDraftService(session).revision(draft_id, revision_no)
    return ok(result, req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/author-drafts/{draft_id}/proposals")
def get_author_draft_proposals(draft_id: str, request: Request, session: Session = Depends(get_session)):
    result = AuthorDraftService(session).proposals(draft_id)
    return ok(result, req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/author-drafts/{draft_id}/proposals/{proposal_id}/diff")
def get_author_draft_proposal_diff(
    draft_id: str,
    proposal_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    result = AuthorDraftService(session).proposal_diff(draft_id, proposal_id)
    return ok(result, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/author-drafts/{draft_id}/apply-proposal")
def apply_author_draft_scoped_proposal(
    draft_id: str,
    request: Request,
    payload: ScopedProposalApplyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/apply-proposal",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).apply_proposal_to_draft(draft_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-drafts/{draft_id}/proposals/generate")
def generate_author_draft_proposal(
    draft_id: str,
    request: Request,
    payload: ProposalGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/proposals/generate",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).generate_proposal(draft_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-drafts/{draft_id}/proposals/generate-set")
def generate_author_draft_proposal_set(
    draft_id: str,
    request: Request,
    payload: ProposalGenerateSetRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-drafts/{draft_id}/proposals/generate-set",
        payload={"draft_id": draft_id, "body": body},
        action=lambda: AuthorDraftService(session).generate_proposal_set(draft_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-draft-proposals/{proposal_id}/apply")
def apply_author_draft_proposal(
    proposal_id: str,
    request: Request,
    payload: ProposalApplyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-draft-proposals/{proposal_id}/apply",
        payload={"proposal_id": proposal_id, "body": body},
        action=lambda: AuthorDraftService(session).apply_proposal(proposal_id, body, actor_ref=actor_ref),
    )


@router.post("/api/v1/author-draft-proposals/{proposal_id}/reject")
def reject_author_draft_proposal(
    proposal_id: str,
    request: Request,
    payload: ProposalRejectRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/author-draft-proposals/{proposal_id}/reject",
        payload={"proposal_id": proposal_id, "body": body},
        action=lambda: AuthorDraftService(session).reject_proposal(proposal_id, body, actor_ref=actor_ref),
    )


