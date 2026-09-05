from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _prepare_style_reference_backup_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "style_reference_test_repo"
    backup_dir = repo_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "style_reference_legacy_test.json").write_text(
        '{"row_count": 0, "profiles": [], "source": "alembic-env"}',
        encoding="utf-8",
    )
    return repo_root


def test_alembic_upgrade_respects_database_url_env(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    backend_dir = repo_root
    db_path = tmp_path / "alembic-runtime.sqlite"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir / "src")
    env["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["STYLE_REFERENCE_REPO_ROOT"] = str(_prepare_style_reference_backup_root(tmp_path))

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend_dir / "alembic.ini"), "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        check=True,
    )

    connection = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(chapter_states)").fetchall()]
        chapter_goal_columns = [row[1] for row in connection.execute("PRAGMA table_info(chapter_goals)").fetchall()]
        scene_card_columns = [row[1] for row in connection.execute("PRAGMA table_info(scene_cards)").fetchall()]
        canon_commits_exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='canon_commits'"
        ).fetchone()
    finally:
        connection.close()

    assert "manual_hold_reason" in columns
    assert "trashed_flag" in chapter_goal_columns
    assert "trashed_at" in chapter_goal_columns
    assert "trashed_by" in chapter_goal_columns
    assert "trashed_flag" in scene_card_columns
    assert "trashed_at" in scene_card_columns
    assert "trashed_by" in scene_card_columns
    assert canon_commits_exists == ("canon_commits",)


def test_alembic_upgrade_repairs_existing_human_review_event_table(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    backend_dir = repo_root
    db_path = tmp_path / "alembic-existing.sqlite"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE human_review_events (
                event_id VARCHAR NOT NULL PRIMARY KEY,
                scene_id VARCHAR,
                chapter_id VARCHAR,
                event_source VARCHAR NOT NULL,
                priority VARCHAR NOT NULL,
                owner VARCHAR,
                status VARCHAR NOT NULL,
                allowed_actions_json JSON,
                result_status_map_json JSON,
                default_action VARCHAR,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_dir / "src")
    env["NOVEL_SYSTEM_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["STYLE_REFERENCE_REPO_ROOT"] = str(_prepare_style_reference_backup_root(tmp_path))

    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend_dir / "alembic.ini"), "upgrade", "head"],
        cwd=backend_dir,
        env=env,
        check=True,
    )

    connection = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(human_review_events)").fetchall()]
    finally:
        connection.close()

    assert "object_ref" in columns
    assert "details_json" in columns
