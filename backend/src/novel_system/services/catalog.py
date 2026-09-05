"""FE-ALIGN Phase 3: 目录统一 —— ChapterGoal/SceneCard 之上的章节/场景树服务。

`GET/PATCH /api/v2/projects/{id}/catalog…` 的服务层；雪花物化（approve_outline_plan）
创建的行与本服务读写的是同一批行（护栏测试 test_catalog_single_source.py）。

约定：
- 章顺序 = display_order（混合 chapter_id 格式下不能依赖字典序；缺号惰性补齐）。
- 场景顺序 = 既有 scene_seq（与 v1 scene-order 端点同一套逻辑，不另建列）。
- 章标题写 narrative_json["title"]；读取回退 writer_brief_json.chapter_title →
  writer_brief_json.title → chapter_goal 首行。
- slug 不入库：章 slug = "ch"+序号两位，场景 slug = 章slug+"s"+scene_seq（原型
  ch08s3 格式，⌘K/深链/写作器历史 id 都用它）。
- C4 裁决：scene brief 按 kind 返回 GCS（proactive）或 RDD（reactive），
  前端 store 适配层负责映射到视图槽位。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    StoryProject,
)
from novel_system.services.chapter_approval import (
    is_chapter_approved,
    require_chapter_mutation_allowed,
)
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.errors import DomainError
from novel_system.services.projects import (
    PROJECT_STATUS_CHAPTER_FINAL_REVIEW,
    ProjectService,
)

CHAPTER_STATES = ("planned", "todo", "writing", "draft", "review", "approved")
SCENE_STATES = ("todo", "writing", "done")

# narrative_json 里由目录 API 维护的字段（形状抄 design/ws-catalog.jsx 章节对象）
NARRATIVE_FIELDS = (
    "title",
    "act",
    "tension",
    "pov",
    "time_label",
    "place",
    "entry",
    "exit",
    "align",
    "promise",
    "drama",
    "threads",
    "notes",
)

SCENE_BRIEF_GCS = ("goal", "conflict", "setback")
SCENE_BRIEF_RDD = ("reaction", "dilemma", "decision")


def scene_kind(scene: SceneCard) -> str:
    brief = dict(scene.writer_brief_json or {})
    raw = str(brief.get("primary_form") or scene.scene_type or "proactive").strip().lower()
    return "reactive" if raw.startswith("react") or raw == "反应" else "proactive"


def chapter_title(chapter: ChapterGoal) -> str:
    narrative = dict(chapter.narrative_json or {})
    if str(narrative.get("title") or "").strip():
        return str(narrative["title"]).strip()
    brief = dict(chapter.writer_brief_json or {})
    for key in ("chapter_title", "title"):
        if str(brief.get(key) or "").strip():
            return str(brief[key]).strip()
    goal = str(chapter.chapter_goal or "").strip()
    return (goal.splitlines()[0][:24] if goal else "") or chapter.chapter_id


def scene_title(scene: SceneCard) -> str:
    brief = dict(scene.writer_brief_json or {})
    if str(brief.get("title") or "").strip():
        return str(brief["title"]).strip()
    return str(scene.scene_goal or "").strip() or scene.scene_id


class CatalogService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._projects = ProjectService(session)

    # ---------- 读 ----------

    def catalog(self, project_id: str) -> dict[str, Any]:
        project = self._projects.require_project(project_id)
        chapters = self.chapter_rows(project_id)
        return {
            "project_id": project_id,
            "chapters": [
                self.chapter_payload(project, chapter, index)
                for index, chapter in enumerate(chapters)
            ],
        }

    def chapter_rows(self, project_id: str) -> list[ChapterGoal]:
        rows = list(
            self.session.execute(
                select(ChapterGoal).where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
            ).scalars().all()
        )
        # 缺 display_order 的行（雪花物化/旧数据）按 chapter_id 字典序惰性补号
        rows.sort(key=lambda c: (c.display_order is None, c.display_order or 0, c.chapter_id))
        pending = [
            (chapter, index)
            for index, chapter in enumerate(rows, start=1)
            if chapter.display_order != index
        ]
        # A read/backfill must never move an approved row as a side effect.  A
        # legacy drift remains visible and must be repaired through the explicit
        # reopen flow rather than silently weakening final-approval immutability.
        if pending and not any(
            is_chapter_approved(self.session, chapter) for chapter, _ in pending
        ):
            self._assign_chapter_orders(pending)
        return rows

    def scene_rows(self, chapter_id: str) -> list[SceneCard]:
        return list(
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars().all()
        )

    def chapter_payload(
        self, project: StoryProject, chapter: ChapterGoal, index: int
    ) -> dict[str, Any]:
        narrative = dict(chapter.narrative_json or {})
        slug = f"ch{index + 1:02d}"
        scenes = self.scene_rows(chapter.chapter_id)
        words_cur = sum(int(s.words_current or 0) for s in scenes)
        return {
            "chapter_id": chapter.chapter_id,
            "slug": slug,
            "no": f"{index + 1:02d}",
            "title": chapter_title(chapter),
            "state": str(chapter.state or "planned"),
            "current": chapter.chapter_id == project.current_chapter_id,
            "words": {"cur": words_cur, "target": chapter.words_target},
            "act": narrative.get("act"),
            "tension": narrative.get("tension"),
            "pov": narrative.get("pov"),
            "time_label": narrative.get("time_label"),
            "place": narrative.get("place"),
            "entry": narrative.get("entry"),
            "exit": narrative.get("exit"),
            "align": narrative.get("align"),
            "promise": narrative.get("promise"),
            "drama": dict(narrative.get("drama") or {}),
            "threads": list(narrative.get("threads") or []),
            "scenes": [self.scene_payload(scene, chapter_slug=slug) for scene in scenes],
        }

    def scene_payload(self, scene: SceneCard, *, chapter_slug: str) -> dict[str, Any]:
        kind = scene_kind(scene)
        brief_json = dict(scene.writer_brief_json or {})
        keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        pov_id = str(scene.pov_character_id or "")
        pov_name = ""
        if pov_id:
            character = self.session.get(StoryCharacter, pov_id)
            pov_name = character.display_name if character is not None else ""
        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "slug": f"{chapter_slug}s{scene.scene_seq}",
            "seq": scene.scene_seq,
            "title": scene_title(scene),
            "kind": kind,
            "state": str(scene.state or "todo"),
            "words": int(scene.words_current or 0),
            "brief": {"kind": kind, **{key: str(brief_json.get(key) or "") for key in keys}},
            "pov_character_id": pov_id,
            "pov_character_name": pov_name,
            # 章节编排 LLM 规划（2026-07-16）可填的两个交接槽；可加性扩展，旧前端忽略即可。
            "exit_change": str(scene.exit_change or ""),
            "hook": str(scene.hook or ""),
        }

    # ---------- 写 ----------

    def update_chapter(self, project_id: str, chapter_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project = self._projects.require_project(project_id)
        chapter = self._require_chapter(project_id, chapter_id)
        body = payload or {}
        updates: dict[str, Any] = {}
        if "state" in body:
            state = str(body["state"] or "").strip()
            if state not in CHAPTER_STATES:
                raise DomainError("CATALOG_STATE_INVALID", f"chapter state must be one of {CHAPTER_STATES}", status_code=400)
            if state == "approved" and not is_chapter_approved(self.session, chapter):
                raise DomainError(
                    "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                    "chapter approval must use the project final-approval flow",
                    status_code=409,
                )
            if (
                state != "approved"
                and state != chapter.state
                and is_chapter_approved(self.session, chapter)
            ):
                raise DomainError(
                    "CATALOG_APPROVED_CHAPTER_REOPEN_REQUIRED",
                    "approved chapter state can change only after reopening its final",
                    status_code=409,
                )
            # ``review`` is also used by the catalog as an editorial/structural
            # label while an approved outline is being maintained.  Canonical
            # FinalScene coverage is required only when this PATCH is the
            # project's real final-review submission; outline materialization
            # and re-materialization happen in ``chapter_ready`` before prose
            # exists and must remain valid structural operations.
            if (
                state == "review"
                and project.status == PROJECT_STATUS_CHAPTER_FINAL_REVIEW
                and project.current_chapter_id == chapter.chapter_id
            ):
                from novel_system.services.chapter_manuscripts import (
                    ChapterManuscriptService,
                )

                ChapterManuscriptService(self.session).require_publishable(chapter_id)
            updates["state"] = state
        if "words_target" in body:
            value = body["words_target"]
            updates["words_target"] = int(value) if value not in (None, "") else None
        narrative = dict(chapter.narrative_json or {})
        for key in NARRATIVE_FIELDS:
            if key in body:
                narrative[key] = body[key]
        if any(key in body for key in NARRATIVE_FIELDS):
            updates["narrative_json"] = narrative
        set_current = bool(body.get("current"))
        changed_fields = [
            key for key, value in updates.items() if getattr(chapter, key) != value
        ]
        if set_current and project.current_chapter_id != chapter.chapter_id:
            changed_fields.append("project.current_chapter_id")
        changed = require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=changed_fields,
            operation="catalog.update_chapter",
        )
        if changed:
            for key, value in updates.items():
                setattr(chapter, key, value)
            if set_current:
                project.current_chapter_id = chapter.chapter_id
            self.session.flush()
        chapters = self.chapter_rows(project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter_id)
        return {
            "chapter": self.chapter_payload(project, chapter, index),
            "changed": changed,
        }

    def create_chapter(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        project = self._projects.require_project(project_id)
        body = payload or {}
        existing = self.chapter_rows(project_id)
        title = str(body.get("title") or "").strip() or f"第 {len(existing) + 1} 章"
        # 空目录首章立为在写章（抄 WsCatalog.addChapter 语义）；调用方也可显式传 state/current
        is_first = not existing
        state = str(body.get("state") or ("writing" if is_first else "planned"))
        if state not in CHAPTER_STATES:
            raise DomainError("CATALOG_STATE_INVALID", f"chapter state must be one of {CHAPTER_STATES}", status_code=400)
        if state == "approved":
            raise DomainError(
                "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW",
                "chapter approval must use the project final-approval flow",
                status_code=409,
            )
        chapter = ChapterGoal(
            chapter_id=f"{project_id}_CH_{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            planned_scene_count=0,
            chapter_goal=title,
            state=state,
            words_target=int(body["words_target"]) if body.get("words_target") else None,
            display_order=len(existing) + 1,
            narrative_json={"title": title, **{k: body[k] for k in NARRATIVE_FIELDS if k in body and k != "title"}},
            writer_brief_json={"source": "catalog_api", "title": title},
        )
        self.session.add(chapter)
        self.session.flush()
        # 审计 P-1：冷启动章与雪花物化/章 API 同约定补建运行时状态行，
        # 否则场景 run 通过全部 QC 后在归档/聚合段对缺行章 None 解引用 500。
        ensure_chapter_state(self.session, chapter.chapter_id)
        if is_first or body.get("current", True):
            project.current_chapter_id = chapter.chapter_id
        # 默认带一个开场场景（抄 addChapter：scenes=[开场]）；传 with_scene=False 可跳过
        if body.get("with_scene", True):
            self._insert_scene(
                chapter,
                position=0,
                title=str(body.get("scene_title") or "开场"),
                kind="proactive",
                state="writing" if is_first else "todo",
                brief={"goal": title},
            )
        self.session.flush()
        chapters = self.chapter_rows(project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter.chapter_id)
        return {"chapter": self.chapter_payload(project, chapter, index)}

    def update_scene(self, project_id: str, scene_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        scene = self._require_scene(project_id, scene_id)
        chapter = self._require_chapter(project_id, scene.chapter_id)
        body = payload or {}
        brief = dict(scene.writer_brief_json or {})
        updates: dict[str, Any] = {}
        if "title" in body:
            brief["title"] = str(body["title"] or "").strip()
        if "kind" in body:
            kind = "reactive" if str(body["kind"]).strip().lower() in {"reactive", "反应"} else "proactive"
            updates["scene_type"] = kind
            brief["primary_form"] = kind
        if "state" in body:
            state = str(body["state"] or "").strip()
            if state not in SCENE_STATES:
                raise DomainError("CATALOG_STATE_INVALID", f"scene state must be one of {SCENE_STATES}", status_code=400)
            updates["state"] = state
        if "exit_change" in body:
            updates["exit_change"] = str(body.get("exit_change") or "")
        if "hook" in body:
            updates["hook"] = str(body.get("hook") or "")
        for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
            if key in body:
                brief[key] = str(body[key] or "")
        nested = body.get("brief")
        if isinstance(nested, dict):
            for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
                if key in nested:
                    brief[key] = str(nested[key] or "")
        # POV 角色:按 id 选既有角色,或按名 find-or-create(让冷启动作品无需走完整雪花
        # 物化即可设 pov,解执行契约的 pov_character_id 硬阻断);空串显式清空。
        needs_character_create = False
        character_name = ""
        if "pov_character_id" in body or "pov_character_name" in body:
            pov_id = str(body.get("pov_character_id") or "").strip()
            pov_name = str(body.get("pov_character_name") or "").strip()
            if pov_id:
                character = self.session.get(StoryCharacter, pov_id)
                if character is None or character.project_id != project_id:
                    raise DomainError("CATALOG_POV_CHARACTER_NOT_FOUND", "pov character not found in project", status_code=400)
                updates["pov_character_id"] = pov_id
            elif pov_name:
                existing_character = self.session.execute(
                    select(StoryCharacter).where(
                        StoryCharacter.project_id == project_id,
                        StoryCharacter.display_name == pov_name,
                    )
                ).scalars().first()
                if existing_character is None:
                    needs_character_create = True
                    character_name = pov_name
                else:
                    updates["pov_character_id"] = existing_character.character_id
            else:
                updates["pov_character_id"] = None
        if brief != dict(scene.writer_brief_json or {}):
            updates["writer_brief_json"] = brief
        changed_fields = [
            key for key, value in updates.items() if getattr(scene, key) != value
        ]
        if needs_character_create:
            changed_fields.extend(
                ["story_character.create", "pov_character_id"]
            )
        changed = require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=changed_fields,
            operation="catalog.update_scene",
        )
        if changed:
            if needs_character_create:
                updates["pov_character_id"] = self._find_or_create_character(
                    project_id,
                    character_name,
                ).character_id
            for key, value in updates.items():
                setattr(scene, key, value)
            self.session.flush()
        return {
            "scene": self._scene_payload_with_slug(scene),
            "changed": changed,
        }

    def _find_or_create_character(self, project_id: str, display_name: str) -> StoryCharacter:
        existing = self.session.execute(
            select(StoryCharacter).where(
                StoryCharacter.project_id == project_id,
                StoryCharacter.display_name == display_name,
            )
        ).scalars().first()
        if existing is not None:
            return existing
        character = StoryCharacter(
            character_id=f"CHAR_{uuid.uuid4().hex[:10].upper()}",
            project_id=project_id,
            display_name=display_name,
        )
        self.session.add(character)
        self.session.flush()
        return character

    def create_scene(self, project_id: str, chapter_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        chapter = self._require_chapter(project_id, chapter_id)
        require_chapter_mutation_allowed(
            self.session,
            chapter,
            changed_fields=["scenes.create"],
            operation="catalog.create_scene",
        )
        body = payload or {}
        scenes = self.scene_rows(chapter_id)
        at = body.get("at")
        position = int(at) if at is not None else len(scenes)
        position = max(0, min(position, len(scenes)))
        kind = "reactive" if str(body.get("kind") or "").strip().lower() in {"reactive", "反应"} else "proactive"
        state = str(body.get("state") or "todo")
        if state not in SCENE_STATES:
            raise DomainError("CATALOG_STATE_INVALID", f"scene state must be one of {SCENE_STATES}", status_code=400)
        brief_keys = SCENE_BRIEF_GCS if kind == "proactive" else SCENE_BRIEF_RDD
        brief = {key: str(body.get(key) or "") for key in brief_keys if body.get(key)}
        nested = body.get("brief")
        if isinstance(nested, dict):
            for key in (*SCENE_BRIEF_GCS, *SCENE_BRIEF_RDD):
                if key in nested:
                    brief[key] = str(nested[key] or "")
        scene = self._insert_scene(
            chapter,
            position=position,
            title=str(body.get("title") or "").strip() or "新场景",
            kind=kind,
            state=state,
            brief=brief,
        )
        if "exit_change" in body:
            scene.exit_change = str(body.get("exit_change") or "")
        if "hook" in body:
            scene.hook = str(body.get("hook") or "")
        if "pov_character_id" in body or "pov_character_name" in body:
            pov_id = str(body.get("pov_character_id") or "").strip()
            pov_name = str(body.get("pov_character_name") or "").strip()
            if pov_id:
                character = self.session.get(StoryCharacter, pov_id)
                if character is None or character.project_id != project_id:
                    raise DomainError(
                        "CATALOG_POV_CHARACTER_NOT_FOUND",
                        "pov character not found in project",
                        status_code=400,
                    )
                scene.pov_character_id = pov_id
            elif pov_name:
                scene.pov_character_id = self._find_or_create_character(
                    project_id,
                    pov_name,
                ).character_id
            else:
                scene.pov_character_id = None
        self.session.flush()
        return {"scene": self._scene_payload_with_slug(scene), "changed": True}

    def reorder_chapters(
        self,
        project_id: str,
        chapter_ids: list[str],
    ) -> dict[str, Any]:
        """Persist the complete active chapter order without moving finals.

        The endpoint accepts a full-set replacement so stale clients cannot
        accidentally drop a newly-created chapter.  Approved chapters retain
        both their relative sequence and their absolute catalog position.
        """

        project = self._projects.require_project(project_id)
        requested = list(chapter_ids)
        if len(requested) != len(set(requested)):
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_DUPLICATE",
                "chapter_ids must not contain duplicates",
                status_code=400,
            )

        current_rows = self.chapter_rows(project_id)
        current_ids = [chapter.chapter_id for chapter in current_rows]
        requested_set = set(requested)
        current_set = set(current_ids)
        foreign_ids = [
            row.chapter_id
            for row in self.session.execute(
                select(ChapterGoal).where(ChapterGoal.chapter_id.in_(requested or [""]))
            ).scalars().all()
            if row.project_id != project.project_id or row.trashed_flag == 1
        ]
        if foreign_ids:
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_PROJECT_MISMATCH",
                "chapter_ids must contain only active chapters from this project",
                status_code=409,
                details={"chapter_ids": sorted(foreign_ids)},
            )
        if requested_set != current_set or len(requested) != len(current_ids):
            raise DomainError(
                "CATALOG_CHAPTER_ORDER_INCOMPLETE",
                "chapter_ids must contain every active chapter in the project exactly once",
                status_code=409,
                details={
                    "missing_chapter_ids": sorted(current_set - requested_set),
                    "unknown_chapter_ids": sorted(requested_set - current_set),
                },
            )

        if requested == current_ids:
            return {
                "project_id": project.project_id,
                "chapter_ids": current_ids,
                "chapters": [
                    {
                        "chapter_id": chapter.chapter_id,
                        "display_order": chapter.display_order,
                    }
                    for chapter in current_rows
                ],
                "changed": False,
            }

        by_id = {chapter.chapter_id: chapter for chapter in current_rows}
        approved_ids = {
            chapter.chapter_id
            for chapter in current_rows
            if is_chapter_approved(self.session, chapter)
        }
        old_approved_order = [chapter_id for chapter_id in current_ids if chapter_id in approved_ids]
        new_approved_order = [chapter_id for chapter_id in requested if chapter_id in approved_ids]
        old_positions = {
            chapter_id: index for index, chapter_id in enumerate(current_ids) if chapter_id in approved_ids
        }
        new_positions = {
            chapter_id: index for index, chapter_id in enumerate(requested) if chapter_id in approved_ids
        }
        moved_approved_ids = [
            chapter_id
            for chapter_id in old_approved_order
            if new_positions.get(chapter_id) != old_positions[chapter_id]
        ]
        if new_approved_order != old_approved_order or moved_approved_ids:
            raise DomainError(
                "CATALOG_APPROVED_CHAPTER_ORDER_LOCKED",
                "approved chapters cannot change relative order or catalog position",
                status_code=409,
                details={
                    "approved_chapter_ids": old_approved_order,
                    "moved_chapter_ids": moved_approved_ids,
                    "reopen_required": True,
                },
            )

        # Do not rewrite even an equivalent display_order value on approved
        # rows.  If historical data is already inconsistent, fail closed.
        inconsistent_approved_ids = [
            chapter_id
            for chapter_id, position in old_positions.items()
            if by_id[chapter_id].display_order != position + 1
        ]
        if inconsistent_approved_ids:
            raise DomainError(
                "CATALOG_APPROVED_CHAPTER_ORDER_INCONSISTENT",
                "approved chapter order is inconsistent and must be reopened before repair",
                status_code=409,
                details={
                    "chapter_ids": inconsistent_approved_ids,
                    "reopen_required": True,
                },
            )

        self._assign_chapter_orders(
            [
                (by_id[chapter_id], position)
                for position, chapter_id in enumerate(requested, start=1)
                if chapter_id not in approved_ids
            ]
        )
        return {
            "project_id": project.project_id,
            "chapter_ids": requested,
            "chapters": [
                {
                    "chapter_id": chapter_id,
                    "display_order": by_id[chapter_id].display_order,
                }
                for chapter_id in requested
            ],
            "changed": True,
        }

    def import_catalog(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """一次性迁移入口（admin 保护）：localStorage 旧目录 → 后端行。仅允许空目录导入。"""
        project = self._projects.require_project(project_id)
        if self.chapter_rows(project_id):
            raise DomainError(
                "CATALOG_NOT_EMPTY",
                "catalog import is only allowed into an empty catalog",
                status_code=409,
            )
        chapters = list((payload or {}).get("chapters") or [])
        if not chapters:
            raise DomainError("CATALOG_IMPORT_EMPTY", "chapters are required", status_code=400)
        normalized_states = [
            state if state in CHAPTER_STATES else "planned"
            for state in (str(item.get("state") or "planned") for item in chapters)
        ]
        requested_current_indexes = [index for index, item in enumerate(chapters) if bool(item.get("current"))]
        if len(requested_current_indexes) > 1:
            raise DomainError(
                "CATALOG_IMPORT_CURRENT_INVALID",
                "catalog import can contain at most one current chapter",
                status_code=400,
            )
        if requested_current_indexes:
            expected_current_index = requested_current_indexes[0]
            if normalized_states[expected_current_index] == "approved":
                raise DomainError(
                    "CATALOG_IMPORT_CURRENT_INVALID",
                    "current chapter cannot already be approved",
                    status_code=400,
                )
            if any(state == "approved" for state in normalized_states[expected_current_index + 1:]):
                raise DomainError(
                    "CATALOG_IMPORT_APPROVAL_ORDER_INVALID",
                    "approved chapters must precede the current chapter",
                    status_code=400,
                )
            # A controlled legacy import may carry intermediate review/draft
            # labels before its explicit current chapter.  The project flow is
            # linear, so canonicalize that historical prefix as approved.
            normalized_states[:expected_current_index] = ["approved"] * expected_current_index
            approved_prefix_length = expected_current_index
        else:
            approved_prefix_length = 0
            for state in normalized_states:
                if state != "approved":
                    break
                approved_prefix_length += 1
            if any(state == "approved" for state in normalized_states[approved_prefix_length:]):
                raise DomainError(
                    "CATALOG_IMPORT_APPROVAL_ORDER_INVALID",
                    "approved chapters must form a contiguous prefix in catalog order",
                    status_code=400,
                )
            expected_current_index = approved_prefix_length if approved_prefix_length < len(chapters) else None
        created_scenes = 0
        created_chapter_ids: list[str] = []
        for order, item in enumerate(chapters, start=1):
            title = str(item.get("title") or f"第 {order} 章").strip()
            state = normalized_states[order - 1]
            chapter = ChapterGoal(
                chapter_id=f"{project_id}_CH_{uuid.uuid4().hex[:8]}",
                project_id=project_id,
                planned_scene_count=len(item.get("scenes") or []),
                chapter_goal=title,
                state=state,
                words_target=int((item.get("words") or {}).get("target") or 0) or None,
                display_order=order,
                narrative_json={
                    "title": title,
                    "act": item.get("act"),
                    "tension": item.get("tension"),
                    "pov": item.get("pov"),
                    "time_label": item.get("time"),
                    "place": item.get("place"),
                    "entry": item.get("entry"),
                    "exit": item.get("exit"),
                    "align": item.get("align"),
                    "promise": item.get("promise"),
                    "drama": dict(item.get("drama") or {}),
                    "threads": list(item.get("threads") or []),
                },
                writer_brief_json={"source": "catalog_import", "title": title},
            )
            self.session.add(chapter)
            self.session.flush()
            created_chapter_ids.append(chapter.chapter_id)
            scenes_in = list(item.get("scenes") or [])
            # 旧目录的字数挂在章级（words.cur），场景级缺失时把差额摊给零字数场景，
            # 保证 rollup（章字数 = Σ场景字数）不丢数据。
            chapter_cur = int((item.get("words") or {}).get("cur") or 0)
            scene_words = [int(sc.get("words") or 0) for sc in scenes_in]
            shortfall = chapter_cur - sum(scene_words)
            zero_slots = [i for i, w in enumerate(scene_words) if w == 0]
            if scenes_in and shortfall > 0:
                slots = zero_slots or [len(scenes_in) - 1]
                base, remainder = divmod(shortfall, len(slots))
                for j, i in enumerate(slots):
                    scene_words[i] += base + (remainder if j == len(slots) - 1 else 0)
            for seq, sc in enumerate(scenes_in, start=1):
                kind = "reactive" if str(sc.get("kind") or "").strip() in {"反应", "reactive"} else "proactive"
                s_state = str(sc.get("state") or "todo")
                scene = SceneCard(
                    scene_id=f"{chapter.chapter_id}_SC{seq:02d}",
                    chapter_id=chapter.chapter_id,
                    project_id=project_id,
                    scene_seq=seq,
                    scene_goal=str(sc.get("title") or "").strip() or f"场景 {seq}",
                    scene_type=kind,
                    state=s_state if s_state in SCENE_STATES else ("writing" if s_state == "active" else "todo"),
                    words_current=scene_words[seq - 1],
                    is_chapter_last=1 if seq == len(scenes_in) else 0,
                    writer_brief_json={
                        "source": "catalog_import",
                        "title": str(sc.get("title") or "").strip(),
                        "primary_form": kind,
                        **(
                            {"goal": str(sc.get("goal") or ""), "conflict": str(sc.get("obstacle") or ""), "setback": str(sc.get("turn") or "")}
                            if kind == "proactive"
                            else {"reaction": str(sc.get("goal") or ""), "dilemma": str(sc.get("obstacle") or ""), "decision": str(sc.get("turn") or "")}
                        ),
                    },
                )
                self.session.add(scene)
                # 导入路径不在这里补建 SceneRunState：测试夹具（fixture_works）经由本路径
                # 播种并自行管理状态行；运行本章对缺行场景会惰性补建（chapter_runner）。
                created_scenes += 1
        project.approved_chapter_ids_json = created_chapter_ids[:approved_prefix_length]
        if expected_current_index is None:
            project.current_chapter_id = None
            project.status = "completed"
        else:
            project.current_chapter_id = created_chapter_ids[expected_current_index]
            project.status = "chapter_ready"
        self.session.flush()
        return {"created_chapter_count": len(chapters), "created_scene_count": created_scenes}

    # ---------- 字数 rollup（正文保存埋点用） ----------

    def words_rollup(self, scene: SceneCard) -> dict[str, Any]:
        chapter_words = sum(
            int(s.words_current or 0) for s in self.scene_rows(scene.chapter_id)
        )
        return {
            "scene_id": scene.scene_id,
            "scene_words": int(scene.words_current or 0),
            "chapter_id": scene.chapter_id,
            "chapter_words": chapter_words,
        }

    # ---------- internals ----------

    def _require_chapter(self, project_id: str, chapter_id: str) -> ChapterGoal:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.trashed_flag == 1 or chapter.project_id != project_id:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found in project", status_code=404)
        return chapter

    def _require_scene(self, project_id: str, scene_id: str) -> SceneCard:
        scene = self.session.get(SceneCard, scene_id)
        if scene is None or scene.trashed_flag == 1:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        owner = scene.project_id
        if not owner:
            chapter = self.session.get(ChapterGoal, scene.chapter_id)
            owner = chapter.project_id if chapter else None
        if owner != project_id:
            raise DomainError("SCENE_NOT_FOUND", "scene not found in project", status_code=404)
        return scene

    def _scene_payload_with_slug(self, scene: SceneCard) -> dict[str, Any]:
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        project = self._projects.require_project(chapter.project_id)
        chapters = self.chapter_rows(chapter.project_id)
        index = next(i for i, c in enumerate(chapters) if c.chapter_id == chapter.chapter_id)
        return self.scene_payload(scene, chapter_slug=f"ch{index + 1:02d}")

    def _insert_scene(
        self,
        chapter: ChapterGoal,
        *,
        position: int,
        title: str,
        kind: str,
        state: str,
        brief: dict[str, Any],
    ) -> SceneCard:
        scenes = self.scene_rows(chapter.chapter_id)
        seq_base = max((int(s.scene_seq or 0) for s in self._all_chapter_scenes(chapter.chapter_id)), default=0)
        scene = SceneCard(
            scene_id=f"{chapter.chapter_id}_SC_{uuid.uuid4().hex[:8]}",
            chapter_id=chapter.chapter_id,
            project_id=chapter.project_id,
            scene_seq=seq_base + 1,
            scene_goal=title,
            scene_type=kind,
            state=state,
            words_current=0,
            writer_brief_json={"source": "catalog_api", "title": title, "primary_form": kind, **brief},
        )
        self.session.add(scene)
        # 与 v1 scenes POST 同约定补建运行时状态行：章节运行（运行本章）按 scene_id
        # 取 SceneRunState，缺行会让整章一起步就 SCENE_NOT_FOUND。
        self.session.add(SceneRunState(scene_id=scene.scene_id, scene_status="ready"))
        self.session.flush()
        ordered = list(scenes)
        ordered.insert(max(0, min(position, len(ordered))), scene)
        self._renumber(ordered)
        chapter.planned_scene_count = len(ordered)
        return scene

    def _all_chapter_scenes(self, chapter_id: str) -> list[SceneCard]:
        return list(
            self.session.execute(
                select(SceneCard).where(SceneCard.chapter_id == chapter_id)
            ).scalars().all()
        )

    def _renumber(self, ordered: list[SceneCard]) -> None:
        if not ordered:
            return
        temporary_start = max(int(scene.scene_seq or 0) for scene in ordered) + 1
        for offset, scene in enumerate(ordered):
            scene.scene_seq = temporary_start + offset
        self.session.flush()
        for index, scene in enumerate(ordered, start=1):
            scene.scene_seq = index
            scene.is_chapter_last = 1 if index == len(ordered) else 0
        self.session.flush()

    def _assign_chapter_orders(
        self,
        assignments: list[tuple[ChapterGoal, int]],
    ) -> None:
        changed = [
            (chapter, int(display_order))
            for chapter, display_order in assignments
            if chapter.display_order != int(display_order)
        ]
        if not changed:
            return
        project_ids = {chapter.project_id for chapter, _ in changed}
        active_rows = list(
            self.session.execute(
                select(ChapterGoal).where(
                    ChapterGoal.project_id.in_(project_ids),
                    ChapterGoal.trashed_flag == 0,
                )
            ).scalars().all()
        )
        next_temporary_by_project = {
            project_id: max(
                (
                    int(chapter.display_order or 0)
                    for chapter in active_rows
                    if chapter.project_id == project_id
                ),
                default=0,
            )
            + 1
            for project_id in project_ids
        }
        for chapter, _display_order in changed:
            project_id = chapter.project_id
            chapter.display_order = next_temporary_by_project[project_id]
            next_temporary_by_project[project_id] += 1
        self.session.flush()
        for chapter, display_order in changed:
            chapter.display_order = display_order
        self.session.flush()
