from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    QcReport,
    SceneBlueprint,
    SceneCard,
    SceneExecutionContract,
    SceneRunState,
    SnowflakeArtifact,
    StoryProject,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
)
from novel_system.services.errors import DomainError
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.story_slots import (
    normalize_story_slot,
    normalize_story_slot_mapping,
)
from novel_system.services.hash_engine import canonical_json
from novel_system.services.narrative_position import NarrativePositionService

EXECUTION_CONTRACT_VERSION = "scene_execution_contract_v1"
logger = logging.getLogger(__name__)


class SceneExecutionContractService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def latest(self, scene_id: str) -> SceneExecutionContract | None:
        return self.session.execute(
            select(SceneExecutionContract)
            .where(
                SceneExecutionContract.scene_id == scene_id,
                SceneExecutionContract.status != "superseded",
            )
            .order_by(SceneExecutionContract.updated_at.desc(), SceneExecutionContract.contract_id.desc())
        ).scalars().first()

    def get_or_create(self, scene_id: str, *, actor_ref: str = "operator") -> SceneExecutionContract:
        *_, cached = self._resolve_context(scene_id)
        if cached is not None:
            return cached
        return self.generate(scene_id, actor_ref=actor_ref)

    def preview(self, scene_id: str, *, actor_ref: str = "preview") -> SceneExecutionContract:
        """Project the current contract without inserting or superseding any row."""

        scene, chapter, project, blueprint, reference_rules, snapshot_hash, cached = self._resolve_context(scene_id)
        if cached is not None:
            return cached

        payload, missing_fields, blocking_fields = self._assemble_payload(
            scene, chapter, project, blueprint, reference_rules
        )
        return SceneExecutionContract(
            contract_id=None,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            project_id=scene.project_id,
            contract_version=EXECUTION_CONTRACT_VERSION,
            source_snapshot_hash=snapshot_hash,
            payload_json=payload,
            missing_fields_json=missing_fields,
            status="active" if not blocking_fields else "blocked",
            created_by=actor_ref or "preview",
        )

    def generate(self, scene_id: str, *, actor_ref: str = "operator") -> SceneExecutionContract:
        scene, chapter, project, blueprint, reference_rules, snapshot_hash, cached = self._resolve_context(scene_id)
        if cached is not None:
            return cached

        payload, missing_fields, blocking_fields = self._assemble_payload(
            scene, chapter, project, blueprint, reference_rules
        )
        status = "active" if not blocking_fields else "blocked"

        rows = self.session.execute(
            select(SceneExecutionContract).where(
                SceneExecutionContract.scene_id == scene_id,
                SceneExecutionContract.status.in_(("active", "blocked")),
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"

        contract = SceneExecutionContract(
            contract_id=f"scene_execution_contract_{scene_id}_{uuid.uuid4().hex[:10]}",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            project_id=scene.project_id,
            contract_version=EXECUTION_CONTRACT_VERSION,
            source_snapshot_hash=snapshot_hash,
            payload_json=payload,
            missing_fields_json=missing_fields,
            status=status,
            created_by=actor_ref or "operator",
        )
        self.session.add(contract)
        self.session.flush()
        return contract

    def _resolve_context(
        self,
        scene_id: str,
    ) -> tuple[
        SceneCard,
        ChapterGoal,
        StoryProject | None,
        SceneBlueprint | None,
        dict[str, list[str]],
        str,
        SceneExecutionContract | None,
    ]:
        """解析场景快照上下文；末位返回可直接复用的最新契约（快照未变且非 stale），否则为 None。"""
        scene = self._require_scene(scene_id)
        chapter = self._require_chapter(scene.chapter_id)
        project = self.session.get(StoryProject, scene.project_id) if scene.project_id else None
        blueprint = self._latest_blueprint(scene_id)
        reference_rules = self._reference_rules(project)
        snapshot = self._source_snapshot(scene, chapter, project, blueprint, reference_rules)
        snapshot_hash = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
        latest = self.latest(scene_id)
        cached = (
            latest
            if latest is not None and latest.source_snapshot_hash == snapshot_hash and latest.status != "stale"
            else None
        )
        return scene, chapter, project, blueprint, reference_rules, snapshot_hash, cached

    def _assemble_payload(
        self,
        scene: SceneCard,
        chapter: ChapterGoal,
        project: StoryProject | None,
        blueprint: SceneBlueprint | None,
        reference_rules: dict[str, list[str]],
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """组装契约 payload，返回 (payload, missing_fields, blocking_fields)。"""
        payload, missing_fields = self._payload(scene, chapter, blueprint, reference_rules)

        blocking_fields = [f for f in missing_fields if not f.endswith("(advisory)")]
        return payload, missing_fields, blocking_fields

    @staticmethod
    def serialize(row: SceneExecutionContract | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "contract_id": row.contract_id,
            "scene_id": row.scene_id,
            "chapter_id": row.chapter_id,
            "project_id": row.project_id,
            "contract_version": row.contract_version,
            "source_snapshot_hash": row.source_snapshot_hash,
            "payload": row.payload_json or {},
            "missing_fields": list(row.missing_fields_json or []),
            "status": row.status,
            "ready_to_draft": row.status == "active",
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _payload(
        self,
        scene: SceneCard,
        chapter: ChapterGoal,
        blueprint: SceneBlueprint | None,
        reference_rules: dict[str, list[str]],
    ) -> tuple[dict[str, Any], list[str]]:
        brief = normalize_story_slot_mapping(scene.writer_brief_json or {})
        blueprint_json = dict(blueprint.blueprint_json or {}) if blueprint is not None else {}
        scene_mode = _infer_scene_mode(scene, brief)
        explicit_scene_contract = _is_explicit_structured_scene(scene, brief)
        common_payload = {
            "scene_mode": scene_mode,
            "pov_character_id": _first_text(scene.pov_character_id),
            "scene_crucible": _first_text(
                brief.get("scene_crucible"),
                blueprint_json.get("scene_crucible") if explicit_scene_contract else None,
                brief.get("conflict") if scene_mode == "proactive" else brief.get("dilemma"),
                brief.get("choice_under_pressure") if not explicit_scene_contract else None,
                blueprint_json.get("choice_under_pressure") if not explicit_scene_contract else None,
                blueprint_json.get("concrete_obstacle"),
                brief.get("obstacle"),
                scene.scene_goal if not explicit_scene_contract else None,
                chapter.main_plot_push if not explicit_scene_contract else None,
            ),
            "timebox": _first_text(brief.get("timebox"), scene.target_length_band, "single_scene"),
            "expected_reader_emotion": _first_text(
                brief.get("expected_reader_emotion"),
                brief.get("reader_aftertaste"),
                brief.get("emotional_turn"),
                chapter.emotional_target,
            ),
            "must_reveal": _first_text(brief.get("must_reveal"), brief.get("new_information"), scene.must_include_text),
            "must_withhold": _first_text(
                brief.get("must_withhold"),
                brief.get("secret_or_misunderstanding"),
                scene.forbidden_text,
            ),
            "exit_change": _first_text(scene.exit_change, brief.get("irreversible_change")),
            "next_scene_pull": _first_text(scene.hook, brief.get("reader_question"), brief.get("next_scene_pull")),
            "anti_summary_rule": _first_text(
                blueprint_json.get("anti_summary_rule"),
                "End on a visible action and do not explain the scene's meaning after the final beat.",
            ),
            "image_anchor": _first_text(
                brief.get("image_anchor"),
                blueprint_json.get("image_anchor"),
                blueprint_json.get("image_promise"),
            ),
            "relationship_turn": _first_text(
                brief.get("relationship_turn"),
                brief.get("power_shift"),
                blueprint_json.get("relationship_turn"),
                blueprint_json.get("power_shift"),
            ),
            "price_paid": _first_text(
                brief.get("price_paid"),
                brief.get("stakes"),
                blueprint_json.get("price_paid"),
                scene.exit_change,
            ),
            "cost_requirement": _first_text(
                brief.get("cost_requirement"),
                blueprint_json.get("cost_requirement"),
                brief.get("price_paid"),
                brief.get("stakes"),
                blueprint_json.get("price_paid"),
                scene.exit_change,
            ),
            "function_tag": _first_text(
                brief.get("function_tag"),
                blueprint_json.get("function_tag"),
            ),
            "tension_target": brief.get("tension_target") or blueprint_json.get("tension_target"),
            "causal_prerequisite_scene_id": _first_text(
                brief.get("causal_prerequisite_scene_id"),
                blueprint_json.get("causal_prerequisite_scene_id"),
            ),
            "downstream_obligations": brief.get("downstream_obligations") or blueprint_json.get("downstream_obligations") or [],
            "reference_rules": reference_rules,
            # Internal marker: True if scene_crucible comes from explicit spec, not fallback
            "_has_explicit_crucible": bool(
                _first_text(brief.get("scene_crucible"), blueprint_json.get("scene_crucible"))
            ),
        }
        mode_payload: dict[str, Any]
        if scene_mode == "reactive":
            mode_payload = {
                "reaction": _first_text(
                    brief.get("reaction"),
                    brief.get("emotional_turn"),
                    blueprint_json.get("emotional_turn"),
                    _beat_text(scene, 0) if not explicit_scene_contract else None,
                    scene.scene_goal if not explicit_scene_contract else None,
                ),
                "dilemma": _first_text(
                    brief.get("dilemma"),
                    brief.get("choice_under_pressure"),
                    blueprint_json.get("choice_under_pressure"),
                    blueprint_json.get("concrete_obstacle"),
                    brief.get("obstacle"),
                    _beat_text(scene, 1) if not explicit_scene_contract else None,
                ),
                "decision": _first_text(
                    brief.get("decision"),
                    brief.get("irreversible_change") if not explicit_scene_contract else None,
                    blueprint_json.get("irreversible_consequence"),
                    scene.exit_change if not explicit_scene_contract else None,
                    _last_beat(scene) if not explicit_scene_contract else None,
                    brief.get("reader_question"),
                ),
            }
        else:
            mode_payload = {
                "goal": _first_text(
                    brief.get("goal"),
                    blueprint_json.get("character_current_desire"),
                    scene.scene_goal,
                ),
                "conflict": _first_text(
                    brief.get("conflict"),
                    blueprint_json.get("concrete_obstacle"),
                    brief.get("obstacle"),
                    _beat_text(scene, 1) if not explicit_scene_contract else None,
                    scene.hook if not explicit_scene_contract else None,
                ),
                "setback_or_victory": _first_text(
                    brief.get("setback_or_victory"),
                    brief.get("setback"),
                    brief.get("victory"),
                    brief.get("irreversible_change") if not explicit_scene_contract else None,
                    blueprint_json.get("information_release"),
                    blueprint_json.get("irreversible_consequence"),
                    scene.exit_change if not explicit_scene_contract else None,
                    _last_beat(scene) if not explicit_scene_contract else None,
                    scene.hook if not explicit_scene_contract else None,
                ),
            }
        payload = {**common_payload, **mode_payload}
        missing_fields = self._missing_fields(payload)
        return payload, missing_fields

    def _missing_fields(self, payload: dict[str, Any]) -> list[str]:
        missing = []
        for f in ("scene_mode", "pov_character_id", "scene_crucible"):
            if not _has_text(payload.get(f)):
                missing.append(f)
        scene_mode = str(payload.get("scene_mode") or "proactive")
        if scene_mode == "reactive":
            for f in ("reaction", "dilemma", "decision"):
                if not _has_text(payload.get(f)):
                    missing.append(f)
        else:
            for f in ("goal", "conflict", "setback_or_victory"):
                if not _has_text(payload.get(f)):
                    missing.append(f)
        # §4 cost_requirement — blocking for scenes with explicit structured specs
        # (scene_crucible in writer_brief or blueprint), advisory for simple/legacy scenes.
        # Blueprint §4: "「代价」字段是关键 — AI 最常见的毛病是免费选择"
        if not _has_text(payload.get("cost_requirement")):
            is_explicit = payload.get("_has_explicit_crucible", False)
            missing.append("cost_requirement" if is_explicit else "cost_requirement(advisory)")
        # §10 function_tag — advisory but tracked for rhythm enforcement
        if not _has_text(payload.get("function_tag")):
            missing.append("function_tag(advisory)")
        # §10 tension_target — advisory
        if payload.get("tension_target") is None:
            missing.append("tension_target(advisory)")
        return missing

    def _latest_blueprint(self, scene_id: str) -> SceneBlueprint | None:
        return self.session.execute(
            select(SceneBlueprint)
            .where(SceneBlueprint.scene_id == scene_id, SceneBlueprint.status.in_(("accepted", "draft")))
            .order_by(SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc())
        ).scalars().first()

    def _source_snapshot(
        self,
        scene: SceneCard,
        chapter: ChapterGoal,
        project: StoryProject | None,
        blueprint: SceneBlueprint | None,
        reference_rules: dict[str, list[str]],
    ) -> dict[str, Any]:
        return {
            "scene": {
                "scene_id": scene.scene_id,
                "scene_type": scene.scene_type,
                "scene_goal": normalize_story_slot(scene.scene_goal),
                "location": normalize_story_slot(scene.location),
                "exit_change": normalize_story_slot(scene.exit_change),
                "hook": normalize_story_slot(scene.hook),
                "writer_brief_json": normalize_story_slot_mapping(
                    scene.writer_brief_json or {}
                ),
            },
            "chapter": {
                "chapter_id": chapter.chapter_id,
                "chapter_goal": normalize_story_slot(chapter.chapter_goal),
                "main_plot_push": normalize_story_slot(chapter.main_plot_push),
                "emotional_target": normalize_story_slot(chapter.emotional_target),
                "writer_brief_json": normalize_story_slot_mapping(
                    chapter.writer_brief_json or {}
                ),
            },
            "project": {
                "project_id": project.project_id if project is not None else None,
                "reference_profile_ids": self._reference_profile_ids(project) if project is not None else [],
            },
            "blueprint_json": dict(blueprint.blueprint_json or {}) if blueprint is not None else {},
            "reference_rules": reference_rules,
            "contract_version": EXECUTION_CONTRACT_VERSION,
        }


    def _reference_rules(self, project: StoryProject | None) -> dict[str, list[str]]:
        if project is None:
            return {"style_rules": [], "structure_rules": [], "safety_rules": []}
        style_profile = self._active_style_reference_profile(project.project_id)
        if style_profile is not None and style_profile.status == "active":
            return _normalize_reference_rules(style_profile.profile_json or {})
        return {"style_rules": [], "structure_rules": [], "safety_rules": []}

    def _active_style_reference_profile(self, project_id: str) -> StyleReferenceProfile | None:
        binding = self.session.execute(
            select(StyleReferenceInjectionBinding)
            .where(
                StyleReferenceInjectionBinding.scope == "project",
                StyleReferenceInjectionBinding.scope_ref_id == project_id,
                StyleReferenceInjectionBinding.task_type == "scene_generation",
                StyleReferenceInjectionBinding.status == "active",
            )
            .order_by(
                StyleReferenceInjectionBinding.created_at.desc(),
                StyleReferenceInjectionBinding.binding_id.desc(),
            )
        ).scalars().first()
        if binding is None:
            return None
        return self.session.get(StyleReferenceProfile, binding.profile_id)

    def _reference_profile_ids(self, project: StoryProject) -> list[str]:
        style_profile = self._active_style_reference_profile(project.project_id)
        if style_profile is not None:
            return [style_profile.profile_id]
        return []


    def _canonical_completed_scene_ids(
        self,
        project_id: str,
        *,
        position_service: NarrativePositionService | None = None,
    ) -> set[str]:
        """Return only scenes whose runtime pointer names the authority row.

        ``final_scenes`` is append-only history after author-draft promotion.  A
        historical/superseded row must never satisfy a causal prerequisite merely
        because it still exists.  The scene is complete only when the current
        ``SceneRunState`` pointer resolves to a ``FinalScene`` for that same scene.
        """

        positions = position_service or NarrativePositionService(self.session)
        statement = (
            positions.scene_statement(project_id)
            .join(SceneRunState, SceneRunState.scene_id == SceneCard.scene_id)
            .join(
                FinalScene,
                and_(
                    FinalScene.row_id == SceneRunState.current_final_scene_row_id,
                    FinalScene.scene_id == SceneCard.scene_id,
                ),
            )
        )
        return {
            completed_scene.scene_id
            for completed_scene in self.session.execute(statement).scalars().all()
        }

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)


def _infer_scene_mode(scene: SceneCard, brief: dict[str, Any]) -> str:
    for value in (brief.get("scene_mode"), brief.get("scene_form"), scene.scene_type):
        text = str(value or "").strip().lower()
        if text in {"reactive", "reaction"}:
            return "reactive"
        if text in {"proactive", "goal"}:
            return "proactive"
    return "reactive" if brief.get("reaction") or brief.get("decision") else "proactive"


def _is_explicit_structured_scene(scene: SceneCard, brief: dict[str, Any]) -> bool:
    for value in (brief.get("scene_mode"), brief.get("scene_form"), scene.scene_type):
        text = str(value or "").strip().lower()
        if text in {"proactive", "reactive", "reaction", "goal"}:
            return True
    return False


def _normalize_reference_rules(profile_json: dict[str, Any]) -> dict[str, list[str]]:
    style_rules = _listify(profile_json.get("style_rules"))
    structure_rules = _listify(profile_json.get("structure_rules"))
    safety_rules = _listify(profile_json.get("safety_rules"))
    if not style_rules:
        style_rules = (
            _listify(profile_json.get("style_features"))
            + _listify(profile_json.get("rhythm"))
            + _listify(profile_json.get("syntax"))
            + _listify(profile_json.get("narrative_methods"))
        )
    if not structure_rules:
        structure_rules = (
            _listify(profile_json.get("narrative_patterns"))
            + _listify(profile_json.get("calibration_guidance"))
            + _listify(profile_json.get("structure_patterns"))
            + _listify(profile_json.get("structure_techniques"))
        )
    if not safety_rules:
        safety_rules = (
            _listify(profile_json.get("banned_replication_rules"))
            + _listify(profile_json.get("forbidden_copy_rules"))
            + _listify(profile_json.get("safety_constraints"))
        )
    return {
        "style_rules": _dedupe(style_rules),
        "structure_rules": _dedupe(structure_rules),
        "safety_rules": _dedupe(safety_rules),
    }


FIELD_LABELS = {
    "scene_crucible": "坩埚/场景压力",
    "crucible": "坩埚/场景压力",
    "conflict": "冲突推进",
    "setback_or_victory": "挫折/胜负变化",
    "setback": "挫折",
    "goal": "场景目标",
    "reaction": "反应",
    "dilemma": "困境",
    "decision": "决定",
}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and normalize_story_slot(value):
            return normalize_story_slot(value)
    return ""


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _listify(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _beat_text(scene: SceneCard, index: int) -> str:
    beats = [str(item).strip() for item in list(scene.beats_json or []) if str(item).strip()]
    if index < 0 or index >= len(beats):
        return ""
    return beats[index]


def _last_beat(scene: SceneCard) -> str:
    beats = [str(item).strip() for item in list(scene.beats_json or []) if str(item).strip()]
    return beats[-1] if beats else ""


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered
