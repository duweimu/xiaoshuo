from __future__ import annotations

import json

from sqlalchemy import func, select

from novel_system.db.models import (
    AttemptTracker,
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterMemory,
    ChapterRollingNote,
    ChapterRunJob,
    ChapterState,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    IdempotencyKey,
    LlmCall,
    LlmCallAttempt,
    NarrativeEvent,
    OperationLog,
    OutlinePlan,
    PassagePatchCandidate,
    ProjectWritingStats,
    QcReport,
    RelationProfile,
    ReviewDerivedSnooze,
    ReviewItem,
    RevisionCandidate,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneMemory,
    SceneRunState,
    SnowflakeArtifact,
    SnowflakeChapterPlan,
    SnowflakeCharacterPlan,
    SnowflakeRevisionLink,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
    SystemConfigSnapshot,
    SystemSecret,
    VoiceProfile,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.tools.reset_author_state import collect_reset_summary, execute_reset, main


def _count(session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _create_v2_project(client, *, key: str = "after-reset") -> dict:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Reset Check",
            "genre": "mystery",
            "target_chapter_count": 1,
            "target_word_count": 90000,
            "outline_text": "A reporter returns to a coastal city and reopens a sealed disappearance.",
        },
        headers={"X-Idempotency-Key": f"reset-v2-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _seed_preserved_state(session) -> None:
    session.add(
        ReviewItem(
            review_id="review_reference_finding",
            chapter_id=None,
            scene_id=None,
            item_type="style_observation",
            status="pending",
            candidate_text="reference-derived style pattern",
            candidate_payload_json={
                "source": "reference_book_learning",
                "reference_book_id": "REF_BOOK",
                "reference_segment_id": "REF_SEGMENT",
            },
        )
    )
    session.add(
        SystemConfigSnapshot(
            snapshot_id="CFG_ACTIVE",
            category="models",
            version=3,
            yaml_raw="routing: default",
            parsed_json={"routing": "default"},
            validation_json={"status": "ok"},
            status="active",
            active_flag=1,
            created_by="tester",
            activated_at="2026-04-28T00:00:00+00:00",
        )
    )
    session.add(
        SystemSecret(
            secret_id="SECRET_ACTIVE",
            encrypted_value="cipher",
            value_hint="...abcd",
            secret_type="api_key",
            metadata_json={"provider": "openai"},
            updated_by="tester",
        )
    )
    # node_id 只需命中 PRESERVED_LLM_NODE_PREFIXES 的 reference_ 前缀（历史审计痕迹保留契约）；
    # 取遗留占位串，不要求（也不应）是现役注册节点——reference_* 节点族已删除。
    session.add(
        LlmCall(
            llm_call_id="llm_call_reference_profile",
            scope_type="system",
            scope_id="reference_legacy_audit",
            provider="mock",
            model="gpt-reference",
            node_id="reference_legacy_audit",
            step="reference_profile",
            request_payload_summary={"source": "reference"},
            response_payload_summary={"ok": True},
        )
    )


def _seed_author_state(session) -> None:
    session.add_all(
        [
            StoryProject(
                project_id="PRJ_RESET_OUTLINE",
                title="Outline Project",
                genre="mystery",
                target_word_count=80000,
                target_chapter_count=1,
                outline_text="outline project",
                planning_mode="outline_driven",
                status="chapter_ready",
                active_outline_plan_id="plan_reset_outline",
                current_chapter_id="CH_RESET_OUTLINE",
                approved_chapter_ids_json=[],
            ),
            StoryProject(
                project_id="PRJ_RESET_SNOW",
                title="Snowflake Project",
                genre="thriller",
                target_word_count=90000,
                target_chapter_count=1,
                outline_text="snowflake project",
                planning_mode="snowflake",
                status="outline_review",
                active_outline_plan_id="plan_reset_snow",
                current_chapter_id="CH_RESET_SNOW",
                approved_chapter_ids_json=[],
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            ProjectWritingStats(project_id="PRJ_RESET_SNOW", words_total=1200),
            SnowflakeChapterPlan(
                chapter_plan_id="chapter_plan_reset",
                project_id="PRJ_RESET_SNOW",
                row_uid="CHAPTER-RESET-1",
                chapter_seq=1,
            ),
            NarrativeEvent(
                event_id="narrative_event_reset",
                project_id="PRJ_RESET_SNOW",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                event_type="character_state",
                entity_type="character",
                entity_id="CHAR_RESET",
                fact_key="resolve",
                fact_value="hardens",
            ),
            VolumeSummary(
                row_id="volume_summary_reset",
                project_id="PRJ_RESET_SNOW",
                volume_seq=1,
            ),
            ReviewDerivedSnooze(
                project_id="PRJ_RESET_SNOW",
                fingerprint="review-snooze-reset",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            OutlinePlan(
                plan_id="plan_reset_outline",
                project_id="PRJ_RESET_OUTLINE",
                version=1,
                status="approved",
                plan_json={"chapters": [{"chapter_id": "CH_RESET_OUTLINE"}]},
                approved_at="2026-04-28T00:00:00+00:00",
            ),
            OutlinePlan(
                plan_id="plan_reset_snow",
                project_id="PRJ_RESET_SNOW",
                version=1,
                status="approved",
                plan_json={"source": "snowflake_method", "chapters": [{"chapter_id": "CH_RESET_SNOW"}]},
                approved_at="2026-04-28T00:00:00+00:00",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            SnowflakeArtifact(
                artifact_id="artifact_reset_book_brief",
                project_id="PRJ_RESET_SNOW",
                step_key="book_brief",
                version=1,
                status="approved",
                artifact_json={"category": "thriller"},
                approved_at="2026-04-28T00:00:00+00:00",
            ),
            SnowflakeStepRun(
                step_run_id="step_run_reset_book_brief",
                project_id="PRJ_RESET_SNOW",
                step_key="book_brief",
                version=1,
                status="approved",
                draft_json={"category": "thriller"},
                health_json={"severity": "info"},
                approved_at="2026-04-28T00:00:00+00:00",
            ),
            SnowflakeCharacterPlan(
                character_plan_id="char_plan_reset",
                project_id="PRJ_RESET_SNOW",
                character_id="CHAR_RESET",
                display_name="Lin",
                role="lead",
                summary_json={"goal": "find the truth"},
                bible_json={"fear": "exposure"},
                status="approved",
            ),
            SnowflakeScenePlan(
                scene_plan_id="scene_plan_reset",
                project_id="PRJ_RESET_SNOW",
                row_uid="SCENE-RESET-1",
                scene_id="SC_RESET_SNOW_01",
                chapter_plan_id="chapter_plan_reset",
                chapter_id="CH_RESET_SNOW",
                chapter_title="Reset Chapter",
                chapter_goal="push the investigation forward",
                scene_seq=1,
                title="Archive scene",
                summary="open the sealed room",
                scene_type="proactive",
                scene_crucible="Leaving means losing the only witness.",
                goal="open the sealed room",
                conflict="the lock and a hostile witness block her",
                setback="the lead now knows the leak is internal",
                exit_change="the lead now knows the leak is internal",
                hook="someone is already waiting outside",
                target_length_band="medium",
                status="approved",
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            SnowflakeSceneTriageItem(
                triage_id="triage_reset",
                project_id="PRJ_RESET_SNOW",
                scene_plan_id="scene_plan_reset",
                scene_id="SC_RESET_SNOW_01",
                recommended_status="maybe",
                manual_status="",
                effective_status="maybe",
                score=75,
                missing_fields_json=["setback"],
                fix_steps_json=["Raise the cost."],
                repair_patch_json={"setback": "make it worse"},
                pressure_flags_json=["missing_setback"],
                notes="repair before materializing",
            ),
            SnowflakeRevisionLink(
                revision_link_id="revision_link_reset",
                project_id="PRJ_RESET_SNOW",
                source_step_key="book_brief",
                source_step_run_id="step_run_reset_book_brief",
                affected_kind="scene_plan",
                affected_id="scene_plan_reset",
                reason="upstream changed",
                status="open",
            ),
            StoryCharacter(
                character_id="CHAR_RESET",
                project_id="PRJ_RESET_SNOW",
                display_name="Lin",
                role="lead",
                summary_json={"goal": "find the truth"},
                bible_json={"fear": "exposure"},
                status="approved",
            ),
            ChapterGoal(
                chapter_id="CH_RESET_SNOW",
                project_id="PRJ_RESET_SNOW",
                outline_plan_id="plan_reset_snow",
                planned_scene_count=1,
                chapter_goal="push the investigation forward",
                main_plot_push="reveal the hidden archive",
                emotional_target="pressure",
                ending_effect="new suspicion",
                writer_brief_json={"source": "snowflake_method"},
            ),
            ChapterGoal(
                chapter_id="CH_RESET_OUTLINE",
                project_id="PRJ_RESET_OUTLINE",
                outline_plan_id="plan_reset_outline",
                planned_scene_count=1,
                chapter_goal="deliver the outline beat",
                main_plot_push="escalate the cover-up",
                emotional_target="doubt",
                ending_effect="turn back",
                writer_brief_json={"source": "project_outline_plan"},
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            ChapterState(
                chapter_id="CH_RESET_SNOW",
                current_phase="drafting",
                chapter_passed_scene_count=0,
                chapter_backfill_pending_count=1,
                mid_aggregate_enabled_effective=0,
                aggregate_block_reason="none",
            ),
            SceneCard(
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                project_id="PRJ_RESET_SNOW",
                outline_plan_id="plan_reset_snow",
                scene_seq=1,
                pov_character_id="CHAR_RESET",
                onstage_chars_json=["CHAR_RESET"],
                location="Old archive",
                scene_goal="open the sealed room",
                beats_json=["arrive", "unlock", "discover"],
                must_include_text="sealed ledger",
                forbidden_text="no exposition dump",
                exit_change="the lead now knows the leak is internal",
                hook="someone is already waiting outside",
                writer_brief_json={"scene_form": "proactive"},
                target_length_band="medium",
                scene_type="scene",
                is_chapter_last=1,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            SceneRunState(
                scene_id="SC_RESET_SNOW_01",
                scene_status="archived",
                current_bundle_id="bundle_reset_scene",
                current_bundle_hash="hash-reset",
                current_neutral_draft_row_id="scene_draft_reset",
                current_style_draft_row_id="scene_draft_reset",
                current_final_scene_row_id="final_scene_reset",
                current_human_review_event_id="human_review_reset",
                current_qc_report_id="qc_report_reset",
                total_attempt_count=1,
            ),
            SceneBundle(
                bundle_id="bundle_reset_scene",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                execution_mode="P2",
                bundle_snapshot_hash="hash-reset",
                frozen_snapshot_json={"bundle": True},
            ),
            SceneBlueprint(
                row_id="scene_blueprint_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                source_bundle_id="bundle_reset_scene",
                source_bundle_hash="hash-reset",
                blueprint_json={"pressure": "high"},
                llm_call_id="llm_call_author_scene",
                status="accepted",
            ),
            SceneExecutionContract(
                contract_id="scene_execution_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                project_id="PRJ_RESET_SNOW",
                contract_version="scene_execution_contract_v1",
                source_snapshot_hash="snapshot-hash",
                payload_json={"scene_goal": "open the sealed room"},
                missing_fields_json=[],
                status="active",
                created_by="scene_execution",
            ),
            GenerationPlanningArtifact(
                row_id="planning_artifact_reset",
                artifact_type="character_pressure_blueprint",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                scene_id="SC_RESET_SNOW_01",
                payload_json={"pressure": ["risk", "clock"]},
                llm_call_id="llm_call_author_scene",
                source_bundle_id="bundle_reset_scene",
                source_bundle_hash="hash-reset",
                status="active",
            ),
            LlmCall(
                llm_call_id="llm_call_author_scene",
                scope_type="scene",
                scope_id="SC_RESET_SNOW_01",
                provider="mock",
                model="gpt-author",
                node_id="scene_execution",
                step="scene_execution",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                request_payload_summary={"scope": "author"},
                response_payload_summary={"ok": True},
            ),
            SceneDraft(
                row_id="scene_draft_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                stage="near_final",
                status="active",
                content="draft scene content",
                source_bundle_id="bundle_reset_scene",
                source_bundle_hash="hash-reset",
                generation_llm_call_id="llm_call_author_scene",
            ),
            QcReport(
                qc_report_id="qc_report_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                qc_type="scene_quality",
                status="active",
                source_draft_row_id="scene_draft_reset",
                source_bundle_id="bundle_reset_scene",
                resolution_code="needs_revision",
                pass_flag=0,
                next_action="rewrite",
                issues_json=[{"dimension": "stakes"}],
                rewrite_brief_json=[{"action": "raise the cost"}],
            ),
            WriterEvaluation(
                evaluation_id="writer_eval_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                scene_id="SC_RESET_SNOW_01",
                rubric_id="literary_revision_v1",
                source_text_ref="author_draft:author_draft_reset",
                source_bundle_id="bundle_reset_scene",
                evaluator_llm_call_id="llm_call_author_scene",
                lens="aggregate",
                source_blueprint_row_id="scene_blueprint_reset",
                overall_score=0.51,
                scores_json={"choice_pressure": 0.4},
                findings_json=[{"dimension": "choice_pressure"}],
                revision_brief_json=[{"action": "make the cost visible"}],
                requires_human_review=1,
                status="completed",
            ),
            RevisionCandidate(
                revision_id="revision_reset",
                evaluation_id="writer_eval_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                scene_id="SC_RESET_SNOW_01",
                revision_type="near_final",
                source_text_ref="author_draft:author_draft_reset",
                proposed_text="revised scene",
                instruction_json=[{"instruction": "tighten scene"}],
                diff_summary_json={"changed": True},
                patches_json=[{"kind": "replace"}],
                apply_mode="manual_only",
                target_text_ref="author_draft:author_draft_reset",
                status="candidate",
            ),
            FinalScene(
                row_id="final_scene_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                content="final scene content",
                status="approved",
                source_bundle_id="bundle_reset_scene",
                source_bundle_hash="hash-reset",
                generation_llm_call_id="llm_call_author_scene",
            ),
            AuthorDraft(
                draft_id="author_draft_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                source_text_ref="final_scene:final_scene_reset",
                content="author draft content",
                revision_no=1,
                status="current",
            ),
            PassagePatchCandidate(
                patch_id="patch_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                scene_id="SC_RESET_SNOW_01",
                source_text_ref="author_draft:author_draft_reset",
                target_text_ref="author_draft:author_draft_reset",
                source_draft_id="author_draft_reset",
                generation_llm_call_id="llm_call_author_scene",
                quality_signal_id="writer_eval_reset",
                source_excerpt="old sentence",
                issue_dimension="choice_pressure",
                candidate_category="local_patch",
                target_range_json={"unit": "text"},
                revision_strategy="make the cost explicit",
                preference_tags_json=["pressure"],
                inserted_into_author_draft=0,
                replacement_options_json=[{"text": "new sentence"}],
                rationale="increase the visible risk",
                manual_only=1,
                status="candidate",
                author_decision="pending",
            ),
            AuthorDraftProposal(
                proposal_id="proposal_reset",
                draft_id="author_draft_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                proposal_type="passage_candidate",
                proposal_source="writer_room",
                content="proposal text",
                rationale="fix pacing",
                source_llm_call_id="llm_call_author_scene",
                target_range_json={"unit": "text"},
                before_text_hash="hash-before",
                replacement_text="proposal replacement",
                proposal_kind="local_patch",
                source_evaluation_id="writer_eval_reset",
                merge_status="pending",
                status="candidate",
            ),
            AuthorDraftEvent(
                event_id="draft_event_reset",
                draft_id="author_draft_reset",
                object_type="scene",
                object_id="SC_RESET_SNOW_01",
                event_type="created",
                patch_id="patch_reset",
                revision_id="revision_reset",
                note="draft created",
                payload_json={"source": "writer_room"},
            ),
            AuthorPreferenceProfile(
                profile_id="author_pref_reset",
                scope_type="global",
                scope_ref_id="global",
                status="approved",
                runtime_eligible=1,
                summary_json={"patterns": ["tight pacing"]},
                source_patch_ids_json=["patch_reset"],
                created_by="writer_deep_review",
            ),
            SceneMemory(
                row_id="scene_memory_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                content="scene memory",
                carry_notes_json=[{"note": "keep the ledger active"}],
                source_bundle_id="bundle_reset_scene",
                final_scene_row_id="final_scene_reset",
                source_review_id="review_author_scene",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            ),
            ChapterMemory(
                row_id="chapter_memory_reset",
                chapter_id="CH_RESET_SNOW",
                aggregate_stage="summary",
                content="chapter memory",
                source_review_id="review_author_scene",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            ),
            ChapterRollingNote(
                row_id="chapter_note_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                source_scene_memory_row_id="scene_memory_reset",
                note_text="carry the leaked ledger into the next chapter",
                revision_no=1,
            ),
            AttemptTracker(
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                step="scene_execution",
                status="failed",
                source_bundle_id="bundle_reset_scene",
                details_json={"reason": "blocked"},
            ),
            ChapterRunJob(
                job_id="chapter_job_reset",
                chapter_id="CH_RESET_SNOW",
                status="completed",
                job_type="chapter_full",
                payload_json={"chapter_id": "CH_RESET_SNOW"},
                result_summary_json={"status": "completed"},
                worker_id="worker-1",
                attempt_no=1,
            ),
            ReviewItem(
                review_id="review_author_scene",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                item_type="scene_memory",
                status="pending",
                candidate_text="promote the scene memory",
                candidate_payload_json={"source": "scene_qc"},
            ),
            ReviewItem(
                review_id="review_author_global",
                scene_id=None,
                chapter_id=None,
                item_type="author_preference_profile",
                status="pending",
                candidate_text="refresh author preference profile",
                candidate_payload_json={"source": "writer_deep_review"},
            ),
            HumanReviewEvent(
                event_id="human_review_reset",
                scene_id="SC_RESET_SNOW_01",
                chapter_id="CH_RESET_SNOW",
                object_ref="review_author_scene",
                event_source="system",
                priority="high",
                owner="operator",
                status="open",
                allowed_actions_json=["approve", "reject"],
                result_status_map_json={"approve": "approved"},
                details_json={"source": "scene_qc"},
                default_action="approve",
            ),
            VoiceProfile(
                row_id="voice_profile_reset",
                voice_profile_id="VOICE_RESET",
                version=1,
                character_id="CHAR_RESET",
                content="measured clipped tone",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            ),
            RelationProfile(
                row_id="relation_profile_reset",
                relation_profile_id="REL_RESET",
                left_character_id="CHAR_RESET",
                right_character_id="CHAR_OTHER",
                version=1,
                content="built on mutual suspicion",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            ),
            IdempotencyKey(
                idempotency_key="idem_reset_author_scene",
                request_hash="req-hash-reset",
                status="succeeded",
                response_json={"ok": True},
                worker_id="http",
                attempt_no=1,
            ),
            OperationLog(
                event_type="operator_action",
                object_type="review_item",
                object_ref="review_author_scene",
                payload_json={"action": "queue"},
            ),
        ]
    )


def _seed_all(session) -> None:
    _seed_preserved_state(session)
    _seed_author_state(session)
    session.flush()
    session.add_all(
        [
            LlmCallAttempt(
                attempt_id="attempt_reference_preserved",
                llm_call_id="llm_call_reference_profile",
                provider_attempt_no=0,
                dispatch_kind="initial",
                accounting_status="settled",
            ),
            LlmCallAttempt(
                attempt_id="attempt_author_deleted",
                llm_call_id="llm_call_author_scene",
                provider_attempt_no=0,
                dispatch_kind="initial",
                accounting_status="settled",
            ),
        ]
    )
    session.commit()


def test_collect_reset_summary_is_dry_run_and_preserves_reference_audit_traces(session) -> None:
    _seed_all(session)

    summary = collect_reset_summary(session)

    assert summary["mode"] == "dry_run"
    assert summary["status"] == "ok"
    assert summary["planned_counts"]["story_projects"] == 2
    assert summary["planned_counts"]["review_items"] == 2
    assert summary["planned_counts"]["llm_calls"] == 1
    assert summary["planned_counts"]["llm_call_attempts"] == 1
    assert summary["planned_counts"]["project_writing_stats"] == 1
    assert summary["planned_counts"]["snowflake_chapter_plans"] == 1
    assert summary["planned_counts"]["narrative_events"] == 1
    assert summary["planned_counts"]["volume_summaries"] == 1
    assert summary["planned_counts"]["review_derived_snoozes"] == 1
    assert "reference_books" not in summary["planned_counts"]
    assert summary["preserved_domains"] == [
        "ReviewItem / LlmCall 中的历史 reference 审计痕迹",
        "SystemConfigSnapshot / SystemSecret",
        "config/models.yaml and config/prompts.yaml",
    ]
    assert _count(session, StoryProject) == 2
    assert _count(session, ReviewItem) == 3
    assert session.get(ReviewItem, "review_reference_finding") is not None


def test_execute_reset_clears_author_state_and_preserves_reference_audit_traces(session) -> None:
    _seed_all(session)

    summary = execute_reset(session)
    session.commit()

    assert summary["mode"] == "execute"
    assert summary["status"] == "ok"
    assert summary["deleted_counts"]["story_projects"] == 2
    assert summary["deleted_counts"]["review_items"] == 2
    assert summary["deleted_counts"]["llm_calls"] == 1
    assert summary["deleted_counts"]["llm_call_attempts"] == 1

    deleted_models = [
        StoryProject,
        OutlinePlan,
        SnowflakeArtifact,
        SnowflakeStepRun,
        SnowflakeCharacterPlan,
        SnowflakeChapterPlan,
        SnowflakeScenePlan,
        SnowflakeSceneTriageItem,
        SnowflakeRevisionLink,
        StoryCharacter,
        ProjectWritingStats,
        NarrativeEvent,
        VolumeSummary,
        ReviewDerivedSnooze,
        ChapterGoal,
        ChapterState,
        SceneCard,
        SceneRunState,
        SceneBundle,
        SceneBlueprint,
        SceneExecutionContract,
        GenerationPlanningArtifact,
        SceneDraft,
        QcReport,
        WriterEvaluation,
        RevisionCandidate,
        PassagePatchCandidate,
        AuthorDraft,
        AuthorDraftProposal,
        AuthorDraftEvent,
        AuthorPreferenceProfile,
        FinalScene,
        SceneMemory,
        ChapterMemory,
        ChapterRollingNote,
        AttemptTracker,
        ChapterRunJob,
        HumanReviewEvent,
        VoiceProfile,
        RelationProfile,
        IdempotencyKey,
        OperationLog,
    ]
    assert all(_count(session, model) == 0 for model in deleted_models)
    assert _count(session, ReviewItem) == 1
    assert _count(session, LlmCall) == 1
    assert _count(session, LlmCallAttempt) == 1
    assert session.get(LlmCallAttempt, "attempt_reference_preserved") is not None
    assert session.get(ReviewItem, "review_reference_finding") is not None
    assert session.get(LlmCall, "llm_call_reference_profile") is not None
    assert _count(session, SystemConfigSnapshot) == 1
    assert _count(session, SystemSecret) == 1


def test_execute_reset_is_safe_on_empty_db_and_idempotent(session) -> None:
    first = collect_reset_summary(session)
    deleted = execute_reset(session)
    session.commit()
    second = execute_reset(session)
    session.commit()

    assert all(count == 0 for count in first["planned_counts"].values())
    assert all(count == 0 for count in deleted["deleted_counts"].values())
    assert all(count == 0 for count in second["deleted_counts"].values())


def test_reset_cli_outputs_json_and_does_not_mutate_without_execute(session, capsys) -> None:
    _seed_all(session)

    main([])
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "dry_run"
    assert payload["planned_counts"]["story_projects"] == 2
    assert _count(session, StoryProject) == 2
    assert session.get(ReviewItem, "review_reference_finding") is not None


def test_reset_allows_creating_a_new_snowflake_workspace_after_cleanup(session, client) -> None:
    _seed_all(session)
    execute_reset(session)
    session.commit()

    project = _create_v2_project(client)
    workspace_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")

    assert project["planning_mode"] == "snowflake"
    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()["data"]
    assert workspace["project"]["project_id"] == project["project_id"]
    assert workspace["current_step_key"] == "book_brief"
