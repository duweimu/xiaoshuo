"""Near-final archive checkpoint stage cluster extracted from the scene orchestrator.

This module owns the ``near_final_ready`` sub-checkpoints 4..11 and the final
``archived`` node: the ``_archive_near_final_checkpoint`` driver plus its
product builders, per-stage validators, recovery helpers, and the ordered
archive manifest.  Every method here is a verbatim move from ``Orchestrator`` —
checkpoint node keys, step keys, sub_index values, artifact_refs/hashes keys,
and all RUN_CHECKPOINT_CORRUPT validation semantics are byte-for-byte
unchanged (this wave is a pure move; no table-driving of the stage blocks).

Dispatch contract: every cross-call — cluster-internal siblings, the checkpoint
kernel (``_save_run_checkpoint`` / ``_checkpoint_hash`` / ...), the archive
effect recorders, near-final loaders, and result projection helpers — routes
through ``self._orch`` (the hosting ``Orchestrator``, which keeps a one-line
delegate for each moved method).  This preserves the long-standing test seam
where suites override individual cluster methods as instance attributes on the
orchestrator and expect the driver and validators to observe the override, and
keeps the kernel's per-run execution ownership fields authoritative.

Hosts must construct a fresh ``SceneArchiveCheckpoint`` per call rather than
caching one across runs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    FinalScene,
    LlmCall,
    NarrativeEvent,
    SceneCard,
    SceneMemory,
    SceneRunState,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import (
    ACCOUNTING_EXECUTION_MODE_KEY,
    LLMAccountingError,
    LLMCallContext,
    validate_product_call,
)
from novel_system.services.llm_audit import sanitize_audit_summary
from novel_system.services.scene_run_checkpoint import RUN_CHECKPOINT_ORDER

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult

_LOGGER = logging.getLogger(__name__)


class SceneArchiveCheckpoint:
    def __init__(self, session: Session, host) -> None:
        self.session = session
        # All cross-calls route through the hosting Orchestrator so
        # instance-level overrides (a test seam) keep intercepting sibling
        # calls and the checkpoint kernel fields stay authoritative.
        self._orch = host

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
        scene_id = scene.scene_id
        selected_style = self._orch._load_selected_style_checkpoint(scene_id)
        soft_qc, soft_generation = self._orch._load_soft_qc_checkpoint(
            scene_id,
            selected_style_generation=selected_style,
        )
        final_scene, near_final_payload = self._orch._load_near_final_checkpoint(
            scene=scene,
            bundle=bundle,
            source_generation=soft_generation,
        )
        state_payload = state.run_checkpoint_json or {}
        refs = state_payload.get("artifact_refs") or {}
        carry_notes = list(refs.get("carry_notes") or [])
        if self._orch._json_hash(carry_notes) != self._orch._checkpoint_hash("carry_notes"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "near-final carry notes hash mismatch",
                status_code=409,
            )
        progress = self._orch._near_final_checkpoint_progress()
        if progress < 4:
            archive_result = self._orch.archiver.archive_final_scene(
                scene_id,
                final_scene.row_id,
                qc_report_id=soft_qc.qc_report_id,
                carry_notes_json=carry_notes,
                execution_id=self._orch._execution_id,
                finalize_scene_status=False,
            )
            archive_core_product = self._orch._archive_product(
                scene=scene,
                kind="core_archive",
                outcome="completed",
                step_key="archive:core:0",
                input_hash=self._orch._text_hash(final_scene.content),
                final_scene_row_id=final_scene.row_id,
                scene_memory_row_id=archive_result["scene_memory_row_id"],
                chapter_rolling_note_row_id=archive_result[
                    "chapter_rolling_note_row_id"
                ],
                archive_attempt_id=archive_result["archive_attempt_id"],
                final_scene_snapshot=self._orch._archive_final_scene_snapshot(final_scene),
                scene_memory_snapshot=self._orch._archive_scene_memory_snapshot(
                    self.session.get(SceneMemory, archive_result["scene_memory_row_id"])
                ),
                rolling_note_snapshot=self._orch._archive_rolling_note_snapshot(
                    self.session.get(
                        ChapterRollingNote,
                        archive_result["chapter_rolling_note_row_id"],
                    )
                ),
                archive_attempt_snapshot=self._orch._archive_attempt_snapshot(
                    self.session.get(
                        AttemptTracker,
                        archive_result["archive_attempt_id"],
                    )
                ),
            )
            self._orch._validate_archive_core_checkpoint(
                scene=scene,
                final_scene=final_scene,
                carry_notes=carry_notes,
                product=archive_core_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=4,
                artifact_refs={
                    "scene_memory_row_id": archive_result["scene_memory_row_id"],
                    "archive_core": archive_core_product,
                    "archive_final_scene_snapshot": archive_core_product[
                        "final_scene_snapshot"
                    ],
                    "archive_scene_memory_snapshot": archive_core_product[
                        "scene_memory_snapshot"
                    ],
                    "archive_rolling_note_snapshot": archive_core_product[
                        "rolling_note_snapshot"
                    ],
                    "archive_attempt_snapshot": archive_core_product[
                        "archive_attempt_snapshot"
                    ],
                },
                artifact_hashes={
                    "archive_core": self._orch._json_hash(archive_core_product),
                    "archive_final_scene_snapshot": self._orch._json_hash(
                        archive_core_product["final_scene_snapshot"]
                    ),
                    "archive_scene_memory_snapshot": self._orch._json_hash(
                        archive_core_product["scene_memory_snapshot"]
                    ),
                    "archive_rolling_note_snapshot": self._orch._json_hash(
                        archive_core_product["rolling_note_snapshot"]
                    ),
                    "archive_attempt_snapshot": self._orch._json_hash(
                        archive_core_product["archive_attempt_snapshot"]
                    ),
                },
            )
            progress = 4
        archive_result = self._orch._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
        )
        if progress < 5:
            rule_event_ids = (
                self._orch._record_narrative_events(
                    scene,
                    contract,
                    final_scene.content,
                    include_prose=False,
                    degrade_errors=False,
                    final_scene_row_id=final_scene.row_id,
                )
                or []
            )
            for ordinal, event_id in enumerate(rule_event_ids):
                event = self.session.get(NarrativeEvent, event_id)
                event.payload_json = {
                    **dict(event.payload_json or {}),
                    "archive_execution_id": self._orch._execution_id,
                    "archive_step_key": "archive:rule_events:0",
                    "archive_ordinal": ordinal,
                }
            self.session.flush()
            rule_events = self._orch._narrative_event_snapshots(rule_event_ids)
            rule_product = self._orch._archive_product(
                scene=scene,
                kind="rule_events",
                outcome="recorded",
                step_key="archive:rule_events:0",
                input_hash=self._orch._text_hash(final_scene.content),
                event_ids=rule_event_ids,
                events=rule_events,
            )
            self._orch._validate_archive_rule_events_checkpoint(
                scene,
                product=rule_product,
                event_ids=rule_event_ids,
                events=rule_events,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=5,
                artifact_refs={
                    "archive_rule_event_ids": rule_event_ids,
                    "archive_rule_events": rule_events,
                    "archive_rule_product": rule_product,
                },
                artifact_hashes={
                    "archive_rule_events": self._orch._json_hash(rule_events),
                    "archive_rule_product": self._orch._json_hash(rule_product),
                },
            )
            progress = 5
        self._orch._validate_archive_rule_events_checkpoint(scene)
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=5,
        )
        if progress < 6:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            self._orch._reconcile_execution_step("archive:prose_event_extract:0")
            recovered_prose = self._orch._recover_archive_prose_rejection()
            if recovered_prose is None:
                prose_result, prose_event_ids = self._orch._record_prose_events(
                    NarrativeEventLog(self.session),
                    scene,
                    self._orch._archive_event_base(scene, contract),
                    final_scene.content,
                    final_scene_row_id=final_scene.row_id,
                    return_event_ids=True,
                )
            else:
                prose_result, prose_event_ids = recovered_prose, []
            self.session.flush()
            prose_events = self._orch._narrative_event_snapshots(prose_event_ids)
            extraction_snapshot = prose_result.product_snapshot()
            prose_product = self._orch._archive_product(
                scene=scene,
                kind="prose_extraction",
                outcome=extraction_snapshot["outcome"],
                step_key="archive:prose_event_extract:0",
                input_hash=self._orch._text_hash(final_scene.content),
                extraction=extraction_snapshot,
                event_ids=prose_event_ids,
                events=prose_events,
            )
            if prose_result.llm_call_id is not None:
                prose_parent = self.session.get(LlmCall, prose_result.llm_call_id)
                if prose_parent is None:
                    raise LLMAccountingError(
                        "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID",
                        "prose extraction product parent disappeared before archive checkpoint",
                    )
                prose_parent.response_payload_summary = sanitize_audit_summary(
                    {
                        **dict(prose_parent.response_payload_summary or {}),
                        "archive_prose_product_hash": self._orch._json_hash(prose_product),
                    }
                )
            self.session.flush()
            self._orch._validate_archive_prose_checkpoint(
                scene,
                contract,
                product=prose_product,
                event_ids=prose_event_ids,
                events=prose_events,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=6,
                artifact_refs={
                    "archive_prose_product": prose_product,
                    "archive_prose_event_ids": prose_event_ids,
                    "archive_prose_events": prose_events,
                },
                artifact_hashes={
                    "archive_prose_product": self._orch._json_hash(prose_product),
                    "archive_prose_events": self._orch._json_hash(prose_events),
                },
            )
            progress = 6
        self._orch._validate_archive_prose_checkpoint(scene, contract)
        # Checkpointed extractor output is only a candidate product.  Stage it into
        # the accepted-canon review ledger; pending rows are invisible to replay.
        prose_checkpoint = dict(
            ((state.run_checkpoint_json or {}).get("artifact_refs") or {}).get(
                "archive_prose_product"
            )
            or {}
        )
        extraction_checkpoint = dict(prose_checkpoint.get("extraction") or {})
        from novel_system.services.canon_continuity import CanonContinuityService

        CanonContinuityService(self.session).stage_extraction(
            final_scene.row_id,
            outcome=str(extraction_checkpoint.get("outcome") or "not_invoked"),
            event_ids=list(prose_checkpoint.get("event_ids") or []),
            reason=extraction_checkpoint.get("reason"),
            error_code=extraction_checkpoint.get("error_code"),
        )
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=6,
        )
        if progress < 7:
            vector_result = self._orch._index_scene_to_vector_store(
                scene,
                final_scene.content,
                project_id=self._orch._resolve_scene_project_id(scene, contract),
            )
            vector_product = self._orch._archive_product(
                scene=scene,
                kind="vector_index",
                outcome=vector_result["outcome"],
                step_key="archive:vector_index:0",
                input_hash=self._orch._text_hash(final_scene.content),
                **{
                    key: value
                    for key, value in vector_result.items()
                    if key != "outcome"
                },
            )
            self._orch._validate_archive_vector_product(
                scene,
                final_scene,
                vector_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=7,
                artifact_refs={"archive_vector_product": vector_product},
                artifact_hashes={
                    "archive_vector_product": self._orch._json_hash(vector_product)
                },
            )
            progress = 7
        self._orch._validate_archive_vector_product(scene, final_scene)
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=7,
        )

        if progress < 8:
            chapter_product = self._orch._run_archive_chapter_aggregate(scene, final_scene)
            self._orch._validate_archive_chapter_product(
                scene,
                chapter_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=8,
                artifact_refs={"archive_chapter_product": chapter_product},
                artifact_hashes={
                    "archive_chapter_product": self._orch._json_hash(chapter_product)
                },
            )
            progress = 8
        self._orch._validate_archive_chapter_product(scene)
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=8,
        )

        if progress < 9:
            volume_product = self._orch._run_archive_volume_aggregate(scene, final_scene)
            self._orch._validate_archive_volume_product(
                scene,
                volume_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=9,
                artifact_refs={"archive_volume_product": volume_product},
                artifact_hashes={
                    "archive_volume_product": self._orch._json_hash(volume_product)
                },
            )
            progress = 9
        self._orch._validate_archive_volume_product(scene)
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=9,
        )

        chapter_near_final = None
        if progress < 10:
            chapter_evaluation_product = self._orch._run_archive_chapter_evaluation(
                scene,
                final_scene,
            )
            self._orch._validate_archive_chapter_evaluation_product(
                scene,
                chapter_evaluation_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=10,
                artifact_refs={
                    "archive_chapter_evaluation_product": chapter_evaluation_product,
                },
                artifact_hashes={
                    "archive_chapter_evaluation_product": self._orch._json_hash(
                        chapter_evaluation_product
                    ),
                },
            )
            progress = 10
        chapter_evaluation_product = self._orch._validate_archive_chapter_evaluation_product(
            scene
        )
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=10,
        )
        if chapter_evaluation_product.get("outcome") == "evaluated":
            chapter_near_final = chapter_evaluation_product.get("evaluation")

        if progress < 11:
            drift_result = (
                self._orch._detect_and_store_style_drift(scene)
                if scene.is_chapter_last == 1
                else {"outcome": "not_applicable", "reason": "not_chapter_last"}
            )
            drift_product = self._orch._archive_product(
                scene=scene,
                kind="style_drift",
                outcome=drift_result["outcome"],
                step_key="archive:style_drift:0",
                input_hash=self._orch._text_hash(final_scene.content),
                **{
                    key: value
                    for key, value in drift_result.items()
                    if key != "outcome"
                },
            )
            self._orch._validate_archive_drift_product(
                scene,
                drift_product,
                require_checkpoint_hash=False,
            )
            self._orch._save_run_checkpoint(
                "near_final_ready",
                sub_index=11,
                artifact_refs={"archive_drift_product": drift_product},
                artifact_hashes={
                    "archive_drift_product": self._orch._json_hash(drift_product)
                },
            )
            progress = 11
        self._orch._validate_archive_drift_product(scene)
        self._orch._validate_archive_prefix(
            scene=scene,
            contract=contract,
            final_scene=final_scene,
            carry_notes=carry_notes,
            through=11,
        )

        manifest = self._orch._archive_manifest()
        state.scene_status = "archived"
        self._orch._save_run_checkpoint(
            "archived",
            artifact_refs={
                "final_scene_row_id": final_scene.row_id,
                "scene_memory_row_id": archive_result.get("scene_memory_row_id"),
                "archive_manifest": manifest,
            },
            artifact_hashes={
                "final_scene": self._orch._text_hash(final_scene.content),
                "archive_manifest": self._orch._json_hash(manifest),
            },
        )

        near_final_warnings = self._orch._near_final_warning_findings(near_final_payload)
        result = self._orch._with_author_projection(
            scene_id,
            state,
            {
                "scene_status": state.scene_status,
                "current_bundle_id": bundle["bundle_id"],
                "current_bundle_hash": bundle["bundle_snapshot_hash"],
                "current_final_scene_row_id": final_scene.row_id,
                "current_qc_report_id": state.current_qc_report_id,
                "current_human_review_event_id": state.current_human_review_event_id,
                "hard_qc": hard_qc_payload,
                "soft_qc": self._orch._soft_qc_result_payload(soft_qc),
                "planning": planning,
                "near_final": near_final_payload,
                "chapter_near_final": chapter_near_final,
                "style_candidates": candidate_summaries,
                "run_policy": run_policy,
            },
        )
        result["quality_warnings"] = self._orch._merged_warnings(
            result.get("quality_warnings"), near_final_warnings
        )
        archive_attempt = self.session.get(
            AttemptTracker, archive_result.get("archive_attempt_id")
        )
        gate_summary = (
            (archive_attempt.details_json or {}).get("final_text_gate")
            if archive_attempt is not None
            else {}
        )
        self._orch._apply_finality(
            result, gate_summary=gate_summary, warnings=near_final_warnings
        )
        if near_final_warnings and "author_review_optional_fix" not in (
            result.get("recommended_actions") or []
        ):
            result["recommended_actions"] = [
                *(result.get("recommended_actions") or []),
                "author_review_optional_fix",
            ]
        return result

    def _near_final_checkpoint_progress(self) -> int:
        if self._orch._execution_id is None or self._orch._checkpoint_service is None:
            return -1
        state = self._orch._active_checkpoint_state()
        current = state.run_checkpoint
        if current not in RUN_CHECKPOINT_ORDER:
            return -1
        near_index = RUN_CHECKPOINT_ORDER.index("near_final_ready")
        current_index = RUN_CHECKPOINT_ORDER.index(current)
        if current_index < near_index:
            return -1
        if current_index > near_index:
            return 11
        payload = state.run_checkpoint_json or {}
        sub_index = payload.get("sub_index") if isinstance(payload, dict) else None
        if (
            isinstance(sub_index, int)
            and not isinstance(sub_index, bool)
            and sub_index in set(range(12))
        ):
            return sub_index
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if (
            sub_index is None
            and isinstance(refs, dict)
            and refs.get("final_scene_row_id")
        ):
            return 3
        raise DomainError(
            "RUN_CHECKPOINT_CORRUPT",
            "near-final checkpoint sub-index is invalid",
            status_code=409,
        )

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
        return {
            "schema_version": 1,
            "kind": kind,
            "outcome": outcome,
            "execution_id": self._orch._execution_id,
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "step_key": step_key,
            "input_hash": input_hash,
            **details,
        }

    @staticmethod
    def _archive_final_scene_snapshot(row: FinalScene) -> dict[str, Any]:
        return {
            "row_id": row.row_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "content": row.content,
            "status": row.status,
            "source_bundle_id": row.source_bundle_id,
            "source_bundle_hash": row.source_bundle_hash,
            "generation_llm_call_id": row.generation_llm_call_id,
            "created_at": row.created_at,
        }

    @staticmethod
    def _archive_scene_memory_snapshot(row: SceneMemory) -> dict[str, Any]:
        return {
            "row_id": row.row_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "content": row.content,
            "carry_notes_json": list(row.carry_notes_json or []),
            "source_bundle_id": row.source_bundle_id,
            "final_scene_row_id": row.final_scene_row_id,
            "source_review_id": row.source_review_id,
            "active_flag": row.active_flag,
            "runtime_eligible": row.runtime_eligible,
            "runtime_eligibility_basis": row.runtime_eligibility_basis,
            "effective_at": row.effective_at,
            "created_at": row.created_at,
        }

    @staticmethod
    def _archive_rolling_note_snapshot(row: ChapterRollingNote) -> dict[str, Any]:
        return {
            "row_id": row.row_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "source_scene_memory_row_id": row.source_scene_memory_row_id,
            "note_text": row.note_text,
            "revision_no": row.revision_no,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _archive_attempt_snapshot(row: AttemptTracker) -> dict[str, Any]:
        return {
            "attempt_id": row.attempt_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "step": row.step,
            "status": row.status,
            "source_bundle_id": row.source_bundle_id,
            "details_json": dict(row.details_json or {}),
            "created_at": row.created_at,
        }

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
        payload = self._orch._active_checkpoint_state().run_checkpoint_json or {}
        refs = payload.get("artifact_refs") or {}
        product = product or refs.get("archive_core")
        if (
            not isinstance(product, dict)
            or set(product)
            != {
                "schema_version",
                "kind",
                "outcome",
                "execution_id",
                "scene_id",
                "chapter_id",
                "step_key",
                "input_hash",
                "final_scene_row_id",
                "scene_memory_row_id",
                "chapter_rolling_note_row_id",
                "archive_attempt_id",
                "final_scene_snapshot",
                "scene_memory_snapshot",
                "rolling_note_snapshot",
                "archive_attempt_snapshot",
            }
            or product.get("schema_version") != 1
            or product.get("kind") != "core_archive"
            or product.get("outcome") != "completed"
            or product.get("execution_id") != self._orch._execution_id
            or product.get("scene_id") != scene.scene_id
            or product.get("chapter_id") != scene.chapter_id
            or product.get("step_key") != "archive:core:0"
            or product.get("input_hash") != self._orch._text_hash(final_scene.content)
            or product.get("final_scene_row_id") != final_scene.row_id
            or (
                require_checkpoint_hash
                and self._orch._json_hash(product) != self._orch._checkpoint_hash("archive_core")
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive core checkpoint product schema/owner/hash is invalid",
                status_code=409,
            )
        memory = self.session.get(SceneMemory, product["scene_memory_row_id"])
        rolling = self.session.get(
            ChapterRollingNote,
            product["chapter_rolling_note_row_id"],
        )
        attempt = self.session.get(AttemptTracker, product["archive_attempt_id"])
        state = self._orch._active_checkpoint_state()
        snapshot_refs = {
            "final_scene_snapshot": refs.get("archive_final_scene_snapshot"),
            "scene_memory_snapshot": refs.get("archive_scene_memory_snapshot"),
            "rolling_note_snapshot": refs.get("archive_rolling_note_snapshot"),
            "archive_attempt_snapshot": refs.get("archive_attempt_snapshot"),
        }
        if require_checkpoint_hash and any(
            snapshot_refs[key] != product.get(key)
            or self._orch._json_hash(snapshot_refs[key])
            != self._orch._checkpoint_hash(
                {
                    "final_scene_snapshot": "archive_final_scene_snapshot",
                    "scene_memory_snapshot": "archive_scene_memory_snapshot",
                    "rolling_note_snapshot": "archive_rolling_note_snapshot",
                    "archive_attempt_snapshot": "archive_attempt_snapshot",
                }[key]
            )
            for key in snapshot_refs
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive core independent snapshot hashes are invalid",
                status_code=409,
            )
        if memory is None or rolling is None or attempt is None:
            self._orch._raise_checkpoint_output_missing(
                row_id=(
                    product["scene_memory_row_id"]
                    if memory is None
                    else (
                        product["chapter_rolling_note_row_id"]
                        if rolling is None
                        else str(product["archive_attempt_id"])
                    )
                )
            )
        if (
            product.get("final_scene_snapshot")
            != self._orch._archive_final_scene_snapshot(final_scene)
            or product.get("scene_memory_snapshot")
            != self._orch._archive_scene_memory_snapshot(memory)
            or product.get("rolling_note_snapshot")
            != self._orch._archive_rolling_note_snapshot(rolling)
            or product.get("archive_attempt_snapshot")
            != self._orch._archive_attempt_snapshot(attempt)
            or final_scene.status != "archived"
            or (
                state.scene_status != "archived"
                if allow_terminal
                else state.scene_status == "archived"
            )
            or state.current_final_scene_row_id != final_scene.row_id
            or memory.scene_id != scene.scene_id
            or memory.chapter_id != scene.chapter_id
            or memory.final_scene_row_id != final_scene.row_id
            or memory.source_bundle_id != final_scene.source_bundle_id
            or memory.content != final_scene.content
            or memory.carry_notes_json != carry_notes
            or memory.active_flag != 1
            or memory.runtime_eligible != 1
            or rolling.scene_id != scene.scene_id
            or rolling.chapter_id != scene.chapter_id
            or rolling.source_scene_memory_row_id != memory.row_id
            or rolling.note_text != final_scene.content
            or attempt.scene_id != scene.scene_id
            or attempt.chapter_id != scene.chapter_id
            or attempt.step != "archive"
            or attempt.status != "completed"
            or attempt.source_bundle_id != final_scene.source_bundle_id
            or (attempt.details_json or {}).get("final_scene_row_id")
            != final_scene.row_id
            or (attempt.details_json or {}).get("execution_id") != self._orch._execution_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive core checkpoint product graph is inconsistent",
                status_code=409,
            )
        return {
            "scene_memory_row_id": memory.row_id,
            "chapter_rolling_note_row_id": rolling.row_id,
            "archive_attempt_id": attempt.attempt_id,
            "scene_status": state.scene_status,
        }

    @staticmethod
    def _narrative_event_snapshot(event: NarrativeEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "project_id": event.project_id,
            "scene_id": event.scene_id,
            "chapter_id": event.chapter_id,
            "scene_seq": event.scene_seq,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "fact_key": event.fact_key,
            "fact_value": event.fact_value,
            "confidence": event.confidence,
            "causal_predecessor_id": event.causal_predecessor_id,
            "theme_tags": list(event.theme_tags or []),
            "obligation_ids": list(event.obligation_ids or []),
            "source_text_excerpt": event.source_text_excerpt,
            "payload_json": dict(event.payload_json or {}),
            "created_at": event.created_at,
        }

    def _narrative_event_snapshots(self, event_ids: list[str]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for event_id in event_ids:
            event = self.session.get(NarrativeEvent, event_id)
            if event is None:
                self._orch._raise_checkpoint_output_missing(row_id=event_id)
            snapshots.append(self._orch._narrative_event_snapshot(event))
        return snapshots

    def _validate_archive_rule_events_checkpoint(
        self,
        scene: SceneCard,
        *,
        product: dict[str, Any] | None = None,
        event_ids: list[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        require_checkpoint_hash: bool = True,
    ) -> None:
        refs = (self._orch._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        event_ids = (
            event_ids if event_ids is not None else refs.get("archive_rule_event_ids")
        )
        events = events if events is not None else refs.get("archive_rule_events")
        product = product if product is not None else refs.get("archive_rule_product")
        final_scene = self.session.get(FinalScene, refs.get("final_scene_row_id"))
        expected_product = (
            self._orch._archive_product(
                scene=scene,
                kind="rule_events",
                outcome="recorded",
                step_key="archive:rule_events:0",
                input_hash=self._orch._text_hash(final_scene.content),
                event_ids=event_ids,
                events=events,
            )
            if final_scene is not None
            else None
        )
        if (
            not isinstance(event_ids, list)
            or any(
                not isinstance(event_id, str) or not event_id for event_id in event_ids
            )
            or len(event_ids) != len(set(event_ids))
            or not isinstance(events, list)
            or not isinstance(product, dict)
            or product != expected_product
            or (
                require_checkpoint_hash
                and self._orch._json_hash(events)
                != self._orch._checkpoint_hash("archive_rule_events")
            )
            or (
                require_checkpoint_hash
                and self._orch._json_hash(product)
                != self._orch._checkpoint_hash("archive_rule_product")
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive rule-event checkpoint schema/owner/hash is invalid",
                status_code=409,
            )
        actual = self._orch._narrative_event_snapshots(event_ids)
        if actual != events or any(
            event.get("scene_id") != scene.scene_id
            or event.get("chapter_id") != scene.chapter_id
            or event.get("confidence") != "high"
            or (event.get("payload_json") or {}).get("source") == "prose"
            or (event.get("payload_json") or {}).get("archive_execution_id")
            != self._orch._execution_id
            or (event.get("payload_json") or {}).get("archive_step_key")
            != "archive:rule_events:0"
            or (event.get("payload_json") or {}).get("archive_ordinal") != ordinal
            for ordinal, event in enumerate(actual)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive rule-event checkpoint rows are missing, detached, or changed",
                status_code=409,
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
        refs = (self._orch._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_refs",
            {},
        )
        product = product if product is not None else refs.get("archive_prose_product")
        event_ids = (
            event_ids if event_ids is not None else refs.get("archive_prose_event_ids")
        )
        events = events if events is not None else refs.get("archive_prose_events")
        final_scene = self.session.get(FinalScene, refs.get("final_scene_row_id"))
        extraction = product.get("extraction") if isinstance(product, dict) else None
        if (
            final_scene is None
            or not isinstance(product, dict)
            or not isinstance(extraction, dict)
            or product
            != self._orch._archive_product(
                scene=scene,
                kind="prose_extraction",
                outcome=extraction.get("outcome"),
                step_key="archive:prose_event_extract:0",
                input_hash=self._orch._text_hash(final_scene.content),
                extraction=extraction,
                event_ids=event_ids,
                events=events,
            )
            or not isinstance(event_ids, list)
            or any(
                not isinstance(event_id, str) or not event_id for event_id in event_ids
            )
            or len(event_ids) != len(set(event_ids))
            or not isinstance(events, list)
            or (
                require_checkpoint_hash
                and self._orch._json_hash(product)
                != self._orch._checkpoint_hash("archive_prose_product")
            )
            or (
                require_checkpoint_hash
                and self._orch._json_hash(events)
                != self._orch._checkpoint_hash("archive_prose_events")
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive prose-extraction checkpoint schema/owner/hash is invalid",
                status_code=409,
            )

        expected_extraction_fields = {
            "schema_version",
            "outcome",
            "events",
            "llm_call_id",
            "execution_id",
            "execution_step_key",
            "run_job_id",
            "reason",
            "error_code",
        }
        outcome = extraction.get("outcome")
        if (
            set(extraction) != expected_extraction_fields
            or extraction.get("schema_version") != 1
            or outcome
            not in {
                "not_invoked",
                "rejected_before_dispatch",
                "provider_failed",
                "parse_failed",
                "completed_empty",
                "completed_events",
            }
            or not self._orch._checkpoint_execution_owner_matches(
                extraction.get("execution_id"), extraction.get("run_job_id")
            )
            or extraction.get("execution_step_key") != "archive:prose_event_extract:0"
            or not isinstance(extraction.get("events"), list)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive prose-extraction product field matrix is invalid",
                status_code=409,
            )
        call_id = extraction.get("llm_call_id")
        if outcome == "not_invoked":
            if call_id is not None or extraction.get("error_code") is not None:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive prose no-call product has a parent/error",
                    status_code=409,
                )
            ledger = (
                self.session.execute(
                    select(LlmCall).where(
                        LlmCall.execution_id == self._orch._execution_id,
                        LlmCall.execution_step_key == "archive:prose_event_extract:0",
                    )
                )
                .scalars()
                .all()
            )
            if ledger:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive prose no-call product unexpectedly has a ledger",
                    status_code=409,
                )
        else:
            if not isinstance(call_id, str) or not call_id:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive prose called product has no parent id",
                    status_code=409,
                )
            parent = self.session.get(LlmCall, call_id)
            base = self._orch._archive_event_base(scene, contract)
            context = LLMCallContext(
                scope_type="scene",
                scope_id=scene.scene_id,
                project_id=base["project_id"],
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                node_id="extraction",
                step="archive:prose_event_extract:0",
                execution_id=self._orch._execution_id,
                execution_step_key="archive:prose_event_extract:0",
                run_job_id=self._orch._run_job_id,
                provider_execution_mode="online",
            )
            expected_outcome = {
                "completed_empty": "completed",
                "completed_events": "completed",
                "parse_failed": "parse_failed",
                "provider_failed": "provider_failed",
                "rejected_before_dispatch": "rejected_before_dispatch",
            }[outcome]
            try:
                validate_product_call(
                    self.session,
                    call_id,
                    context,
                    expected_outcome=expected_outcome,
                    expected_error_code=(
                        extraction.get("error_code")
                        if expected_outcome
                        in {"provider_failed", "rejected_before_dispatch"}
                        else None
                    ),
                )
            except LLMAccountingError as exc:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive prose parent/attempt ledger is invalid",
                    status_code=409,
                    details={"llm_call_id": call_id, "error_code": exc.code},
                ) from exc
            if not isinstance(
                parent.response_payload_summary, dict
            ) or parent.response_payload_summary.get(
                "archive_prose_product_hash"
            ) != self._orch._json_hash(
                product
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive prose product hash is detached from its parent",
                    status_code=409,
                )
            if outcome in {"completed_empty", "completed_events"}:
                from novel_system.services.prose_event_extractor import (
                    prose_extraction_parsed_hash,
                )

                if parent.response_payload_summary.get(
                    "prose_extraction_parsed_hash"
                ) != prose_extraction_parsed_hash(extraction.get("events") or []):
                    raise DomainError(
                        "RUN_CHECKPOINT_CORRUPT",
                        "archive prose parsed output hash is detached from its parent",
                        status_code=409,
                    )
        actual = self._orch._narrative_event_snapshots(event_ids)
        extracted_events = extraction.get("events") or []
        if (
            actual != events
            or len(actual) != len(extracted_events)
            or any(
                event.get("scene_id") != scene.scene_id
                or event.get("chapter_id") != scene.chapter_id
                or event.get("confidence") != "extracted"
                or (event.get("payload_json") or {}).get("source") != "prose"
                or (event.get("payload_json") or {}).get("archive_execution_id")
                != self._orch._execution_id
                or (event.get("payload_json") or {}).get("archive_step_key")
                != "archive:prose_event_extract:0"
                or (event.get("payload_json") or {}).get("archive_ordinal") != ordinal
                or {
                    "event_type": event.get("event_type"),
                    "entity_id": event.get("entity_id"),
                    "fact_key": event.get("fact_key"),
                    "fact_value": event.get("fact_value"),
                    "evidence": (
                        event.get("source_text_excerpt") or ""
                        if extracted_events[ordinal].get("evidence")
                        else ""
                    ),
                }
                != extracted_events[ordinal]
                for ordinal, event in enumerate(actual)
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive prose event rows are missing, detached, or changed",
                status_code=409,
            )

    def _recover_archive_prose_rejection(self) -> ProseExtractionResult | None:
        """Restore a durable local rejection without creating a second parent call."""

        from novel_system.services.prose_event_extractor import ProseExtractionResult

        calls = list(
            self.session.scalars(
                select(LlmCall)
                .where(
                    LlmCall.scene_id == self._orch._active_checkpoint_state().scene_id,
                    LlmCall.execution_id == self._orch._execution_id,
                    LlmCall.execution_step_key == "archive:prose_event_extract:0",
                )
                .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
            ).all()
        )
        rejected = [
            call
            for call in calls
            if call.accounting_status == "rejected"
            and call.request_dispatched_at is None
        ]
        if not rejected:
            return None
        if len(rejected) != 1 or any(
            call is not rejected[0] and call.accounting_status != "released"
            for call in calls
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive prose rejected tombstone ledger is ambiguous",
                status_code=409,
            )
        parent = rejected[0]
        if not isinstance(parent.error_code, str) or not parent.error_code:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive prose rejected tombstone has no error code",
                status_code=409,
            )
        return ProseExtractionResult(
            outcome="rejected_before_dispatch",
            llm_call_id=parent.llm_call_id,
            execution_id=self._orch._execution_id,
            execution_step_key="archive:prose_event_extract:0",
            run_job_id=self._orch._run_job_id,
            reason="pre_dispatch_rejection",
            error_code=parent.error_code,
        )

    def _archive_checkpoint_ref(self, key: str) -> Any:
        return (
            (self._orch._active_checkpoint_state().run_checkpoint_json or {}).get(
                "artifact_refs", {}
            )
        ).get(key)

    def _validate_common_archive_product(
        self,
        *,
        scene: SceneCard,
        product: Any,
        kind: str,
        step_key: str,
        outcomes: set[str],
    ) -> dict[str, Any]:
        if (
            not isinstance(product, dict)
            or product.get("schema_version") != 1
            or product.get("kind") != kind
            or product.get("outcome") not in outcomes
            or product.get("execution_id") != self._orch._execution_id
            or product.get("scene_id") != scene.scene_id
            or product.get("chapter_id") != scene.chapter_id
            or product.get("step_key") != step_key
            or not isinstance(product.get("input_hash"), str)
            or not product.get("input_hash")
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"archive {kind} product schema/owner is invalid",
                status_code=409,
            )
        return product

    def _validate_archive_vector_product(
        self,
        scene: SceneCard,
        final_scene: FinalScene,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._orch._archive_checkpoint_ref("archive_vector_product")
        product = self._orch._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="vector_index",
            step_key="archive:vector_index:0",
            outcomes={"indexed", "already_present", "non_persistent", "failed"},
        )
        if (
            product.get("input_hash") != self._orch._text_hash(final_scene.content)
            or product.get("vector_id") != scene.scene_id
            or product.get("text_hash")
            != self._orch._text_hash((final_scene.content or "")[:600])
            or not isinstance(product.get("collection_name"), str)
            or product.get("backend") not in {"memory", "chroma"}
            or product.get("validation_scope")
            != ("process_local" if product.get("backend") == "memory" else "persistent")
            or product.get("write_status")
            not in {"indexed", "already_present", "failed"}
            or (
                product.get("backend") == "memory"
                and product.get("outcome") not in {"non_persistent", "failed"}
            )
            or (
                product.get("backend") != "memory"
                and product.get("outcome") == "non_persistent"
            )
            or (
                require_checkpoint_hash
                and self._orch._json_hash(product)
                != self._orch._checkpoint_hash("archive_vector_product")
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive vector product identity/hash is invalid",
                status_code=409,
            )
        if product["outcome"] == "non_persistent":
            if product.get("error_code") is not None or product.get(
                "write_status"
            ) not in {
                "indexed",
                "already_present",
            }:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-persistent vector product has invalid local write evidence",
                    status_code=409,
                )
            return product
        if product["outcome"] in {"indexed", "already_present"}:
            if (
                product.get("error_code") is not None
                or product.get("write_status") != product["outcome"]
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "persistent vector product outcome does not match its write evidence",
                    status_code=409,
                )
            from novel_system.services.vector_store import get_vector_store

            store = get_vector_store(backend=product["backend"])
            collection_exists = store.collection_exists(product["collection_name"])
            if not collection_exists and product["validation_scope"] == "process_local":
                return product
            rows = (
                store.load_collection(product["collection_name"])
                if collection_exists
                else []
            )
            matches = [row for row in rows if row.get("id") == scene.scene_id]
            if (
                len(matches) != 1
                or self._orch._text_hash(str(matches[0].get("text") or ""))
                != product["text_hash"]
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "archive vector product no longer matches the external index",
                    status_code=409,
                )
        elif (
            not isinstance(product.get("error_code"), str)
            or product.get("write_status") != "failed"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive vector failure has no stable error code",
                status_code=409,
            )
        return product

    def _scene_memory_inputs(self, chapter_id: str) -> list[dict[str, str]]:
        memories = list(
            self.session.scalars(
                select(SceneMemory)
                .where(
                    SceneMemory.chapter_id == chapter_id,
                    SceneMemory.active_flag == 1,
                )
                .order_by(SceneMemory.row_id.asc())
            ).all()
        )
        return [
            {
                "row_id": memory.row_id,
                "scene_id": memory.scene_id,
                "chapter_id": memory.chapter_id,
                "content_hash": self._orch._text_hash(memory.content),
            }
            for memory in memories
        ]

    @staticmethod
    def _chapter_memory_snapshot(memory: ChapterMemory) -> dict[str, Any]:
        return {
            "row_id": memory.row_id,
            "chapter_id": memory.chapter_id,
            "aggregate_stage": memory.aggregate_stage,
            "content": memory.content,
            "memory_kind": memory.memory_kind,
            "source_review_id": memory.source_review_id,
            "active_flag": memory.active_flag,
            "runtime_eligible": memory.runtime_eligible,
            "runtime_eligibility_basis": memory.runtime_eligibility_basis,
            "effective_at": memory.effective_at,
            "created_at": memory.created_at,
        }

    def _run_archive_chapter_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._orch._archive_product(
                scene=scene,
                kind="chapter_aggregate",
                outcome="not_applicable",
                step_key="archive:chapter_aggregate:0",
                input_hash=self._orch._text_hash(final_scene.content),
                reason="not_chapter_last",
                inputs=[],
                result=None,
                chapter_memory=None,
            )
        inputs = self._orch._scene_memory_inputs(scene.chapter_id)
        result = self._orch.aggregator.run_final_aggregate(scene.chapter_id)
        self.session.flush()
        row_id = (
            result.get("chapter_memory_row_id") if isinstance(result, dict) else None
        )
        memory = (
            self.session.get(ChapterMemory, row_id) if isinstance(row_id, str) else None
        )
        return self._orch._archive_product(
            scene=scene,
            kind="chapter_aggregate",
            outcome=("aggregated" if memory is not None else "no_op"),
            step_key="archive:chapter_aggregate:0",
            input_hash=self._orch._json_hash(inputs),
            reason=(
                (result or {}).get("reason")
                if isinstance(result, dict)
                else "no_result"
            ),
            inputs=inputs,
            result=result,
            chapter_memory=(
                self._orch._chapter_memory_snapshot(memory) if memory is not None else None
            ),
        )

    def _validate_archive_chapter_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._orch._archive_checkpoint_ref("archive_chapter_product")
        product = self._orch._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="chapter_aggregate",
            step_key="archive:chapter_aggregate:0",
            outcomes={"not_applicable", "aggregated", "no_op"},
        )
        if require_checkpoint_hash and self._orch._json_hash(
            product
        ) != self._orch._checkpoint_hash("archive_chapter_product"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter aggregate product hash mismatch",
                status_code=409,
            )
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._orch._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("inputs") != []
                or final_scene is None
                or product.get("input_hash") != self._orch._text_hash(final_scene.content)
                or product.get("result") is not None
                or product.get("chapter_memory") is not None
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-final scene chapter product is invalid",
                    status_code=409,
                )
            return product
        inputs = product.get("inputs")
        if (
            not isinstance(inputs, list)
            or inputs != sorted(inputs, key=lambda item: item.get("row_id", ""))
            or product.get("input_hash") != self._orch._json_hash(inputs)
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter aggregate input manifest is invalid",
                status_code=409,
            )
        for item in inputs:
            memory = self.session.get(
                SceneMemory, item.get("row_id") if isinstance(item, dict) else None
            )
            if memory is None:
                self._orch._raise_checkpoint_output_missing(row_id=(item or {}).get("row_id"))
            if (
                memory.scene_id != item.get("scene_id")
                or memory.chapter_id != scene.chapter_id
                or item.get("chapter_id") != scene.chapter_id
                or self._orch._text_hash(memory.content) != item.get("content_hash")
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "chapter aggregate input memory changed",
                    status_code=409,
                )
        snapshot = product.get("chapter_memory")
        if product.get("outcome") == "aggregated":
            memory = self.session.get(ChapterMemory, (snapshot or {}).get("row_id"))
            if memory is None:
                self._orch._raise_checkpoint_output_missing(
                    row_id=(snapshot or {}).get("row_id")
                )
            actual = self._orch._chapter_memory_snapshot(memory)
            for mutable_field in (
                "active_flag",
                "runtime_eligible",
                "runtime_eligibility_basis",
            ):
                actual[mutable_field] = snapshot.get(mutable_field)
            expected_content = "\n".join(
                self.session.get(SceneMemory, item["row_id"]).content for item in inputs
            )
            if actual != snapshot or memory.content != expected_content:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "chapter aggregate output changed",
                    status_code=409,
                )
        elif snapshot is not None:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter no-op unexpectedly has output",
                status_code=409,
            )
        if (
            product.get("outcome") == "no_op"
            and isinstance(product.get("result"), dict)
            and product["result"].get("status") == "created"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter aggregate created result lost its output",
                status_code=409,
            )
        return product

    def _volume_input_memories(self, scene: SceneCard) -> list[dict[str, str]]:
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if (
            chapter is None
            or chapter.project_id is None
            or chapter.display_order is None
        ):
            return []
        chapters = list(
            self.session.scalars(
                select(ChapterGoal)
                .where(
                    ChapterGoal.project_id == chapter.project_id,
                    ChapterGoal.trashed_flag == 0,
                    ChapterGoal.display_order.isnot(None),
                    ChapterGoal.display_order <= chapter.display_order,
                )
                .order_by(ChapterGoal.display_order.desc())
                .limit(5)
            ).all()
        )
        chapter_ids = [item.chapter_id for item in reversed(chapters)]
        rows = list(
            self.session.scalars(
                select(ChapterMemory).where(
                    ChapterMemory.chapter_id.in_(chapter_ids),
                    ChapterMemory.aggregate_stage == "final",
                    ChapterMemory.active_flag == 1,
                )
            ).all()
        )
        order = {chapter_id: ordinal for ordinal, chapter_id in enumerate(chapter_ids)}
        rows.sort(key=lambda row: (order.get(row.chapter_id, 999), row.row_id))
        return [
            {
                "row_id": row.row_id,
                "chapter_id": row.chapter_id,
                "content_hash": self._orch._text_hash(row.content),
            }
            for row in rows
        ]

    @staticmethod
    def _volume_snapshot(row: VolumeSummary) -> dict[str, Any]:
        return {
            "row_id": row.row_id,
            "project_id": row.project_id,
            "volume_seq": row.volume_seq,
            "chapter_id_start": row.chapter_id_start,
            "chapter_id_end": row.chapter_id_end,
            "chapter_count": row.chapter_count,
            "atmosphere_summary": row.atmosphere_summary,
            "factual_digest": row.factual_digest,
            "active_flag": row.active_flag,
            "runtime_eligible": row.runtime_eligible,
            "runtime_eligibility_basis": row.runtime_eligibility_basis,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _run_archive_volume_aggregate(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._orch._archive_product(
                scene=scene,
                kind="volume_aggregate",
                outcome="not_applicable",
                step_key="archive:volume_aggregate:0",
                input_hash=self._orch._text_hash(final_scene.content),
                reason="not_chapter_last",
                inputs=[],
                result=None,
                volume_summary=None,
            )
        inputs = self._orch._volume_input_memories(scene)
        try:
            result = self._orch.aggregator.maybe_aggregate_volume(scene.chapter_id)
            row_id = (
                result.get("volume_summary_row_id")
                if isinstance(result, dict)
                else None
            )
            row = (
                self.session.get(VolumeSummary, row_id)
                if isinstance(row_id, str)
                else None
            )
            outcome = "aggregated" if row is not None else "no_op"
            error_code = None
        except Exception as exc:
            _LOGGER.warning(
                "volume aggregation degraded for chapter %s",
                scene.chapter_id,
                exc_info=True,
            )
            result, row, outcome, error_code = (
                None,
                None,
                "degraded",
                exc.__class__.__name__,
            )
        return self._orch._archive_product(
            scene=scene,
            kind="volume_aggregate",
            outcome=outcome,
            step_key="archive:volume_aggregate:0",
            input_hash=self._orch._json_hash(inputs),
            reason=(
                (result or {}).get("reason")
                if isinstance(result, dict)
                else ("aggregation_failed" if error_code else "no_result")
            ),
            error_code=error_code,
            inputs=inputs,
            result=result,
            volume_summary=(self._orch._volume_snapshot(row) if row is not None else None),
        )

    def _validate_archive_volume_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._orch._archive_checkpoint_ref("archive_volume_product")
        product = self._orch._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="volume_aggregate",
            step_key="archive:volume_aggregate:0",
            outcomes={"not_applicable", "aggregated", "no_op", "degraded"},
        )
        if require_checkpoint_hash and self._orch._json_hash(
            product
        ) != self._orch._checkpoint_hash("archive_volume_product"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "volume aggregate product hash mismatch",
                status_code=409,
            )
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._orch._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("inputs") != []
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-final scene volume product is invalid",
                    status_code=409,
                )
            if (
                final_scene is None
                or product.get("input_hash") != self._orch._text_hash(final_scene.content)
                or product.get("result") is not None
                or product.get("volume_summary") is not None
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-final scene volume no-op payload is invalid",
                    status_code=409,
                )
            return product
        inputs = product.get("inputs")
        if not isinstance(inputs, list) or product.get("input_hash") != self._orch._json_hash(
            inputs
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "volume aggregate input manifest is invalid",
                status_code=409,
            )
        for item in inputs:
            row = self.session.get(
                ChapterMemory, item.get("row_id") if isinstance(item, dict) else None
            )
            if row is None:
                self._orch._raise_checkpoint_output_missing(row_id=(item or {}).get("row_id"))
            if row.chapter_id != item.get("chapter_id") or self._orch._text_hash(
                row.content
            ) != item.get("content_hash"):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "volume aggregate input changed",
                    status_code=409,
                )
        snapshot = product.get("volume_summary")
        if product.get("outcome") == "aggregated":
            row = self.session.get(VolumeSummary, (snapshot or {}).get("row_id"))
            if row is None:
                self._orch._raise_checkpoint_output_missing(
                    row_id=(snapshot or {}).get("row_id")
                )
            actual = self._orch._volume_snapshot(row)
            for mutable_field in (
                "active_flag",
                "runtime_eligible",
                "runtime_eligibility_basis",
                "updated_at",
            ):
                actual[mutable_field] = snapshot.get(mutable_field)
            if actual != snapshot:
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "volume aggregate output changed",
                    status_code=409,
                )
        elif snapshot is not None:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "volume non-output product has a row",
                status_code=409,
            )
        if (
            product.get("outcome") == "no_op"
            and isinstance(product.get("result"), dict)
            and product["result"].get("status") == "created"
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "volume aggregate created result lost its output",
                status_code=409,
            )
        if product.get("outcome") == "degraded" and not isinstance(
            product.get("error_code"), str
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "volume degraded product has no error code",
                status_code=409,
            )
        return product

    @staticmethod
    def _archive_writer_evaluation_snapshot(row: WriterEvaluation) -> dict[str, Any]:
        return {
            "evaluation_id": row.evaluation_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "chapter_id": row.chapter_id,
            "scene_id": row.scene_id,
            "rubric_id": row.rubric_id,
            "source_text_ref": row.source_text_ref,
            "source_bundle_id": row.source_bundle_id,
            "evaluator_llm_call_id": row.evaluator_llm_call_id,
            "lens": row.lens,
            "parent_evaluation_id": row.parent_evaluation_id,
            "evidence_spans_json": list(row.evidence_spans_json or []),
            "source_blueprint_row_id": row.source_blueprint_row_id,
            "failure_class": row.failure_class,
            "auto_rewrite_eligible": row.auto_rewrite_eligible,
            "contract_field_refs_json": dict(row.contract_field_refs_json or {}),
            "promotion_blockers_json": list(row.promotion_blockers_json or []),
            "overall_score": row.overall_score,
            "scores_json": dict(row.scores_json or {}),
            "findings_json": list(row.findings_json or []),
            "revision_brief_json": list(row.revision_brief_json or []),
            "requires_human_review": row.requires_human_review,
            "status": row.status,
            "created_at": row.created_at,
        }

    def _run_archive_chapter_evaluation(
        self, scene: SceneCard, final_scene: FinalScene
    ) -> dict[str, Any]:
        if scene.is_chapter_last != 1:
            return self._orch._archive_product(
                scene=scene,
                kind="chapter_near_final",
                outcome="not_applicable",
                step_key="archive:chapter_near_final:0",
                input_hash=self._orch._text_hash(final_scene.content),
                reason="not_chapter_last",
                evaluation=None,
                evaluator_llm_call_id=None,
            )
        self._orch._reconcile_execution_step(
            "archive:chapter_near_final:0",
            chapter_scope=True,
        )
        evaluation_result = self._orch.near_final_service.evaluate_chapter(
            scene.chapter_id,
            execution_step_key="archive:chapter_near_final:0",
        )
        evaluation_id = evaluation_result.get("evaluation_id")
        row = self.session.get(WriterEvaluation, evaluation_id)
        if row is None:
            self._orch._raise_checkpoint_output_missing(row_id=evaluation_id)
        snapshot = self._orch._archive_writer_evaluation_snapshot(row)
        product = self._orch._archive_product(
            scene=scene,
            kind="chapter_near_final",
            outcome="evaluated",
            step_key="archive:chapter_near_final:0",
            input_hash=self._orch._json_hash(
                {
                    "chapter_product_hash": self._orch._checkpoint_hash(
                        "archive_chapter_product"
                    ),
                    "source_text_ref": row.source_text_ref,
                }
            ),
            reason=None,
            evaluation=dict(evaluation_result),
            evaluation_row=snapshot,
            evaluator_llm_call_id=row.evaluator_llm_call_id,
        )
        parent = self.session.get(LlmCall, row.evaluator_llm_call_id)
        if parent is None:
            self._orch._raise_checkpoint_output_missing(row_id=row.evaluator_llm_call_id)
        parent.response_payload_summary = sanitize_audit_summary(
            {
                **dict(parent.response_payload_summary or {}),
                "archive_chapter_near_final_product_hash": self._orch._json_hash(product),
            }
        )
        self.session.flush()
        return product

    def _validate_archive_chapter_evaluation_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._orch._archive_checkpoint_ref(
            "archive_chapter_evaluation_product"
        )
        product = self._orch._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="chapter_near_final",
            step_key="archive:chapter_near_final:0",
            outcomes={"not_applicable", "evaluated"},
        )
        if require_checkpoint_hash and self._orch._json_hash(
            product
        ) != self._orch._checkpoint_hash("archive_chapter_evaluation_product"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation product hash mismatch",
                status_code=409,
            )
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._orch._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or product.get("evaluation") is not None
                or product.get("evaluator_llm_call_id") is not None
                or final_scene is None
                or product.get("input_hash") != self._orch._text_hash(final_scene.content)
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-final scene chapter evaluation is invalid",
                    status_code=409,
                )
            return product
        snapshot = product.get("evaluation_row")
        if not isinstance(snapshot, dict) or not snapshot.get("evaluation_id"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                f"chapter evaluation product has no full row snapshot: {snapshot!r}; keys={sorted(product)!r}",
                status_code=409,
                details={"product_keys": sorted(product), "snapshot": snapshot},
            )
        row = self.session.get(WriterEvaluation, (snapshot or {}).get("evaluation_id"))
        if row is None:
            self._orch._raise_checkpoint_output_missing(
                row_id=(snapshot or {}).get("evaluation_id")
            )
        if (
            self._orch._archive_writer_evaluation_snapshot(row) != snapshot
            or row.object_type != "chapter"
            or row.object_id != scene.chapter_id
            or row.chapter_id != scene.chapter_id
            or row.scene_id is not None
            or row.evaluator_llm_call_id != product.get("evaluator_llm_call_id")
            or (product.get("evaluation") or {}).get("evaluation_id")
            != row.evaluation_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation row is detached or changed",
                status_code=409,
            )
        expected_input_hash = self._orch._json_hash(
            {
                "chapter_product_hash": self._orch._checkpoint_hash(
                    "archive_chapter_product"
                ),
                "source_text_ref": row.source_text_ref,
            }
        )
        if product.get("input_hash") != expected_input_hash:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation input hash mismatch",
                status_code=409,
            )
        parent = self.session.get(LlmCall, row.evaluator_llm_call_id)
        if parent is None:
            self._orch._raise_checkpoint_output_missing(row_id=row.evaluator_llm_call_id)
        execution_mode = (
            (parent.request_payload_summary or {}).get(ACCOUNTING_EXECUTION_MODE_KEY)
            if isinstance(parent.request_payload_summary, dict)
            else None
        )
        if execution_mode != "online":
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation parent execution mode is missing or invalid",
                status_code=409,
            )
        expected_outcome = (
            "completed"
            if parent.accounting_status == "settled"
            else (
                "rejected_before_dispatch"
                if parent.accounting_status == "rejected"
                else "provider_failed"
            )
        )
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        authoritative_project_id = chapter.project_id if chapter is not None else None
        if (
            not isinstance(authoritative_project_id, str)
            or not authoritative_project_id
            or (
                scene.project_id is not None
                and scene.project_id != authoritative_project_id
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation project ownership is inconsistent",
                status_code=409,
            )
        context = LLMCallContext(
            scope_type="chapter",
            scope_id=scene.chapter_id,
            project_id=authoritative_project_id,
            chapter_id=scene.chapter_id,
            scene_id=None,
            node_id="chapter_near_final_review",
            step="chapter_near_final_review",
            execution_id=self._orch._execution_id,
            execution_step_key="archive:chapter_near_final:0",
            run_job_id=self._orch._run_job_id,
            provider_execution_mode=execution_mode,
        )
        try:
            validate_product_call(
                self.session,
                parent.llm_call_id,
                context,
                expected_outcome=expected_outcome,
                expected_error_code=(
                    parent.error_code if expected_outcome != "completed" else None
                ),
            )
        except (LLMAccountingError, ValueError) as exc:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation parent ledger is invalid",
                status_code=409,
            ) from exc
        if (parent.response_payload_summary or {}).get(
            "archive_chapter_near_final_product_hash"
        ) != self._orch._json_hash(product):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "chapter evaluation hash is detached from parent",
                status_code=409,
            )
        return product

    def _validate_archive_drift_product(
        self,
        scene: SceneCard,
        product: dict[str, Any] | None = None,
        *,
        require_checkpoint_hash: bool = True,
    ) -> dict[str, Any]:
        product = product or self._orch._archive_checkpoint_ref("archive_drift_product")
        product = self._orch._validate_common_archive_product(
            scene=scene,
            product=product,
            kind="style_drift",
            step_key="archive:style_drift:0",
            outcomes={
                "not_applicable",
                "no_op",
                "degraded",
            },
        )
        if require_checkpoint_hash and self._orch._json_hash(
            product
        ) != self._orch._checkpoint_hash("archive_drift_product"):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style drift product hash mismatch",
                status_code=409,
            )
        if scene.is_chapter_last != 1:
            final_scene = self.session.get(
                FinalScene,
                self._orch._archive_checkpoint_ref("final_scene_row_id"),
            )
            if (
                product.get("outcome") != "not_applicable"
                or product.get("reason") != "not_chapter_last"
                or final_scene is None
                or product.get("input_hash") != self._orch._text_hash(final_scene.content)
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "non-final scene drift product is invalid",
                    status_code=409,
                )
            return product
        if product.get("outcome") == "degraded" and not isinstance(
            product.get("error_code"), str
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "style drift degraded product has no error code",
                status_code=409,
            )
        return product

    def _archive_manifest(self) -> list[dict[str, Any]]:
        entries = [
            (4, "core_archive", "archive_core"),
            (5, "rule_events", "archive_rule_product"),
            (6, "prose_extraction", "archive_prose_product"),
            (7, "vector_index", "archive_vector_product"),
            (8, "chapter_aggregate", "archive_chapter_product"),
            (9, "volume_aggregate", "archive_volume_product"),
            (10, "chapter_near_final", "archive_chapter_evaluation_product"),
            (11, "style_drift", "archive_drift_product"),
        ]
        hashes = (self._orch._active_checkpoint_state().run_checkpoint_json or {}).get(
            "artifact_hashes", {}
        )
        manifest = [
            {
                "sub_index": sub_index,
                "kind": kind,
                "hash_key": hash_key,
                "product_hash": hashes.get(hash_key),
            }
            for sub_index, kind, hash_key in entries
        ]
        if any(not isinstance(entry["product_hash"], str) for entry in manifest):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "archive manifest is incomplete",
                status_code=409,
            )
        return manifest

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
        if through >= 4:
            self._orch._validate_archive_core_checkpoint(
                scene=scene,
                final_scene=final_scene,
                carry_notes=carry_notes,
                allow_terminal=allow_terminal,
            )
        if through >= 5:
            self._orch._validate_archive_rule_events_checkpoint(scene)
        if through >= 6:
            self._orch._validate_archive_prose_checkpoint(scene, contract)
        if through >= 7:
            self._orch._validate_archive_vector_product(scene, final_scene)
        if through >= 8:
            self._orch._validate_archive_chapter_product(scene)
        if through >= 9:
            self._orch._validate_archive_volume_product(scene)
        if through >= 10:
            self._orch._validate_archive_chapter_evaluation_product(scene)
        if through >= 11:
            self._orch._validate_archive_drift_product(scene)
