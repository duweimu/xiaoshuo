from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.contracts.bundle import BundleSnapshotHashProjection
from novel_system.db.models import (
    AuthorPreferenceProfile,
    ChapterGoal,
    FinalScene,
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
    StoryCharacter,
    VolumeSummary,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import compute_bundle_hash_projection
from novel_system.services.literary_quality import fingerprint_literary_quality
from novel_system.services.resolver import Resolver
from novel_system.services.character_continuity import (
    CHARACTER_CONTRACT_VERSION,
    build_character_contract_digest,
)
from novel_system.services.scene_digest import scene_card_digest
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    build_style_runtime_contract,
)
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
    writer_brief_has_content,
)
from novel_system.services.author_preferences import (
    merge_preference_summaries,
    safe_preference_summary_for_prompt,
)
from novel_system.services.author_instructions import normalize_author_note


_LOGGER = logging.getLogger(__name__)


class BundleBuilder:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.resolver = Resolver()
        # 审计 P-11：可选注入槽的降级不允许静默——WARNING 落日志并随快照暴露。
        self._degraded_slots: set[str] = set()

    def _slot_degraded(self, slot: str, scene: SceneCard | None = None) -> None:
        """记录一个可选注入槽的降级（在 except 块内调用，exc_info 取当前异常）。"""
        self._degraded_slots.add(slot)
        _LOGGER.warning(
            "bundle slot %s degraded for scene %s",
            slot,
            getattr(scene, "scene_id", "?"),
            exc_info=True,
        )

    @staticmethod
    def _single_or_list(values: list[str]) -> str | list[str]:
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _combined_text(rows: list[Any], text_field: str) -> str:
        return "\n\n".join(
            str(getattr(row, text_field))
            for row in rows
            if getattr(row, text_field, None)
        )

    def _next_bundle_id(self, scene_id: str, state: SceneRunState) -> tuple[str, int]:
        build_no = (state.bundle_build_count or 0) + 1
        while True:
            bundle_id = f"bundle_{scene_id}_v{build_no}"
            if self.session.get(SceneBundle, bundle_id) is None:
                return bundle_id, build_no
            build_no += 1

    def build(
        self,
        scene_id: str,
        execution_mode: str = "P2",
        force_rebuild: bool = False,
        *,
        author_note: str | None = None,
    ) -> dict[str, Any]:
        self._degraded_slots = set()
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
        state = self.session.get(SceneRunState, scene_id)
        previous_memory = (
            self.session.execute(
                select(SceneMemory)
                .join(SceneCard, SceneCard.scene_id == SceneMemory.scene_id)
                .where(
                    SceneMemory.chapter_id == scene.chapter_id,
                    SceneMemory.active_flag == 1,
                    SceneMemory.runtime_eligible == 1,
                    SceneCard.trashed_flag == 0,
                    SceneCard.scene_seq < scene.scene_seq,
                )
                .order_by(SceneCard.scene_seq.desc(), SceneMemory.created_at.desc())
            )
            .scalars()
            .first()
        )

        source_version_refs = {
            "chapter_goal": chapter.chapter_id,
            "scene_card": scene.scene_id,
        }
        style_injection = InjectionService(self.session)
        style_character_ids = list(
            dict.fromkeys(
                value
                for value in [
                    scene.pov_character_id,
                    *(scene.onstage_chars_json or []),
                ]
                if value
            )
        )
        reference_resolution_degraded = False
        try:
            reference_layers = style_injection.resolve_binding_layers(
                scene.project_id,
                "scene_generation",
                character_ids=style_character_ids,
                scene_id=scene.scene_id,
            )
        except Exception:  # noqa: BLE001 — optional style layer degrades visibly
            reference_layers = []
            reference_resolution_degraded = True
            self._slot_degraded("style_reference_binding_resolution", scene)
        reference_profile_ids = list(
            dict.fromkeys(layer.profile_id for layer in reference_layers)
        )
        if reference_profile_ids:
            # 来源画像必须进入冻结 bundle 的版本引用：归档/回放时据此加载动态
            # protected_terms / scene_bridges，不能只在 prompt 注入侧短暂可见。
            source_version_refs["reference_profile_ids"] = reference_profile_ids
        ordered_injections = [
            {
                "slot": "chapter_goal",
                "ref_id": chapter.chapter_id,
                "digest_key": "chapter_goal",
            },
            {
                "slot": "scene_card",
                "ref_id": scene.scene_id,
                "digest_key": "scene_card",
            },
        ]
        inline_digests = {
            "chapter_goal": chapter.chapter_goal,
            "scene_card": scene_card_digest(scene),
        }
        source_version_refs["style_reference_runtime_contract_version"] = (
            STYLE_RUNTIME_CONTRACT_VERSION
        )
        source_version_refs["style_reference_runtime_contract_status"] = (
            "degraded"
            if reference_resolution_degraded
            else ("frozen" if reference_layers else "absent")
        )
        if reference_layers:
            try:
                style_runtime_contract = build_style_runtime_contract(
                    style_injection.repo,
                    reference_layers,
                    task_type="scene_generation",
                )
                if style_runtime_contract is not None:
                    source_version_refs["style_reference_runtime_contract_hash"] = (
                        style_runtime_contract["contract_hash"]
                    )
                    source_version_refs["reference_binding_ids"] = (
                        style_runtime_contract["binding_ids"]
                    )
                    inline_digests["_style_reference_runtime_contract"] = json.dumps(
                        style_runtime_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
            except Exception:  # noqa: BLE001 — optional style layer degrades visibly
                source_version_refs["style_reference_runtime_contract_status"] = (
                    "degraded"
                )
                self._slot_degraded("style_reference_runtime_contract", scene)
        normalized_author_note = normalize_author_note(author_note)
        if normalized_author_note:
            instruction_hash = hashlib.sha256(
                normalized_author_note.encode("utf-8")
            ).hexdigest()
            source_version_refs["author_instruction_hash"] = instruction_hash
            ordered_injections.append(
                {
                    "slot": "author_instruction",
                    "ref_id": f"author_instruction:{instruction_hash}",
                    "digest_key": "author_instruction",
                }
            )
            inline_digests["author_instruction"] = normalized_author_note
        chapter_writer_brief = normalize_chapter_writer_brief(chapter.writer_brief_json)
        if writer_brief_has_content(chapter_writer_brief):
            source_version_refs["chapter_writer_brief"] = chapter.chapter_id
            ordered_injections.append(
                {
                    "slot": "chapter_writer_brief",
                    "ref_id": chapter.chapter_id,
                    "digest_key": "chapter_writer_brief",
                }
            )
            inline_digests["chapter_writer_brief"] = json.dumps(
                chapter_writer_brief,
                ensure_ascii=False,
                sort_keys=True,
            )
        scene_writer_brief = normalize_scene_writer_brief(scene.writer_brief_json)
        if writer_brief_has_content(scene_writer_brief):
            source_version_refs["scene_writer_brief"] = scene.scene_id
            ordered_injections.append(
                {
                    "slot": "scene_writer_brief",
                    "ref_id": scene.scene_id,
                    "digest_key": "scene_writer_brief",
                }
            )
            inline_digests["scene_writer_brief"] = json.dumps(
                scene_writer_brief,
                ensure_ascii=False,
                sort_keys=True,
            )

        scene_blueprint = (
            self.session.execute(
                select(SceneBlueprint)
                .where(
                    SceneBlueprint.scene_id == scene.scene_id,
                    SceneBlueprint.status.in_(("accepted", "draft")),
                )
                .order_by(
                    SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc()
                )
            )
            .scalars()
            .first()
        )
        if scene_blueprint is not None:
            source_version_refs["scene_blueprint_row_id"] = scene_blueprint.row_id
            ordered_injections.append(
                {
                    "slot": "scene_blueprint",
                    "ref_id": scene_blueprint.row_id,
                    "digest_key": "scene_blueprint",
                }
            )
            inline_digests["scene_blueprint"] = json.dumps(
                scene_blueprint.blueprint_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        character_pressure = self._latest_planning_artifact(
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id=scene.scene_id,
        )
        if character_pressure is not None:
            source_version_refs["character_pressure_artifact_row_id"] = (
                character_pressure.row_id
            )
            ordered_injections.append(
                {
                    "slot": "character_pressure",
                    "ref_id": character_pressure.row_id,
                    "digest_key": "character_pressure",
                }
            )
            inline_digests["character_pressure"] = json.dumps(
                character_pressure.payload_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        chapter_architecture = self._latest_planning_artifact(
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=scene.chapter_id,
        )
        if chapter_architecture is not None:
            source_version_refs["chapter_story_architecture_artifact_row_id"] = (
                chapter_architecture.row_id
            )
            ordered_injections.append(
                {
                    "slot": "chapter_story_architecture",
                    "ref_id": chapter_architecture.row_id,
                    "digest_key": "chapter_story_architecture",
                }
            )
            inline_digests["chapter_story_architecture"] = json.dumps(
                chapter_architecture.payload_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        voice_profile_id = self.resolver.resolve_voice_profile_id(scene)
        voice_profile = self.resolver.resolve_active_voice_profile(self.session, scene)
        if voice_profile_id and voice_profile is None:
            raise DomainError(
                "BUNDLE_SOURCE_MISSING",
                f"active voice profile missing for {voice_profile_id}",
                status_code=409,
            )
        if voice_profile:
            source_version_refs["voice_profile_id"] = voice_profile.voice_profile_id
            source_version_refs["voice_profile_row_id"] = voice_profile.row_id
            source_version_refs["voice_profile_version"] = voice_profile.version
            ordered_injections.append(
                {
                    "slot": "pov_voice",
                    "ref_id": voice_profile.voice_profile_id,
                    "digest_key": "voice_card",
                }
            )
            inline_digests["voice_card"] = voice_profile.content

        relation_profile_id = self.resolver.resolve_relation_profile_id(scene)
        relation_profile = self.resolver.resolve_active_relation_profile(
            self.session, scene
        )
        if relation_profile_id and relation_profile is None:
            raise DomainError(
                "BUNDLE_SOURCE_MISSING",
                f"active relation profile missing for {relation_profile_id}",
                status_code=409,
            )
        if relation_profile:
            source_version_refs["relation_profile_id"] = (
                relation_profile.relation_profile_id
            )
            source_version_refs["relation_profile_row_id"] = relation_profile.row_id
            source_version_refs["relation_profile_version"] = relation_profile.version
            ordered_injections.append(
                {
                    "slot": "relation",
                    "ref_id": relation_profile.relation_profile_id,
                    "digest_key": "relation_card",
                }
            )
            inline_digests["relation_card"] = relation_profile.content

        # 解析 pov/onstage 的权威 display_name（StoryCharacter），避免裸 id 进提示词当人名
        contract_char_ids = [
            cid
            for cid in [scene.pov_character_id, *(scene.onstage_chars_json or [])]
            if cid
        ]
        character_display_names: dict[str, str] = {}
        if contract_char_ids:
            for row in (
                self.session.execute(
                    select(StoryCharacter).where(
                        StoryCharacter.character_id.in_(contract_char_ids)
                    )
                )
                .scalars()
                .all()
            ):
                if row.display_name:
                    character_display_names[row.character_id] = row.display_name
        character_contract = build_character_contract_digest(
            pov_character_id=scene.pov_character_id,
            onstage_character_ids=scene.onstage_chars_json,
            voice_profile_content=voice_profile.content if voice_profile else None,
            relation_profile_content=(
                relation_profile.content if relation_profile else None
            ),
            display_names=character_display_names,
        )
        if character_contract:
            source_version_refs["character_contract"] = CHARACTER_CONTRACT_VERSION
            ordered_injections.append(
                {
                    "slot": "character_contract",
                    "ref_id": CHARACTER_CONTRACT_VERSION,
                    "digest_key": "character_contract",
                }
            )
            inline_digests["character_contract"] = character_contract

        narrative_state = self._narrative_state_digest(scene)
        if narrative_state:
            inline_digests["narrative_state"] = narrative_state

        info_asymmetry = self._information_asymmetry_digest(scene)
        if info_asymmetry:
            inline_digests["information_asymmetry"] = info_asymmetry

        chapter_transition = self._chapter_transition_buffer(scene)
        if chapter_transition:
            inline_digests["chapter_transition_buffer"] = chapter_transition

        similar_scenes = self._similar_scene_context(scene)
        if similar_scenes:
            inline_digests["similar_scene"] = similar_scenes

        if previous_memory:
            source_version_refs["scene_memory_prev"] = previous_memory.scene_id
            ordered_injections.append(
                {
                    "slot": "prev_scene_memory",
                    "ref_id": previous_memory.scene_id,
                    "digest_key": "scene_memory",
                }
            )
            inline_digests["scene_memory"] = previous_memory.content

        freshness_budget = self._literary_freshness_budget(scene)
        if freshness_budget is not None:
            source_version_refs["literary_freshness_source_final_scene_ids"] = (
                freshness_budget["source_final_scene_ids"]
            )
            ordered_injections.append(
                {
                    "slot": "literary_freshness_budget",
                    "ref_id": scene.chapter_id,
                    "digest_key": "literary_freshness_budget",
                }
            )
            inline_digests["literary_freshness_budget"] = json.dumps(
                freshness_budget["budget"],
                ensure_ascii=False,
                sort_keys=True,
            )

        author_preference_profiles = self._approved_runtime_author_preference_profiles(
            scene, chapter
        )
        if author_preference_profiles:
            author_preference_profile = author_preference_profiles[-1]
            merged_preference: dict[str, Any] = {}
            for row in author_preference_profiles:
                merged_preference = merge_preference_summaries(
                    merged_preference, row.summary_json or {}
                )
            runtime_preference = safe_preference_summary_for_prompt(merged_preference)
            profile_ids = [row.profile_id for row in author_preference_profiles]
            source_version_refs["author_preference_profile_id"] = (
                author_preference_profile.profile_id
            )
            source_version_refs["author_preference_profile_ids"] = profile_ids
            source_version_refs["author_preference_profile_updated_at"] = (
                author_preference_profile.updated_at
            )
            ordered_injections.append(
                {
                    "slot": "author_preference_profile",
                    "ref_id": author_preference_profile.profile_id,
                    "digest_key": "author_preference_profile",
                }
            )
            inline_digests["author_preference_profile"] = json.dumps(
                {
                    "profile_id": author_preference_profile.profile_id,
                    "profile_ids": profile_ids,
                    "kind": "approved_author_preference_profile",
                    "summary": runtime_preference,
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        scene_summary = self.resolver.resolve_scene_summary(self.session, scene)
        if scene_summary:
            source_version_refs["scene_summary_id"] = scene_summary.scene_id
            ordered_injections.append(
                {
                    "slot": "scene_summary",
                    "ref_id": scene_summary.scene_id,
                    "digest_key": "scene_summary",
                }
            )
            inline_digests["scene_summary"] = scene_summary.content

        chapter_summary = self.resolver.resolve_chapter_summary(self.session, scene)
        if chapter_summary:
            source_version_refs["chapter_summary_id"] = chapter_summary.chapter_id
            ordered_injections.append(
                {
                    "slot": "chapter_summary",
                    "ref_id": chapter_summary.chapter_id,
                    "digest_key": "chapter_summary",
                }
            )
            inline_digests["chapter_summary"] = chapter_summary.content

        # §2 summary tower: far-horizon volume atmosphere (read-only, NOT a fact source)
        volume_summary = self._latest_volume_summary(scene)
        if volume_summary is not None:
            source_version_refs["volume_summary_row_id"] = volume_summary.row_id
            ordered_injections.append(
                {
                    "slot": "volume_summary",
                    "ref_id": volume_summary.row_id,
                    "digest_key": "volume_summary",
                }
            )
            inline_digests["volume_summary"] = (
                "【卷级远景氛围 — 仅供语气/基调延续，严禁当作事实来源；事实一律以权威状态为准】\n"
                + (volume_summary.atmosphere_summary or "")
            )

        projection = BundleSnapshotHashProjection(
            contract_version="BSHASH_v1",
            stage_allowlist_name="bundle_build_allowlist_v1",
            source_version_refs=source_version_refs,
            resolved_ref_ids={
                "relation_ids": (
                    [relation_profile.relation_profile_id] if relation_profile else []
                ),
            },
            ordered_injections=ordered_injections,
            inline_digests=inline_digests,
        )
        bundle_hash = compute_bundle_hash_projection(projection)
        bundle_id, build_count = self._next_bundle_id(scene.scene_id, state)
        snapshot = projection.model_dump(mode="json")
        snapshot["scene_id"] = scene.scene_id
        snapshot["chapter_id"] = scene.chapter_id
        # 审计 P-11：降级槽位随快照可见（hash 之后追加——不参与 bundle_snapshot_hash，
        # 与 scene_id/chapter_id 同一约定）。
        if self._degraded_slots:
            snapshot["degraded_slots"] = sorted(self._degraded_slots)

        bundle = SceneBundle(
            bundle_id=bundle_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            execution_mode=execution_mode,
            bundle_snapshot_hash=bundle_hash,
            frozen_snapshot_json=snapshot,
        )
        self.session.add(bundle)

        state.current_bundle_id = bundle_id
        state.current_bundle_hash = bundle_hash
        state.bundle_build_count = build_count
        state.scene_status = "bundle_built"
        self.session.flush()

        return {
            "bundle_id": bundle_id,
            "bundle_snapshot_hash": bundle_hash,
            "snapshot": snapshot,
        }

    def _latest_planning_artifact(
        self,
        *,
        artifact_type: str,
        object_type: str,
        object_id: str,
    ) -> GenerationPlanningArtifact | None:
        return (
            self.session.execute(
                select(GenerationPlanningArtifact)
                .where(
                    GenerationPlanningArtifact.artifact_type == artifact_type,
                    GenerationPlanningArtifact.object_type == object_type,
                    GenerationPlanningArtifact.object_id == object_id,
                    GenerationPlanningArtifact.status == "active",
                )
                .order_by(
                    GenerationPlanningArtifact.created_at.desc(),
                    GenerationPlanningArtifact.row_id.desc(),
                )
            )
            .scalars()
            .first()
        )


    def _narrative_state_digest(self, scene: SceneCard) -> str | None:
        """Inject authoritative character state from event log into the prompt."""
        try:
            from novel_system.services.canon_continuity import CanonContinuityService
            from novel_system.services.narrative_event_log import NarrativeEventLog

            log = NarrativeEventLog(self.session)
            project_id = require_scene_project_id(self.session, scene)
            # Wave 4（§5.6）：传 pov_character_id → format_state_for_prompt 委派
            # PovKnowledgeProjection 做减法投影，隐藏非 POV 秘密内容（硬 QC 仍读全量）。
            text = log.format_state_for_prompt(
                project_id,
                None,
                scene_id=scene.scene_id,
                pov_character_id=scene.pov_character_id,
                onstage_character_ids=scene.onstage_chars_json,
            )
            checkpoint = CanonContinuityService(
                self.session
            ).format_recent_checkpoint_for_prompt(
                project_id,
                scene.scene_id,
                pov_character_id=scene.pov_character_id,
            )
            parts = [part for part in (text, checkpoint) if part]
            return "\n\n".join(parts) if parts else None
        except Exception:
            self._slot_degraded("narrative_state", scene)
            return None

    def _literary_freshness_budget(self, scene: SceneCard) -> dict[str, Any] | None:
        rows = (
            self.session.execute(
                select(FinalScene)
                .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                .where(
                    FinalScene.chapter_id == scene.chapter_id,
                    # Wave 1 词表统一：archived 是归档事务写入的权威成稿态，必须与旧值并列
                    FinalScene.status.in_(("approved", "near_final_ready", "archived")),
                    SceneCard.trashed_flag == 0,
                    SceneCard.scene_seq < scene.scene_seq,
                )
                .order_by(
                    SceneCard.scene_seq.asc(),
                    FinalScene.created_at.asc(),
                    FinalScene.row_id.asc(),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None

        source_rows = rows[-3:]
        combined_text = "\n".join(row.content or "" for row in source_rows)
        fingerprint = fingerprint_literary_quality(combined_text)
        action_templates = [
            row["value"]
            for row in fingerprint.get("action_templates", [])
            if int(row.get("count") or 0) >= 2
        ]
        image_fields = [
            row["value"]
            for row in fingerprint.get("image_fields", [])
            if int(row.get("count") or 0) >= 2
        ]
        syntax_shapes = [
            row["value"]
            for row in fingerprint.get("syntax_shapes", [])
            if int(row.get("count") or 0) >= 3
        ]
        budget = {
            "schema_version": "literary_freshness_budget_v1",
            "source_scene_ids": [row.scene_id for row in source_rows],
            "avoid_action_templates": action_templates,
            "avoid_image_fields": image_fields[:6],
            "vary_syntax_shapes": syntax_shapes[:5],
            "avoid_false_clarity": ["她知道", "他知道", "忽然意识到", "突然意识到"],
            "avoid_summary_endings": [
                "这意味着",
                "一切都变了",
                "事情从此不同",
                "解释了一切",
            ],
            "instruction": (
                "Use this as a freshness budget: do not repeat high-frequency action templates, "
                "rotate image fields, and end on a hard action instead of explanation."
            ),
        }
        try:
            from novel_system.services.self_repetition import (
                SelfRepetitionDetector,
                format_semantic_repetition_guidance,
            )

            detector = SelfRepetitionDetector(self.session)
            repeated_ngrams = detector.top_repeated_ngrams(
                scene.chapter_id, lookback_scenes=6, top_n=8
            )
            if repeated_ngrams:
                budget["avoid_recent_ngrams"] = repeated_ngrams
            corpus_texts, corpus_ids = detector._load_corpus(
                scene.scene_id, scene.chapter_id, lookback_scenes=6
            )
            if corpus_texts:
                from novel_system.services.self_repetition import (
                    check_semantic_repetition,
                )

                sem_hits = check_semantic_repetition(
                    scene.scene_goal or scene.hook or "",
                    corpus_texts,
                    corpus_ids,
                )
                if sem_hits:
                    budget["semantic_repetition_alert"] = (
                        format_semantic_repetition_guidance(sem_hits)
                    )
            # §9 blueprint: whole-book banned expression list (LifetimeExpressionRegistry)
            from novel_system.services.self_repetition import LifetimeExpressionRegistry

            lifetime_reg = LifetimeExpressionRegistry(self.session)
            lifetime_guidance = lifetime_reg.get_lifetime_avoidance_guidance(
                scene.project_id
            )
            if lifetime_guidance:
                budget["lifetime_banned_expressions"] = lifetime_guidance
        except Exception:
            self._slot_degraded("literary_freshness_enrichment", scene)
        return {
            "source_final_scene_ids": [row.row_id for row in source_rows],
            "budget": budget,
        }

    def _latest_volume_summary(self, scene: SceneCard) -> VolumeSummary | None:
        """§2: most recent active volume atmosphere summary for the scene's project."""
        project_id = scene.project_id
        if not project_id:
            return None
        return (
            self.session.execute(
                select(VolumeSummary)
                .where(
                    VolumeSummary.project_id == project_id,
                    VolumeSummary.active_flag == 1,
                    VolumeSummary.runtime_eligible == 1,
                )
                .order_by(VolumeSummary.volume_seq.desc(), VolumeSummary.row_id.desc())
            )
            .scalars()
            .first()
        )

    def _approved_runtime_author_preference_profiles(
        self,
        scene: SceneCard,
        chapter: ChapterGoal,
    ) -> list[AuthorPreferenceProfile]:
        project_id = scene.project_id or chapter.project_id
        project = self.session.get(StoryProject, project_id) if project_id else None
        scopes: list[tuple[str, str]] = [("global", "global")]
        genre = (
            " ".join(str(project.genre or "").strip().lower().split())
            if project
            else ""
        )
        if genre:
            scopes.append(("genre", genre[:120]))
        if project_id:
            scopes.append(("project", project_id))
        scopes.append(("chapter", chapter.chapter_id))
        rows: list[AuthorPreferenceProfile] = []
        for scope_type, scope_ref_id in scopes:
            rows.extend(
                self.session.execute(
                    select(AuthorPreferenceProfile)
                    .where(
                        AuthorPreferenceProfile.scope_type == scope_type,
                        AuthorPreferenceProfile.scope_ref_id == scope_ref_id,
                        AuthorPreferenceProfile.status == "approved",
                        AuthorPreferenceProfile.runtime_eligible == 1,
                    )
                    .order_by(
                        AuthorPreferenceProfile.updated_at.asc(),
                        AuthorPreferenceProfile.profile_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
        return rows


    def _chapter_transition_buffer(self, scene: SceneCard) -> str | None:
        """Blueprint §3: inject last 500-1000 chars of previous chapter as continuity anchor.

        Only fires for the first scene of a chapter (scene_seq == 1).
        Uses ChapterGoal.display_order (or chapter_id alphabetical) to find the previous chapter.
        """
        try:
            if scene.scene_seq and scene.scene_seq > 1:
                return None
            current_chapter = self.session.get(ChapterGoal, scene.chapter_id)
            if current_chapter is None:
                return None
            current_order = current_chapter.display_order
            if current_order is not None:
                prev_chapter = (
                    self.session.execute(
                        select(ChapterGoal)
                        .where(
                            ChapterGoal.project_id == scene.project_id,
                            ChapterGoal.display_order < current_order,
                        )
                        .order_by(ChapterGoal.display_order.desc())
                    )
                    .scalars()
                    .first()
                )
            else:
                prev_chapter = (
                    self.session.execute(
                        select(ChapterGoal)
                        .where(
                            ChapterGoal.project_id == scene.project_id,
                            ChapterGoal.chapter_id < scene.chapter_id,
                        )
                        .order_by(ChapterGoal.chapter_id.desc())
                    )
                    .scalars()
                    .first()
                )
            if prev_chapter is None:
                return None
            last_final = (
                self.session.execute(
                    select(FinalScene)
                    .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                    .where(
                        FinalScene.chapter_id == prev_chapter.chapter_id,
                        FinalScene.status.in_(
                            ("approved", "near_final_ready", "archived")
                        ),
                        SceneCard.trashed_flag == 0,
                    )
                    .order_by(SceneCard.scene_seq.desc(), FinalScene.created_at.desc())
                )
                .scalars()
                .first()
            )
            if last_final and last_final.content:
                tail = last_final.content[-800:]
                return f"## Chapter Transition Buffer (previous chapter ending — maintain tone continuity)\n\n{tail}"
            return None
        except Exception:
            self._slot_degraded("chapter_transition_buffer", scene)
            return None

    def _similar_scene_context(self, scene: SceneCard) -> str | None:
        """Blueprint §3 Track 3: semantic retrieval for atmosphere/echo material."""
        try:
            # 审计 P-7 关联：统一走 get_vector_store()（memory=进程级单例 / chroma=持久化）。
            # 行为保持"每次由 DB 重建集合再查询"——自包含且结果始终新鲜。
            from novel_system.services.vector_store import get_vector_store

            project_id = require_scene_project_id(self.session, scene)
            collection_name = f"scenes_{project_id}"
            store = get_vector_store()
            approved_scenes = (
                self.session.execute(
                    select(FinalScene)
                    .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                    .where(
                        FinalScene.status.in_(
                            ("approved", "near_final_ready", "archived")
                        ),
                        SceneCard.trashed_flag == 0,
                        SceneCard.scene_id != scene.scene_id,
                        SceneCard.project_id == project_id,
                    )
                    .order_by(SceneCard.scene_seq.asc())
                )
                .scalars()
                .all()
            )
            if not approved_scenes or len(approved_scenes) < 2:
                return None
            documents = [
                {"id": fs.scene_id, "text": (fs.content or "")[:600]}
                for fs in approved_scenes
                if fs.content
            ]
            if not documents:
                return None
            store.write_collection(collection_name, documents)
            query_text = scene.scene_goal or ""
            if scene.location:
                query_text += f" {scene.location}"
            results = store.query(collection_name, query_text, top_k=2)
            if not results:
                return None
            lines = [
                "## Similar Scene Context (§3 Track 3 — inspiration only, NOT fact-authoritative)",
                "These excerpts are for atmosphere/echo reference. They may be imprecise.",
                "Do NOT copy facts, character states, or plot points from them.",
                "Use them only for tonal resonance, imagery contrast, or emotional echoing.",
            ]
            for item in results:
                lines.append(
                    f"\n[scene {item.get('id', '?')}]\n{item.get('text', '')[:400]}"
                )
            return "\n".join(lines)
        except Exception:
            self._slot_degraded("similar_scene", scene)
            return None


    def _information_asymmetry_digest(self, scene: SceneCard) -> str | None:
        """Blueprint §2/§11: inject information gaps between onstage characters."""
        try:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            log = NarrativeEventLog(self.session)
            project_id = require_scene_project_id(self.session, scene)
            onstage = scene.onstage_chars_json or []
            if len(onstage) < 2:
                return None
            # Wave 4（§5.6）：写作提示词走 POV 减法投影——传 pov 后，他人秘密/错误信念
            # 内容被抑制，只保留 POV 独有认知与内容无关的盲区提示。
            text = log.information_asymmetry_digest(
                project_id,
                None,
                onstage,
                scene_id=scene.scene_id,
                pov_character_id=scene.pov_character_id,
            )
            return text if text else None
        except Exception:
            self._slot_degraded("information_asymmetry", scene)
            return None
