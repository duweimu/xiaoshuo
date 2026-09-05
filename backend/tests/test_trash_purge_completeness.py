"""审计 P-2 回归：purge_project"永久清除"后全库无该项目残留。

历史缺陷：purge 只删 FE 域 + 雪花域表，正文全文仍以 SceneDraft/FinalScene/
SceneMemory/AuthorDraftRevision/LlmCall 载荷等形式留库，且孤儿行永远无法
经 UI 清理。本测试在项目名下种满派生表后 purge，逐表断言清零。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import (
    AttemptTracker,
    AuthorPreferenceProfile,
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorDraftRevision,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterRunJob,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    NarrativeEvent,
    QcReport,
    ReviewItem,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    SnowflakeChapterPlan,
    SnowflakeRevisionLink,
    SnowflakeScenePlan,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
    VolumeSummary,
    WriterEvaluation,
    utcnow,
)
from novel_system.services.trash import TrashService
from novel_system.services.errors import DomainError
from novel_system.services.vector_store import InMemoryVectorStore
from tests.fixture_works import cleanup_fixture_works


PROJECT_ID = "proj_purge"
CHAPTER_ID = f"{PROJECT_ID}_CH01"
SCENE_ID = f"{CHAPTER_ID}_SC01"


def _seed_full_project(session) -> None:
    session.add(StoryProject(
        project_id=PROJECT_ID, title="T", outline_text="o",
        planning_mode="snowflake", trashed_flag=1, trashed_at=utcnow(), trashed_by="op",
    ))
    session.flush()
    session.add(ChapterGoal(chapter_id=CHAPTER_ID, project_id=PROJECT_ID, chapter_goal="g"))
    session.flush()
    session.add(ChapterState(chapter_id=CHAPTER_ID))
    session.add(
        SnowflakeChapterPlan(
            chapter_plan_id="chapter_plan_purge",
            project_id=PROJECT_ID,
            row_uid="CHAPTER-ROW-1",
            chapter_seq=1,
        )
    )
    session.add(
        SnowflakeScenePlan(
            scene_plan_id="scene_plan_purge",
            project_id=PROJECT_ID,
            row_uid="SCENE-ROW-1",
            scene_id=SCENE_ID,
            chapter_plan_id="chapter_plan_purge",
            chapter_id=CHAPTER_ID,
            scene_seq=1,
        )
    )
    session.add(SceneCard(
        scene_id=SCENE_ID, chapter_id=CHAPTER_ID, project_id=PROJECT_ID,
        scene_seq=1, scene_goal="推进",
    ))
    session.flush()
    session.add_all(
        [
            ChapterRunJob(
                job_id="scene_run_job_purge",
                chapter_id=CHAPTER_ID,
                scene_id=SCENE_ID,
                status="completed",
                job_type="scene_run_full",
            ),
            ChapterRunJob(
                job_id="chapter_run_job_purge",
                chapter_id=CHAPTER_ID,
                status="completed",
                job_type="chapter_run_full",
            ),
        ]
    )
    session.add(SceneRunState(scene_id=SCENE_ID))
    # —— 正文/运行时派生行（修复前全部漏删）——
    session.add(SceneDraft(
        row_id="sd1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID, stage="style_draft",
        content="草稿正文", source_bundle_id="b1", source_bundle_hash="h1",
    ))
    session.add(FinalScene(
        row_id="fs1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID, content="成稿正文",
        source_bundle_id="b1", source_bundle_hash="h1",
    ))
    session.add(SceneMemory(
        row_id="sm1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID, content="记忆",
        source_bundle_id="b1", final_scene_row_id="fs1",
    ))
    session.add(ChapterMemory(row_id="cm1", chapter_id=CHAPTER_ID, aggregate_stage="final", content="章记忆"))
    session.add(ChapterRollingNote(
        row_id="rn1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID,
        source_scene_memory_row_id="sm1", note_text="滚动",
    ))
    session.add(SceneBundle(
        bundle_id="b1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID,
        bundle_snapshot_hash="h1", frozen_snapshot_json={},
    ))
    session.add(QcReport(qc_report_id="qc1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID))
    session.add(WriterEvaluation(
        evaluation_id="ev1", object_type="scene", object_id=SCENE_ID,
        scene_id=SCENE_ID, chapter_id=CHAPTER_ID, rubric_id="r1",
    ))
    session.add(AttemptTracker(scene_id=SCENE_ID, chapter_id=CHAPTER_ID, step="archive", status="completed"))
    session.add(
        LlmCall(
            llm_call_id="llm1",
            scope_type="scene",
            scope_id=SCENE_ID,
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
        )
    )
    session.add(
        LlmCall(
            llm_call_id="llm2",
            scope_type="project",
            scope_id=PROJECT_ID,
            project_id=PROJECT_ID,
        )
    )
    session.flush()
    session.commit()
    session.add_all(
        [
            LlmCallAttempt(
                attempt_id="llm_attempt_1",
                llm_call_id="llm1",
                provider_attempt_no=0,
                dispatch_kind="initial",
                accounting_status="settled",
            ),
            LlmCallAttempt(
                attempt_id="llm_attempt_2",
                llm_call_id="llm2",
                provider_attempt_no=0,
                dispatch_kind="initial",
                accounting_status="settled",
            ),
        ]
    )
    session.add(
        SnowflakeRevisionLink(
            revision_link_id="snowflake_revision_purge",
            project_id=PROJECT_ID,
            source_step_key="book_brief",
            source_step_run_id=None,
            affected_kind="future_kind",
            affected_id="future-id",
            reason="project purge must remove the audit link",
            status="open",
        )
    )
    session.add(HumanReviewEvent(event_id="hre1", scene_id=SCENE_ID, chapter_id=CHAPTER_ID))
    session.add(NarrativeEvent(
        event_id="ne1", project_id=PROJECT_ID, scene_id=SCENE_ID, chapter_id=CHAPTER_ID,
        event_type="character_state", entity_type="character", entity_id="c1",
        fact_key="k", fact_value="v",
    ))
    session.add(VolumeSummary(row_id="vs1", project_id=PROJECT_ID, volume_seq=1))
    session.add(ReviewItem(
        review_id="ri1", project_id=PROJECT_ID, item_type="fe_card", candidate_text="卡",
    ))
    # —— 无 project_id 的间接所有权行（scope / version / interop）——
    session.add(AuthorPreferenceProfile(
        profile_id="ap_purge", scope_type="project", scope_ref_id=PROJECT_ID,
        summary_json={"voice": "作品私有偏好"},
    ))
    session.add(StyleReferenceBook(
        book_id="style_book_shared", title="共享参考书", source_kind="paste",
        cloud_policy="local_only", text_checksum="style-book-purge-checksum",
    ))
    session.flush()
    session.add(StyleReferenceRun(run_id="style_run_shared", book_id="style_book_shared"))
    session.flush()
    session.add(StyleReferenceProfile(
        profile_id="style_profile_shared", book_id="style_book_shared",
        run_id="style_run_shared", title="共享画像", status="active",
    ))
    session.flush()
    session.add(StyleReferenceInjectionBinding(
        binding_id="style_binding_purge", profile_id="style_profile_shared",
        scope="project", scope_ref_id=PROJECT_ID, task_type="scene_generation",
        strategy="A", status="active",
    ))
    # —— 作者草稿链（修订快照/提案修复前漏删）——
    session.add(AuthorDraft(
        draft_id="d1", object_type="scene", object_id=SCENE_ID,
        source_text_ref="ref", content="作者稿",
    ))
    session.add(AuthorDraftEvent(
        event_id="de1", draft_id="d1", object_type="scene", object_id=SCENE_ID, event_type="created",
    ))
    session.add(AuthorDraftRevision(draft_revision_id="dr1", draft_id="d1", revision_no=1, content="修订全文"))
    session.add(AuthorDraftProposal(
        proposal_id="dp1", draft_id="d1", object_type="scene", object_id=SCENE_ID, content="提案",
    ))
    session.flush()


def test_purge_project_leaves_no_residual_rows(session):
    _seed_full_project(session)
    vector_store = InMemoryVectorStore()
    vector_store.write_collection(
        f"scenes_{PROJECT_ID}",
        [{"id": SCENE_ID, "text": "不得在永久清除后残留的正文"}],
    )

    result = TrashService(session, vector_store=vector_store).purge_project(PROJECT_ID)
    session.flush()

    assert sorted(result["purged_vector_collections"]) == [f"scenes_{PROJECT_ID}"]
    assert vector_store.collection_exists(f"scenes_{PROJECT_ID}") is False

    residuals: dict[str, int] = {}
    checks = {
        "story_projects": session.query(StoryProject).filter_by(project_id=PROJECT_ID),
        "chapter_goals": session.query(ChapterGoal).filter_by(project_id=PROJECT_ID),
        "chapter_states": session.query(ChapterState).filter_by(chapter_id=CHAPTER_ID),
        "chapter_run_jobs": session.query(ChapterRunJob).filter(
            (ChapterRunJob.scene_id == SCENE_ID)
            | (ChapterRunJob.chapter_id == CHAPTER_ID)
        ),
        "scene_cards": session.query(SceneCard).filter_by(project_id=PROJECT_ID),
        "scene_run_states": session.query(SceneRunState).filter_by(scene_id=SCENE_ID),
        "scene_drafts": session.query(SceneDraft).filter_by(scene_id=SCENE_ID),
        "final_scenes": session.query(FinalScene).filter_by(scene_id=SCENE_ID),
        "scene_memories": session.query(SceneMemory).filter_by(scene_id=SCENE_ID),
        "chapter_memories": session.query(ChapterMemory).filter_by(chapter_id=CHAPTER_ID),
        "chapter_rolling_notes": session.query(ChapterRollingNote).filter_by(scene_id=SCENE_ID),
        "scene_bundles": session.query(SceneBundle).filter_by(scene_id=SCENE_ID),
        "qc_reports": session.query(QcReport).filter_by(scene_id=SCENE_ID),
        "writer_evaluations": session.query(WriterEvaluation).filter_by(scene_id=SCENE_ID),
        "attempt_tracker": session.query(AttemptTracker).filter_by(scene_id=SCENE_ID),
        "llm_calls(scene)": session.query(LlmCall).filter_by(scene_id=SCENE_ID),
        "llm_calls(project)": session.query(LlmCall).filter_by(project_id=PROJECT_ID),
        "llm_call_attempts": session.query(LlmCallAttempt).filter(
            LlmCallAttempt.attempt_id.in_(["llm_attempt_1", "llm_attempt_2"])
        ),
        "snowflake_revision_links": session.query(SnowflakeRevisionLink).filter_by(
            project_id=PROJECT_ID
        ),
        "snowflake_chapter_plans": session.query(SnowflakeChapterPlan).filter_by(
            project_id=PROJECT_ID
        ),
        "snowflake_scene_plans": session.query(SnowflakeScenePlan).filter_by(
            project_id=PROJECT_ID
        ),
        "human_review_events": session.query(HumanReviewEvent).filter_by(scene_id=SCENE_ID),
        "narrative_events": session.query(NarrativeEvent).filter_by(project_id=PROJECT_ID),
        "volume_summaries": session.query(VolumeSummary).filter_by(project_id=PROJECT_ID),
        "review_items": session.query(ReviewItem).filter_by(project_id=PROJECT_ID),
        "author_drafts": session.query(AuthorDraft).filter_by(draft_id="d1"),
        "author_draft_events": session.query(AuthorDraftEvent).filter_by(draft_id="d1"),
        "author_draft_revisions": session.query(AuthorDraftRevision).filter_by(draft_id="d1"),
        "author_draft_proposals": session.query(AuthorDraftProposal).filter_by(draft_id="d1"),
        "author_preference_profiles": session.query(AuthorPreferenceProfile).filter_by(
            scope_ref_id=PROJECT_ID
        ),
        "style_reference_injection_bindings": session.query(
            StyleReferenceInjectionBinding
        ).filter_by(binding_id="style_binding_purge"),
    }
    for table, query in checks.items():
        count = query.count()
        if count:
            residuals[table] = count
    assert not residuals, f"purge 后仍有残留: {residuals}"
    # 参考画像是共享资产；永久清除作品只能移除作品绑定，不能误删画像本体。
    assert session.get(StyleReferenceProfile, "style_profile_shared") is not None


def test_demo_cleanup_deletes_revision_links_before_demo_projects(session):
    session.add(
        StoryProject(
            project_id="work-a",
            title="Demo",
            outline_text="demo",
            planning_mode="snowflake",
        )
    )
    session.flush()
    session.add(
        SnowflakeRevisionLink(
            revision_link_id="demo-revision-link",
            project_id="work-a",
            source_step_key="book_brief",
            source_step_run_id=None,
            affected_kind="future_kind",
            affected_id="future-id",
            reason="demo reseed",
            status="open",
        )
    )
    session.flush()

    cleanup_fixture_works(session)

    assert session.get(SnowflakeRevisionLink, "demo-revision-link") is None
    assert session.get(StoryProject, "work-a") is None


def test_purge_project_fails_closed_when_vector_cleanup_fails(session):
    _seed_full_project(session)

    class FailingVectorStore(InMemoryVectorStore):
        def delete_collection(self, collection_name: str) -> None:
            raise RuntimeError(f"backend unavailable: {collection_name}")

    with pytest.raises(DomainError) as error:
        TrashService(session, vector_store=FailingVectorStore()).purge_project(PROJECT_ID)
    assert error.value.code == "PROJECT_VECTOR_PURGE_FAILED"

    # Relational ownership metadata must remain available for a safe retry.
    assert session.get(StoryProject, PROJECT_ID) is not None
