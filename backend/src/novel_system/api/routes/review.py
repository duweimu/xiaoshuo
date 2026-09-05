from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest, StrictRequestModel
from novel_system.api.response import ok
from novel_system.db.models import ReviewItem
from novel_system.services.errors import DomainError
from novel_system.services.pagination import paginate_select, resolve_pagination_request
from novel_system.services.review_cards import ReviewCardService

router = APIRouter(tags=["review"])

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
OptionalIdentifier = Annotated[str, Field(max_length=255)]
CardListItem = Annotated[str, Field(max_length=4000)]

# legacy 候选创建只接受仍有落点的 item_type（知识注册表已退役）。
SUPPORTED_REVIEW_ITEM_TYPES: tuple[str, ...] = ("author_preference_profile",)


class ReviewCardCreateRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    # Values remain domain-validated for REVIEW_CARD_KIND_INVALID.
    kind: str = Field(max_length=64)
    priority: int | None = Field(default=None, ge=1, le=10)
    title: str | None = Field(default=None, max_length=10_000)
    source: str | None = Field(default=None, max_length=255)
    where: str | None = Field(default=None, max_length=1000)
    occurred_at: str | None = Field(default=None, max_length=128)
    detail: str | None = Field(default=None, max_length=100_000)
    preview: str | None = Field(default=None, max_length=100_000)
    checklist: list[CardListItem] | None = Field(default=None, max_length=500)
    options: list[CardListItem] | None = Field(default=None, max_length=500)
    actions: list[BoundedJsonObject] | None = Field(default=None, max_length=100)
    dedupe_key: str | None = Field(default=None, max_length=512)

class ReviewCandidateCreateRequest(StrictRequestModel):
    # review_id remains optional so the established REVIEW_ID_REQUIRED domain
    # response is retained for an omitted identifier.
    review_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    item_type: str = Field(min_length=1, max_length=128)
    candidate_text: str = Field(max_length=2_000_000)
    candidate_payload_json: BoundedJsonObject = Field(default_factory=dict)
    active_on_approve: int = Field(default=1, ge=0, le=1)

class ReviewCardResolveRequest(StrictRequestModel):
    action_index: int | None = Field(default=None, ge=0, le=10_000)
    project_id: OptionalIdentifier | None = None

class ReviewCardProjectRequest(StrictRequestModel):
    project_id: OptionalIdentifier | None = None

@router.get("/api/v1/review-items")
def list_review_items(
    request: Request,
    session: Session = Depends(get_session),
    status: str | None = None,
    item_type: str | None = None,
    target_collection: str | None = None,
    scene_id: str | None = None,
    chapter_id: str | None = None,
    state: str | None = None,
    project_id: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    cursor: str | None = None,
    limit: int | None = None,
):
    # FE-ALIGN P5 卡片模式：?state=open|snoozed&project_id=… → 持久卡 ∪ 派生卡（统一形状）
    if state is not None:
        if not project_id:
            raise DomainError("REVIEW_PROJECT_REQUIRED", "project_id is required with state filter", status_code=400)
        result = ReviewCardService(session).list_cards(project_id, state=state)
        return ok(result, req_id=getattr(request.state, "request_id", None))
    query = select(ReviewItem)
    if status:
        query = query.where(ReviewItem.status == status)
    if item_type:
        query = query.where(ReviewItem.item_type == item_type)
    if target_collection:
        query = query.where(ReviewItem.target_collection == target_collection)
    if scene_id:
        query = query.where(ReviewItem.scene_id == scene_id)
    if chapter_id:
        query = query.where(ReviewItem.chapter_id == chapter_id)
    page_items, pagination = paginate_select(
        session,
        query,
        request=resolve_pagination_request(page=page, page_size=page_size, cursor=cursor, limit=limit),
        order_columns=(
            (ReviewItem.created_at, "desc"),
            (ReviewItem.review_id, "desc"),
        ),
        cursor_values=lambda item: [item.created_at, item.review_id],
    )
    return ok(
        {"items": [_serialize_review(item, session=session) for item in page_items], "pagination": pagination},
        req_id=getattr(request.state, "request_id", None),
    )

@router.get("/api/v1/review-items/badge")
def review_badge(project_id: str, request: Request, session: Session = Depends(get_session)):
    return ok(ReviewCardService(session).badge(project_id), req_id=getattr(request.state, "request_id", None))

@router.get("/api/v1/review-items/{review_id}")
def review_detail(review_id: str, request: Request, session: Session = Depends(get_session)):
    item = session.get(ReviewItem, review_id)
    if item is None:
        raise DomainError("REVIEW_NOT_FOUND", f"review {review_id} not found", status_code=404)
    return ok(_serialize_review(item, session=session), req_id=getattr(request.state, "request_id", None))

@router.post("/api/v1/review-items")
def create_review_item(
    payload: ReviewCardCreateRequest | ReviewCandidateCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    # FE-ALIGN P5：带 kind 的载荷走卡片创建（dedupe_key 去重）；legacy 载荷保持原 upsert 流
    is_card = "kind" in body and "review_id" not in body
    action = (
        (lambda: ReviewCardService(session).create_card(body, actor_ref=actor_ref))
        if is_card
        else (lambda: _upsert_review_item(session, body))
    )
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items",
        payload=body,
        action=action,
    )

@router.post("/api/v1/review-items/{review_id}/resolve")
def resolve_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardResolveRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/resolve",
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).resolve(
            review_id,
            action_index=body.get("action_index"),
            project_id=body.get("project_id"),
            actor_ref=actor_ref,
        ),
    )

@router.post("/api/v1/review-items/{review_id}/unresolve")
def unresolve_review_card(
    review_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/unresolve",
        payload={"review_id": review_id},
        action=lambda: ReviewCardService(session).unresolve(review_id),
    )

@router.post("/api/v1/review-items/{review_id}/snooze")
def snooze_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardProjectRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/snooze",
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).snooze(review_id, project_id=body.get("project_id")),
    )

@router.post("/api/v1/review-items/{review_id}/unsnooze")
def unsnooze_review_card(
    review_id: str,
    request: Request,
    payload: ReviewCardProjectRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/review-items/{review_id}/unsnooze",
        payload={"review_id": review_id, **body},
        action=lambda: ReviewCardService(session).unsnooze(review_id, project_id=body.get("project_id")),
    )

def _upsert_review_item(session: Session, payload: dict) -> dict:
    review_id = payload.get("review_id")
    if not isinstance(review_id, str) or not review_id:
        raise DomainError("REVIEW_ID_REQUIRED", "missing review_id", status_code=400)
    item_type = payload.get("item_type")
    if item_type not in SUPPORTED_REVIEW_ITEM_TYPES:
        raise DomainError(
            "REVIEW_ITEM_TYPE_INVALID",
            f"item_type {item_type!r} has no approval target; expected one of {list(SUPPORTED_REVIEW_ITEM_TYPES)}",
            status_code=400,
            details={"item_type": item_type, "supported_item_types": list(SUPPORTED_REVIEW_ITEM_TYPES)},
        )

    item = session.get(ReviewItem, review_id)
    if item is None:
        item = ReviewItem(**payload)
        session.add(item)
    else:
        for key, value in payload.items():
            setattr(item, key, value)
    session.flush()
    session.refresh(item)
    return _serialize_review(item, session=session)

def _serialize_review(item: ReviewItem, *, session: Session | None = None) -> dict:
    return {
        "review_id": item.review_id,
        "scene_id": item.scene_id,
        "chapter_id": item.chapter_id,
        "item_type": item.item_type,
        "target_collection": item.target_collection,
        "status": item.status,
        "candidate_text": item.candidate_text,
        "candidate_payload_json": item.candidate_payload_json,
        "active_on_approve": item.active_on_approve,
        "materialize_status": item.materialize_status,
        "approved_item_row_id": item.approved_item_row_id,
    }
