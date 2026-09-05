from __future__ import annotations

import ast
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex

from novel_system.accounting_contract import DEFAULT_PROVIDER_ATTEMPT_BUDGET
from novel_system.db.base import Base
from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    FinalScene,
    LlmCall,
    QcReport,
    SceneCard,
    SceneDraft,
    StoryProject,
)


def _alembic_head() -> str:
    """当前迁移链的 head。

    这两处断言的意思是「upgrade head 确实跑到了头」，不是「head 必须永远是某个具体
    修订号」——以前写死修订号，于是每加一条迁移这个测试就过期一次（本次就是被未提交
    的 0074 挂在 0073 上）。从脚本目录取真值，链再长也不会假红。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


EXPECTED_PATCH_CANDIDATE_METADATA_COLUMNS = {
    "candidate_category",
    "target_range_json",
    "revision_strategy",
    "preference_tags_json",
    "inserted_into_author_draft",
}

EXPECTED_AUTHOR_DRAFT_PROPOSAL_COLUMNS = {
    "proposal_id",
    "draft_id",
    "object_type",
    "object_id",
    "proposal_type",
    "proposal_source",
    "content",
    "rationale",
    "source_llm_call_id",
    "target_range_json",
    "before_text_hash",
    "replacement_text",
    "proposal_kind",
    "source_evaluation_id",
    "merge_status",
    "status",
    "author_decision_note",
}

EXPECTED_WORK_PROFILE_COLUMNS = {
    "profile_id",
    "scope_type",
    "scope_ref_id",
    "profile_key",
    "display_name",
    "description",
    "profile_json",
    "status",
    "created_by",
}

EXPECTED_STORY_PROJECT_COLUMNS = {
    "project_id",
    "title",
    "genre",
    "target_word_count",
    "target_chapter_count",
    "outline_text",
    "planning_mode",
    "snowflake_schema_version",
    "snowflake_workflow_mode",
    "status",
    "active_outline_plan_id",
    "current_chapter_id",
    "approved_chapter_ids_json",
}

EXPECTED_OUTLINE_PLAN_COLUMNS = {
    "plan_id",
    "project_id",
    "version",
    "status",
    "plan_json",
    "approved_at",
}

EXPECTED_SNOWFLAKE_ARTIFACT_COLUMNS = {
    "artifact_id",
    "project_id",
    "step_key",
    "version",
    "status",
    "artifact_json",
    "input_refs_json",
    "diagnosis_json",
    "llm_call_id",
    "approved_at",
}

EXPECTED_STORY_CHARACTER_COLUMNS = {
    "character_id",
    "project_id",
    "display_name",
    "role",
    "summary_json",
    "bible_json",
    "status",
}

EXPECTED_SNOWFLAKE_ASSISTANT_TURN_COLUMNS = {
    "turn_id",
    "project_id",
    "step_key",
    "focus_scene_id",
    "user_message",
    "reply",
    "suggestions_json",
    "candidate_label",
    "candidate_patch_json",
    "source",
    "llm_call_id",
    "created_at",
}

EXPECTED_LLM_CALL_COLUMNS = {
    "llm_call_id",
    "provider",
    "provider_id",
    "account_id",
    "model",
    "node_id",
    "reasoning_level",
    "native_reasoning_json",
    "credential_mode",
    "prompt_hash",
    "step",
    "project_id",
    "scene_id",
    "chapter_id",
    "request_payload_summary",
    "response_payload_summary",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "latency_ms",
    "finish_reason",
    "error_code",
    "scope_type",
    "scope_id",
    "run_job_id",
    "execution_id",
    "execution_step_key",
    "estimated_tokens",
    "reserved_tokens",
    "budget_charged_tokens",
    "usage_is_estimate",
    "accounting_status",
    "request_dispatched_at",
    "settled_at",
}


def test_llm_execution_step_claim_has_postgresql_partial_unique_ddl() -> None:
    claim_index = next(
        index
        for index in LlmCall.__table__.indexes
        if index.name == "uq_llm_calls_execution_step_claim"
    )
    ddl = str(CreateIndex(claim_index).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX uq_llm_calls_execution_step_claim" in ddl
    assert "execution_id IS NOT NULL" in ddl
    assert "execution_step_key IS NOT NULL" in ddl
    assert "request_dispatched_at IS NULL" in ddl
    assert "accounting_status IN ('released','rejected')" in ddl

EXPECTED_LLM_CALL_ATTEMPT_COLUMNS = {
    "attempt_id",
    "llm_call_id",
    "provider_attempt_no",
    "dispatch_kind",
    "request_max_output_tokens",
    "provider_request_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_tokens",
    "reserved_tokens",
    "budget_charged_tokens",
    "usage_is_estimate",
    "accounting_status",
    "request_dispatched_at",
    "settled_at",
    "latency_ms",
    "error_code",
    "error_text",
    "created_at",
}


def _prepare_style_reference_backup_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "style_reference_test_repo"
    backup_dir = repo_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "style_reference_legacy_test.json").write_text(
        '{"row_count": 0, "profiles": [], "source": "generation-persistence"}',
        encoding="utf-8",
    )
    return repo_root


def _materialize_legacy_dynamic_checkout(db_path: Path, revision: str) -> None:
    """Reproduce databases created when 0001 still called live Base.metadata.

    Those checkouts materialized the then-current ORM schema first and only recorded
    an older Alembic revision. New migrations are frozen, so this compatibility
    fixture constructs that already-published database shape explicitly.
    """

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)
        with Session(engine) as session:
            session.add(
                StoryProject(
                    project_id="PRJ_DEMO",
                    title="Historical generation fixture",
                    outline_text="Compatibility parent rows",
                )
            )
            session.add(
                ChapterGoal(
                    chapter_id="CH001",
                    project_id="PRJ_DEMO",
                    chapter_goal="Preserve historical generation rows",
                    display_order=1,
                )
            )
            session.add(
                SceneCard(
                    scene_id="CH001_SC01",
                    chapter_id="CH001",
                    project_id="PRJ_DEMO",
                    scene_seq=1,
                    scene_goal="Historical scene",
                    onstage_chars_json=[],
                    beats_json=[],
                )
            )
            session.commit()
    finally:
        engine.dispose()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (revision,),
        )


def test_generation_persistence_migration_is_frozen_with_explicit_ddl() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "20260414_0007_add_llm_qc_and_chapter_jobs.py"
    )
    migration_source = migration_path.read_text(encoding="utf-8")
    migration_module = ast.parse(migration_source)
    downgrade_function = next(
        node for node in migration_module.body if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
    )

    assert re.search(r'op\.create_table\(\s*"llm_calls"', migration_source)
    assert re.search(r'op\.create_table\(\s*"qc_reports"', migration_source)
    assert re.search(r'op\.create_table\(\s*"chapter_run_jobs"', migration_source)
    assert "Base.metadata.create_all" not in migration_source
    assert ".drop(bind=" not in migration_source
    assert re.search(r"dynamic base migration", migration_source)
    assert len(downgrade_function.body) == 1
    assert isinstance(downgrade_function.body[0], ast.Pass)


def test_generation_persistence_alembic_schema_contract(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-persistence-head.sqlite"
    backup_root = _prepare_style_reference_backup_root(tmp_path)

    _run_alembic(repo_root, db_path, "head", backup_root=backup_root)

    connection = sqlite3.connect(db_path)
    try:
        table_names = _table_names(connection)
        llm_columns = _pragma_columns_by_name(connection, "llm_calls")
        attempt_columns = _pragma_columns_by_name(connection, "llm_call_attempts")
        qc_columns = _pragma_columns_by_name(connection, "qc_reports")
        draft_columns = _pragma_columns_by_name(connection, "scene_drafts")
        final_columns = _pragma_columns_by_name(connection, "final_scenes")
        chapter_job_columns = _pragma_columns_by_name(connection, "chapter_run_jobs")
        patch_columns = _pragma_columns_by_name(connection, "passage_patch_candidates")
        proposal_columns = _pragma_columns_by_name(connection, "author_draft_proposals")
        story_project_columns = _pragma_columns_by_name(connection, "story_projects")
        outline_plan_columns = _pragma_columns_by_name(connection, "outline_plans")
        snowflake_columns = _pragma_columns_by_name(connection, "snowflake_artifacts")
        story_character_columns = _pragma_columns_by_name(connection, "story_characters")
        assistant_turn_columns = _pragma_columns_by_name(connection, "snowflake_assistant_turns")
        chapter_goal_columns = _pragma_columns_by_name(connection, "chapter_goals")
        scene_card_columns = _pragma_columns_by_name(connection, "scene_cards")
    finally:
        connection.close()

    assert "llm_calls" in table_names
    assert "llm_call_attempts" in table_names
    assert "qc_reports" in table_names
    assert "chapter_run_jobs" in table_names
    assert "author_draft_proposals" in table_names
    assert "story_projects" in table_names
    assert "outline_plans" in table_names
    assert "snowflake_artifacts" in table_names
    assert "story_characters" in table_names
    assert "snowflake_assistant_turns" in table_names
    assert EXPECTED_LLM_CALL_COLUMNS <= llm_columns.keys()
    assert EXPECTED_LLM_CALL_ATTEMPT_COLUMNS == attempt_columns.keys()
    assert {
        "qc_report_id",
        "scene_id",
        "chapter_id",
        "qc_type",
        "source_draft_row_id",
        "source_bundle_id",
        "resolution_code",
        "pass_flag",
        "next_action",
        "issues_json",
        "rewrite_brief_json",
    } <= qc_columns.keys()
    assert "generation_llm_call_id" in draft_columns
    assert "generation_llm_call_id" in final_columns
    assert {"job_id", "chapter_id", "status", "job_type"} <= chapter_job_columns.keys()
    assert EXPECTED_PATCH_CANDIDATE_METADATA_COLUMNS <= patch_columns.keys()
    assert EXPECTED_AUTHOR_DRAFT_PROPOSAL_COLUMNS <= proposal_columns.keys()
    assert EXPECTED_STORY_PROJECT_COLUMNS <= story_project_columns.keys()
    assert "reference_profile_ids_json" not in story_project_columns
    assert EXPECTED_OUTLINE_PLAN_COLUMNS <= outline_plan_columns.keys()
    assert EXPECTED_SNOWFLAKE_ARTIFACT_COLUMNS <= snowflake_columns.keys()
    assert EXPECTED_STORY_CHARACTER_COLUMNS <= story_character_columns.keys()
    assert EXPECTED_SNOWFLAKE_ASSISTANT_TURN_COLUMNS <= assistant_turn_columns.keys()
    if "chapter_goals" in table_names:
        assert {"project_id", "outline_plan_id"} <= chapter_goal_columns.keys()
    if "scene_cards" in table_names:
        assert {"project_id", "outline_plan_id"} <= scene_card_columns.keys()
    assert chapter_job_columns["status"][3] == 1
    assert chapter_job_columns["job_type"][3] == 1


def test_generation_persistence_orm_round_trip(session) -> None:
    session.add(
        StoryProject(
            project_id="PRJ_DEMO",
            title="Generation persistence",
            outline_text="Round-trip parent rows",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id="CH001",
            project_id="PRJ_DEMO",
            chapter_goal="Persist generation artifacts",
            display_order=1,
        )
    )
    session.add(
        SceneCard(
            scene_id="CH001_SC01",
            chapter_id="CH001",
            project_id="PRJ_DEMO",
            scene_seq=1,
            scene_goal="Round-trip scene",
            onstage_chars_json=[],
            beats_json=[],
        )
    )
    session.flush()

    llm_call = LlmCall(
        llm_call_id="llm_call_scene_CH001_SC01_style",
        scope_type="scene",
        scope_id="CH001_SC01",
        provider="demo-provider",
        model="demo-model",
        prompt_hash="hash_prompt_demo",
        project_id="PRJ_DEMO",
        step="style_draft",
        scene_id="CH001_SC01",
        chapter_id="CH001",
        request_payload_summary={"messages": 3, "temperature": 0.7},
        response_payload_summary={"choice_count": 1},
        prompt_tokens=123,
        completion_tokens=456,
        total_tokens=579,
        latency_ms=2300,
        finish_reason="stop",
        error_code=None,
    )
    session.add(llm_call)
    session.add(
        ChapterRunJob(
            job_id="chapter_job_CH001_qc_pass",
            chapter_id="CH001",
            status="queued",
            job_type="chapter_qc",
        )
    )
    session.add(
        SceneDraft(
            row_id="draft_CH001_SC01_style_v1",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            stage="style_draft",
            content="draft text",
            source_bundle_id="bundle_CH001_SC01",
            source_bundle_hash="bundle_hash_demo",
            generation_llm_call_id=llm_call.llm_call_id,
        )
    )
    session.add(
        FinalScene(
            row_id="final_scene_CH001_SC01_v1",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            content="final text",
            status="approved",
            source_bundle_id="bundle_CH001_SC01",
            source_bundle_hash="bundle_hash_demo",
            generation_llm_call_id=llm_call.llm_call_id,
        )
    )
    session.add(
        QcReport(
            qc_report_id="qc_report_CH001_SC01_hard_v1",
            scene_id="CH001_SC01",
            chapter_id="CH001",
            qc_type="hard_qc",
            source_draft_row_id="draft_CH001_SC01_style_v1",
            source_bundle_id="bundle_CH001_SC01",
            resolution_code="hard_pass",
            pass_flag=1,
            next_action="pass",
            issues_json=[{"issue_key": "continuity_ok"}],
            rewrite_brief_json=[],
        )
    )
    session.commit()

    stored_qc = session.get(QcReport, "qc_report_CH001_SC01_hard_v1")
    stored_draft = session.get(SceneDraft, "draft_CH001_SC01_style_v1")
    stored_final = session.get(FinalScene, "final_scene_CH001_SC01_v1")

    assert stored_qc is not None
    assert stored_qc.source_draft_row_id == "draft_CH001_SC01_style_v1"
    assert stored_qc.issues_json == [{"issue_key": "continuity_ok"}]
    assert stored_draft is not None
    assert stored_draft.generation_llm_call_id == llm_call.llm_call_id
    assert stored_final is not None
    assert stored_final.generation_llm_call_id == llm_call.llm_call_id


def test_generation_persistence_upgrade_keeps_historical_rows_readable(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-persistence.sqlite"
    backup_root = _prepare_style_reference_backup_root(tmp_path)

    _build_true_pre_0007_database(db_path)
    _run_alembic(repo_root, db_path, "head", backup_root=backup_root)

    connection = sqlite3.connect(db_path)
    try:
        table_names = _table_names(connection)
        draft_columns = _pragma_columns_by_name(connection, "scene_drafts")
        final_columns = _pragma_columns_by_name(connection, "final_scenes")
        llm_columns = _pragma_columns_by_name(connection, "llm_calls")
        qc_columns = _pragma_columns_by_name(connection, "qc_reports")
        job_columns = _pragma_columns_by_name(connection, "chapter_run_jobs")
        config_columns = _pragma_columns_by_name(connection, "system_config_snapshots")
        secret_columns = _pragma_columns_by_name(connection, "system_secrets")
        planning_columns = _pragma_columns_by_name(connection, "generation_planning_artifacts")
        patch_columns = _pragma_columns_by_name(connection, "passage_patch_candidates")
        proposal_columns = _pragma_columns_by_name(connection, "author_draft_proposals")
        story_project_columns = _pragma_columns_by_name(connection, "story_projects")
        outline_plan_columns = _pragma_columns_by_name(connection, "outline_plans")
        snowflake_columns = _pragma_columns_by_name(connection, "snowflake_artifacts")
        story_character_columns = _pragma_columns_by_name(connection, "story_characters")
        assistant_turn_columns = _pragma_columns_by_name(connection, "snowflake_assistant_turns")
        chapter_goal_columns = _pragma_columns_by_name(connection, "chapter_goals")
        scene_card_columns = _pragma_columns_by_name(connection, "scene_cards")
        historical_draft = connection.execute(
            """
            SELECT row_id, scene_id, chapter_id, stage, generation_llm_call_id
            FROM scene_drafts
            WHERE row_id = 'draft_hist_CH001_SC01'
            """
        ).fetchone()
        historical_final = connection.execute(
            """
            SELECT row_id, scene_id, chapter_id, status, generation_llm_call_id
            FROM final_scenes
            WHERE row_id = 'final_hist_CH001_SC01'
            """
        ).fetchone()
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()

        assert version_row == (_alembic_head(),)
    assert "llm_calls" in table_names
    assert "qc_reports" in table_names
    assert "chapter_run_jobs" in table_names
    assert "scene_blueprints" in table_names
    assert "passage_patch_candidates" in table_names
    assert "author_preference_profiles" in table_names
    assert "author_drafts" in table_names
    assert "author_draft_events" in table_names
    assert "author_draft_proposals" in table_names
    assert "story_projects" in table_names
    assert "outline_plans" in table_names
    assert "snowflake_artifacts" in table_names
    assert "story_characters" in table_names
    assert "snowflake_assistant_turns" in table_names
    assert "scene_execution_contracts" in table_names
    assert "generation_planning_artifacts" in table_names
    assert "system_config_snapshots" in table_names
    assert "system_secrets" in table_names
    assert "generation_llm_call_id" in draft_columns
    assert "status" in draft_columns
    assert "generation_llm_call_id" in final_columns
    assert {"llm_call_id", "provider", "provider_id", "account_id", "model", "node_id"} <= llm_columns.keys()
    assert {"qc_report_id", "source_draft_row_id", "issues_json", "status"} <= qc_columns.keys()
    assert {"job_id", "chapter_id", "status", "job_type"} <= job_columns.keys()
    assert {"snapshot_id", "category", "version", "yaml_raw", "active_flag"} <= config_columns.keys()
    assert {"secret_id", "encrypted_value", "value_hint", "secret_type", "metadata_json", "expires_at"} <= secret_columns.keys()
    assert {
        "row_id",
        "artifact_type",
        "object_type",
        "object_id",
        "payload_json",
        "status",
    } <= planning_columns.keys()
    assert EXPECTED_PATCH_CANDIDATE_METADATA_COLUMNS <= patch_columns.keys()
    assert EXPECTED_AUTHOR_DRAFT_PROPOSAL_COLUMNS <= proposal_columns.keys()
    assert EXPECTED_STORY_PROJECT_COLUMNS <= story_project_columns.keys()
    assert "reference_profile_ids_json" not in story_project_columns
    assert EXPECTED_OUTLINE_PLAN_COLUMNS <= outline_plan_columns.keys()
    assert EXPECTED_SNOWFLAKE_ARTIFACT_COLUMNS <= snowflake_columns.keys()
    assert EXPECTED_STORY_CHARACTER_COLUMNS <= story_character_columns.keys()
    assert EXPECTED_SNOWFLAKE_ASSISTANT_TURN_COLUMNS <= assistant_turn_columns.keys()
    if "chapter_goals" in table_names:
        assert {"project_id", "outline_plan_id"} <= chapter_goal_columns.keys()
    if "scene_cards" in table_names:
        assert {"project_id", "outline_plan_id"} <= scene_card_columns.keys()
    assert job_columns["status"][3] == 1
    assert job_columns["job_type"][3] == 1
    assert historical_draft == ("draft_hist_CH001_SC01", "CH001_SC01", "CH001", "neutral_draft", None)
    assert historical_final == ("final_hist_CH001_SC01", "CH001_SC01", "CH001", "approved", None)


def test_generation_persistence_downgrade_is_non_destructive_on_dynamic_checkout(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-persistence-downgrade.sqlite"
    backup_root = _prepare_style_reference_backup_root(tmp_path)

    _materialize_legacy_dynamic_checkout(db_path, "20260414_0007")
    _run_alembic_downgrade(repo_root, db_path, "20260413_0006", backup_root=backup_root)

    connection = sqlite3.connect(db_path)
    try:
        table_names = _table_names(connection)
        draft_columns = _pragma_columns_by_name(connection, "scene_drafts")
        final_columns = _pragma_columns_by_name(connection, "final_scenes")
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()

    assert version_row == ("20260413_0006",)
    assert "llm_calls" in table_names
    assert "qc_reports" in table_names
    assert "chapter_run_jobs" in table_names
    assert "generation_llm_call_id" in draft_columns
    assert "generation_llm_call_id" in final_columns


def test_generation_persistence_upgrade_is_idempotent_when_0006_already_materialized_task3_objects(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "generation-persistence-idempotent.sqlite"
    backup_root = _prepare_style_reference_backup_root(tmp_path)

    _materialize_legacy_dynamic_checkout(db_path, "20260413_0006")
    _seed_dynamic_0006_materialized_generation_rows(db_path)
    _run_alembic(repo_root, db_path, "head", backup_root=backup_root)

    connection = sqlite3.connect(db_path)
    try:
        table_names = _table_names(connection)
        llm_call = connection.execute(
            """
            SELECT llm_call_id, provider, model, step, total_tokens
            FROM llm_calls
            WHERE llm_call_id = 'llm_call_existing'
            """
        ).fetchone()
        qc_report = connection.execute(
            """
            SELECT qc_report_id, source_draft_row_id, source_bundle_id, next_action
            FROM qc_reports
            WHERE qc_report_id = 'qc_report_existing'
            """
        ).fetchone()
        chapter_job = connection.execute(
            """
            SELECT job_id, chapter_id, status, job_type
            FROM chapter_run_jobs
            WHERE job_id = 'chapter_job_existing'
            """
        ).fetchone()
        config_columns = _pragma_columns_by_name(connection, "system_config_snapshots")
        secret_columns = _pragma_columns_by_name(connection, "system_secrets")
        planning_columns = _pragma_columns_by_name(connection, "generation_planning_artifacts")
        patch_columns = _pragma_columns_by_name(connection, "passage_patch_candidates")
        proposal_columns = _pragma_columns_by_name(connection, "author_draft_proposals")
        story_project_columns = _pragma_columns_by_name(connection, "story_projects")
        outline_plan_columns = _pragma_columns_by_name(connection, "outline_plans")
        snowflake_columns = _pragma_columns_by_name(connection, "snowflake_artifacts")
        story_character_columns = _pragma_columns_by_name(connection, "story_characters")
        assistant_turn_columns = _pragma_columns_by_name(connection, "snowflake_assistant_turns")
        chapter_goal_columns = _pragma_columns_by_name(connection, "chapter_goals")
        scene_card_columns = _pragma_columns_by_name(connection, "scene_cards")
        scene_draft = connection.execute(
            """
            SELECT row_id, generation_llm_call_id
            FROM scene_drafts
            WHERE row_id = 'draft_existing'
            """
        ).fetchone()
        final_scene = connection.execute(
            """
            SELECT row_id, generation_llm_call_id
            FROM final_scenes
            WHERE row_id = 'final_existing'
            """
        ).fetchone()
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()

    assert version_row == (_alembic_head(),)
    assert "llm_calls" in table_names
    assert "qc_reports" in table_names
    assert "chapter_run_jobs" in table_names
    assert "scene_blueprints" in table_names
    assert "passage_patch_candidates" in table_names
    assert "author_preference_profiles" in table_names
    assert "author_drafts" in table_names
    assert "author_draft_events" in table_names
    assert "author_draft_proposals" in table_names
    assert "scene_execution_contracts" in table_names
    assert "story_projects" in table_names
    assert "outline_plans" in table_names
    assert "snowflake_artifacts" in table_names
    assert "story_characters" in table_names
    assert "snowflake_assistant_turns" in table_names
    assert "generation_planning_artifacts" in table_names
    assert "system_config_snapshots" in table_names
    assert "system_secrets" in table_names
    assert {"snapshot_id", "category", "version", "yaml_raw", "active_flag"} <= config_columns.keys()
    assert {"secret_id", "encrypted_value", "value_hint", "secret_type", "metadata_json", "expires_at"} <= secret_columns.keys()
    assert {
        "row_id",
        "artifact_type",
        "object_type",
        "object_id",
        "payload_json",
        "status",
    } <= planning_columns.keys()
    assert EXPECTED_PATCH_CANDIDATE_METADATA_COLUMNS <= patch_columns.keys()
    assert EXPECTED_AUTHOR_DRAFT_PROPOSAL_COLUMNS <= proposal_columns.keys()
    assert EXPECTED_STORY_PROJECT_COLUMNS <= story_project_columns.keys()
    assert "reference_profile_ids_json" not in story_project_columns
    assert EXPECTED_OUTLINE_PLAN_COLUMNS <= outline_plan_columns.keys()
    assert EXPECTED_SNOWFLAKE_ARTIFACT_COLUMNS <= snowflake_columns.keys()
    assert EXPECTED_STORY_CHARACTER_COLUMNS <= story_character_columns.keys()
    assert EXPECTED_SNOWFLAKE_ASSISTANT_TURN_COLUMNS <= assistant_turn_columns.keys()
    assert {"project_id", "outline_plan_id"} <= chapter_goal_columns.keys()
    assert {"project_id", "outline_plan_id"} <= scene_card_columns.keys()
    assert llm_call == ("llm_call_existing", "seed-provider", "seed-model", "style_draft", 42)
    assert qc_report == ("qc_report_existing", "draft_existing", "bundle_existing", "pass")
    assert chapter_job == ("chapter_job_existing", "CH001", "queued", "chapter_qc")
    assert scene_draft == ("draft_existing", "llm_call_existing")
    assert final_scene == ("final_existing", "llm_call_existing")


def test_c1b_migration_upgrades_a_0064_copy_with_conservative_backfill(tmp_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    migration_source = (
        backend_dir / "alembic" / "versions" / "20260713_0065_llm_accounting_budget_cancel.py"
    ).read_text(encoding="utf-8")
    assert "novel_system" not in migration_source
    assert "MIGRATION_PROVIDER_ATTEMPT_BUDGET = 32" in migration_source
    assert "server_default=str(MIGRATION_PROVIDER_ATTEMPT_BUDGET)" in migration_source
    db_path = tmp_path / "c1b-legacy-0064.sqlite"
    _build_c1b_legacy_0064_database(db_path)

    _run_alembic(backend_dir, db_path, "20260713_0065")

    with sqlite3.connect(db_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = _table_names(connection)
        scene_columns = _pragma_columns_by_name(connection, "scene_run_states")
        llm_columns = _pragma_columns_by_name(connection, "llm_calls")
        attempt_columns = _pragma_columns_by_name(connection, "llm_call_attempts")
        job_columns = _pragma_columns_by_name(connection, "chapter_run_jobs")
        attempts = connection.execute("SELECT COUNT(*) FROM llm_call_attempts").fetchone()[0]
        orphans = connection.execute(
            """
            SELECT COUNT(*)
            FROM llm_call_attempts AS attempt
            LEFT JOIN llm_calls AS call ON call.llm_call_id = attempt.llm_call_id
            WHERE call.llm_call_id IS NULL
            """
        ).fetchone()[0]
        charged_over_reservation = connection.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE budget_charged_tokens > reserved_tokens"
        ).fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_list(llm_call_attempts)").fetchall()
        llm_rows = connection.execute(
            """
            SELECT llm_call_id, scope_type, scope_id, estimated_tokens,
                   reserved_tokens, budget_charged_tokens, usage_is_estimate,
                   accounting_status
            FROM llm_calls
            ORDER BY llm_call_id
            """
        ).fetchall()
        job_rows = connection.execute(
            "SELECT job_id, scene_id FROM chapter_run_jobs ORDER BY job_id"
        ).fetchall()
        indexes = {
            row[1]
            for table in ("llm_calls", "llm_call_attempts", "chapter_run_jobs")
            for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE scene_run_states SET scene_tokens_reserved = -1 WHERE scene_id = 'SC001'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE llm_calls SET budget_charged_tokens = -1 WHERE llm_call_id = 'call-scene'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE llm_calls SET budget_charged_tokens = 1 WHERE llm_call_id = 'call-scene'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE llm_calls SET accounting_status = 'unknown' WHERE llm_call_id = 'call-scene'"
            )
        connection.execute(
            """
            INSERT INTO llm_calls (
                llm_call_id, node_id, created_at, scope_type, scope_id,
                execution_id, execution_step_key, accounting_status,
                request_dispatched_at
            ) VALUES (
                'claim-released', 'claim', '2026-07-13T02:00:00+00:00',
                'system', 'claim', 'claim-execution', 'claim-step',
                'released', NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO llm_calls (
                llm_call_id, node_id, created_at, scope_type, scope_id,
                execution_id, execution_step_key, accounting_status
            ) VALUES (
                'claim-retry', 'claim', '2026-07-13T02:00:01+00:00',
                'system', 'claim', 'claim-execution', 'claim-step', 'reserved'
            )
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO llm_calls (
                    llm_call_id, node_id, created_at, scope_type, scope_id,
                    execution_id, execution_step_key, accounting_status
                ) VALUES (
                    'claim-duplicate', 'claim', '2026-07-13T02:00:02+00:00',
                    'system', 'claim', 'claim-execution', 'claim-step', 'reserved'
                )
                """
            )

    assert version == ("20260713_0065",)
    assert "llm_call_attempts" in tables
    assert {
        "scene_tokens_reserved",
        "scene_budget_basis_json",
        "provider_attempts_used",
        "provider_attempt_budget",
        "active_execution_id",
        "run_execution_status",
        "run_checkpoint",
        "run_checkpoint_json",
        "active_run_job_id",
    } <= scene_columns.keys()
    assert (
        int(str(scene_columns["provider_attempt_budget"][4]).strip("'\""))
        == DEFAULT_PROVIDER_ATTEMPT_BUDGET
    )
    assert {
        "scope_type",
        "scope_id",
        "run_job_id",
        "execution_id",
        "execution_step_key",
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "usage_is_estimate",
        "accounting_status",
        "request_dispatched_at",
        "settled_at",
    } <= llm_columns.keys()
    assert EXPECTED_LLM_CALL_ATTEMPT_COLUMNS == attempt_columns.keys()
    assert "scene_id" in job_columns
    assert attempts == 0  # historical logical calls must not fabricate physical attempts
    assert orphans == 0
    assert foreign_keys[0][2:7] == ("llm_calls", "llm_call_id", "llm_call_id", "NO ACTION", "NO ACTION")
    assert llm_rows == [
        ("call-chapter", "chapter", "CH001", 99, 0, 0, 1, "failed"),
        ("call-project", "project", "PRJ001", 30, 0, 0, 1, "settled"),
        ("call-scene", "scene", "SC001", 42, 0, 0, 1, "settled"),
        ("call-system", "system", "legacy_failure", 0, 0, 0, 1, "failed"),
    ]
    assert charged_over_reservation == 0
    assert job_rows == [
        ("job-empty", None),
        ("job-payload", "SC_PAYLOAD"),
        ("job-result", "SC_RESULT"),
    ]
    assert {
        "ix_llm_calls_scope_created",
        "ix_llm_calls_run_job",
        "ix_llm_calls_execution_step",
        "uq_llm_calls_execution_step_claim",
        "ix_llm_calls_accounting_status",
        "ix_llm_call_attempts_call_status",
        "ix_chapter_run_jobs_scene_created",
    } <= indexes

    _run_alembic_downgrade(backend_dir, db_path, "20260712_0064")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260712_0064",
        )
        assert "llm_call_attempts" not in _table_names(connection)
        assert "scene_tokens_reserved" not in _pragma_columns_by_name(connection, "scene_run_states")
        assert "scope_type" not in _pragma_columns_by_name(connection, "llm_calls")
        assert "scene_id" not in _pragma_columns_by_name(connection, "chapter_run_jobs")


def test_c1b_migration_partial_replay_preserves_new_accounting_rows(tmp_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "c1b-partial-replay.sqlite"
    _build_c1b_legacy_0064_database(db_path)
    _run_alembic(backend_dir, db_path, "20260713_0065")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO llm_calls (
                llm_call_id, node_id, project_id, total_tokens, created_at,
                scope_type, scope_id, run_job_id, execution_id, execution_step_key,
                estimated_tokens, reserved_tokens, budget_charged_tokens,
                usage_is_estimate, accounting_status, request_dispatched_at, settled_at
            ) VALUES (
                'call-post-0065', 'new_accounting', 'PRJ_DENORMALIZED', 13,
                '2026-07-13T01:02:03+00:00',
                'project', 'PRJ_AUTHORITATIVE', 'job-new', 'execution-new', 'step-new',
                77, 80, 70, 0, 'settled',
                '2026-07-13T01:02:00+00:00', '2026-07-13T01:02:03+00:00'
            )
            """
        )
        new_row_before_replay = connection.execute(
            "SELECT * FROM llm_calls WHERE llm_call_id = 'call-post-0065'"
        ).fetchone()
        connection.execute(
            "UPDATE alembic_version SET version_num = '20260712_0064'"
        )

    _run_alembic(backend_dir, db_path, "20260713_0065")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "20260713_0065",
        )
        assert connection.execute(
            "SELECT * FROM llm_calls WHERE llm_call_id = 'call-post-0065'"
        ).fetchone() == new_row_before_replay
        assert connection.execute(
            """
            SELECT llm_call_id, scope_type, scope_id, estimated_tokens,
                   reserved_tokens, budget_charged_tokens, usage_is_estimate,
                   accounting_status
            FROM llm_calls
            WHERE llm_call_id != 'call-post-0065'
            ORDER BY llm_call_id
            """
        ).fetchall() == [
            ("call-chapter", "chapter", "CH001", 99, 0, 0, 1, "failed"),
            ("call-project", "project", "PRJ001", 30, 0, 0, 1, "settled"),
            ("call-scene", "scene", "SC001", 42, 0, 0, 1, "settled"),
            ("call-system", "system", "legacy_failure", 0, 0, 0, 1, "failed"),
        ]
        assert EXPECTED_LLM_CALL_ATTEMPT_COLUMNS == _pragma_columns_by_name(
            connection, "llm_call_attempts"
        ).keys()
        attempt_table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_call_attempts'"
        ).fetchone()[0]
        for constraint_name in (
            "ck_llm_call_attempts_provider_attempt_no_nonnegative",
            "ck_llm_call_attempts_budget_charged_within_reservation",
            "ck_llm_call_attempts_accounting_status",
            "ck_llm_call_attempts_dispatch_kind",
            "uq_llm_call_attempts_call_ordinal",
        ):
            assert constraint_name in attempt_table_sql
        assert "ix_llm_call_attempts_call_status" in {
            row[1] for row in connection.execute("PRAGMA index_list(llm_call_attempts)")
        }


def _run_alembic(backend_dir: Path, db_path: Path, revision: str, *, backup_root: Path | None = None) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir / "src")
    env["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    if backup_root is not None:
        env["STYLE_REFERENCE_REPO_ROOT"] = str(backup_root)

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend_dir / "alembic.ini"), "upgrade", revision],
        cwd=backend_dir,
        env=env,
        check=True,
    )


def _build_c1b_legacy_0064_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version (version_num) VALUES ('20260712_0064');

            CREATE TABLE scene_run_states (
                scene_id VARCHAR NOT NULL PRIMARY KEY,
                scene_token_budget INTEGER,
                scene_tokens_used INTEGER NOT NULL DEFAULT 0,
                updated_at VARCHAR NOT NULL
            );
            INSERT INTO scene_run_states (
                scene_id, scene_token_budget, scene_tokens_used, updated_at
            ) VALUES ('SC001', 500, 120, '2026-07-12T00:00:00+00:00');

            CREATE TABLE llm_calls (
                llm_call_id VARCHAR NOT NULL PRIMARY KEY,
                provider VARCHAR,
                model VARCHAR,
                node_id VARCHAR,
                prompt_hash VARCHAR,
                step VARCHAR,
                project_id VARCHAR,
                scene_id VARCHAR,
                chapter_id VARCHAR,
                request_payload_summary JSON,
                response_payload_summary JSON,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                latency_ms INTEGER,
                finish_reason VARCHAR,
                error_code VARCHAR,
                created_at VARCHAR NOT NULL
            );
            INSERT INTO llm_calls (
                llm_call_id, node_id, scene_id, prompt_tokens, completion_tokens,
                total_tokens, created_at
            ) VALUES (
                'call-scene', 'style_draft', 'SC001', 10, 32, 42,
                '2026-07-12T00:00:00+00:00'
            );
            INSERT INTO llm_calls (
                llm_call_id, node_id, project_id, prompt_tokens, completion_tokens,
                total_tokens, created_at
            ) VALUES (
                'call-project', 'outline', 'PRJ001', 10, 20, NULL,
                '2026-07-12T00:00:01+00:00'
            );
            INSERT INTO llm_calls (
                llm_call_id, node_id, chapter_id, total_tokens, error_code, created_at
            ) VALUES (
                'call-chapter', 'chapter_qc', 'CH001', 99, 'PROVIDER_FAILED',
                '2026-07-12T00:00:02+00:00'
            );
            INSERT INTO llm_calls (
                llm_call_id, node_id, error_code, created_at
            ) VALUES (
                'call-system', 'legacy_failure', 'PROVIDER_FAILED',
                '2026-07-12T00:00:03+00:00'
            );

            CREATE TABLE chapter_run_jobs (
                job_id VARCHAR NOT NULL PRIMARY KEY,
                chapter_id VARCHAR,
                status VARCHAR NOT NULL,
                job_type VARCHAR NOT NULL,
                payload_json JSON,
                result_summary_json JSON,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            );
            INSERT INTO chapter_run_jobs (
                job_id, status, job_type, payload_json, created_at, updated_at
            ) VALUES (
                'job-payload', 'completed', 'scene_run', '{"scene_id":"SC_PAYLOAD"}',
                '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00'
            );
            INSERT INTO chapter_run_jobs (
                job_id, status, job_type, result_summary_json, created_at, updated_at
            ) VALUES (
                'job-result', 'completed', 'scene_run', '{"scene_id":"SC_RESULT"}',
                '2026-07-12T00:00:01+00:00', '2026-07-12T00:00:01+00:00'
            );
            INSERT INTO chapter_run_jobs (
                job_id, status, job_type, created_at, updated_at
            ) VALUES (
                'job-empty', 'completed', 'chapter_run',
                '2026-07-12T00:00:02+00:00', '2026-07-12T00:00:02+00:00'
            );
            """
        )


def _run_alembic_downgrade(
    backend_dir: Path,
    db_path: Path,
    revision: str,
    *,
    backup_root: Path | None = None,
) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir / "src")
    env["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    if backup_root is not None:
        env["STYLE_REFERENCE_REPO_ROOT"] = str(backup_root)

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend_dir / "alembic.ini"), "downgrade", revision],
        cwd=backend_dir,
        env=env,
        check=True,
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _pragma_columns_by_name(connection: sqlite3.Connection, table_name: str) -> dict[str, tuple]:
    return {row[1]: row for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _build_true_pre_0007_database(db_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    _run_alembic(backend_dir, db_path, "20260413_0006")

    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            INSERT INTO chapter_goals (
                chapter_id,
                planned_scene_count,
                mid_aggregate_enabled,
                chapter_goal,
                created_at,
                updated_at,
                trashed_flag
            ) VALUES (
                'CH001', 1, 0, 'Historical generation chapter',
                '2026-04-13T00:00:00+00:00',
                '2026-04-13T00:00:00+00:00', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO scene_cards (
                scene_id,
                chapter_id,
                scene_seq,
                onstage_chars_json,
                scene_goal,
                beats_json,
                is_chapter_last,
                created_at,
                updated_at,
                trashed_flag
            ) VALUES (
                'CH001_SC01', 'CH001', 1, '[]', 'Historical scene', '[]', 1,
                '2026-04-13T00:00:00+00:00',
                '2026-04-13T00:00:00+00:00', 0
            )
            """
        )
        connection.execute(
            """
            INSERT INTO scene_drafts (
                row_id,
                scene_id,
                chapter_id,
                stage,
                content,
                source_bundle_id,
                source_bundle_hash,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "draft_hist_CH001_SC01",
                "CH001_SC01",
                "CH001",
                "neutral_draft",
                "historical draft text",
                "bundle_hist_CH001_SC01",
                "bundle_hash_hist",
                "2026-04-13T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO final_scenes (
                row_id,
                scene_id,
                chapter_id,
                content,
                status,
                source_bundle_id,
                source_bundle_hash,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "final_hist_CH001_SC01",
                "CH001_SC01",
                "CH001",
                "historical final text",
                "approved",
                "bundle_hist_CH001_SC01",
                "bundle_hash_hist",
                "2026-04-13T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_dynamic_0006_materialized_generation_rows(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        scene_draft_columns = _pragma_columns_by_name(connection, "scene_drafts")
        qc_report_columns = _pragma_columns_by_name(connection, "qc_reports")
        connection.execute(
            """
            INSERT INTO llm_calls (
                llm_call_id,
                scope_type,
                scope_id,
                provider,
                model,
                prompt_hash,
                step,
                scene_id,
                chapter_id,
                request_payload_summary,
                response_payload_summary,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                latency_ms,
                finish_reason,
                error_code,
                created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "llm_call_existing",
                "scene",
                "CH001_SC01",
                "seed-provider",
                "seed-model",
                "prompt_hash_existing",
                "style_draft",
                "CH001_SC01",
                "CH001",
                '{"messages": 1}',
                '{"choices": 1}',
                10,
                32,
                42,
                1500,
                "stop",
                None,
                "2026-04-14T00:00:00+00:00",
            ),
        )
        if "status" in scene_draft_columns:
            connection.execute(
                """
                INSERT INTO scene_drafts (
                    row_id,
                    scene_id,
                    chapter_id,
                    stage,
                    status,
                    content,
                    source_bundle_id,
                    source_bundle_hash,
                    generation_llm_call_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "draft_existing",
                    "CH001_SC01",
                    "CH001",
                    "style_draft",
                    "active",
                    "existing draft",
                    "bundle_existing",
                    "bundle_hash_existing",
                    "llm_call_existing",
                    "2026-04-14T00:00:00+00:00",
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO scene_drafts (
                    row_id,
                    scene_id,
                    chapter_id,
                    stage,
                    content,
                    source_bundle_id,
                    source_bundle_hash,
                    generation_llm_call_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "draft_existing",
                    "CH001_SC01",
                    "CH001",
                    "style_draft",
                    "existing draft",
                    "bundle_existing",
                    "bundle_hash_existing",
                    "llm_call_existing",
                    "2026-04-14T00:00:00+00:00",
                ),
            )
        connection.execute(
            """
            INSERT INTO final_scenes (
                row_id,
                scene_id,
                chapter_id,
                content,
                status,
                source_bundle_id,
                source_bundle_hash,
                source_kind,
                created_by,
                generation_llm_call_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "final_existing",
                "CH001_SC01",
                "CH001",
                "existing final",
                "approved",
                "bundle_existing",
                "bundle_hash_existing",
                "generation",
                "migration-test",
                "llm_call_existing",
                "2026-04-14T00:00:00+00:00",
            ),
        )
        if "status" in qc_report_columns:
            connection.execute(
                """
                INSERT INTO qc_reports (
                    qc_report_id,
                    scene_id,
                    chapter_id,
                    qc_type,
                    status,
                    source_draft_row_id,
                    source_bundle_id,
                    resolution_code,
                    pass_flag,
                    next_action,
                    issues_json,
                    rewrite_brief_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "qc_report_existing",
                    "CH001_SC01",
                    "CH001",
                    "hard_qc",
                    "active",
                    "draft_existing",
                    "bundle_existing",
                    "hard_pass",
                    1,
                    "pass",
                    '[{"issue_key":"ok"}]',
                    "[]",
                    "2026-04-14T00:00:00+00:00",
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO qc_reports (
                    qc_report_id,
                    scene_id,
                    chapter_id,
                    qc_type,
                    source_draft_row_id,
                    source_bundle_id,
                    resolution_code,
                    pass_flag,
                    next_action,
                    issues_json,
                    rewrite_brief_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "qc_report_existing",
                    "CH001_SC01",
                    "CH001",
                    "hard_qc",
                    "draft_existing",
                    "bundle_existing",
                    "hard_pass",
                    1,
                    "pass",
                    '[{"issue_key":"ok"}]',
                    "[]",
                    "2026-04-14T00:00:00+00:00",
                ),
            )
        connection.execute(
            """
            INSERT INTO chapter_run_jobs (
                job_id,
                chapter_id,
                status,
                job_type,
                payload_json,
                result_summary_json,
                worker_id,
                attempt_no,
                heartbeat_at,
                lease_expires_at,
                started_at,
                finished_at,
                error_code,
                error_text,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chapter_job_existing",
                "CH001",
                "queued",
                "chapter_qc",
                '{"scene_count": 1}',
                '{"status": "pending"}',
                None,
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                "2026-04-14T00:00:00+00:00",
                "2026-04-14T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()
