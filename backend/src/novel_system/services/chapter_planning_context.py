"""章节编排 LLM 的上下文底座（chapter planning context）。

与 bundle_builder 同族但面向「规划一章」而非「起草一场」：把雪花 canon、章节蓝图、
叙事事件账本、伏笔债、张力邻域、作者约束等既有资产汇编成一份确定性的 prompt payload，
带 source_version_refs（可审计）与 degraded_slots（缺料降级，冷启动不阻断）。

设计文档：docs/chapter-arrangement-llm-design-2026-07-16.md §3。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    GenerationPlanningArtifact,
    SceneCard,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.author_preferences import (
    merge_preference_summaries,
    safe_preference_summary_for_prompt,
)
from novel_system.services.catalog import (
    CatalogService,
    SCENE_BRIEF_GCS,
    SCENE_BRIEF_RDD,
    chapter_title,
    scene_kind,
    scene_title,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json, normalize

CHAPTER_ARCHITECTURE_ARTIFACT = "chapter_story_architecture"

# 雪花 canon 摘要只取世界观/主线约束层；场景清单/场景细节体量大且已物化进目录，不重复注入。
_CANON_STEP_KEYS = (
    "one_sentence_summary",
    "one_paragraph_summary",
    "short_synopsis",
    "long_synopsis",
    "character_synopses",
)
# 单步 canon 摘要的字符预算（长大纲最大）；超出截断并标注。
_CANON_CHAR_BUDGET = {"long_synopsis": 2000, "character_synopses": 1200}
_CANON_DEFAULT_BUDGET = 600

# 张力邻域窗口：前 3 章 + 本章 + 后 2 章。
_TENSION_WINDOW_BEFORE = 3
_TENSION_WINDOW_AFTER = 2
# POV 近期分布回看的章数。
_POV_LOOKBACK = 6


@dataclass(slots=True)
class ChapterPlanningContext:
    project_id: str
    chapter_id: str
    prompt_payload: dict[str, Any]
    source_version_refs: dict[str, Any]
    degraded_slots: list[str]
    context_fingerprint: str
    chapter: ChapterGoal = field(repr=False, default=None)  # type: ignore[assignment]
    scenes: list[SceneCard] = field(repr=False, default_factory=list)


class ChapterPlanningContextBuilder:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._catalog = CatalogService(session)
        self._degraded: list[str] = []

    def build(self, project_id: str, chapter_id: str) -> ChapterPlanningContext:
        self._degraded = []
        project = self.session.get(StoryProject, project_id)
        if project is None:
            raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
        chapters = self._catalog.chapter_rows(project_id)
        index = next((i for i, row in enumerate(chapters) if row.chapter_id == chapter_id), None)
        if index is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)
        chapter = chapters[index]
        scenes = self._catalog.scene_rows(chapter_id)

        refs: dict[str, Any] = {
            "project_id": project_id,
            "chapter_goal": chapter.chapter_id,
            "scene_card_ids": [scene.scene_id for scene in scenes],
        }
        payload: dict[str, Any] = {
            "project": {
                "project_id": project_id,
                "title": project.title,
                "genre": project.genre,
            },
            "chapter_card": self._chapter_card_slot(chapter, index),
            "scene_cards_current": [self._scene_slot(scene) for scene in scenes],
            "neighbor_handoff": self._neighbor_slot(chapters, index),
        }

        architecture = latest_chapter_architecture(self.session, chapter_id)
        if architecture is not None:
            payload["chapter_architecture"] = architecture.payload_json or {}
            refs["chapter_story_architecture_artifact_row_id"] = architecture.row_id
        else:
            self._slot_degraded("chapter_architecture")

        canon = self._snowflake_canon_slot(project_id, refs)
        if canon:
            payload["snowflake_canon"] = canon
        else:
            self._slot_degraded("snowflake_canon")

        first_scene = scenes[0] if scenes else None
        narrative_state = self._narrative_state_slot(project_id, first_scene)
        if narrative_state:
            payload["narrative_state"] = narrative_state
        else:
            self._slot_degraded("narrative_state")

        foreshadow = None
        if foreshadow:
            payload["foreshadow_debts"] = foreshadow
        else:
            self._slot_degraded("foreshadow_debts")

        payload["tension_neighborhood"] = self._tension_slot(chapters, index)
        payload["character_positions"] = self._character_slot(chapters, index, scenes)
        payload["author_constraints"] = self._constraints_slot(project, chapter, refs)

        fingerprint = uuid.uuid5(uuid.NAMESPACE_URL, canonical_json(normalize(payload))).hex
        return ChapterPlanningContext(
            project_id=project_id,
            chapter_id=chapter_id,
            prompt_payload=payload,
            source_version_refs=refs,
            degraded_slots=list(self._degraded),
            context_fingerprint=fingerprint,
            chapter=chapter,
            scenes=scenes,
        )

    # ---------- slots ----------

    def _slot_degraded(self, slot: str) -> None:
        if slot not in self._degraded:
            self._degraded.append(slot)

    def _chapter_card_slot(self, chapter: ChapterGoal, index: int) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        return {
            "chapter_id": chapter.chapter_id,
            "no": index + 1,
            "title": chapter_title(chapter),
            "state": str(chapter.state or "planned"),
            "words_target": chapter.words_target,
            "act": narrative.get("act"),
            "tension": narrative.get("tension"),
            "pov": narrative.get("pov"),
            "entry": narrative.get("entry"),
            "exit": narrative.get("exit"),
            "promise": narrative.get("promise"),
            "drama": dict(narrative.get("drama") or {}),
            "threads": list(narrative.get("threads") or []),
        }

    def _scene_slot(self, scene: SceneCard) -> dict[str, Any]:
        kind = scene_kind(scene)
        brief_json = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        pov_name = ""
        if scene.pov_character_id:
            character = self.session.get(StoryCharacter, scene.pov_character_id)
            pov_name = character.display_name if character is not None else ""
        return {
            "scene_id": scene.scene_id,
            "seq": scene.scene_seq,
            "title": scene_title(scene),
            "kind": kind,
            "state": str(scene.state or "todo"),
            "brief": {key: str(brief_json.get(key) or "") for key in keys},
            "pov_character_name": pov_name,
            "exit_change": str(scene.exit_change or ""),
            "hook": str(scene.hook or ""),
            "words_current": int(scene.words_current or 0),
        }

    def _neighbor_slot(self, chapters: list[ChapterGoal], index: int) -> dict[str, Any]:
        def _chapter_edge(row: ChapterGoal, edge: str) -> dict[str, Any]:
            narrative = dict(row.narrative_json or {})
            info: dict[str, Any] = {
                "chapter_id": row.chapter_id,
                "title": chapter_title(row),
                edge: narrative.get(edge),
            }
            if edge == "exit":
                last_scene = self._catalog.scene_rows(row.chapter_id)[-1:]
                if last_scene:
                    info["last_scene"] = {
                        "title": scene_title(last_scene[0]),
                        "exit_change": str(last_scene[0].exit_change or ""),
                        "hook": str(last_scene[0].hook or ""),
                    }
            return info

        prev_row = chapters[index - 1] if index > 0 else None
        next_row = chapters[index + 1] if index + 1 < len(chapters) else None
        return {
            "prev": _chapter_edge(prev_row, "exit") if prev_row is not None else None,
            "next": _chapter_edge(next_row, "entry") if next_row is not None else None,
        }

    def _snowflake_canon_slot(self, project_id: str, refs: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            from novel_system.services.snowflake_steps import merge_step_draft

            rows = self.session.execute(
                select(SnowflakeStepRun)
                .where(SnowflakeStepRun.project_id == project_id)
                .order_by(SnowflakeStepRun.version.asc(), SnowflakeStepRun.created_at.asc())
            ).scalars().all()
            latest: dict[str, SnowflakeStepRun] = {}
            for row in rows:
                if row.status == "superseded":
                    continue
                latest[row.step_key] = row
            items: list[dict[str, Any]] = []
            run_ids: list[str] = []
            for step_key in _CANON_STEP_KEYS:
                artifact = latest.get(step_key)
                if artifact is None or str(artifact.status or "") not in {"approved", "skipped"}:
                    continue
                draft = merge_step_draft(step_key, artifact.artifact_json, latest_by_step=latest)
                budget = _CANON_CHAR_BUDGET.get(step_key, _CANON_DEFAULT_BUDGET)
                items.append(
                    {
                        "step_key": step_key,
                        "draft": _truncate_value(draft, budget),
                    }
                )
                run_ids.append(artifact.step_run_id)
            if run_ids:
                refs["snowflake_step_run_ids"] = run_ids
            return items
        except Exception:
            self._slot_degraded("snowflake_canon")
            return []

    def _narrative_state_slot(self, project_id: str, first_scene: SceneCard | None) -> str | None:
        if first_scene is None:
            return None
        try:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            log = NarrativeEventLog(self.session)
            text = log.format_state_for_prompt(
                project_id,
                None,
                scene_id=first_scene.scene_id,
                pov_character_id=None,
                onstage_character_ids=None,
            )
            return text or None
        except Exception:
            return None


    def _tension_slot(self, chapters: list[ChapterGoal], index: int) -> dict[str, Any]:
        lo = max(0, index - _TENSION_WINDOW_BEFORE)
        hi = min(len(chapters), index + _TENSION_WINDOW_AFTER + 1)
        window = []
        for i in range(lo, hi):
            narrative = dict(chapters[i].narrative_json or {})
            window.append(
                {
                    "no": i + 1,
                    "title": chapter_title(chapters[i]),
                    "tension": narrative.get("tension"),
                    "act": narrative.get("act"),
                    "is_current": i == index,
                }
            )
        return {"window": window, "total_chapters": len(chapters)}

    def _character_slot(
        self,
        chapters: list[ChapterGoal],
        index: int,
        scenes: list[SceneCard],
    ) -> dict[str, Any]:
        pov_counts: dict[str, int] = {}
        for i in range(max(0, index - _POV_LOOKBACK), index):
            pov = str(dict(chapters[i].narrative_json or {}).get("pov") or "").strip()
            if pov:
                pov_counts[pov] = pov_counts.get(pov, 0) + 1
        character_ids: list[str] = []
        for scene in scenes:
            for cid in [scene.pov_character_id, *(scene.onstage_chars_json or [])]:
                if cid and cid not in character_ids:
                    character_ids.append(cid)
        names: list[str] = []
        for cid in character_ids:
            row = self.session.get(StoryCharacter, cid)
            if row is not None and row.display_name:
                names.append(row.display_name)
        return {"recent_pov_distribution": pov_counts, "onstage_characters": names}

    def _constraints_slot(
        self,
        project: StoryProject,
        chapter: ChapterGoal,
        refs: dict[str, Any],
    ) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        drama = dict(narrative.get("drama") or {})
        constraints: dict[str, Any] = {
            "forbidden": str(drama.get("forbidden") or ""),
            "must_not": str(chapter.must_not or ""),
            "notes": str(drama.get("notes") or ""),
        }
        try:
            from novel_system.db.models import AuthorPreferenceProfile

            scopes: list[tuple[str, str]] = [("global", "global")]
            genre = " ".join(str(project.genre or "").strip().lower().split())
            if genre:
                scopes.append(("genre", genre[:120]))
            scopes.append(("project", project.project_id))
            scopes.append(("chapter", chapter.chapter_id))
            profiles: list[AuthorPreferenceProfile] = []
            for scope_type, scope_ref_id in scopes:
                profiles.extend(
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
                    ).scalars().all()
                )
            if profiles:
                merged: dict[str, Any] = {}
                for row in profiles:
                    merged = merge_preference_summaries(merged, row.summary_json or {})
                constraints["author_preferences"] = safe_preference_summary_for_prompt(merged)
                refs["author_preference_profile_ids"] = [row.profile_id for row in profiles]
            else:
                self._slot_degraded("author_preferences")
        except Exception:
            self._slot_degraded("author_preferences")
        return constraints


def latest_chapter_architecture(
    session: Session, chapter_id: str
) -> GenerationPlanningArtifact | None:
    return session.execute(
        select(GenerationPlanningArtifact)
        .where(
            GenerationPlanningArtifact.artifact_type == CHAPTER_ARCHITECTURE_ARTIFACT,
            GenerationPlanningArtifact.object_type == "chapter",
            GenerationPlanningArtifact.object_id == chapter_id,
            GenerationPlanningArtifact.status == "active",
        )
        .order_by(
            GenerationPlanningArtifact.created_at.desc(),
            GenerationPlanningArtifact.row_id.desc(),
        )
    ).scalars().first()


def _truncate_value(value: Any, budget: int) -> Any:
    """按字符预算截断 canon 摘要；结构保持 JSON 可序列化。"""
    if isinstance(value, str):
        return value if len(value) <= budget else value[:budget] + "…（截断）"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        remaining = budget
        for key, item in value.items():
            out[key] = _truncate_value(item, max(80, remaining))
            if isinstance(out[key], str):
                remaining -= len(out[key])
            if remaining <= 0:
                break
        return out
    if isinstance(value, list):
        out_list = []
        remaining = budget
        for item in value:
            trimmed = _truncate_value(item, max(80, remaining))
            out_list.append(trimmed)
            remaining -= len(canonical_json(normalize(trimmed)))
            if remaining <= 0:
                break
        return out_list
    return value
