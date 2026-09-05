from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PREVIOUS_HEAD = "20260802_0079"
CURRENT_HEAD = "20260904_0083"


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def _upgrade(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novel_system.db.session import reset_engine

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "style_reference_legacy_0080.json").write_text("[]", encoding="utf-8")
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(tmp_path))
        reset_engine()
        try:
            command.upgrade(_config(), revision)
        finally:
            reset_engine()


def test_0080_adds_empty_deep_review_preferences_without_changing_scenes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "scene-deep-review-0080.db"
    _upgrade(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            INSERT INTO story_projects(
                project_id, title, outline_text, planning_mode,
                snowflake_workflow_mode, status, approved_chapter_ids_json,
                trashed_flag, created_at, updated_at
            ) VALUES ('p', 'P', '', 'outline_driven', 'strict', 'outline_draft', '[]', 0, '1', '1');
            INSERT INTO chapter_goals(
                chapter_id, project_id, mid_aggregate_enabled, chapter_goal,
                state, display_order, trashed_flag, created_at, updated_at
            ) VALUES ('c', 'p', 0, 'goal', 'planned', 1, 0, '1', '1');
            INSERT INTO scene_cards(
                scene_id, chapter_id, project_id, scene_seq,
                onstage_chars_json, scene_goal, beats_json,
                is_chapter_last, state, words_current, trashed_flag,
                created_at, updated_at
            ) VALUES ('s', 'c', 'p', 1, '[]', 'goal', '[]', 0, 'todo', 0, 0, '1', '1');
            """
        )

    _upgrade(path, "head", monkeypatch, tmp_path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)
        assert connection.execute(
            """
            SELECT deep_review_decision_log_json,
                   deep_review_ignored_keys_json,
                   deep_review_preferences_revision_no
            FROM scene_cards WHERE scene_id='s'
            """
        ).fetchone() == ("[]", "[]", 0)
