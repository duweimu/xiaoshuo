from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    JSON,
    CheckConstraint,
    Computed,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.accounting_contract import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.db.base import Base


_utcnow_lock = threading.Lock()
_utcnow_last = datetime.min.replace(tzinfo=UTC)


def utcnow() -> str:
    """进程内严格单调的 UTC ISO 时间戳。

    Windows 时钟粒度粗，连续插入常落入同一 tick，按 created_at 排序会
    退化为随机主键序；同 tick 时微秒 +1 兜底，保证排序确定。
    """
    global _utcnow_last
    with _utcnow_lock:
        now = datetime.now(UTC)
        if now <= _utcnow_last:
            now = _utcnow_last + timedelta(microseconds=1)
        _utcnow_last = now
        return now.isoformat()


class StoryProject(Base):
    __tablename__ = "story_projects"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    genre: Mapped[str | None] = mapped_column(String, nullable=True)
    target_word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_chapter_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # FE-ALIGN P2: 作品档案字段（原型 WsWorks 作品对象 mark/accent/sub/今日目标）。
    mark: Mapped[str | None] = mapped_column(String, nullable=True)
    accent: Mapped[str | None] = mapped_column(String, nullable=True)
    synopsis_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    words_target_daily: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outline_text: Mapped[str] = mapped_column(Text)
    planning_mode: Mapped[str] = mapped_column(String, default="outline_driven")
    snowflake_schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    snowflake_workflow_mode: Mapped[str] = mapped_column(String, default="strict")
    status: Mapped[str] = mapped_column(String, default="outline_draft")
    active_outline_plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_chapter_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    # FE-ALIGN P4: 作品级软删（沿用章/场景 trash 的列名约定；级联只动可见性不动数据）
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ProjectWritingStats(Base):
    """FE-ALIGN P2: 每个项目一行的写作统计（D2：服务端计算，Asia/Shanghai）。

    today/streak 规则照抄原型 ws-catalog.jsx 的 catAddToday/catBumpStreak/
    catEffectiveStreak：当天首次正向增量记账；昨天也写过 +1 否则重记 1；
    断更超一天展示为 0（展示态在服务层算，不落库）。
    """

    __tablename__ = "project_writing_stats"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("story_projects.project_id"), primary_key=True
    )
    words_total: Mapped[int] = mapped_column(Integer, default=0)
    day: Mapped[str | None] = mapped_column(String, nullable=True)
    words_today: Mapped[int] = mapped_column(Integer, default=0)
    streak_last_day: Mapped[str | None] = mapped_column(String, nullable=True)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class OutlinePlan(Base):
    __tablename__ = "outline_plans"

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeArtifact(Base):
    __tablename__ = "snowflake_artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # P0-3: per-upstream content signatures captured at approval ("what I consumed,
    # at what version"). Powers dependency/diff-aware staleness instead of marking
    # every downstream step stale on any upstream change.
    consumed_input_sigs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeStepRun(Base):
    __tablename__ = "snowflake_step_runs"

    step_run_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending_review")
    draft_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    health_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    input_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # P0-3: per-upstream content signatures captured at approval — see SnowflakeArtifact.
    consumed_input_sigs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)

    @property
    def artifact_json(self) -> dict[str, Any]:
        return self.draft_json or {}


class SnowflakeAssistantTurn(Base):
    __tablename__ = "snowflake_assistant_turns"

    turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    step_key: Mapped[str] = mapped_column(String)
    focus_scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_message: Mapped[str] = mapped_column(Text)
    reply: Mapped[str] = mapped_column(Text)
    suggestions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    candidate_label: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_patch_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    source: Mapped[str] = mapped_column(String, default="fallback")
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SnowflakeCharacterPlan(Base):
    __tablename__ = "snowflake_character_plans"

    character_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    character_id: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    synopsis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    bible_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    source_step_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeChapterPlan(Base):
    """构思侧的「章」（P2）。

    在此之前章在整条雪花管线里没有归属者：09/10 步没有章字段，提示词让 LLM 把
    chapter_id 留空说「server assigns」，而服务端的起始值就是 ``{project_id}_CH01``
    并丢弃作者输入 —— 全书落进一章。唯一编了章的 07 长篇大纲只是四段自由文本，
    物化时根本不读。这张表把章变成有稳定身份、可编辑标题与章目标的一等规划行。
    """

    __tablename__ = "snowflake_chapter_plans"
    __table_args__ = (
        # 与场景计划同一条纪律（P1-1）：作者可改的序号/标题不能当身份，row_uid 才是。
        Index("ix_snowflake_chapter_plans_row_uid", "project_id", "row_uid", unique=True),
        Index("ix_snowflake_chapter_plans_seq", "project_id", "chapter_seq"),
    )

    chapter_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    row_uid: Mapped[str] = mapped_column(String)
    chapter_seq: Mapped[int] = mapped_column(Integer, default=1)
    act: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 灾一 / 灾二 / 灾三 —— 三幕结构的铰链，分章时与同标记的场互相锚定
    spine: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeScenePlan(Base):
    __tablename__ = "snowflake_scene_plans"
    __table_args__ = (
        # P1-1: immutable, system-minted row identity. Scene identity is no longer
        # derived from the author-editable ``scene_id`` — ``row_uid`` is the stable
        # anchor the staleness diff (P0-3) relies on.
        Index("ix_snowflake_scene_plans_row_uid", "project_id", "row_uid", unique=True),
        # P1-2: scene_id 是这一行对外的物化目标身份 —— SceneCard 直接拿它当主键。
        # 历史铸造规则是 f"{chapter_id}_SC{scene_seq:02d}"，创建时铸死而 scene_seq
        # 每次保存都按传入列表重算，于是「删一场再加一场」必然撞号；撞号后
        # _build_outline_plan 的 detail_by_id 与 approve_outline_plan 的
        # session.get(SceneCard, scene_id) 会双重覆盖，静默丢掉一场。铸造规则已改成
        # row_uid 基（见 snowflake_workspace._mint_scene_id），这条唯一索引是结构兜底：
        # 万一还有别的路径铸出重复 id，宁可硬报错也不要静默丢场。
        Index("ix_snowflake_scene_plans_scene_id", "project_id", "scene_id", unique=True),
        Index("ix_snowflake_scene_plans_chapter_plan_id", "chapter_plan_id"),
    )

    scene_plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    row_uid: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str] = mapped_column(String)
    # P2：章归属。chapter_id 从「创建时铸死的系统身份」降级为由分章结果推导的
    # 物化目标 id；真正的归属锚是 chapter_plan_id（NULL = 还没分章）。
    chapter_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "snowflake_chapter_plans.chapter_plan_id",
            name="fk_snowflake_scene_plans_chapter_plan_id",
        ),
        nullable=True,
    )
    chapter_id: Mapped[str] = mapped_column(String)
    # 作者在 09 场景列表上标的灾一/灾二/灾三。历史上前端从不上行、水合还硬写回 ""，
    # 标记每次刷新就丢；脊柱锚点分章要靠它，所以现在往返保真。
    spine: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_title: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_role: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_seq: Mapped[int] = mapped_column(Integer, default=1)
    pov_character_id: Mapped[str | None] = mapped_column(String, nullable=True)
    onstage_chars_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_type: Mapped[str] = mapped_column(String, default="proactive")
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_crucible: Mapped[str | None] = mapped_column(Text, nullable=True)
    goal: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict: Mapped[str | None] = mapped_column(Text, nullable=True)
    setback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reaction: Mapped[str | None] = mapped_column(Text, nullable=True)
    dilemma: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    beats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    must_include_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    tension_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    function_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    involved_foreshadowing_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    causal_prerequisite_scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_requirement: Mapped[str | None] = mapped_column(Text, nullable=True)
    downstream_obligations_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    target_length_band: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stale_accepted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    stale_accepted_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnosis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    # P1-3 收口：作者在场景列表里删掉的场。历史上 _sync_scene_plans 只增不删，
    # 被删的场永远留在库里、拿不到第 10 步细化、被诊断成 rewrite，于是用一个
    # 作者根本看不见的场把物化闸门永久堵死。软删（不物删）保留可恢复与可审计。
    removed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # 构思侧已删、但目录侧已经落库成 SceneCard（可能已有正文）的场：不能静默删，
    # 标记出来交作者裁决（Phase 2 的分章预览面板会把它列进告警区）。
    orphaned_flag: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeSceneTriageItem(Base):
    __tablename__ = "snowflake_scene_triage_items"

    triage_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    scene_plan_id: Mapped[str] = mapped_column(ForeignKey("snowflake_scene_plans.scene_plan_id"))
    scene_id: Mapped[str] = mapped_column(String)
    recommended_status: Mapped[str] = mapped_column(String, default="")
    manual_status: Mapped[str] = mapped_column(String, default="")
    effective_status: Mapped[str] = mapped_column(String, default="")
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    missing_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    fix_steps_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    repair_patch_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pressure_flags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    blocking: Mapped[int] = mapped_column(Integer, default=0)
    manual_override: Mapped[int] = mapped_column(Integer, default=0)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SnowflakeRevisionLink(Base):
    __tablename__ = "snowflake_revision_links"

    revision_link_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    source_step_key: Mapped[str] = mapped_column(String)
    source_step_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    affected_kind: Mapped[str] = mapped_column(String)
    affected_id: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="open")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    resolved_at: Mapped[str | None] = mapped_column(String, nullable=True)


class StoryCharacter(Base):
    __tablename__ = "story_characters"

    character_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    synopsis_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    bible_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class LibraryEntity(Base):
    """资料库实体(地点/物品/阵营/设定等非人物对象)。

    人物的权威实体是 StoryCharacter,不在此表重复;资料库聚合接口
    会把两者合并输出。kind 用字符串常量(不新增 Enum):
    location / item / faction / concept。
    """

    __tablename__ = "library_entities"
    __table_args__ = (Index("ix_library_entities_project", "project_id"),)

    entity_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    kind: Mapped[str] = mapped_column(String, default="concept")
    name: Mapped[str] = mapped_column(String)
    aliases_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    tags_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class LibraryRelation(Base):
    """资料库关系边。端点用带前缀的 ref:"character:<id>" 或 "entity:<id>"。"""

    __tablename__ = "library_relations"
    __table_args__ = (Index("ix_library_relations_project", "project_id"),)

    relation_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    from_ref: Mapped[str] = mapped_column(String)
    to_ref: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="related")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class TimelineEvent(Base):
    """FE-ALIGN P6: 资料库时间线事件（原型 ws-library 大事记 cat=events）。

    entity_refs_json 元素用带前缀 ref（"character:<id>" / "entity:<id>"），
    chapter_ref 是展示用章标记（如 "CH02" / "贯穿"），不强约束外键。
    """

    __tablename__ = "timeline_events"
    __table_args__ = (
        Index("ix_timeline_events_project", "project_id"),
        Index("ix_timeline_events_realized_canon_commit_id", "realized_canon_commit_id"),
        Index("ix_timeline_events_realized_scene_id", "realized_scene_id"),
        CheckConstraint(
            "event_mode IN ('planned','recorded')",
            name="ck_timeline_events_event_mode",
        ),
        CheckConstraint(
            "realization_status IN ('planned','realized')",
            name="ck_timeline_events_realization_status",
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    label: Mapped[str] = mapped_column(String)
    time_label: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_refs_json: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # planned = 作者意图；recorded = 仅作历史展示。正文真正兑现后通过
    # realized_canon_commit_id 指向不可变正史提交，不再靠双表内容猜测。
    event_mode: Mapped[str] = mapped_column(String, default="planned")
    realization_status: Mapped[str] = mapped_column(String, default="planned")
    realized_canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    realized_scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ChapterGoal(Base):
    __tablename__ = "chapter_goals"
    __table_args__ = (
        CheckConstraint(
            "display_order IS NULL OR display_order >= 0",
            name="ck_chapter_goals_display_order_nonnegative",
        ),
        Index(
            "ix_chapter_goals_project_display_order",
            "project_id",
            "display_order",
            "chapter_id",
        ),
        Index(
            "ux_chapter_goals_active_project_display_order",
            "project_id",
            "display_order",
            unique=True,
            sqlite_where=text(
                "trashed_flag = 0 AND project_id IS NOT NULL "
                "AND display_order IS NOT NULL"
            ),
            postgresql_where=text(
                "trashed_flag = 0 AND project_id IS NOT NULL "
                "AND display_order IS NOT NULL"
            ),
        ),
    )

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("story_projects.project_id"), nullable=True)
    outline_plan_id: Mapped[str | None] = mapped_column(ForeignKey("outline_plans.plan_id"), nullable=True)
    planned_scene_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mid_aggregate_enabled: Mapped[int] = mapped_column(Integer, default=0)
    chapter_goal: Mapped[str] = mapped_column(Text)
    # FE-ALIGN P3 目录统一：叙事卡（act/tension/pov/entry/exit/promise/drama/threads/title）、
    # 章状态、目标字数、显示顺序（混合 id 格式下不能依赖 chapter_id 字典序）。
    narrative_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String, default="planned")
    words_target: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    main_plot_push: Mapped[str | None] = mapped_column(Text, nullable=True)
    emotional_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    ending_effect: Mapped[str | None] = mapped_column(Text, nullable=True)
    must_not: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    writer_brief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneCard(Base):
    __tablename__ = "scene_cards"
    __table_args__ = (
        CheckConstraint(
            "scene_seq >= 1",
            name="ck_scene_cards_scene_seq_positive",
        ),
        Index(
            "ix_scene_cards_project_chapter_seq",
            "project_id",
            "chapter_id",
            "scene_seq",
            "scene_id",
        ),
        Index(
            "ux_scene_cards_active_chapter_scene_seq",
            "chapter_id",
            "scene_seq",
            unique=True,
            sqlite_where=text("trashed_flag = 0"),
            postgresql_where=text("trashed_flag = 0"),
        ),
    )

    scene_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    project_id: Mapped[str | None] = mapped_column(ForeignKey("story_projects.project_id"), nullable=True)
    outline_plan_id: Mapped[str | None] = mapped_column(ForeignKey("outline_plans.plan_id"), nullable=True)
    scene_seq: Mapped[int] = mapped_column(Integer)
    pov_character_id: Mapped[str | None] = mapped_column(String, nullable=True)
    onstage_chars_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    resolved_relation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_goal: Mapped[str] = mapped_column(Text)
    beats_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    must_include_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    forbidden_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_change: Mapped[str | None] = mapped_column(Text, nullable=True)
    hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    writer_brief_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    target_length_band: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_type: Mapped[str | None] = mapped_column(String, nullable=True)
    is_chapter_last: Mapped[int] = mapped_column(Integer, default=0)
    # FE-ALIGN P3：场景写作状态（todo/writing/done）与当前正文字数
    # （正文保存时更新；排序复用既有 scene_seq，不另建 display_order）。
    state: Mapped[str] = mapped_column(String, default="todo")
    words_current: Mapped[int] = mapped_column(Integer, default=0)
    # Writer-side reminders are authoritative author data, not disposable
    # browser cache. The revision is used as a compare-and-swap fence between
    # browsers/devices.
    author_notes: Mapped[str] = mapped_column(Text, default="")
    author_notes_revision_no: Mapped[int] = mapped_column(Integer, default=0)
    deep_review_decision_log_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    deep_review_ignored_keys_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    deep_review_preferences_revision_no: Mapped[int] = mapped_column(Integer, default=0)
    # §16 "breathing gap" — author-facing slider; 0.0=free-flow, 1.0=full-rigor, NULL=auto (criticality-based)
    constraint_intensity: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    trashed_flag: Mapped[int] = mapped_column(Integer, default=0)
    trashed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    trashed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    voice_profile_id: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    character_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class RelationProfile(Base):
    __tablename__ = "relation_profiles"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    relation_profile_id: Mapped[str] = mapped_column(String)
    left_character_id: Mapped[str] = mapped_column(String)
    right_character_id: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneRunState(Base):
    __tablename__ = "scene_run_states"
    __table_args__ = (
        CheckConstraint(
            "scene_tokens_reserved >= 0",
            name="ck_scene_run_states_tokens_reserved_nonnegative",
        ),
        CheckConstraint(
            "provider_attempts_used >= 0",
            name="ck_scene_run_states_provider_attempts_used_nonnegative",
        ),
        CheckConstraint(
            "provider_attempt_budget >= 0",
            name="ck_scene_run_states_provider_attempt_budget_nonnegative",
        ),
    )

    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"), primary_key=True)
    scene_status: Mapped[str] = mapped_column(String, default="ready")
    current_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    current_neutral_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_style_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 治理 §4.3：最近有效正文指针——与 current_* 不同，失败/重写路径不清空，
    # 任何后续失败都能回退到该版本（仅项目级运行时失效才重置）
    latest_valid_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_human_review_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_qc_report_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bundle_build_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_partial_rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    hard_full_rewrite_count: Mapped[int] = mapped_column(Integer, default=0)
    soft_patch_count: Mapped[int] = mapped_column(Integer, default=0)
    total_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt_budget: Mapped[int] = mapped_column(Integer, default=4)
    repeat_issue_key: Mapped[str | None] = mapped_column(String, nullable=True)
    repeat_issue_count: Mapped[int] = mapped_column(Integer, default=0)
    # §6 dispersion signal — last Best-of-N candidate Jaccard dispersion (0.0–1.0)
    candidate_dispersion_score: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # §6 criticality classification result for this run
    criticality_level: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    criticality_reasons_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=None)
    # Wave 3（治理 §5.5/§6.1）：运行策略 + 场景 token 预算（与 attempt_budget
    # 次数预算双轨）。预算按场景生命周期累计，自动流程不得重置（§7.12），
    # 扩容唯一入口是作者显式 topup（留审计）。
    run_policy: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    scene_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    scene_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    scene_tokens_reserved: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    scene_budget_basis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        default=None,
    )
    provider_attempts_used: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    provider_attempt_budget: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_PROVIDER_ATTEMPT_BUDGET,
        server_default=str(DEFAULT_PROVIDER_ATTEMPT_BUDGET),
    )
    active_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_execution_status: Mapped[str | None] = mapped_column(String, nullable=True)
    run_checkpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    run_checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    active_run_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 作者稿提升为权威正文后，叙事事件是否已明确与当前 FinalScene 对齐。
    # v1 只允许作者显式确认 facts_unchanged；需要事件重建的稿件不得静默放行。
    narrative_sync_status: Mapped[str] = mapped_column(String, default="synced")
    narrative_sync_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ChapterState(Base):
    __tablename__ = "chapter_states"

    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"), primary_key=True)
    current_phase: Mapped[str] = mapped_column(String, default="planning")
    chapter_passed_scene_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_backfill_pending_count: Mapped[int] = mapped_column(Integer, default=0)
    mid_aggregate_enabled_effective: Mapped[int] = mapped_column(Integer, default=0)
    aggregate_block_reason: Mapped[str] = mapped_column(String, default="none")
    manual_hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_interim_memory_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_final_memory_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SceneBundle(Base):
    __tablename__ = "scene_bundles"
    __table_args__ = (Index("ix_scene_bundles_scene", "scene_id"),)

    bundle_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chapter_goals.chapter_id",
            name="fk_scene_bundles_chapter_id",
        )
    )
    execution_mode: Mapped[str] = mapped_column(String, default="P2")
    bundle_snapshot_hash: Mapped[str] = mapped_column(String)
    frozen_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneBlueprint(Base):
    __tablename__ = "scene_blueprints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','accepted','superseded')",
            name="ck_scene_blueprints_status",
        ),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_scene_blueprints_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_scene_blueprints_chapter_id")
    )
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    blueprint_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneExecutionContract(Base):
    __tablename__ = "scene_execution_contracts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','blocked','stale','superseded')",
            # 约束名以迁移 0027 的冻结 DDL 为准（生产库带迁移名，改 ORM 侧对齐，不新开迁移）
            name="ck_scene_execution_contract_status",
        ),
    )

    contract_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey(
            "scene_cards.scene_id",
            name="fk_scene_execution_contracts_scene_id",
        )
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey(
            "chapter_goals.chapter_id",
            name="fk_scene_execution_contracts_chapter_id",
        )
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "story_projects.project_id",
            name="fk_scene_execution_contracts_project_id",
        ),
        nullable=True,
    )
    contract_version: Mapped[str] = mapped_column(String, default="scene_execution_contract_v1")
    source_snapshot_hash: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    missing_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    created_by: Mapped[str] = mapped_column(String, default="scene_execution")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class GenerationPlanningArtifact(Base):
    __tablename__ = "generation_planning_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('character_pressure_blueprint','chapter_story_architecture')",
            name="ck_generation_planning_artifacts_type",
        ),
        CheckConstraint("object_type IN ('scene','chapter')", name="ck_generation_planning_artifacts_object_type"),
        CheckConstraint("status IN ('active','superseded')", name="ck_generation_planning_artifacts_status"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_type: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    created_by: Mapped[str] = mapped_column(String, default="near_final_planning")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(
            "estimated_tokens >= 0",
            name="ck_llm_calls_estimated_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_tokens >= 0",
            name="ck_llm_calls_reserved_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens >= 0",
            name="ck_llm_calls_budget_charged_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens <= reserved_tokens",
            name="ck_llm_calls_budget_charged_within_reservation",
        ),
        CheckConstraint(
            "accounting_status IN ('reserved','settled','failed','released','rejected','usage_exceeds_reservation')",
            name="ck_llm_calls_accounting_status",
        ),
        Index("ix_llm_calls_scene_created", "scene_id", "created_at"),
        Index("ix_llm_calls_scope_created", "scope_type", "scope_id", "created_at"),
        Index("ix_llm_calls_run_job", "run_job_id"),
        Index("ix_llm_calls_execution_step", "execution_id", "execution_step_key"),
        Index(
            "uq_llm_calls_execution_step_claim",
            "execution_id",
            "execution_step_key",
            unique=True,
            sqlite_where=text(
                "execution_id IS NOT NULL AND execution_step_key IS NOT NULL "
                "AND NOT (request_dispatched_at IS NULL "
                "AND accounting_status IN ('released','rejected'))"
            ),
            postgresql_where=text(
                "execution_id IS NOT NULL AND execution_step_key IS NOT NULL "
                "AND NOT (request_dispatched_at IS NULL "
                "AND accounting_status IN ('released','rejected'))"
            ),
        ),
        Index("ix_llm_calls_accounting_status", "accounting_status"),
    )

    llm_call_id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning_level: Mapped[str | None] = mapped_column(String, nullable=True)
    native_reasoning_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credential_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    step: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_payload_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_payload_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    scope_type: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    run_job_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_step_key: Mapped[str | None] = mapped_column(String, nullable=True)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    budget_charged_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usage_is_estimate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    accounting_status: Mapped[str] = mapped_column(
        String,
        default="reserved",
        server_default="reserved",
    )
    request_dispatched_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class LlmCallAttempt(Base):
    __tablename__ = "llm_call_attempts"
    __table_args__ = (
        UniqueConstraint(
            "llm_call_id",
            "provider_attempt_no",
            name="uq_llm_call_attempts_call_ordinal",
        ),
        CheckConstraint(
            "provider_attempt_no >= 0",
            name="ck_llm_call_attempts_provider_attempt_no_nonnegative",
        ),
        CheckConstraint(
            "request_max_output_tokens >= 0",
            name="ck_llm_call_attempts_request_max_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "prompt_tokens >= 0",
            name="ck_llm_call_attempts_prompt_tokens_nonnegative",
        ),
        CheckConstraint(
            "completion_tokens >= 0",
            name="ck_llm_call_attempts_completion_tokens_nonnegative",
        ),
        CheckConstraint(
            "total_tokens >= 0",
            name="ck_llm_call_attempts_total_tokens_nonnegative",
        ),
        CheckConstraint(
            "estimated_tokens >= 0",
            name="ck_llm_call_attempts_estimated_tokens_nonnegative",
        ),
        CheckConstraint(
            "reserved_tokens >= 0",
            name="ck_llm_call_attempts_reserved_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens >= 0",
            name="ck_llm_call_attempts_budget_charged_tokens_nonnegative",
        ),
        CheckConstraint(
            "budget_charged_tokens <= reserved_tokens",
            name="ck_llm_call_attempts_budget_charged_within_reservation",
        ),
        CheckConstraint(
            "latency_ms >= 0",
            name="ck_llm_call_attempts_latency_ms_nonnegative",
        ),
        CheckConstraint(
            "accounting_status IN ('reserved','settled','failed','released','rejected','usage_exceeds_reservation')",
            name="ck_llm_call_attempts_accounting_status",
        ),
        CheckConstraint(
            "dispatch_kind IN ('initial','transport_retry','response_parse_retry','api_mode_degrade','structured_output_degrade','missing_text_degrade','system_probe')",
            name="ck_llm_call_attempts_dispatch_kind",
        ),
        Index("ix_llm_call_attempts_call_status", "llm_call_id", "accounting_status"),
    )

    attempt_id: Mapped[str] = mapped_column(String, primary_key=True)
    llm_call_id: Mapped[str] = mapped_column(ForeignKey("llm_calls.llm_call_id"))
    provider_attempt_no: Mapped[int] = mapped_column(Integer)
    dispatch_kind: Mapped[str] = mapped_column(String)
    request_max_output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    provider_request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    budget_charged_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    usage_is_estimate: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="1",
    )
    accounting_status: Mapped[str] = mapped_column(String)
    request_dispatched_at: Mapped[str | None] = mapped_column(String, nullable=True)
    settled_at: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneDraft(Base):
    __tablename__ = "scene_drafts"
    __table_args__ = (Index("ix_scene_drafts_scene", "scene_id"),)

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_scene_drafts_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_scene_drafts_chapter_id")
    )
    stage: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    content: Mapped[str] = mapped_column(Text)
    source_bundle_id: Mapped[str] = mapped_column(String)
    source_bundle_hash: Mapped[str] = mapped_column(String)
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class QcReport(Base):
    __tablename__ = "qc_reports"
    __table_args__ = (Index("ix_qc_reports_scene", "scene_id"),)

    qc_report_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_qc_reports_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_qc_reports_chapter_id"),
        nullable=True,
    )
    qc_type: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    source_draft_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String, nullable=True)
    pass_flag: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_action: Mapped[str | None] = mapped_column(String, nullable=True)
    issues_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    rewrite_brief_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class WriterEvaluation(Base):
    __tablename__ = "writer_evaluations"

    evaluation_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    rubric_id: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluator_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    lens: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_spans_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    source_blueprint_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String, nullable=True)
    auto_rewrite_eligible: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_field_refs_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    promotion_blockers_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    findings_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision_brief_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    requires_human_review: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="completed")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class RevisionCandidate(Base):
    __tablename__ = "revision_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_revision_candidates_status",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_type: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    proposed_text: Mapped[str] = mapped_column(Text)
    instruction_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    diff_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    patches_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=list)
    apply_mode: Mapped[str] = mapped_column(String, default="manual_only")
    target_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="writer_engine")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class PassagePatchCandidate(Base):
    __tablename__ = "passage_patch_candidates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_passage_patch_candidates_status",
        ),
        CheckConstraint(
            "author_decision IN ('pending','accepted','rejected','regenerate')",
            name="ck_passage_patch_candidates_author_decision",
        ),
    )

    patch_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    target_text_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    source_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    quality_signal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_excerpt: Mapped[str] = mapped_column(Text)
    issue_dimension: Mapped[str] = mapped_column(String)
    candidate_category: Mapped[str] = mapped_column(String, default="local_patch")
    target_range_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    revision_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    preference_tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    inserted_into_author_draft: Mapped[int] = mapped_column(Integer, default=0)
    replacement_options_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_only: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision: Mapped[str] = mapped_column(String, default="pending")
    selected_option_id: Mapped[str | None] = mapped_column(String, nullable=True)
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="writer_deep_review")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorPreferenceProfile(Base):
    __tablename__ = "author_preference_profiles"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global','genre','project','chapter')",
            name="ck_author_preference_profiles_scope_type",
        ),
        CheckConstraint(
            "status IN ('draft','approved','rejected','superseded')",
            name="ck_author_preference_profiles_status",
        ),
        CheckConstraint(
            "runtime_eligible IN (0,1)",
            name="ck_author_preference_profiles_runtime_eligible",
        ),
    )

    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, default="global")
    scope_ref_id: Mapped[str] = mapped_column(String, default="global")
    status: Mapped[str] = mapped_column(String, default="draft")
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_patch_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String, default="writer_deep_review")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraft(Base):
    __tablename__ = "author_drafts"
    __table_args__ = (
        CheckConstraint("object_type IN ('scene','chapter','project')", name="ck_author_drafts_object_type"),
        CheckConstraint("status IN ('current','superseded','archived')", name="ck_author_drafts_status"),
    )

    draft_id: Mapped[str] = mapped_column(String, primary_key=True)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    source_text_ref: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    # 草稿保存与权威正文提升是两个独立动作；这两个字段记录最近一次成功提升，
    # 也为 promote-canonical 提供 revision + FinalScene 双重 CAS 的持久化证据。
    last_promoted_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_promoted_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="current")
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    updated_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraftProposal(Base):
    __tablename__ = "author_draft_proposals"
    __table_args__ = (
        CheckConstraint("object_type IN ('scene','chapter','project')", name="ck_author_draft_proposals_object_type"),
        CheckConstraint(
            "status IN ('candidate','accepted','rejected','superseded')",
            name="ck_author_draft_proposals_status",
        ),
    )

    proposal_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    proposal_type: Mapped[str] = mapped_column(String, default="scene_draft")
    proposal_source: Mapped[str] = mapped_column(String, default="single_request")
    content: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_range_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    before_text_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    replacement_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_kind: Mapped[str] = mapped_column(String, default="whole_draft")
    source_evaluation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    merge_status: Mapped[str] = mapped_column(String, default="pending")
    status: Mapped[str] = mapped_column(String, default="candidate")
    author_decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="author_draft_proposal")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AuthorDraftEvent(Base):
    __tablename__ = "author_draft_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'created','edited','candidate_inserted','candidate_saved','candidate_rejected',"
            "'proposal_applied','proposal_rejected'"
            ")",
            name="ck_author_draft_events_type",
        ),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_id: Mapped[str] = mapped_column(String)
    event_type: Mapped[str] = mapped_column(String)
    patch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    option_id: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class AuthorDraftRevision(Base):
    """正文修订快照（FE-ALIGN F2）：每次 revision_no 推进时存一行完整内容。"""

    __tablename__ = "author_draft_revisions"
    __table_args__ = (
        UniqueConstraint("draft_id", "revision_no", name="uq_author_draft_revisions_draft_rev"),
        Index("ix_author_draft_revisions_draft", "draft_id"),
    )

    draft_revision_id: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String)
    revision_no: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    words: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String, default="edited")
    created_by: Mapped[str] = mapped_column(String, default="author_draft")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class FinalScene(Base):
    __tablename__ = "final_scenes"
    __table_args__ = (
        Index("ix_final_scenes_scene", "scene_id"),
        Index("ix_final_scenes_chapter", "chapter_id"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_final_scenes_scene_id")
    )
    chapter_id: Mapped[str] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_final_scenes_chapter_id")
    )
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="approved")
    source_bundle_id: Mapped[str] = mapped_column(String)
    source_bundle_hash: Mapped[str] = mapped_column(String)
    source_kind: Mapped[str] = mapped_column(String, default="generation")
    source_author_draft_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_author_draft_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    superseded_by_final_scene_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, default="system")
    generation_llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class SceneMemory(Base):
    __tablename__ = "scene_memories"
    __table_args__ = (
        Index("ix_scene_memories_scene", "scene_id"),
        Index("ix_scene_memories_chapter", "chapter_id"),
    )

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    carry_notes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_bundle_id: Mapped[str] = mapped_column(String)
    final_scene_row_id: Mapped[str] = mapped_column(String)
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="direct_read")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class ChapterMemory(Base):
    __tablename__ = "chapter_memories"
    __table_args__ = (Index("ix_chapter_memories_chapter", "chapter_id"),)

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(String)
    aggregate_stage: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    # §2 summary tower: "事实从日志查，氛围从摘要读". memory_kind labels how the
    # content may be used downstream — "mixed" (legacy, both), "factual" (state
    # cross-reference only), "atmosphere" (tone/mood far-horizon, never as facts).
    memory_kind: Mapped[str] = mapped_column(String, default="mixed")
    source_review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=0)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="stage_blocked")
    effective_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class VolumeSummary(Base):
    """§2 summary tower — volume/book-level far-horizon ATMOSPHERE summary.

    Blueprint §2: the summary tower is a read-only auxiliary layer supplying
    far-horizon tone/atmosphere context. It must NEVER be a fact-bearing source —
    facts are projected from the event log. This rolls up chapter memories into a
    volume-level digest used as far-horizon mood context for generation.
    """
    __tablename__ = "volume_summaries"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String)
    volume_seq: Mapped[int] = mapped_column(Integer)
    chapter_id_start: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id_end: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    # Atmosphere-only far-horizon context (tone, mood, thematic arc). NOT facts.
    atmosphere_summary: Mapped[str] = mapped_column(Text, default="")
    # Optional structured factual digest derived from event log (state milestones).
    # 声明未实现：无生成路径，恒 NULL（蓝图 §2 的事实摘要尚未落地）。
    factual_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_flag: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligible: Mapped[int] = mapped_column(Integer, default=1)
    runtime_eligibility_basis: Mapped[str] = mapped_column(String, default="direct_read")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ChapterRollingNote(Base):
    __tablename__ = "chapter_rolling_notes"

    row_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(String, unique=True)
    chapter_id: Mapped[str] = mapped_column(String)
    source_scene_memory_row_id: Mapped[str] = mapped_column(String)
    note_text: Mapped[str] = mapped_column(Text)
    revision_no: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class AttemptTracker(Base):
    __tablename__ = "attempt_tracker"
    __table_args__ = (Index("ix_attempt_tracker_scene", "scene_id"),)

    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_attempt_tracker_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_attempt_tracker_chapter_id"),
        nullable=True,
    )
    step: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    source_bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class ChapterRunJob(Base):
    __tablename__ = "chapter_run_jobs"
    __table_args__ = (Index("ix_chapter_run_jobs_scene_created", "scene_id", "created_at"),)

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_chapter_run_jobs_chapter_id"),
        nullable=True,
    )
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_chapter_run_jobs_scene_id"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String)
    job_type: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class BackgroundRecoveryLease(Base):
    """Short database lease that elects one startup recovery scanner."""

    __tablename__ = "background_recovery_leases"

    lease_key: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String)
    lease_expires_at: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_review_items_status"),
        # onceTask: 同一作品同一 dedupe_key 只允许一张卡（NULL 不参与唯一性）。
        # 镜像迁移 0050 的唯一索引，使测试的 create_all 与生产迁移同样强制该唯一性。
        Index("ux_review_items_project_dedupe", "project_id", "dedupe_key", unique=True),
        # 审计 P-9 热路径索引（迁移 0060）
        Index("ix_review_items_project_state", "project_id", "state"),
        Index("ix_review_items_scene", "scene_id"),
    )

    review_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_type: Mapped[str] = mapped_column(String)
    target_collection: Mapped[str] = mapped_column(
        String,
        Computed(
            "CASE "
            "WHEN item_type = 'style_observation' THEN 'style_observations' "
            "WHEN item_type = 'style_rule_set' THEN 'style_rules' "
            "WHEN item_type = 'banned_rule_cluster' THEN 'banned_rule_clusters' "
            "WHEN item_type = 'narrative_pattern' THEN 'narrative_patterns' "
            "WHEN item_type = 'voice_card_candidate' THEN 'voice_cards' "
            "WHEN item_type = 'relation_card_candidate' THEN 'relation_cards' "
            "WHEN item_type = 'world_rule' THEN 'world_rules' "
            "WHEN item_type = 'calibration_candidate' THEN 'calibration_lines' "
            "WHEN item_type = 'foreshadow_open' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'foreshadow_touch' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'foreshadow_resolve' THEN 'foreshadow_tracker' "
            "WHEN item_type = 'scene_memory' THEN 'scene_memories' "
            "WHEN item_type = 'scene_summary' THEN 'scene_memories' "
            "WHEN item_type = 'chapter_summary' THEN 'chapter_memories' "
            "WHEN item_type = 'author_preference_profile' THEN 'author_preference_profiles' "
            "WHEN item_type = 'longform_structure_guidance' THEN 'longform_structure_guidance' "
            "ELSE 'review_items' END",
            persisted=True,
        ),
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    candidate_text: Mapped[str] = mapped_column(Text)
    candidate_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active_on_approve: Mapped[int] = mapped_column(Integer, default=1)
    materialize_status: Mapped[str] = mapped_column(String, default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, default=3)
    approved_item_row_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # FE-ALIGN P5: 待办收件箱卡片模型（原型 ws-review 五类卡；legacy 行这些列为 NULL，
    # 响应里把 status pending/approved/rejected 映射成统一 state open/resolved）。
    # 卡片行 item_type="fe_card"、status 恒 "pending"（CheckConstraint 兼容），生命周期走 state。
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    card_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    actions_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    snooze_until: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_action_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ReviewDerivedSnooze(Base):
    """FE-ALIGN P5: 实时派生待办的稍后记录（按内容指纹 id 存——指纹变化即重新浮现）。"""

    __tablename__ = "review_derived_snoozes"

    project_id: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    snooze_until: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class HumanReviewEvent(Base):
    __tablename__ = "human_review_events"
    __table_args__ = (
        Index("ix_human_review_events_scene", "scene_id"),
        Index("ix_human_review_events_status", "status"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id", name="fk_human_review_events_scene_id"),
        nullable=True,
    )
    chapter_id: Mapped[str | None] = mapped_column(
        ForeignKey("chapter_goals.chapter_id", name="fk_human_review_events_chapter_id"),
        nullable=True,
    )
    object_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    event_source: Mapped[str] = mapped_column(String, default="system")
    priority: Mapped[str] = mapped_column(String, default="normal")
    owner: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")
    allowed_actions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    result_status_map_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    default_action: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="started")
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    operation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String)
    object_type: Mapped[str] = mapped_column(String)
    object_ref: Mapped[str] = mapped_column(String)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class NarrativeEvent(Base):
    """Append-only narrative event log — the single source of truth for story state.

    Every fact about characters, locations, relationships, and information flow
    is recorded as an event tied to a scene. Character state at any point is
    reconstructed by replaying events up to that scene.
    """
    __tablename__ = "narrative_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, index=True)
    scene_id: Mapped[str] = mapped_column(String, index=True)
    chapter_id: Mapped[str] = mapped_column(String, index=True)
    scene_seq: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    fact_key: Mapped[str] = mapped_column(String)
    fact_value: Mapped[str] = mapped_column(String)
    confidence: Mapped[str] = mapped_column(String, default="high")
    causal_predecessor_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Blueprint §2: each event carries theme tags for theme-aware queries
    theme_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    # Blueprint §2: forward-pointing obligation IDs (foreshadow / causal obligations)
    obligation_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, default=list)
    source_text_excerpt: Mapped[str | None] = mapped_column(String, nullable=True)
    # Fail closed: unspecified writes are plans, never runtime canon. Extractor
    # rows explicitly use pending; only the canon service may promote to accepted.
    authority_status: Mapped[str] = mapped_column(String, default="planned")
    source_kind: Mapped[str] = mapped_column(String, default="legacy_plan")
    final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)

    __table_args__ = (
        Index("ix_narrative_events_entity_scene", "entity_id", "scene_seq"),
        Index("ix_narrative_events_project_scene", "project_id", "scene_seq"),
        Index(
            "ix_narrative_events_project_chapter_scene",
            "project_id",
            "chapter_id",
            "scene_id",
        ),
        Index(
            "ix_narrative_events_project_entity_scene",
            "project_id",
            "entity_id",
            "scene_id",
        ),
        Index(
            "ix_narrative_events_authority_project_scene",
            "authority_status",
            "project_id",
            "scene_id",
        ),
        Index("ix_narrative_events_final_scene", "final_scene_row_id"),
        Index("ix_narrative_events_canon_commit", "canon_commit_id"),
        CheckConstraint(
            "authority_status IN ('accepted','pending','rejected','planned','superseded')",
            name="ck_narrative_events_authority_status",
        ),
    )


class CanonCommit(Base):
    """正文事实经过作者/规则裁决后的不可变正史提交。"""

    __tablename__ = "canon_commits"
    __table_args__ = (
        Index(
            "ix_canon_commits_project_scene_final",
            "project_id",
            "scene_id",
            "final_scene_row_id",
        ),
        Index("ix_canon_commits_chapter", "chapter_id"),
        Index("ix_canon_commits_scene", "scene_id"),
        Index("ix_canon_commits_final_scene", "final_scene_row_id"),
        Index("ix_canon_commits_source_final_scene", "source_final_scene_row_id"),
        CheckConstraint(
            "status IN ('active','superseded')",
            name="ck_canon_commits_status",
        ),
        CheckConstraint(
            "commit_kind IN ('candidate_acceptance','author_verification','facts_unchanged')",
            name="ck_canon_commits_commit_kind",
        ),
    )

    commit_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    final_scene_row_id: Mapped[str] = mapped_column(ForeignKey("final_scenes.row_id"))
    final_content_hash: Mapped[str] = mapped_column(String)
    commit_kind: Mapped[str] = mapped_column(String, default="candidate_acceptance")
    candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="active")
    actor_ref: Mapped[str] = mapped_column(String, default="operator")
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class FactCandidate(Base):
    """从终稿抽取、尚未成为事实的可审计候选。"""

    __tablename__ = "fact_candidates"
    __table_args__ = (
        Index(
            "ix_fact_candidates_project_chapter_status",
            "project_id",
            "chapter_id",
            "status",
        ),
        Index("ix_fact_candidates_scene_status", "scene_id", "status"),
        Index("ix_fact_candidates_final_scene", "final_scene_row_id"),
        Index("ix_fact_candidates_chapter", "chapter_id"),
        Index("ix_fact_candidates_planned_timeline", "planned_timeline_event_id"),
        Index("ix_fact_candidates_canon_commit", "canon_commit_id"),
        UniqueConstraint("staged_event_id", name="ux_fact_candidates_staged_event"),
        CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded')",
            name="ck_fact_candidates_status",
        ),
        CheckConstraint(
            "entity_resolution_status IN ('exact','alias','ambiguous','unresolved','manual')",
            name="ck_fact_candidates_entity_resolution_status",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str] = mapped_column(ForeignKey("scene_cards.scene_id"))
    final_scene_row_id: Mapped[str] = mapped_column(ForeignKey("final_scenes.row_id"))
    staged_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("narrative_events.event_id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String)
    entity_type: Mapped[str] = mapped_column(String)
    raw_entity_ref: Mapped[str] = mapped_column(String)
    resolved_entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_resolution_status: Mapped[str] = mapped_column(String, default="unresolved")
    entity_candidates_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    fact_key: Mapped[str] = mapped_column(String)
    fact_value: Mapped[str] = mapped_column(Text)
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kind: Mapped[str] = mapped_column(String, default="prose_extraction")
    confidence: Mapped[str] = mapped_column(String, default="extracted")
    criticality: Mapped[str] = mapped_column(String, default="critical")
    planned_timeline_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("timeline_events.event_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    canon_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class ContinuitySnapshot(Base):
    """可重建的结构化连续性投影；原始正文仍由 FinalScene 保存。"""

    __tablename__ = "continuity_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "scope_type",
            "scope_id",
            name="ux_continuity_snapshots_scope",
        ),
        Index("ix_continuity_snapshots_chapter", "chapter_id", "scope_type"),
        Index("ix_continuity_snapshots_scene", "scene_id"),
        Index("ix_continuity_snapshots_final_scene", "final_scene_row_id"),
        Index("ix_continuity_snapshots_latest_commit", "latest_commit_id"),
        CheckConstraint(
            "scope_type IN ('scene','chapter')",
            name="ck_continuity_snapshots_scope_type",
        ),
        CheckConstraint(
            "status IN ('pending','complete','degraded','superseded')",
            name="ck_continuity_snapshots_status",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.project_id"))
    scope_type: Mapped[str] = mapped_column(String)
    scope_id: Mapped[str] = mapped_column(String)
    chapter_id: Mapped[str] = mapped_column(ForeignKey("chapter_goals.chapter_id"))
    scene_id: Mapped[str | None] = mapped_column(
        ForeignKey("scene_cards.scene_id"), nullable=True
    )
    final_scene_row_id: Mapped[str | None] = mapped_column(
        ForeignKey("final_scenes.row_id"), nullable=True
    )
    latest_commit_id: Mapped[str | None] = mapped_column(
        ForeignKey("canon_commits.commit_id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, default="pending")
    summary_text: Mapped[str] = mapped_column(Text, default="")
    state_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    knowledge_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    relationship_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    item_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    timeline_deltas_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    open_obligations_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    entity_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_commit_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class SystemConfigSnapshot(Base):
    __tablename__ = "system_config_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String, primary_key=True)
    category: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer)
    yaml_raw: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="draft")
    active_flag: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    activated_at: Mapped[str | None] = mapped_column(String, nullable=True)


class SystemSecret(Base):
    __tablename__ = "system_secrets"

    secret_id: Mapped[str] = mapped_column(String, primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    value_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    secret_type: Mapped[str] = mapped_column(String, default="generic")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Wave 5（结果闭环治理 §6.2）— 质量实验通道：匿名 A/B 人类盲评三张表。
# 实验通道**不写 FinalScene**，只写实验产物；实验失败不影响生产状态（§5.1）。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 第二阶段质量证据：隐藏题包只落不可逆哈希；生成结果、真人价值观测与
# 题材×场景功能策略分开存证。任何表都不保存隐藏答案或 rubric 正文。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Style Reference (v1.1) — 11 张表
# 参见 plans/style-reference-v1-1-fancy-shannon.md 与
# 《风格参考模块重构执行手册 v1.1》§4。
# ---------------------------------------------------------------------------


class StyleReferenceBook(Base):
    __tablename__ = "style_reference_books"
    __table_args__ = (
        UniqueConstraint("text_checksum", name="uq_style_reference_books_text_checksum"),
        Index("ix_style_reference_books_status_updated_at", "status", "updated_at"),
    )

    book_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    author_label: Mapped[str | None] = mapped_column(String, nullable=True)
    source_kind: Mapped[str] = mapped_column(String)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    cloud_policy: Mapped[str] = mapped_column(String)
    text_checksum: Mapped[str] = mapped_column(String)
    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceParagraph(Base):
    __tablename__ = "style_reference_paragraphs"
    __table_args__ = (
        Index(
            "ix_style_reference_paragraphs_book_type",
            "book_id",
            "paragraph_type",
        ),
        Index(
            "ix_style_reference_paragraphs_book_index",
            "book_id",
            "paragraph_index",
        ),
    )

    paragraph_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    paragraph_index: Mapped[int] = mapped_column(Integer)
    paragraph_type: Mapped[str] = mapped_column(String)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    classifier_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceRun(Base):
    __tablename__ = "style_reference_runs"
    __table_args__ = (
        Index("ix_style_reference_runs_book_status", "book_id", "status"),
        Index("ix_style_reference_runs_dispatch_state", "dispatch_state"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    status: Mapped[str] = mapped_column(String, default="pending")
    phase: Mapped[str] = mapped_column(String, default="ingest")
    # ``status`` describes the domain run while ``dispatch_state`` describes
    # durable background ownership.  Keeping them separate lets startup
    # recovery re-dispatch work that never started without pretending that a
    # partially executed extraction can be resumed safely.
    dispatch_state: Mapped[str] = mapped_column(String, default="completed")
    requested_layers_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceExtraction(Base):
    __tablename__ = "style_reference_extractions"
    __table_args__ = (
        Index(
            "ix_style_reference_extractions_book_layer_sub",
            "book_id",
            "layer",
            "sub_dimension",
        ),
        Index(
            "ix_style_reference_extractions_run_status",
            "run_id",
            "status",
        ),
    )

    extraction_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    layer: Mapped[str] = mapped_column(String)
    sub_dimension: Mapped[str] = mapped_column(String)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    validation_errors_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(String, default="extract")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceQuote(Base):
    __tablename__ = "style_reference_quotes"
    __table_args__ = (
        Index("ix_style_reference_quotes_book", "book_id"),
    )

    quote_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    # paragraph_id 可空:支持 anchor_kind=counter_example 的合成 quote 不指向真实段落
    paragraph_id: Mapped[str | None] = mapped_column(
        ForeignKey("style_reference_paragraphs.paragraph_id"), nullable=True
    )
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    quote_text: Mapped[str] = mapped_column(Text)
    illustrates_dims: Mapped[list[str]] = mapped_column(JSON, default=list)
    extracted_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceFinding(Base):
    __tablename__ = "style_reference_findings"
    __table_args__ = (
        Index(
            "ix_style_reference_findings_book_sub_kind",
            "book_id",
            "sub_dimension",
            "finding_kind",
        ),
        UniqueConstraint("review_id", name="uq_style_reference_findings_review_id"),
        # PR-3 hotfix 0038:UNIQUE 复合 4 列(原 3 列与 §6.5 0-8 条 obs 输出矛盾)
        # 详见 plans/style-reference-v1-1-fancy-shannon.md §"v1.2 文档修订清单 #8"
        UniqueConstraint(
            "extraction_id",
            "sub_dimension",
            "finding_kind",
            "statement_hash",
            name="uq_style_reference_findings_extract_sub_kind_hash",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("style_reference_extractions.extraction_id")
    )
    sub_dimension: Mapped[str] = mapped_column(String)
    finding_kind: Mapped[str] = mapped_column(String)
    statement: Mapped[str] = mapped_column(Text)
    # PR-3 hotfix 0038:statement 的 SHA256[:16],用于 UNIQUE 复合;应用层 / repository
    # 在 create_finding 时自动填充
    statement_hash: Mapped[str] = mapped_column(String)
    confidence: Mapped[str] = mapped_column(String, default="medium")
    # 立项 B — 合成时的基线置信度。NULL = 尚无用户反馈(confidence 即基线);
    # 首次反馈时由应用层回填为当时的 confidence,使反馈调档可重算/可逆。
    base_confidence: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceEvidence(Base):
    __tablename__ = "style_reference_evidences"
    __table_args__ = (
        UniqueConstraint(
            "finding_id",
            "quote_id",
            name="uq_style_reference_evidences_finding_quote",
        ),
    )

    evidence_id: Mapped[str] = mapped_column(String, primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("style_reference_findings.finding_id"))
    quote_id: Mapped[str] = mapped_column(ForeignKey("style_reference_quotes.quote_id"))
    anchor_kind: Mapped[str] = mapped_column(String)
    is_synthetic: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceProfile(Base):
    __tablename__ = "style_reference_profiles"
    __table_args__ = (
        Index(
            "ix_style_reference_profiles_book_status",
            "book_id",
            "status",
        ),
        Index("ix_style_reference_profiles_version_tag", "version_tag"),
    )

    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_finding_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    version_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceInjectionBinding(Base):
    __tablename__ = "style_reference_injection_bindings"
    __table_args__ = (
        Index(
            "ix_style_reference_injection_bindings_profile_scope_ref",
            "profile_id",
            "scope",
            "scope_ref_id",
        ),
        Index(
            "ix_style_reference_injection_bindings_task_type",
            "task_type",
        ),
        # 并发 apply 的「先查后建」竞态兜底:同 (profile, scope, scope_ref, task)
        # 不允许重复 binding(否则注入选取顺序不确定)
        UniqueConstraint(
            "profile_id",
            "scope",
            "scope_ref_id",
            "task_type",
            name="uq_style_reference_injection_bindings_target",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("style_reference_profiles.profile_id"))
    scope: Mapped[str] = mapped_column(String)
    scope_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_type: Mapped[str] = mapped_column(String)
    strategy: Mapped[str] = mapped_column(String)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceValidationReport(Base):
    __tablename__ = "style_reference_validation_reports"
    __table_args__ = (
        Index(
            "ix_style_reference_validation_reports_profile_target",
            "profile_id",
            "target_ref_id",
        ),
        Index(
            "ix_style_reference_validation_reports_verdict",
            "verdict",
        ),
        Index(
            "ix_style_reference_validation_reports_status",
            "status",
        ),
    )

    report_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("style_reference_profiles.profile_id"))
    target_kind: Mapped[str] = mapped_column(String)
    target_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    verdict: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="completed")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    quantitative_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    semantic_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    plagiarism_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    forbidden_hits_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    mode_executed: Mapped[str] = mapped_column(String, default="async_full")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceBannedTerm(Base):
    __tablename__ = "style_reference_banned_terms"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "term",
            "scope",
            name="uq_style_reference_banned_terms_profile_term_scope",
        ),
    )

    term_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("style_reference_profiles.profile_id"))
    term: Mapped[str] = mapped_column(String)
    replacement_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String, default="generation")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceMetricEvent(Base):
    """PR-10 §13 — 可观测性事件流(append-only,无 FK)。

    InjectionService / qc gate / ValidationOrchestrator / SceneAutoRewriteService
    各调用点写 1 行;MetricsAggregator 按 event_kind + 时间窗口 group by。
    event_kind 5 个允许值(由文档约束,**不**是 Python Enum):
    injection_invoked / qc_gate_decided / validation_executed /
    auto_rewrite_triggered / auto_rewrite_completed
    """

    __tablename__ = "style_reference_metric_events"
    __table_args__ = (
        Index("ix_sr_metric_events_kind_created", "event_kind", "created_at"),
        Index("ix_sr_metric_events_profile_created", "profile_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_kind: Mapped[str] = mapped_column(String)
    target_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    target_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    binding_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceFindingFeedback(Base):
    """立项 B — finding 的用户反馈(👍/👎)持续校准回路。

    一人(operator_ref)对一条 finding 仅一票(uq 约束);改向投票 = 更新该行 vote。
    聚合 net = #up − #down(去重用户),按 config/style_reference/feedback.yaml 阈值
    在 finding.base_confidence 基础上 ±1 档写回 finding.confidence。
    """

    __tablename__ = "style_reference_finding_feedback"
    __table_args__ = (
        UniqueConstraint(
            "finding_id",
            "operator_ref",
            name="uq_style_reference_finding_feedback_finding_operator",
        ),
        Index("ix_sr_finding_feedback_finding", "finding_id"),
    )

    feedback_id: Mapped[str] = mapped_column(String, primary_key=True)
    # ondelete CASCADE：运行连接默认强制 FK；purge_derived_data 仍显式删除，
    # 作为维护期开关关闭时的兜底并保留清晰的删除审计顺序。
    finding_id: Mapped[str] = mapped_column(
        ForeignKey("style_reference_findings.finding_id", ondelete="CASCADE")
    )
    operator_ref: Mapped[str] = mapped_column(String)
    vote: Mapped[str] = mapped_column(String)  # "up" | "down"
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


# Keep ``Base.metadata.create_all`` test databases aligned with Alembic 0081.
# Every declared foreign key used for parent lookup/deletion must have an index
# whose first column is that foreign-key column.  Existing composite indexes are
# left in their domain models; these are the previously uncovered lookups.
_FOREIGN_KEY_LOOKUP_INDEXES: tuple[tuple[str, str], ...] = (
    ("attempt_tracker", "chapter_id"),
    ("chapter_goals", "outline_plan_id"),
    ("chapter_run_jobs", "chapter_id"),
    ("human_review_events", "chapter_id"),
    ("outline_plans", "project_id"),
    ("qc_reports", "chapter_id"),
    ("scene_blueprints", "chapter_id"),
    ("scene_blueprints", "scene_id"),
    ("scene_bundles", "chapter_id"),
    ("scene_cards", "outline_plan_id"),
    ("scene_drafts", "chapter_id"),
    ("scene_execution_contracts", "project_id"),
    ("scene_execution_contracts", "chapter_id"),
    ("scene_execution_contracts", "scene_id"),
    ("snowflake_artifacts", "project_id"),
    ("snowflake_assistant_turns", "project_id"),
    ("snowflake_character_plans", "project_id"),
    ("snowflake_revision_links", "project_id"),
    ("snowflake_scene_triage_items", "scene_plan_id"),
    ("snowflake_scene_triage_items", "project_id"),
    ("snowflake_step_runs", "project_id"),
    ("story_characters", "project_id"),
    ("style_reference_evidences", "quote_id"),
    ("style_reference_findings", "run_id"),
    ("style_reference_profiles", "run_id"),
    ("style_reference_quotes", "paragraph_id"),
)

for _table_name, _column_name in _FOREIGN_KEY_LOOKUP_INDEXES:
    Index(
        f"ix_{_table_name}_{_column_name}",
        Base.metadata.tables[_table_name].c[_column_name],
    )
