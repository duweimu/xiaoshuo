from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterRunJob,
    ChapterState,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    IdempotencyKey,
    LlmCall,
    LlmCallAttempt,
    OperationLog,
    OutlinePlan,
    PassagePatchCandidate,
    QcReport,
    RelationProfile,
    ReviewItem,
    RevisionCandidate,
    SnowflakeAssistantTurn,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneMemory,
    SceneRunState,
    SnowflakeArtifact,
    SnowflakeCharacterPlan,
    SnowflakeRevisionLink,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
    SystemConfigSnapshot,
    SystemSecret,
    VoiceProfile,
    WriterEvaluation,
)
from novel_system.db.session import SessionLocal
from novel_system.services.project_ownership import project_owned_models_child_first

PRESERVED_DOMAINS = [
    "ReviewItem / LlmCall 中的历史 reference 审计痕迹",
    "SystemConfigSnapshot / SystemSecret",
    "config/models.yaml and config/prompts.yaml",
]
PRESERVED_REVIEW_SOURCES = {"reference_book_learning", "reference_profile_apply"}
PRESERVED_LLM_NODE_PREFIXES = ("reference_",)


@dataclass(frozen=True)
class ResetTarget:
    key: str
    model: type[Any]
    id_attr: str | None = None
    collect_ids: Callable[[Session], list[Any]] | None = None


@dataclass(frozen=True)
class ResetStep:
    target: ResetTarget
    count: int
    delete_ids: list[Any] | None = None


def collect_reset_summary(session: Session) -> dict[str, Any]:
    planned_counts = {step.target.key: step.count for step in _build_reset_plan(session)}
    return {
        "mode": "dry_run",
        "planned_counts": planned_counts,
        "preserved_domains": list(PRESERVED_DOMAINS),
        "status": "ok",
    }


def execute_reset(session: Session) -> dict[str, Any]:
    deleted_counts: dict[str, int] = {}
    for step in _build_reset_plan(session):
        deleted_counts[step.target.key] = step.count
        if step.count == 0:
            continue
        if step.delete_ids is None:
            session.execute(delete(step.target.model))
            continue
        id_column = getattr(step.target.model, step.target.id_attr or "")
        for chunk in _chunked(step.delete_ids, size=500):
            session.execute(delete(step.target.model).where(id_column.in_(chunk)))
    session.flush()
    return {
        "mode": "execute",
        "deleted_counts": deleted_counts,
        "preserved_domains": list(PRESERVED_DOMAINS),
        "status": "ok",
    }


def _build_reset_plan(session: Session) -> list[ResetStep]:
    steps: list[ResetStep] = []
    for target in _reset_targets():
        if target.collect_ids is None:
            steps.append(ResetStep(target=target, count=_count_rows(session, target.model)))
            continue
        delete_ids = target.collect_ids(session)
        steps.append(ResetStep(target=target, count=len(delete_ids), delete_ids=delete_ids))
    return steps


def _reset_targets() -> list[ResetTarget]:
    targets = [
        ResetTarget("operation_logs", OperationLog),
        ResetTarget("idempotency_keys", IdempotencyKey),
        ResetTarget("human_review_events", HumanReviewEvent),
        ResetTarget("review_items", ReviewItem, id_attr="review_id", collect_ids=_review_item_ids_to_delete),
        ResetTarget("chapter_run_jobs", ChapterRunJob),
        ResetTarget("attempt_tracker", AttemptTracker),
        ResetTarget(
            "llm_call_attempts",
            LlmCallAttempt,
            id_attr="attempt_id",
            collect_ids=_llm_call_attempt_ids_to_delete,
        ),
        ResetTarget("llm_calls", LlmCall, id_attr="llm_call_id", collect_ids=_llm_call_ids_to_delete),
        ResetTarget("passage_patch_candidates", PassagePatchCandidate),
        ResetTarget("author_draft_events", AuthorDraftEvent),
        ResetTarget("author_draft_proposals", AuthorDraftProposal),
        ResetTarget("author_drafts", AuthorDraft),
        ResetTarget("revision_candidates", RevisionCandidate),
        ResetTarget("writer_evaluations", WriterEvaluation),
        ResetTarget("qc_reports", QcReport),
        ResetTarget("generation_planning_artifacts", GenerationPlanningArtifact),
        ResetTarget("scene_execution_contracts", SceneExecutionContract),
        ResetTarget("scene_blueprints", SceneBlueprint),
        ResetTarget("scene_bundles", SceneBundle),
        ResetTarget("scene_drafts", SceneDraft),
        ResetTarget("final_scenes", FinalScene),
        ResetTarget("scene_memories", SceneMemory),
        ResetTarget("chapter_memories", ChapterMemory),
        ResetTarget("chapter_rolling_notes", ChapterRollingNote),
        ResetTarget("author_preference_profiles", AuthorPreferenceProfile),
        ResetTarget("voice_profiles", VoiceProfile),
        ResetTarget("relation_profiles", RelationProfile),
        ResetTarget("scene_run_states", SceneRunState),
        ResetTarget("scene_cards", SceneCard),
        ResetTarget("chapter_states", ChapterState),
        ResetTarget("chapter_goals", ChapterGoal),
        ResetTarget("snowflake_assistant_turns", SnowflakeAssistantTurn),
        ResetTarget("snowflake_scene_triage_items", SnowflakeSceneTriageItem),
        ResetTarget("snowflake_revision_links", SnowflakeRevisionLink),
        ResetTarget("snowflake_scene_plans", SnowflakeScenePlan),
        ResetTarget("snowflake_character_plans", SnowflakeCharacterPlan),
        ResetTarget("snowflake_step_runs", SnowflakeStepRun),
        ResetTarget("story_characters", StoryCharacter),
        ResetTarget("snowflake_artifacts", SnowflakeArtifact),
        ResetTarget("outline_plans", OutlinePlan),
        ResetTarget("story_projects", StoryProject),
    ]
    registered_models = {target.model for target in targets}
    project_root = targets.pop()
    targets.extend(
        ResetTarget(model.__table__.name, model)
        for model in project_owned_models_child_first()
        if model not in registered_models
    )
    targets.append(project_root)
    return targets


def _review_item_ids_to_delete(session: Session) -> list[str]:
    review_ids: list[str] = []
    reviews = session.execute(select(ReviewItem)).scalars().all()
    for review in reviews:
        payload = dict(review.candidate_payload_json or {})
        source = str(payload.get("source") or "").strip()
        if source in PRESERVED_REVIEW_SOURCES:
            continue
        review_ids.append(review.review_id)
    return sorted(review_ids)


def _llm_call_ids_to_delete(session: Session) -> list[str]:
    preserved_call_ids = _preserved_llm_call_ids(session)
    return sorted(
        call.llm_call_id
        for call in session.execute(select(LlmCall)).scalars().all()
        if call.llm_call_id not in preserved_call_ids
    )


def _llm_call_attempt_ids_to_delete(session: Session) -> list[str]:
    preserved_call_ids = _preserved_llm_call_ids(session)
    return sorted(
        attempt.attempt_id
        for attempt in session.execute(select(LlmCallAttempt)).scalars().all()
        if attempt.llm_call_id not in preserved_call_ids
    )


def _preserved_llm_call_ids(session: Session) -> set[str]:
    call_ids: list[str] = []
    calls = session.execute(select(LlmCall)).scalars().all()
    for call in calls:
        node_id = str(call.node_id or "").strip()
        if any(node_id.startswith(prefix) for prefix in PRESERVED_LLM_NODE_PREFIXES):
            call_ids.append(call.llm_call_id)
    return set(call_ids)


def _count_rows(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _chunked(items: list[Any], *, size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="perform the destructive reset")
    parser.add_argument("--yes", action="store_true", help="confirm the destructive reset")
    args = parser.parse_args(argv)

    with SessionLocal() as session:
        if args.execute and not args.yes:
            summary = collect_reset_summary(session)
            summary["status"] = "confirmation_required"
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return

        if args.execute:
            summary = execute_reset(session)
            session.commit()
        else:
            summary = collect_reset_summary(session)
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
