from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    FinalScene,
    GenerationPlanningArtifact,
    LlmCall,
    RevisionCandidate,
    SceneBlueprint,
    SceneCard,
    WriterEvaluation,
)
from novel_system.services.author_actions import author_action
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMAccountingRejected, LLMCallContext
from novel_system.services.llm_fail_closed import is_llm_capability_error, raise_llm_domain_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
    current_llm_run_job_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.writer_briefs import normalize_chapter_writer_brief, normalize_scene_writer_brief


NEAR_FINAL_RUBRIC_ID = "near_final_acceptance_v1"
CHARACTER_PRESSURE_ARTIFACT = "character_pressure_blueprint"
CHAPTER_ARCHITECTURE_ARTIFACT = "chapter_story_architecture"
NEAR_FINAL_REWRITE_TYPE = "near_final_scene_rewrite"

SCENE_FAILURE_CLASSES = {
    "fact_blocker",
    "scene_structure_failure",
    "character_flatness",
    "prose_model_voice",
    "ending_weakness",
    "chapter_payoff_gap",
    "reference_safety",
}
AUTOMATED_REWRITE_FAILURE_CLASSES = {
    "scene_structure_failure",
    "character_flatness",
    "prose_model_voice",
    "ending_weakness",
    "chapter_payoff_gap",
}


CHARACTER_PRESSURE_FIELDS = (
    "surface_goal",
    "hidden_fear",
    "wrong_belief",
    "shame_point",
    "avoidance_strategy",
    "relationship_debt",
    "current_mask",
)
CHAPTER_ARCHITECTURE_FIELDS = (
    "chapter_promise",
    "escalation_path",
    "reveal_plan",
    "payoff_target",
    "character_shift",
    "ending_question",
)
SCENE_ACCEPTANCE_SCORE_KEYS = frozenset(
    {
        "story_necessity",
        "character_pressure",
        "forced_choice_pressure",
        "dialogue_edge",
        "information_release",
        "prose_freshness",
        "ending_drive",
        "continuity",
        "author_voice_match",
        "model_voice_risk",
        "reference_safety",
    }
)
CHAPTER_ACCEPTANCE_SCORE_KEYS = frozenset(
    {
        "chapter_promise",
        "escalation",
        "payoff_integrity",
        "character_shift",
        "ending_drive",
        "continuity",
    }
)


class NearFinalPlanningService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)

    def ensure_scene_planning(
        self,
        scene_id: str,
        actor_ref: str = "operator",
        *,
        step_reconciler: Callable[[str], None] | None = None,
        artifact_committed: Callable[[str, dict[str, Any], bool], None] | None = None,
        resume_artifacts: dict[str, GenerationPlanningArtifact] | None = None,
    ) -> dict[str, Any]:
        scene = self._require_scene(scene_id)
        chapter = self._require_chapter(scene.chapter_id)
        resume_artifacts = resume_artifacts or {}
        chapter_artifact = resume_artifacts.get("chapter_architecture")
        if chapter_artifact is None:
            chapter_artifact = self._latest_artifact(
                artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
                object_type="chapter",
                object_id=chapter.chapter_id,
            )
        chapter_reused = chapter_artifact is not None
        if chapter_artifact is None:
            chapter_step_key = "planning:chapter_architecture"
            if step_reconciler is not None:
                step_reconciler(chapter_step_key)
            chapter_artifact = self._generate_chapter_architecture(
                scene=scene,
                chapter=chapter,
                actor_ref=actor_ref,
                execution_step_key=chapter_step_key,
            )
        if artifact_committed is not None:
            artifact_committed(
                "chapter_architecture",
                self.serialize_artifact(chapter_artifact),
                chapter_reused,
            )

        character_artifact = resume_artifacts.get("character_pressure")
        if character_artifact is None:
            character_artifact = self._latest_artifact(
                artifact_type=CHARACTER_PRESSURE_ARTIFACT,
                object_type="scene",
                object_id=scene.scene_id,
            )
        character_reused = character_artifact is not None
        if character_artifact is None:
            character_step_key = "planning:character_pressure"
            if step_reconciler is not None:
                step_reconciler(character_step_key)
            character_artifact = self._generate_character_pressure(
                scene=scene,
                chapter=chapter,
                actor_ref=actor_ref,
                execution_step_key=character_step_key,
            )
        if artifact_committed is not None:
            artifact_committed(
                "character_pressure",
                self.serialize_artifact(character_artifact),
                character_reused,
            )

        return {
            "chapter_architecture": self.serialize_artifact(chapter_artifact),
            "character_pressure": self.serialize_artifact(character_artifact),
        }

    @staticmethod
    def serialize_artifact(artifact: GenerationPlanningArtifact) -> dict[str, Any]:
        return {
            "row_id": artifact.row_id,
            "artifact_type": artifact.artifact_type,
            "object_type": artifact.object_type,
            "object_id": artifact.object_id,
            "chapter_id": artifact.chapter_id,
            "scene_id": artifact.scene_id,
            "payload": artifact.payload_json or {},
            "llm_call_id": artifact.llm_call_id,
            "source_bundle_id": artifact.source_bundle_id,
            "source_bundle_hash": artifact.source_bundle_hash,
            "status": artifact.status,
            "created_at": artifact.created_at,
        }

    def _generate_chapter_architecture(
        self,
        *,
        scene: SceneCard,
        chapter: ChapterGoal,
        actor_ref: str,
        execution_step_key: str | None = None,
    ) -> GenerationPlanningArtifact:
        source = self._source_snapshot(scene=scene, chapter=chapter, include_chapter_architecture=False)
        prompt = self.prompt_builder.build(source["snapshot"], CHAPTER_ARCHITECTURE_ARTIFACT)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=chapter.chapter_id,
                bundle_id=source["source_bundle_id"],
                bundle_hash=source["source_bundle_hash"],
                node_id=CHAPTER_ARCHITECTURE_ARTIFACT,
                step=CHAPTER_ARCHITECTURE_ARTIFACT,
                prompt=prompt,
                user_prompt=_planning_user_prompt(prompt["user_prompt"], scene=scene, chapter=chapter),
                execution_step_key=execution_step_key,
            )
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code="CHAPTER_STORY_ARCHITECTURE_LLM_REQUIRED",
                failure_code="CHAPTER_STORY_ARCHITECTURE_FAILED",
                operation="chapter architecture generation",
                node_id=CHAPTER_ARCHITECTURE_ARTIFACT,
                next_action="configure_chapter_story_architecture_route_and_retry",
            )
        try:
            payload = _normalize_chapter_architecture_payload(node_result.response.structured_output)
        except ValueError as exc:
            raise DomainError(
                "CHAPTER_STORY_ARCHITECTURE_OUTPUT_INVALID",
                f"chapter architecture generation returned an invalid payload: {exc}",
                status_code=502,
                details={"llm_call_id": node_result.llm_call_id, "node_id": CHAPTER_ARCHITECTURE_ARTIFACT},
            ) from exc
        llm_call_id = node_result.llm_call_id
        return self._persist_artifact(
            artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            payload=payload,
            llm_call_id=llm_call_id,
            source_bundle_id=source["source_bundle_id"],
            source_bundle_hash=source["source_bundle_hash"],
            actor_ref=actor_ref,
        )

    def _generate_character_pressure(
        self,
        *,
        scene: SceneCard,
        chapter: ChapterGoal,
        actor_ref: str,
        execution_step_key: str | None = None,
    ) -> GenerationPlanningArtifact:
        source = self._source_snapshot(scene=scene, chapter=chapter, include_chapter_architecture=True)
        prompt = self.prompt_builder.build(source["snapshot"], CHARACTER_PRESSURE_ARTIFACT)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=chapter.chapter_id,
                bundle_id=source["source_bundle_id"],
                bundle_hash=source["source_bundle_hash"],
                node_id=CHARACTER_PRESSURE_ARTIFACT,
                step=CHARACTER_PRESSURE_ARTIFACT,
                prompt=prompt,
                user_prompt=_planning_user_prompt(prompt["user_prompt"], scene=scene, chapter=chapter),
                execution_step_key=execution_step_key,
            )
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code="CHARACTER_PRESSURE_BLUEPRINT_LLM_REQUIRED",
                failure_code="CHARACTER_PRESSURE_BLUEPRINT_FAILED",
                operation="character pressure generation",
                node_id=CHARACTER_PRESSURE_ARTIFACT,
                next_action="configure_character_pressure_blueprint_route_and_retry",
            )
        try:
            payload = _normalize_character_pressure_payload(node_result.response.structured_output)
        except ValueError as exc:
            raise DomainError(
                "CHARACTER_PRESSURE_BLUEPRINT_OUTPUT_INVALID",
                f"character pressure generation returned an invalid payload: {exc}",
                status_code=502,
                details={"llm_call_id": node_result.llm_call_id, "node_id": CHARACTER_PRESSURE_ARTIFACT},
            ) from exc
        llm_call_id = node_result.llm_call_id
        return self._persist_artifact(
            artifact_type=CHARACTER_PRESSURE_ARTIFACT,
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=chapter.chapter_id,
            scene_id=scene.scene_id,
            payload=payload,
            llm_call_id=llm_call_id,
            source_bundle_id=source["source_bundle_id"],
            source_bundle_hash=source["source_bundle_hash"],
            actor_ref=actor_ref,
        )

    def _persist_artifact(
        self,
        *,
        artifact_type: str,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        payload: dict[str, Any],
        llm_call_id: str | None,
        source_bundle_id: str,
        source_bundle_hash: str,
        actor_ref: str,
    ) -> GenerationPlanningArtifact:
        for row in self.session.execute(
            select(GenerationPlanningArtifact).where(
                GenerationPlanningArtifact.artifact_type == artifact_type,
                GenerationPlanningArtifact.object_type == object_type,
                GenerationPlanningArtifact.object_id == object_id,
                GenerationPlanningArtifact.status == "active",
            )
        ).scalars().all():
            row.status = "superseded"
        artifact = GenerationPlanningArtifact(
            row_id=f"planning_{artifact_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            artifact_type=artifact_type,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            payload_json=payload,
            llm_call_id=llm_call_id,
            source_bundle_id=source_bundle_id,
            source_bundle_hash=source_bundle_hash,
            status="active",
            created_by=actor_ref or "near_final_planning",
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def _source_snapshot(
        self,
        *,
        scene: SceneCard,
        chapter: ChapterGoal,
        include_chapter_architecture: bool,
    ) -> dict[str, Any]:
        scene_blueprint = self._latest_scene_blueprint(scene.scene_id)
        source_refs: dict[str, Any] = {
            "chapter_goal": chapter.chapter_id,
            "scene_card": scene.scene_id,
            "chapter_writer_brief": chapter.chapter_id,
            "scene_writer_brief": scene.scene_id,
        }
        injections = [
            {"slot": "chapter_goal", "ref_id": chapter.chapter_id, "digest_key": "chapter_goal"},
            {"slot": "scene_card", "ref_id": scene.scene_id, "digest_key": "scene_card"},
            {"slot": "chapter_writer_brief", "ref_id": chapter.chapter_id, "digest_key": "chapter_writer_brief"},
            {"slot": "scene_writer_brief", "ref_id": scene.scene_id, "digest_key": "scene_writer_brief"},
        ]
        inline_digests: dict[str, str] = {
            "chapter_goal": chapter.chapter_goal or "",
            "scene_card": json.dumps(
                {
                    "scene_goal": scene.scene_goal or "",
                    "location": scene.location or "",
                    "beats": scene.beats_json or [],
                    "must_include_text": scene.must_include_text or "",
                    "exit_change": scene.exit_change or "",
                    "hook": scene.hook or "",
                    "all_chapter_scene_cards": self._chapter_scene_digest(chapter.chapter_id),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "chapter_writer_brief": json.dumps(
                normalize_chapter_writer_brief(chapter.writer_brief_json),
                ensure_ascii=False,
                sort_keys=True,
            ),
            "scene_writer_brief": json.dumps(
                normalize_scene_writer_brief(scene.writer_brief_json),
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        if scene_blueprint is not None:
            source_refs["scene_blueprint_row_id"] = scene_blueprint.row_id
            injections.append({"slot": "scene_blueprint", "ref_id": scene_blueprint.row_id, "digest_key": "scene_blueprint"})
            inline_digests["scene_blueprint"] = json.dumps(
                scene_blueprint.blueprint_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )
        if include_chapter_architecture:
            architecture = self._latest_artifact(
                artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
                object_type="chapter",
                object_id=chapter.chapter_id,
            )
            if architecture is not None:
                source_refs["chapter_story_architecture_artifact_row_id"] = architecture.row_id
                injections.append(
                    {
                        "slot": "chapter_story_architecture",
                        "ref_id": architecture.row_id,
                        "digest_key": "chapter_story_architecture",
                    }
                )
                inline_digests["chapter_story_architecture"] = json.dumps(
                    architecture.payload_json or {},
                    ensure_ascii=False,
                    sort_keys=True,
                )
        snapshot = {
            "contract_version": "NEAR_FINAL_PLANNING_SOURCE_v1",
            "stage_allowlist_name": "near_final_planning",
            "scene_id": scene.scene_id,
            "chapter_id": chapter.chapter_id,
            "source_version_refs": source_refs,
            "resolved_ref_ids": {},
            "ordered_injections": injections,
            "inline_digests": inline_digests,
        }
        source_hash = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        return {
            "source_bundle_id": f"near_final_planning_source_{scene.scene_id}",
            "source_bundle_hash": source_hash,
            "snapshot": snapshot,
        }

    def _chapter_scene_digest(self, chapter_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        return [
            {
                "scene_id": row.scene_id,
                "scene_seq": row.scene_seq,
                "scene_goal": row.scene_goal,
                "exit_change": row.exit_change,
                "hook": row.hook,
            }
            for row in rows
        ]

    def _latest_artifact(self, *, artifact_type: str, object_type: str, object_id: str) -> GenerationPlanningArtifact | None:
        return self.session.execute(
            select(GenerationPlanningArtifact)
            .where(
                GenerationPlanningArtifact.artifact_type == artifact_type,
                GenerationPlanningArtifact.object_type == object_type,
                GenerationPlanningArtifact.object_id == object_id,
                GenerationPlanningArtifact.status == "active",
            )
            .order_by(GenerationPlanningArtifact.created_at.desc(), GenerationPlanningArtifact.row_id.desc())
        ).scalars().first()

    def _latest_scene_blueprint(self, scene_id: str) -> SceneBlueprint | None:
        return self.session.execute(
            select(SceneBlueprint)
            .where(SceneBlueprint.scene_id == scene_id, SceneBlueprint.status.in_(("accepted", "draft")))
            .order_by(SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc())
        ).scalars().first()

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)


class NearFinalAcceptanceService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)

    def evaluate_scene(
        self,
        scene_id: str,
        *,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        source_content: str,
        actor_ref: str = "operator",
        execution_step_key: str = "near_final_acceptance:0",
    ) -> dict[str, Any]:
        scene = self._require_scene(scene_id)
        prompt = self.prompt_builder.build(bundle["snapshot"], "near_final_acceptance_review")
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="near_final_acceptance_review",
                step="near_final_acceptance_review",
                prompt=prompt,
                user_prompt=_acceptance_user_prompt(prompt["user_prompt"], source_content=source_content),
                source_draft_row_id=source_draft_row_id,
                source_draft_content=source_content,
                execution_step_key=execution_step_key,
            )
            payload = _normalize_acceptance_payload(node_result.response.structured_output)
            llm_call_id = node_result.llm_call_id
        except LLMNodeExecutionError as exc:
            llm_call_id = self._ledgered_failure_llm_call_id(
                exc,
                node_id="near_final_acceptance_review",
                operation="场景准定稿验收",
                target_ref=f"scene_card:{scene.scene_id}",
            )
            payload = _execution_failure_payload(exc.message)

        payload = _apply_scene_near_final_gates(payload, source_content)
        source = {
            "content": source_content,
            "source_text_ref": f"source_draft:{source_draft_row_id}",
            "source_bundle_id": bundle["bundle_id"],
        }
        evaluation = self._persist_evaluation(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            source=source,
            payload=payload,
            llm_call_id=llm_call_id,
        )
        candidate = None
        if payload["pass_flag"]:
            self._supersede_open_scene_candidates(scene.scene_id)
        else:
            candidate = self._create_scene_candidate(
                evaluation=evaluation,
                source=source,
                payload=payload,
                actor_ref=actor_ref,
            )
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            status=payload["near_final_status"],
            details={
                "evaluation_id": evaluation.evaluation_id,
                "revision_candidate_id": candidate.revision_id if candidate is not None else None,
                "source_draft_row_id": source_draft_row_id,
                "llm_call_id": llm_call_id,
                "failure_class": payload.get("failure_class"),
                "execution_step_key": execution_step_key,
            },
        )
        self.session.flush()
        return {
            **payload,
            "evaluation_id": evaluation.evaluation_id,
            "revision_candidate_id": candidate.revision_id if candidate is not None else None,
            "should_rewrite": self._should_rewrite(payload),
        }

    def evaluate_chapter(
        self,
        chapter_id: str,
        actor_ref: str = "operator",
        *,
        execution_step_key: str = "chapter_near_final_review:0",
    ) -> dict[str, Any]:
        chapter = self._require_chapter(chapter_id)
        source = self._chapter_source(chapter)
        bundle = self._chapter_bundle(chapter, source)
        prompt = self.prompt_builder.build(bundle["snapshot"], "chapter_near_final_review")
        execution_id = current_llm_execution_id()
        context = LLMCallContext(
            scope_type="chapter",
            scope_id=chapter.chapter_id,
            project_id=chapter.project_id,
            chapter_id=chapter.chapter_id,
            node_id="chapter_near_final_review",
            step="chapter_near_final_review",
            execution_id=execution_id,
            execution_step_key=execution_step_key if execution_id is not None else None,
            run_job_id=current_llm_run_job_id(),
            provider_execution_mode=self._llm_runner.provider_execution_mode,
        )
        try:
            node_result = self._llm_runner.run(
                scene_id=None,
                chapter_id=chapter.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="chapter_near_final_review",
                step="chapter_near_final_review",
                prompt=prompt,
                user_prompt=_acceptance_user_prompt(prompt["user_prompt"], source_content=source["content"]),
                source_draft_row_id=source["source_text_ref"],
                source_draft_content=source["content"],
                execution_step_key=execution_step_key,
                context=context,
            )
            payload = _normalize_acceptance_payload(node_result.response.structured_output)
            llm_call_id = node_result.llm_call_id
        except LLMNodeExecutionError as exc:
            llm_call_id = self._ledgered_failure_llm_call_id(
                exc,
                node_id="chapter_near_final_review",
                operation="章级准定稿评审",
                target_ref=f"chapter:{chapter.chapter_id}",
            )
            payload = _execution_failure_payload(exc.message)

        evaluation = self._persist_evaluation(
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            source=source,
            payload=payload,
            llm_call_id=llm_call_id,
        )
        self._record_attempt(
            scene_id=None,
            chapter_id=chapter.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            status=payload["near_final_status"],
            details={
                "evaluation_id": evaluation.evaluation_id,
                "llm_call_id": llm_call_id,
                "failure_class": payload.get("failure_class"),
                "actor_ref": actor_ref,
            },
        )
        self.session.flush()
        return {**payload, "evaluation_id": evaluation.evaluation_id, "should_rewrite": False}

    def _ledgered_failure_llm_call_id(
        self,
        exc: LLMNodeExecutionError,
        *,
        node_id: str,
        operation: str,
        target_ref: str,
    ) -> str:
        """只有已落台账的失败才允许降级成"验收执行失败"评估行（正文照常交付）。

        LLMNodeRunner.run 在台账落行之前就被拒（例如记账上下文 / 运行任务归属校验失败、
        请求构造异常）时，异常携带的 llm_call_id 从未写入 llm_calls。若照旧把它持久化成
        evaluator_llm_call_id，归档检查点随后会以 RUN_CHECKPOINT_OUTPUT_MISSING 假失败，
        真实错误码被吞掉。这里 fail-closed：抛出真实错误码，且不留下悬空的评估行。
        """

        if self.session.get(LlmCall, exc.llm_call_id) is not None:
            return exc.llm_call_id
        control_plane_failure = is_llm_capability_error(exc) or isinstance(
            exc.original_error, LLMAccountingRejected
        )
        raise DomainError(
            exc.error_code,
            exc.message,
            status_code=409 if control_plane_failure else 502,
            details={
                "llm_call_id": exc.llm_call_id,
                "node_id": node_id,
                "error_code": exc.error_code,
                "retryable": exc.retryable,
                "next_action": "retry_after_restoring_llm_execution_context",
                "response_summary": exc.response_summary,
                "author_action": author_action(
                    f"{operation}未能启动",
                    f"{operation}请求在进入模型台账之前被拒绝（{exc.error_code}），已有正文没有被撤销。"
                    "请重新运行；若持续失败，请检查系统配置里的模型路由与运行任务状态。",
                    target_view="workbench",
                    target_ref=target_ref,
                    primary_button_label="去场景工作台",
                    evidence_summary=[f"节点：{node_id}", f"错误：{exc.error_code}"],
                ),
            },
        ) from exc

    @staticmethod
    def _should_rewrite(payload: dict[str, Any]) -> bool:
        if payload.get("pass_flag") or payload.get("requires_human_review"):
            return False
        return str(payload.get("failure_class") or "") in AUTOMATED_REWRITE_FAILURE_CLASSES

    def _persist_evaluation(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
        payload: dict[str, Any],
        llm_call_id: str | None,
    ) -> WriterEvaluation:
        raw_contract_field_refs = payload.get("contract_field_refs")
        contract_field_refs = (
            dict(raw_contract_field_refs)
            if isinstance(raw_contract_field_refs, dict)
            else {}
        )
        evaluation = WriterEvaluation(
            evaluation_id=f"near_final_eval_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            rubric_id=NEAR_FINAL_RUBRIC_ID,
            source_text_ref=source.get("source_text_ref"),
            source_bundle_id=source.get("source_bundle_id"),
            evaluator_llm_call_id=llm_call_id,
            lens="near_final_acceptance",
            overall_score=payload.get("overall_score"),
            scores_json=payload.get("scores") or {},
            findings_json=payload.get("findings") or [],
            failure_class=payload.get("failure_class"),
            auto_rewrite_eligible=1 if NearFinalAcceptanceService._should_rewrite(payload) else 0,
            contract_field_refs_json=contract_field_refs,
            promotion_blockers_json=_promotion_blockers_from_acceptance(payload),
            revision_brief_json=payload.get("revision_brief") or [],
            requires_human_review=1 if payload.get("requires_human_review") else 0,
            status="completed",
        )
        self.session.add(evaluation)
        self.session.flush()
        return evaluation

    def _create_scene_candidate(
        self,
        *,
        evaluation: WriterEvaluation,
        source: dict[str, Any],
        payload: dict[str, Any],
        actor_ref: str,
    ) -> RevisionCandidate:
        candidate = RevisionCandidate(
            revision_id=f"revision_near_final_{evaluation.object_id}_{uuid.uuid4().hex[:10]}",
            evaluation_id=evaluation.evaluation_id,
            object_type="scene",
            object_id=evaluation.object_id,
            chapter_id=evaluation.chapter_id,
            scene_id=evaluation.scene_id,
            revision_type=NEAR_FINAL_REWRITE_TYPE,
            source_text_ref=source.get("source_text_ref"),
            proposed_text=source.get("content") or "",
            instruction_json=payload.get("revision_brief") or _default_structure_revision_brief(),
            diff_summary_json={
                "failure_class": payload.get("failure_class"),
                "near_final_status": payload.get("near_final_status"),
                "summary": "Near-final acceptance requested a bounded full-scene literary rewrite.",
                "source_text_ref": source.get("source_text_ref"),
            },
            patches_json=[],
            apply_mode="manual_or_regenerate",
            target_text_ref=source.get("source_text_ref"),
            status="candidate",
            created_by=actor_ref or "near_final_acceptance",
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate

    def _supersede_open_scene_candidates(self, scene_id: str) -> None:
        rows = self.session.execute(
            select(RevisionCandidate).where(
                RevisionCandidate.object_type == "scene",
                RevisionCandidate.object_id == scene_id,
                RevisionCandidate.revision_type == NEAR_FINAL_REWRITE_TYPE,
                RevisionCandidate.status == "candidate",
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"

    def _record_attempt(
        self,
        *,
        scene_id: str | None,
        chapter_id: str,
        source_bundle_id: str | None,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=chapter_id,
                step="near_final_acceptance_review" if scene_id else "chapter_near_final_review",
                status=status,
                source_bundle_id=source_bundle_id,
                details_json=details,
            )
        )

    def _chapter_source(self, chapter: ChapterGoal) -> dict[str, Any]:
        memory = self.session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == chapter.chapter_id,
                ChapterMemory.aggregate_stage == "final",
                ChapterMemory.active_flag == 1,
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()
        if memory is not None and (memory.content or "").strip():
            return {
                "content": memory.content,
                "source_text_ref": f"chapter_memory:{memory.row_id}",
                "source_bundle_id": None,
            }
        scenes = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        parts: list[str] = []
        for scene in scenes:
            final = self.session.execute(
                select(FinalScene)
                .where(FinalScene.scene_id == scene.scene_id)
                .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
            ).scalars().first()
            if final is not None and final.content:
                parts.append(final.content)
        content = "\n\n".join(parts).strip()
        if not content:
            raise DomainError("CHAPTER_NEAR_FINAL_SOURCE_MISSING", "chapter near-final review needs aggregate or final scene text", status_code=409)
        return {"content": content, "source_text_ref": f"chapter_assembled:{chapter.chapter_id}", "source_bundle_id": None}

    def _chapter_bundle(self, chapter: ChapterGoal, source: dict[str, Any]) -> dict[str, Any]:
        snapshot = {
            "contract_version": "CHAPTER_NEAR_FINAL_SOURCE_v1",
            "stage_allowlist_name": "chapter_near_final_review",
            "scene_id": "",
            "chapter_id": chapter.chapter_id,
            "source_version_refs": {
                "chapter_goal": chapter.chapter_id,
                "chapter_writer_brief": chapter.chapter_id,
                "source_text_ref": source.get("source_text_ref"),
            },
            "resolved_ref_ids": {},
            "ordered_injections": [
                {"slot": "chapter_goal", "ref_id": chapter.chapter_id, "digest_key": "chapter_goal"},
                {"slot": "chapter_writer_brief", "ref_id": chapter.chapter_id, "digest_key": "chapter_writer_brief"},
                {"slot": "chapter_summary", "ref_id": source.get("source_text_ref"), "digest_key": "chapter_summary"},
            ],
            "inline_digests": {
                "chapter_goal": chapter.chapter_goal or "",
                "chapter_writer_brief": json.dumps(
                    normalize_chapter_writer_brief(chapter.writer_brief_json),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "chapter_summary": _compact_text(source.get("content") or ""),
            },
        }
        snapshot_hash = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        return {
            "bundle_id": f"chapter_near_final_{chapter.chapter_id}_{uuid.uuid4().hex[:10]}",
            "bundle_snapshot_hash": snapshot_hash,
            "snapshot": snapshot,
        }

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)


def _planning_user_prompt(base_prompt: str, *, scene: SceneCard, chapter: ChapterGoal) -> str:
    return "\n".join(
        [
            base_prompt,
            "",
            "## Planning Target",
            f"Scene ID: {scene.scene_id}",
            f"Chapter ID: {chapter.chapter_id}",
            f"POV Character ID: {scene.pov_character_id or ''}",
        ]
    ).strip()


def _acceptance_user_prompt(base_prompt: str, *, source_content: str) -> str:
    return "\n".join([base_prompt, "", "## Draft Under Near-Final Review", source_content]).strip()


def _normalize_character_pressure_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    # 只要求规范字段齐全且非空；多余字段忽略——response_format 是 json_object
    # （非严格 schema），真实模型可能多返解释性键，不应因此判整份产物失败。
    missing = [field for field in CHARACTER_PRESSURE_FIELDS if field not in payload]
    if missing:
        raise ValueError(
            "character pressure payload is missing required fields: " + ", ".join(missing)
        )
    normalized: dict[str, str] = {}
    for field in CHARACTER_PRESSURE_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        normalized[field] = value.strip()
    return normalized


def _normalize_chapter_architecture_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    # 同上：只查规范字段齐全，多余字段忽略（json_object 非严格 schema）。
    missing = [field for field in CHAPTER_ARCHITECTURE_FIELDS if field not in payload]
    if missing:
        raise ValueError(
            "chapter architecture payload is missing required fields: " + ", ".join(missing)
        )
    scalar_fields = ("chapter_promise", "payoff_target", "character_shift", "ending_question")
    normalized: dict[str, Any] = {}
    for field in scalar_fields:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        normalized[field] = value.strip()
    for field in ("escalation_path", "reveal_plan"):
        value = payload.get(field)
        if not isinstance(value, list) or not value:
            raise ValueError(f"{field} must be a non-empty string array")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{field} must contain only non-empty strings")
        normalized[field] = [item.strip() for item in value]
    return {
        "chapter_promise": normalized["chapter_promise"],
        "escalation_path": normalized["escalation_path"],
        "reveal_plan": normalized["reveal_plan"],
        "payoff_target": normalized["payoff_target"],
        "character_shift": normalized["character_shift"],
        "ending_question": normalized["ending_question"],
    }


def _promotion_blockers_from_acceptance(payload: dict[str, Any]) -> list[str]:
    blockers = payload.get("promotion_blockers")
    if isinstance(blockers, list):
        normalized = [str(item).strip() for item in blockers if str(item).strip()]
        if normalized:
            return normalized
    if payload.get("pass_flag"):
        return []
    if payload.get("requires_human_review"):
        return ["human_review_required"]
    if str(payload.get("failure_class") or "") not in AUTOMATED_REWRITE_FAILURE_CLASSES:
        return [str(payload.get("failure_class") or payload.get("near_final_status") or "auto_rewrite_not_eligible")]
    return []


def _normalize_acceptance_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _execution_failure_payload("near-final reviewer returned an invalid payload")
    scores = {str(key): _score(value) for key, value in (payload.get("scores") or {}).items() if _score(value) is not None} if isinstance(payload.get("scores"), dict) else {}
    findings = [item for item in payload.get("findings", []) if isinstance(item, dict)] if isinstance(payload.get("findings"), list) else []
    revision_brief = _revision_brief_list(payload.get("revision_brief"))
    requires_human_review = bool(payload.get("requires_human_review"))
    pass_flag = bool(payload.get("pass_flag"))
    status = _scalar_text(payload.get("near_final_status"))
    if requires_human_review:
        status = "human_review_required"
        pass_flag = False
    elif status not in {"near_final_ready", "revision_required", "human_review_required"}:
        status = "near_final_ready" if pass_flag else "revision_required"
    failure_class = _scalar_text(payload.get("failure_class"))
    if pass_flag:
        failure_class = None
    elif failure_class not in SCENE_FAILURE_CLASSES:
        failure_class = "prose_model_voice"
    overall_score = _score(payload.get("overall_score"))
    return {
        "near_final_status": status,
        "pass_flag": pass_flag and status == "near_final_ready",
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
        "failure_class": failure_class,
        "requires_human_review": requires_human_review or status == "human_review_required",
    }


def _apply_scene_near_final_gates(payload: dict[str, Any], source_content: str) -> dict[str, Any]:
    if _is_test_placeholder_draft(source_content):
        return payload
    missing = _missing_scene_machinery(source_content)
    if not missing:
        model_voice_findings = _model_voice_gate_findings(source_content)
        if not model_voice_findings:
            return payload
        findings = [*model_voice_findings, *(payload.get("findings") or [])]
        revision_brief = list(payload.get("revision_brief") or [])
        revision_brief.insert(
            0,
            {
                "dimension": "model_voice_risk",
                "action": "删掉抽象总结、解释性因果和万能情绪句；把判断改成物件移动、沉默、反问或不可撤回动作。",
                "priority": "high",
            },
        )
        scores = dict(payload.get("scores") or {})
        scores["model_voice_risk"] = min(float(scores.get("model_voice_risk", 0.4) or 0.4), 0.4)
        scores["author_voice_match"] = min(float(scores.get("author_voice_match", 0.55) or 0.55), 0.55)
        overall_score = payload.get("overall_score")
        if payload.get("pass_flag"):
            overall_score = min(float(overall_score or 0.56), 0.56)
        return {
            **payload,
            "near_final_status": "revision_required",
            "pass_flag": False,
            "overall_score": overall_score,
            "scores": scores,
            "failure_class": "prose_model_voice",
            "requires_human_review": False,
            "findings": findings,
            "revision_brief": revision_brief,
        }
    scores = dict(payload.get("scores") or {})
    scores.setdefault("choice_pressure", 0.3)
    scores.setdefault("ending_drive", 0.3)
    findings = list(payload.get("findings") or [])
    findings.insert(
        0,
        {
            "dimension": "story_necessity",
            "severity": "blocker",
            "issue": "场景缺少可见选择、已支付代价或结尾动作。",
            "recommendation": "补足人物必须二选一的动作、选择带来的具体损失，以及能推动下一场的结尾动作。",
            "evidence_excerpt": _compact_text(source_content, limit=120),
            "evidence_location": "scene body",
            "why_it_matters": "准定稿不能只说明事情重要，必须让读者看见人物在压力下改变局面。",
            "missing_machinery": missing,
        },
    )
    revision_brief = list(payload.get("revision_brief") or [])
    if not revision_brief:
        revision_brief = _default_structure_revision_brief()
    overall_score = payload.get("overall_score")
    if payload.get("pass_flag"):
        overall_score = min(float(overall_score or 0.55), 0.54)
    return {
        **payload,
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": overall_score,
        "scores": scores,
        "failure_class": "scene_structure_failure",
        "requires_human_review": False,
        "findings": findings,
        "revision_brief": revision_brief,
    }


def _missing_scene_machinery(content: str) -> list[str]:
    text = content or ""
    missing: list[str] = []
    if not _has_choice(text):
        missing.append("forced_choice")
    if not _has_cost(text):
        missing.append("price_paid")
    if not _has_ending_action(text):
        missing.append("ending_action")
    return missing


def _model_voice_gate_findings(content: str) -> list[dict[str, Any]]:
    text = content or ""
    terms = [
        term
        for term in (
            "某种意义上",
            "一切都变得",
            "她知道",
            "他知道",
            "忽然意识到",
            "突然意识到",
            "解释了一切",
            "解释了所有",
            "前因后果",
            "事情从此不同",
            "意义重大",
        )
        if term in text
    ]
    if not terms:
        return []
    return [
        {
            "dimension": "model_voice_risk",
            "severity": "revision",
            "issue": f"准终稿仍保留模型腔或解释性总结：{'、'.join(terms[:4])}。",
            "recommendation": "把概括性判断改成角色必须承担的动作、物件转移、沉默或反问。",
            "evidence_excerpt": _compact_text(_first_term_window(text, terms[0]), limit=120),
            "evidence_location": "scene body",
            "why_it_matters": "强情节准终稿需要让读者自行从压力中推断意义，不能由叙述替读者总结。",
        }
    ]


def _is_test_placeholder_draft(content: str) -> bool:
    stripped = (content or "").strip()
    return stripped.startswith(("Provider-generated ", "Offline style draft for ", "Offline patched draft for "))


def _has_choice(text: str) -> bool:
    return _contains_any(
        text,
        ("选择", "决定", "公开", "保护", "还是", "不能同时", "二选一", "分成两份", "拆成", "split", "choose", "choice"),
    )


def _has_cost(text: str) -> bool:
    return _contains_any(
        text,
        ("代价", "暴露", "失去", "风险", "追踪", "追缉", "不能", "只剩", "交给", "递给", "藏", "拆成", "分成", "cost", "risk"),
    )


def _has_ending_action(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return False
    if stripped.endswith(("从此不同。", "变得不同。", "很重要。", "意义重大。")):
        return False
    tail = stripped[-80:]
    return _contains_any(
        tail,
        (
            "转身",
            "递给",
            "交给",
            "藏",
            "看见",
            "推开",
            "关上",
            "走进",
            "拿起",
            "按住",
            "沉入",
            "亮起",
            "留下",
            "拆成",
            "分成",
            "turns",
            "hands",
            "leaves",
            "sees",
            "opens",
        ),
    )


def _execution_failure_payload(message: str) -> dict[str, Any]:
    # Wave 2（治理 §5.4/§7.7）：评审自身执行失败不是正文的错——不再返回
    # human_review_required 硬语义；fail + 非自动重写 failure_class，由编排层
    # 按 Q2 警告随稿交付（QC 超时/模型不可用不撤销已有正文）。
    return {
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": None,
        "scores": {},
        "findings": [
            {
                "dimension": "near_final_payload",
                "severity": "revision",
                "issue": message,
                "recommendation": "准定稿验收未能执行；正文照常交付，可修复模型输出后重跑验收。",
                "evidence_excerpt": "",
                "evidence_location": "review execution",
                "why_it_matters": "无效验收不能作为准定稿依据，但也不能撤销已有正文。",
            }
        ],
        "revision_brief": [],
        "failure_class": "fact_blocker",
        "requires_human_review": False,
    }


def _default_structure_revision_brief() -> list[dict[str, str]]:
    return [
        {
            "dimension": "story_necessity",
            "action": "补足人物选择、已支付代价、关系位移和结尾动作，不要用总结句替代场景推进。",
            "priority": "high",
        }
    ]


def _revision_brief_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(item)
        elif isinstance(item, str) and item.strip():
            items.append({"dimension": "near_final", "action": item.strip(), "priority": "medium"})
    return items


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(1.0, float(value)))


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_scalar_text(item) for item in value if _scalar_text(item)]


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _first_term_window(text: str, term: str) -> str:
    index = text.find(term)
    if index < 0:
        return text[:120]
    return text[max(0, index - 36) : index + len(term) + 64]


def _compact_text(text: str, limit: int = 1600) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit].rstrip()}\n...[truncated]..."
