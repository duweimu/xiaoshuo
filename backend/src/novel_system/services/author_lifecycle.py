from __future__ import annotations

import re

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterRunJob,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    QcReport,
    ReviewItem,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneMemory,
    SceneRunState,
    StoryProject,
    utcnow,
)
from novel_system.services.chapter_approval import (
    approved_chapter_block,
    is_chapter_approved,
)
from novel_system.services.errors import DomainError
from novel_system.services.vector_store import VectorStore, get_vector_store
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
)

TRASH_BLOCK_REASON_HAS_TRASHED_SCENES = "章节下已有单独移入回收站的场景"
SCENE_RUNTIME_ARTIFACTS_REASON = "场景已有下游运行产物"
CHAPTER_RUNTIME_ARTIFACTS_REASON = "章节下仍有场景存在下游运行产物"
SCENE_CHAPTER_TRASHED_RESTORE_REASON = "请先恢复所属章节，再恢复该场景"
SCENE_CHAPTER_TRASHED_PURGE_REASON = "该场景随章节一起回收，请在章节行中处理"


class AuthorLifecycleService:
    def __init__(
        self,
        session: Session,
        *,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.session = session
        self._vector_store = vector_store

    def require_active_chapter(self, chapter_id: str) -> ChapterGoal:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
        if chapter.trashed_flag == 1:
            raise DomainError("CHAPTER_TRASHED", "chapter is currently in author trash")
        self._require_active_parent_project(chapter.project_id)
        return chapter

    def require_active_scene(self, scene_id: str) -> SceneCard:
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        if scene.trashed_flag == 1:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash")
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is not None and chapter.trashed_flag == 1:
            raise DomainError("SCENE_TRASHED", "scene is currently in author trash")
        if chapter is not None:
            self._require_active_parent_project(chapter.project_id)
        elif scene.project_id:
            self._require_active_parent_project(scene.project_id)
        return scene

    def list_active_chapters(self) -> list[dict]:
        chapters = self.session.execute(
            select(ChapterGoal)
            .outerjoin(
                StoryProject,
                StoryProject.project_id == ChapterGoal.project_id,
            )
            .where(
                ChapterGoal.trashed_flag == 0,
                or_(
                    ChapterGoal.project_id.is_(None),
                    and_(
                        StoryProject.project_id.is_not(None),
                        or_(
                            StoryProject.trashed_flag.is_(None),
                            StoryProject.trashed_flag == 0,
                        ),
                    ),
                ),
            )
            .order_by(ChapterGoal.chapter_id.asc())
        ).scalars().all()
        return [self.serialize_chapter_summary(chapter) for chapter in chapters]

    def _require_active_parent_project(self, project_id: str | None) -> None:
        # Legacy chapter rows may predate project ownership and remain readable.
        if not project_id:
            return
        project = self.session.get(StoryProject, project_id)
        if project is None or project.trashed_flag == 1:
            raise DomainError(
                "PROJECT_TRASHED",
                "chapter or scene belongs to an unavailable project",
                status_code=404,
            )

    def serialize_chapter_summary(self, chapter: ChapterGoal) -> dict:
        chapter_state = self.session.get(ChapterState, chapter.chapter_id)
        active_scene_count = self._count_scenes(chapter.chapter_id, trashed_flag=0)
        trashed_scene_count = self._count_scenes(chapter.chapter_id, trashed_flag=1)
        if is_chapter_approved(self.session, chapter):
            trash_block_reason = "已批准章节须先重新打开，才能移入回收站"
        else:
            trash_block_reason = TRASH_BLOCK_REASON_HAS_TRASHED_SCENES if trashed_scene_count > 0 else None
        return {
            "chapter_id": chapter.chapter_id,
            "planned_scene_count": chapter.planned_scene_count,
            "chapter_goal": chapter.chapter_goal,
            "main_plot_push": chapter.main_plot_push,
            "emotional_target": chapter.emotional_target,
            "ending_effect": chapter.ending_effect,
            "must_not": chapter.must_not,
            "notes": chapter.notes,
            "current_phase": chapter_state.current_phase if chapter_state else "planning",
            "chapter_passed_scene_count": chapter_state.chapter_passed_scene_count if chapter_state else 0,
            "chapter_backfill_pending_count": chapter_state.chapter_backfill_pending_count if chapter_state else 0,
            "active_scene_count": active_scene_count,
            "trashed_scene_count": trashed_scene_count,
            "trash_allowed": 0 if trash_block_reason else 1,
            "trash_block_reason": trash_block_reason,
        }


    def trash_scenes(self, scene_ids: list[str], actor_ref: str) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for scene_id in self._unique_ids(scene_ids):
            scene = self.session.get(SceneCard, scene_id)
            if scene is None:
                blocked.append({"scene_id": scene_id, "code": "SCENE_NOT_FOUND", "message": "scene not found"})
                continue
            if scene.trashed_flag == 1:
                continue
            chapter = self.require_active_chapter(scene.chapter_id)
            approval_block = approved_chapter_block(
                self.session,
                chapter,
                object_id_key="scene_id",
                object_id=scene.scene_id,
                operation="lifecycle.trash_scene",
            )
            if approval_block is not None:
                blocked.append(approval_block)
                continue
            scene.trashed_flag = 1
            scene.trashed_at = self._now()
            scene.trashed_by = actor_ref
            scene.is_chapter_last = 0
            self._normalize_active_last_scene(chapter.chapter_id)
            processed.append({"scene_id": scene.scene_id})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def restore_scenes(self, scene_ids: list[str]) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for scene_id in self._unique_ids(scene_ids):
            scene = self.session.get(SceneCard, scene_id)
            if scene is None:
                blocked.append({"scene_id": scene_id, "code": "SCENE_NOT_FOUND", "message": "scene not found"})
                continue
            if scene.trashed_flag == 0:
                continue
            chapter = self.session.get(ChapterGoal, scene.chapter_id)
            if chapter is None:
                blocked.append({"scene_id": scene_id, "code": "CHAPTER_NOT_FOUND", "message": "chapter not found"})
                continue
            if chapter.trashed_flag == 1:
                blocked.append(
                    {
                        "scene_id": scene_id,
                        "code": "SCENE_RESTORE_BLOCKED_CHAPTER_TRASHED",
                        "message": SCENE_CHAPTER_TRASHED_RESTORE_REASON,
                    }
                )
                continue
            approval_block = approved_chapter_block(
                self.session,
                chapter,
                object_id_key="scene_id",
                object_id=scene.scene_id,
                operation="lifecycle.restore_scene",
            )
            if approval_block is not None:
                blocked.append(approval_block)
                continue
            self._make_room_for_restored_scene(scene)
            scene.trashed_flag = 0
            scene.trashed_at = None
            scene.trashed_by = None
            scene.is_chapter_last = 0
            self._normalize_active_last_scene(scene.chapter_id)
            processed.append({"scene_id": scene.scene_id})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def purge_scenes(self, scene_ids: list[str]) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for scene_id in self._unique_ids(scene_ids):
            scene = self.session.get(SceneCard, scene_id)
            if scene is None:
                blocked.append({"scene_id": scene_id, "code": "SCENE_NOT_FOUND", "message": "scene not found"})
                continue
            if scene.trashed_flag == 0:
                blocked.append({"scene_id": scene_id, "code": "SCENE_NOT_TRASHED", "message": "scene is not in author trash"})
                continue
            chapter = self.session.get(ChapterGoal, scene.chapter_id)
            if chapter is not None and chapter.trashed_flag == 1:
                blocked.append(
                    {
                        "scene_id": scene_id,
                        "code": "SCENE_PURGE_BLOCKED_CHAPTER_TRASHED",
                        "message": SCENE_CHAPTER_TRASHED_PURGE_REASON,
                    }
                )
                continue
            if chapter is not None:
                approval_block = approved_chapter_block(
                    self.session,
                    chapter,
                    object_id_key="scene_id",
                    object_id=scene.scene_id,
                    operation="lifecycle.purge_scene",
                )
                if approval_block is not None:
                    blocked.append(approval_block)
                    continue
            reason = self.scene_purge_block_reason(scene)
            if reason is not None:
                blocked.append(
                    {
                        "scene_id": scene_id,
                        "code": "SCENE_PURGE_BLOCKED_RUNTIME_ARTIFACTS",
                        "message": reason,
                    }
                )
                continue
            self._delete_scene_vectors([scene])
            run_state = self.session.get(SceneRunState, scene.scene_id)
            if run_state is not None:
                self.session.delete(run_state)
                self.session.flush()
            self.session.delete(scene)
            self._normalize_active_last_scene(scene.chapter_id)
            processed.append({"scene_id": scene_id})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def trash_chapters(self, chapter_ids: list[str], actor_ref: str) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for chapter_id in self._unique_ids(chapter_ids):
            chapter = self.session.get(ChapterGoal, chapter_id)
            if chapter is None:
                blocked.append({"chapter_id": chapter_id, "code": "CHAPTER_NOT_FOUND", "message": "chapter not found"})
                continue
            if chapter.trashed_flag == 1:
                continue
            approval_block = approved_chapter_block(
                self.session,
                chapter,
                object_id_key="chapter_id",
                object_id=chapter.chapter_id,
                operation="lifecycle.trash_chapter",
            )
            if approval_block is not None:
                blocked.append(approval_block)
                continue
            if self._count_scenes(chapter_id, trashed_flag=1) > 0:
                blocked.append(
                    {
                        "chapter_id": chapter_id,
                        "code": "CHAPTER_TRASH_BLOCKED_HAS_TRASHED_SCENES",
                        "message": TRASH_BLOCK_REASON_HAS_TRASHED_SCENES,
                    }
                )
                continue
            chapter.trashed_flag = 1
            chapter.trashed_at = self._now()
            chapter.trashed_by = actor_ref
            scene_ids: list[str] = []
            for scene in self._chapter_scenes(chapter_id, trashed_flag=0):
                scene.trashed_flag = 1
                scene.trashed_at = chapter.trashed_at
                scene.trashed_by = actor_ref
                scene_ids.append(scene.scene_id)
            processed.append({"chapter_id": chapter_id, "scene_ids": scene_ids})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def restore_chapters(self, chapter_ids: list[str]) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for chapter_id in self._unique_ids(chapter_ids):
            chapter = self.session.get(ChapterGoal, chapter_id)
            if chapter is None:
                blocked.append({"chapter_id": chapter_id, "code": "CHAPTER_NOT_FOUND", "message": "chapter not found"})
                continue
            if chapter.trashed_flag == 0:
                continue
            approval_block = approved_chapter_block(
                self.session,
                chapter,
                object_id_key="chapter_id",
                object_id=chapter.chapter_id,
                operation="lifecycle.restore_chapter",
            )
            if approval_block is not None:
                blocked.append(approval_block)
                continue
            self._make_room_for_restored_chapter(chapter)
            chapter.trashed_flag = 0
            chapter.trashed_at = None
            chapter.trashed_by = None
            scene_ids: list[str] = []
            for scene in self._chapter_scenes(chapter_id, trashed_flag=1):
                scene.trashed_flag = 0
                scene.trashed_at = None
                scene.trashed_by = None
                scene_ids.append(scene.scene_id)
            self._normalize_active_last_scene(chapter_id)
            processed.append({"chapter_id": chapter_id, "scene_ids": scene_ids})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def purge_chapters(self, chapter_ids: list[str]) -> dict:
        processed: list[dict] = []
        blocked: list[dict] = []
        for chapter_id in self._unique_ids(chapter_ids):
            chapter = self.session.get(ChapterGoal, chapter_id)
            if chapter is None:
                blocked.append({"chapter_id": chapter_id, "code": "CHAPTER_NOT_FOUND", "message": "chapter not found"})
                continue
            if chapter.trashed_flag == 0:
                blocked.append({"chapter_id": chapter_id, "code": "CHAPTER_NOT_TRASHED", "message": "chapter is not in author trash"})
                continue
            approval_block = approved_chapter_block(
                self.session,
                chapter,
                object_id_key="chapter_id",
                object_id=chapter.chapter_id,
                operation="lifecycle.purge_chapter",
            )
            if approval_block is not None:
                blocked.append(approval_block)
                continue
            reason = self.chapter_purge_block_reason(chapter)
            if reason is not None:
                blocked.append(
                    {
                        "chapter_id": chapter_id,
                        "code": "CHAPTER_PURGE_BLOCKED_RUNTIME_ARTIFACTS",
                        "message": reason,
                    }
                )
                continue
            scene_ids = [scene.scene_id for scene in self._chapter_scenes(chapter_id, trashed_flag=1)]
            scenes = [
                scene
                for scene_id in scene_ids
                if (scene := self.session.get(SceneCard, scene_id)) is not None
            ]
            self._delete_scene_vectors(scenes)
            for scene_id in scene_ids:
                state = self.session.get(SceneRunState, scene_id)
                if state is not None:
                    self.session.delete(state)
            self.session.flush()
            for scene_id in scene_ids:
                scene = self.session.get(SceneCard, scene_id)
                if scene is not None:
                    self.session.delete(scene)
            self.session.flush()
            chapter_state = self.session.get(ChapterState, chapter_id)
            if chapter_state is not None:
                self.session.delete(chapter_state)
                self.session.flush()
            self.session.delete(chapter)
            processed.append({"chapter_id": chapter_id, "scene_ids": scene_ids})
        self.session.flush()
        return {"processed": processed, "blocked": blocked}

    def serialize_chapter(self, chapter: ChapterGoal) -> dict:
        return {
            "chapter_id": chapter.chapter_id,
            "planned_scene_count": chapter.planned_scene_count,
            "mid_aggregate_enabled": chapter.mid_aggregate_enabled,
            "chapter_goal": chapter.chapter_goal,
            "main_plot_push": chapter.main_plot_push,
            "emotional_target": chapter.emotional_target,
            "ending_effect": chapter.ending_effect,
            "must_not": chapter.must_not,
            "notes": chapter.notes,
            "writer_brief_json": normalize_chapter_writer_brief(chapter.writer_brief_json),
        }

    def serialize_chapter_state(self, chapter_state: ChapterState | None, chapter_id: str) -> dict:
        if chapter_state is None:
            return {
                "chapter_id": chapter_id,
                "current_phase": "planning",
                "chapter_passed_scene_count": 0,
                "chapter_backfill_pending_count": 0,
            }
        return {
            "chapter_id": chapter_state.chapter_id,
            "current_phase": chapter_state.current_phase,
            "chapter_passed_scene_count": chapter_state.chapter_passed_scene_count,
            "chapter_backfill_pending_count": chapter_state.chapter_backfill_pending_count,
        }

    def serialize_author_scene(self, scene: SceneCard, scene_state: SceneRunState | None) -> dict:
        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "scene_seq": scene.scene_seq,
            "pov_character_id": scene.pov_character_id,
            "onstage_chars_json": scene.onstage_chars_json,
            "resolved_relation_id": scene.resolved_relation_id,
            "location": scene.location,
            "scene_goal": scene.scene_goal,
            "beats_json": scene.beats_json,
            "must_include_text": scene.must_include_text,
            "forbidden_text": scene.forbidden_text,
            "exit_change": scene.exit_change,
            "hook": scene.hook,
            "writer_brief_json": normalize_scene_writer_brief(scene.writer_brief_json),
            "target_length_band": scene.target_length_band,
            "scene_type": scene.scene_type,
            "is_chapter_last": scene.is_chapter_last,
            "scene_status": scene_state.scene_status if scene_state else "ready",
            "current_bundle_id": scene_state.current_bundle_id if scene_state else None,
            "current_final_scene_row_id": scene_state.current_final_scene_row_id if scene_state else None,
        }

    def serialize_trashed_chapter(self, chapter: ChapterGoal) -> dict:
        return {
            "chapter_id": chapter.chapter_id,
            "chapter_goal": chapter.chapter_goal,
            "trashed_at": chapter.trashed_at,
            "trashed_by": chapter.trashed_by,
            "scene_count": self._count_scenes(chapter.chapter_id, trashed_flag=1),
            "restore_allowed": 1,
            "restore_block_reason": None,
            "purge_allowed": 0 if self.chapter_purge_block_reason(chapter) else 1,
            "purge_block_reason": self.chapter_purge_block_reason(chapter),
        }

    def serialize_trashed_scene(self, scene: SceneCard) -> dict:
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        chapter_trashed = 1 if chapter is not None and chapter.trashed_flag == 1 else 0
        restore_block_reason = SCENE_CHAPTER_TRASHED_RESTORE_REASON if chapter_trashed else None
        if chapter_trashed:
            purge_block_reason = SCENE_CHAPTER_TRASHED_PURGE_REASON
        else:
            purge_block_reason = self.scene_purge_block_reason(scene)
        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "scene_seq": scene.scene_seq,
            "scene_goal": scene.scene_goal,
            "trashed_at": scene.trashed_at,
            "trashed_by": scene.trashed_by,
            "chapter_trashed": chapter_trashed,
            "restore_allowed": 0 if restore_block_reason else 1,
            "restore_block_reason": restore_block_reason,
            "purge_allowed": 0 if purge_block_reason else 1,
            "purge_block_reason": purge_block_reason,
        }

    def scene_purge_block_reason(self, scene: SceneCard) -> str | None:
        state = self.session.get(SceneRunState, scene.scene_id)
        if state is not None:
            if any(
                [
                    state.current_bundle_id,
                    state.current_bundle_hash,
                    state.current_neutral_draft_row_id,
                    state.current_style_draft_row_id,
                    state.current_final_scene_row_id,
                    state.current_human_review_event_id,
                    state.current_qc_report_id,
                    state.bundle_build_count > 0,
                    state.hard_partial_rewrite_count > 0,
                    state.hard_full_rewrite_count > 0,
                    state.soft_patch_count > 0,
                    state.total_attempt_count > 0,
                    state.repeat_issue_key,
                    state.repeat_issue_count > 0,
                ]
            ):
                return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(SceneBundle.bundle_id).where(SceneBundle.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(SceneBlueprint.row_id).where(SceneBlueprint.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(
            select(SceneExecutionContract.contract_id).where(
                SceneExecutionContract.scene_id == scene.scene_id
            )
        ):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(SceneDraft.row_id).where(SceneDraft.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(FinalScene.row_id).where(FinalScene.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(QcReport.qc_report_id).where(QcReport.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(SceneMemory.row_id).where(SceneMemory.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(AttemptTracker.attempt_id).where(AttemptTracker.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(ReviewItem.review_id).where(ReviewItem.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(HumanReviewEvent.event_id).where(HumanReviewEvent.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(ChapterRunJob.job_id).where(ChapterRunJob.scene_id == scene.scene_id)):
            return SCENE_RUNTIME_ARTIFACTS_REASON
        return None

    def _delete_scene_vectors(self, scenes: list[SceneCard]) -> None:
        if not scenes:
            return
        by_project: dict[str, list[str]] = {}
        for scene in scenes:
            project_id = scene.project_id
            if not project_id:
                chapter = self.session.get(ChapterGoal, scene.chapter_id)
                project_id = chapter.project_id if chapter is not None else None
            # Pre-project legacy fixtures used opaque chapter IDs such as
            # ``CH630`` and indexed them under that exact collection suffix.
            # This cleanup-only compatibility path is safe because it never
            # infers a prefix from a structured ``*_CH_*`` identifier.
            if not project_id and "_" not in scene.chapter_id:
                project_id = scene.chapter_id
            if not project_id:
                raise DomainError(
                    "PROJECT_OWNERSHIP_UNRESOLVED",
                    "cannot permanently delete scene vectors without authoritative project ownership",
                    status_code=409,
                    details={
                        "scene_id": scene.scene_id,
                        "chapter_id": scene.chapter_id,
                    },
                )
            by_project.setdefault(project_id, []).append(scene.scene_id)

        store = self._vector_store or get_vector_store()
        for project_id, scene_ids in by_project.items():
            collection_name = f"scenes_{project_id}"
            try:
                store.delete_documents(collection_name, scene_ids)
            except DomainError:
                raise
            except Exception as exc:
                raise DomainError(
                    "SCENE_VECTOR_PURGE_FAILED",
                    "scene vector documents could not be permanently deleted",
                    status_code=503,
                    details={
                        "project_id": project_id,
                        "scene_ids": scene_ids,
                        "collection_name": collection_name,
                        "retryable": True,
                    },
                ) from exc

    def chapter_purge_block_reason(self, chapter: ChapterGoal) -> str | None:
        if self._has_rows(
            select(ChapterRunJob.job_id).where(
                ChapterRunJob.chapter_id == chapter.chapter_id
            )
        ):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(ChapterMemory.row_id).where(ChapterMemory.chapter_id == chapter.chapter_id)):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(select(ChapterRollingNote.row_id).where(ChapterRollingNote.chapter_id == chapter.chapter_id)):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(
            select(ReviewItem.review_id).where(ReviewItem.chapter_id == chapter.chapter_id, ReviewItem.scene_id.is_(None))
        ):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(
            select(HumanReviewEvent.event_id).where(HumanReviewEvent.chapter_id == chapter.chapter_id, HumanReviewEvent.scene_id.is_(None))
        ):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(
            select(AttemptTracker.attempt_id).where(
                AttemptTracker.chapter_id == chapter.chapter_id,
                AttemptTracker.scene_id.is_(None),
            )
        ):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        if self._has_rows(
            select(QcReport.qc_report_id).where(
                QcReport.chapter_id == chapter.chapter_id,
                QcReport.scene_id.is_(None),
            )
        ):
            return CHAPTER_RUNTIME_ARTIFACTS_REASON
        for scene in self._chapter_scenes(chapter.chapter_id, trashed_flag=1):
            if self.scene_purge_block_reason(scene) is not None:
                return CHAPTER_RUNTIME_ARTIFACTS_REASON
        return None

    def _normalize_active_last_scene(self, chapter_id: str) -> None:
        active_scenes = self._chapter_scenes(chapter_id, trashed_flag=0)
        for scene in active_scenes:
            scene.is_chapter_last = 0
        if active_scenes:
            active_scenes[-1].is_chapter_last = 1

    def _chapter_scenes(self, chapter_id: str, *, trashed_flag: int | None = None) -> list[SceneCard]:
        statement = select(SceneCard).where(SceneCard.chapter_id == chapter_id)
        if trashed_flag is not None:
            statement = statement.where(SceneCard.trashed_flag == trashed_flag)
        statement = statement.order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        return self.session.execute(statement).scalars().all()

    def _count_scenes(self, chapter_id: str, *, trashed_flag: int) -> int:
        return len(self._chapter_scenes(chapter_id, trashed_flag=trashed_flag))

    def _next_active_scene_seq(self, chapter_id: str) -> int:
        active_scenes = self._chapter_scenes(chapter_id, trashed_flag=0)
        if not active_scenes:
            return 1
        return max(scene.scene_seq for scene in active_scenes) + 1

    def _make_room_for_restored_scene(self, restored: SceneCard) -> None:
        """Preserve the scene's original position, shifting active collisions right."""

        desired_seq = max(1, int(restored.scene_seq or 1))
        active_scenes = [
            scene
            for scene in self._chapter_scenes(restored.chapter_id, trashed_flag=0)
            if scene.scene_id != restored.scene_id and int(scene.scene_seq or 0) >= desired_seq
        ]
        final_positions = {
            scene.scene_id: int(scene.scene_seq or 0) + 1
            for scene in active_scenes
        }
        self._park_scene_orders(active_scenes)
        for scene in active_scenes:
            scene.scene_seq = final_positions[scene.scene_id]
        if active_scenes:
            self.session.flush()
        restored.scene_seq = desired_seq

    def _make_room_for_restored_chapter(self, restored: ChapterGoal) -> None:
        if restored.project_id is None or restored.display_order is None:
            return
        desired_order = max(0, int(restored.display_order))
        active_chapters = list(
            self.session.execute(
                select(ChapterGoal)
                .where(
                    ChapterGoal.project_id == restored.project_id,
                    ChapterGoal.trashed_flag == 0,
                    ChapterGoal.chapter_id != restored.chapter_id,
                    ChapterGoal.display_order.is_not(None),
                    ChapterGoal.display_order >= desired_order,
                )
                .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc())
            ).scalars().all()
        )
        final_positions = {
            chapter.chapter_id: int(chapter.display_order or 0) + 1
            for chapter in active_chapters
        }
        if active_chapters:
            temporary_start = max(
                int(chapter.display_order or 0) for chapter in active_chapters
            ) + 1
            for offset, chapter in enumerate(active_chapters):
                chapter.display_order = temporary_start + offset
            self.session.flush()
            for chapter in active_chapters:
                chapter.display_order = final_positions[chapter.chapter_id]
            self.session.flush()
        restored.display_order = desired_order

    def _park_scene_orders(self, scenes: list[SceneCard]) -> None:
        if not scenes:
            return
        temporary_start = max(int(scene.scene_seq or 0) for scene in scenes) + 1
        for offset, scene in enumerate(scenes):
            scene.scene_seq = temporary_start + offset
        self.session.flush()

    def next_scene_append_seq(self, chapter_id: str) -> int:
        chapter_scenes = self._chapter_scenes(chapter_id)
        if not chapter_scenes:
            return 1
        return max(scene.scene_seq for scene in chapter_scenes) + 1

    def _last_active_scene(self, chapter_id: str) -> SceneCard | None:
        active_scenes = self._chapter_scenes(chapter_id, trashed_flag=0)
        if not active_scenes:
            return None
        return active_scenes[-1]

    def _suggest_next_scene_id(self, chapter_id: str) -> str:
        pattern = re.compile(rf"^{re.escape(chapter_id)}_SC(\d+)$")
        used_suffixes: set[int] = set()
        suffix_width = 2
        for scene in self._chapter_scenes(chapter_id):
            match = pattern.match(scene.scene_id)
            if match is None:
                continue
            used_suffixes.add(int(match.group(1)))
            suffix_width = max(suffix_width, len(match.group(1)))

        next_suffix = 1
        while next_suffix in used_suffixes:
            next_suffix += 1

        width = max(suffix_width, len(str(next_suffix)), 2)
        return f"{chapter_id}_SC{next_suffix:0{width}d}"

    def _has_rows(self, statement) -> bool:
        return self.session.execute(statement.limit(1)).first() is not None

    def _unique_ids(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value or value in deduped:
                continue
            deduped.append(value)
        return deduped

    def _now(self) -> str:
        return utcnow()
