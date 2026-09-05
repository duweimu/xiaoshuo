from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    RelationProfile,
    ReviewItem,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StoryProject,
    VoiceProfile,
)
from novel_system.db.session import SessionLocal

DEMO_PROJECT = {
    "project_id": "PRJ_DEMO_CH001",
    "title": "Demo CH001",
    "outline_text": "Traceable demo project for the CH001 runtime fixtures.",
}

DEMO_CHAPTER = {
    "chapter_id": "CH001",
    "project_id": DEMO_PROJECT["project_id"],
    "planned_scene_count": 3,
    "chapter_goal": "重逢与试探成立",
    "main_plot_push": "旧信线索被正式打开",
    "emotional_target": "由迟疑转入警觉",
    "ending_effect": "留下余波",
}

DEMO_SCENES = [
    {
        "scene_id": "CH001_SC01",
        "project_id": DEMO_PROJECT["project_id"],
        "chapter_id": "CH001",
        "scene_seq": 1,
        "pov_character_id": "CHAR_A",
        "onstage_chars_json": ["CHAR_A", "CHAR_B"],
        "location": "旧城门廊",
        "scene_goal": "让两人重新见面并建立张力",
        "beats_json": ["重逢", "试探", "留钩子"],
        # The deterministic offline client intentionally never echoes required
        # facts. Keep the runnable demo advisory instead of falsely claiming an
        # offline placeholder satisfied a hard continuity requirement.
        "must_include_text": None,
        "target_length_band": "short",
        "scene_type": "reunion",
        "is_chapter_last": 0,
    },
    {
        "scene_id": "CH001_SC02",
        "project_id": DEMO_PROJECT["project_id"],
        "chapter_id": "CH001",
        "scene_seq": 2,
        "pov_character_id": "CHAR_B",
        "onstage_chars_json": ["CHAR_A", "CHAR_B", "CHAR_C"],
        "location": "档案库侧室",
        "scene_goal": "把旧信中的矛盾线索抬到台面上",
        "beats_json": ["核对笔迹", "暴露缺口", "压下结论"],
        "must_include_text": None,
        "target_length_band": "medium",
        "scene_type": "investigation",
        "is_chapter_last": 0,
    },
    {
        "scene_id": "CH001_SC03",
        "project_id": DEMO_PROJECT["project_id"],
        "chapter_id": "CH001",
        "scene_seq": 3,
        "pov_character_id": "CHAR_A",
        "onstage_chars_json": ["CHAR_A", "CHAR_C"],
        "location": "雨夜码头",
        "scene_goal": "让角色带着未解问题进入下一章",
        "beats_json": ["追到码头", "交换条件", "余波收束"],
        "must_include_text": None,
        # 章末场景(is_chapter_last=1)必须声明非空 hook,否则触发蓝图 §10 章末 hook
        # 硬门(missing_hook_type)→ partial_rewrite、不产出 final_scene。该门当前仅校验
        # hook 非空(classify_hook_type 对任意非空文本都会兜底归类),此处给一条语义贴合
        # 的悬念 hook,既满足硬门又是合理的 demo 数据。
        "hook": "汽笛压过最后一句话，他没说出口的秘密，到底会把两人引向怎样的命运。",
        "target_length_band": "medium",
        "scene_type": "cliffhanger",
        "is_chapter_last": 1,
    },
]

DEMO_VOICE_PROFILES = [
    {
        "row_id": "voice_profile_VOICE_CHAR_A_v1",
        "voice_profile_id": "VOICE_CHAR_A",
        "version": 1,
        "character_id": "CHAR_A",
        "content": "short clipped lines; pressure makes the tone harder",
        "active_flag": 1,
        "source_note": "demo baseline",
    },
    {
        "row_id": "voice_profile_VOICE_CHAR_B_v1",
        "voice_profile_id": "VOICE_CHAR_B",
        "version": 1,
        "character_id": "CHAR_B",
        "content": "measured, observant phrasing; rarely answers directly",
        "active_flag": 1,
        "source_note": "demo baseline",
    },
]
DEMO_RELATION_PROFILES = [
    {
        "row_id": "relation_profile_REL_CHAR_A_CHAR_B_v1",
        "relation_profile_id": "REL_CHAR_A_CHAR_B",
        "left_character_id": "CHAR_A",
        "right_character_id": "CHAR_B",
        "version": 1,
        "content": "reunion tension; B knows slightly more than A",
        "active_flag": 1,
        "source_note": "demo baseline",
    },
    {
        "row_id": "relation_profile_REL_CHAR_A_CHAR_C_v1",
        "relation_profile_id": "REL_CHAR_A_CHAR_C",
        "left_character_id": "CHAR_A",
        "right_character_id": "CHAR_C",
        "version": 1,
        "content": "uneasy cooperation; both sides hold back a condition",
        "active_flag": 1,
        "source_note": "demo baseline",
    },
]
DEMO_SCENE_SUMMARIES = [
    {
        "row_id": "scene_memory_CH001_SC01_summary_v1",
        "scene_id": "CH001_SC01",
        "chapter_id": "CH001",
        "content": "scene summary for the first reunion beat",
        "carry_notes_json": [],
        "source_bundle_id": "test_fixture",
        "final_scene_row_id": "test_fixture",
        "source_review_id": None,
        "active_flag": 1,
        "runtime_eligible": 1,
        "runtime_eligibility_basis": "direct_read",
    }
]
DEMO_CHAPTER_SUMMARIES = [
    {
        "row_id": "chapter_memory_CH001_summary_v1",
        "chapter_id": "CH001",
        "aggregate_stage": "summary",
        "content": "chapter summary for the first reunion chapter",
        "source_review_id": None,
        "active_flag": 1,
        "runtime_eligible": 1,
        "runtime_eligibility_basis": "direct_read",
    }
]

DEMO_CHAPTER_OPS_E2E_FIXTURE = "chapter_ops_e2e"
DEMO_ALL_E2E_FIXTURE = "all_e2e"
DEMO_PREFLIGHT_BLOCKED_CHAPTER = {
    "chapter_id": "CH210",
    "planned_scene_count": 1,
    "chapter_goal": "Block scene run when POV voice is missing",
    "main_plot_push": "Make the blocker obvious before operators click run",
    "emotional_target": "Reduce avoidable runtime failures",
    "ending_effect": "Stay blocked until source dependencies are restored",
}
DEMO_PREFLIGHT_BLOCKED_SCENE = {
    "scene_id": "CH210_SC01",
    "chapter_id": "CH210",
    "scene_seq": 1,
    "pov_character_id": "CHAR_MISSING",
    "onstage_chars_json": ["CHAR_B"],
    "location": "North archive gate",
    "scene_goal": "Try to run a scene whose POV voice profile is missing",
    "beats_json": ["approach", "inspect", "hesitate"],
    "must_include_text": "missing voice profile should stop the run",
    "target_length_band": "short",
    "scene_type": "investigation",
    "is_chapter_last": 1,
}
DEMO_PREFLIGHT_WARNING_CHAPTER = {
    "chapter_id": "CH211",
    "planned_scene_count": 1,
    "chapter_goal": "Show warnings without blocking scene run",
    "main_plot_push": "Let operators see incomplete author fields early",
    "emotional_target": "Keep the panel informative without hard-stop gating",
    "ending_effect": "Warnings remain visible while run stays available",
}
DEMO_PREFLIGHT_WARNING_SCENE = {
    "scene_id": "CH211_SC01",
    "chapter_id": "CH211",
    "scene_seq": 1,
    "pov_character_id": "",
    "onstage_chars_json": [],
    "location": "",
    "scene_goal": "",
    "beats_json": [],
    "must_include_text": "",
    "target_length_band": "short",
    "scene_type": "bridge",
    "is_chapter_last": 1,
}
CHAPTER_OPS_MARKER_TOKEN = '{{backfill id=F200 text="旧信寄件人线索"}}'
CHAPTER_OPS_CHAPTER = {
    "chapter_id": "CH200",
    "planned_scene_count": 1,
    "chapter_goal": "补齐章节运行治理闭环",
    "main_plot_push": "把 backfill 和 final aggregate 走通",
    "emotional_target": "让卡住的线索重新可操作",
    "ending_effect": "形成新的 final aggregate 摘要",
}
CHAPTER_OPS_SCENES = [
    {
        "scene_id": "CH200_SC01",
        "chapter_id": "CH200",
        "scene_seq": 1,
        "pov_character_id": "CHAR_A",
        "onstage_chars_json": ["CHAR_A", "CHAR_B"],
        "location": "旧城门洞",
        "scene_goal": "把模板 marker 治理成 staged backfill",
        "beats_json": ["重逢", "试探", "收束"],
        "must_include_text": CHAPTER_OPS_MARKER_TOKEN,
        "target_length_band": "short",
        "scene_type": "reunion",
        "is_chapter_last": 1,
    }
]
CHAPTER_OPS_FINAL_SCENE = {
    "row_id": "final_scene_CH200_SC01_seed",
    "scene_id": "CH200_SC01",
    "chapter_id": "CH200",
    "content": f"归档里仍然保留 {CHAPTER_OPS_MARKER_TOKEN}",
    "status": "approved",
    "source_bundle_id": "bundle_chapter_ops_seed",
    "source_bundle_hash": "hash_chapter_ops_seed",
}
CHAPTER_OPS_SCENE_MEMORY = {
    "row_id": "scene_memory_CH200_SC01_seed",
    "scene_id": "CH200_SC01",
    "chapter_id": "CH200",
    "content": f"场景记忆仍然写着 {CHAPTER_OPS_MARKER_TOKEN}",
    "carry_notes_json": [],
    "source_bundle_id": "bundle_chapter_ops_seed",
    "final_scene_row_id": "final_scene_CH200_SC01_seed",
    "source_review_id": None,
    "active_flag": 1,
    "runtime_eligible": 1,
    "runtime_eligibility_basis": "direct_read",
}
CHAPTER_OPS_PENDING_REVIEW = {
    "review_id": "review_chapter_ops_pending",
    "scene_id": "CH200_SC01",
    "chapter_id": "CH200",
    "item_type": "scene_summary",
    "status": "pending",
    "candidate_text": f"待审摘要仍然引用 {CHAPTER_OPS_MARKER_TOKEN}",
    "candidate_payload_json": {
        "lineage_key": "CH200_SC01",
        "scene_id": "CH200_SC01",
        "text": f"待审摘要仍然引用 {CHAPTER_OPS_MARKER_TOKEN}",
    },
    "active_on_approve": 1,
}


def _upsert(session: Any, model: type[Any], identity: str, payload: dict[str, Any]) -> Any:
    row = session.get(model, payload[identity])
    if row is None:
        row = model(**payload)
        session.add(row)
    else:
        for key, value in payload.items():
            setattr(row, key, value)
    return row


def _upsert_chapter(session: Any, payload: dict[str, Any]) -> None:
    _upsert(session, ChapterGoal, "chapter_id", payload)
    # These catalog models intentionally do not expose ORM relationships, so
    # SQLAlchemy cannot infer FK insertion order.  Materialize the parent before
    # the runtime state child while keeping the whole seed operation atomic.
    session.flush()
    _upsert(
        session,
        ChapterState,
        "chapter_id",
        {
            "chapter_id": payload["chapter_id"],
            "current_phase": "drafting",
            "chapter_passed_scene_count": 0,
            "chapter_backfill_pending_count": 0,
            "mid_aggregate_enabled_effective": 0,
            "aggregate_block_reason": "none",
            "manual_hold_reason": None,
            "last_interim_memory_row_id": None,
            "last_final_memory_row_id": None,
        },
    )


def _upsert_scene(session: Any, payload: dict[str, Any]) -> None:
    _upsert(session, SceneCard, "scene_id", payload)
    session.flush()
    _upsert(
        session,
        SceneRunState,
        "scene_id",
        {
            "scene_id": payload["scene_id"],
            "scene_status": "ready",
            "current_bundle_id": None,
            "current_bundle_hash": None,
            "current_neutral_draft_row_id": None,
            "current_style_draft_row_id": None,
            "current_final_scene_row_id": None,
            "current_human_review_event_id": None,
            "current_qc_report_id": None,
            "bundle_build_count": 0,
            "hard_partial_rewrite_count": 0,
            "hard_full_rewrite_count": 0,
            "soft_patch_count": 0,
            "total_attempt_count": 0,
            "attempt_budget": 4,
            "repeat_issue_key": None,
            "repeat_issue_count": 0,
        },
    )


def _upsert_review_item(session: Any, payload: dict[str, Any]) -> None:
    _upsert(session, ReviewItem, "review_id", payload)


def _cleanup_demo_runtime(session: Session) -> None:
    chapter_id = DEMO_CHAPTER["chapter_id"]
    demo_voice_ids = [item["voice_profile_id"] for item in DEMO_VOICE_PROFILES]
    demo_relation_ids = [item["relation_profile_id"] for item in DEMO_RELATION_PROFILES]

    session.execute(delete(AttemptTracker).where(AttemptTracker.chapter_id == chapter_id))
    session.execute(delete(SceneBundle).where(SceneBundle.chapter_id == chapter_id))
    session.execute(delete(SceneDraft).where(SceneDraft.chapter_id == chapter_id))
    session.execute(delete(FinalScene).where(FinalScene.chapter_id == chapter_id))
    session.execute(delete(SceneMemory).where(SceneMemory.chapter_id == chapter_id))
    session.execute(delete(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id))
    session.execute(delete(ChapterRollingNote).where(ChapterRollingNote.chapter_id == chapter_id))
    session.execute(delete(HumanReviewEvent).where(HumanReviewEvent.chapter_id == chapter_id))
    session.execute(delete(VoiceProfile).where(VoiceProfile.voice_profile_id.in_(demo_voice_ids)))
    session.execute(delete(RelationProfile).where(RelationProfile.relation_profile_id.in_(demo_relation_ids)))


def _cleanup_chapter_ops_runtime(session: Session) -> None:
    chapter_id = CHAPTER_OPS_CHAPTER["chapter_id"]
    scene_ids = [item["scene_id"] for item in CHAPTER_OPS_SCENES]

    session.execute(delete(AttemptTracker).where(AttemptTracker.chapter_id == chapter_id))
    session.execute(delete(SceneBundle).where(SceneBundle.chapter_id == chapter_id))
    session.execute(delete(SceneDraft).where(SceneDraft.chapter_id == chapter_id))
    session.execute(delete(FinalScene).where(FinalScene.chapter_id == chapter_id))
    session.execute(delete(SceneMemory).where(SceneMemory.chapter_id == chapter_id))
    session.execute(delete(ChapterMemory).where(ChapterMemory.chapter_id == chapter_id))
    session.execute(delete(ChapterRollingNote).where(ChapterRollingNote.chapter_id == chapter_id))
    session.execute(delete(HumanReviewEvent).where(HumanReviewEvent.chapter_id == chapter_id))
    session.execute(delete(ReviewItem).where(ReviewItem.chapter_id == chapter_id))
    session.execute(delete(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids)))
    session.execute(delete(SceneCard).where(SceneCard.scene_id.in_(scene_ids)))
    session.execute(delete(ChapterState).where(ChapterState.chapter_id == chapter_id))
    session.execute(delete(ChapterGoal).where(ChapterGoal.chapter_id == chapter_id))


def _cleanup_preflight_e2e_runtime(session: Session) -> None:
    chapter_ids = [
        DEMO_PREFLIGHT_BLOCKED_CHAPTER["chapter_id"],
        DEMO_PREFLIGHT_WARNING_CHAPTER["chapter_id"],
    ]
    scene_ids = [
        DEMO_PREFLIGHT_BLOCKED_SCENE["scene_id"],
        DEMO_PREFLIGHT_WARNING_SCENE["scene_id"],
    ]

    session.execute(delete(AttemptTracker).where(AttemptTracker.chapter_id.in_(chapter_ids)))
    session.execute(delete(SceneBundle).where(SceneBundle.chapter_id.in_(chapter_ids)))
    session.execute(delete(SceneDraft).where(SceneDraft.chapter_id.in_(chapter_ids)))
    session.execute(delete(FinalScene).where(FinalScene.chapter_id.in_(chapter_ids)))
    session.execute(delete(SceneMemory).where(SceneMemory.chapter_id.in_(chapter_ids)))
    session.execute(delete(ChapterMemory).where(ChapterMemory.chapter_id.in_(chapter_ids)))
    session.execute(delete(ChapterRollingNote).where(ChapterRollingNote.chapter_id.in_(chapter_ids)))
    session.execute(delete(HumanReviewEvent).where(HumanReviewEvent.chapter_id.in_(chapter_ids)))
    session.execute(delete(ReviewItem).where(ReviewItem.chapter_id.in_(chapter_ids)))
    session.execute(delete(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids)))
    session.execute(delete(SceneCard).where(SceneCard.scene_id.in_(scene_ids)))
    session.execute(delete(ChapterState).where(ChapterState.chapter_id.in_(chapter_ids)))
    session.execute(delete(ChapterGoal).where(ChapterGoal.chapter_id.in_(chapter_ids)))


def _seed_chapter_ops_e2e(session: Session) -> dict[str, list[str] | str]:
    _cleanup_chapter_ops_runtime(session)
    _upsert_chapter(session, CHAPTER_OPS_CHAPTER)
    for payload in CHAPTER_OPS_SCENES:
        _upsert_scene(session, payload)
    session.flush()
    _upsert(session, FinalScene, "row_id", CHAPTER_OPS_FINAL_SCENE)
    _upsert(session, SceneMemory, "row_id", CHAPTER_OPS_SCENE_MEMORY)
    _upsert_review_item(session, CHAPTER_OPS_PENDING_REVIEW)

    scene_state = session.get(SceneRunState, CHAPTER_OPS_SCENES[0]["scene_id"])
    if scene_state is not None:
        scene_state.scene_status = "archived"
        scene_state.current_final_scene_row_id = CHAPTER_OPS_FINAL_SCENE["row_id"]

    return {
        "chapter_id": CHAPTER_OPS_CHAPTER["chapter_id"],
        "scene_ids": [item["scene_id"] for item in CHAPTER_OPS_SCENES],
        "review_ids": [CHAPTER_OPS_PENDING_REVIEW["review_id"]],
    }


def _seed_preflight_e2e(session: Session) -> dict[str, list[str]]:
    _cleanup_preflight_e2e_runtime(session)
    _upsert_chapter(session, DEMO_PREFLIGHT_BLOCKED_CHAPTER)
    _upsert_scene(session, DEMO_PREFLIGHT_BLOCKED_SCENE)
    _upsert_chapter(session, DEMO_PREFLIGHT_WARNING_CHAPTER)
    _upsert_scene(session, DEMO_PREFLIGHT_WARNING_SCENE)
    return {
        "chapter_ids": [
            DEMO_PREFLIGHT_BLOCKED_CHAPTER["chapter_id"],
            DEMO_PREFLIGHT_WARNING_CHAPTER["chapter_id"],
        ],
        "scene_ids": [
            DEMO_PREFLIGHT_BLOCKED_SCENE["scene_id"],
            DEMO_PREFLIGHT_WARNING_SCENE["scene_id"],
        ],
    }


def _seed_runtime_fixture(session: Session, *, fixture: str | None = None) -> dict[str, list[str] | str]:
    # FE-ALIGN P2: 两部种子作品（work-a / work-b）后端化（独立模块，自带幂等清理）。
    try:
        from tests.fixture_works import seed_fixture_works
    except ImportError:  # 直接以脚本运行（python tests/fixture_runtime.py）
        from fixture_works import seed_fixture_works

    seed_fixture_works(session)
    _cleanup_demo_runtime(session)
    _upsert(session, StoryProject, "project_id", DEMO_PROJECT)
    session.flush()
    _upsert_chapter(session, DEMO_CHAPTER)
    for payload in DEMO_SCENES:
        _upsert_scene(session, payload)
    for payload in DEMO_VOICE_PROFILES:
        _upsert(session, VoiceProfile, "row_id", payload)
    for payload in DEMO_RELATION_PROFILES:
        _upsert(session, RelationProfile, "row_id", payload)
    for payload in DEMO_SCENE_SUMMARIES:
        _upsert(session, SceneMemory, "row_id", payload)
    for payload in DEMO_CHAPTER_SUMMARIES:
        _upsert(session, ChapterMemory, "row_id", payload)
    review_ids: list[str] = []
    extra_chapter_ids: list[str] = []
    extra_scene_ids: list[str] = []
    extra_review_ids: list[str] = []
    if fixture is None:
        pass
    elif fixture == DEMO_CHAPTER_OPS_E2E_FIXTURE:
        chapter_ops_summary = _seed_chapter_ops_e2e(session)
        extra_chapter_ids.append(chapter_ops_summary["chapter_id"])
        extra_scene_ids.extend(chapter_ops_summary["scene_ids"])
        extra_review_ids.extend(chapter_ops_summary["review_ids"])
    elif fixture == DEMO_ALL_E2E_FIXTURE:
        chapter_ops_summary = _seed_chapter_ops_e2e(session)
        preflight_summary = _seed_preflight_e2e(session)
        extra_chapter_ids.append(chapter_ops_summary["chapter_id"])
        extra_chapter_ids.extend(preflight_summary["chapter_ids"])
        extra_scene_ids.extend(chapter_ops_summary["scene_ids"])
        extra_scene_ids.extend(preflight_summary["scene_ids"])
        extra_review_ids.extend(chapter_ops_summary["review_ids"])
    else:
        raise ValueError(f"Unsupported demo fixture: {fixture}")
    summary = {
        "chapter_id": DEMO_CHAPTER["chapter_id"],
        "scene_ids": [item["scene_id"] for item in DEMO_SCENES],
        "review_ids": review_ids,
    }
    if extra_chapter_ids:
        summary["extra_chapter_ids"] = extra_chapter_ids
        summary["extra_scene_ids"] = extra_scene_ids
        summary["extra_review_ids"] = extra_review_ids
    return summary


def seed_runtime_fixture(session: Session | None = None, *, fixture: str | None = None) -> dict[str, list[str] | str]:
    if session is not None:
        return _seed_runtime_fixture(session, fixture=fixture)

    with SessionLocal() as managed_session:
        summary = _seed_runtime_fixture(managed_session, fixture=fixture)
        managed_session.commit()
        return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", choices=[DEMO_CHAPTER_OPS_E2E_FIXTURE, DEMO_ALL_E2E_FIXTURE])
    args = parser.parse_args(argv)
    print(json.dumps(seed_runtime_fixture(fixture=args.fixture), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
