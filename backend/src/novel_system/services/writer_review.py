from __future__ import annotations

import json
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    RevisionCandidate,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.author_actions import author_action
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_task_runner import (
    SCENE_SPLIT_RECOMMENDATION,
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.writer_briefs import normalize_chapter_writer_brief, normalize_scene_writer_brief

WRITER_RUBRIC_ID = "drama_effectiveness_v1"

# 修订腿失败时挂在聚合评审 findings 上的阻塞项维度。诊断结果照常入库，
# 只是"修订候选不可用 + 原因"作为一条 blocker finding 持久化，GET 与 run 响应
# 都从它派生 revision_blocker，作家刷新页面后原因不会丢。
REVISION_BLOCKER_DIMENSION = "writer_revision_candidate"
REVISION_PAYLOAD_INVALID_CODE = "WRITER_REVISION_PAYLOAD_INVALID"

WRITER_RUBRIC_DIMENSIONS: tuple[str, ...] = (
    "desire",
    "obstacle",
    "stakes",
    "turn",
    "subtext",
    "irreversible_change",
    "scene_necessity",
    "reader_hook",
    "continuity",
)

PROFESSIONAL_WRITER_DIMENSIONS: tuple[str, ...] = (
    "character_agency",
    "dialogue_edge",
    "information_rhythm",
    "imagery_freshness",
    "expression_repetition",
    "power_shift",
    "ending_drive",
)

ALL_WRITER_REVIEW_DIMENSIONS: tuple[str, ...] = WRITER_RUBRIC_DIMENSIONS + PROFESSIONAL_WRITER_DIMENSIONS


def _revision_blocker_from_evaluation(evaluation: WriterEvaluation | None) -> dict[str, Any] | None:
    if evaluation is None:
        return None
    for finding in evaluation.findings_json or []:
        if isinstance(finding, dict) and finding.get("dimension") == REVISION_BLOCKER_DIMENSION:
            blocker = finding.get("revision_blocker")
            if isinstance(blocker, dict):
                return blocker
    return None


def _compact_source_for_prompt(text: str, limit: int = 1600) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit].rstrip()}\n...[truncated for writer review prompt]..."


class WriterReviewService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)


    def scene_summary(self, scene_id: str) -> dict[str, Any]:
        return self._review_payload("scene", scene_id)

    def chapter_summary(self, chapter_id: str) -> dict[str, Any]:
        return self._review_payload("chapter", chapter_id)

    def summaries(self, object_type: str, object_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Load review summaries for one object type in a bounded query set."""

        ordered_ids = list(dict.fromkeys(object_id for object_id in object_ids if object_id))
        if not ordered_ids:
            return {}

        latest_rows = self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id.in_(ordered_ids),
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
            .order_by(
                WriterEvaluation.object_id.asc(),
                WriterEvaluation.created_at.desc(),
                WriterEvaluation.evaluation_id.desc(),
            )
        ).scalars().all()
        latest_by_object: dict[str, WriterEvaluation] = {}
        for row in latest_rows:
            latest_by_object.setdefault(row.object_id, row)

        candidate_rows = self.session.execute(
            select(RevisionCandidate)
            .where(
                RevisionCandidate.object_type == object_type,
                RevisionCandidate.object_id.in_(ordered_ids),
            )
            .order_by(
                RevisionCandidate.object_id.asc(),
                RevisionCandidate.created_at.desc(),
                RevisionCandidate.revision_id.desc(),
            )
        ).scalars().all()
        candidates_by_object: dict[str, list[RevisionCandidate]] = {
            object_id: [] for object_id in ordered_ids
        }
        for row in candidate_rows:
            candidates_by_object.setdefault(row.object_id, []).append(row)

        lens_by_parent: dict[str, list[WriterEvaluation]] = {}
        parent_ids = [row.evaluation_id for row in latest_by_object.values()]
        if parent_ids:
            lens_rows = self.session.execute(
                select(WriterEvaluation)
                .where(WriterEvaluation.parent_evaluation_id.in_(parent_ids))
                .order_by(
                    WriterEvaluation.parent_evaluation_id.asc(),
                    WriterEvaluation.lens.asc(),
                    WriterEvaluation.evaluation_id.asc(),
                )
            ).scalars().all()
            for row in lens_rows:
                if row.parent_evaluation_id:
                    lens_by_parent.setdefault(row.parent_evaluation_id, []).append(row)

        return {
            object_id: self._review_payload_from_rows(
                object_type=object_type,
                object_id=object_id,
                latest=latest_by_object.get(object_id),
                candidates=candidates_by_object.get(object_id, []),
                lens_rows=lens_by_parent.get(
                    latest_by_object[object_id].evaluation_id,
                    [],
                )
                if object_id in latest_by_object
                else [],
            )
            for object_id in ordered_ids
        }

    @staticmethod
    def serialize_evaluation(evaluation: WriterEvaluation | None) -> dict[str, Any] | None:
        if evaluation is None:
            return None
        return {
            "evaluation_id": evaluation.evaluation_id,
            "object_type": evaluation.object_type,
            "object_id": evaluation.object_id,
            "chapter_id": evaluation.chapter_id,
            "scene_id": evaluation.scene_id,
            "rubric_id": evaluation.rubric_id,
            "source_text_ref": evaluation.source_text_ref,
            "source_bundle_id": evaluation.source_bundle_id,
            "evaluator_llm_call_id": evaluation.evaluator_llm_call_id,
            "lens": evaluation.lens or "aggregate",
            "parent_evaluation_id": evaluation.parent_evaluation_id,
            "evidence_spans": evaluation.evidence_spans_json or [],
            "source_blueprint_row_id": evaluation.source_blueprint_row_id,
            "overall_score": evaluation.overall_score,
            "scores": evaluation.scores_json or {},
            "findings": evaluation.findings_json or [],
            "failure_class": evaluation.failure_class,
            "auto_rewrite_eligible": bool(evaluation.auto_rewrite_eligible) if evaluation.auto_rewrite_eligible is not None else None,
            "contract_field_refs": evaluation.contract_field_refs_json or {},
            "promotion_blockers": evaluation.promotion_blockers_json or [],
            "revision_brief": evaluation.revision_brief_json or [],
            "requires_human_review": bool(evaluation.requires_human_review),
            "status": evaluation.status,
            "created_at": evaluation.created_at,
        }

    @staticmethod
    def serialize_revision(revision: RevisionCandidate) -> dict[str, Any]:
        return {
            "revision_id": revision.revision_id,
            "evaluation_id": revision.evaluation_id,
            "object_type": revision.object_type,
            "object_id": revision.object_id,
            "chapter_id": revision.chapter_id,
            "scene_id": revision.scene_id,
            "revision_type": revision.revision_type,
            "source_text_ref": revision.source_text_ref,
            "proposed_text": revision.proposed_text,
            "instruction": revision.instruction_json or [],
            "diff_summary": revision.diff_summary_json or {},
            "patches": revision.patches_json or [],
            "apply_mode": revision.apply_mode or "manual_only",
            "target_text_ref": revision.target_text_ref or revision.source_text_ref,
            "status": revision.status,
            "author_decision_note": revision.author_decision_note,
            "created_by": revision.created_by,
            "created_at": revision.created_at,
            "updated_at": revision.updated_at,
        }

    def _review_payload(self, object_type: str, object_id: str) -> dict[str, Any]:
        return self.summaries(object_type, [object_id])[object_id]

    def _review_payload_from_rows(
        self,
        *,
        object_type: str,
        object_id: str,
        latest: WriterEvaluation | None,
        candidates: list[RevisionCandidate],
        lens_rows: list[WriterEvaluation],
    ) -> dict[str, Any]:
        serialized_latest = self.serialize_evaluation(latest)
        lens_evaluations = [item for item in (self.serialize_evaluation(row) for row in lens_rows) if item]
        return {
            "status": "reviewed" if latest else "not_run",
            "object_type": object_type,
            "object_id": object_id,
            "rubric_id": WRITER_RUBRIC_ID,
            "latest_evaluation": serialized_latest,
            "latest_score": serialized_latest["overall_score"] if serialized_latest else None,
            "requires_human_review": bool(serialized_latest["requires_human_review"]) if serialized_latest else False,
            "lens_evaluations": lens_evaluations,
            "candidate_count": len(candidates),
            "candidates": [self.serialize_revision(candidate) for candidate in candidates],
            "revision_blocker": _revision_blocker_from_evaluation(latest),
        }


    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)


