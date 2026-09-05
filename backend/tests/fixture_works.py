"""测试/E2E 专用：两部中性夹具作品（样例长卷 work-a / 样例短卷 work-b）。

原 demo seed（潮汐档案/盐镇来信）已随「演示数据退役」删除；本模块是它的
中性化后继：同样的行结构（项目档案、写作统计基线、雪花步骤状态、目录
章/场景树、在写场景正文草稿、审阅卡、资料库、审计 findings、锚点库），
但内容全部是无剧情的占位文本——只服务测试与契约 E2E，绝不进入产品运行时。

目录写入复用 CatalogService.import_catalog —— 与一次性迁移走同一代码路径。
幂等：按固定 project_id 清理后重建。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ProjectWritingStats,
    SceneCard,
    SnowflakeStepRun,
    StoryProject,
    utcnow,
)
from novel_system.services.catalog import CatalogService
from novel_system.services.writing_stats import (
    WRITING_STATS_TZ,
    WritingStatsService,
    count_words,
)

FIXTURE_WORK_IDS = ("work-a", "work-b")

_CATALOG_JSON = Path(__file__).with_name("fixture_catalog.json")
_LIBRARY_JSON = Path(__file__).with_name("fixture_library.json")

# 雪花步骤状态（按 SNOWFLAKE_STEP_CATALOG 的 step_key 顺序；映射见 project_overview）：
# approved→done；pending_review 且为首个未过闸步→active；其余 pending_review→warn；缺行→todo。
_WORK_A_STEPS = {
    "book_brief": "approved",
    "one_sentence_summary": "approved",
    "one_paragraph_summary": "approved",
    "character_sheets": "approved",
    "short_synopsis": "approved",
    "character_synopses": "pending_review",  # active
    "long_synopsis": "approved",
    "character_bibles": "pending_review",  # warn
    "scene_list": "approved",
    "scene_details": "approved",
}

_WORK_B_STEPS = {
    "book_brief": "approved",
    "one_sentence_summary": "approved",
    "one_paragraph_summary": "pending_review",  # active
    "character_sheets": "approved",
    "short_synopsis": "pending_review",  # warn
}

_WORK_A_RESUME_LINES = [
    "样例正文最后一行甲：占位句，用于 resume 卡回读。",
    "样例正文最后一行乙：占位句，用于 resume 卡回读。",
]

_WORK_B_RESUME_LINES = [
    "样例正文最后一行丙：占位句，用于 resume 卡回读。",
    "样例正文最后一行丁：占位句，用于 resume 卡回读。",
]

_FILLER = "这是一段中性的夹具填充句，仅用于把正文铺到目标字数附近，不含任何剧情。"


def _prose(lines: list[str], target_chars: int) -> str:
    """夹具正文：以中性填充句铺到目标字数附近，签名句压尾（resume 卡读末两行）。"""
    paragraphs: list[str] = []
    total = sum(len(line) for line in lines)
    while total < target_chars - len(_FILLER):
        paragraphs.append(_FILLER)
        total += len(_FILLER)
    paragraphs.extend(lines)
    return "\n".join(paragraphs)


# 原型 kind 展示词 → 后端实体 kind
_ENTITY_KIND_MAP = {
    "地点": "location",
    "场所": "location",
    "物品": "item",
    "线索": "item",
    "信物": "item",
    "机构": "faction",
    "组织": "faction",
    "阵营": "faction",
}


def _seed_work_a_library(session: Session) -> None:
    """资料库夹具：人物→StoryCharacter、世界→LibraryEntity、
    大事记→TimelineEvent、links→LibraryRelation（事件端点不建边——关系表只接受
    character/entity ref）。"""
    from novel_system.db.models import (
        LibraryEntity,
        LibraryRelation,
        StoryCharacter,
        TimelineEvent,
    )

    if not _LIBRARY_JSON.exists():
        return
    entries = json.loads(_LIBRARY_JSON.read_text(encoding="utf-8")).get("work-a") or []
    for model in (LibraryRelation, TimelineEvent, LibraryEntity):
        session.execute(delete(model).where(model.project_id == "work-a"))
    session.execute(delete(StoryCharacter).where(StoryCharacter.project_id == "work-a"))
    session.flush()

    ref_of: dict[str, str] = {}
    for entry in entries:
        extras = {
            key: entry.get(key)
            for key in ("code", "accent", "glyph", "blurb", "facts", "appears", "arc", "state", "pinned", "updated")
            if entry.get(key) is not None
        }
        if entry["cat"] == "people":
            session.add(
                StoryCharacter(
                    character_id=entry["id"],
                    project_id="work-a",
                    display_name=entry["name"],
                    role=entry.get("kind") or None,
                    summary_json={"one_line": entry.get("summary") or "", "fe_details": extras},
                    status="active",
                )
            )
            ref_of[entry["id"]] = f"character:{entry['id']}"
        elif entry["cat"] == "world":
            session.add(
                LibraryEntity(
                    entity_id=entry["id"],
                    project_id="work-a",
                    kind=_ENTITY_KIND_MAP.get(str(entry.get("kind") or ""), "concept"),
                    name=entry["name"],
                    aliases_json=[],
                    summary=entry.get("summary") or "",
                    details_json=extras,
                    tags_json=list(entry.get("tags") or []),
                    status="active",
                )
            )
            ref_of[entry["id"]] = f"entity:{entry['id']}"
        elif entry["cat"] == "events":
            facts = {f.get("k"): f.get("v") for f in (entry.get("facts") or []) if isinstance(f, dict)}
            appears = list(entry.get("appears") or [])
            session.add(
                TimelineEvent(
                    event_id=entry["id"],
                    project_id="work-a",
                    label=entry["name"],
                    time_label=str(facts.get("时间") or facts.get("时点") or entry.get("summary") or ""),
                    chapter_ref=appears[0] if appears else None,
                    entity_refs_json=[],
                    note=entry.get("blurb") or entry.get("summary") or "",
                    display_order=None,
                )
            )
    session.flush()

    # links → 关系边（端点限 character/entity；目标是事件的链接进事件 entity_refs）
    event_ids = {e["id"] for e in entries if e["cat"] == "events"}
    seen_pairs: set[tuple[str, str]] = set()
    for entry in entries:
        source_ref = ref_of.get(entry["id"])
        for link in entry.get("links") or []:
            target_id = link.get("id")
            if target_id in event_ids and source_ref:
                event = session.get(TimelineEvent, target_id)
                if event is not None:
                    refs = list(event.entity_refs_json or [])
                    if source_ref not in refs:
                        refs.append(source_ref)
                        event.entity_refs_json = refs
                continue
            target_ref = ref_of.get(target_id)
            if not source_ref or not target_ref:
                continue
            pair = tuple(sorted((source_ref, target_ref)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            session.add(
                LibraryRelation(
                    relation_id=f"REL_FIX_{len(seen_pairs):03d}",
                    project_id="work-a",
                    from_ref=source_ref,
                    to_ref=target_ref,
                    kind=str(link.get("type") or "related"),
                    note=str(link.get("rel") or ""),
                )
            )
    session.flush()


# 设定冲突夹具（结构对齐原 LF2_CANON conflict 条目；locked 条目不建 finding）


# 扩展审计层夹具（空降/断链/线索不公平；ORM 直插，不产卡；FE 形状存 evidence）


# 锚点库夹具（悬念债/设定锚点/故事线/人物弧线 → LongformAnchor）


def cleanup_fixture_works(session: Session) -> None:
    from novel_system.db.models import (
        ChapterState,
        LibraryEntity,
        LibraryRelation,
        OutlinePlan,
        SceneBundle,
        SceneRunState,
        SnowflakeArtifact,
        SnowflakeAssistantTurn,
        SnowflakeCharacterPlan,
        SnowflakeRevisionLink,
        SnowflakeScenePlan,
        SnowflakeSceneTriageItem,
        StoryCharacter,
        TimelineEvent,
    )

    scene_ids = [
        row
        for row in session.execute(
            select(SceneCard.scene_id).where(SceneCard.project_id.in_(FIXTURE_WORK_IDS))
        ).scalars().all()
    ]
    if scene_ids:
        session.execute(
            delete(AuthorDraft).where(
                AuthorDraft.object_type == "scene", AuthorDraft.object_id.in_(scene_ids)
            )
        )
        session.execute(delete(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids)))
        session.execute(delete(SceneBundle).where(SceneBundle.scene_id.in_(scene_ids)))
    chapter_ids = [
        row
        for row in session.execute(
            select(ChapterGoal.chapter_id).where(
                ChapterGoal.project_id.in_(FIXTURE_WORK_IDS)
            )
        ).scalars()
    ]
    if chapter_ids:
        session.execute(
            delete(ChapterState).where(ChapterState.chapter_id.in_(chapter_ids))
        )

    session.execute(delete(SceneCard).where(SceneCard.project_id.in_(FIXTURE_WORK_IDS)))
    session.execute(delete(ChapterGoal).where(ChapterGoal.project_id.in_(FIXTURE_WORK_IDS)))
    for model in (
        SnowflakeSceneTriageItem,
        SnowflakeRevisionLink,
        SnowflakeScenePlan,
        SnowflakeCharacterPlan,
        SnowflakeAssistantTurn,
        SnowflakeStepRun,
        SnowflakeArtifact,
        LibraryRelation,
        LibraryEntity,
        TimelineEvent,
        StoryCharacter,
        OutlinePlan,
        ProjectWritingStats,
    ):
        session.execute(delete(model).where(model.project_id.in_(FIXTURE_WORK_IDS)))
    session.execute(delete(StoryProject).where(StoryProject.project_id.in_(FIXTURE_WORK_IDS)))
    session.flush()


def seed_fixture_works(session: Session) -> list[str]:
    cleanup_fixture_works(session)
    catalogs = json.loads(_CATALOG_JSON.read_text(encoding="utf-8"))
    now_sh = datetime.now(WRITING_STATS_TZ)
    yesterday = (now_sh - timedelta(days=1)).date().isoformat()

    _seed_work(
        session,
        project_id="work-a",
        title="样例长卷",
        genre="悬疑 · 长篇",
        mark="样",
        accent="crimson",
        synopsis_line="样例作品甲：用于测试与端到端验证的长篇结构样例，正文与设定均为占位文本。",
        target_word_count=120000,
        target_chapter_count=24,
        words_target_daily=1500,
        catalog_chapters=catalogs["work-a"],
        steps=_WORK_A_STEPS,
        resume_lines=_WORK_A_RESUME_LINES,
        paused_days_ago=3,
        words_total=38420,
        streak_days=6,
        streak_last_day=yesterday,
    )

    _seed_work(
        session,
        project_id="work-b",
        title="样例短卷",
        genre="年代 · 家族",
        mark="例",
        accent="gold",
        synopsis_line="样例作品乙：用于测试与端到端验证的短篇结构样例，正文与设定均为占位文本。",
        target_word_count=100000,
        target_chapter_count=20,
        words_target_daily=1200,
        catalog_chapters=catalogs["work-b"],
        steps=_WORK_B_STEPS,
        resume_lines=_WORK_B_RESUME_LINES,
        paused_days_ago=1,
        words_total=12600,
        streak_days=0,
        streak_last_day=None,
    )
    # 项目列表按 created_at 倒序 —— 把 work-b 拨早 1 秒，保证 work-a 居首。
    work_a = session.get(StoryProject, "work-a")
    work_b = session.get(StoryProject, "work-b")
    if work_a and work_b and work_b.created_at >= work_a.created_at:
        work_b.created_at = (
            datetime.fromisoformat(work_a.created_at) - timedelta(seconds=1)
        ).isoformat()
    session.flush()
    return list(FIXTURE_WORK_IDS)


def _seed_work(
    session: Session,
    *,
    project_id: str,
    title: str,
    genre: str,
    mark: str,
    accent: str,
    synopsis_line: str,
    target_word_count: int,
    target_chapter_count: int,
    words_target_daily: int,
    catalog_chapters: list[dict],
    steps: dict[str, str],
    resume_lines: list[str],
    paused_days_ago: int,
    words_total: int,
    streak_days: int,
    streak_last_day: str | None,
) -> None:
    project = StoryProject(
        project_id=project_id,
        title=title,
        genre=genre,
        mark=mark,
        accent=accent,
        synopsis_line=synopsis_line,
        target_word_count=target_word_count,
        target_chapter_count=target_chapter_count,
        words_target_daily=words_target_daily,
        outline_text=synopsis_line,
        planning_mode="snowflake",
        snowflake_workflow_mode="explore",
        approved_chapter_ids_json=[],
    )
    session.add(project)
    session.flush()

    # 目录：与一次性迁移同一条代码路径（带「待写」占位章的过滤）
    chapters = [c for c in catalog_chapters if str(c.get("title") or "").strip() not in ("", "（待写）")]
    catalog = CatalogService(session)
    catalog.import_catalog(project_id, {"chapters": chapters})

    # 当前章的在写场景（state=writing）挂正文草稿，resume 卡读其末两行
    writing_scene = None
    if project.current_chapter_id:
        writing_scene = session.execute(
            select(SceneCard).where(
                SceneCard.chapter_id == project.current_chapter_id,
                SceneCard.state == "writing",
                SceneCard.trashed_flag == 0,
            ).order_by(SceneCard.scene_seq.asc())
        ).scalars().first()
    if writing_scene is not None:
        paused_at = (datetime.now(WRITING_STATS_TZ) - timedelta(days=paused_days_ago)).isoformat()
        content = _prose(resume_lines, int(writing_scene.words_current or 0) or 800)
        draft = AuthorDraft(
            draft_id=f"author_draft_scene_{writing_scene.scene_id}_fixture",
            object_type="scene",
            object_id=writing_scene.scene_id,
            source_text_ref="test_fixture",
            content=content,
            revision_no=1,
            status="current",
            created_by="test_fixture",
            updated_by="test_fixture",
        )
        session.add(draft)
        session.flush()
        draft.updated_at = paused_at
        writing_scene.words_current = count_words(content)
        if not project.current_chapter_id:
            project.current_chapter_id = writing_scene.chapter_id

    for step_key, status in steps.items():
        session.add(
            SnowflakeStepRun(
                step_run_id=f"{project_id}-step-{step_key}",
                project_id=project_id,
                step_key=step_key,
                version=1,
                status=status,
                draft_json={"summary": "夹具数据"},
                approved_at=utcnow() if status == "approved" else None,
            )
        )
    session.flush()

    if project_id == "work-a":
        _seed_work_a_review_cards(session)
        _seed_work_a_library(session)

    WritingStatsService(session).seed_stats(
        project_id,
        words_total=words_total,
        streak_days=streak_days,
        streak_last_day=streak_last_day,
    )


def _seed_work_a_review_cards(session: Session) -> None:
    """审阅卡夹具（effect 指向真实章节行；dedupe 幂等）。"""
    from novel_system.db.models import ReviewItem
    from novel_system.services.review_cards import ReviewCardService

    session.execute(
        delete(ReviewItem).where(
            ReviewItem.project_id == "work-a", ReviewItem.item_type == "fe_card"
        )
    )
    session.flush()
    chapters = session.execute(
        select(ChapterGoal)
        .where(ChapterGoal.project_id == "work-a", ChapterGoal.trashed_flag == 0)
        .order_by(ChapterGoal.display_order.asc())
    ).scalars().all()
    by_no = {index + 1: chapter for index, chapter in enumerate(chapters)}
    cards = ReviewCardService(session)
    # import_catalog 会把显式 current 章之前的历史前缀规范化为 approved；
    # 可执行的夹具 effect 必须指向 current 章，不能再改已锁定的第 6/7 章。
    if 8 in by_no:
        cards.create_card(
            {
                "project_id": "work-a",
                "kind": "decision",
                "priority": 2,
                "title": "第 8 章标题在两个候选间未定",
                "source": "章节编排",
                "where": "第 8 章 · 标题",
                "detail": "「候选标题一」与「候选标题二」二选一。选定后会直接改写目录里第 8 章的标题。",
                "options": ["候选标题一", "候选标题二"],
                "dedupe_key": "fixture:work-a:ch08-title",
                "actions": [
                    {"label": "用「候选标题一」", "intent": "primary", "op": "resolve",
                     "effect": {"type": "rename_chapter", "chapter_id": by_no[8].chapter_id, "title": "候选标题一"}},
                    {"label": "用「候选标题二」", "intent": "ghost", "op": "resolve",
                     "effect": {"type": "rename_chapter", "chapter_id": by_no[8].chapter_id, "title": "候选标题二"}},
                    {"label": "再想想", "intent": "quiet", "op": "snooze"},
                ],
            },
            actor_ref="test_fixture",
        )
    if 8 in by_no:
        cards.create_card(
            {
                "project_id": "work-a",
                "kind": "qc",
                "priority": 2,
                "title": "第 8 章节奏过快，建议补一段反应场景",
                "source": "文学质检",
                "where": "第 8 章 · SC 03 之后",
                "detail": "连续三个主动场景之间没有喘息。采纳会直接在目录第 8 章 SC 03 后插入一个待写的反应场。",
                "dedupe_key": "fixture:work-a:ch08-reaction",
                "actions": [
                    {"label": "去章节编排看结构", "intent": "primary", "op": "nav", "nav_to": "author"},
                    {"label": "采纳 · 插入反应场", "intent": "ghost", "op": "resolve",
                     "effect": {"type": "insert_scene", "chapter_id": by_no[8].chapter_id, "at": 3,
                                "scene": {"title": "样例反应场", "kind": "reactive", "state": "todo",
                                          "brief": {"reaction": "样例反应：消化上一场的发现", "dilemma": "样例两难", "decision": "（待规划）"}}}},
                    {"label": "忽略", "intent": "quiet", "op": "resolve"},
                ],
            },
            actor_ref="test_fixture",
        )
    cards.create_card(
        {
            "project_id": "work-a",
            "kind": "risk",
            "priority": 2,
            "title": "时间线：第 3 章与第 5 章季节描写不一致",
            "source": "时间线",
            "where": "第 3 章 → 第 5 章",
            "detail": "第 3 章与第 5 章的季节锚点冲突，需统一。",
            "dedupe_key": "fixture:work-a:season",
            "actions": [
                {"label": "打开时间线", "intent": "primary", "op": "nav", "nav_to": "library"},
                {"label": "标记为已核", "intent": "quiet", "op": "resolve"},
            ],
        },
        actor_ref="test_fixture",
    )
    cards.create_card(
        {
            "project_id": "work-a",
            "kind": "note",
            "priority": 3,
            "title": "批注 · 核心意象是否前后呼应",
            "source": "写作房间",
            "where": "第 8 章 · 第 12 段",
            "detail": "样例批注：开篇的核心意象在结尾是否应再次响起，形成回环？",
            "dedupe_key": "fixture:work-a:note-motif",
            "actions": [
                {"label": "回到该段", "intent": "primary", "op": "nav", "nav_to": "writer", "nav_scene": "ch08s3"},
                {"label": "标记已读", "intent": "quiet", "op": "resolve"},
            ],
        },
        actor_ref="test_fixture",
    )
    cards.create_card(
        {
            "project_id": "work-a",
            "kind": "qc",
            "priority": 3,
            "title": "核心意象词出现 47 次，可能过载",
            "source": "文学质检",
            "where": "全书 · 用词",
            "detail": "核心意象高频复现有记忆点，但密度偏高易显刻意。可在非关键段落用近义替换 8–10 处。",
            "dedupe_key": "fixture:work-a:word-overload",
            "actions": [
                {"label": "在深改姿态里看", "intent": "primary", "op": "nav", "nav_to": "writer", "nav_posture": "deep"},
                {"label": "知道了", "intent": "quiet", "op": "resolve"},
            ],
        },
        actor_ref="test_fixture",
    )
    session.flush()
