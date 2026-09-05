from __future__ import annotations

import hashlib
import re
import threading
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    ChapterState,
    FinalScene,
    OperationLog,
    OutlinePlan,
    QcReport,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.style_reference.materialization import MaterializationService
from novel_system.services.style_reference.schemas import (
    BindingScope,
    TaskType,
)
from novel_system.services.system_config import SystemConfigService
from novel_system.services.snowflake_steps import SNOWFLAKE_METHOD_VERSION
from novel_system.settings import get_settings

PROJECT_STATUS_OUTLINE_DRAFT = "outline_draft"
PROJECT_STATUS_OUTLINE_REVIEW = "outline_review"
PROJECT_STATUS_CHAPTER_READY = "chapter_ready"
PROJECT_STATUS_CHAPTER_RUNNING = "chapter_running"
PROJECT_STATUS_CHAPTER_BLOCKED = "chapter_blocked"
PROJECT_STATUS_CHAPTER_FINAL_REVIEW = "chapter_final_review"
PROJECT_STATUS_COMPLETED = "completed"

PLAN_STATUS_PENDING_REVIEW = "pending_review"
PLAN_STATUS_APPROVED = "approved"

REFERENCE_SAFETY_RULES = [
    "参考书只进入抽象风格画像，不复制原文表达。",
    "不得复刻参考书人物、设定、桥段、特殊意象或标志性句式。",
    "运行时只使用节奏、句法、叙事手法、结构技巧和禁复刻规则。",
]


class ProjectService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        outline_text = str(payload.get("outline_text") or "").strip()
        if not outline_text:
            raise DomainError(
                "PROJECT_OUTLINE_REQUIRED", "outline_text is required", status_code=400
            )

        planning_mode = _planning_mode(payload.get("planning_mode"))
        title = str(payload.get("title") or "未命名小说").strip() or "未命名小说"
        project = StoryProject(
            project_id=self._next_project_id(),
            title=title,
            genre=_optional_text(payload.get("genre")),
            target_word_count=_optional_positive_int(payload.get("target_word_count")),
            target_chapter_count=_optional_positive_int(
                payload.get("target_chapter_count")
            ),
            mark=_optional_text(payload.get("mark")) or (title[:1] if title else None),
            accent=_optional_text(payload.get("accent")),
            synopsis_line=_optional_text(payload.get("synopsis_line")),
            words_target_daily=_optional_positive_int(
                payload.get("words_target_daily")
            ),
            outline_text=outline_text,
            planning_mode=planning_mode,
            snowflake_schema_version=(
                SNOWFLAKE_METHOD_VERSION if planning_mode == "snowflake" else None
            ),
            snowflake_workflow_mode=_snowflake_workflow_mode(
                payload.get("snowflake_workflow_mode"),
                default="explore" if planning_mode == "snowflake" else "strict",
            ),
            status=PROJECT_STATUS_OUTLINE_DRAFT,
            approved_chapter_ids_json=[],
        )
        self.session.add(project)
        self.session.flush()
        return {"project": project_payload(project)}

    def list(self) -> dict[str, Any]:
        # FE-ALIGN P4: 软删作品默认不出现在任何列表（回收站统一列表单独供给）
        projects = (
            self.session.execute(
                select(StoryProject)
                .where(
                    (StoryProject.trashed_flag.is_(None))
                    | (StoryProject.trashed_flag == 0)
                )
                .order_by(
                    StoryProject.created_at.desc(), StoryProject.project_id.desc()
                )
            )
            .scalars()
            .all()
        )
        return {"items": [project_summary_payload(project) for project in projects]}

    # FE-ALIGN P2: 作品档案局部更新（PATCH /api/v2/projects/{id}/profile）。
    # 字数/进度类字段是只读派生（writing-stats / 目录 rollup），不在可写清单内。
    PROFILE_PATCHABLE = (
        "title",
        "genre",
        "mark",
        "accent",
        "synopsis_line",
        "target_word_count",
        "target_chapter_count",
        "words_target_daily",
    )

    def update_profile(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.require_project(project_id)
        body = payload or {}
        for key in self.PROFILE_PATCHABLE:
            if key not in body:
                continue
            value = body[key]
            if key == "title":
                title = str(value or "").strip()
                if not title:
                    raise DomainError(
                        "PROJECT_TITLE_REQUIRED",
                        "title must not be empty",
                        status_code=400,
                    )
                project.title = title
            elif key in (
                "target_word_count",
                "target_chapter_count",
                "words_target_daily",
            ):
                setattr(project, key, _optional_positive_int(value))
            else:
                setattr(project, key, _optional_text(value))
        self.session.flush()
        return {"project": project_payload(project)}

    def dashboard(self, project_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        latest_plan = self._latest_plan(project_id)
        chapters = self._chapter_payloads(project_id)
        reference_profile_ids = self._project_reference_profile_ids(project)
        current_chapter = next(
            (
                chapter
                for chapter in chapters
                if chapter["chapter_id"] == project.current_chapter_id
            ),
            None,
        )
        backtrack_items: list[dict[str, Any]] = []
        return {
            "project": project_payload(
                project, reference_profile_ids=reference_profile_ids
            ),
            "latest_plan": outline_plan_payload(latest_plan) if latest_plan else None,
            "chapters": chapters,
            "current_chapter": current_chapter,
            "reference_profiles": self._reference_profile_payloads(project),
            "backtrack_items": backtrack_items,
            "review_packet": ProjectChapterFlowService(self.session).review_packet(
                project, project.current_chapter_id
            ),
            "next_action": self._next_action(
                project, latest_plan, backtrack_items=backtrack_items
            ),
            "runtime": self._runtime_readiness(),
        }


    def approve_outline_plan(self, project_id: str, plan_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        plan = self._require_plan(project_id, plan_id)
        if plan.status == PLAN_STATUS_APPROVED:
            return self._approved_plan_result(project, plan)
        if plan.status != PLAN_STATUS_PENDING_REVIEW:
            raise DomainError(
                "OUTLINE_PLAN_NOT_REVIEWABLE", "outline plan is not pending review"
            )

        chapters = list((plan.plan_json or {}).get("chapters") or [])
        if not chapters:
            raise DomainError(
                "OUTLINE_PLAN_EMPTY", "outline plan has no chapters", status_code=422
            )

        created_chapter_count = 0
        created_scene_count = 0
        for chapter_plan in chapters:
            chapter_id = str(chapter_plan.get("chapter_id") or "").strip()
            if not chapter_id:
                raise DomainError(
                    "OUTLINE_PLAN_INVALID", "chapter_id is required", status_code=422
                )
            chapter = self.session.get(ChapterGoal, chapter_id)
            is_new_chapter = chapter is None
            if chapter is None:
                chapter = ChapterGoal(chapter_id=chapter_id, chapter_goal="")
                self.session.add(chapter)
                created_chapter_count += 1
            elif chapter.project_id and chapter.project_id != project.project_id:
                raise DomainError(
                    "CHAPTER_ALREADY_OWNED", "chapter belongs to another project"
                )

            chapter.project_id = project.project_id
            chapter.outline_plan_id = plan.plan_id
            chapter.planned_scene_count = len(chapter_plan.get("scenes") or [])
            chapter.mid_aggregate_enabled = 0
            chapter.chapter_goal = str(
                chapter_plan.get("chapter_goal")
                or chapter_plan.get("title")
                or chapter_id
            )
            # 目录侧读章名的首选字段是 narrative_json["title"]（catalog.chapter_title）。
            # 雪花物化以前不写它，于是作者在 07 里起的章名到不了目录，用户看到的是章 id
            # 字符串。这里补上 —— 但**只在新建章时播种**：narrative_json / display_order
            # 是目录侧的权威字段（章节编排里能改名、能重排），重新物化不得把作者在那边
            # 的改动冲掉。
            if is_new_chapter:
                narrative = dict(chapter_plan.get("narrative_json") or {})
                if narrative:
                    chapter.narrative_json = {
                        **dict(chapter.narrative_json or {}),
                        **narrative,
                    }
                display_order = chapter_plan.get("display_order")
                if display_order is not None:
                    chapter.display_order = int(display_order)
            chapter.main_plot_push = _optional_text(chapter_plan.get("main_plot_push"))
            chapter.emotional_target = _optional_text(
                chapter_plan.get("emotional_target")
            )
            chapter.ending_effect = _optional_text(chapter_plan.get("ending_effect"))
            chapter.must_not = _optional_text(chapter_plan.get("must_not"))
            chapter.notes = _optional_text(chapter_plan.get("notes"))
            chapter.writer_brief_json = {
                "source": (plan.plan_json or {}).get("source")
                or "project_outline_plan",
                "project_id": project.project_id,
                "outline_plan_id": plan.plan_id,
                "chapter_title": chapter_plan.get("title"),
                "reference_safety": list(
                    (plan.plan_json or {}).get("reference_safety")
                    or REFERENCE_SAFETY_RULES
                ),
                **dict(chapter_plan.get("writer_brief_json") or {}),
            }
            # ChapterState/SceneCard both carry immediate SQLite FKs to this row.
            self.session.flush()

            state = self.session.get(ChapterState, chapter.chapter_id)
            if state is None:
                self.session.add(
                    ChapterState(
                        chapter_id=chapter.chapter_id,
                        current_phase="drafting",
                        mid_aggregate_enabled_effective=0,
                        aggregate_block_reason="none",
                    )
                )

            scenes = list(chapter_plan.get("scenes") or [])
            for index, scene_plan in enumerate(scenes, start=1):
                scene_id = str(
                    scene_plan.get("scene_id") or f"{chapter_id}_SC{index:02d}"
                ).strip()
                scene = self.session.get(SceneCard, scene_id)
                if scene is None:
                    scene = SceneCard(
                        scene_id=scene_id,
                        chapter_id=chapter_id,
                        scene_seq=index,
                        scene_goal="",
                    )
                    self.session.add(scene)
                    created_scene_count += 1
                elif scene.project_id and scene.project_id != project.project_id:
                    raise DomainError(
                        "SCENE_ALREADY_OWNED", "scene belongs to another project"
                    )

                scene.chapter_id = chapter_id
                scene.project_id = project.project_id
                scene.outline_plan_id = plan.plan_id
                scene.scene_seq = int(scene_plan.get("scene_seq") or index)
                scene.pov_character_id = _optional_text(
                    scene_plan.get("pov_character_id")
                )
                scene.onstage_chars_json = _string_list(
                    scene_plan.get("onstage_chars_json")
                )
                scene.location = _optional_text(scene_plan.get("location"))
                scene.scene_goal = str(
                    scene_plan.get("scene_goal") or chapter.chapter_goal
                )
                scene.beats_json = _string_list(scene_plan.get("beats_json")) or [
                    scene.scene_goal
                ]
                scene.must_include_text = _optional_text(
                    scene_plan.get("must_include_text")
                )
                scene.forbidden_text = (
                    _optional_text(scene_plan.get("forbidden_text"))
                    or "不得复制参考书原文表达、人物、设定或桥段。"
                )
                scene.exit_change = _optional_text(scene_plan.get("exit_change"))
                scene.hook = _optional_text(scene_plan.get("hook"))
                scene.target_length_band = (
                    _optional_text(scene_plan.get("target_length_band")) or "medium"
                )
                scene.scene_type = (
                    _optional_text(scene_plan.get("scene_type")) or "outline_driven"
                )
                scene.is_chapter_last = 1 if index == len(scenes) else 0
                scene.writer_brief_json = {
                    "source": (plan.plan_json or {}).get("source")
                    or "project_outline_plan",
                    "project_id": project.project_id,
                    "outline_plan_id": plan.plan_id,
                    "reference_safety": list(
                        (plan.plan_json or {}).get("reference_safety")
                        or REFERENCE_SAFETY_RULES
                    ),
                    **dict(scene_plan.get("writer_brief_json") or {}),
                }
                # SceneRunState has an immediate FK to SceneCard and the ORM models
                # intentionally do not declare relationships for dependency ordering.
                self.session.flush()

                if self.session.get(SceneRunState, scene.scene_id) is None:
                    self.session.add(
                        SceneRunState(scene_id=scene.scene_id, scene_status="ready")
                    )

        plan.status = PLAN_STATUS_APPROVED
        plan.approved_at = utcnow()
        project.active_outline_plan_id = plan.plan_id
        project.current_chapter_id = str(chapters[0]["chapter_id"])
        project.status = PROJECT_STATUS_CHAPTER_READY
        self.session.flush()
        return {
            "project": project_payload(project),
            "plan": outline_plan_payload(plan),
            "created_chapter_count": created_chapter_count,
            "created_scene_count": created_scene_count,
        }

    def require_project(self, project_id: str) -> StoryProject:
        project = self.session.get(StoryProject, project_id)
        if project is None:
            raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
        return project

    def _approved_plan_result(
        self, project: StoryProject, plan: OutlinePlan
    ) -> dict[str, Any]:
        chapters = self._chapter_payloads(project.project_id)
        scene_count = sum(len(chapter.get("scenes") or []) for chapter in chapters)
        return {
            "project": project_payload(project),
            "plan": outline_plan_payload(plan),
            "created_chapter_count": len(chapters),
            "created_scene_count": scene_count,
        }

    def _require_plan(self, project_id: str, plan_id: str) -> OutlinePlan:
        plan = self.session.get(OutlinePlan, plan_id)
        if plan is None or plan.project_id != project_id:
            raise DomainError(
                "OUTLINE_PLAN_NOT_FOUND", "outline plan not found", status_code=404
            )
        return plan

    def _latest_plan(self, project_id: str) -> OutlinePlan | None:
        return (
            self.session.execute(
                select(OutlinePlan)
                .where(OutlinePlan.project_id == project_id)
                .order_by(OutlinePlan.version.desc(), OutlinePlan.created_at.desc())
            )
            .scalars()
            .first()
        )

    def _chapter_payloads(self, project_id: str) -> list[dict[str, Any]]:
        chapters = (
            self.session.execute(
                select(ChapterGoal)
                .where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
                .order_by(ChapterGoal.chapter_id.asc())
            )
            .scalars()
            .all()
        )
        return [chapter_payload(self.session, chapter) for chapter in chapters]

    def _reference_profile_payloads(
        self, project: StoryProject
    ) -> list[dict[str, Any]]:
        bound_profiles = self._bound_style_reference_profiles(project.project_id)
        return [
            reference_profile_payload(profile, binding=binding)
            for binding, profile in bound_profiles
        ]

    def _bound_style_reference_profiles(
        self,
        project_id: str,
    ) -> list[tuple[StyleReferenceInjectionBinding, StyleReferenceProfile]]:
        rows = self.session.execute(
            select(StyleReferenceInjectionBinding, StyleReferenceProfile)
            .join(
                StyleReferenceProfile,
                StyleReferenceProfile.profile_id
                == StyleReferenceInjectionBinding.profile_id,
            )
            .where(
                StyleReferenceInjectionBinding.scope == BindingScope.PROJECT.value,
                StyleReferenceInjectionBinding.scope_ref_id == project_id,
                StyleReferenceInjectionBinding.task_type
                == TaskType.SCENE_GENERATION.value,
                StyleReferenceInjectionBinding.status == "active",
            )
            .order_by(
                StyleReferenceInjectionBinding.created_at.desc(),
                StyleReferenceInjectionBinding.binding_id.desc(),
            )
        ).all()
        return [(binding, profile) for binding, profile in rows]

    def _project_reference_profile_ids(self, project: StoryProject) -> list[str]:
        bound_profiles = self._bound_style_reference_profiles(project.project_id)
        return [profile.profile_id for _, profile in bound_profiles]

    def _next_action(
        self,
        project: StoryProject,
        latest_plan: OutlinePlan | None,
        *,
        backtrack_items: list[dict[str, Any]] | None = None,
    ) -> str:
        if any(item.get("status") == "pending" for item in (backtrack_items or [])):
            return "resolve_backtrack_items"
        if project.status == PROJECT_STATUS_COMPLETED:
            return "completed"
        if project.status == PROJECT_STATUS_CHAPTER_FINAL_REVIEW:
            return "approve_chapter_final"
        if project.status == PROJECT_STATUS_CHAPTER_RUNNING:
            return "view_chapter_progress"
        if project.status == PROJECT_STATUS_CHAPTER_READY:
            return "run_current_chapter"
        if project.status == PROJECT_STATUS_CHAPTER_BLOCKED:
            return "resolve_blocker"
        if latest_plan and latest_plan.status == PLAN_STATUS_PENDING_REVIEW:
            return "approve_outline_plan"
        return "generate_outline_plan"

    def _runtime_readiness(self) -> dict[str, Any]:
        settings = get_settings()
        generation_mode = "live" if settings.llm_enabled else "offline_disabled"
        missing_routes: list[str] = []
        provider_ready = False
        try:
            overview = SystemConfigService(self.session).llm_overview()
            readiness = overview.get("readiness") or {}
            provider_ready = bool(
                settings.llm_enabled
                and int(readiness.get("active_provider_count") or 0) > 0
            )
            missing_routes = [
                str(item)
                for item in (overview.get("missing_active_routes") or [])
                if str(item).strip()
            ]
            for item in overview.get("blocked_routes") or []:
                if isinstance(item, dict) and item.get("node_id"):
                    missing_routes.append(str(item["node_id"]))
        except (
            Exception
        ):  # pragma: no cover - readiness should not block dashboard loading
            provider_ready = bool(settings.llm_enabled)
        missing_routes = list(dict.fromkeys(missing_routes))
        next_setup_action = None
        if not settings.llm_enabled or not provider_ready or missing_routes:
            next_setup_action = llm_setup_action(
                llm_enabled=bool(settings.llm_enabled),
                generation_mode=generation_mode,
                missing_routes=[],
            )
        return {
            "llm_enabled": bool(settings.llm_enabled),
            "generation_mode": generation_mode,
            "provider_ready": provider_ready,
            "missing_routes": missing_routes,
            "next_setup_action": next_setup_action,
        }

    def _next_project_id(self) -> str:
        while True:
            project_id = f"PRJ_{uuid.uuid4().hex[:10].upper()}"
            if self.session.get(StoryProject, project_id) is None:
                return project_id


class ProjectChapterFlowService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def run_chapter(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        project = ProjectService(self.session).require_project(project_id)
        self._require_project_chapter(project, chapter_id)
        if project.current_chapter_id != chapter_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_CURRENT",
                "only the current chapter can be run from project dashboard",
            )

        project.status = PROJECT_STATUS_CHAPTER_RUNNING
        self.session.flush()
        run_result = ChapterRunnerService(self.session).run_full(chapter_id)
        if run_result.get("status") == "completed":
            ChapterManuscriptService(self.session).require_complete(chapter_id)
            project.status = PROJECT_STATUS_CHAPTER_FINAL_REVIEW
        elif run_result.get("status") == "blocked":
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
        else:
            project.status = PROJECT_STATUS_CHAPTER_READY
        self.session.flush()
        return {
            "project": project_payload(project),
            "run": run_result,
            "review_packet": self.review_packet(project, chapter_id),
        }

    def prepare_chapter_run_job(
        self,
        project_id: str,
        chapter_id: str,
        *,
        offline_demo: bool = False,
    ) -> dict[str, Any]:
        # 离线演示已退役：offline_demo 保留为受校验的遗留契约字段（非布尔仍拒），
        # 但 True 不再绕过 fail-closed —— LLM 未启用照样拦截。
        if type(offline_demo) is not bool:
            raise DomainError(
                "INVALID_CHAPTER_RUN_MODE",
                "offline_demo must be a boolean",
                status_code=400,
            )
        project = ProjectService(self.session).require_project(project_id)
        self._require_project_chapter(project, chapter_id)
        if project.current_chapter_id != chapter_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_CURRENT",
                "only the current chapter can be run from project dashboard",
            )

        llm_enabled = get_settings().llm_enabled
        if not llm_enabled:
            raise DomainError(
                "LLM_DISABLED_FOR_CHAPTER_RUN",
                "LLM is disabled; enable a live model before starting chapter generation.",
                status_code=409,
                details={
                    "retryable": False,
                    "generation_mode": "offline_disabled",
                    "author_action": llm_setup_action(
                        llm_enabled=False,
                        generation_mode="offline_disabled",
                    ),
                },
            )

        run_payload, should_start_worker = ChapterRunnerService(
            self.session
        ).prepare_full_run(chapter_id)
        if run_payload.get("status") in {"pending", "running"}:
            project.status = PROJECT_STATUS_CHAPTER_RUNNING
            next_action = "view_chapter_progress"
        elif run_payload.get("status") == "completed":
            ChapterManuscriptService(self.session).require_complete(chapter_id)
            project.status = PROJECT_STATUS_CHAPTER_FINAL_REVIEW
            next_action = "approve_chapter_final"
        elif run_payload.get("status") == "blocked":
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
            next_action = "resolve_blocker"
        else:
            project.status = PROJECT_STATUS_CHAPTER_READY
            next_action = "run_current_chapter"
        self.session.flush()
        return {
            "project": project_payload(project),
            "run": run_payload,
            "review_packet": self.review_packet(project, chapter_id),
            "next_action": next_action,
            "_start_worker": should_start_worker
            and run_payload.get("status") == "pending",
        }

    def approve_final(
        self,
        project_id: str,
        chapter_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        body = payload or {}
        project = ProjectService(self.session).require_project(project_id)
        self._require_project_chapter(project, chapter_id)
        if project.current_chapter_id != chapter_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_CURRENT",
                "only the current chapter final can be approved",
            )
        revision_notes = str(body.get("revision_notes") or "").strip()
        if len(revision_notes) > 2000:
            raise DomainError(
                "CHAPTER_APPROVAL_NOTES_TOO_LONG",
                "revision_notes must be 2000 characters or fewer",
                status_code=400,
            )
        ChapterManuscriptService(self.session).require_publishable(chapter_id)
        read_confirmation = self._require_current_read_confirmation(project, chapter_id)

        approved = list(project.approved_chapter_ids_json or [])
        if chapter_id not in approved:
            approved.append(chapter_id)
        project.approved_chapter_ids_json = approved
        chapter = self._require_project_chapter(project, chapter_id)
        chapter.state = "approved"

        next_chapter_id = self._next_chapter_id(project.project_id, chapter_id)
        if next_chapter_id:
            project.current_chapter_id = next_chapter_id
            project.status = PROJECT_STATUS_CHAPTER_READY
        else:
            project.current_chapter_id = None
            project.status = PROJECT_STATUS_COMPLETED
        approval_note = {
            "revision_notes": revision_notes,
            "actor_ref": actor_ref or "operator",
            "body_hash": read_confirmation.get("body_hash"),
            "read_confirmed_at": read_confirmation.get("confirmed_at"),
            "read_confirmed_by": read_confirmation.get("confirmed_by"),
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_approval",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json={
                    "project_id": project.project_id,
                    "chapter_id": chapter_id,
                    "next_chapter_id": project.current_chapter_id,
                    "project_status": project.status,
                    **approval_note,
                },
            )
        )
        self.session.flush()
        return {
            "project": project_payload(project),
            "next_chapter_id": project.current_chapter_id,
            "approved_chapter_id": chapter_id,
            "approval_note": approval_note,
        }

    def reopen_final(
        self,
        project_id: str,
        chapter_id: str,
        *,
        reason: str,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        project = ProjectService(self.session).require_project(project_id)
        self._require_project_chapter(project, chapter_id)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 1000:
            raise DomainError(
                "CHAPTER_REOPEN_REASON_INVALID",
                "reason must contain between 1 and 1000 characters",
                status_code=400,
            )

        approved = list(project.approved_chapter_ids_json or [])
        if chapter_id not in approved:
            raise DomainError(
                "CHAPTER_FINAL_NOT_APPROVED",
                "only a project-approved chapter can be reopened",
                status_code=409,
                details={"chapter_id": chapter_id},
            )

        reopen_index = approved.index(chapter_id)
        invalidated_chapter_ids = approved[reopen_index:]
        project.approved_chapter_ids_json = approved[:reopen_index]
        project.current_chapter_id = chapter_id
        project.status = PROJECT_STATUS_CHAPTER_READY

        for invalidated_id in invalidated_chapter_ids:
            invalidated = self.session.get(ChapterGoal, invalidated_id)
            if invalidated is None or invalidated.project_id != project.project_id:
                continue
            invalidated.state = "draft" if invalidated_id == chapter_id else "planned"

        audit_payload = {
            "project_id": project.project_id,
            "chapter_id": chapter_id,
            "reason": normalized_reason,
            "invalidated_chapter_ids": invalidated_chapter_ids,
            "remaining_approved_chapter_ids": list(
                project.approved_chapter_ids_json or []
            ),
            "actor_ref": actor_ref or "operator",
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_reopened",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json=audit_payload,
            )
        )
        self.session.flush()
        return {
            "project": project_payload(project),
            "reopened_chapter_id": chapter_id,
            "invalidated_chapter_ids": invalidated_chapter_ids,
            "reason": normalized_reason,
            "actor_ref": actor_ref or "operator",
        }

    def confirm_read(
        self,
        project_id: str,
        chapter_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        body = payload or {}
        project = ProjectService(self.session).require_project(project_id)
        self._require_project_chapter(project, chapter_id)
        if project.current_chapter_id != chapter_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_CURRENT",
                "only the current chapter final can be confirmed",
                status_code=409,
            )
        ChapterManuscriptService(self.session).require_publishable(chapter_id)
        packet = self.review_packet(project, chapter_id)
        if not packet or not packet.get("body_hash"):
            raise DomainError(
                "CHAPTER_FINAL_READ_CONFIRM_UNAVAILABLE",
                "current chapter body is not available for read confirmation",
                status_code=409,
            )
        note = str(body.get("note") or "").strip()
        if len(note) > 1000:
            raise DomainError(
                "CHAPTER_READ_CONFIRM_NOTE_TOO_LONG",
                "note must be 1000 characters or fewer",
                status_code=400,
            )
        confirmed_at = utcnow()
        confirmed_by = actor_ref or "operator"
        confirmation = {
            "project_id": project.project_id,
            "chapter_id": chapter_id,
            "body_hash": packet["body_hash"],
            "char_count": int(packet.get("char_count") or 0),
            "body_source": packet.get("body_source"),
            "confirmed_at": confirmed_at,
            "confirmed_by": confirmed_by,
            "note": note,
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_read_confirmed",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json=confirmation,
            )
        )
        self.session.flush()
        return confirmation


    def review_packet(
        self, project: StoryProject, chapter_id: str | None
    ) -> dict[str, Any] | None:
        if not chapter_id or project.status != PROJECT_STATUS_CHAPTER_FINAL_REVIEW:
            return None
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None:
            return None
        latest_job = self._latest_job(chapter_id)
        issues_summary = []
        latest_error = (
            (latest_job.result_summary_json or {}).get("latest_error")
            if latest_job
            else None
        )
        if latest_error:
            issues_summary.append(latest_error)
        manuscript = ChapterManuscriptService(self.session).manuscript_detail(
            chapter_id
        )
        aggregate = manuscript.get("aggregate") or None
        assembled = manuscript.get("assembled") or {}
        if aggregate and str(aggregate.get("content") or ""):
            body = str(aggregate.get("content") or "")
            body_source = "aggregate"
            char_count = int(aggregate.get("char_count") or len(body))
            aggregate_row_id = aggregate.get("row_id")
        else:
            body = str(assembled.get("content") or "")
            body_source = "assembled" if body else "empty"
            char_count = int(assembled.get("char_count") or len(body))
            aggregate_row_id = None
        missing_scene_ids = list(assembled.get("missing_scene_ids") or [])
        completion_status = manuscript.get("completion_status") or "empty"
        body_empty_reason = None
        if not body:
            body_empty_reason = (
                "no_generated_scenes"
                if completion_status == "empty"
                else "manuscript_body_empty"
            )
        body_hash = self._chapter_body_hash(body) if body else ""
        read_confirmation = (
            self._latest_read_confirmation(
                project.project_id, chapter.chapter_id, body_hash
            )
            if body_hash
            else None
        )
        scene_reviews = self._scene_reviews(chapter.chapter_id)
        return {
            "chapter_id": chapter.chapter_id,
            "chapter_goal": chapter.chapter_goal,
            "body": body,
            "body_hash": body_hash,
            "read_confirmation": read_confirmation,
            "body_source": body_source,
            "char_count": char_count,
            "body_empty_reason": body_empty_reason,
            "completion_status": completion_status,
            "comparison_status": manuscript.get("comparison_status"),
            "missing_scene_ids": missing_scene_ids,
            "missing_scene_labels": self._missing_scene_labels(
                missing_scene_ids, scene_reviews
            ),
            "scene_coverage": self._scene_coverage(scene_reviews),
            "target_word_count_band": self._target_word_count_band(project),
            "aggregate_row_id": aggregate_row_id,
            "source_safety_scan": manuscript.get("source_safety_scan"),
            "scene_reviews": scene_reviews,
            "issues_summary": issues_summary,
            "run_status": latest_job.status if latest_job else "idle",
            "reference_safety": list(REFERENCE_SAFETY_RULES),
            "reference_profile_summaries": [
                profile.get("safe_summary")
                for profile in ProjectService(self.session)._reference_profile_payloads(
                    project
                )
                if profile.get("safe_summary")
            ],
            "small_revision_entry": {
                "writer_room_object_type": "chapter",
                "writer_room_object_id": chapter.chapter_id,
                "deepdesk_object_id": chapter.chapter_id,
            },
        }

    def _require_current_read_confirmation(
        self, project: StoryProject, chapter_id: str
    ) -> dict[str, Any]:
        packet = self.review_packet(project, chapter_id)
        body_hash = str((packet or {}).get("body_hash") or "")
        confirmation = (packet or {}).get("read_confirmation")
        if not body_hash or not confirmation:
            raise DomainError(
                "CHAPTER_FINAL_READ_CONFIRM_REQUIRED",
                "read and confirm the current chapter body before approving final",
                status_code=409,
                details={"chapter_id": chapter_id, "body_hash": body_hash},
            )
        return confirmation

    @staticmethod
    def _chapter_body_hash(body: str) -> str:
        return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()

    def _latest_read_confirmation(
        self, project_id: str, chapter_id: str, body_hash: str
    ) -> dict[str, Any] | None:
        if not body_hash:
            return None
        rows = (
            self.session.execute(
                select(OperationLog)
                .where(
                    OperationLog.event_type.in_(
                        ("chapter_final_read_confirmed", "chapter_final_reopened")
                    ),
                    OperationLog.object_type == "chapter",
                    or_(
                        OperationLog.object_ref == chapter_id,
                        OperationLog.event_type == "chapter_final_reopened",
                    ),
                )
                .order_by(
                    OperationLog.created_at.desc(), OperationLog.operation_id.desc()
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            payload = dict(row.payload_json or {})
            if row.event_type == "chapter_final_reopened":
                if payload.get("project_id") != project_id:
                    continue
                invalidated_ids = list(payload.get("invalidated_chapter_ids") or [])
                if chapter_id == row.object_ref or chapter_id in invalidated_ids:
                    return None
                continue
            if (
                payload.get("project_id") != project_id
                or payload.get("chapter_id") != chapter_id
            ):
                continue
            if payload.get("body_hash") != body_hash:
                continue
            return {
                "chapter_id": chapter_id,
                "body_hash": body_hash,
                "confirmed_at": payload.get("confirmed_at") or row.created_at,
                "confirmed_by": payload.get("confirmed_by")
                or payload.get("actor_ref")
                or "operator",
                "note": payload.get("note") or "",
                "operation_id": row.operation_id,
            }
        return None

    def _scene_reviews(self, chapter_id: str) -> list[dict[str, Any]]:
        scenes = (
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            )
            .scalars()
            .all()
        )
        reviews: list[dict[str, Any]] = []
        for scene in scenes:
            state = self.session.get(SceneRunState, scene.scene_id)
            final_row = (
                self.session.get(FinalScene, state.current_final_scene_row_id)
                if state and state.current_final_scene_row_id
                else None
            )
            qc_report = self._latest_scene_qc_report(scene.scene_id)
            qc_issues = _qc_issue_summaries(qc_report)
            body = final_row.content if final_row is not None else ""
            excerpt = " ".join(str(body or "").split())
            reviews.append(
                {
                    "scene_id": scene.scene_id,
                    "scene_seq": scene.scene_seq,
                    "title": scene.scene_goal or scene.hook or scene.scene_id,
                    "body_excerpt": excerpt[:240],
                    "char_count": len(body or ""),
                    "missing": not bool(body),
                    "issues_summary": qc_issues,
                    "evidence_summary": [
                        issue.get("evidence")
                        for issue in qc_issues
                        if issue.get("evidence")
                    ],
                    "suggested_actions": [
                        issue.get("suggested_action")
                        for issue in qc_issues
                        if issue.get("suggested_action")
                    ],
                    "qc_summary": {
                        "qc_report_id": (
                            qc_report.qc_report_id if qc_report is not None else None
                        ),
                        "status": qc_report.status if qc_report is not None else "",
                        "next_action": (
                            qc_report.next_action if qc_report is not None else ""
                        ),
                        "issue_count": len(qc_issues),
                    },
                    "current_decision": "pending",
                }
            )
        return reviews

    def _latest_scene_qc_report(self, scene_id: str) -> QcReport | None:
        return (
            self.session.execute(
                select(QcReport)
                .where(QcReport.scene_id == scene_id)
                .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
            )
            .scalars()
            .first()
        )

    @staticmethod
    def _scene_coverage(scene_reviews: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(scene_reviews)
        completed = sum(
            1 for item in scene_reviews if int(item.get("char_count") or 0) > 0
        )
        return {
            "completed_count": completed,
            "total_count": total,
            "percent": round((completed / total) * 100) if total else 0,
        }

    @staticmethod
    def _missing_scene_labels(
        missing_scene_ids: list[str], scene_reviews: list[dict[str, Any]]
    ) -> list[str]:
        by_id = {str(item.get("scene_id") or ""): item for item in scene_reviews}
        missing_ids = {str(item or "") for item in missing_scene_ids if str(item or "")}
        for item in scene_reviews:
            if item.get("missing"):
                scene_id = str(item.get("scene_id") or "")
                if scene_id:
                    missing_ids.add(scene_id)
        labels: list[str] = []
        for scene_id in sorted(missing_ids):
            item = by_id.get(scene_id)
            if item is None:
                labels.append(scene_id)
                continue
            seq = item.get("scene_seq")
            title = str(item.get("title") or scene_id).strip() or scene_id
            prefix = f"第 {seq} 场" if seq else "场景"
            labels.append(f"{prefix}：{title}")
        return labels

    @staticmethod
    def _target_word_count_band(project: StoryProject) -> dict[str, Any] | None:
        target_word_count = int(project.target_word_count or 0)
        target_chapter_count = int(project.target_chapter_count or 0)
        if target_word_count <= 0 or target_chapter_count <= 0:
            return None
        per_chapter = max(1, round(target_word_count / target_chapter_count))
        lower = max(1, round(per_chapter * 0.85))
        upper = max(lower, round(per_chapter * 1.15))
        return {
            "target": per_chapter,
            "min": lower,
            "max": upper,
            "label": f"{lower}-{upper} 字",
        }

    def _require_project_chapter(
        self, project: StoryProject, chapter_id: str
    ) -> ChapterGoal:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id != project.project_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_FOUND",
                "chapter does not belong to project",
                status_code=404,
            )
        return chapter

    def _next_chapter_id(self, project_id: str, chapter_id: str) -> str | None:
        chapter_ids = [
            row[0]
            for row in self.session.execute(
                select(ChapterGoal.chapter_id)
                .where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
                .order_by(
                    ChapterGoal.display_order.is_(None).asc(),
                    ChapterGoal.display_order.asc(),
                    ChapterGoal.chapter_id.asc(),
                )
            ).all()
        ]
        try:
            index = chapter_ids.index(chapter_id)
        except ValueError:
            return None
        return chapter_ids[index + 1] if index + 1 < len(chapter_ids) else None

    def _latest_job(self, chapter_id: str) -> ChapterRunJob | None:
        return (
            self.session.execute(
                select(ChapterRunJob)
                .where(ChapterRunJob.chapter_id == chapter_id)
                .order_by(ChapterRunJob.created_at.desc(), ChapterRunJob.job_id.desc())
            )
            .scalars()
            .first()
        )


def start_project_chapter_run_job_worker(
    project_id: str, chapter_id: str, job_id: str
) -> None:
    thread = threading.Thread(
        target=_run_project_chapter_job_worker,
        args=(project_id, chapter_id, job_id),
        daemon=True,
    )
    thread.start()


def _run_project_chapter_job_worker(
    project_id: str, chapter_id: str, job_id: str
) -> None:
    session = SessionLocal()
    try:
        project = ProjectService(session).require_project(project_id)
        if project.current_chapter_id != chapter_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_CURRENT",
                "only the current chapter can be run from project dashboard",
            )
        project.status = PROJECT_STATUS_CHAPTER_RUNNING
        session.commit()

        run_result = ChapterRunnerService(session).run_full(chapter_id)
        project = ProjectService(session).require_project(project_id)
        if run_result.get("status") == "completed":
            ChapterManuscriptService(session).require_complete(chapter_id)
            project.status = PROJECT_STATUS_CHAPTER_FINAL_REVIEW
        elif run_result.get("status") == "blocked":
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
        elif run_result.get("status") == "failed":
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
        else:
            project.status = PROJECT_STATUS_CHAPTER_READY
        session.commit()
    except DomainError as exc:
        session.rollback()
        # Startup recovery may be invoked concurrently by multiple ASGI
        # workers.  Losing the durable chapter-job CAS is a benign duplicate
        # dispatch and must never overwrite the winning worker with FAILED.
        # RUN_OWNER_LEASE_LOST is the same situation observed after the claim:
        # another worker replaced this one, and the job now belongs to it.
        if exc.code in {"RUN_JOB_IN_PROGRESS", "RUN_JOB_NOT_CLAIMABLE", "RUN_OWNER_LEASE_LOST"}:
            return
        _mark_project_chapter_job_failed(
            job_id,
            project_id,
            chapter_id,
            exc.code,
            exc.message,
            author_action=_domain_error_author_action(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        session.rollback()
        _mark_project_chapter_job_failed(
            job_id,
            project_id,
            chapter_id,
            "CHAPTER_RUN_JOB_FAILED",
            str(exc) or "chapter run job failed",
        )
    finally:
        session.close()


def _domain_error_author_action(exc: DomainError) -> dict[str, Any] | None:
    details = exc.details if isinstance(exc.details, dict) else None
    action = details.get("author_action") if details else None
    return dict(action) if isinstance(action, dict) else None


def _mark_project_chapter_job_failed(
    job_id: str,
    project_id: str,
    chapter_id: str,
    error_code: str,
    error_text: str,
    *,
    author_action: dict[str, Any] | None = None,
) -> None:
    session = SessionLocal()
    try:
        job = session.get(ChapterRunJob, job_id)
        if job is not None:
            job.status = "failed"
            job.error_code = error_code
            job.error_text = error_text
            job.finished_at = utcnow()
            summary = dict(job.result_summary_json or {})
            latest_error: dict[str, Any] = {"code": error_code, "message": error_text}
            # ChapterRunnerService 在 claim 后失败时已把带 author_action 的 latest_error
            # 提交进任务行；这里是同一错误的二次落库，不能把作者指引覆盖掉。
            previous = summary.get("latest_error")
            action = author_action
            if action is None and isinstance(previous, dict) and previous.get("code") == error_code:
                previous_action = previous.get("author_action")
                action = dict(previous_action) if isinstance(previous_action, dict) else None
            if action:
                latest_error["author_action"] = action
            summary["latest_error"] = latest_error
            job.result_summary_json = summary
        project = session.get(StoryProject, project_id)
        if project is not None and project.current_chapter_id == chapter_id:
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
        session.commit()
    finally:
        session.close()


def project_summary_payload(project: StoryProject) -> dict[str, Any]:
    payload = project_payload(project)
    payload.pop("outline_text", None)
    return payload


def project_payload(
    project: StoryProject,
    *,
    reference_profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "title": project.title,
        "genre": project.genre,
        "target_word_count": project.target_word_count,
        "target_chapter_count": project.target_chapter_count,
        # FE-ALIGN P2 作品档案字段（原型 WsWorks 作品对象）
        "mark": getattr(project, "mark", None),
        "accent": getattr(project, "accent", None),
        "synopsis_line": getattr(project, "synopsis_line", None),
        "words_target_daily": getattr(project, "words_target_daily", None),
        # Compatibility-only response field. Demo project identity was retired
        # by migration 20260717_0074 and is no longer persisted.
        "is_demo": False,
        "outline_text": project.outline_text,
        "planning_mode": getattr(project, "planning_mode", "outline_driven")
        or "outline_driven",
        "snowflake_schema_version": getattr(project, "snowflake_schema_version", None),
        "snowflake_workflow_mode": getattr(project, "snowflake_workflow_mode", "strict")
        or "strict",
        "status": project.status,
        "active_outline_plan_id": project.active_outline_plan_id,
        "current_chapter_id": project.current_chapter_id,
        "approved_chapter_ids": list(project.approved_chapter_ids_json or []),
        "reference_profile_ids": list(reference_profile_ids or []),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def outline_plan_payload(plan: OutlinePlan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "project_id": plan.project_id,
        "version": plan.version,
        "status": plan.status,
        "plan_json": plan.plan_json or {},
        "created_at": plan.created_at,
        "approved_at": plan.approved_at,
    }


def chapter_payload(session: Session, chapter: ChapterGoal) -> dict[str, Any]:
    scenes = (
        session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0
            )
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        )
        .scalars()
        .all()
    )
    return {
        "chapter_id": chapter.chapter_id,
        "project_id": chapter.project_id,
        "outline_plan_id": chapter.outline_plan_id,
        "chapter_goal": chapter.chapter_goal,
        "main_plot_push": chapter.main_plot_push,
        "emotional_target": chapter.emotional_target,
        "ending_effect": chapter.ending_effect,
        "must_not": chapter.must_not,
        "planned_scene_count": chapter.planned_scene_count,
        "scenes": [scene_payload(scene) for scene in scenes],
    }


def scene_payload(scene: SceneCard) -> dict[str, Any]:
    return {
        "scene_id": scene.scene_id,
        "chapter_id": scene.chapter_id,
        "project_id": scene.project_id,
        "outline_plan_id": scene.outline_plan_id,
        "scene_seq": scene.scene_seq,
        "scene_goal": scene.scene_goal,
        "beats_json": list(scene.beats_json or []),
        "must_include_text": scene.must_include_text,
        "forbidden_text": scene.forbidden_text,
        "exit_change": scene.exit_change,
        "hook": scene.hook,
        "target_length_band": scene.target_length_band,
        "scene_type": scene.scene_type,
        "is_chapter_last": scene.is_chapter_last,
    }


def reference_profile_payload(
    profile: StyleReferenceProfile,
    *,
    binding: StyleReferenceInjectionBinding | None = None,
) -> dict[str, Any]:
    safe_summary = _reference_profile_safe_summary(profile.profile_json or {})
    payload = {
        "profile_id": profile.profile_id,
        "title": profile.title,
        "status": profile.status,
        "profile_json": profile.profile_json or {},
        "safe_summary": safe_summary,
    }
    if binding is not None:
        payload.update(
            {
                "binding_id": binding.binding_id,
                "scope": binding.scope,
                "scope_ref_id": binding.scope_ref_id,
                "task_type": binding.task_type,
                "strategy": binding.strategy,
            }
        )
    return payload


def _reference_profile_safe_summary(profile_json: dict[str, Any]) -> dict[str, Any]:
    tags = []
    for label, keys in (
        ("节奏", ("style_features", "rhythm", "pacing", "style_rules")),
        (
            "结构",
            (
                "narrative_patterns",
                "structure_patterns",
                "structure_techniques",
                "structure_rules",
                "calibration_guidance",
            ),
        ),
        (
            "安全提示",
            (
                "banned_replication_rules",
                "safety_rules",
                "forbidden_copy_rules",
                "safety_constraints",
            ),
        ),
    ):
        values: list[str] = []
        for key in keys:
            values.extend(_string_list(profile_json.get(key)))
        if values:
            tags.append({"label": label, "summary": values[0][:120]})
    return {
        "abstract_tags": tags[:6],
        "safety_note": "仅使用抽象节奏、结构和安全规则；不展示或复制参考书原文。",
    }


def _qc_issue_summaries(
    report: QcReport | None, *, limit: int = 3
) -> list[dict[str, str]]:
    if report is None:
        return []
    items: list[dict[str, str]] = []
    for issue in list(report.issues_json or [])[:limit]:
        if not isinstance(issue, dict):
            continue
        evidence = ""
        spans = (
            issue.get("evidence_spans")
            if isinstance(issue.get("evidence_spans"), list)
            else []
        )
        if spans:
            first = spans[0]
            if isinstance(first, dict):
                evidence = str(
                    first.get("text")
                    or first.get("excerpt")
                    or first.get("snippet")
                    or ""
                ).strip()
        items.append(
            {
                "issue": str(
                    issue.get("message")
                    or issue.get("issue")
                    or issue.get("dimension")
                    or "质检提示"
                ).strip(),
                "evidence": evidence[:180],
                "suggested_action": str(
                    issue.get("recommendation")
                    or issue.get("next_action")
                    or report.next_action
                    or "先回到场景工作台处理。"
                ).strip(),
                "qc_report_id": report.qc_report_id,
            }
        )
    return items


def _outline_points(outline_text: str, chapter_count: int) -> list[str]:
    lines = [
        re.sub(r"^[\s\-\*\d\.、）)]+", "", line).strip()
        for line in str(outline_text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        lines = [
            part.strip()
            for part in re.split(r"[。！？!?；;]\s*", str(outline_text or ""))
            if part.strip()
        ]
    if not lines:
        lines = ["围绕用户大纲推进核心冲突"]
    while len(lines) < chapter_count:
        lines.append(lines[-1])
    return lines[:chapter_count]


def _scene_role(scene_index: int, scene_count: int) -> str:
    if scene_index == 1:
        return "开场承压"
    if scene_index == scene_count:
        return "转折收束"
    return "冲突升级"


def _chapter_push(index: int, chapter_count: int, point: str) -> str:
    if index == 1:
        return f"建立主矛盾和行动入口：{point}"
    if index == chapter_count:
        return f"兑现核心承诺并留下长期余波：{point}"
    return f"升级阻力并改变人物关系：{point}"


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _planning_mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode == "snowflake":
        return "snowflake"
    return "outline_driven"


def _snowflake_workflow_mode(value: Any, *, default: str = "strict") -> str:
    mode = str(value or default).strip().lower()
    if mode in {"strict", "explore"}:
        return mode
    return default


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, ""):
        return []
    return [str(value).strip()]
