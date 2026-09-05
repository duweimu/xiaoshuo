from __future__ import annotations

import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

_LOGGER = logging.getLogger(__name__)

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    NarrativeEvent,
    QcReport,
    RevisionCandidate,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    VolumeSummary,
    WriterEvaluation,
    utcnow,
)
from novel_system.services import idempotency as _idempotency
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.hash_engine import verify_bundle_snapshot_hash
from novel_system.services.llm_accounting import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    LLMAccountingError,
    LLMAccountingRejected,
    LLMCallContext,
    is_llm_control_plane_failure,
    validate_product_call,
    validate_product_call_ledger,
)
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.llm_task_runner import (
    LLMNodeRunner,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.near_final import (
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.qc_engine import (
    HardQcDecision,
    HardQcEngine,
    SoftQcDecision,
    SoftQcEngine,
)
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_execution import SceneExecutionContractService
from novel_system.services.scene_generation import (
    NeutralGenerationResult,
    SceneGenerationPostprocessError,
    SceneGenerationService,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_run_checkpoint import (
    RUN_CHECKPOINT_ORDER,
    RunCheckpointContext,
    SceneRunCheckpointService,
)
from novel_system.services.scene_archive_checkpoint import SceneArchiveCheckpoint
from novel_system.services.scene_archive_effects import SceneArchiveEffects

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult


class Orchestrator:
    def __init__(
        self,
        session: Session,
        *,
        scene_generation_service: SceneGenerationService | None = None,
        hard_qc_engine: HardQcEngine | None = None,
        soft_qc_engine: SoftQcEngine | None = None,
        planning_service: NearFinalPlanningService | None = None,
        near_final_service: NearFinalAcceptanceService | None = None,
    ) -> None:
        self.session = session
        self.bundle_builder = BundleBuilder(session)
        self.archiver = Archiver(session)
        self.aggregator = Aggregator(session)
        llm_runner = LLMNodeRunner(session)
        self.llm_runner = llm_runner
        self.scene_generation_service = (
            scene_generation_service
            or SceneGenerationService(session, llm_runner=llm_runner)
        )
        self.hard_qc_engine = hard_qc_engine or HardQcEngine(
            session, llm_runner=llm_runner
        )
        self.soft_qc_engine = soft_qc_engine or SoftQcEngine(
            session, llm_runner=llm_runner
        )
        self.scene_blueprint_service = SceneBlueprintService(
            session, llm_runner=llm_runner
        )
        self.execution_contract_service = SceneExecutionContractService(session)
        self.planning_service = planning_service or NearFinalPlanningService(
            session, llm_runner=llm_runner
        )
        self.near_final_service = near_final_service or NearFinalAcceptanceService(
            session, llm_runner=llm_runner
        )
        # Per-run execution ownership (execution_id / run_job_id /
        # checkpoint_service / lease_renewer) lives on the checkpoint
        # kernel context and is exposed via forwarding properties below.
        # lease TTL 经 lambda 在调用时解析模块属性，保住测试对
        # idempotency.owner_lease_ttl_seconds 的打桩。
        self._ckpt = RunCheckpointContext(
            session,
            lease_ttl_seconds=lambda: _idempotency.owner_lease_ttl_seconds(),
        )

    # ------------------------------------------------------------------
    # Per-run checkpoint kernel state lives on RunCheckpointContext; these
    # forwarding properties keep the four fields readable/writable on the
    # orchestrator itself (run entrypoints and tests set them directly, and
    # per-call workers such as SceneArchiveEffects read their current value).
    @property
    def _execution_id(self) -> str | None:
        return self._ckpt._execution_id

    @_execution_id.setter
    def _execution_id(self, value: str | None) -> None:
        self._ckpt._execution_id = value

    @property
    def _run_job_id(self) -> str | None:
        return self._ckpt._run_job_id

    @_run_job_id.setter
    def _run_job_id(self, value: str | None) -> None:
        self._ckpt._run_job_id = value

    @property
    def _checkpoint_service(self) -> SceneRunCheckpointService | None:
        return self._ckpt._checkpoint_service

    @_checkpoint_service.setter
    def _checkpoint_service(self, value: SceneRunCheckpointService | None) -> None:
        self._ckpt._checkpoint_service = value

    @property
    def _lease_renewer(self):
        return self._ckpt._lease_renewer

    @_lease_renewer.setter
    def _lease_renewer(self, value) -> None:
        self._ckpt._lease_renewer = value

    def run_scene(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        author_note = normalize_author_note(author_note)
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()

        effective_execution_id = execution_id or f"direct:{scene_id}:{uuid4().hex}"
        checkpoints = SceneRunCheckpointService(self.session)
        claim = checkpoints.acquire_execution(scene_id, effective_execution_id)
        if claim.last_node == "archived":
            self._execution_id = effective_execution_id
            self._run_job_id = run_job_id
            self._checkpoint_service = checkpoints
            try:
                self._assert_author_note_matches_bundle(
                    self._load_checkpoint_bundle(scene_id),
                    author_note,
                    scene_id=scene_id,
                )
                final_scene = self._load_archived_checkpoint(scene_id)
                if claim.status != "completed":
                    checkpoints.mark_completed(scene_id, effective_execution_id)
                    self.session.commit()
                return self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": state.current_bundle_id,
                        "current_bundle_hash": state.current_bundle_hash,
                        "current_final_scene_row_id": final_scene.row_id,
                    },
                )
            finally:
                self._execution_id = None
                self._run_job_id = None
                self._checkpoint_service = None
        self._prepare_state_for_run(state, new_execution=not claim.resumed)
        state.run_policy = run_policy
        self.session.commit()

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            self._raise_if_run_cancelled()
            result = self._run_scene_pipeline(
                scene_id,
                author_note=author_note,
                run_policy=run_policy,
            )
            if result.get("scene_status") in {
                "archived",
                "quality_warning_pending_acceptance",
            }:
                # Strict mode deliberately stops with a valid draft awaiting the
                # author's Q2/Q3 acceptance.  It is a successful execution
                # terminal, not a failure/retry checkpoint.
                checkpoints.mark_completed(scene_id, effective_execution_id)
            elif result.get("scene_status") == "awaiting_candidate_selection":
                checkpoints.mark_waiting_selection(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except DomainError as exc:
            if exc.code == "RUN_JOB_CANCELLED_BY_AUTHOR":
                checkpoints.mark_cancelled(scene_id, effective_execution_id)
                self.session.commit()
                raise
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None

    @staticmethod
    def _assert_author_note_matches_bundle(
        bundle: dict[str, Any],
        author_note: str | None,
        *,
        scene_id: str,
    ) -> None:
        expected_note = str(author_note or "").strip()
        actual_note = str(
            ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
                "author_instruction"
            )
            or ""
        )
        if actual_note != expected_note:
            raise DomainError(
                "RUN_INPUT_MISMATCH",
                "author instruction differs from the frozen run checkpoint",
                status_code=409,
                details={"scene_id": scene_id, "field": "author_note"},
            )

    def _run_scene_pipeline(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
    ) -> dict:
        # Wave 2/3（治理 §5.4/§5.5）：run_policy 现已落列（Wave 3 迁移 0062）。
        # reliable（默认）：Q2/Q3 警告随稿归档；strict：存在 Q2 时停在可归档的
        # quality_warning，由作者经 adopt-current 显式接受；auto 保留（按
        # criticality 决策），当前按 reliable 处理。Q0/Q1 阻断与模式无关。
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            # FE 目录直接建的场景没有运行时状态行（scenes POST 才会建）：按同一约定补建
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()
        contract = self.execution_contract_service.get_or_create(
            scene_id, actor_ref="orchestrator"
        )
        if contract.status != "active":
            detail_reason = "scene execution contract is not ready for drafting"
            if contract.status == "blocked":
                missing_fields = list(contract.missing_fields_json or [])
                detail_reason = "scene execution contract is missing required fields"
                raise DomainError(
                    "SCENE_EXECUTION_CONTRACT_BLOCKED",
                    detail_reason,
                    status_code=409,
                    details={
                        "scene_id": scene_id,
                        "execution_contract_id": contract.contract_id,
                        "status": contract.status,
                        "missing_fields": missing_fields,
                    },
                )
            raise DomainError(
                "SCENE_EXECUTION_CONTRACT_BLOCKED",
                detail_reason,
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "execution_contract_id": contract.contract_id,
                    "status": contract.status,
                    "missing_fields": list(contract.missing_fields_json or []),
                },
            )
        # Wave 3（§6.1）：本次运行的生效策略落列（预算/使用量不在 _prepare 重置，§7.12）
        state.run_policy = run_policy

        # Wave 3（§4.6/§5.5）：确立场景 token 预算 = 5 × 单发基线（已设不覆盖）
        from novel_system.services import scene_budget

        if not self._checkpoint_reached("budget_ready"):
            state = scene_budget.ensure_scene_budget_initialized(self.session, scene_id)
            self._save_run_checkpoint(
                "budget_ready",
                artifact_refs={"scene_token_budget": state.scene_token_budget},
                artifact_hashes={
                    "budget_basis": self._json_hash(state.scene_budget_basis_json or {})
                },
            )
        else:
            self._validate_budget_checkpoint(state)

        planning_progress = self._planning_checkpoint_progress()
        if planning_progress < 3:
            resume_planning_artifacts: dict[str, GenerationPlanningArtifact] = {}
            if planning_progress >= 0:
                self._validate_planning_prefix(scene_id, through=planning_progress)
                blueprint = self._load_planning_blueprint_checkpoint(scene_id)
                if planning_progress >= 1:
                    resume_planning_artifacts["chapter_architecture"] = (
                        self._load_planning_artifact_checkpoint(
                            scene_id,
                            prefix="planning_chapter_architecture",
                            expected_step_key="planning:chapter_architecture",
                            expected_kind="chapter_architecture",
                        )
                    )
                if planning_progress >= 2:
                    resume_planning_artifacts["character_pressure"] = (
                        self._load_planning_artifact_checkpoint(
                            scene_id,
                            prefix="planning_character_pressure",
                            expected_step_key="planning:character_pressure",
                            expected_kind="character_pressure",
                        )
                    )
            else:
                existing_blueprint = self.scene_blueprint_service.latest(scene_id)
                blueprint_reused = existing_blueprint is not None
                if existing_blueprint is None:
                    self._reconcile_execution_step("scene_blueprint")
                blueprint = self.scene_blueprint_service.ensure_for_scene(
                    scene_id,
                    execution_step_key="scene_blueprint",
                )
                blueprint_payload = self.scene_blueprint_service.serialize(blueprint)
                assert blueprint_payload is not None
                blueprint_refs = self._planning_artifact_refs(
                    prefix="planning_scene_blueprint",
                    serialized=blueprint_payload,
                    execution_step_key="scene_blueprint",
                    reused=blueprint_reused,
                )
                self._save_run_checkpoint(
                    "planning_ready",
                    sub_index=0,
                    artifact_refs=blueprint_refs
                    | {"scene_blueprint": blueprint_payload},
                    artifact_hashes={
                        "planning_scene_blueprint": self._json_hash(blueprint_payload),
                        "planning_scene_blueprint_provenance": self._json_hash(
                            self._planning_provenance(
                                blueprint_refs, "planning_scene_blueprint"
                            )
                        ),
                        "scene_blueprint": self._json_hash(blueprint_payload),
                    },
                    strategy="planning_in_progress",
                )

            def _planning_artifact_committed(
                kind: str,
                serialized: dict[str, Any],
                reused: bool,
            ) -> None:
                substeps = {
                    "chapter_architecture": (
                        1,
                        "planning_chapter_architecture",
                        "planning:chapter_architecture",
                    ),
                    "character_pressure": (
                        2,
                        "planning_character_pressure",
                        "planning:character_pressure",
                    ),
                }
                if kind not in substeps:
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        f"unknown planning artifact callback: {kind}",
                        status_code=409,
                    )
                sub_index, prefix, step_key = substeps[kind]
                current_progress = self._planning_checkpoint_progress()
                if current_progress >= sub_index:
                    checkpoint_row = self._load_planning_artifact_checkpoint(
                        scene_id,
                        prefix=prefix,
                        expected_step_key=step_key,
                        expected_kind=kind,
                    )
                    if checkpoint_row.row_id != serialized.get("row_id"):
                        raise DomainError(
                            "RUN_CHECKPOINT_CORRUPT",
                            f"{kind} callback differs from durable planning checkpoint",
                            status_code=409,
                        )
                    return
                artifact_refs = self._planning_artifact_refs(
                    prefix=prefix,
                    serialized=serialized,
                    execution_step_key=step_key,
                    reused=reused,
                )
                self._save_run_checkpoint(
                    "planning_ready",
                    sub_index=sub_index,
                    artifact_refs=artifact_refs | {prefix: serialized},
                    artifact_hashes={
                        prefix: self._json_hash(serialized),
                        f"{prefix}_provenance": self._json_hash(
                            self._planning_provenance(artifact_refs, prefix)
                        ),
                    },
                    strategy="planning_in_progress",
                )

            planning = self.planning_service.ensure_scene_planning(
                scene_id,
                step_reconciler=self._reconcile_execution_step,
                artifact_committed=_planning_artifact_committed,
                resume_artifacts=resume_planning_artifacts,
            )
            blueprint_payload = self.scene_blueprint_service.serialize(blueprint)
            assert blueprint_payload is not None
            self._save_run_checkpoint(
                "planning_ready",
                sub_index=3,
                artifact_refs={
                    "planning": planning,
                    "scene_blueprint": blueprint_payload,
                },
                artifact_hashes={
                    "planning": self._json_hash(planning),
                    "scene_blueprint": self._json_hash(blueprint_payload),
                },
                strategy="planning_complete",
            )
        else:
            planning = self._load_planning_checkpoint(scene_id)

        if not self._checkpoint_reached("bundle_ready"):
            bundle = self.bundle_builder.build(scene_id, "P2", author_note=author_note)
            self._save_run_checkpoint(
                "bundle_ready",
                artifact_refs={"bundle_id": bundle["bundle_id"]},
                artifact_hashes={"bundle": bundle["bundle_snapshot_hash"]},
            )
        else:
            bundle = self._load_checkpoint_bundle(scene_id)
            self._assert_author_note_matches_bundle(
                bundle, author_note, scene_id=scene_id
            )

        from novel_system.services.scene_criticality import classify_scene_with_context

        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        # §6.4 / §16：chapter_seq、连续过渡计数、constraint_intensity 的上下文推导
        # 统一收敛在 classify_scene_with_context——与崩溃续跑同一入口，判定不得分叉。
        criticality = classify_scene_with_context(self.session, scene)
        _LOGGER.info(
            "scene %s criticality=%s reasons=%s best_of_n=%d",
            scene_id,
            criticality.level,
            criticality.reasons,
            criticality.best_of_n,
        )
        # §6 Defect D: persist criticality classification for API exposure
        state.criticality_level = criticality.level
        state.criticality_reasons_json = criticality.reasons

        # §10 / §12: pre-generation tension + theme diagnostics

        if self._checkpoint_reached("neutral_ready"):
            neutral_generation = self._load_checkpoint_draft(
                scene_id,
                ref_key="neutral_draft_row_id",
                expected_stage="neutral_draft",
                expected_node_at_least="neutral_ready",
                result_type="neutral",
            )
        else:
            self._reconcile_execution_step("neutral_draft")
            neutral_generation = self.scene_generation_service.generate_neutral_draft(
                scene_id,
                bundle,
                author_note=author_note,
            )
            self._save_run_checkpoint(
                "neutral_ready",
                artifact_refs={
                    "neutral_draft_row_id": neutral_generation.row_id,
                    "neutral_llm_call_id": neutral_generation.llm_call_id,
                    "neutral_execution_step_key": neutral_generation.execution_step_key
                    or "neutral_draft",
                    "neutral_artifact_execution_id": self._execution_id,
                    "bundle_id": neutral_generation.bundle_id,
                },
                artifact_hashes={
                    "draft": self._text_hash(neutral_generation.content),
                    "bundle": neutral_generation.bundle_hash,
                },
            )
        neutral_content = neutral_generation.content

        if self._checkpoint_reached("hard_qc_ready"):
            hard_qc = self._load_hard_qc_checkpoint(scene_id)
        else:
            self._reconcile_execution_step("hard_qc:0")
            hard_qc = self.hard_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                neutral_draft_row_id=neutral_generation.row_id,
                neutral_content=neutral_content,
                execution_step_key="hard_qc:0",
            )
            # 键序不变：前六键沿用 _hard_qc_result_payload 的顺序，哈希才可复现。
            hard_decision = {
                **self._hard_qc_result_payload(hard_qc),
                "should_continue": hard_qc.should_continue,
                "llm_call_id": hard_qc.llm_call_id,
                "execution_step_key": hard_qc.execution_step_key,
            }
            self.session.flush()
            hard_report = self.session.get(QcReport, hard_qc.qc_report_id)
            if hard_report is None:
                self._raise_checkpoint_output_missing(row_id=hard_qc.qc_report_id)
            self._save_run_checkpoint(
                "hard_qc_ready",
                artifact_refs={
                    **hard_decision,
                    "hard_qc_source_draft_row_id": neutral_generation.row_id,
                    "hard_qc_bundle_id": bundle["bundle_id"],
                    "hard_qc_llm_call_id": hard_qc.llm_call_id,
                    "hard_qc_execution_step_key": hard_qc.execution_step_key,
                    "hard_qc_artifact_execution_id": self._execution_id,
                },
                artifact_hashes={
                    "hard_qc_decision": self._json_hash(hard_decision),
                    "hard_qc_report": self._json_hash(
                        self._qc_report_snapshot(hard_report)
                    ),
                },
                strategy=hard_qc.resolution_code,
                branch=hard_qc.branch,
            )
        if not hard_qc.should_continue:
            self.session.flush()
            # Wave 2 项 5：所有早退结果都携带 author_state 契约（含 latest_valid 指针）
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": state.scene_status,
                    "current_bundle_id": bundle["bundle_id"],
                    "current_bundle_hash": bundle["bundle_snapshot_hash"],
                    "current_qc_report_id": state.current_qc_report_id,
                    "current_human_review_event_id": state.current_human_review_event_id,
                    "hard_qc": self._hard_qc_result_payload(hard_qc),
                },
            )

        # Wave 3（§5.5 成本分配）：初始 N（关键 3/标准 2/过渡 1），低分散在预算内
        # 渐进补候选至上限（关键 5/标准 3）——不再一次生成后整批无上限重试。
        self._best_of_n_policy_cap = None
        n_candidates = self._best_of_n_count(contract, criticality=criticality)
        authorized_max_candidates = self._best_of_n_max_count(
            criticality=criticality,
            initial_count=n_candidates,
        )
        candidate_summaries: list[dict[str, Any]] = []
        if self._checkpoint_reached("style_ready"):
            candidates = self._load_style_checkpoint_candidates(scene_id)
            style_generation = candidates[0]
        else:
            style_work_items = self._load_partial_style_work_items(
                scene_id,
                expected_initial_count=n_candidates,
            )
            resume_bases, resume_products = self._style_resume_products(
                style_work_items, scene_id=scene_id
            )

            def _style_product_checkpoint(
                slot_key: str,
                phase: str,
                product: StyleGenerationResult,
                metadata: dict[str, Any],
            ) -> None:
                self._update_style_work_item(
                    style_work_items,
                    slot_key=slot_key,
                    phase=phase,
                    product=product,
                    metadata=metadata,
                    neutral_draft_row_id=neutral_generation.row_id,
                )
                completed = [
                    item["final"]
                    for item in style_work_items
                    if item.get("final") is not None
                ]
                slot_order = metadata.get("slot_order")
                if not isinstance(slot_order, int):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "style product slot order is invalid",
                        status_code=409,
                    )
                self._save_run_checkpoint(
                    "hard_qc_ready",
                    sub_index=slot_order * 2 + (1 if phase == "final" else 0),
                    artifact_refs={
                        "style_work_items": deepcopy(style_work_items),
                        "style_initial_candidate_count": n_candidates,
                        "style_candidate_row_ids": [
                            item["row_id"] for item in completed
                        ],
                        "style_candidate_llm_call_ids": [
                            item["llm_call_id"] for item in completed
                        ],
                        "style_candidate_step_keys": [
                            item["execution_step_key"] for item in completed
                        ],
                        "style_candidate_execution_ids": [
                            item["artifact_execution_id"] for item in completed
                        ],
                    },
                    artifact_hashes={
                        "style_work_items": self._json_hash(style_work_items)
                    },
                    strategy=(
                        "best_of_n_in_progress"
                        if n_candidates > 1
                        else "single_in_progress"
                    ),
                )

            if n_candidates > 1:
                candidates = (
                    self.scene_generation_service.generate_style_draft_candidates(
                        scene_id,
                        bundle,
                        neutral_draft_row_id=neutral_generation.row_id,
                        neutral_content=neutral_content,
                        author_note=author_note,
                        n_candidates=n_candidates,
                        max_candidates=authorized_max_candidates,
                        step_reconciler=self._reconcile_execution_step,
                        resume_bases=resume_bases,
                        resume_products=resume_products,
                        product_callback=_style_product_checkpoint,
                    )
                )
            else:
                if "initial:0" not in resume_bases:
                    self._reconcile_execution_step("style_draft:0")
                style_generation = self.scene_generation_service.generate_style_draft(
                    scene_id,
                    bundle,
                    neutral_draft_row_id=neutral_generation.row_id,
                    neutral_content=neutral_content,
                    author_note=author_note,
                    resume_base=resume_bases.get("initial:0"),
                    product_callback=_style_product_checkpoint,
                    step_reconciler=self._reconcile_execution_step,
                )
                candidates = [style_generation]
            style_generation = candidates[0]

        # 标准场景：机器下限 + 受约束风格信号继续管线；关键场景在下方暂停终选。
        # 从 checkpoint 恢复时也重建同一摘要，避免审计信息因一次进程中断消失。
        from novel_system.services.literary_quality import adversarial_rank_score

        for idx, cand in enumerate(candidates):
            ranking = cand.ranking_audit or {}
            cand_score = ranking.get("quality_score")
            if not isinstance(cand_score, (int, float)):
                cand_score = adversarial_rank_score(cand.content)
            rerank_audit = (
                ranking.get("rerank") if isinstance(ranking.get("rerank"), dict) else {}
            )
            candidate_summaries.append(
                {
                    "row_id": cand.row_id,
                    "rank": idx,
                    "adversarial_score": round(cand_score, 3),
                    "style_score": ranking.get("style_score"),
                    "style_confidence": ranking.get("style_confidence"),
                    "style_rerank_mode": rerank_audit.get("applied_mode"),
                    "plagiarism_passed": ranking.get("plagiarism_passed"),
                    "selection_reason": ranking.get(
                        "selection_reason", "quality_order"
                    ),
                    "content_preview": (cand.content or "")[:300],
                    "selected": idx == 0,
                }
            )
        if not self._checkpoint_reached("style_ready"):
            style_candidate_rankings = [
                candidate.ranking_audit for candidate in candidates
            ]
            self._save_run_checkpoint(
                "style_ready",
                artifact_refs={
                    "style_draft_row_id": style_generation.row_id,
                    "candidate_row_ids": [candidate.row_id for candidate in candidates],
                    "llm_call_ids": [candidate.llm_call_id for candidate in candidates],
                    "style_execution_step_keys": [
                        candidate.execution_step_key for candidate in candidates
                    ],
                    "style_artifact_execution_ids": [
                        candidate.artifact_execution_id or self._execution_id
                        for candidate in candidates
                    ],
                    "style_llm_call_id": style_generation.llm_call_id,
                    "style_execution_step_key": style_generation.execution_step_key,
                    "style_artifact_execution_id": style_generation.artifact_execution_id
                    or self._execution_id,
                    "bundle_id": style_generation.bundle_id,
                    "style_candidate_rankings": style_candidate_rankings,
                },
                artifact_hashes={
                    "selected_draft": self._text_hash(style_generation.content),
                    "bundle": style_generation.bundle_hash,
                    "style_candidate_rankings": self._json_hash(
                        style_candidate_rankings
                    ),
                    **{
                        f"style_ready_candidate_{index}": self._text_hash(
                            candidate.content
                        )
                        for index, candidate in enumerate(candidates)
                    },
                },
                strategy="best_of_n" if n_candidates > 1 else "single",
            )

        hard_qc_payload = self._hard_qc_result_payload(hard_qc)

        # Wave 3（§5.5）：关键场景在候选生成后暂停编排——确定性坏稿淘汰 →
        # 匿名终选 gate；作者选择后经 resume-after-selection 从批判修订/QC 继续。
        # 「§6.3 终选决定质量上界，归人」从推荐信号升级为强制暂停。
        if criticality.human_gate and len(candidates) > 1:
            offered_row_ids = self._offer_candidates_for_selection(
                scene, state, bundle, candidates
            )
            if offered_row_ids is not None:
                content_by_row_id = {
                    candidate.row_id: candidate.content for candidate in candidates
                }
                self._save_run_checkpoint(
                    "selection_wait",
                    artifact_refs={
                        "selection_event_id": state.current_human_review_event_id,
                        "selection_candidate_row_ids": offered_row_ids,
                    },
                    artifact_hashes={
                        f"selection_candidate_{index}": self._text_hash(
                            content_by_row_id[row_id]
                        )
                        for index, row_id in enumerate(offered_row_ids)
                    },
                    strategy="human_selection",
                )
                return self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": bundle["bundle_id"],
                        "current_bundle_hash": bundle["bundle_snapshot_hash"],
                        "current_qc_report_id": state.current_qc_report_id,
                        "current_human_review_event_id": state.current_human_review_event_id,
                        "hard_qc": hard_qc_payload,
                        "planning": planning,
                        "run_policy": run_policy,
                        # 盲化：暂停响应只报数量，不带分数/预览（候选经盲化视图取用）
                        "candidate_count": len(offered_row_ids),
                        "candidate_selection_required": True,
                    },
                )

        return self._finalize_after_style(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            planning=planning,
            hard_qc_payload=hard_qc_payload,
            style_generation=style_generation,
            candidate_summaries=candidate_summaries if candidate_summaries else None,
            candidates_total=len(candidates),
            run_policy=run_policy,
        )

    def _finalize_after_style(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        contract,
        bundle: dict[str, Any],
        criticality,
        planning,
        hard_qc_payload: dict[str, Any],
        style_generation,
        candidate_summaries: list[dict[str, Any]] | None,
        candidates_total: int,
        run_policy: str,
    ) -> dict:
        """§5.5 顺序的后半段：批判修订 → 软 QC → near-final → 严格停点 → 归档。

        run_scene 与 resume_after_selection 共用。可选支出（LLM 批判、补丁、
        near-final 重写）过预算闸（§5.8 预算耗尽停止新调用、交付最佳稿）；
        候选补满上限的场按 §5.5 固定预算优先级放弃 LLM 批判与补丁。
        """
        from novel_system.services import scene_budget

        scene_id = scene.scene_id
        strict_mode = run_policy == "strict"
        gave_up_optional = (
            criticality is not None
            and criticality.max_best_of_n > 1
            and candidates_total >= criticality.max_best_of_n
        )

        def _optional_spend_allowed() -> bool:
            return (not gave_up_optional) and scene_budget.can_spend(
                state, scene_budget.budget_unit(state)
            )

        if self._near_final_checkpoint_progress() >= 3:
            return self._archive_near_final_checkpoint(
                scene=scene,
                state=state,
                contract=contract,
                bundle=bundle,
                hard_qc_payload=hard_qc_payload,
                planning=planning,
                candidate_summaries=candidate_summaries,
                run_policy=run_policy,
            )

        # §8 reflexion-style auto-critique pass (after best-of-N selection, before soft QC).
        # Default: rule-based pass only. Opt-in (NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED +
        # llm_enabled) layers the independent LLM editor critic on top — degrades to
        # rule-only on any runner error, never blocks (blueprint §8 + §15 honest-bounds).
        soft_qc, final_generation = self._ensure_soft_qc_subcheckpoints(
            scene=scene,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            selected_style_generation=style_generation,
            optional_spend_allowed=_optional_spend_allowed,
        )
        if soft_qc.branch == "human_review_required":
            # Wave 2：软 QC 只在 verified Q0/Q1 时才会走到这里（LLM-only 意见已在
            # 引擎内降级为 waive）——这是真硬阻断，正文保留、契约随行。
            self.session.flush()
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": state.scene_status,
                    "current_bundle_id": bundle["bundle_id"],
                    "current_bundle_hash": bundle["bundle_snapshot_hash"],
                    "current_qc_report_id": state.current_qc_report_id,
                    "current_human_review_event_id": state.current_human_review_event_id,
                    "hard_qc": hard_qc_payload,
                    "soft_qc": self._soft_qc_result_payload(soft_qc),
                    "planning": planning,
                },
            )

        near_final, final_generation, rewrite_count, near_final_skip_reason = (
            self._ensure_near_final_subcheckpoints(
                scene=scene,
                bundle=bundle,
                source_generation=final_generation,
                optional_spend_allowed=_optional_spend_allowed,
            )
        )
        near_final_payload = self._near_final_result_payload(
            near_final, rewrite_count=rewrite_count
        )
        # Wave 2（§5.4 / Wave 2 项 4）：near-final 是 LLM 提案层（Q2/Q3）——达自动
        # 修订上限（软补丁 ≤1 + 准终稿重写 ≤1 = 2 次）后不再断头，交付当前最好稿；
        # 其 requires_human_review 亦为提案，不得产生 human_review_required 断头。
        near_final_warnings = self._near_final_warning_findings(near_final)

        # 严格模式停点：存在 Q2 级警告（软 QC 报告或 near-final 未过）时不自动归档，
        # 停在可归档的 quality_warning，由作者经 adopt-current 显式接受（留审计）。
        if strict_mode:
            strict_warnings = self._collect_q2_warnings(state, near_final_warnings)
            if strict_warnings:
                strict_gate = FinalTextGateService(self.session).evaluate(
                    scene_id=scene_id,
                    content=final_generation.content,
                    source_bundle_id=bundle["bundle_id"],
                )
                state.scene_status = "quality_warning_pending_acceptance"
                self.session.flush()
                result = self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": bundle["bundle_id"],
                        "current_bundle_hash": bundle["bundle_snapshot_hash"],
                        "current_qc_report_id": state.current_qc_report_id,
                        "current_human_review_event_id": state.current_human_review_event_id,
                        "hard_qc": hard_qc_payload,
                        "soft_qc": self._soft_qc_result_payload(soft_qc),
                        "planning": planning,
                        "near_final": near_final_payload,
                        "run_policy": run_policy,
                    },
                )
                result["quality_warnings"] = self._merged_warnings(
                    result.get("quality_warnings"), near_final_warnings
                )
                self._apply_finality(
                    result, gate_summary=strict_gate, warnings=strict_warnings
                )
                return result

        # Wave 3：旧的 near-final 后置 critical_scene_human_gate 被前移的候选终选
        # gate 取代（§5.5 顺序——终选在批判修订/硬检查之前，此处不再二次人工门）。

        final_row_id = versioned_scene_artifact_id("final_scene", scene_id, bundle)
        soft_risk_acceptance_event_id = self._soft_risk_acceptance_event_id(soft_qc)
        carry_notes_json = (
            self._carry_notes_from_report(soft_qc.qc_report_id)
            if soft_qc.branch == "waive"
            else []
        )
        if soft_risk_acceptance_event_id:
            carry_notes_json.append(
                {
                    "kind": "soft_risk_acceptance",
                    "human_review_event_id": soft_risk_acceptance_event_id,
                    "qc_report_id": soft_qc.qc_report_id,
                }
            )
        if not near_final.get("pass_flag"):
            # Wave 2：达修订上限仍未过的 near-final 意见随稿归档留痕（作者行动建议）
            carry_notes_json.append(
                {
                    "kind": "near_final_unresolved",
                    "failure_class": near_final.get("failure_class"),
                    "rewrite_count": rewrite_count,
                    "recommended_action": "author_review_optional_fix",
                }
            )
        self.session.add(
            FinalScene(
                row_id=final_row_id,
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                content=final_generation.content,
                status="near_final_ready",
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=final_generation.llm_call_id,
            )
        )
        self.session.flush()
        state.current_final_scene_row_id = final_row_id
        finalize_details = {
            "source_style_draft_row_id": final_generation.row_id,
            "source_qc_report_id": soft_qc.qc_report_id,
            "final_generation_llm_call_id": final_generation.llm_call_id,
        }
        if soft_risk_acceptance_event_id:
            finalize_details["soft_risk_acceptance_event_id"] = (
                soft_risk_acceptance_event_id
            )
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                step="finalize",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=finalize_details,
            )
        )
        self.session.flush()

        near_final_evaluation_step_key = f"near_final_acceptance:{rewrite_count}"
        near_completion = {
            "final_evaluation_round": rewrite_count,
            "rewrite_count": rewrite_count,
            "skip_reason": near_final_skip_reason,
            "branch": near_final.get("near_final_status"),
            "evaluation_id": near_final.get("evaluation_id"),
            "source_draft_row_id": final_generation.row_id,
            "final_scene_row_id": final_row_id,
        }
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=3,
            artifact_refs={
                "final_scene_row_id": final_row_id,
                "near_final_source_draft_row_id": final_generation.row_id,
                "final_generation_llm_call_id": final_generation.llm_call_id,
                "final_generation_execution_step_key": final_generation.execution_step_key,
                "final_generation_artifact_execution_id": (
                    final_generation.artifact_execution_id or self._execution_id
                ),
                "near_final_evaluation_id": near_final.get("evaluation_id"),
                "near_final_evaluation_llm_call_id": self._writer_evaluation_llm_call_id(
                    near_final.get("evaluation_id")
                ),
                "near_final_evaluation_step_key": near_final_evaluation_step_key,
                "near_final_evaluation_execution_id": self._execution_id,
                "near_final": near_final_payload,
                "carry_notes": carry_notes_json,
                "soft_qc_report_id": soft_qc.qc_report_id,
                "near_final_branch": near_final.get("near_final_status"),
                "near_final_skip_reason": near_final_skip_reason,
                "near_final_rewrite_count": rewrite_count,
                "near_completion": near_completion,
            },
            artifact_hashes={
                "final_scene": self._text_hash(final_generation.content),
                "near_final": self._json_hash(near_final_payload),
                "carry_notes": self._json_hash(carry_notes_json),
                "near_completion": self._json_hash(near_completion),
            },
            branch=str(near_final.get("near_final_status") or "near_final_ready"),
        )

        return self._archive_near_final_checkpoint(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            hard_qc_payload=hard_qc_payload,
            planning=planning,
            candidate_summaries=candidate_summaries,
            run_policy=run_policy,
        )

    def _archive_checkpoint(self) -> SceneArchiveCheckpoint:
        """Build the archive-checkpoint worker for the CURRENT run.

        Constructed at call time — never cached — and every cross-call inside
        the cluster dispatches back through ``self`` so instance-level
        overrides (a test seam) keep intercepting sibling stage methods.
        """
        return SceneArchiveCheckpoint(self.session, host=self)

    def _archive_near_final_checkpoint(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        contract,
        bundle: dict[str, Any],
        hard_qc_payload: dict[str, Any],
        planning,
        candidate_summaries: list[dict[str, Any]] | None,
        run_policy: str,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._archive_near_final_checkpoint(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            hard_qc_payload=hard_qc_payload,
            planning=planning,
            candidate_summaries=candidate_summaries,
            run_policy=run_policy,
        )

    def _checkpoint_reached(self, node_key: str) -> bool:
        return self._ckpt._checkpoint_reached(node_key)

    def _checkpoint_artifact(self, key: str, *, expected_node_at_least: str) -> Any:
        return self._ckpt._checkpoint_artifact(
            key, expected_node_at_least=expected_node_at_least
        )

    def _save_run_checkpoint(
        self,
        node_key: str,
        *,
        artifact_refs: dict[str, Any] | None = None,
        artifact_hashes: dict[str, str] | None = None,
        sub_index: int | None = None,
        strategy: str | None = None,
        branch: str | None = None,
    ) -> None:
        self._ckpt._save_run_checkpoint(
            node_key,
            artifact_refs=artifact_refs,
            artifact_hashes=artifact_hashes,
            sub_index=sub_index,
            strategy=strategy,
            branch=branch,
        )

    def _reconcile_execution_step(
        self,
        execution_step_key: str,
        *,
        chapter_scope: bool = False,
    ) -> None:
        self._ckpt._reconcile_execution_step(
            execution_step_key, chapter_scope=chapter_scope
        )

    def _validate_checkpoint_llm_output(
        self,
        *,
        scene_id: str,
        llm_call_id: Any,
        execution_step_key: Any,
        execution_id: str | None = None,
        allowed_accounting_statuses: tuple[str, ...] = ("settled",),
        allow_local_rejected_output: bool = False,
    ) -> LlmCall:
        return self._ckpt._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            execution_id=execution_id,
            allowed_accounting_statuses=allowed_accounting_statuses,
            allow_local_rejected_output=allow_local_rejected_output,
        )

    def _validate_artifact_execution_owner(self, owner_execution_id: Any) -> str:
        return self._ckpt._validate_artifact_execution_owner(owner_execution_id)

    def _checkpoint_execution_owner_matches(
        self,
        execution_id: Any,
        run_job_id: Any,
    ) -> bool:
        return self._ckpt._checkpoint_execution_owner_matches(execution_id, run_job_id)

    def _renew_owner_lease(self, *, lease_seconds: int) -> None:
        self._ckpt._renew_owner_lease(lease_seconds=lease_seconds)

    def _raise_if_run_cancelled(self) -> None:
        self._ckpt._raise_if_run_cancelled()

    def _validate_budget_checkpoint(self, state: SceneRunState) -> None:
        from novel_system.services.scene_budget import (
            audited_scene_budget_prefixes,
            ensure_scene_budget_initialized,
        )

        try:
            state = ensure_scene_budget_initialized(self.session, state.scene_id)
        except ValueError as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "budget state cannot be reconstructed from its immutable basis and topup audit",
                status_code=409,
            ) from exc
        expected_budget = self._checkpoint_artifact(
            "scene_token_budget",
            expected_node_at_least="budget_ready",
        )
        values = {
            "scene_token_budget": state.scene_token_budget,
            "scene_tokens_used": state.scene_tokens_used,
            "scene_tokens_reserved": state.scene_tokens_reserved,
            "attempt_budget": state.attempt_budget,
            "total_attempt_count": state.total_attempt_count,
            "provider_attempt_budget": state.provider_attempt_budget,
            "provider_attempts_used": state.provider_attempts_used,
        }
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values.values()
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "budget checkpoint counters are invalid",
                status_code=409,
            )
        budget_prefixes = audited_scene_budget_prefixes(self.session, state)
        if (
            expected_budget not in budget_prefixes
            or state.scene_tokens_used + state.scene_tokens_reserved
            > state.scene_token_budget
            or state.total_attempt_count > state.attempt_budget
            or state.provider_attempts_used > state.provider_attempt_budget
            or not isinstance(state.scene_budget_basis_json, dict)
            or self._json_hash(state.scene_budget_basis_json)
            != self._checkpoint_hash("budget_basis")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "budget state differs from its durable checkpoint",
                status_code=409,
            )

    def _planning_checkpoint_progress(self) -> int:
        state = self._active_checkpoint_state()
        current = state.run_checkpoint
        if current not in RUN_CHECKPOINT_ORDER:
            return -1
        planning_index = RUN_CHECKPOINT_ORDER.index("planning_ready")
        current_index = RUN_CHECKPOINT_ORDER.index(current)
        if current_index < planning_index:
            return -1
        if current_index > planning_index:
            return 3
        payload = state.run_checkpoint_json or {}
        sub_index = payload.get("sub_index") if isinstance(payload, dict) else None
        if (
            isinstance(sub_index, int)
            and not isinstance(sub_index, bool)
            and 0 <= sub_index <= 3
        ):
            return sub_index
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if (
            sub_index is None
            and isinstance(refs, dict)
            and isinstance(refs.get("planning"), dict)
        ):
            return 3
        raise DomainError(
            "RUN_CHECKPOINT_CORRUPT",
            "planning checkpoint sub-index is invalid",
            status_code=409,
        )

    def _planning_artifact_refs(
        self,
        *,
        prefix: str,
        serialized: dict[str, Any],
        execution_step_key: str,
        reused: bool,
    ) -> dict[str, Any]:
        llm_call_id = serialized.get("llm_call_id")
        call = (
            self.session.get(LlmCall, llm_call_id)
            if isinstance(llm_call_id, str)
            else None
        )
        if call is not None and isinstance(call.execution_id, str):
            artifact_execution_id = call.execution_id
        elif reused:
            artifact_execution_id = None
        else:
            artifact_execution_id = self._execution_id
        if not reused and artifact_execution_id != self._execution_id:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "new planning artifact is not owned by the current execution",
                status_code=409,
            )
        return {
            f"{prefix}_row_id": serialized.get("row_id"),
            f"{prefix}_llm_call_id": llm_call_id,
            f"{prefix}_execution_step_key": execution_step_key,
            f"{prefix}_artifact_execution_id": artifact_execution_id,
            f"{prefix}_reused": reused,
        }

    @staticmethod
    def _planning_provenance(refs: dict[str, Any], prefix: str) -> dict[str, Any]:
        return {
            "row_id": refs.get(f"{prefix}_row_id"),
            "llm_call_id": refs.get(f"{prefix}_llm_call_id"),
            "execution_step_key": refs.get(f"{prefix}_execution_step_key"),
            "artifact_execution_id": refs.get(f"{prefix}_artifact_execution_id"),
            "reused": refs.get(f"{prefix}_reused"),
        }

    def _validate_planning_provenance(
        self,
        *,
        refs: dict[str, Any],
        prefix: str,
        llm_call_id: str | None,
    ) -> str | None:
        provenance = self._planning_provenance(refs, prefix)
        if self._json_hash(provenance) != self._checkpoint_hash(f"{prefix}_provenance"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{prefix} provenance hash mismatch",
                status_code=409,
            )
        reused = provenance.get("reused")
        owner_execution_id = provenance.get("artifact_execution_id")
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        if not isinstance(reused, bool):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning reuse marker is invalid",
                status_code=409,
            )
        if llm_call_id is None:
            if reused:
                valid_owner = owner_execution_id is None
            else:
                allowed = {
                    self._execution_id,
                    (
                        payload.get("selection_origin_execution_id")
                        if isinstance(payload, dict)
                        else None
                    ),
                }
                if isinstance(payload, dict):
                    allowed.update(payload.get("artifact_execution_lineage_ids") or [])
                valid_owner = owner_execution_id in allowed
            if not valid_owner:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "local planning provenance is invalid",
                    status_code=409,
                )
            return None
        if not isinstance(owner_execution_id, str):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning execution provenance is missing",
                status_code=409,
            )
        superseded = (
            set(payload.get("superseded_execution_ids") or [])
            if isinstance(payload, dict)
            else set()
        )
        if reused:
            if (
                owner_execution_id == self._execution_id
                or owner_execution_id not in superseded
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "reused planning artifact is outside the superseded execution lineage",
                    status_code=409,
                )
        elif owner_execution_id != self._execution_id:
            allowed = {
                (
                    payload.get("selection_origin_execution_id")
                    if isinstance(payload, dict)
                    else None
                ),
            }
            if isinstance(payload, dict):
                allowed.update(payload.get("artifact_execution_lineage_ids") or [])
            if owner_execution_id not in allowed:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "current planning artifact is owned by another execution",
                    status_code=409,
                )
        return owner_execution_id

    @staticmethod
    def _planning_snapshot_matches(
        current: dict[str, Any] | None, snapshot: dict[str, Any]
    ) -> bool:
        if current == snapshot:
            return True
        if not isinstance(current, dict):
            return False
        normalized = dict(current)
        if (
            snapshot.get("status") == "active"
            and normalized.get("status") == "superseded"
        ):
            normalized["status"] = "active"
        return normalized == snapshot

    def _validate_planning_prefix(self, scene_id: str, *, through: int) -> None:
        if through >= 0:
            self._load_planning_blueprint_checkpoint(scene_id)
        if through >= 1:
            self._load_planning_artifact_checkpoint(
                scene_id,
                prefix="planning_chapter_architecture",
                expected_step_key="planning:chapter_architecture",
                expected_kind="chapter_architecture",
            )
        if through >= 2:
            self._load_planning_artifact_checkpoint(
                scene_id,
                prefix="planning_character_pressure",
                expected_step_key="planning:character_pressure",
                expected_kind="character_pressure",
            )

    def _load_planning_blueprint_checkpoint(self, scene_id: str) -> SceneBlueprint:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if not isinstance(refs, dict):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning references are invalid",
                status_code=409,
            )
        serialized = refs.get("scene_blueprint")
        row_id = refs.get("planning_scene_blueprint_row_id")
        row = (
            self.session.get(SceneBlueprint, row_id)
            if isinstance(row_id, str)
            else None
        )
        if row is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        assert row is not None
        scene = self.session.get(SceneCard, scene_id)
        if (
            not isinstance(serialized, dict)
            or serialized.get("row_id") != row_id
            or not self._planning_snapshot_matches(
                self.scene_blueprint_service.serialize(row),
                serialized,
            )
            or self._json_hash(serialized)
            != self._checkpoint_hash("planning_scene_blueprint")
            or row.scene_id != scene_id
            or scene is None
            or row.chapter_id != scene.chapter_id
            or refs.get("planning_scene_blueprint_llm_call_id") != row.llm_call_id
            or refs.get("planning_scene_blueprint_execution_step_key")
            != "scene_blueprint"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning blueprint checkpoint is invalid",
                status_code=409,
            )
        owner_execution_id = self._validate_planning_provenance(
            refs=refs,
            prefix="planning_scene_blueprint",
            llm_call_id=row.llm_call_id,
        )
        assert owner_execution_id is not None
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=row.llm_call_id,
            execution_step_key="scene_blueprint",
            execution_id=owner_execution_id,
        )
        return row

    def _load_planning_artifact_checkpoint(
        self,
        scene_id: str,
        *,
        prefix: str,
        expected_step_key: str,
        expected_kind: str,
    ) -> GenerationPlanningArtifact:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if not isinstance(refs, dict):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning references are invalid",
                status_code=409,
            )
        serialized = refs.get(prefix)
        row_id = refs.get(f"{prefix}_row_id")
        row = (
            self.session.get(GenerationPlanningArtifact, row_id)
            if isinstance(row_id, str)
            else None
        )
        if row is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        assert row is not None
        scene = self.session.get(SceneCard, scene_id)
        expected_shapes = {
            "chapter_architecture": (
                "chapter_story_architecture",
                "chapter",
                scene.chapter_id if scene is not None else None,
                None,
            ),
            "character_pressure": (
                "character_pressure_blueprint",
                "scene",
                scene_id,
                scene_id,
            ),
        }
        artifact_type, object_type, object_id, artifact_scene_id = expected_shapes[
            expected_kind
        ]
        if (
            scene is None
            or not isinstance(serialized, dict)
            or serialized.get("row_id") != row_id
            or not self._planning_snapshot_matches(
                self.planning_service.serialize_artifact(row),
                serialized,
            )
            or self._json_hash(serialized) != self._checkpoint_hash(prefix)
            or row.artifact_type != artifact_type
            or row.object_type != object_type
            or row.object_id != object_id
            or row.chapter_id != scene.chapter_id
            or row.scene_id != artifact_scene_id
            or refs.get(f"{prefix}_llm_call_id") != row.llm_call_id
            or refs.get(f"{prefix}_execution_step_key") != expected_step_key
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{expected_kind} checkpoint is invalid",
                status_code=409,
            )
        owner_execution_id = self._validate_planning_provenance(
            refs=refs,
            prefix=prefix,
            llm_call_id=row.llm_call_id,
        )
        if row.llm_call_id is not None:
            assert owner_execution_id is not None
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=row.llm_call_id,
                execution_step_key=expected_step_key,
                execution_id=owner_execution_id,
            )
        return row

    def _load_planning_checkpoint(self, scene_id: str) -> dict[str, Any]:
        state_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        state_refs = (
            state_payload.get("artifact_refs")
            if isinstance(state_payload, dict)
            else None
        )
        if (
            isinstance(state_refs, dict)
            and "planning_scene_blueprint_row_id" in state_refs
        ):
            self._validate_planning_prefix(scene_id, through=2)
        planning = self._checkpoint_artifact(
            "planning", expected_node_at_least="planning_ready"
        )
        blueprint_payload = self._checkpoint_artifact(
            "scene_blueprint",
            expected_node_at_least="planning_ready",
        )
        if (
            not isinstance(planning, dict)
            or self._json_hash(planning) != self._checkpoint_hash("planning")
            or not isinstance(blueprint_payload, dict)
            or self._json_hash(blueprint_payload)
            != self._checkpoint_hash("scene_blueprint")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning checkpoint payload/hash is invalid",
                status_code=409,
            )

        scene = self.session.get(SceneCard, scene_id)
        blueprint_id = blueprint_payload.get("row_id")
        blueprint = (
            self.session.get(SceneBlueprint, blueprint_id)
            if isinstance(blueprint_id, str)
            else None
        )
        if blueprint is None:
            self._raise_checkpoint_output_missing(row_id=blueprint_id)
        assert blueprint is not None and scene is not None
        if (
            blueprint.scene_id != scene_id
            or blueprint.chapter_id != scene.chapter_id
            or self.scene_blueprint_service.serialize(blueprint) != blueprint_payload
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "scene blueprint checkpoint row is misbound",
                status_code=409,
            )
        self._validate_reused_planning_call(
            scene_id=scene_id,
            llm_call_id=blueprint.llm_call_id,
            expected_step_key="scene_blueprint",
        )

        expected_shapes = {
            "chapter_architecture": {
                "artifact_type": "chapter_story_architecture",
                "object_type": "chapter",
                "object_id": scene.chapter_id,
                "chapter_id": scene.chapter_id,
                "scene_id": None,
                "step_key": "planning:chapter_architecture",
            },
            "character_pressure": {
                "artifact_type": "character_pressure_blueprint",
                "object_type": "scene",
                "object_id": scene_id,
                "chapter_id": scene.chapter_id,
                "scene_id": scene_id,
                "step_key": "planning:character_pressure",
            },
        }
        for key, shape in expected_shapes.items():
            serialized = planning.get(key)
            row_id = serialized.get("row_id") if isinstance(serialized, dict) else None
            row = (
                self.session.get(GenerationPlanningArtifact, row_id)
                if isinstance(row_id, str)
                else None
            )
            if row is None:
                self._raise_checkpoint_output_missing(row_id=row_id)
            assert row is not None
            if (
                not isinstance(serialized, dict)
                or self.planning_service.serialize_artifact(row) != serialized
                or row.artifact_type != shape["artifact_type"]
                or row.object_type != shape["object_type"]
                or row.object_id != shape["object_id"]
                or row.chapter_id != shape["chapter_id"]
                or row.scene_id != shape["scene_id"]
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    f"{key} planning artifact is misbound",
                    status_code=409,
                )
            self._validate_reused_planning_call(
                scene_id=scene_id,
                llm_call_id=row.llm_call_id,
                expected_step_key=str(shape["step_key"]),
                allow_absent=True,
            )
        return planning

    def _validate_reused_planning_call(
        self,
        *,
        scene_id: str,
        llm_call_id: str | None,
        expected_step_key: str,
        allow_absent: bool = False,
    ) -> None:
        if llm_call_id is None and allow_absent:
            return
        call = (
            self.session.get(LlmCall, llm_call_id)
            if isinstance(llm_call_id, str)
            else None
        )
        if call is None:
            self._raise_checkpoint_output_missing(row_id=llm_call_id)
        assert call is not None
        if (
            call.scene_id != scene_id
            or call.request_dispatched_at is None
            or call.accounting_status != "settled"
            or (
                call.execution_id == self._execution_id
                and call.execution_step_key != expected_step_key
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "planning artifact LLM call is misbound",
                status_code=409,
            )
        if call.execution_id == self._execution_id:
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=llm_call_id,
                execution_step_key=expected_step_key,
            )

    def _writer_evaluation_llm_call_id(self, evaluation_id: Any) -> str | None:
        row = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        return row.evaluator_llm_call_id if row is not None else None

    def _validate_qc_attempt(
        self,
        *,
        scene_id: str,
        step: str,
        qc_report_id: str,
        source_bundle_id: str,
        source_draft_row_id: str | None,
        llm_call_id: str | None,
        execution_step_key: str | None,
    ) -> None:
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == step,
                    AttemptTracker.source_bundle_id == source_bundle_id,
                )
            )
            .scalars()
            .all()
        )
        matched = []
        for attempt in attempts:
            details = attempt.details_json or {}
            if details.get("qc_report_id") != qc_report_id:
                continue
            if (
                source_draft_row_id is not None
                and details.get("source_draft_row_id") != source_draft_row_id
            ):
                continue
            if details.get("llm_call_id") != llm_call_id:
                continue
            if details.get("execution_step_key") != execution_step_key:
                continue
            matched.append(attempt)
        if len(matched) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{step} checkpoint has no unique matching attempt audit row",
                status_code=409,
                details={
                    "qc_report_id": qc_report_id,
                    "matching_attempts": len(matched),
                },
            )

    def _capture_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        self.session.flush()
        calls = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == execution_id,
                )
            )
            .scalars()
            .all()
        )
        call_snapshots = [
            {
                column.name: deepcopy(getattr(call, column.name))
                for column in LlmCall.__table__.columns
            }
            for call in calls
        ]
        call_ids = {call.llm_call_id for call in calls}
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.status == "failed",
                )
            )
            .scalars()
            .all()
        )
        attempt_snapshots: list[dict[str, Any]] = []
        for attempt in attempts:
            details = dict(attempt.details_json or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in call_ids:
                continue
            attempt_snapshots.append(
                {
                    "scene_id": attempt.scene_id,
                    "chapter_id": attempt.chapter_id,
                    "step": attempt.step,
                    "status": attempt.status,
                    "source_bundle_id": attempt.source_bundle_id,
                    "details_json": deepcopy(details),
                    "created_at": attempt.created_at,
                }
            )
        return {"calls": call_snapshots, "attempts": attempt_snapshots}

    def _persist_failure_audits_or_fence(
        self,
        scene_id: str,
        execution_id: str,
        checkpoints: SceneRunCheckpointService,
    ) -> None:
        try:
            snapshots = self._capture_failure_audits(scene_id, execution_id)
        except Exception as exc:
            _LOGGER.exception(
                "failed to snapshot failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="snapshot",
                cause=exc,
            )
            return

        self.session.rollback()
        try:
            self._restore_failure_audits(scene_id, execution_id, snapshots)
            checkpoints.mark_failed(scene_id, execution_id)
            self.session.commit()
        except Exception as exc:
            _LOGGER.exception(
                "failed to restore failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="restore",
                cause=exc,
            )

    def _persist_unrecoverable_execution_fence(
        self,
        scene_id: str,
        execution_id: str,
        *,
        phase: str,
        cause: Exception,
    ) -> None:
        """Best-effort terminal fence when failure-audit durability is unknown."""
        try:
            state = self.session.get(SceneRunState, scene_id)
            if state is None:
                return
            self.session.refresh(state)
            if state.active_execution_id != execution_id:
                return
            if (
                state.run_execution_status == "cancelled"
                and state.run_checkpoint == "cancelled"
            ):
                return
            payload = (
                dict(state.run_checkpoint_json)
                if isinstance(state.run_checkpoint_json, dict)
                else {}
            )
            artifact_refs = payload.get("artifact_refs")
            artifact_hashes = payload.get("artifact_hashes")
            cancelled_payload = {
                **payload,
                "execution_id": execution_id,
                "node_key": "cancelled",
                "artifact_refs": (
                    artifact_refs if isinstance(artifact_refs, dict) else {}
                ),
                "artifact_hashes": (
                    artifact_hashes if isinstance(artifact_hashes, dict) else {}
                ),
                "cancelled_from_node": state.run_checkpoint,
                "cancelled_at": utcnow(),
                "unrecoverable_failure_audit": {
                    "phase": phase,
                    "error_type": type(cause).__name__,
                },
            }
            fenced = self.session.execute(
                update(SceneRunState)
                .where(
                    SceneRunState.scene_id == scene_id,
                    SceneRunState.active_execution_id == execution_id,
                    SceneRunState.run_execution_status.in_(("active", "failed")),
                )
                .values(
                    run_execution_status="cancelled",
                    run_checkpoint="cancelled",
                    run_checkpoint_json=cancelled_payload,
                )
                .execution_options(synchronize_session=False)
            )
            if fenced.rowcount != 1:
                self.session.rollback()
                return
            self.session.commit()
        except Exception:
            self.session.rollback()
            _LOGGER.exception(
                "failed to persist unrecoverable execution fence for scene %s execution %s",
                scene_id,
                execution_id,
            )

    def _restore_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
        snapshots: dict[str, list[dict[str, Any]]],
    ) -> None:
        state = self.session.get(SceneRunState, scene_id)
        terminal_accounting_statuses = {
            "settled",
            "failed",
            "usage_exceeds_reservation",
        }
        restored_call_ids: set[str] = set()
        for call_snapshot in snapshots.get("calls", []):
            call_data = dict(call_snapshot)
            if (
                call_data.get("execution_id") != execution_id
                or call_data.get("scene_id") != scene_id
            ):
                continue
            llm_call_id = call_data["llm_call_id"]
            existing = self.session.get(LlmCall, llm_call_id)
            existing_status = (
                existing.accounting_status if existing is not None else None
            )
            existing_reserved = (
                int(existing.reserved_tokens or 0) if existing is not None else 0
            )
            existing_total = (
                int(existing.total_tokens or 0) if existing is not None else 0
            )
            snapshot_status = call_data.get("accounting_status")
            snapshot_total = int(call_data.get("total_tokens") or 0)
            if existing is None:
                self.session.add(
                    LlmCall(
                        scope_type=call_data.pop("scope_type", None),
                        scope_id=call_data.pop("scope_id", None),
                        **call_data,
                    )
                )
            else:
                for column in LlmCall.__table__.columns:
                    if column.primary_key:
                        continue
                    setattr(existing, column.name, deepcopy(call_data[column.name]))
            restored_call_ids.add(llm_call_id)

            if state is None or state.active_execution_id != execution_id:
                continue
            if snapshot_status in terminal_accounting_statuses:
                if existing_status == "reserved":
                    state.scene_tokens_reserved = max(
                        0,
                        int(state.scene_tokens_reserved or 0) - existing_reserved,
                    )
                if existing_status not in terminal_accounting_statuses:
                    state.scene_tokens_used = (
                        int(state.scene_tokens_used or 0) + snapshot_total
                    )
                elif snapshot_total != existing_total:
                    state.scene_tokens_used = max(
                        0,
                        int(state.scene_tokens_used or 0)
                        + snapshot_total
                        - existing_total,
                    )

        restored = 0
        restored_business_attempts = 0
        for attempt_snapshot in snapshots.get("attempts", []):
            attempt_data = dict(attempt_snapshot)
            details = dict(attempt_data.get("details_json") or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in restored_call_ids:
                continue
            existing_attempts = (
                self.session.execute(
                    select(AttemptTracker).where(
                        AttemptTracker.scene_id == scene_id,
                        AttemptTracker.step == attempt_data["step"],
                        AttemptTracker.status == "failed",
                    )
                )
                .scalars()
                .all()
            )
            if any(
                (attempt.details_json or {}).get("llm_call_id") == llm_call_id
                for attempt in existing_attempts
            ):
                continue
            self.session.add(AttemptTracker(**attempt_data))
            restored += 1
            if details.get("business_attempt_consumed", True) is not False:
                restored_business_attempts += 1
        if restored_business_attempts:
            if state is not None and state.active_execution_id == execution_id:
                state.total_attempt_count = (
                    int(state.total_attempt_count or 0) + restored_business_attempts
                )
        self.session.flush()

    def _near_final_checkpoint_progress(self) -> int:
        return self._archive_checkpoint()._near_final_checkpoint_progress()

    def _archive_product(
        self,
        *,
        scene: SceneCard,
        kind: str,
        outcome: str,
        step_key: str,
        input_hash: str,
        **details: Any,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._archive_product(
            scene=scene,
            kind=kind,
            outcome=outcome,
            step_key=step_key,
            input_hash=input_hash,
            **details,
        )

    @staticmethod
    def _archive_final_scene_snapshot(row: FinalScene) -> dict[str, Any]:
        return SceneArchiveCheckpoint._archive_final_scene_snapshot(row)

    @staticmethod
    def _archive_scene_memory_snapshot(row: SceneMemory) -> dict[str, Any]:
        return SceneArchiveCheckpoint._archive_scene_memory_snapshot(row)

    @staticmethod
    def _archive_rolling_note_snapshot(row: ChapterRollingNote) -> dict[str, Any]:
        return SceneArchiveCheckpoint._archive_rolling_note_snapshot(row)

    @staticmethod
    def _archive_attempt_snapshot(row: AttemptTracker) -> dict[str, Any]:
        return SceneArchiveCheckpoint._archive_attempt_snapshot(row)

    def _validate_archive_core_checkpoint(
        self,
        *,
        scene: SceneCard,
        final_scene: FinalScene,
        carry_notes: list[dict[str, Any]],
        allow_terminal: bool = False,
        product: dict[str, Any] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            allow_terminal=allow_terminal,
            product=product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    @staticmethod
    def _narrative_event_snapshot(event: NarrativeEvent) -> dict[str, Any]:
        return SceneArchiveCheckpoint._narrative_event_snapshot(event)

    def _narrative_event_snapshots(self, event_ids: list[str]) -> list[dict[str, Any]]:
        return self._archive_checkpoint()._narrative_event_snapshots(event_ids)

    def _validate_archive_rule_events_checkpoint(
        self,
        scene: SceneCard,
        *,
        product: dict[str, Any] | None = None,
        event_ids: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> None:
        self._archive_checkpoint()._validate_archive_rule_events_checkpoint(
            scene,
            product=product,
            event_ids=event_ids,
            events=events,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _validate_archive_prose_checkpoint(
        self,
        scene: SceneCard,
        contract,
        *,
        product: dict[str, Any] | None = None,
        event_ids: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> None:
        self._archive_checkpoint()._validate_archive_prose_checkpoint(
            scene,
            contract,
            product=product,
            event_ids=event_ids,
            events=events,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _recover_archive_prose_rejection(self) -> ProseExtractionResult | None:
        return self._archive_checkpoint()._recover_archive_prose_rejection()

    def _archive_checkpoint_ref(self, key: str) -> Any:
        return self._archive_checkpoint()._archive_checkpoint_ref(key)

    def _validate_common_archive_product(
        self,
        *,
        scene: SceneCard,
        product: Any,
        kind: str,
        step_key: str,
        outcomes: set[str],
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_common_archive_product(
            scene=scene,
            product=product,
            kind=kind,
            step_key=step_key,
            outcomes=outcomes,
        )

    def _validate_archive_vector_product(
        self,
        scene: SceneCard,
        final_scene: FinalScene,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_vector_product(
            scene,
            final_scene,
            product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _scene_memory_inputs(self, chapter_id: str) -> list[dict[str, str]]:
        return self._archive_checkpoint()._scene_memory_inputs(chapter_id)

    @staticmethod
    def _chapter_memory_snapshot(memory: ChapterMemory) -> dict[str, Any]:
        return SceneArchiveCheckpoint._chapter_memory_snapshot(memory)

    def _run_archive_chapter_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._run_archive_chapter_aggregate(
            scene, final_scene
        )

    def _validate_archive_chapter_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_chapter_product(
            scene,
            product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _volume_input_memories(self, scene: SceneCard) -> list[dict[str, str]]:
        return self._archive_checkpoint()._volume_input_memories(scene)

    @staticmethod
    def _volume_snapshot(row: VolumeSummary) -> dict[str, Any]:
        return SceneArchiveCheckpoint._volume_snapshot(row)

    def _run_archive_volume_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._run_archive_volume_aggregate(
            scene, final_scene
        )

    def _validate_archive_volume_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_volume_product(
            scene,
            product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    @staticmethod
    def _archive_writer_evaluation_snapshot(row: WriterEvaluation) -> dict[str, Any]:
        return SceneArchiveCheckpoint._archive_writer_evaluation_snapshot(row)

    def _run_archive_chapter_evaluation(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._run_archive_chapter_evaluation(
            scene, final_scene
        )

    def _validate_archive_chapter_evaluation_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_chapter_evaluation_product(
            scene,
            product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _validate_archive_drift_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        return self._archive_checkpoint()._validate_archive_drift_product(
            scene,
            product,
            require_checkpoint_hash=require_checkpoint_hash,
        )

    def _archive_manifest(self) -> list[dict[str, Any]]:
        return self._archive_checkpoint()._archive_manifest()

    def _validate_archive_prefix(
        self,
        *,
        scene: SceneCard,
        contract,
        final_scene: FinalScene,
        carry_notes: list[dict[str, Any]],
        through: int,
        allow_terminal: bool = False,
    ) -> None:
        self._archive_checkpoint()._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=through,
            allow_terminal=allow_terminal,
        )

    def _ensure_near_final_subcheckpoints(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult,
        optional_spend_allowed,
    ) -> tuple[dict[str, Any], StyleGenerationResult, int, str | None]:
        progress = self._near_final_checkpoint_progress()
        if progress >= 3:
            final_scene, payload = self._load_near_final_checkpoint(
                scene=scene,
                bundle=bundle,
                source_generation=source_generation,
            )
            generation = StyleGenerationResult(
                row_id=str(
                    (self._active_checkpoint_state().run_checkpoint_json or {})
                    .get("artifact_refs", {})
                    .get("near_final_source_draft_row_id")
                ),
                content=final_scene.content,
                llm_call_id=final_scene.generation_llm_call_id or "",
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=(
                    self._active_checkpoint_state().run_checkpoint_json or {}
                )
                .get("artifact_refs", {})
                .get("final_generation_execution_step_key"),
                artifact_execution_id=(
                    self._active_checkpoint_state().run_checkpoint_json or {}
                )
                .get("artifact_refs", {})
                .get("final_generation_artifact_execution_id"),
            )
            return (
                payload,
                generation,
                int(payload.get("rewrite_count") or 0),
                (
                    (self._active_checkpoint_state().run_checkpoint_json or {})
                    .get("artifact_refs", {})
                    .get("near_final_skip_reason")
                ),
            )

        if progress < 0:
            self._reconcile_execution_step("near_final_acceptance:0")
            eval0 = self.near_final_service.evaluate_scene(
                scene.scene_id,
                bundle=bundle,
                source_draft_row_id=source_generation.row_id,
                source_content=source_generation.content,
                execution_step_key="near_final_acceptance:0",
            )
            rewrite_requested = not bool(eval0.get("pass_flag")) and bool(
                eval0.get("should_rewrite")
            )
            rewrite_allowed = rewrite_requested and optional_spend_allowed()
            if rewrite_requested and not rewrite_allowed:
                skip_reason = "budget_or_candidate_cap"
                branch = "rewrite_skipped"
                _LOGGER.warning(
                    "near-final rewrite skipped for scene %s (budget/candidate cap)",
                    scene.scene_id,
                )
            elif eval0.get("requires_human_review"):
                skip_reason = "human_review_proposal"
                branch = "human_review_proposal"
            elif eval0.get("pass_flag"):
                skip_reason = "no_rewrite_requested"
                branch = "pass"
            elif not rewrite_requested:
                skip_reason = "not_auto_rewrite_eligible"
                branch = "unresolved"
            else:
                skip_reason = None
                branch = "rewrite"
            control = {
                "branch": branch,
                "rewrite_requested": rewrite_requested,
                "rewrite_allowed": rewrite_allowed,
                "skip_reason": skip_reason,
            }
            self._save_near_evaluation_checkpoint(
                sub_index=0,
                round_index=0,
                result=eval0,
                source_generation=source_generation,
                bundle=bundle,
                control=control,
            )
            progress = 0
        else:
            eval0 = self._load_near_evaluation_checkpoint(
                scene_id=scene.scene_id,
                round_index=0,
                source_generation=source_generation,
            )
            control = self._load_near_eval0_control(eval0)

        if bool(control["rewrite_allowed"]):
            if progress < 1:
                self._reconcile_execution_step("near_final_rewrite:0")
                rewrite_generation = (
                    self.scene_generation_service.generate_near_final_rewrite(
                        scene.scene_id,
                        bundle,
                        source_draft_row_id=source_generation.row_id,
                        source_content=source_generation.content,
                        revision_brief=self._near_final_rewrite_brief(eval0),
                        source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                        execution_step_key="near_final_rewrite:0",
                    )
                )
                self._save_near_rewrite_checkpoint(
                    generation=rewrite_generation,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                    bundle=bundle,
                )
                progress = 1
            else:
                rewrite_generation = self._load_near_rewrite_checkpoint(
                    scene_id=scene.scene_id,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                )
            if progress < 2:
                self._reconcile_execution_step("near_final_acceptance:1")
                eval1 = self.near_final_service.evaluate_scene(
                    scene.scene_id,
                    bundle=bundle,
                    source_draft_row_id=rewrite_generation.row_id,
                    source_content=rewrite_generation.content,
                    execution_step_key="near_final_acceptance:1",
                )
                self._save_near_evaluation_checkpoint(
                    sub_index=2,
                    round_index=1,
                    result=eval1,
                    source_generation=rewrite_generation,
                    bundle=bundle,
                )
            else:
                eval1 = self._load_near_evaluation_checkpoint(
                    scene_id=scene.scene_id,
                    round_index=1,
                    source_generation=rewrite_generation,
                )
            return eval1, rewrite_generation, 1, None

        if progress >= 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final rewrite checkpoint exists for a non-rewrite eval0 branch",
                status_code=409,
            )
        return eval0, source_generation, 0, control.get("skip_reason")

    @staticmethod
    def _near_evaluation_payload(result: dict[str, Any]) -> dict[str, Any]:
        return {
            "near_final_status": result.get("near_final_status"),
            "pass_flag": bool(result.get("pass_flag")),
            "overall_score": result.get("overall_score"),
            "scores": deepcopy(result.get("scores") or {}),
            "failure_class": result.get("failure_class"),
            "requires_human_review": bool(result.get("requires_human_review")),
            "evaluation_id": result.get("evaluation_id"),
            "revision_candidate_id": result.get("revision_candidate_id"),
            "should_rewrite": bool(result.get("should_rewrite")),
            "findings": deepcopy(result.get("findings") or []),
            "revision_brief": deepcopy(result.get("revision_brief") or []),
        }

    @staticmethod
    def _writer_evaluation_snapshot(evaluation: WriterEvaluation) -> dict[str, Any]:
        return {
            "object_type": evaluation.object_type,
            "object_id": evaluation.object_id,
            "chapter_id": evaluation.chapter_id,
            "scene_id": evaluation.scene_id,
            "rubric_id": evaluation.rubric_id,
            "source_text_ref": evaluation.source_text_ref,
            "source_bundle_id": evaluation.source_bundle_id,
            "evaluator_llm_call_id": evaluation.evaluator_llm_call_id,
            "lens": evaluation.lens,
            "overall_score": evaluation.overall_score,
            "scores": deepcopy(evaluation.scores_json or {}),
            "findings": deepcopy(evaluation.findings_json or []),
            "failure_class": evaluation.failure_class,
            "auto_rewrite_eligible": evaluation.auto_rewrite_eligible,
            "contract_field_refs": deepcopy(evaluation.contract_field_refs_json or {}),
            "promotion_blockers": deepcopy(evaluation.promotion_blockers_json or []),
            "revision_brief": deepcopy(evaluation.revision_brief_json or []),
            "requires_human_review": evaluation.requires_human_review,
            "status": evaluation.status,
        }

    @staticmethod
    def _revision_candidate_snapshot(candidate: RevisionCandidate) -> dict[str, Any]:
        return {
            "evaluation_id": candidate.evaluation_id,
            "object_type": candidate.object_type,
            "object_id": candidate.object_id,
            "chapter_id": candidate.chapter_id,
            "scene_id": candidate.scene_id,
            "revision_type": candidate.revision_type,
            "source_text_ref": candidate.source_text_ref,
            "proposed_text": candidate.proposed_text,
            "instruction": deepcopy(candidate.instruction_json or []),
            "diff_summary": deepcopy(candidate.diff_summary_json or {}),
            "patches": deepcopy(candidate.patches_json or []),
            "apply_mode": candidate.apply_mode,
            "target_text_ref": candidate.target_text_ref,
            "status": candidate.status,
            "author_decision_note": candidate.author_decision_note,
            "created_by": candidate.created_by,
        }

    def _near_candidate_refs_and_hashes(
        self,
        *,
        prefix: str,
        evaluation_id: str,
        candidate_id: Any,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        rows = (
            self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.evaluation_id == evaluation_id
                )
            )
            .scalars()
            .all()
        )
        if candidate_id is None:
            if rows:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "passing near-final evaluation has an unexpected revision candidate",
                    status_code=409,
                )
            snapshot = None
        else:
            candidate = (
                self.session.get(RevisionCandidate, candidate_id)
                if isinstance(candidate_id, str)
                else None
            )
            if candidate is None:
                self._raise_checkpoint_output_missing(row_id=candidate_id)
            if len(rows) != 1 or rows[0].revision_id != candidate_id:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "near-final evaluation candidate reference is not unique",
                    status_code=409,
                )
            snapshot = self._revision_candidate_snapshot(candidate)
        return (
            {
                f"{prefix}_revision_candidate_id": candidate_id,
                f"{prefix}_candidate_snapshot": snapshot,
            },
            {f"{prefix}_candidate": self._json_hash(snapshot)},
        )

    def _save_near_evaluation_checkpoint(
        self,
        *,
        sub_index: int,
        round_index: int,
        result: dict[str, Any],
        source_generation: StyleGenerationResult,
        bundle: dict[str, Any],
        control: dict[str, Any] | None = None,
    ) -> None:
        self.session.flush()
        prefix = f"near_eval{round_index}"
        evaluation_id = result.get("evaluation_id")
        evaluation = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        if evaluation is None:
            self._raise_checkpoint_output_missing(row_id=evaluation_id)
        normalized = self._near_evaluation_payload(result)
        candidate_refs, candidate_hashes = self._near_candidate_refs_and_hashes(
            prefix=prefix,
            evaluation_id=evaluation.evaluation_id,
            candidate_id=result.get("revision_candidate_id"),
        )
        refs: dict[str, Any] = {
            f"{prefix}_evaluation_id": evaluation.evaluation_id,
            f"{prefix}_payload": normalized,
            f"{prefix}_source_draft_row_id": source_generation.row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
            f"{prefix}_llm_call_id": evaluation.evaluator_llm_call_id,
            f"{prefix}_execution_step_key": f"near_final_acceptance:{round_index}",
            f"{prefix}_artifact_execution_id": self._execution_id,
            **candidate_refs,
        }
        hashes = {
            f"{prefix}_payload": self._json_hash(normalized),
            f"{prefix}_evaluation": self._json_hash(
                self._writer_evaluation_snapshot(evaluation)
            ),
            **candidate_hashes,
        }
        if round_index == 0 and control is not None:
            refs["near_eval0_control"] = deepcopy(control)
            hashes["near_eval0_control"] = self._json_hash(control)
        if round_index == 1:
            state_refs = (
                self._active_checkpoint_state().run_checkpoint_json or {}
            ).get("artifact_refs") or {}
            eval0_id = state_refs.get("near_eval0_evaluation_id")
            eval0_candidate_id = state_refs.get("near_eval0_revision_candidate_id")
            refreshed_refs, refreshed_hashes = self._near_candidate_refs_and_hashes(
                prefix="near_eval0",
                evaluation_id=str(eval0_id or ""),
                candidate_id=eval0_candidate_id,
            )
            refs.update(refreshed_refs)
            hashes.update(refreshed_hashes)
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=sub_index,
            artifact_refs=refs,
            artifact_hashes=hashes,
            branch=str(result.get("near_final_status") or "near_final_ready"),
        )

    def _load_near_evaluation_checkpoint(
        self,
        *,
        scene_id: str,
        round_index: int,
        source_generation: StyleGenerationResult,
    ) -> dict[str, Any]:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        prefix = f"near_eval{round_index}"
        evaluation_id = refs.get(f"{prefix}_evaluation_id")
        evaluation = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        if evaluation is None:
            self._raise_checkpoint_output_missing(row_id=evaluation_id)
        normalized = refs.get(f"{prefix}_payload")
        if (
            not isinstance(normalized, dict)
            or self._json_hash(normalized) != self._checkpoint_hash(f"{prefix}_payload")
            or self._json_hash(self._writer_evaluation_snapshot(evaluation))
            != self._checkpoint_hash(f"{prefix}_evaluation")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"near-final evaluation {round_index} payload/content hash mismatch",
                status_code=409,
            )
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get(f"{prefix}_llm_call_id")
        step_key = refs.get(f"{prefix}_execution_step_key")
        owner = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
            execution_id=owner,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        if (
            evaluation.object_type != "scene"
            or evaluation.object_id != scene_id
            or evaluation.scene_id != scene_id
            or evaluation.rubric_id != "near_final_acceptance_v1"
            or evaluation.source_text_ref != f"source_draft:{source_generation.row_id}"
            or evaluation.source_bundle_id != bundle["bundle_id"]
            or evaluation.evaluator_llm_call_id != llm_call_id
            or refs.get(f"{prefix}_source_draft_row_id") != source_generation.row_id
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or step_key != f"near_final_acceptance:{round_index}"
            or normalized.get("evaluation_id") != evaluation.evaluation_id
            or normalized.get("overall_score") != evaluation.overall_score
            or normalized.get("scores") != (evaluation.scores_json or {})
            or normalized.get("findings") != (evaluation.findings_json or [])
            or normalized.get("failure_class") != evaluation.failure_class
            or normalized.get("should_rewrite")
            != bool(evaluation.auto_rewrite_eligible)
            or normalized.get("revision_brief")
            != (evaluation.revision_brief_json or [])
            or normalized.get("requires_human_review")
            != bool(evaluation.requires_human_review)
            or normalized.get("pass_flag")
            != (normalized.get("near_final_status") == "near_final_ready")
            or normalized.get("requires_human_review")
            != (normalized.get("near_final_status") == "human_review_required")
            or (
                normalized.get("pass_flag")
                and (
                    normalized.get("failure_class") is not None
                    or normalized.get("revision_candidate_id") is not None
                )
            )
            or (
                not normalized.get("pass_flag")
                and not isinstance(normalized.get("revision_candidate_id"), str)
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"near-final evaluation {round_index} identity/source mismatch",
                status_code=409,
            )
        candidate_id = refs.get(f"{prefix}_revision_candidate_id")
        expected_candidate_id = normalized.get("revision_candidate_id")
        candidate_snapshot = refs.get(f"{prefix}_candidate_snapshot")
        candidates = (
            self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.evaluation_id == evaluation.evaluation_id
                )
            )
            .scalars()
            .all()
        )
        if expected_candidate_id is None:
            if candidate_id is not None or candidate_snapshot is not None or candidates:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    f"near-final evaluation {round_index} has a forged candidate reference",
                    status_code=409,
                )
        else:
            candidate = (
                self.session.get(RevisionCandidate, candidate_id)
                if isinstance(candidate_id, str)
                else None
            )
            if candidate is None:
                self._raise_checkpoint_output_missing(row_id=candidate_id)
            if (
                candidate_id != expected_candidate_id
                or len(candidates) != 1
                or candidates[0].revision_id != candidate_id
                or candidate.evaluation_id != evaluation.evaluation_id
                or candidate.object_type != "scene"
                or candidate.object_id != scene_id
                or candidate.scene_id != scene_id
                or candidate.revision_type != "near_final_scene_rewrite"
                or candidate.source_text_ref
                != f"source_draft:{source_generation.row_id}"
                or candidate.proposed_text != source_generation.content
                or candidate.status not in {"candidate", "superseded"}
                or self._revision_candidate_snapshot(candidate) != candidate_snapshot
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    f"near-final evaluation {round_index} candidate is misbound",
                    status_code=409,
                )
        if self._json_hash(candidate_snapshot) != self._checkpoint_hash(
            f"{prefix}_candidate"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"near-final evaluation {round_index} candidate hash mismatch",
                status_code=409,
            )
        self._validate_near_final_attempt(
            scene_id=scene_id,
            evaluation_id=evaluation.evaluation_id,
            candidate_id=expected_candidate_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_generation.row_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
        )
        return deepcopy(normalized)

    def _validate_near_final_attempt(
        self,
        *,
        scene_id: str,
        evaluation_id: str,
        candidate_id: str | None,
        source_bundle_id: str,
        source_draft_row_id: str,
        llm_call_id: str,
        execution_step_key: str,
    ) -> None:
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == "near_final_acceptance_review",
                    AttemptTracker.source_bundle_id == source_bundle_id,
                )
            )
            .scalars()
            .all()
        )
        matched = []
        for attempt in attempts:
            details = attempt.details_json or {}
            if (
                details.get("evaluation_id") == evaluation_id
                and details.get("revision_candidate_id") == candidate_id
                and details.get("source_draft_row_id") == source_draft_row_id
                and details.get("llm_call_id") == llm_call_id
                and details.get("execution_step_key") == execution_step_key
            ):
                matched.append(attempt)
        if len(matched) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final checkpoint has no unique matching attempt audit row",
                status_code=409,
                details={
                    "evaluation_id": evaluation_id,
                    "matching_attempts": len(matched),
                },
            )

    def _load_near_eval0_control(self, eval0: dict[str, Any]) -> dict[str, Any]:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs"
        ) or {}
        control = refs.get("near_eval0_control")
        rewrite_requested = not bool(eval0.get("pass_flag")) and bool(
            eval0.get("should_rewrite")
        )
        if (
            not isinstance(control, dict)
            or not isinstance(control.get("branch"), str)
            or not isinstance(control.get("rewrite_requested"), bool)
            or not isinstance(control.get("rewrite_allowed"), bool)
            or self._json_hash(control) != self._checkpoint_hash("near_eval0_control")
            or control["rewrite_requested"] != rewrite_requested
            or (control["rewrite_allowed"] and not control["rewrite_requested"])
            or (control["rewrite_allowed"] and control.get("skip_reason") is not None)
            or (
                control.get("skip_reason") is not None
                and not isinstance(control.get("skip_reason"), str)
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final eval0 branch control is invalid",
                status_code=409,
            )
        expected_branch: str
        expected_skip_reason: str | None
        if eval0.get("requires_human_review"):
            expected_branch, expected_skip_reason = (
                "human_review_proposal",
                "human_review_proposal",
            )
        elif eval0.get("pass_flag"):
            expected_branch, expected_skip_reason = "pass", "no_rewrite_requested"
        elif rewrite_requested and control["rewrite_allowed"]:
            expected_branch, expected_skip_reason = "rewrite", None
        elif rewrite_requested:
            expected_branch, expected_skip_reason = (
                "rewrite_skipped",
                "budget_or_candidate_cap",
            )
        else:
            expected_branch, expected_skip_reason = (
                "unresolved",
                "not_auto_rewrite_eligible",
            )
        if (
            control.get("branch") != expected_branch
            or control.get("skip_reason") != expected_skip_reason
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final eval0 branch/skip reason is inconsistent",
                status_code=409,
            )
        return deepcopy(control)

    def _save_near_rewrite_checkpoint(
        self,
        *,
        generation: StyleGenerationResult,
        source_generation: StyleGenerationResult,
        source_evaluation_id: str,
        bundle: dict[str, Any],
    ) -> None:
        self._save_run_checkpoint(
            "near_final_ready",
            sub_index=1,
            artifact_refs={
                "near_rewrite_draft_row_id": generation.row_id,
                "near_rewrite_llm_call_id": generation.llm_call_id,
                "near_rewrite_execution_step_key": generation.execution_step_key,
                "near_rewrite_artifact_execution_id": generation.artifact_execution_id
                or self._execution_id,
                "near_rewrite_source_draft_row_id": source_generation.row_id,
                "near_rewrite_source_evaluation_id": source_evaluation_id,
                "near_rewrite_bundle_id": bundle["bundle_id"],
                "near_rewrite_bundle_hash": bundle["bundle_snapshot_hash"],
            },
            artifact_hashes={"near_rewrite_draft": self._text_hash(generation.content)},
            branch="rewrite",
        )

    def _load_near_rewrite_checkpoint(
        self,
        *,
        scene_id: str,
        source_generation: StyleGenerationResult,
        source_evaluation_id: str,
    ) -> StyleGenerationResult:
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs"
        ) or {}
        row_id = refs.get("near_rewrite_draft_row_id")
        draft = (
            self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        )
        if draft is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get("near_rewrite_llm_call_id")
        step_key = refs.get("near_rewrite_execution_step_key")
        owner = self._validate_artifact_execution_owner(
            refs.get("near_rewrite_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=step_key,
            execution_id=owner,
        )
        if (
            draft.scene_id != scene_id
            or draft.stage != "near_final_rewrite"
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != llm_call_id
            or refs.get("near_rewrite_source_draft_row_id") != source_generation.row_id
            or refs.get("near_rewrite_source_evaluation_id") != source_evaluation_id
            or refs.get("near_rewrite_bundle_id") != bundle["bundle_id"]
            or refs.get("near_rewrite_bundle_hash") != bundle["bundle_snapshot_hash"]
            or step_key != "near_final_rewrite:0"
            or self._text_hash(draft.content)
            != self._checkpoint_hash("near_rewrite_draft")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final rewrite checkpoint identity/source/hash mismatch",
                status_code=409,
            )
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.step == "scene_literary_rewrite",
                    AttemptTracker.source_bundle_id == bundle["bundle_id"],
                )
            )
            .scalars()
            .all()
        )
        matched = [
            attempt
            for attempt in attempts
            if (attempt.details_json or {}).get("row_id") == draft.row_id
            and (attempt.details_json or {}).get("llm_call_id") == llm_call_id
            and (attempt.details_json or {}).get("source_draft_row_id")
            == source_generation.row_id
            and (attempt.details_json or {}).get("source_evaluation_id")
            == source_evaluation_id
        ]
        if len(matched) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final rewrite checkpoint has no unique matching attempt audit row",
                status_code=409,
            )
        return StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=step_key,
            artifact_execution_id=owner,
        )

    def _load_near_final_checkpoint(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        source_generation: StyleGenerationResult | None = None,
    ) -> tuple[FinalScene, dict[str, Any]]:
        scene_id = scene.scene_id
        final_row_id = self._checkpoint_artifact(
            "final_scene_row_id",
            expected_node_at_least="near_final_ready",
        )
        final_scene = (
            self.session.get(FinalScene, final_row_id)
            if isinstance(final_row_id, str)
            else None
        )
        if final_scene is None:
            self._raise_checkpoint_output_missing(row_id=final_row_id)
        assert final_scene is not None
        state_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = state_payload.get("artifact_refs") or {}
        generation_call_id = refs.get("final_generation_llm_call_id")
        generation_step_key = refs.get("final_generation_execution_step_key")
        generation_execution_id = self._validate_artifact_execution_owner(
            refs.get("final_generation_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=generation_call_id,
            execution_step_key=generation_step_key,
            execution_id=generation_execution_id,
        )
        if (
            final_scene.scene_id != scene_id
            or final_scene.chapter_id != scene.chapter_id
            or final_scene.source_bundle_id != bundle["bundle_id"]
            or final_scene.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or final_scene.generation_llm_call_id != generation_call_id
            or self._text_hash(final_scene.content)
            != self._checkpoint_hash("final_scene")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final checkpoint identity/hash mismatch",
                status_code=409,
            )

        near_final_payload = refs.get("near_final")
        if not isinstance(near_final_payload, dict) or self._json_hash(
            near_final_payload
        ) != self._checkpoint_hash("near_final"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final payload hash mismatch",
                status_code=409,
            )
        carry_notes = refs.get("carry_notes")
        if not isinstance(carry_notes, list) or self._json_hash(
            carry_notes
        ) != self._checkpoint_hash("carry_notes"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final carry notes hash mismatch",
                status_code=409,
            )
        evaluation_id = refs.get("near_final_evaluation_id")
        evaluation = (
            self.session.get(WriterEvaluation, evaluation_id)
            if isinstance(evaluation_id, str)
            else None
        )
        if evaluation is None:
            self._raise_checkpoint_output_missing(row_id=evaluation_id)
        assert evaluation is not None
        evaluation_call_id = refs.get("near_final_evaluation_llm_call_id")
        evaluation_step_key = refs.get("near_final_evaluation_step_key")
        evaluation_execution_id = self._validate_artifact_execution_owner(
            refs.get("near_final_evaluation_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=evaluation_call_id,
            execution_step_key=evaluation_step_key,
            execution_id=evaluation_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        source_draft_id = refs.get("near_final_source_draft_row_id")
        if (
            evaluation.object_type != "scene"
            or evaluation.object_id != scene_id
            or evaluation.scene_id != scene_id
            or evaluation.chapter_id != scene.chapter_id
            or evaluation.rubric_id != "near_final_acceptance_v1"
            or evaluation.source_text_ref != f"source_draft:{source_draft_id}"
            or evaluation.source_bundle_id != bundle["bundle_id"]
            or evaluation.evaluator_llm_call_id != evaluation_call_id
            or near_final_payload.get("evaluation_id") != evaluation.evaluation_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final evaluation checkpoint is misbound",
                status_code=409,
            )
        if refs.get("near_completion") is not None:
            if source_generation is None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "near-final prefix validation requires the soft-final source draft",
                    status_code=409,
                )
            eval0 = self._load_near_evaluation_checkpoint(
                scene_id=scene_id,
                round_index=0,
                source_generation=source_generation,
            )
            control = self._load_near_eval0_control(eval0)
            rewrite_count = refs.get("near_final_rewrite_count")
            if rewrite_count == 1:
                if (
                    not control.get("rewrite_allowed")
                    or control.get("skip_reason") is not None
                ):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "near-final rewrite completion is not reachable from eval0",
                        status_code=409,
                    )
                expected_generation = self._load_near_rewrite_checkpoint(
                    scene_id=scene_id,
                    source_generation=source_generation,
                    source_evaluation_id=str(eval0.get("evaluation_id") or ""),
                )
                final_evaluation = self._load_near_evaluation_checkpoint(
                    scene_id=scene_id,
                    round_index=1,
                    source_generation=expected_generation,
                )
            elif rewrite_count == 0:
                if control.get("rewrite_allowed"):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "near-final completion skipped an allowed rewrite",
                        status_code=409,
                    )
                expected_generation = source_generation
                final_evaluation = eval0
            else:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "near-final rewrite count is invalid",
                    status_code=409,
                )
            expected_payload = self._near_final_result_payload(
                final_evaluation,
                rewrite_count=rewrite_count,
            )
            eval0_candidate_id = refs.get("near_eval0_revision_candidate_id")
            if isinstance(eval0_candidate_id, str):
                eval0_candidate = self.session.get(
                    RevisionCandidate, eval0_candidate_id
                )
                if eval0_candidate is None:
                    self._raise_checkpoint_output_missing(row_id=eval0_candidate_id)
                expected_eval0_candidate_status = (
                    "superseded"
                    if rewrite_count == 1 and final_evaluation.get("pass_flag")
                    else "candidate"
                )
                if eval0_candidate.status != expected_eval0_candidate_status:
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "near-final eval0 candidate lifecycle is inconsistent with the final evaluation",
                        status_code=409,
                    )
            completion = refs.get("near_completion")
            expected_completion = {
                "final_evaluation_round": rewrite_count,
                "rewrite_count": rewrite_count,
                "skip_reason": refs.get("near_final_skip_reason"),
                "branch": final_evaluation.get("near_final_status"),
                "evaluation_id": final_evaluation.get("evaluation_id"),
                "source_draft_row_id": expected_generation.row_id,
                "final_scene_row_id": final_scene.row_id,
            }
            if (
                completion != expected_completion
                or self._json_hash(completion)
                != self._checkpoint_hash("near_completion")
                or refs.get("near_final_branch")
                != final_evaluation.get("near_final_status")
                or refs.get("near_final_skip_reason")
                != (control.get("skip_reason") if rewrite_count == 0 else None)
                or near_final_payload != expected_payload
                or refs.get("near_final_source_draft_row_id")
                != expected_generation.row_id
                or final_scene.content != expected_generation.content
                or final_scene.generation_llm_call_id != expected_generation.llm_call_id
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "near-final completion prefix/branch/hash mismatch",
                    status_code=409,
                )
            finalize_attempts = (
                self.session.execute(
                    select(AttemptTracker).where(
                        AttemptTracker.scene_id == scene_id,
                        AttemptTracker.step == "finalize",
                        AttemptTracker.source_bundle_id == bundle["bundle_id"],
                    )
                )
                .scalars()
                .all()
            )
            matched_finalize = [
                attempt
                for attempt in finalize_attempts
                if (attempt.details_json or {}).get("source_style_draft_row_id")
                == expected_generation.row_id
                and (attempt.details_json or {}).get("final_generation_llm_call_id")
                == expected_generation.llm_call_id
                and (attempt.details_json or {}).get("source_qc_report_id")
                == refs.get("soft_qc_report_id")
            ]
            if len(matched_finalize) != 1:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "near-final checkpoint has no unique finalize attempt audit row",
                    status_code=409,
                )
        return final_scene, near_final_payload

    def _load_archived_checkpoint(self, scene_id: str) -> FinalScene:
        state = self._active_checkpoint_state()
        scene = self.session.get(SceneCard, scene_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        selected_style = self._load_selected_style_checkpoint(scene_id)
        _soft_qc, soft_generation = self._load_soft_qc_checkpoint(
            scene_id,
            selected_style_generation=selected_style,
        )
        final_scene, _near_final = self._load_near_final_checkpoint(
            scene=scene,
            bundle=bundle,
            source_generation=soft_generation,
        )
        refs = (state.run_checkpoint_json or {}).get("artifact_refs", {})
        carry_notes = list(refs.get("carry_notes") or [])
        if self._json_hash(carry_notes) != self._checkpoint_hash("carry_notes"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archived carry notes hash mismatch",
                status_code=409,
            )
        contract = self.execution_contract_service.get_or_create(
            scene_id,
            actor_ref="orchestrator",
        )
        self._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            allow_terminal=True,
        )
        self._validate_archive_rule_events_checkpoint(scene)
        self._validate_archive_prose_checkpoint(scene, contract)
        self._validate_archive_vector_product(scene, final_scene)
        self._validate_archive_chapter_product(scene)
        self._validate_archive_volume_product(scene)
        self._validate_archive_chapter_evaluation_product(scene)
        self._validate_archive_drift_product(scene)
        manifest = refs.get("archive_manifest")
        expected_manifest = self._archive_manifest()
        if manifest != expected_manifest or self._json_hash(
            manifest
        ) != self._checkpoint_hash("archive_manifest"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archived product manifest is incomplete or changed",
                status_code=409,
            )
        memory_id = self._checkpoint_artifact(
            "scene_memory_row_id", expected_node_at_least="archived"
        )
        memory = (
            self.session.get(SceneMemory, memory_id)
            if isinstance(memory_id, str)
            else None
        )
        if memory is None:
            self._raise_checkpoint_output_missing(row_id=memory_id)
        assert memory is not None
        if (
            state.run_checkpoint != "archived"
            or state.scene_status != "archived"
            or state.current_final_scene_row_id != final_scene.row_id
            or state.current_bundle_id != bundle["bundle_id"]
            or state.current_bundle_hash != bundle["bundle_snapshot_hash"]
            or final_scene.status != "archived"
            or memory.scene_id != scene_id
            or memory.chapter_id != scene.chapter_id
            or memory.final_scene_row_id != final_scene.row_id
            or memory.source_bundle_id != bundle["bundle_id"]
            or memory.content != final_scene.content
            or memory.active_flag != 1
            or memory.runtime_eligible != 1
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archived checkpoint product graph is inconsistent",
                status_code=409,
            )
        return final_scene

    def _load_checkpoint_bundle(self, scene_id: str) -> dict[str, Any]:
        bundle_id = self._checkpoint_artifact(
            "bundle_id", expected_node_at_least="bundle_ready"
        )
        expected_hash = self._checkpoint_hash("bundle")
        bundle = (
            self.session.get(SceneBundle, bundle_id)
            if isinstance(bundle_id, str)
            else None
        )
        if bundle is None:
            raise DomainError(
                "RUN_CHECKPOINT_OUTPUT_MISSING",
                "checkpoint bundle output is missing",
                status_code=409,
                details={"bundle_id": bundle_id},
            )
        if bundle.scene_id != scene_id or bundle.bundle_snapshot_hash != expected_hash:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint bundle identity/hash mismatch",
                status_code=409,
            )
        bundle_integrity = verify_bundle_snapshot_hash(
            bundle.frozen_snapshot_json,
            expected_hash=bundle.bundle_snapshot_hash,
        )
        if not bundle_integrity["valid"]:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint bundle snapshot no longer matches its recorded hash",
                status_code=409,
                details={
                    "bundle_id": bundle.bundle_id,
                    "bundle_integrity": bundle_integrity,
                },
            )
        return {
            "bundle_id": bundle.bundle_id,
            "bundle_snapshot_hash": bundle.bundle_snapshot_hash,
            "snapshot": bundle.frozen_snapshot_json,
        }

    def _load_checkpoint_draft(
        self,
        scene_id: str,
        *,
        ref_key: str,
        expected_stage: str,
        expected_node_at_least: str,
        result_type: str,
    ) -> NeutralGenerationResult | StyleGenerationResult:
        row_id = self._checkpoint_artifact(
            ref_key, expected_node_at_least=expected_node_at_least
        )
        row = self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        if row is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        assert row is not None
        bundle = self._load_checkpoint_bundle(scene_id)
        hash_key = "draft" if result_type == "neutral" else "selected_draft"
        if result_type == "neutral":
            llm_call_id = self._checkpoint_artifact(
                "neutral_llm_call_id",
                expected_node_at_least=expected_node_at_least,
            )
            execution_step_key = self._checkpoint_artifact(
                "neutral_execution_step_key",
                expected_node_at_least=expected_node_at_least,
            )
            artifact_execution_id = self._checkpoint_artifact(
                "neutral_artifact_execution_id",
                expected_node_at_least=expected_node_at_least,
            )
        else:
            llm_call_id = self._checkpoint_artifact(
                "style_llm_call_id",
                expected_node_at_least=expected_node_at_least,
            )
            execution_step_key = self._checkpoint_artifact(
                "style_execution_step_key",
                expected_node_at_least=expected_node_at_least,
            )
            artifact_execution_id = self._checkpoint_artifact(
                "style_artifact_execution_id",
                expected_node_at_least=expected_node_at_least,
            )
        artifact_execution_id = self._validate_artifact_execution_owner(
            artifact_execution_id
        )
        generation_parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            execution_id=artifact_execution_id,
        )
        try:
            self._validate_settled_parent_ledger(generation_parent)
        except LLMAccountingError as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{result_type} checkpoint generation attempt ledger is invalid",
                status_code=409,
                details={"llm_call_id": llm_call_id, "error_code": exc.code},
            ) from exc
        if (
            row.scene_id != scene_id
            or row.stage != expected_stage
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != llm_call_id
            or self._text_hash(row.content) != self._checkpoint_hash(hash_key)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint draft identity/hash mismatch",
                status_code=409,
            )
        kwargs = {
            "row_id": row.row_id,
            "content": row.content,
            "llm_call_id": llm_call_id,
            "bundle_id": bundle["bundle_id"],
            "bundle_hash": bundle["bundle_snapshot_hash"],
            "execution_step_key": execution_step_key,
            "artifact_execution_id": artifact_execution_id,
        }
        if result_type == "neutral":
            return NeutralGenerationResult(**kwargs)
        return StyleGenerationResult(**kwargs)

    @staticmethod
    def _style_slot_identity(slot_key: str) -> tuple[str, int]:
        parts = slot_key.split(":") if isinstance(slot_key, str) else []
        if (
            len(parts) != 2
            or parts[0] not in {"initial", "topup"}
            or not parts[1].isdigit()
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style work-item slot key is invalid",
                status_code=409,
            )
        index = int(parts[1])
        if (parts[0] == "initial" and index < 0) or (parts[0] == "topup" and index < 1):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style work-item slot index is invalid",
                status_code=409,
            )
        return parts[0], index

    def _style_artifact_descriptor(
        self,
        product: StyleGenerationResult,
        *,
        phase: str,
        source_neutral_draft_row_id: str,
        source_base_row_id: str | None,
    ) -> dict[str, Any]:
        row = self.session.get(SceneDraft, product.row_id)
        if row is None:
            self._raise_checkpoint_output_missing(row_id=product.row_id)
        assert row is not None
        owner = product.artifact_execution_id or self._execution_id
        if not isinstance(owner, str):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style artifact owner is missing",
                status_code=409,
            )
        return {
            "phase": phase,
            "row_id": product.row_id,
            "content_hash": self._text_hash(product.content),
            "stage": row.stage,
            "source_neutral_draft_row_id": source_neutral_draft_row_id,
            "source_base_row_id": source_base_row_id,
            "bundle_id": product.bundle_id,
            "bundle_hash": product.bundle_hash,
            "llm_call_id": product.llm_call_id,
            "execution_step_key": product.execution_step_key,
            "artifact_execution_id": owner,
        }

    def _update_style_work_item(
        self,
        work_items: list[dict[str, Any]],
        *,
        slot_key: str,
        phase: str,
        product: StyleGenerationResult,
        metadata: dict[str, Any],
        neutral_draft_row_id: str,
    ) -> None:
        kind, slot_index = self._style_slot_identity(slot_key)
        slot_order = metadata.get("slot_order")
        if (
            phase not in {"base", "final"}
            or not isinstance(slot_order, int)
            or slot_order < 0
            or metadata.get("source_neutral_draft_row_id") != neutral_draft_row_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style product callback metadata is invalid",
                status_code=409,
            )
        item = next(
            (
                candidate
                for candidate in work_items
                if candidate.get("slot_key") == slot_key
            ),
            None,
        )
        if phase == "base":
            if item is not None or slot_order != len(work_items):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style base phase is not a monotonic prefix",
                    status_code=409,
                )
            descriptor = self._style_artifact_descriptor(
                product,
                phase="base",
                source_neutral_draft_row_id=neutral_draft_row_id,
                source_base_row_id=None,
            )
            if descriptor["stage"] != "style_draft":
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style base product has the wrong stage",
                    status_code=409,
                )
            work_items.append(
                {
                    "slot_key": slot_key,
                    "slot_order": slot_order,
                    "kind": kind,
                    "slot_index": slot_index,
                    "base": descriptor,
                    "gate_decision": None,
                    "de_template_outcome": None,
                    "final": None,
                }
            )
            return
        if (
            item is None
            or item.get("slot_order") != slot_order
            or item.get("final") is not None
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style final phase has no matching base prefix",
                status_code=409,
            )
        gate_decision = metadata.get("gate_decision")
        de_template_outcome = metadata.get("de_template_outcome")
        source_base_row_id = metadata.get("source_base_row_id")
        if (
            not isinstance(gate_decision, dict)
            or not isinstance(gate_decision.get("triggered"), bool)
            or not isinstance(de_template_outcome, dict)
            or de_template_outcome.get("status")
            not in {"not_required", "completed", "failed", "rejected"}
            or source_base_row_id != item["base"]["row_id"]
            or (
                not gate_decision["triggered"]
                and de_template_outcome.get("status") != "not_required"
            )
            or (
                gate_decision["triggered"]
                and de_template_outcome.get("status") == "not_required"
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style final gate/source metadata is invalid",
                status_code=409,
            )
        if de_template_outcome["status"] == "completed" and (
            de_template_outcome.get("llm_call_id") != product.llm_call_id
            or de_template_outcome.get("execution_step_key")
            != product.execution_step_key
            or de_template_outcome.get("accounting_status") != "settled"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "completed de-template outcome is invalid",
                status_code=409,
            )
        if de_template_outcome["status"] == "failed" and (
            not isinstance(de_template_outcome.get("llm_call_id"), str)
            or not isinstance(de_template_outcome.get("execution_step_key"), str)
            or not isinstance(de_template_outcome.get("artifact_execution_id"), str)
            or de_template_outcome.get("accounting_status")
            not in {"failed", "rejected"}
            or not isinstance(de_template_outcome.get("error_code"), str)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "failed de-template outcome is invalid",
                status_code=409,
            )
        if de_template_outcome["status"] == "rejected" and (
            not isinstance(de_template_outcome.get("llm_call_id"), str)
            or not isinstance(de_template_outcome.get("execution_step_key"), str)
            or not isinstance(de_template_outcome.get("artifact_execution_id"), str)
            or de_template_outcome.get("accounting_status") != "settled"
            or not isinstance(de_template_outcome.get("row_id"), str)
            or not isinstance(de_template_outcome.get("acceptance"), dict)
            or de_template_outcome["acceptance"].get("accepted") is not False
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "rejected de-template outcome is invalid",
                status_code=409,
            )
        descriptor = self._style_artifact_descriptor(
            product,
            phase="final",
            source_neutral_draft_row_id=neutral_draft_row_id,
            source_base_row_id=source_base_row_id,
        )
        expected_stage = (
            "de_template"
            if de_template_outcome["status"] == "completed"
            else "style_draft"
        )
        if descriptor["stage"] != expected_stage:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style final product contradicts its gate",
                status_code=409,
            )
        item["gate_decision"] = deepcopy(gate_decision)
        item["de_template_outcome"] = deepcopy(de_template_outcome)
        item["final"] = descriptor

    def _validate_style_artifact_descriptor(
        self,
        descriptor: Any,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        expected_phase: str,
        expected_stage: str,
        expected_step_key: str,
        source_neutral_draft_row_id: str,
        source_base_row_id: str | None,
    ) -> StyleGenerationResult:
        if not isinstance(descriptor, dict):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style artifact descriptor is invalid",
                status_code=409,
            )
        row_id = descriptor.get("row_id")
        row = self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        if row is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        assert row is not None
        owner = self._validate_artifact_execution_owner(
            descriptor.get("artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=descriptor.get("llm_call_id"),
            execution_step_key=descriptor.get("execution_step_key"),
            execution_id=owner,
        )
        if (
            descriptor.get("phase") != expected_phase
            or descriptor.get("stage") != expected_stage
            or descriptor.get("execution_step_key") != expected_step_key
            or descriptor.get("source_neutral_draft_row_id")
            != source_neutral_draft_row_id
            or descriptor.get("source_base_row_id") != source_base_row_id
            or descriptor.get("bundle_id") != bundle["bundle_id"]
            or descriptor.get("bundle_hash") != bundle["bundle_snapshot_hash"]
            or row.scene_id != scene_id
            or row.stage != expected_stage
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != descriptor.get("llm_call_id")
            or self._text_hash(row.content) != descriptor.get("content_hash")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style artifact identity/source/hash mismatch",
                status_code=409,
            )
        attempt_step = (
            "de_template" if expected_stage == "de_template" else "style_draft"
        )
        matching_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == attempt_step,
                AttemptTracker.status == "completed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            expected_source_key = (
                "source_style_draft_row_id"
                if expected_stage == "de_template"
                else "source_draft_row_id"
            )
            expected_source = (
                source_base_row_id
                if expected_stage == "de_template"
                else source_neutral_draft_row_id
            )
            if (
                details.get("row_id") == row.row_id
                and details.get("llm_call_id") == row.generation_llm_call_id
                and details.get(expected_source_key) == expected_source
            ):
                matching_attempts.append(attempt)
        if len(matching_attempts) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style artifact attempt ledger is invalid",
                status_code=409,
            )
        return StyleGenerationResult(
            row_id=row.row_id,
            content=row.content,
            llm_call_id=row.generation_llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=expected_step_key,
            artifact_execution_id=owner,
        )

    def _validate_style_work_items(
        self,
        work_items: Any,
        *,
        scene_id: str,
        expected_initial_count: int,
        require_complete: bool,
    ) -> list[tuple[StyleGenerationResult, StyleGenerationResult | None]]:
        if not isinstance(work_items, list) or (require_complete and not work_items):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style work-item cursor is invalid",
                status_code=409,
            )
        bundle = self._load_checkpoint_bundle(scene_id)
        neutral_row_id = self._checkpoint_artifact(
            "neutral_draft_row_id", expected_node_at_least="neutral_ready"
        )
        products: list[tuple[StyleGenerationResult, StyleGenerationResult | None]] = []
        saw_partial = False
        for order, item in enumerate(work_items):
            expected_slot = (
                f"initial:{order}"
                if order < expected_initial_count
                else f"topup:{order - expected_initial_count + 1}"
            )
            if (
                saw_partial
                or not isinstance(item, dict)
                or item.get("slot_key") != expected_slot
                or item.get("slot_order") != order
                or item.get("kind")
                != ("initial" if order < expected_initial_count else "topup")
                or item.get("slot_index")
                != (
                    order
                    if order < expected_initial_count
                    else order - expected_initial_count + 1
                )
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style work-item prefix/slot identity is invalid",
                    status_code=409,
                )
            base_step_key = (
                f"style_draft:{order}"
                if order < expected_initial_count
                else f"style_draft:topup:{order - expected_initial_count + 1}"
            )
            base = self._validate_style_artifact_descriptor(
                item.get("base"),
                scene_id=scene_id,
                bundle=bundle,
                expected_phase="base",
                expected_stage="style_draft",
                expected_step_key=base_step_key,
                source_neutral_draft_row_id=neutral_row_id,
                source_base_row_id=None,
            )
            final_descriptor = item.get("final")
            if final_descriptor is None:
                if (
                    require_complete
                    or item.get("gate_decision") is not None
                    or item.get("de_template_outcome") is not None
                    or order != len(work_items) - 1
                ):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "style work-item final prefix is incomplete",
                        status_code=409,
                    )
                saw_partial = True
                products.append((base, None))
                continue
            gate = item.get("gate_decision")
            outcome = item.get("de_template_outcome")
            if (
                not isinstance(gate, dict)
                or not isinstance(gate.get("triggered"), bool)
                or not isinstance(outcome, dict)
                or outcome.get("status")
                not in {"not_required", "completed", "failed", "rejected"}
                or (not gate["triggered"] and outcome.get("status") != "not_required")
                or (gate["triggered"] and outcome.get("status") == "not_required")
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style work-item gate decision is invalid",
                    status_code=409,
                )
            final_stage = (
                "de_template" if outcome["status"] == "completed" else "style_draft"
            )
            final_step_key = (
                f"{base_step_key}:de_template"
                if outcome["status"] == "completed"
                else base_step_key
            )
            final = self._validate_style_artifact_descriptor(
                final_descriptor,
                scene_id=scene_id,
                bundle=bundle,
                expected_phase="final",
                expected_stage=final_stage,
                expected_step_key=final_step_key,
                source_neutral_draft_row_id=neutral_row_id,
                source_base_row_id=base.row_id,
            )
            if outcome["status"] in {"not_required", "failed", "rejected"} and (
                final.row_id != base.row_id
                or final.llm_call_id != base.llm_call_id
                or final.content != base.content
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "fallback style product is not base=final",
                    status_code=409,
                )
            if outcome["status"] == "completed" and final.row_id == base.row_id:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "de-template product does not have independent lineage",
                    status_code=409,
                )
            if outcome["status"] == "completed" and (
                outcome.get("llm_call_id") != final.llm_call_id
                or outcome.get("execution_step_key") != final.execution_step_key
                or outcome.get("artifact_execution_id") != final.artifact_execution_id
                or outcome.get("accounting_status") != "settled"
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "completed de-template outcome is misbound",
                    status_code=409,
                )
            if outcome["status"] == "failed":
                self._validate_failed_style_de_template_outcome(
                    outcome,
                    scene_id=scene_id,
                    bundle=bundle,
                    execution_step_key=f"{base_step_key}:de_template",
                    source_base_row_id=base.row_id,
                )
            if outcome["status"] == "rejected":
                self._validate_rejected_style_de_template_outcome(
                    outcome,
                    scene_id=scene_id,
                    bundle=bundle,
                    execution_step_key=f"{base_step_key}:de_template",
                    source_base_row_id=base.row_id,
                )
            products.append((base, final))
        return products

    def _validate_rejected_style_de_template_outcome(
        self,
        outcome: dict[str, Any],
        *,
        scene_id: str,
        bundle: dict[str, Any],
        execution_step_key: str,
        source_base_row_id: str,
    ) -> None:
        owner = self._validate_artifact_execution_owner(
            outcome.get("artifact_execution_id")
        )
        call = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=outcome.get("llm_call_id"),
            execution_step_key=outcome.get("execution_step_key"),
            execution_id=owner,
        )
        row_id = outcome.get("row_id")
        row = self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        acceptance = outcome.get("acceptance")
        if (
            call.step != "de_template"
            or outcome.get("status") != "rejected"
            or outcome.get("execution_step_key") != execution_step_key
            or outcome.get("accounting_status") != "settled"
            or not isinstance(acceptance, dict)
            or acceptance.get("accepted") is not False
            or row is None
            or row.scene_id != scene_id
            or row.stage != "de_template"
            or row.status != "rejected"
            or row.source_bundle_id != bundle["bundle_id"]
            or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or row.generation_llm_call_id != call.llm_call_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "rejected de-template artifact ledger is invalid",
                status_code=409,
            )
        matching_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "completed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            if (
                details.get("row_id") == row.row_id
                and details.get("llm_call_id") == call.llm_call_id
                and details.get("source_style_draft_row_id")
                == source_base_row_id
                and details.get("acceptance") == acceptance
            ):
                matching_attempts.append(attempt)
        if len(matching_attempts) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "rejected de-template attempt is missing or duplicated",
                status_code=409,
            )

    def _validate_failed_style_de_template_outcome(
        self,
        outcome: dict[str, Any],
        *,
        scene_id: str,
        bundle: dict[str, Any],
        execution_step_key: str,
        source_base_row_id: str,
    ) -> None:
        owner = self._validate_artifact_execution_owner(
            outcome.get("artifact_execution_id")
        )
        llm_call_id = outcome.get("llm_call_id")
        call = (
            self.session.get(LlmCall, llm_call_id)
            if isinstance(llm_call_id, str)
            else None
        )
        if (
            call is None
            or call.scene_id != scene_id
            or call.step != "de_template"
            or call.execution_id != owner
            or call.execution_step_key != execution_step_key
            or call.accounting_status not in {"failed", "rejected"}
            or outcome.get("execution_step_key") != execution_step_key
            or outcome.get("accounting_status") != call.accounting_status
            or not isinstance(outcome.get("error_code"), str)
            or outcome.get("error_code") != call.error_code
            or (
                call.accounting_status == "failed"
                and call.request_dispatched_at is None
            )
            or (
                call.accounting_status == "rejected"
                and call.request_dispatched_at is not None
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "failed de-template call ledger is invalid",
                status_code=409,
            )
        provider_attempts = (
            self.session.execute(
                select(LlmCallAttempt).where(
                    LlmCallAttempt.llm_call_id == call.llm_call_id
                )
            )
            .scalars()
            .all()
        )
        if provider_attempts:
            ordinals = [attempt.provider_attempt_no for attempt in provider_attempts]
            numeric_fields = (
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "latency_ms",
            )
            aggregate_fields = numeric_fields
            if (
                sorted(ordinals) != list(range(len(ordinals)))
                or any(
                    attempt.accounting_status
                    in {"reserved", "usage_exceeds_reservation"}
                    or any(
                        not isinstance(getattr(attempt, field), int)
                        or getattr(attempt, field) < 0
                        for field in numeric_fields
                    )
                    or attempt.budget_charged_tokens > attempt.reserved_tokens
                    or attempt.total_tokens
                    != attempt.prompt_tokens + attempt.completion_tokens
                    for attempt in provider_attempts
                )
                or any(
                    getattr(call, field)
                    != sum(getattr(attempt, field) for attempt in provider_attempts)
                    for field in aggregate_fields
                )
                or (
                    call.accounting_status == "failed"
                    and not any(
                        attempt.request_dispatched_at is not None
                        for attempt in provider_attempts
                    )
                )
                or (
                    call.accounting_status == "rejected"
                    and any(
                        attempt.request_dispatched_at is not None
                        for attempt in provider_attempts
                    )
                )
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "failed de-template provider-attempt ledger is invalid",
                    status_code=409,
                )
        failed_attempts = []
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "failed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            if (
                details.get("llm_call_id") == call.llm_call_id
                and details.get("source_draft_row_id") == source_base_row_id
                and details.get("error_code") == outcome.get("error_code")
            ):
                failed_attempts.append(attempt)
        if len(failed_attempts) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "failed de-template attempt is missing or duplicated",
                status_code=409,
            )

    def _load_partial_style_work_items(
        self,
        scene_id: str,
        *,
        expected_initial_count: int,
    ) -> list[dict[str, Any]]:
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        work_items = refs.get("style_work_items") if isinstance(refs, dict) else None
        if work_items is None:
            return []
        if (
            state.run_checkpoint != "hard_qc_ready"
            or refs.get("style_initial_candidate_count") != expected_initial_count
            or self._json_hash(work_items) != self._checkpoint_hash("style_work_items")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "partial style work-item cursor is invalid",
                status_code=409,
            )
        products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=expected_initial_count,
            require_complete=False,
        )
        last_order = len(products) - 1
        last_phase = 1 if products and products[-1][1] is not None else 0
        if payload.get("sub_index") != last_order * 2 + last_phase:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "partial style subcursor does not match its phase",
                status_code=409,
            )
        return deepcopy(work_items)

    def _style_resume_products(
        self,
        work_items: list[dict[str, Any]],
        *,
        scene_id: str,
    ) -> tuple[dict[str, StyleGenerationResult], dict[str, StyleGenerationResult]]:
        if not work_items:
            return {}, {}
        initial_count = sum(1 for item in work_items if item.get("kind") == "initial")
        products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=initial_count,
            require_complete=False,
        )
        bases: dict[str, StyleGenerationResult] = {}
        finals: dict[str, StyleGenerationResult] = {}
        for item, (base, final) in zip(work_items, products, strict=True):
            if final is None:
                bases[item["slot_key"]] = base
            else:
                finals[item["slot_key"]] = final
        return bases, finals

    def _load_style_checkpoint_candidates(
        self, scene_id: str
    ) -> list[StyleGenerationResult]:
        row_ids = self._checkpoint_artifact(
            "candidate_row_ids", expected_node_at_least="style_ready"
        )
        selected = self._checkpoint_artifact(
            "style_draft_row_id", expected_node_at_least="style_ready"
        )
        if not isinstance(row_ids, list) or not row_ids or row_ids[0] != selected:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style candidate checkpoint is invalid",
                status_code=409,
            )
        work_items = self._checkpoint_artifact(
            "style_work_items", expected_node_at_least="style_ready"
        )
        initial_count = self._checkpoint_artifact(
            "style_initial_candidate_count", expected_node_at_least="style_ready"
        )
        if (
            not isinstance(initial_count, int)
            or initial_count < 1
            or self._json_hash(work_items) != self._checkpoint_hash("style_work_items")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style work-item completion ledger is invalid",
                status_code=409,
            )
        lineage_products = self._validate_style_work_items(
            work_items,
            scene_id=scene_id,
            expected_initial_count=initial_count,
            require_complete=True,
        )
        final_by_row_id = {
            final.row_id: final
            for _base, final in lineage_products
            if final is not None
        }
        if len(final_by_row_id) != len(lineage_products) or set(row_ids) != set(
            final_by_row_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style candidate ordering is detached from work-item lineage",
                status_code=409,
            )
        results: list[StyleGenerationResult] = []
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_ids = self._checkpoint_artifact(
            "llm_call_ids", expected_node_at_least="style_ready"
        )
        step_keys = self._checkpoint_artifact(
            "style_execution_step_keys", expected_node_at_least="style_ready"
        )
        execution_ids = self._checkpoint_artifact(
            "style_artifact_execution_ids", expected_node_at_least="style_ready"
        )
        checkpoint_payload = self._active_checkpoint_state().run_checkpoint_json or {}
        checkpoint_refs = checkpoint_payload.get("artifact_refs") or {}
        checkpoint_hashes = checkpoint_payload.get("artifact_hashes") or {}
        ranking_audits = checkpoint_refs.get("style_candidate_rankings")
        if (
            not isinstance(llm_call_ids, list)
            or not isinstance(step_keys, list)
            or len(llm_call_ids) != len(row_ids)
            or len(step_keys) != len(row_ids)
            or not isinstance(execution_ids, list)
            or len(execution_ids) != len(row_ids)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style candidate ledger references are invalid",
                status_code=409,
            )
        if ranking_audits is not None and (
            not isinstance(ranking_audits, list)
            or len(ranking_audits) != len(row_ids)
            or self._json_hash(ranking_audits)
            != checkpoint_hashes.get("style_candidate_rankings")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style candidate ranking audit is invalid",
                status_code=409,
            )
        for index, row_id in enumerate(row_ids):
            lineage_result = final_by_row_id.get(row_id)
            if lineage_result is None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style candidate has no final work item",
                    status_code=409,
                )
            row = (
                self.session.get(SceneDraft, row_id)
                if isinstance(row_id, str)
                else None
            )
            if row is None:
                self._raise_checkpoint_output_missing(row_id=row_id)
            assert row is not None
            self._validate_checkpoint_llm_output(
                scene_id=scene_id,
                llm_call_id=llm_call_ids[index],
                execution_step_key=step_keys[index],
                execution_id=self._validate_artifact_execution_owner(
                    execution_ids[index]
                ),
            )
            if (
                row.scene_id != scene_id
                or row.stage not in {"style_draft", "de_template"}
                or row.source_bundle_id != bundle["bundle_id"]
                or row.source_bundle_hash != bundle["bundle_snapshot_hash"]
                or row.generation_llm_call_id != llm_call_ids[index]
                or lineage_result.llm_call_id != llm_call_ids[index]
                or lineage_result.execution_step_key != step_keys[index]
                or lineage_result.artifact_execution_id != execution_ids[index]
                or self._text_hash(row.content)
                != self._checkpoint_hash(f"style_ready_candidate_{index}")
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "style candidate identity/source/hash mismatch",
                    status_code=409,
                )
            if index == 0 and self._text_hash(row.content) != self._checkpoint_hash(
                "selected_draft"
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "selected style draft hash mismatch",
                    status_code=409,
                )
            results.append(
                StyleGenerationResult(
                    row_id=row.row_id,
                    content=row.content,
                    llm_call_id=llm_call_ids[index],
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    execution_step_key=step_keys[index],
                    artifact_execution_id=execution_ids[index],
                    ranking_audit=(
                        deepcopy(ranking_audits[index])
                        if isinstance(ranking_audits, list)
                        and isinstance(ranking_audits[index], dict)
                        else None
                    ),
                )
            )
        return results

    def _load_selected_style_checkpoint(self, scene_id: str) -> StyleGenerationResult:
        candidates = self._load_style_checkpoint_candidates(scene_id)
        refs = (self._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        selected_row_id = refs.get("selected_row_id")
        if selected_row_id is None:
            return candidates[0]
        selected = [
            candidate for candidate in candidates if candidate.row_id == selected_row_id
        ]
        if len(selected) != 1:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selected style candidate is absent or ambiguous in the durable prefix",
                status_code=409,
            )
        return selected[0]

    def _load_hard_qc_checkpoint(self, scene_id: str) -> HardQcDecision:
        qc_report_id = self._checkpoint_artifact(
            "qc_report_id", expected_node_at_least="hard_qc_ready"
        )
        report = (
            self.session.get(QcReport, qc_report_id)
            if isinstance(qc_report_id, str)
            else None
        )
        if report is None:
            self._raise_checkpoint_output_missing(row_id=qc_report_id)
        assert report is not None
        bundle = self._load_checkpoint_bundle(scene_id)
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        source_draft_row_id = refs.get("hard_qc_source_draft_row_id")
        hard_qc_execution_id = self._validate_artifact_execution_owner(
            refs.get("hard_qc_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=refs.get("hard_qc_llm_call_id"),
            execution_step_key=refs.get("hard_qc_execution_step_key"),
            execution_id=hard_qc_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        if (
            report.scene_id != scene_id
            or report.qc_type != "hard_qc"
            or report.source_draft_row_id != source_draft_row_id
            or source_draft_row_id != refs.get("neutral_draft_row_id")
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get("hard_qc_bundle_id") != bundle["bundle_id"]
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "hard QC checkpoint identity/source mismatch",
                status_code=409,
            )
        decision = HardQcDecision(
            branch=str(refs.get("branch") or "continue"),
            qc_report_id=report.qc_report_id,
            human_review_event_id=refs.get("human_review_event_id"),
            resolution_code=str(
                refs.get("resolution_code") or report.resolution_code or ""
            ),
            next_action=str(refs.get("next_action") or report.next_action or ""),
            should_continue=bool(refs.get("should_continue")),
            stop_reason=refs.get("stop_reason"),
            llm_call_id=refs.get("hard_qc_llm_call_id"),
            execution_step_key=refs.get("hard_qc_execution_step_key"),
        )
        decision_summary = {
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "human_review_event_id": decision.human_review_event_id,
            "resolution_code": decision.resolution_code,
            "next_action": decision.next_action,
            "stop_reason": decision.stop_reason,
            "should_continue": decision.should_continue,
            "llm_call_id": decision.llm_call_id,
            "execution_step_key": decision.execution_step_key,
        }
        if self._json_hash(decision_summary) != self._checkpoint_hash(
            "hard_qc_decision"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "hard QC decision hash mismatch",
                status_code=409,
            )
        if self._json_hash(self._qc_report_snapshot(report)) != self._checkpoint_hash(
            "hard_qc_report"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "hard QC report hash mismatch",
                status_code=409,
            )
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="hard_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=None,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        return decision

    def _soft_checkpoint_progress(self) -> int:
        state = self._active_checkpoint_state()
        current = state.run_checkpoint
        if current not in RUN_CHECKPOINT_ORDER:
            return -1
        soft_index = RUN_CHECKPOINT_ORDER.index("soft_qc_ready")
        current_index = RUN_CHECKPOINT_ORDER.index(current)
        if current_index < soft_index:
            return -1
        if current_index > soft_index:
            return 3
        payload = state.run_checkpoint_json or {}
        sub_index = payload.get("sub_index") if isinstance(payload, dict) else None
        if (
            isinstance(sub_index, int)
            and not isinstance(sub_index, bool)
            and sub_index in {0, 1, 2, 3}
        ):
            return sub_index
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if (
            sub_index is None
            and isinstance(refs, dict)
            and refs.get("soft_qc_report_id")
        ):
            # 兼容子游标上线前已经完整提交的 soft checkpoint。
            return 3
        raise DomainError(
            "RUN_CHECKPOINT_CORRUPT",
            "soft QC checkpoint sub-index is invalid",
            status_code=409,
        )

    def _ensure_soft_qc_subcheckpoints(
        self,
        *,
        scene: SceneCard,
        contract: Any,
        bundle: dict[str, Any],
        criticality: Any,
        selected_style_generation: StyleGenerationResult,
        optional_spend_allowed,
    ) -> tuple[SoftQcDecision, StyleGenerationResult]:
        progress = self._soft_checkpoint_progress()
        if progress >= 3:
            return self._load_soft_qc_checkpoint(
                scene.scene_id,
                selected_style_generation=selected_style_generation,
            )

        scene_id = scene.scene_id
        style_generation = selected_style_generation
        if progress < 0:
            critique_outcome = "unchanged"
            critique_skip_reason: str | None = None
            patch_failure_product: dict[str, Any] | None = None
            from novel_system.services.auto_critique import llm_auto_critique

            critique_spend_allowed = optional_spend_allowed()
            critique_runner = (
                self._resolve_auto_critique_runner() if critique_spend_allowed else None
            )
            chapter = self.session.get(ChapterGoal, scene.chapter_id)
            critique_step_key = "soft_qc:auto_critique:0"
            critique_context = LLMCallContext(
                scope_type="scene",
                scope_id=scene.scene_id,
                project_id=scene.project_id
                or (chapter.project_id if chapter is not None else None),
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                node_id="soft_qc",
                step=critique_step_key,
                execution_id=self._execution_id,
                execution_step_key=(
                    critique_step_key if self._execution_id is not None else None
                ),
                run_job_id=self._run_job_id,
                provider_execution_mode=("online"),
            )
            self._reconcile_execution_step(critique_step_key)
            skip_critique = bool(getattr(criticality, "skip_critique", False))
            from novel_system.services.auto_critique import auto_critique

            critique = self._recover_auto_critique_rejected_product(
                critique_context,
                auto_critique(
                    style_generation.content,
                    # A durable rejected parent proves this pass reached its call path;
                    # its recovered deterministic product therefore is not a skip result.
                    skip_critique=False,
                ),
                allow_retry=(
                    critique_runner is not None
                    and not skip_critique
                    and getattr(
                        critique_runner,
                        "provider_execution_mode",
                        "online",
                    )
                    == "online"
                ),
            )
            if critique is None:
                critique = llm_auto_critique(
                    style_generation.content,
                    scene_context=self._scene_critique_context(scene, contract),
                    session=self.session,
                    llm_runner=critique_runner,
                    llm_context=critique_context,
                    skip_critique=skip_critique,
                    not_invoked_reason=(
                        "budget_or_candidate_cap"
                        if not critique_spend_allowed
                        else "feature_disabled"
                    ),
                )
            critique_summary = critique.product_snapshot()

            self._reconcile_execution_step("soft_patch:auto_critique:0")
            patch_spend_allowed = bool(
                critique.should_rewrite and optional_spend_allowed()
            )
            recovered_patch_failure = (
                self._recover_auto_critique_patch_rejected_product(
                    scene_id,
                    allow_retry=patch_spend_allowed,
                )
            )
            if recovered_patch_failure is not None:
                if not critique.should_rewrite:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique patch rejection conflicts with a non-rewrite decision",
                    )
                patch_failure_product = recovered_patch_failure
                critique_outcome = "patch_failed"
                critique_skip_reason = "patch_failed:rejected_before_dispatch"
            elif critique.should_rewrite:
                if not patch_spend_allowed:
                    critique_outcome = "patch_skipped"
                    critique_skip_reason = "budget_or_candidate_cap"
                    _LOGGER.warning(
                        "critique patch skipped for scene %s (budget/candidate cap)",
                        scene_id,
                    )
                else:
                    try:
                        from novel_system.services.auto_critique import (
                            format_critique_brief,
                        )

                        critique_brief = self._pov_desensitize_brief(
                            scene,
                            contract,
                            format_critique_brief(critique),
                        )
                        style_generation = (
                            self.scene_generation_service.generate_style_patch(
                                scene_id,
                                bundle,
                                source_style_draft_row_id=style_generation.row_id,
                                source_style_content=style_generation.content,
                                rewrite_brief=critique_brief,
                                source_qc_report_id=f"auto_critique_{scene_id}",
                                execution_step_key="soft_patch:auto_critique:0",
                            )
                        )
                        critique_outcome = "patched"
                    except Exception as exc:
                        if is_llm_control_plane_failure(exc):
                            raise
                        patch_failure_product = (
                            self._build_auto_critique_patch_failure_product(
                                scene_id,
                                exc,
                            )
                        )
                        critique_outcome = "patch_failed"
                        critique_skip_reason = f"patch_failed:{exc.__class__.__name__}"
                        _LOGGER.warning(
                            "auto-critique patch failed for scene %s; keeping unpatched style draft",
                            scene_id,
                            exc_info=True,
                        )

            reused_selected_generation = (
                style_generation.llm_call_id == selected_style_generation.llm_call_id
            )
            reused_parent = (
                self.session.get(LlmCall, style_generation.llm_call_id)
                if reused_selected_generation
                else None
            )
            generation_parent = self._validate_generation_before_checkpoint(
                scene_id,
                style_generation,
                expected_provider_execution_mode=(
                    self._parent_execution_mode(reused_parent)
                    if reused_parent is not None
                    else getattr(
                        self.scene_generation_service._llm_runner,
                        "provider_execution_mode",
                        "online",
                    )
                ),
            )
            generation_provider_execution_mode = self._parent_execution_mode(
                generation_parent
            )
            self._validate_auto_critique_product_semantics(
                critique_summary,
                source_content=selected_style_generation.content,
                patch_outcome=critique_outcome,
            )
            critique_product_hash = self._json_hash(critique_summary)
            if critique.llm_call_id is not None:
                critique_parent = self.session.get(LlmCall, critique.llm_call_id)
                if critique_parent is None:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique product parent disappeared before checkpoint commit",
                    )
                critique_parent.response_payload_summary = sanitize_audit_summary(
                    {
                        **dict(critique_parent.response_payload_summary or {}),
                        "auto_critique_product_hash": critique_product_hash,
                    }
                )
            # 该摘要只绑定本次事务所见的产品与父账本；不宣称抵抗可同步改写
            # checkpoint、parent summary 与 ledger 的全库特权篡改。
            self._validate_auto_critique_checkpoint(
                scene_id,
                critique_summary,
                source_content=selected_style_generation.content,
            )
            patch_failure_hash = (
                self._json_hash(patch_failure_product)
                if patch_failure_product is not None
                else None
            )
            if patch_failure_product is not None:
                patch_parent = self.session.get(
                    LlmCall,
                    patch_failure_product["llm_call_id"],
                )
                if patch_parent is None:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique patch failure parent disappeared before checkpoint commit",
                    )
                patch_parent.response_payload_summary = sanitize_audit_summary(
                    {
                        **dict(patch_parent.response_payload_summary or {}),
                        "auto_critique_patch_failure_hash": patch_failure_hash,
                    }
                )
                self._validate_auto_critique_patch_failure_checkpoint(
                    scene_id,
                    patch_failure_product,
                    validate_checkpoint_hash=False,
                )
            checkpoint_refs = {
                **self._soft_draft_refs(
                    prefix="soft_input",
                    generation=style_generation,
                    source_draft_row_id=selected_style_generation.row_id,
                    bundle=bundle,
                    provider_execution_mode=generation_provider_execution_mode,
                ),
                "soft_auto_critique_decision": critique_summary,
                "soft_auto_critique_outcome": critique_outcome,
                "soft_auto_critique_skip_reason": critique_skip_reason,
            }
            checkpoint_hashes = {
                "soft_input_draft": self._text_hash(style_generation.content),
                "soft_input_provider_execution_mode": self._text_hash(
                    generation_provider_execution_mode
                ),
                "soft_auto_critique_decision": critique_product_hash,
            }
            if patch_failure_product is not None:
                checkpoint_refs["soft_auto_critique_patch_failure"] = (
                    patch_failure_product
                )
                checkpoint_hashes["soft_auto_critique_patch_failure"] = (
                    patch_failure_hash
                )
            self._save_run_checkpoint(
                "soft_qc_ready",
                sub_index=0,
                artifact_refs=checkpoint_refs,
                artifact_hashes=checkpoint_hashes,
                branch=critique_outcome,
            )
            progress = 0
        else:
            style_generation = self._load_soft_draft_checkpoint(
                scene_id,
                prefix="soft_input",
                expected_source_draft_row_id=selected_style_generation.row_id,
                expected_stages={"style_draft", "de_template", "style_patch"},
            )

        if progress < 1:
            self._reconcile_execution_step("soft_qc:0")
            soft_qc0 = self.soft_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                source_draft_row_id=style_generation.row_id,
                source_draft_content=style_generation.content,
                execution_step_key="soft_qc:0",
            )
            patch_allowed = soft_qc0.branch == "patch" and optional_spend_allowed()
            if soft_qc0.branch == "patch" and not patch_allowed:
                qc0_skip_reason = "budget_or_candidate_cap"
                _LOGGER.warning(
                    "soft patch skipped for scene %s (budget/candidate cap)", scene_id
                )
            elif soft_qc0.branch == "human_review_required":
                qc0_skip_reason = "human_review_required"
            elif soft_qc0.branch != "patch":
                qc0_skip_reason = "no_patch_requested"
            else:
                qc0_skip_reason = None
            self._save_soft_qc_round_checkpoint(
                sub_index=1,
                round_index=0,
                decision=soft_qc0,
                source_generation=style_generation,
                bundle=bundle,
                patch_allowed=patch_allowed,
                skip_reason=qc0_skip_reason,
            )
            progress = 1
        else:
            soft_qc0 = self._load_soft_qc_round_checkpoint(
                scene_id,
                round_index=0,
                source_generation=style_generation,
            )
            patch_allowed, qc0_skip_reason = self._load_soft_qc0_branch_control(
                soft_qc0
            )

        if soft_qc0.branch == "patch" and patch_allowed:
            if progress < 2:
                rewrite_brief = self._pov_desensitize_brief(
                    scene,
                    contract,
                    self._rewrite_brief_from_report(soft_qc0.qc_report_id),
                )
                self._reconcile_execution_step("soft_patch:soft_qc:0")
                final_generation = self.scene_generation_service.generate_style_patch(
                    scene_id,
                    bundle,
                    source_style_draft_row_id=style_generation.row_id,
                    source_style_content=style_generation.content,
                    rewrite_brief=rewrite_brief,
                    source_qc_report_id=soft_qc0.qc_report_id,
                    execution_step_key="soft_patch:soft_qc:0",
                )
                self._save_run_checkpoint(
                    "soft_qc_ready",
                    sub_index=2,
                    artifact_refs=self._soft_draft_refs(
                        prefix="soft_patch",
                        generation=final_generation,
                        source_draft_row_id=style_generation.row_id,
                        bundle=bundle,
                        source_qc_report_id=soft_qc0.qc_report_id,
                    ),
                    artifact_hashes={
                        "soft_patch_draft": self._text_hash(final_generation.content)
                    },
                    branch="patch",
                )
            else:
                final_generation = self._load_soft_draft_checkpoint(
                    scene_id,
                    prefix="soft_patch",
                    expected_source_draft_row_id=style_generation.row_id,
                    expected_stages={"style_patch"},
                    expected_source_qc_report_id=soft_qc0.qc_report_id,
                )

            self._reconcile_execution_step("soft_qc:1")
            soft_qc = self.soft_qc_engine.evaluate(
                scene_id=scene_id,
                bundle=bundle,
                source_draft_row_id=final_generation.row_id,
                source_draft_content=final_generation.content,
                execution_step_key="soft_qc:1",
            )
            self._save_soft_qc_round_checkpoint(
                sub_index=3,
                round_index=1,
                decision=soft_qc,
                source_generation=final_generation,
                bundle=bundle,
                final_generation=final_generation,
                final_skip_reason=None,
            )
            return soft_qc, final_generation

        if progress >= 2:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft patch checkpoint exists for a non-patch QC0 branch",
                status_code=409,
            )
        self._save_soft_qc_round_checkpoint(
            sub_index=3,
            round_index=0,
            decision=soft_qc0,
            source_generation=style_generation,
            bundle=bundle,
            final_generation=style_generation,
            final_skip_reason=qc0_skip_reason,
        )
        return soft_qc0, style_generation

    def _soft_draft_refs(
        self,
        *,
        prefix: str,
        generation: StyleGenerationResult,
        source_draft_row_id: str,
        bundle: dict[str, Any],
        source_qc_report_id: str | None = None,
        provider_execution_mode: str | None = None,
    ) -> dict[str, Any]:
        refs = {
            f"{prefix}_draft_row_id": generation.row_id,
            f"{prefix}_llm_call_id": generation.llm_call_id,
            f"{prefix}_execution_step_key": generation.execution_step_key,
            f"{prefix}_artifact_execution_id": generation.artifact_execution_id
            or self._execution_id,
            f"{prefix}_source_draft_row_id": source_draft_row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
        }
        if source_qc_report_id is not None:
            refs[f"{prefix}_source_qc_report_id"] = source_qc_report_id
        if provider_execution_mode is not None:
            refs[f"{prefix}_provider_execution_mode"] = provider_execution_mode
        return refs

    def _load_soft_draft_checkpoint(
        self,
        scene_id: str,
        *,
        prefix: str,
        expected_source_draft_row_id: str,
        expected_stages: set[str],
        expected_source_qc_report_id: str | None = None,
    ) -> StyleGenerationResult:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        row_id = refs.get(f"{prefix}_draft_row_id")
        draft = (
            self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        )
        if draft is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        source_row_id = refs.get(f"{prefix}_source_draft_row_id")
        source = (
            self.session.get(SceneDraft, source_row_id)
            if isinstance(source_row_id, str)
            else None
        )
        if source is None:
            self._raise_checkpoint_output_missing(row_id=source_row_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        llm_call_id = refs.get(f"{prefix}_llm_call_id")
        execution_step_key = refs.get(f"{prefix}_execution_step_key")
        artifact_execution_id = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        generation_parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            execution_id=artifact_execution_id,
        )
        historical_execution_mode = refs.get(f"{prefix}_provider_execution_mode")
        if prefix == "soft_input" and (
            historical_execution_mode != "online"
            or self._text_hash(historical_execution_mode)
            != self._checkpoint_hash(f"{prefix}_provider_execution_mode")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{prefix} checkpoint provider execution mode snapshot is invalid",
                status_code=409,
            )
        if (
            draft.scene_id != scene_id
            or draft.stage not in expected_stages
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != llm_call_id
            or source_row_id != expected_source_draft_row_id
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or self._text_hash(draft.content)
            != self._checkpoint_hash(f"{prefix}_draft")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{prefix} checkpoint draft identity/source/hash mismatch",
                status_code=409,
            )
        if expected_source_qc_report_id is not None and (
            refs.get(f"{prefix}_source_qc_report_id") != expected_source_qc_report_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{prefix} checkpoint QC source mismatch",
                status_code=409,
            )
        try:
            self._validate_generation_parent_identity(
                scene_id=scene_id,
                parent=generation_parent,
                draft_stage=draft.stage,
                execution_step_key=execution_step_key,
                execution_id=artifact_execution_id,
                expected_provider_execution_mode=(
                    historical_execution_mode if prefix == "soft_input" else None
                ),
            )
            self._validate_settled_parent_ledger(generation_parent)
        except LLMAccountingError as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"{prefix} generation parent/physical-attempt ledger is invalid",
                status_code=409,
                details={"llm_call_id": llm_call_id, "error_code": exc.code},
            ) from exc
        if prefix == "soft_input":
            critique = refs.get("soft_auto_critique_decision")
            outcome = refs.get("soft_auto_critique_outcome")
            skip_reason = refs.get("soft_auto_critique_skip_reason")
            if (
                not isinstance(critique, dict)
                or self._json_hash(critique)
                != self._checkpoint_hash("soft_auto_critique_decision")
                or outcome
                not in {"unchanged", "patched", "patch_skipped", "patch_failed"}
                or (outcome in {"unchanged", "patched"} and skip_reason is not None)
                or (
                    outcome in {"patch_skipped", "patch_failed"}
                    and not isinstance(skip_reason, str)
                )
                or (
                    outcome == "patched"
                    and (draft.stage != "style_patch" or draft.row_id == source_row_id)
                )
                or (outcome != "patched" and draft.row_id != source_row_id)
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "soft input auto-critique decision is inconsistent",
                    status_code=409,
                )
            self._validate_auto_critique_product_semantics(
                critique,
                source_content=source.content,
                patch_outcome=outcome,
            )
            self._validate_auto_critique_checkpoint(
                scene_id,
                critique,
                source_content=source.content,
            )
            patch_failure_product = refs.get("soft_auto_critique_patch_failure")
            if outcome == "patch_failed":
                self._validate_auto_critique_patch_failure_checkpoint(
                    scene_id,
                    patch_failure_product,
                )
            elif patch_failure_product is not None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-failed auto-critique patch has a failure product",
                    status_code=409,
                )
        return StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=execution_step_key,
            artifact_execution_id=artifact_execution_id,
        )

    def _validate_settled_parent_ledger(self, parent: LlmCall) -> None:
        """Validate a provider-success parent without conflating later product parsing."""

        if parent.accounting_status != "settled":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "provider-success product parent is not settled",
                details={"llm_call_id": parent.llm_call_id},
            )
        if parent.provider == "offline_deterministic":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "retired non-provider execution cannot back a product",
                details={"llm_call_id": parent.llm_call_id},
            )
        validate_product_call_ledger(
            self.session,
            parent,
            expected_outcome="completed",
        )

    def _validate_generation_before_checkpoint(
        self,
        scene_id: str,
        generation: StyleGenerationResult,
        *,
        expected_provider_execution_mode: str | None = None,
    ) -> LlmCall:
        parent = self.session.get(LlmCall, generation.llm_call_id)
        owner = generation.artifact_execution_id or self._execution_id
        draft = self.session.get(SceneDraft, generation.row_id)
        if (
            parent is None
            or draft is None
            or draft.scene_id != scene_id
            or draft.generation_llm_call_id != generation.llm_call_id
            or parent.scene_id != scene_id
            or parent.execution_id != owner
            or parent.execution_step_key != generation.execution_step_key
        ):
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product is detached from its durable parent",
                details={"llm_call_id": generation.llm_call_id},
            )
        self._validate_generation_parent_identity(
            scene_id=scene_id,
            parent=parent,
            draft_stage=draft.stage,
            execution_step_key=generation.execution_step_key,
            execution_id=owner,
            expected_provider_execution_mode=expected_provider_execution_mode,
        )
        self._validate_settled_parent_ledger(parent)
        return parent

    def _validate_generation_parent_identity(
        self,
        *,
        scene_id: str,
        parent: LlmCall,
        draft_stage: str,
        execution_step_key: str | None,
        execution_id: str | None,
        expected_provider_execution_mode: str | None = None,
    ) -> None:
        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        stage_owner = {
            "style_draft": ("style_draft", "style_draft"),
            "style_patch": ("style_patch", "soft_patch"),
            "de_template": ("style_patch", "de_template"),
        }.get(draft_stage)
        if scene is None or stage_owner is None:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product stage/scene owner is invalid",
                details={"llm_call_id": parent.llm_call_id, "draft_stage": draft_stage},
            )
        node_id, step = stage_owner
        actual_execution_mode = (
            parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        # Without an explicit trusted snapshot this is a historical product:
        # its strictly validated durable mode/attempt shape is authoritative,
        # because the process configuration may legitimately have changed.
        expected_execution_mode = (
            expected_provider_execution_mode
            if expected_provider_execution_mode is not None
            else actual_execution_mode
        )
        if expected_execution_mode != "online":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product execution mode snapshot is invalid",
                details={"llm_call_id": parent.llm_call_id},
            )
        expected_project_id = scene.project_id or (
            chapter.project_id if chapter is not None else None
        )
        if (
            parent.scope_type != "scene"
            or parent.scope_id != scene_id
            or parent.project_id != expected_project_id
            or parent.chapter_id != scene.chapter_id
            or parent.scene_id != scene_id
            or not self._checkpoint_execution_owner_matches(
                execution_id, parent.run_job_id
            )
            or parent.execution_id != execution_id
            or parent.execution_step_key != execution_step_key
            or parent.node_id != node_id
            or parent.step != step
            or actual_execution_mode != expected_execution_mode
        ):
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "generation product is detached from its exact durable owner",
                details={"llm_call_id": parent.llm_call_id, "draft_stage": draft_stage},
            )

    @staticmethod
    def _parent_execution_mode(parent: LlmCall) -> str:
        mode = (
            parent.request_payload_summary.get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        if mode != "online":
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "durable parent has no valid provider execution mode marker",
                details={"llm_call_id": parent.llm_call_id},
            )
        return mode

    def _auto_critique_patch_context(
        self,
        scene_id: str,
        *,
        provider_execution_mode: str | None = None,
        execution_id: str | None = None,
        run_job_id: str | None = None,
    ) -> LLMCallContext:
        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        if scene is None:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch scene owner is missing",
            )
        return LLMCallContext(
            scope_type="scene",
            scope_id=scene_id,
            project_id=scene.project_id
            or (chapter.project_id if chapter is not None else None),
            chapter_id=scene.chapter_id,
            scene_id=scene_id,
            node_id="style_patch",
            step="soft_patch",
            execution_id=execution_id or self._execution_id,
            execution_step_key="soft_patch:auto_critique:0",
            run_job_id=run_job_id if execution_id is not None else self._run_job_id,
            provider_execution_mode=(
                provider_execution_mode
                or getattr(
                    self.scene_generation_service._llm_runner,
                    "provider_execution_mode",
                    "online",
                )
            ),
        )

    def _build_auto_critique_patch_failure_product(
        self,
        scene_id: str,
        error: BaseException,
    ) -> dict[str, Any]:
        context = self._auto_critique_patch_context(scene_id)
        rows = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == self._execution_id,
                    LlmCall.execution_step_key == context.execution_step_key,
                    LlmCall.accounting_status.in_(("settled", "failed", "rejected")),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            raise error
        if len(rows) != 1:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch failure does not resolve to one durable parent",
                details={"llm_call_ids": [row.llm_call_id for row in rows]},
            ) from error
        parent = rows[0]
        if parent.accounting_status == "rejected":
            outcome = "rejected_before_dispatch"
            reason = "pre_dispatch_rejection"
            error_code = parent.error_code
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=error_code,
            )
        elif parent.accounting_status == "failed":
            outcome = "provider_failed"
            reason = "provider_call_failed"
            error_code = parent.error_code
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=error_code,
            )
        else:
            if (
                not isinstance(error, SceneGenerationPostprocessError)
                or error.llm_call_id != parent.llm_call_id
                or error.error_code != "SCENE_GENERATION_RESPONSE_INVALID"
            ):
                raise error
            outcome = "parse_failed"
            reason = "invalid_scene_generation_response"
            error_code = "SCENE_GENERATION_RESPONSE_INVALID"
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome="parse_failed",
            )
        if not isinstance(error_code, str) or not error_code:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch failure parent has no stable error code",
                details={"llm_call_id": parent.llm_call_id},
            ) from error
        return {
            "schema_version": 1,
            "outcome": outcome,
            "llm_call_id": parent.llm_call_id,
            "execution_id": context.execution_id,
            "execution_step_key": context.execution_step_key,
            "run_job_id": context.run_job_id,
            "provider_execution_mode": context.provider_execution_mode,
            "reason": reason,
            "error_code": error_code,
        }

    def _validate_auto_critique_product_semantics(
        self,
        product: dict[str, Any],
        *,
        source_content: str,
        patch_outcome: str,
    ) -> None:
        from novel_system.services.auto_critique import auto_critique

        expected_rule = auto_critique(
            source_content,
            skip_critique=(
                product.get("outcome") == "not_invoked"
                and product.get("reason") == "skip_critique"
            ),
        )
        rule_fields = {
            "rule_should_rewrite": expected_rule.rule_should_rewrite,
            "rule_directives": expected_rule.rule_directives,
            "rule_dimension_scores": expected_rule.rule_dimension_scores,
            "rule_flagged_dimensions": expected_rule.rule_flagged_dimensions,
        }
        if any(product.get(key) != value for key, value in rule_fields.items()):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique rule product differs from deterministic source analysis",
                status_code=409,
            )
        if product.get("outcome") != "completed":
            merged_rule_fields = {
                "should_rewrite": expected_rule.should_rewrite,
                "directives": expected_rule.directives,
                "dimension_scores": expected_rule.dimension_scores,
                "flagged_dimensions": expected_rule.flagged_dimensions,
            }
            if any(
                product.get(key) != value for key, value in merged_rule_fields.items()
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "auto-critique degraded/no-call product is not the deterministic rule result",
                    status_code=409,
                )
            if product.get("llm_contribution") is not None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "auto-critique non-completed product contains an LLM contribution",
                    status_code=409,
                )
        else:
            contribution = product.get("llm_contribution")
            issues = (
                contribution.get("issues") if isinstance(contribution, dict) else None
            )
            allowed_dimensions = {
                "character_consistency",
                "earned_emotion",
                "conflict_credibility",
                "information_dumping",
                "show_vs_tell",
                "pacing",
                "llm_general",
            }
            if (
                not isinstance(contribution, dict)
                or set(contribution) != {"should_rewrite", "issues"}
                or type(contribution.get("should_rewrite")) is not bool
                or not isinstance(issues, list)
                or any(
                    not isinstance(issue, dict)
                    or set(issue) != {"dimension", "directive", "evidence"}
                    or any(not isinstance(issue.get(key), str) for key in issue)
                    or issue.get("dimension") not in allowed_dimensions
                    or not issue.get("directive", "").strip()
                    or len(issue.get("evidence", "")) > 120
                    for issue in issues
                )
                or contribution.get("should_rewrite") != bool(issues)
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "auto-critique completed LLM contribution schema is invalid",
                    status_code=409,
                )
            expected_directives = list(expected_rule.directives)
            expected_flagged = list(expected_rule.flagged_dimensions)
            seen_dimensions = set(expected_flagged)
            for issue in issues:
                dimension = issue["dimension"]
                directive = issue["directive"]
                evidence = issue["evidence"]
                if dimension not in seen_dimensions and directive:
                    entry = f"[LLM路{dimension}] {directive}"
                    if evidence:
                        entry += f" (evidence: {evidence[:120]})"
                    expected_directives.append(entry)
                    expected_flagged.append(dimension)
                    seen_dimensions.add(dimension)
            completed_invariants_hold = (
                product.get("directives") == expected_directives
                and product.get("flagged_dimensions") == expected_flagged
                and product.get("dimension_scores")
                == product.get("rule_dimension_scores")
                and product.get("should_rewrite")
                == (expected_rule.should_rewrite or contribution["should_rewrite"])
            )
            if not completed_invariants_hold:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "auto-critique completed product violates deterministic merge invariants",
                    status_code=409,
                )
        should_rewrite = product.get("should_rewrite") is True
        if (not should_rewrite and patch_outcome != "unchanged") or (
            should_rewrite
            and patch_outcome not in {"patched", "patch_skipped", "patch_failed"}
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique decision and patch outcome are semantically inconsistent",
                status_code=409,
            )

    def _auto_critique_llm_contribution_hash(self, product: dict[str, Any]) -> str:
        from novel_system.services.auto_critique import critique_llm_contribution_hash

        contribution = product.get("llm_contribution")
        if not isinstance(contribution, dict):
            return ""
        return critique_llm_contribution_hash(contribution)

    def _validate_auto_critique_patch_failure_checkpoint(
        self,
        scene_id: str,
        product: Any,
        *,
        validate_checkpoint_hash: bool = True,
    ) -> None:
        expected_fields = {
            "schema_version",
            "outcome",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "provider_execution_mode",
            "reason",
            "error_code",
        }
        if (
            not isinstance(product, dict)
            or set(product) != expected_fields
            or product.get("schema_version") != 1
            or product.get("outcome")
            not in {"provider_failed", "rejected_before_dispatch", "parse_failed"}
            or not isinstance(product.get("llm_call_id"), str)
            or not self._checkpoint_execution_owner_matches(
                product.get("execution_id"), product.get("run_job_id")
            )
            or product.get("execution_step_key") != "soft_patch:auto_critique:0"
            or product.get("provider_execution_mode") != "online"
            or not isinstance(product.get("reason"), str)
            or not isinstance(product.get("error_code"), str)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique patch failure product schema/owner is invalid",
                status_code=409,
            )
        if validate_checkpoint_hash and self._json_hash(
            product
        ) != self._checkpoint_hash("soft_auto_critique_patch_failure"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique patch failure product hash mismatch",
                status_code=409,
            )
        context = self._auto_critique_patch_context(
            scene_id,
            provider_execution_mode=product["provider_execution_mode"],
            execution_id=product["execution_id"],
            run_job_id=product["run_job_id"],
        )
        call_id = product["llm_call_id"]
        outcome = product["outcome"]
        expected_status = {
            "provider_failed": "failed",
            "rejected_before_dispatch": "rejected",
            "parse_failed": "settled",
        }[outcome]
        parent = self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=call_id,
            execution_step_key=context.execution_step_key,
            execution_id=context.execution_id,
            allowed_accounting_statuses=(expected_status,),
            allow_local_rejected_output=outcome == "rejected_before_dispatch",
        )
        if not isinstance(
            parent.response_payload_summary, dict
        ) or parent.response_payload_summary.get(
            "auto_critique_patch_failure_hash"
        ) != self._json_hash(
            product
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique patch failure product is detached from its parent",
                status_code=409,
            )
        try:
            if outcome == "parse_failed":
                if (
                    product.get("reason") != "invalid_scene_generation_response"
                    or product.get("error_code") != "SCENE_GENERATION_RESPONSE_INVALID"
                ):
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique patch parse failure code is invalid",
                    )
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome="parse_failed",
                )
            else:
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome=outcome,
                    expected_error_code=product["error_code"],
                )
        except LLMAccountingError as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique patch failure parent/attempt ledger is invalid",
                status_code=409,
                details={"llm_call_id": call_id, "error_code": exc.code},
            ) from exc

    def _recover_auto_critique_patch_rejected_product(
        self,
        scene_id: str,
        *,
        allow_retry: bool,
    ) -> dict[str, Any] | None:
        """Recover a rejected patch tombstone before gate changes can hide it."""

        query_context = self._auto_critique_patch_context(scene_id)
        rows = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == self._execution_id,
                    LlmCall.execution_step_key == query_context.execution_step_key,
                )
            )
            .scalars()
            .all()
        )
        rejected = [row for row in rows if row.accounting_status == "rejected"]
        if not rejected:
            released = [row for row in rows if row.accounting_status == "released"]
            if released and len(released) == len(rows):
                if allow_retry:
                    return None
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "auto-critique patch no-call gate cannot replace a released tombstone",
                    status_code=409,
                    details={"llm_call_ids": [row.llm_call_id for row in rows]},
                )
            return None
        if len(rows) != 1:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch rejection does not resolve to one durable parent",
                details={"llm_call_ids": [row.llm_call_id for row in rows]},
            )
        parent = rejected[0]
        historical_execution_mode = self._parent_execution_mode(parent)
        context = self._auto_critique_patch_context(
            scene_id,
            provider_execution_mode=historical_execution_mode,
        )
        if not isinstance(parent.error_code, str) or not parent.error_code:
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique patch rejected tombstone has no terminal error code",
                details={"llm_call_id": parent.llm_call_id},
            )
        validate_product_call(
            self.session,
            parent.llm_call_id,
            context,
            expected_outcome="rejected_before_dispatch",
            expected_error_code=parent.error_code,
        )
        return {
            "schema_version": 1,
            "outcome": "rejected_before_dispatch",
            "llm_call_id": parent.llm_call_id,
            "execution_id": context.execution_id,
            "execution_step_key": context.execution_step_key,
            "run_job_id": context.run_job_id,
            "provider_execution_mode": historical_execution_mode,
            "reason": "pre_dispatch_rejection",
            "error_code": parent.error_code,
        }

    def _recover_auto_critique_rejected_product(
        self,
        context: LLMCallContext,
        rule_result: Any,
        *,
        allow_retry: bool,
    ) -> Any | None:
        """Rebuild a lost no-dispatch product before a flipped gate emits no-call."""

        from dataclasses import replace

        rows = (
            self.session.execute(
                select(LlmCall)
                .where(
                    LlmCall.scene_id == context.scene_id,
                    LlmCall.execution_id == context.execution_id,
                    LlmCall.execution_step_key == context.execution_step_key,
                )
                .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            return None

        rejected: list[LlmCall] = []
        for row in rows:
            if row.accounting_status == "rejected":
                if not isinstance(row.error_code, str) or not row.error_code:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique rejected tombstone has no terminal error code",
                        details={"llm_call_id": row.llm_call_id},
                    )
                validate_product_call(
                    self.session,
                    row.llm_call_id,
                    context,
                    expected_outcome="rejected_before_dispatch",
                    expected_error_code=row.error_code,
                )
                rejected.append(row)
                continue
            if row.accounting_status == "released":
                attempts = (
                    self.session.execute(
                        select(LlmCallAttempt).where(
                            LlmCallAttempt.llm_call_id == row.llm_call_id
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    row.request_dispatched_at is not None
                    or row.budget_charged_tokens != 0
                    or row.total_tokens != 0
                    or row.scope_type != context.scope_type
                    or row.scope_id != context.scope_id
                    or row.node_id != context.node_id
                    or row.step != context.step
                    or row.project_id != context.project_id
                    or row.chapter_id != context.chapter_id
                    or row.run_job_id != context.run_job_id
                    or any(
                        attempt.request_dispatched_at is not None
                        or attempt.budget_charged_tokens != 0
                        or attempt.total_tokens != 0
                        or attempt.accounting_status != "released"
                        for attempt in attempts
                    )
                ):
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "auto-critique released tombstone is not a zero-dispatch ledger",
                        details={"llm_call_id": row.llm_call_id},
                    )
                continue
            raise LLMAccountingError(
                "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                "auto-critique no-call gate conflicts with a non-retryable ledger row",
                details={
                    "llm_call_id": row.llm_call_id,
                    "accounting_status": row.accounting_status,
                },
            )

        if not rejected:
            if allow_retry:
                return None
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique no-call gate cannot replace a released accounting tombstone",
                status_code=409,
                details={"execution_step_key": context.execution_step_key},
            )
        parent = rejected[-1]
        return replace(
            rule_result,
            outcome="rejected_before_dispatch",
            llm_call_id=parent.llm_call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
            reason="pre_dispatch_rejection",
            error_code=parent.error_code,
        )

    def _validate_auto_critique_checkpoint(
        self,
        scene_id: str,
        product: dict[str, Any],
        *,
        source_content: str,
    ) -> None:
        expected_fields = {
            "schema_version",
            "outcome",
            "should_rewrite",
            "directives",
            "dimension_scores",
            "flagged_dimensions",
            "rule_should_rewrite",
            "rule_directives",
            "rule_dimension_scores",
            "rule_flagged_dimensions",
            "llm_contribution",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "reason",
            "error_code",
        }

        def corrupt(message: str) -> None:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                message,
                status_code=409,
            )

        if set(product) != expected_fields or product.get("schema_version") != 1:
            corrupt("auto-critique checkpoint product schema is invalid")
        outcome = product.get("outcome")
        if outcome not in {
            "not_invoked",
            "completed",
            "rejected_before_dispatch",
            "provider_failed",
            "parse_failed",
        }:
            corrupt("auto-critique checkpoint outcome is invalid")
        if (
            type(product.get("should_rewrite")) is not bool
            or type(product.get("rule_should_rewrite")) is not bool
            or any(
                not isinstance(product.get(field_name), list)
                or any(not isinstance(item, str) for item in product[field_name])
                for field_name in (
                    "directives",
                    "flagged_dimensions",
                    "rule_directives",
                    "rule_flagged_dimensions",
                )
            )
            or any(
                not isinstance(product.get(field_name), dict)
                or any(
                    not isinstance(key, str)
                    or type(value) not in {int, float}
                    or not 0 <= value <= 1
                    for key, value in product[field_name].items()
                )
                for field_name in ("dimension_scores", "rule_dimension_scores")
            )
        ):
            corrupt("auto-critique checkpoint product fields are invalid")
        if outcome != "completed" and product.get("llm_contribution") is not None:
            corrupt("auto-critique non-completed contribution field is invalid")
        expected_step = "soft_qc:auto_critique:0"
        if (
            not self._checkpoint_execution_owner_matches(
                product.get("execution_id"), product.get("run_job_id")
            )
            or product.get("execution_step_key") != expected_step
        ):
            corrupt("auto-critique checkpoint execution ownership is invalid")

        call_id = product.get("llm_call_id")
        reason = product.get("reason")
        error_code = product.get("error_code")
        if outcome == "not_invoked":
            if (
                call_id is not None
                or reason
                not in {
                    "skip_critique",
                    "budget_or_candidate_cap",
                    "feature_disabled",
                    "runner_unavailable",
                    "offline_unsupported",
                }
                or error_code is not None
            ):
                corrupt("auto-critique no-call outcome field matrix is invalid")
            ledger_rows = (
                self.session.execute(
                    select(LlmCall).where(
                        LlmCall.execution_id == product.get("execution_id"),
                        LlmCall.execution_step_key == expected_step,
                    )
                )
                .scalars()
                .all()
            )
            if ledger_rows:
                corrupt(
                    "auto-critique no-call product unexpectedly has an execution ledger"
                )
            from novel_system.services.auto_critique import auto_critique

            expected_rule = auto_critique(
                source_content,
                skip_critique=reason == "skip_critique",
            )
            if any(
                product.get(product_key) != expected_value
                for product_key, expected_value in {
                    "should_rewrite": expected_rule.should_rewrite,
                    "directives": expected_rule.directives,
                    "dimension_scores": expected_rule.dimension_scores,
                    "flagged_dimensions": expected_rule.flagged_dimensions,
                    "rule_should_rewrite": expected_rule.rule_should_rewrite,
                    "rule_directives": expected_rule.rule_directives,
                    "rule_dimension_scores": expected_rule.rule_dimension_scores,
                    "rule_flagged_dimensions": expected_rule.rule_flagged_dimensions,
                }.items()
            ):
                corrupt(
                    "auto-critique no-call product differs from its deterministic rule result"
                )
            return
        if not isinstance(call_id, str) or not call_id:
            corrupt("auto-critique called outcome is missing its parent id")
        if outcome == "completed":
            if reason is not None or error_code is not None:
                corrupt("auto-critique completed outcome field matrix is invalid")
        elif (
            not isinstance(reason, str)
            or not reason
            or not isinstance(error_code, str)
            or not error_code
        ):
            corrupt("auto-critique degraded outcome field matrix is invalid")
        if outcome == "parse_failed" and (
            reason != "invalid_llm_response"
            or error_code != "LLM_CRITIQUE_RESPONSE_INVALID"
        ):
            corrupt("auto-critique parse-failed outcome code is invalid")

        scene = self.session.get(SceneCard, scene_id)
        chapter = (
            self.session.get(ChapterGoal, scene.chapter_id)
            if scene is not None
            else None
        )
        if scene is None:
            corrupt("auto-critique checkpoint scene owner is missing")
        product_parent = self.session.get(LlmCall, call_id)
        context = LLMCallContext(
            scope_type="scene",
            scope_id=scene_id,
            project_id=scene.project_id
            or (chapter.project_id if chapter is not None else None),
            chapter_id=scene.chapter_id,
            scene_id=scene_id,
            node_id="soft_qc",
            step=expected_step,
            execution_id=product.get("execution_id"),
            execution_step_key=expected_step,
            run_job_id=product.get("run_job_id"),
            provider_execution_mode="online",
        )
        expected_status = {
            "completed": "settled",
            "parse_failed": "settled",
            "rejected_before_dispatch": "rejected",
            "provider_failed": "failed",
        }[outcome]
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=call_id,
            execution_step_key=expected_step,
            execution_id=product.get("execution_id"),
            allowed_accounting_statuses=(expected_status,),
            allow_local_rejected_output=outcome == "rejected_before_dispatch",
        )
        parent = product_parent
        if (
            parent is None
            or not isinstance(parent.response_payload_summary, dict)
            or parent.response_payload_summary.get("auto_critique_product_hash")
            != self._json_hash(product)
        ):
            corrupt("auto-critique product hash is detached from its accounting parent")
        if outcome == "completed" and (
            parent.response_payload_summary.get("auto_critique_parsed_llm_hash")
            != self._auto_critique_llm_contribution_hash(product)
        ):
            corrupt(
                "auto-critique LLM merge payload is detached from its parsed-result hash"
            )
        try:
            validate_product_call(
                self.session,
                call_id,
                context,
                expected_outcome=outcome,
                expected_error_code=(
                    error_code
                    if outcome in {"rejected_before_dispatch", "provider_failed"}
                    else None
                ),
            )
        except LLMAccountingError as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "auto-critique checkpoint parent/physical-attempt ledger is invalid",
                status_code=409,
                details={"llm_call_id": call_id, "error_code": exc.code},
            ) from exc

    @staticmethod
    def _soft_decision_snapshot(
        decision: SoftQcDecision,
        *,
        include_should_continue: bool,
    ) -> dict[str, Any]:
        snapshot = {
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "human_review_event_id": decision.human_review_event_id,
            "resolution_code": decision.resolution_code,
            "next_action": decision.next_action,
            "stop_reason": decision.stop_reason,
            "llm_call_id": decision.llm_call_id,
            "execution_step_key": decision.execution_step_key,
        }
        if include_should_continue:
            snapshot["should_continue"] = decision.should_continue
        return snapshot

    def _save_soft_qc_round_checkpoint(
        self,
        *,
        sub_index: int,
        round_index: int,
        decision: SoftQcDecision,
        source_generation: StyleGenerationResult,
        bundle: dict[str, Any],
        patch_allowed: bool | None = None,
        skip_reason: str | None = None,
        final_generation: StyleGenerationResult | None = None,
        final_skip_reason: str | None = None,
    ) -> None:
        self.session.flush()
        report = self.session.get(QcReport, decision.qc_report_id)
        if report is None:
            self._raise_checkpoint_output_missing(row_id=decision.qc_report_id)
        prefix = f"soft_qc{round_index}"
        decision_snapshot = self._soft_decision_snapshot(
            decision, include_should_continue=True
        )
        refs: dict[str, Any] = {
            f"{prefix}_report_id": decision.qc_report_id,
            f"{prefix}_decision": decision_snapshot,
            f"{prefix}_source_draft_row_id": source_generation.row_id,
            f"{prefix}_bundle_id": bundle["bundle_id"],
            f"{prefix}_bundle_hash": bundle["bundle_snapshot_hash"],
            f"{prefix}_llm_call_id": decision.llm_call_id,
            f"{prefix}_execution_step_key": decision.execution_step_key,
            f"{prefix}_artifact_execution_id": self._execution_id,
        }
        hashes = {
            f"{prefix}_decision": self._json_hash(decision_snapshot),
            f"{prefix}_report": self._json_hash(self._qc_report_snapshot(report)),
        }
        if round_index == 0 and patch_allowed is not None:
            control = {"patch_allowed": patch_allowed, "skip_reason": skip_reason}
            refs["soft_qc0_control"] = control
            hashes["soft_qc0_control"] = self._json_hash(control)
        if final_generation is not None:
            legacy_decision = self._soft_decision_snapshot(
                decision, include_should_continue=False
            )
            completion = {
                "final_qc_round": round_index,
                "skip_reason": final_skip_reason,
                "branch": decision.branch,
                "qc_report_id": decision.qc_report_id,
                "draft_row_id": final_generation.row_id,
            }
            refs.update(
                {
                    "soft_qc_report_id": decision.qc_report_id,
                    "soft_qc_human_review_event_id": decision.human_review_event_id,
                    "soft_qc_branch": decision.branch,
                    "soft_qc_resolution_code": decision.resolution_code,
                    "soft_qc_next_action": decision.next_action,
                    "soft_qc_stop_reason": decision.stop_reason,
                    "soft_final_draft_row_id": final_generation.row_id,
                    "soft_final_llm_call_id": final_generation.llm_call_id,
                    "soft_final_execution_step_key": final_generation.execution_step_key,
                    "soft_final_artifact_execution_id": (
                        final_generation.artifact_execution_id or self._execution_id
                    ),
                    "soft_qc_source_draft_row_id": source_generation.row_id,
                    "soft_qc_bundle_id": bundle["bundle_id"],
                    "soft_qc_llm_call_id": decision.llm_call_id,
                    "soft_qc_execution_step_key": decision.execution_step_key,
                    "soft_qc_artifact_execution_id": self._execution_id,
                    "soft_final_qc_round": round_index,
                    "soft_completion_skip_reason": final_skip_reason,
                    "soft_completion": completion,
                }
            )
            hashes.update(
                {
                    "soft_final_draft": self._text_hash(final_generation.content),
                    "soft_qc_decision": self._json_hash(legacy_decision),
                    "soft_qc_report": self._json_hash(self._qc_report_snapshot(report)),
                    "soft_completion": self._json_hash(completion),
                }
            )
        self._save_run_checkpoint(
            "soft_qc_ready",
            sub_index=sub_index,
            artifact_refs=refs,
            artifact_hashes=hashes,
            branch=decision.branch,
        )

    def _load_soft_qc_round_checkpoint(
        self,
        scene_id: str,
        *,
        round_index: int,
        source_generation: StyleGenerationResult,
    ) -> SoftQcDecision:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        prefix = f"soft_qc{round_index}"
        report_id = refs.get(f"{prefix}_report_id")
        report = (
            self.session.get(QcReport, report_id)
            if isinstance(report_id, str)
            else None
        )
        if report is None:
            self._raise_checkpoint_output_missing(row_id=report_id)
        decision_payload = refs.get(f"{prefix}_decision")
        if not isinstance(decision_payload, dict) or self._json_hash(
            decision_payload
        ) != self._checkpoint_hash(f"{prefix}_decision"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"soft QC{round_index} decision hash mismatch",
                status_code=409,
            )
        decision = SoftQcDecision(
            branch=str(decision_payload.get("branch") or "continue"),
            qc_report_id=str(decision_payload.get("qc_report_id") or ""),
            human_review_event_id=decision_payload.get("human_review_event_id"),
            resolution_code=str(decision_payload.get("resolution_code") or ""),
            next_action=str(decision_payload.get("next_action") or ""),
            should_continue=bool(decision_payload.get("should_continue")),
            stop_reason=decision_payload.get("stop_reason"),
            llm_call_id=decision_payload.get("llm_call_id"),
            execution_step_key=decision_payload.get("execution_step_key"),
        )
        owner = self._validate_artifact_execution_owner(
            refs.get(f"{prefix}_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
            execution_id=owner,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        bundle = self._load_checkpoint_bundle(scene_id)
        if (
            report.scene_id != scene_id
            or report.qc_type != "soft_qc"
            or report.qc_report_id != decision.qc_report_id
            or report.source_draft_row_id != source_generation.row_id
            or refs.get(f"{prefix}_source_draft_row_id") != source_generation.row_id
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_id") != bundle["bundle_id"]
            or refs.get(f"{prefix}_bundle_hash") != bundle["bundle_snapshot_hash"]
            or refs.get(f"{prefix}_llm_call_id") != decision.llm_call_id
            or refs.get(f"{prefix}_execution_step_key") != decision.execution_step_key
            or decision.execution_step_key != f"soft_qc:{round_index}"
            or self._json_hash(self._qc_report_snapshot(report))
            != self._checkpoint_hash(f"{prefix}_report")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"soft QC{round_index} checkpoint identity/source mismatch",
                status_code=409,
            )
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="soft_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_generation.row_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        if decision.branch == "human_review_required":
            event = self.session.get(HumanReviewEvent, decision.human_review_event_id)
            if event is None:
                self._raise_checkpoint_output_missing(
                    row_id=decision.human_review_event_id
                )
            replay_context = (event.details_json or {}).get("replay_context")
            if (
                event.scene_id != scene_id
                or event.object_ref != source_generation.row_id
                or not isinstance(replay_context, dict)
                or replay_context.get("current_qc_report_id") != report.qc_report_id
                or replay_context.get("source_draft_row_id") != source_generation.row_id
                or replay_context.get("source_bundle_id") != bundle["bundle_id"]
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    f"soft QC{round_index} human-review event is misbound",
                    status_code=409,
                )
        return decision

    def _load_soft_qc0_branch_control(
        self, decision: SoftQcDecision
    ) -> tuple[bool, str | None]:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        control = refs.get("soft_qc0_control")
        if (
            not isinstance(control, dict)
            or not isinstance(control.get("patch_allowed"), bool)
            or self._json_hash(control) != self._checkpoint_hash("soft_qc0_control")
            or (decision.branch != "patch" and control["patch_allowed"])
            or (control["patch_allowed"] and control.get("skip_reason") is not None)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC0 branch control is invalid",
                status_code=409,
            )
        skip_reason = control.get("skip_reason")
        if skip_reason is not None and not isinstance(skip_reason, str):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC0 skip reason is invalid",
                status_code=409,
            )
        return control["patch_allowed"], skip_reason

    def _load_soft_qc_checkpoint(
        self,
        scene_id: str,
        *,
        selected_style_generation: StyleGenerationResult | None = None,
    ) -> tuple[SoftQcDecision, StyleGenerationResult]:
        report_id = self._checkpoint_artifact(
            "soft_qc_report_id",
            expected_node_at_least="soft_qc_ready",
        )
        report = (
            self.session.get(QcReport, report_id)
            if isinstance(report_id, str)
            else None
        )
        if report is None:
            self._raise_checkpoint_output_missing(row_id=report_id)
        assert report is not None
        if report.scene_id != scene_id or report.qc_type != "soft_qc":
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC checkpoint identity mismatch",
                status_code=409,
            )

        row_id = self._checkpoint_artifact(
            "soft_final_draft_row_id",
            expected_node_at_least="soft_qc_ready",
        )
        draft = (
            self.session.get(SceneDraft, row_id) if isinstance(row_id, str) else None
        )
        if draft is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        assert draft is not None
        bundle = self._load_checkpoint_bundle(scene_id)
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        soft_qc_execution_id = self._validate_artifact_execution_owner(
            refs.get("soft_qc_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=refs.get("soft_qc_llm_call_id"),
            execution_step_key=refs.get("soft_qc_execution_step_key"),
            execution_id=soft_qc_execution_id,
            allowed_accounting_statuses=("settled", "failed", "rejected"),
            allow_local_rejected_output=True,
        )
        final_llm_call_id = refs.get("soft_final_llm_call_id")
        final_execution_step_key = refs.get("soft_final_execution_step_key")
        final_artifact_execution_id = self._validate_artifact_execution_owner(
            refs.get("soft_final_artifact_execution_id")
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=final_llm_call_id,
            execution_step_key=final_execution_step_key,
            execution_id=final_artifact_execution_id,
        )
        if (
            draft.scene_id != scene_id
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or draft.generation_llm_call_id != final_llm_call_id
            or self._text_hash(draft.content)
            != self._checkpoint_hash("soft_final_draft")
            or report.source_draft_row_id != refs.get("soft_qc_source_draft_row_id")
            or report.source_draft_row_id != draft.row_id
            or report.source_bundle_id != bundle["bundle_id"]
            or refs.get("soft_qc_bundle_id") != bundle["bundle_id"]
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC draft identity/hash mismatch",
                status_code=409,
            )

        decision = SoftQcDecision(
            branch=str(
                refs.get("soft_qc_branch") or payload.get("branch") or "continue"
            ),
            qc_report_id=report.qc_report_id,
            human_review_event_id=refs.get("soft_qc_human_review_event_id"),
            resolution_code=str(
                refs.get("soft_qc_resolution_code") or report.resolution_code or ""
            ),
            next_action=str(
                refs.get("soft_qc_next_action") or report.next_action or ""
            ),
            should_continue=str(refs.get("soft_qc_branch") or payload.get("branch"))
            in {"continue", "waive"},
            stop_reason=refs.get("soft_qc_stop_reason"),
            llm_call_id=refs.get("soft_qc_llm_call_id"),
            execution_step_key=refs.get("soft_qc_execution_step_key"),
        )
        decision_summary = {
            "branch": decision.branch,
            "qc_report_id": decision.qc_report_id,
            "human_review_event_id": decision.human_review_event_id,
            "resolution_code": decision.resolution_code,
            "next_action": decision.next_action,
            "stop_reason": decision.stop_reason,
            "llm_call_id": decision.llm_call_id,
            "execution_step_key": decision.execution_step_key,
        }
        if self._json_hash(decision_summary) != self._checkpoint_hash(
            "soft_qc_decision"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC decision hash mismatch",
                status_code=409,
            )
        if self._json_hash(self._qc_report_snapshot(report)) != self._checkpoint_hash(
            "soft_qc_report"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "soft QC report hash mismatch",
                status_code=409,
            )
        self._validate_qc_attempt(
            scene_id=scene_id,
            step="soft_qc",
            qc_report_id=report.qc_report_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=draft.row_id,
            llm_call_id=decision.llm_call_id,
            execution_step_key=decision.execution_step_key,
        )
        generation = StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=final_llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=final_execution_step_key,
            artifact_execution_id=final_artifact_execution_id,
        )
        if refs.get("soft_completion") is not None:
            if selected_style_generation is None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "soft checkpoint prefix validation requires the selected style source",
                    status_code=409,
                )
            soft_input = self._load_soft_draft_checkpoint(
                scene_id,
                prefix="soft_input",
                expected_source_draft_row_id=selected_style_generation.row_id,
                expected_stages={"style_draft", "de_template", "style_patch"},
            )
            qc0 = self._load_soft_qc_round_checkpoint(
                scene_id,
                round_index=0,
                source_generation=soft_input,
            )
            patch_allowed, skip_reason = self._load_soft_qc0_branch_control(qc0)
            final_qc_round = refs.get("soft_final_qc_round")
            if final_qc_round == 1:
                if (
                    qc0.branch != "patch"
                    or not patch_allowed
                    or skip_reason is not None
                ):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "soft QC1 completion is not reachable from its QC0 branch",
                        status_code=409,
                    )
                patch_generation = self._load_soft_draft_checkpoint(
                    scene_id,
                    prefix="soft_patch",
                    expected_source_draft_row_id=soft_input.row_id,
                    expected_stages={"style_patch"},
                    expected_source_qc_report_id=qc0.qc_report_id,
                )
                checkpoint_decision = self._load_soft_qc_round_checkpoint(
                    scene_id,
                    round_index=1,
                    source_generation=patch_generation,
                )
                expected_generation = patch_generation
            elif final_qc_round == 0:
                if qc0.branch == "patch" and patch_allowed:
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "soft QC0 completion skipped an allowed patch",
                        status_code=409,
                    )
                checkpoint_decision = qc0
                expected_generation = soft_input
            else:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "soft final QC round is invalid",
                    status_code=409,
                )
            completion = refs.get("soft_completion")
            expected_completion = {
                "final_qc_round": final_qc_round,
                "skip_reason": refs.get("soft_completion_skip_reason"),
                "branch": decision.branch,
                "qc_report_id": decision.qc_report_id,
                "draft_row_id": generation.row_id,
            }
            if (
                completion != expected_completion
                or self._json_hash(completion)
                != self._checkpoint_hash("soft_completion")
                or refs.get("soft_completion_skip_reason")
                != (skip_reason if final_qc_round == 0 else None)
                or self._soft_decision_snapshot(
                    checkpoint_decision, include_should_continue=False
                )
                != self._soft_decision_snapshot(decision, include_should_continue=False)
                or expected_generation.row_id != generation.row_id
                or expected_generation.llm_call_id != generation.llm_call_id
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "soft completion prefix/branch/hash mismatch",
                    status_code=409,
                )
        return decision, generation

    @staticmethod
    def _qc_report_snapshot(report: QcReport) -> dict[str, Any]:
        return {
            "qc_type": report.qc_type,
            "status": report.status,
            "source_draft_row_id": report.source_draft_row_id,
            "source_bundle_id": report.source_bundle_id,
            "resolution_code": report.resolution_code,
            "pass_flag": report.pass_flag,
            "next_action": report.next_action,
            "issues": deepcopy(report.issues_json or []),
            "rewrite_brief": deepcopy(report.rewrite_brief_json or []),
        }

    def _checkpoint_hash(self, key: str) -> str | None:
        return self._ckpt._checkpoint_hash(key)

    def _raise_checkpoint_output_missing(self, *, row_id: Any) -> None:
        self._ckpt._raise_checkpoint_output_missing(row_id=row_id)

    def _active_checkpoint_state(self) -> SceneRunState:
        return self._ckpt._active_checkpoint_state()

    @staticmethod
    def _text_hash(content: str) -> str:
        return RunCheckpointContext._text_hash(content)

    @staticmethod
    def _json_hash(payload: Any) -> str:
        return RunCheckpointContext._json_hash(payload)

    @staticmethod
    def _prepare_state_for_run(state: SceneRunState, *, new_execution: bool) -> None:
        if not new_execution:
            return
        state.current_bundle_id = None
        state.current_bundle_hash = None
        state.current_neutral_draft_row_id = None
        state.current_style_draft_row_id = None
        state.current_final_scene_row_id = None
        state.current_human_review_event_id = None
        state.current_qc_report_id = None
        # This counter describes the current run's soft-patch loop; lifecycle
        # provider/total attempt counters and latest_valid remain cumulative.
        state.soft_patch_count = 0

    def _pov_desensitize_brief(
        self, scene: SceneCard, contract, brief: list[str]
    ) -> list[str]:
        """Wave 4（§5.6/§7.11/不变量 11）：回灌自动补丁提示词前做 POV 证据脱敏。

        引用了非 POV 已知秘密的 brief 条目不得进入自动补丁——剔除后只能走作者确认修订。
        硬 QC 自身始终读全量权威状态，不经此路径。pov 缺失或项目无秘密时无副作用；
        脱敏失败降级为原 brief（不阻断主流程），失败以 WARNING 可见。
        """
        if not brief:
            return brief
        try:
            from novel_system.services.pov_knowledge_projection import (
                PovKnowledgeProjection,
            )
            from novel_system.services.narrative_event_log import NarrativeEventLog

            payload = getattr(contract, "payload_json", None) or {}
            pov = scene.pov_character_id or payload.get("pov_character_id")
            if not pov:
                return brief
            project_id = self._resolve_scene_project_id(scene, contract)
            return PovKnowledgeProjection(
                self.session,
                event_log=NarrativeEventLog(self.session),
            ).redact_brief(
                brief,
                project_id,
                None,
                scene_id=scene.scene_id,
                pov_character_id=pov,
                onstage_character_ids=scene.onstage_chars_json or [],
            )
        except Exception:
            _LOGGER.warning(
                "pov brief desensitization degraded for scene %s; keeping raw brief",
                scene.scene_id,
                exc_info=True,
            )
            return brief

    def _rewrite_brief_from_report(self, qc_report_id: str) -> list[str]:
        report = self.session.get(QcReport, qc_report_id)
        if report is None:
            return []
        entries = report.rewrite_brief_json or []
        rewrite_brief: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                instruction = entry.get("instruction")
                if isinstance(instruction, str) and instruction.strip():
                    rewrite_brief.append(instruction.strip())
        return rewrite_brief

    def _carry_notes_from_report(self, qc_report_id: str) -> list[dict[str, Any]]:
        report = self.session.get(QcReport, qc_report_id)
        if report is None:
            return []
        carry_notes: list[dict[str, Any]] = []
        for entry in report.rewrite_brief_json or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "carry_forward_note":
                continue
            note_scope = entry.get("note_scope")
            carry_note_text = entry.get("carry_note_text")
            if (
                isinstance(note_scope, str)
                and note_scope.strip()
                and isinstance(carry_note_text, str)
                and carry_note_text.strip()
            ):
                carry_notes.append(
                    {
                        "kind": "carry_forward_note",
                        "note_scope": note_scope.strip(),
                        "carry_note_text": carry_note_text.strip(),
                    }
                )
        return carry_notes

    @staticmethod
    def _soft_risk_acceptance_event_id(soft_qc) -> str | None:
        stop_reason = str(getattr(soft_qc, "stop_reason", "") or "")
        prefix = "accepted_soft_risk:"
        if not stop_reason.startswith(prefix):
            return None
        event_id = stop_reason[len(prefix) :].strip()
        return event_id or None

    @staticmethod
    def _near_final_rewrite_brief(near_final: dict[str, Any]) -> list[str]:
        rewrite_brief: list[str] = []
        for entry in near_final.get("revision_brief") or []:
            if isinstance(entry, dict):
                action = (
                    entry.get("action")
                    or entry.get("instruction")
                    or entry.get("recommendation")
                )
                if isinstance(action, str) and action.strip():
                    rewrite_brief.append(action.strip())
            elif isinstance(entry, str) and entry.strip():
                rewrite_brief.append(entry.strip())
        if not rewrite_brief:
            rewrite_brief.append(
                "Rewrite the full scene so forced choice, paid cost, relationship turn, and ending action are visible."
            )
        return rewrite_brief

    def _with_author_projection(
        self, scene_id: str, state: SceneRunState, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2 项 5：run 结果（含全部早退路径）统一附 §5.3 作者状态契约。"""
        from novel_system.services.author_state import compute_author_state

        projection = compute_author_state(self.session, scene_id, state)
        return {**payload, **projection}

    @staticmethod
    def _near_final_warning_findings(
        near_final: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """near-final 未过 → Q2/Q3 警告条目（LLM 提案层，不阻断）。"""
        if near_final.get("pass_flag"):
            return []
        failure_class = str(near_final.get("failure_class") or "near_final_unresolved")
        level = "Q3" if failure_class == "prose_model_voice" else "Q2"
        first_finding = next(
            (
                item
                for item in near_final.get("findings") or []
                if isinstance(item, dict)
            ),
            {},
        )
        return [
            {
                "issue_key": f"near_final_{failure_class}",
                "quality_level": level,
                "message": str(first_finding.get("issue") or failure_class),
                "recommended_action": "author_review_optional_fix",
                "verified_by": None,
            }
        ]

    def _collect_q2_warnings(
        self, state: SceneRunState, near_final_warnings: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """严格模式停点判据：当前 QC 报告的 Q2 条目 + near-final Q2 警告（Q3 只诊断不停）。"""
        warnings = [
            item for item in near_final_warnings if item.get("quality_level") == "Q2"
        ]
        report = (
            self.session.get(QcReport, state.current_qc_report_id)
            if state.current_qc_report_id
            else None
        )
        for issue in (report.issues_json or []) if report else []:
            if isinstance(issue, dict) and issue.get("quality_level") == "Q2":
                warnings.append(issue)
        return warnings

    @staticmethod
    def _merged_warnings(
        existing: Any, additions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        merged = [item for item in (existing or []) if isinstance(item, dict)]
        merged.extend(additions)
        return merged

    @staticmethod
    def _near_final_result_payload(
        near_final: dict[str, Any], *, rewrite_count: int
    ) -> dict[str, Any]:
        return {
            "near_final_status": near_final.get("near_final_status"),
            "pass_flag": bool(near_final.get("pass_flag")),
            "overall_score": near_final.get("overall_score"),
            "failure_class": near_final.get("failure_class"),
            "requires_human_review": bool(near_final.get("requires_human_review")),
            "evaluation_id": near_final.get("evaluation_id"),
            "revision_candidate_id": near_final.get("revision_candidate_id"),
            "should_rewrite": bool(near_final.get("should_rewrite")),
            "rewrite_count": rewrite_count,
            "findings": near_final.get("findings") or [],
            "revision_brief": near_final.get("revision_brief") or [],
        }

    def _archive_effects(self) -> SceneArchiveEffects:
        """Build the archive-effects worker for the CURRENT run.

        ``_execution_id`` / ``_run_job_id`` are set per run_scene/resume call, so
        the worker is constructed at call time — never cached — and it dispatches
        cluster-internal cross-calls back through ``self`` so instance-level
        overrides (a test seam) keep intercepting sibling recorder calls.
        """
        return SceneArchiveEffects(
            self.session,
            self.llm_runner,
            execution_id=self._execution_id,
            run_job_id=self._run_job_id,
            dispatch=self,
        )

    def _record_narrative_events(
        self,
        scene: SceneCard,
        contract,
        content: str,
        *,
        include_prose: bool = True,
        degrade_errors: bool = True,
        final_scene_row_id: str | None = None,
    ) -> list[str]:
        return self._archive_effects()._record_narrative_events(
            scene,
            contract,
            content,
            include_prose=include_prose,
            degrade_errors=degrade_errors,
            final_scene_row_id=final_scene_row_id,
        )

    def _resolve_scene_project_id(self, scene: SceneCard, contract=None) -> str:
        return self._archive_effects()._resolve_scene_project_id(scene, contract)

    def _archive_event_base(self, scene: SceneCard, contract) -> dict[str, str]:
        return self._archive_effects()._archive_event_base(scene, contract)

    def _resolve_auto_critique_runner(self):
        """§8 gate: the independent LLM editor critic is layered on ONLY when both
        ``llm_enabled`` and ``llm_auto_critique_enabled`` are set (opt-in); otherwise the
        critic runner is ``None`` and ``llm_auto_critique`` degrades to the rule-based pass.
        Extracted from ``run_scene`` so the opt-in gate is unit-testable in isolation
        (blueprint §8 + §15 honest-bounds)."""
        from novel_system.settings import get_settings

        settings = get_settings()
        return (
            self.llm_runner
            if (settings.llm_enabled and settings.llm_auto_critique_enabled)
            else None
        )

    def _record_prose_events(
        self,
        log,
        scene: SceneCard,
        base: dict,
        content: str,
        *,
        final_scene_row_id: str | None = None,
        return_event_ids: bool = False,
    ) -> ProseExtractionResult | tuple[ProseExtractionResult, list[str]]:
        return self._archive_effects()._record_prose_events(
            log,
            scene,
            base,
            content,
            final_scene_row_id=final_scene_row_id,
            return_event_ids=return_event_ids,
        )

    def _record_relation_events(
        self,
        log,
        scene: SceneCard,
        base: dict,
        pov: str | None,
        all_chars: list[str],
    ) -> None:
        return self._archive_effects()._record_relation_events(
            log, scene, base, pov, all_chars
        )


    @staticmethod
    def _index_scene_to_vector_store(
        scene: SceneCard,
        content: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return SceneArchiveEffects._index_scene_to_vector_store(
            scene, content, project_id=project_id
        )

    def _detect_and_store_style_drift(self, scene: SceneCard) -> dict[str, Any]:
        return self._archive_effects()._detect_and_store_style_drift(scene)


    def _best_of_n_count(self, contract, *, criticality=None) -> int:
        """Number of style-draft candidates to generate for this run.

        The evidence-gated Best-of-N authorization policy was retired; production
        runs always draft a single candidate.  Tests may override this method to
        exercise the multi-candidate selection machinery.
        """
        self._best_of_n_policy_cap = 1
        return 1

    def _best_of_n_max_count(self, *, criticality=None, initial_count: int) -> int:
        """Cap progressive candidate expansion by the evidence authorization.

        A ``None`` cap means a legacy/test override replaced ``_best_of_n_count``;
        preserving the criticality maximum keeps those explicit harnesses stable.
        The real resolver always records an integer cap.
        """

        criticality_max = (
            int(criticality.max_best_of_n)
            if criticality is not None
            else max(1, int(initial_count))
        )
        policy_cap = getattr(self, "_best_of_n_policy_cap", None)
        if policy_cap is None:
            return max(1, criticality_max)
        return max(1, min(criticality_max, int(policy_cap)))

    def _offer_candidates_for_selection(
        self, scene, state, bundle, candidates
    ) -> list[str] | None:
        """Wave 3（§4.4/§5.5）：确定性坏稿淘汰后建立匿名候选终选 gate。

        机器只淘汰空文本与来源安全 Q0 命中的无效候选（不按机器分数删，
        §4.4）；全部无效时返回 None——管线继续，由 QC 层裁决，不装作可选。
        blinded_order 是随机置换（§5.5 展示顺序必须随机化并记录）。
        """
        import random
        import uuid
        from novel_system.db.models import HumanReviewEvent
        from novel_system.services.source_safety import scan_source_safety
        from novel_system.services.style_reference.style_feedback import (
            build_candidate_style_snapshot,
        )

        valid_candidates: list[Any] = []
        for cand in candidates:
            content = (getattr(cand, "content", "") or "").strip()
            if not content:
                continue
            if not scan_source_safety(content).get("safe", True):
                continue
            ranking = getattr(cand, "ranking_audit", None) or {}
            if (
                ranking.get("plagiarism_checked") is True
                and ranking.get("plagiarism_passed") is False
            ):
                continue
            valid_candidates.append(cand)
        valid_row_ids = [str(candidate.row_id) for candidate in valid_candidates]
        if not valid_row_ids:
            _LOGGER.warning(
                "no deterministically valid candidate to offer for scene %s; pipeline continues",
                scene.scene_id,
            )
            return None
        blinded_order = list(valid_row_ids)
        random.shuffle(blinded_order)
        event = HumanReviewEvent(
            event_id=f"hre_sel_{uuid.uuid4().hex[:12]}",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            object_ref=f"candidate_selection:{scene.scene_id}",
            event_source="candidate_selection",
            priority="high",
            status="awaiting_review",
            allowed_actions_json=["select", "reopen"],
            details_json={
                "gate_type": "style_candidate_selection",
                "candidate_row_ids": valid_row_ids,
                "blinded_order": blinded_order,
                "decision_status": "awaiting",
                "selected_row_id": None,
                "tokens_used": int(state.scene_tokens_used or 0),
                "decision_history": [],
                # Kept server-side and omitted from the blinded GET response.
                # Contains only scores/hashes/IDs—never candidate or source prose.
                "style_feedback_snapshot": build_candidate_style_snapshot(
                    valid_candidates
                ),
                "style_feedback_history": [],
            },
            default_action="select",
        )
        self.session.add(event)
        state.scene_status = "awaiting_candidate_selection"
        state.current_human_review_event_id = event.event_id
        self.session.flush()
        return valid_row_ids

    def resume_after_selection(
        self,
        scene_id: str,
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        state = self.session.get(SceneRunState, scene_id)
        previous_execution_id = state.active_execution_id if state is not None else None
        if not previous_execution_id:
            raise DomainError(
                "RESUME_EXECUTION_NOT_FOUND",
                "candidate selection has no durable execution to resume",
                status_code=409,
                details={"scene_id": scene_id},
            )
        effective_execution_id = (
            execution_id or f"direct-selection-resume:{scene_id}:{uuid4().hex}"
        )
        checkpoints = SceneRunCheckpointService(self.session)
        checkpoints.acquire_selection_resume(scene_id, effective_execution_id)

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            # This endpoint always owns the post-selection continuation. A failed
            # retry may already be at soft/near-final sub-checkpoints; routing it
            # through the ordinary run pipeline would recreate the selection gate.
            result = self._resume_after_selection_pipeline(scene_id)
            if result.get("scene_status") == "archived":
                checkpoints.mark_completed(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except LLMAccountingRejected as exc:
            # Candidate selection resume is synchronous, unlike run/jobs. A
            # lifecycle boundary is an expected recoverable stop: return a
            # durable payload so the UI can request an audited top-up and retry
            # this same post-selection pipeline, never the ordinary prefix.
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            state = self.session.get(SceneRunState, scene_id)
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": getattr(state, "scene_status", None),
                    "lifecycle_budget_block": {
                        "code": getattr(
                            exc, "code", "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
                        ),
                        "message": str(exc),
                        "resume_mode": "selection",
                    },
                },
            )
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None

    def _resume_after_selection_pipeline(self, scene_id: str) -> dict:
        """Wave 3（§5.5/§6.3）：作者终选后从批判修订/QC 续跑到归档。

        前置：终选 gate 已 selected，且持久化 checkpoint 已进入终选或
        终选后的软 QC / near-final 阶段。作者可见 scene_status 可能已提前
        发布为可恢复的 patch/revision 状态，不能把它当作 checkpoint 真值。
        选中稿即后续批判/软 QC/near-final 的输入（§4.4 上限归人）。
        """
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        state = self.session.get(SceneRunState, scene_id)
        checkpoint_payload = (
            (state.run_checkpoint_json or {}) if state is not None else {}
        )
        checkpoint = state.run_checkpoint if state is not None else None
        checkpoint_is_post_selection = (
            checkpoint in RUN_CHECKPOINT_ORDER
            and RUN_CHECKPOINT_ORDER.index(checkpoint)
            >= RUN_CHECKPOINT_ORDER.index("selection_wait")
            and bool(checkpoint_payload.get("selection_origin_execution_id"))
        )
        if state is None or not checkpoint_is_post_selection:
            raise DomainError(
                "RESUME_NOT_AVAILABLE",
                "scene has no resumable selection checkpoint",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "scene_status": getattr(state, "scene_status", None),
                    "run_checkpoint": checkpoint,
                },
            )
        # Selection handoff bypasses the ordinary run prefix, so validate the
        # durable lifecycle budget before any optional critique/QC provider work.
        self._validate_budget_checkpoint(state)
        selection_event_id = self._checkpoint_artifact(
            "selection_event_id",
            expected_node_at_least="selection_wait",
        )
        offered_row_ids = self._checkpoint_artifact(
            "selection_candidate_row_ids",
            expected_node_at_least="selection_wait",
        )
        from novel_system.db.models import HumanReviewEvent

        gate = (
            self.session.get(HumanReviewEvent, selection_event_id)
            if isinstance(selection_event_id, str)
            else None
        )
        if (
            gate is None
            or gate.scene_id != scene_id
            or gate.event_source != "candidate_selection"
            or not isinstance(offered_row_ids, list)
            or not offered_row_ids
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selection checkpoint event/candidate context is invalid",
                status_code=409,
            )
        details = dict(gate.details_json or {}) if gate is not None else {}
        selected_row_id = details.get("selected_row_id")
        if details.get("candidate_row_ids") != offered_row_ids:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selection gate candidates differ from the durable checkpoint",
                status_code=409,
            )
        if (
            gate is None
            or details.get("decision_status") != "selected"
            or not selected_row_id
        ):
            raise DomainError(
                "SELECTION_REQUIRED",
                "author terminal selection is required before resuming",
                status_code=409,
                details={"scene_id": scene_id},
            )
        if selected_row_id not in offered_row_ids:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selected candidate is outside the durable offered set",
                status_code=409,
            )

        # Selection is a hand-off, not a new prefix. Validate the complete durable
        # prefix before trusting the chosen style row or entering the post-style path.
        planning = self._load_planning_checkpoint(scene_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        self._load_checkpoint_draft(
            scene_id,
            ref_key="neutral_draft_row_id",
            expected_stage="neutral_draft",
            expected_node_at_least="neutral_ready",
            result_type="neutral",
        )
        hard_qc = self._load_hard_qc_checkpoint(scene_id)
        if not hard_qc.should_continue:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selection checkpoint follows a terminal hard QC decision",
                status_code=409,
            )
        self._load_style_checkpoint_candidates(scene_id)
        draft = self.session.get(SceneDraft, selected_row_id)
        if draft is None or not (draft.content or "").strip():
            self._raise_checkpoint_output_missing(row_id=selected_row_id)

        selected_index = offered_row_ids.index(selected_row_id)
        if (
            draft.scene_id != scene_id
            or draft.stage not in {"style_draft", "de_template"}
            or draft.source_bundle_id != bundle["bundle_id"]
            or draft.source_bundle_hash != bundle["bundle_snapshot_hash"]
            or self._text_hash(draft.content)
            != self._checkpoint_hash(f"selection_candidate_{selected_index}")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selected candidate identity/source/hash differs from the durable checkpoint",
                status_code=409,
            )
        checkpoint_payload = state.run_checkpoint_json or {}
        checkpoint_refs = checkpoint_payload.get("artifact_refs") or {}
        candidate_row_ids = checkpoint_refs.get("candidate_row_ids")
        candidate_llm_call_ids = checkpoint_refs.get("llm_call_ids")
        candidate_step_keys = checkpoint_refs.get("style_execution_step_keys")
        candidate_execution_ids = checkpoint_refs.get("style_artifact_execution_ids")
        if (
            not isinstance(candidate_row_ids, list)
            or selected_row_id not in candidate_row_ids
            or not isinstance(candidate_llm_call_ids, list)
            or not isinstance(candidate_step_keys, list)
            or not isinstance(candidate_execution_ids, list)
            or not (
                len(candidate_row_ids)
                == len(candidate_llm_call_ids)
                == len(candidate_step_keys)
                == len(candidate_execution_ids)
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "selected candidate ledger lineage is incomplete",
                status_code=409,
            )
        candidate_index = candidate_row_ids.index(selected_row_id)
        selected_llm_call_id = candidate_llm_call_ids[candidate_index]
        selected_step_key = candidate_step_keys[candidate_index]
        selected_execution_id = self._validate_artifact_execution_owner(
            candidate_execution_ids[candidate_index]
        )
        self._validate_checkpoint_llm_output(
            scene_id=scene_id,
            llm_call_id=selected_llm_call_id,
            execution_step_key=selected_step_key,
            execution_id=selected_execution_id,
        )
        selection_decision = {
            "selection_event_id": selection_event_id,
            "selected_row_id": selected_row_id,
            "offered_row_ids": offered_row_ids,
        }
        handoff_ref_keys = (
            "selected_row_id",
            "selected_llm_call_id",
            "selected_execution_step_key",
            "selected_artifact_execution_id",
        )
        checkpoint_hashes = checkpoint_payload.get("artifact_hashes") or {}
        handoff_already_committed = any(
            key in checkpoint_refs for key in handoff_ref_keys
        ) or ("selection_decision" in checkpoint_hashes)
        if handoff_already_committed:
            if (
                checkpoint_refs.get("selected_row_id") != selected_row_id
                or checkpoint_refs.get("selected_llm_call_id") != selected_llm_call_id
                or checkpoint_refs.get("selected_execution_step_key")
                != selected_step_key
                or checkpoint_refs.get("selected_artifact_execution_id")
                != selected_execution_id
                or self._json_hash(selection_decision)
                != self._checkpoint_hash("selection_decision")
                or gate.status != "resolved"
                or details.get("resumed") is not True
                or (
                    state.run_checkpoint == "selection_wait"
                    and (
                        state.current_style_draft_row_id != selected_row_id
                        or state.latest_valid_draft_row_id != selected_row_id
                    )
                )
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "selection resume sub-checkpoint differs from committed business state",
                    status_code=409,
                )
        else:
            if state.run_checkpoint != "selection_wait":
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "post-selection checkpoint is missing its durable handoff decision",
                    status_code=409,
                )
            state.current_style_draft_row_id = draft.row_id
            state.latest_valid_draft_row_id = draft.row_id
            gate.status = "resolved"
            gate.details_json = {**details, "resumed": True}
            self._save_run_checkpoint(
                "selection_wait",
                sub_index=0,
                artifact_refs={
                    "selected_row_id": selected_row_id,
                    "selected_llm_call_id": selected_llm_call_id,
                    "selected_execution_step_key": selected_step_key,
                    "selected_artifact_execution_id": selected_execution_id,
                },
                artifact_hashes={
                    "selection_decision": self._json_hash(selection_decision)
                },
                strategy="human_selection_resumed",
            )
        contract = self.execution_contract_service.get_or_create(
            scene_id, actor_ref="orchestrator"
        )
        from novel_system.services.scene_criticality import classify_scene_with_context

        # 与首跑主管线同一入口：续跑同样喂入 §6.4 连续过渡计数，判定不降级。
        criticality = classify_scene_with_context(self.session, scene)
        from types import SimpleNamespace

        style_generation = SimpleNamespace(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=selected_llm_call_id,
            execution_step_key=selected_step_key,
            artifact_execution_id=selected_execution_id,
        )
        hard_qc_payload = self._hard_qc_result_payload(hard_qc)
        candidates_total = len(details.get("candidate_row_ids") or []) or 1
        return self._finalize_after_style(
            scene=scene,
            state=state,
            contract=contract,
            bundle=bundle,
            criticality=criticality,
            planning=planning,
            hard_qc_payload=hard_qc_payload,
            style_generation=style_generation,
            candidate_summaries=None,
            candidates_total=candidates_total,
            run_policy=state.run_policy or "reliable",
        )

    def _scene_critique_context(self, scene: SceneCard, contract):
        """Build the §8 SceneContext for the LLM editor critic (best-effort; the critic
        degrades gracefully when fields are absent)."""
        from novel_system.services.auto_critique import SceneContext

        payload = getattr(contract, "payload_json", None) or {}
        brief = getattr(scene, "writer_brief_json", None) or {}
        tension = brief.get("tension_target")
        return SceneContext(
            scene_goal=str(
                getattr(scene, "scene_goal", "") or payload.get("scene_goal") or ""
            ),
            tension_target=tension if isinstance(tension, int) else None,
            cost_requirement=str(
                payload.get("cost_requirement") or brief.get("cost_requirement") or ""
            ),
        )


    @staticmethod
    def _soft_qc_result_payload(soft_qc) -> dict[str, str | None]:
        return {
            "branch": soft_qc.branch,
            "qc_report_id": soft_qc.qc_report_id,
            "human_review_event_id": soft_qc.human_review_event_id,
            "resolution_code": soft_qc.resolution_code,
            "next_action": soft_qc.next_action,
            "stop_reason": soft_qc.stop_reason,
        }

    # 键序即契约：hard_qc_decision checkpoint 哈希覆盖此序列化值，不得调整键序。
    @staticmethod
    def _hard_qc_result_payload(hard_qc) -> dict[str, str | None]:
        return {
            "branch": hard_qc.branch,
            "qc_report_id": hard_qc.qc_report_id,
            "human_review_event_id": hard_qc.human_review_event_id,
            "resolution_code": hard_qc.resolution_code,
            "next_action": hard_qc.next_action,
            "stop_reason": hard_qc.stop_reason,
        }

    @staticmethod
    def _apply_finality(result: dict, *, gate_summary: dict, warnings) -> None:
        # finality 四件套唯一装配点：顶层三布尔与 finality 镜像必须同源同值。
        result["safe_to_archive"] = bool(
            gate_summary.get("safe_to_archive", gate_summary.get("archivable", False))
        )
        result["literary_warnings_unresolved"] = bool(
            gate_summary.get("literary_warnings_unresolved") or warnings
        )
        result["author_confirmed_final"] = bool(
            gate_summary.get("author_confirmed_final")
        )
        result["finality"] = {
            "safe_to_archive": result["safe_to_archive"],
            "literary_warnings_unresolved": result["literary_warnings_unresolved"],
            "author_confirmed_final": result["author_confirmed_final"],
        }
