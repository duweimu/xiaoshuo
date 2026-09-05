from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy import func, select

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    ReviewItem,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
)
import pytest

from novel_system.services.llm_task_runner import LLMNodeRunner
from tests.fixture_runtime import main, seed_runtime_fixture
from tests.real_llm_fakes import ScenePipelineOnlineFake


@pytest.fixture(autouse=True)
def _accounted_online_default_orchestrator_runner(monkeypatch) -> None:
    """假生成已退役：跑真实管线的用例统一注入在线记账测试替身。"""

    monkeypatch.setattr(
        "novel_system.services.orchestrator.LLMNodeRunner",
        lambda session: LLMNodeRunner(session, llm_client=ScenePipelineOnlineFake()),
    )


def _count_rows(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_fixture_runtime_chapter_ending_scene_runs_to_final_scene(session) -> None:
    """回归守卫:夹具章末场景 CH001_SC03(is_chapter_last=1)在注入的在线记账替身下
    必须跑到可审阅终稿(final_scene),而非停在蓝图 §10 章末 hook 硬门的 partial_rewrite。"""
    from novel_system.services.orchestrator import Orchestrator

    seed_runtime_fixture(session)
    session.commit()

    result = Orchestrator(session).run_scene("CH001_SC03", author_note=None)
    session.commit()

    assert result["scene_status"] == "archived", result
    final_row = result["current_final_scene_row_id"]
    assert final_row and final_row.startswith("final_scene_CH001_SC03"), result
    # hook 已声明 → §10 章末 hook 硬门不再把章末场景判成 partial_rewrite
    assert result["hard_qc"]["branch"] != "rewrite_partial", result


def test_fixture_runtime_creates_first_chapter_and_fixture_works(session) -> None:
    summary = seed_runtime_fixture(session)
    session.commit()

    assert summary["chapter_id"] == "CH001"
    assert summary["scene_ids"] == ["CH001_SC01", "CH001_SC02", "CH001_SC03"]
    assert summary["review_ids"] == []
    # 夹具还会内联两部夹具作品（work-a/work-b 完整目录）；runtime 夹具自身仍是 1 章 3 场。
    fixture_projects = ("work-a", "work-b")
    runtime_chapters = session.scalar(
        select(func.count()).select_from(ChapterGoal).where(
            ChapterGoal.project_id.is_(None) | ChapterGoal.project_id.notin_(fixture_projects)
        )
    )
    runtime_scenes = session.scalar(
        select(func.count()).select_from(SceneCard).where(
            SceneCard.project_id.is_(None) | SceneCard.project_id.notin_(fixture_projects)
        )
    )
    assert runtime_chapters == 1
    assert runtime_scenes == 3
    fe_chapters = session.scalar(
        select(func.count()).select_from(ChapterGoal).where(ChapterGoal.project_id.in_(fixture_projects))
    )
    assert fe_chapters > 0
    assert _count_rows(session, SceneRunState) == 3
    # 只剩 work-a 的 5 张待办卡（item_type=fe_card）；旧式 legacy 候选行已退役
    legacy_reviews = session.scalar(
        select(func.count()).select_from(ReviewItem).where(ReviewItem.item_type != "fe_card")
    )
    assert legacy_reviews == 0
    assert _count_rows(session, ReviewItem) == 5


def test_fixture_runtime_chapter_ops_e2e_fixture_creates_independent_chapter_runtime_seed(session) -> None:
    summary = seed_runtime_fixture(session, fixture="chapter_ops_e2e")
    session.commit()

    assert summary["extra_chapter_ids"] == ["CH200"]
    assert summary["extra_scene_ids"] == ["CH200_SC01"]
    assert summary["extra_review_ids"] == ["review_chapter_ops_pending"]

    chapter = session.get(ChapterGoal, "CH200")
    scene = session.get(SceneCard, "CH200_SC01")
    scene_state = session.get(SceneRunState, "CH200_SC01")
    final_scene = session.get(FinalScene, "final_scene_CH200_SC01_seed")
    scene_memory = session.get(SceneMemory, "scene_memory_CH200_SC01_seed")
    review_item = session.get(ReviewItem, "review_chapter_ops_pending")

    assert chapter is not None
    assert scene is not None
    assert scene.must_include_text == '{{backfill id=F200 text="旧信寄件人线索"}}'
    assert scene_state is not None
    assert scene_state.scene_status == "archived"
    assert scene_state.current_final_scene_row_id == "final_scene_CH200_SC01_seed"
    assert final_scene is not None
    assert scene_memory is not None
    assert review_item is not None
    assert review_item.status == "pending"


def test_fixture_runtime_cli_accepts_chapter_ops_e2e_fixture(capsys) -> None:
    main(["--fixture", "chapter_ops_e2e"])

    summary = json.loads(capsys.readouterr().out)
    assert summary["review_ids"] == []
    assert summary["extra_chapter_ids"] == ["CH200"]


def test_fixture_runtime_is_idempotent(session) -> None:
    first = seed_runtime_fixture(session)
    session.commit()
    second = seed_runtime_fixture(session)
    session.commit()

    assert first == second
    # 重复 seed 不增行（runtime demo 与 FE 种子作品都按固定 id 清理后重建）
    scene_count_after_two_seeds = _count_rows(session, SceneCard)
    seed_runtime_fixture(session)
    session.commit()
    assert _count_rows(session, SceneCard) == scene_count_after_two_seeds
    assert _count_rows(session, ReviewItem) == 5


def test_fixture_runtime_creates_traceable_voice_and_relation_profiles(session) -> None:
    seed_runtime_fixture(session)
    session.commit()

    voice = session.execute(
        text(
            "SELECT row_id, voice_profile_id, version, active_flag, content "
            "FROM voice_profiles WHERE row_id = 'voice_profile_VOICE_CHAR_A_v1'"
        )
    ).mappings().one()
    relation = session.execute(
        text(
            "SELECT row_id, relation_profile_id, version, active_flag, content "
            "FROM relation_profiles WHERE row_id = 'relation_profile_REL_CHAR_A_CHAR_B_v1'"
        )
    ).mappings().one()

    assert voice["voice_profile_id"] == "VOICE_CHAR_A"
    assert voice["version"] == 1
    assert voice["active_flag"] == 1
    assert voice["content"] == "short clipped lines; pressure makes the tone harder"
    assert relation["relation_profile_id"] == "REL_CHAR_A_CHAR_B"
    assert relation["version"] == 1
    assert relation["active_flag"] == 1
    assert relation["content"] == "reunion tension; B knows slightly more than A"


def test_fixture_runtime_creates_scene_and_chapter_summaries(session) -> None:
    seed_runtime_fixture(session)
    session.commit()

    scene_summary = session.get(SceneMemory, "scene_memory_CH001_SC01_summary_v1")
    chapter_summary = session.get(ChapterMemory, "chapter_memory_CH001_summary_v1")

    assert scene_summary is not None
    assert scene_summary.content == "scene summary for the first reunion beat"
    assert chapter_summary is not None
    assert chapter_summary.content == "chapter summary for the first reunion chapter"


def test_fixture_runtime_resets_demo_runtime_state(session) -> None:
    seed_runtime_fixture(session)
    session.commit()

    chapter_state = session.get(ChapterState, "CH001")
    scene_state = session.get(SceneRunState, "CH001_SC01")

    chapter_state.current_phase = "archived"
    chapter_state.chapter_passed_scene_count = 2
    chapter_state.last_final_memory_row_id = "memory_final_demo"
    scene_state.scene_status = "archived"
    scene_state.current_bundle_id = "bundle_demo"
    scene_state.total_attempt_count = 4
    scene_state.repeat_issue_key = "demo_repeat"
    session.commit()

    seed_runtime_fixture(session)
    session.commit()
    session.expire_all()

    reset_chapter_state = session.get(ChapterState, "CH001")
    reset_scene_state = session.get(SceneRunState, "CH001_SC01")

    assert reset_chapter_state.current_phase == "drafting"
    assert reset_chapter_state.chapter_passed_scene_count == 0
    assert reset_chapter_state.last_final_memory_row_id is None
    assert reset_scene_state.scene_status == "ready"
    assert reset_scene_state.current_bundle_id is None
    assert reset_scene_state.total_attempt_count == 0
    assert reset_scene_state.repeat_issue_key is None


def test_fixture_runtime_clears_demo_derived_records(session) -> None:
    seed_runtime_fixture(session)
    session.commit()

    session.add(
        AttemptTracker(
            scene_id="CH001_SC01",
            chapter_id="CH001",
            step="neutral_draft",
            status="completed",
            source_bundle_id="bundle_CH001_SC01",
            details_json={"row_id": "draft_neutral_CH001_SC01"},
        )
    )
    session.add(
        SceneBundle(
            bundle_id="bundle_CH001_SC01",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            bundle_snapshot_hash="bundle_hash_demo",
            frozen_snapshot_json={"scene_id": "CH001_SC01"},
        )
    )
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH001_SC01",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            stage="neutral_draft",
            content="demo draft",
            source_bundle_id="bundle_CH001_SC01",
            source_bundle_hash="bundle_hash_demo",
        )
    )
    session.add(
        FinalScene(
            row_id="final_scene_CH001_SC01",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            content="demo final",
            status="approved",
            source_bundle_id="bundle_CH001_SC01",
            source_bundle_hash="bundle_hash_demo",
        )
    )
    session.add(
        SceneMemory(
            row_id="scene_memory_CH001_SC01",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            content="demo memory",
            carry_notes_json=[],
            source_bundle_id="bundle_CH001_SC01",
            final_scene_row_id="final_scene_CH001_SC01",
        )
    )
    session.add(
        ChapterMemory(
            row_id="chapter_memory_CH001",
            chapter_id="CH001",
            aggregate_stage="final",
            content="demo chapter memory",
        )
    )
    session.add(
        ChapterRollingNote(
            row_id="rolling_note_CH001_SC01",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            source_scene_memory_row_id="scene_memory_CH001_SC01",
            note_text="demo note",
        )
    )
    session.add(
        HumanReviewEvent(
            event_id="human_review_demo",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            allowed_actions_json=["approve"],
            result_status_map_json={"approve": "approved"},
            default_action="approve",
        )
    )
    session.commit()

    seed_runtime_fixture(session)
    session.commit()

    assert _count_rows(session, AttemptTracker) == 0
    assert _count_rows(session, SceneBundle) == 0
    assert _count_rows(session, SceneDraft) == 0
    assert _count_rows(session, FinalScene) == 0
    assert _count_rows(session, SceneMemory) == 1
    assert _count_rows(session, ChapterMemory) == 1
    assert _count_rows(session, ChapterRollingNote) == 0
    assert _count_rows(session, HumanReviewEvent) == 0
