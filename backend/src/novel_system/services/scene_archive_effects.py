"""Archive-time side-effect recorders extracted from the scene orchestrator.

This module owns the post-approval archival effect cluster: narrative event
recording (rule-based + prose-grounded), vector indexing of the final scene
text, and end-of-chapter style-drift guidance. The methods here are a verbatim
move from ``Orchestrator`` — checkpoint step keys, event payload fields, and
every product/degraded return value are byte-for-byte unchanged.

Dispatch contract: cluster-internal cross-calls go through ``self._dispatch``
(the hosting ``Orchestrator`` when constructed by its delegates, else ``self``).
This preserves the long-standing test seam where suites override individual
recorder methods as instance attributes on the orchestrator and expect the
sibling methods to observe the override.

``execution_id`` / ``run_job_id`` are per-run values captured at construction
time; hosts must build a fresh ``SceneArchiveEffects`` per call rather than
caching one across runs.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import (
    LLMAccountingError,
    LLMCallContext,
    is_llm_control_plane_failure,
)
from novel_system.services.scene_ownership import require_scene_project_id

if TYPE_CHECKING:
    from novel_system.services.prose_event_extractor import ProseExtractionResult

_LOGGER = logging.getLogger(__name__)


class SceneArchiveEffects:
    def __init__(
        self,
        session: Session,
        llm_runner,
        *,
        execution_id: str | None,
        run_job_id: str | None,
        dispatch=None,
    ) -> None:
        self.session = session
        self.llm_runner = llm_runner
        self._execution_id = execution_id
        self._run_job_id = run_job_id
        # Cluster-internal cross-calls route through the host so instance-level
        # overrides on the orchestrator (a test seam) keep intercepting them.
        self._dispatch = dispatch if dispatch is not None else self

    @staticmethod
    def _text_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _json_hash(payload: Any) -> str:
        import json

        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

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
        """Extract all 7 event types from approved scene and log to event sourcing.

        Blueprint §2: event log is the single source of truth.
        """
        try:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            base_log = NarrativeEventLog(self.session)
            event_ids: list[str] = []

            class _RecordingEventLog:
                def log_event(self, **kwargs):
                    kwargs.setdefault("authority_status", "planned")
                    kwargs.setdefault("source_kind", "scene_plan")
                    kwargs.setdefault("final_scene_row_id", final_scene_row_id)
                    event = base_log.log_event(**kwargs)
                    event_ids.append(event.event_id)
                    return event

                def __getattr__(self, name: str):
                    return getattr(base_log, name)

            log = _RecordingEventLog()
            payload = contract.payload_json or {}
            project_id = self._dispatch._resolve_scene_project_id(scene, contract)
            pov = scene.pov_character_id or payload.get("pov_character_id")
            onstage = scene.onstage_chars_json or []
            all_chars = list(
                dict.fromkeys(([pov] if pov else []) + [c for c in onstage if c != pov])
            )
            base = dict(
                project_id=project_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
            )

            # --- 1. character_state: appeared_in_scene ---
            for char_id in all_chars:
                if not char_id:
                    continue
                log.log_event(
                    **base,
                    event_type="character_state",
                    entity_type="character",
                    entity_id=char_id,
                    fact_key="appeared_in_scene",
                    fact_value=scene.scene_id,
                    source_text_excerpt=content[:200] if content else None,
                )

            # --- 2. character_state: exit_change ---
            exit_change = scene.exit_change or payload.get("exit_change") or ""
            if exit_change and pov:
                log.log_event(
                    **base,
                    event_type="character_state",
                    entity_type="character",
                    entity_id=pov,
                    fact_key="exit_change",
                    fact_value=exit_change[:500],
                )

            # --- 3. location_change ---
            location = scene.location or payload.get("location")
            if location:
                for char_id in all_chars:
                    if not char_id:
                        continue
                    log.log_event(
                        **base,
                        event_type="location_change",
                        entity_type="character",
                        entity_id=char_id,
                        fact_key="location",
                        fact_value=location[:200],
                    )

            # --- 4. character_learns: from writer_brief must_reveal ---
            writer_brief = scene.writer_brief_json or {}
            must_reveal = writer_brief.get("must_reveal")
            if must_reveal and pov:
                reveal_text = (
                    must_reveal if isinstance(must_reveal, str) else str(must_reveal)
                )
                log.log_event(
                    **base,
                    event_type="character_learns",
                    entity_type="character",
                    entity_id=pov,
                    fact_key="scene_revelation",
                    fact_value=reveal_text[:500],
                )

            # --- 5. relation_change: from scene blueprint relationship_turn ---
            self._dispatch._record_relation_events(log, scene, base, pov, all_chars)

            # --- 7. (opt-in) prose-grounded events: what the TEXT actually realized,
            # not just what the spec planned. Advisory (confidence="extracted"). ---
            if include_prose:
                self._dispatch._record_prose_events(log, scene, base, content)

            self.session.flush()
            return event_ids
        except Exception as exc:
            if is_llm_control_plane_failure(exc) or isinstance(exc, LLMAccountingError):
                raise
            if not degrade_errors:
                raise
            _LOGGER.warning(
                "narrative event recording degraded for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return []

    def _resolve_scene_project_id(self, scene: SceneCard, contract=None) -> str:
        """Resolve project ownership exclusively from relational authority."""
        payload = getattr(contract, "payload_json", None) or {}
        if not isinstance(payload, dict):
            payload = {}
        explicit_project_id = payload.get("project_id")
        return require_scene_project_id(
            self.session,
            scene,
            explicit_project_id=(
                explicit_project_id if isinstance(explicit_project_id, str) else None
            ),
        )

    def _archive_event_base(self, scene: SceneCard, contract) -> dict[str, str]:
        project_id = self._dispatch._resolve_scene_project_id(scene, contract)
        return {
            "project_id": str(project_id),
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
        }

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
        """§2 (opt-in): extract events from the ACTUAL generated prose so model drift away
        from the spec is captured. Tagged confidence="extracted" + source="prose" → advisory
        only, never a hard consistency blocker (blueprint §15 honest-bounds). Returns an
        explicit no-call/degraded/completed product; accounting and control-plane integrity
        failures propagate."""
        from novel_system.services.prose_event_extractor import (
            ProseExtractionResult,
            extract_events_from_prose,
        )
        from novel_system.settings import get_settings

        settings = get_settings()
        extract_step_key = "archive:prose_event_extract:0"
        if not (
            settings.llm_enabled
            and getattr(settings, "llm_event_extraction_enabled", False)
        ):
            result = ProseExtractionResult(
                outcome="not_invoked",
                execution_id=self._execution_id,
                execution_step_key=(
                    extract_step_key if self._execution_id is not None else None
                ),
                run_job_id=self._run_job_id,
                reason="feature_disabled",
            )
            return (result, []) if return_event_ids else result
        if not (content and content.strip()):
            result = ProseExtractionResult(
                outcome="not_invoked",
                execution_id=self._execution_id,
                execution_step_key=(
                    extract_step_key if self._execution_id is not None else None
                ),
                run_job_id=self._run_job_id,
                reason="empty_content",
            )
            return (result, []) if return_event_ids else result
        extract_context = LLMCallContext(
            scope_type="scene",
            scope_id=str(base.get("scene_id") or getattr(scene, "scene_id", "")),
            project_id=str(base.get("project_id") or getattr(scene, "project_id", ""))
            or None,
            chapter_id=str(base.get("chapter_id") or getattr(scene, "chapter_id", ""))
            or None,
            scene_id=str(base.get("scene_id") or getattr(scene, "scene_id", ""))
            or None,
            node_id="extraction",
            step=extract_step_key,
            execution_id=self._execution_id,
            execution_step_key=(
                extract_step_key if self._execution_id is not None else None
            ),
            run_job_id=self._run_job_id,
            provider_execution_mode=getattr(
                self.llm_runner,
                "provider_execution_mode",
                "online",
            ),
        )
        result = extract_events_from_prose(
            content,
            session=self.session,
            llm_runner=self.llm_runner,
            llm_context=extract_context,
        )
        event_ids: list[str] = []
        for ordinal, ev in enumerate(result.events):
            event = log.log_event(
                **base,
                event_type=ev.event_type,
                entity_type=(
                    "relation" if ev.event_type == "relation_change" else "character"
                ),
                entity_id=ev.entity_id,
                fact_key=ev.fact_key,
                fact_value=ev.fact_value,
                confidence="extracted",
                # Missing extractor evidence stays missing. Substituting an
                # arbitrary prose prefix would let an unsupported fact appear
                # grounded during canon review.
                source_text_excerpt=ev.evidence or None,
                authority_status="pending",
                source_kind="prose_extraction",
                final_scene_row_id=final_scene_row_id,
                payload={
                    "source": "prose",
                    "archive_execution_id": self._execution_id,
                    "archive_step_key": extract_step_key,
                    "archive_ordinal": ordinal,
                },
            )
            event_ids.append(event.event_id)
        return (result, event_ids) if return_event_ids else result

    def _record_relation_events(
        self,
        log,
        scene: SceneCard,
        base: dict,
        pov: str | None,
        all_chars: list[str],
    ) -> None:
        """Extract relation_change events from scene blueprint and writer brief."""
        blueprint = (
            self.session.execute(
                select(SceneBlueprint)
                .where(
                    SceneBlueprint.scene_id == scene.scene_id,
                    SceneBlueprint.status.in_(("accepted", "draft")),
                )
                .order_by(SceneBlueprint.created_at.desc())
            )
            .scalars()
            .first()
        )
        relationship_turn = None
        if blueprint and blueprint.blueprint_json:
            relationship_turn = blueprint.blueprint_json.get("relationship_turn")
        if not relationship_turn:
            relationship_turn = (scene.writer_brief_json or {}).get("relationship_turn")
        if relationship_turn and pov and len(all_chars) >= 2:
            other = next((c for c in all_chars if c != pov), pov)
            log.log_event(
                **base,
                event_type="relation_change",
                entity_type="relation",
                entity_id=f"{pov}--{other}",
                fact_key="relationship_turn",
                fact_value=str(relationship_turn)[:500],
            )


    def _detect_and_store_style_drift(self, scene: SceneCard) -> dict[str, Any]:
        """Style-drift guidance was retired; the archive checkpoint keeps a no-op product."""
        return {"outcome": "no_op", "reason": "style_drift_retired"}

    @staticmethod
    def _index_scene_to_vector_store(
        scene: SceneCard,
        content: str,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        from novel_system.services.vector_store import get_vector_store
        from novel_system.settings import get_settings

        backend = get_settings().vector_backend.lower()
        validation_scope = "process_local" if backend == "memory" else "persistent"
        resolved_project_id = project_id or scene.project_id
        if not resolved_project_id:
            raise DomainError(
                "PROJECT_OWNERSHIP_UNRESOLVED",
                "scene vector indexing requires authoritative project ownership",
                status_code=409,
                details={"scene_id": scene.scene_id, "chapter_id": scene.chapter_id},
            )
        if project_id and scene.project_id and project_id != scene.project_id:
            raise DomainError(
                "PROJECT_OWNERSHIP_CONFLICT",
                "scene vector indexing project disagrees with scene ownership",
                status_code=409,
                details={
                    "scene_id": scene.scene_id,
                    "scene_project_id": scene.project_id,
                    "explicit_project_id": project_id,
                },
            )
        collection_name = f"scenes_{resolved_project_id}"
        expected_text = (content or "")[:600]
        text_hash = SceneArchiveEffects._text_hash(expected_text)
        base = {
            "backend": backend,
            "validation_scope": validation_scope,
            "collection_name": collection_name,
            "vector_id": scene.scene_id,
            "text_hash": text_hash,
        }
        try:
            store = get_vector_store()
            existing = (
                store.load_collection(collection_name)
                if store.collection_exists(collection_name)
                else []
            )
            matches = [row for row in existing if row.get("id") == scene.scene_id]
            if len(matches) > 1:
                return {
                    **base,
                    "outcome": "failed",
                    "write_status": "failed",
                    "error_code": "VECTOR_INDEX_DUPLICATE_ID",
                }
            if matches:
                if str(matches[0].get("text") or "") != expected_text:
                    return {
                        **base,
                        "outcome": "failed",
                        "write_status": "failed",
                        "error_code": "VECTOR_INDEX_STALE_CONTENT",
                    }
                return {
                    **base,
                    "outcome": (
                        "non_persistent" if backend == "memory" else "already_present"
                    ),
                    "write_status": "already_present",
                    "error_code": None,
                }
            store.write_collection(
                collection_name,
                [*existing, {"id": scene.scene_id, "text": expected_text}],
            )
            written = store.load_collection(collection_name)
            matches = [row for row in written if row.get("id") == scene.scene_id]
            if len(matches) != 1 or str(matches[0].get("text") or "") != expected_text:
                raise RuntimeError("vector write verification failed")
            return {
                **base,
                "outcome": ("non_persistent" if backend == "memory" else "indexed"),
                "write_status": "indexed",
                "error_code": None,
            }
        except Exception as exc:
            _LOGGER.warning(
                "vector store indexing degraded for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return {
                **base,
                "outcome": "failed",
                "write_status": "failed",
                "error_code": exc.__class__.__name__,
            }


