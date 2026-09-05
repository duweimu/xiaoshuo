from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from novel_system.db.models import Base


PREVIOUS_HEAD = "20260805_0081"
CURRENT_HEAD = "20260904_0083"


def _migration_module():
    backend_dir = Path(__file__).resolve().parents[1]
    path = backend_dir / "alembic" / "versions" / "20260818_0082_canon_continuity.py"
    spec = importlib.util.spec_from_file_location("migration_0082_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def _migrate(
    path: Path,
    revision: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    downgrade: bool = False,
) -> None:
    from novel_system.db.session import reset_engine

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "style_reference_legacy_0082.json").write_text("[]", encoding="utf-8")
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(tmp_path))
        reset_engine()
        try:
            if downgrade:
                command.downgrade(_config(), revision)
            else:
                command.upgrade(_config(), revision)
        finally:
            reset_engine()


def _insert_legacy_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    payload: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO narrative_events(
            event_id, project_id, scene_id, chapter_id, event_type,
            entity_type, entity_id, fact_key, fact_value, payload_json, created_at
        ) VALUES (?, 'legacy_project', 'legacy_scene', 'legacy_chapter',
                  'character_state', 'character', 'legacy_character',
                  'mood', 'uneasy', ?, '2026-08-18T00:00:00Z')
        """,
        (event_id, json.dumps(payload, ensure_ascii=False)),
    )


def test_0082_materialized_schema_check_rejects_missing_nonindexed_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            assert migration._upgrade_already_materialized() is True

            connection.exec_driver_sql(
                "ALTER TABLE canon_commits DROP COLUMN decision_note"
            )
            assert migration._upgrade_already_materialized() is False
    finally:
        engine.dispose()


def test_0082_backfills_legacy_authority_fail_closed_and_downgrades_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "canon-continuity-0082.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with sqlite3.connect(path) as connection:
        _insert_legacy_event(connection, event_id="legacy_plan", payload={})
        _insert_legacy_event(
            connection,
            event_id="legacy_prose",
            payload={"source": "prose", "extract_ordinal": 0},
        )

    _migrate(path, "head", monkeypatch=monkeypatch, tmp_path=tmp_path)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (CURRENT_HEAD,)
        rows = connection.execute(
            """
            SELECT event_id, authority_status, source_kind
            FROM narrative_events ORDER BY event_id
            """
        ).fetchall()
        assert rows == [
            ("legacy_plan", "planned", "legacy_plan"),
            ("legacy_prose", "pending", "prose_extraction"),
        ]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"canon_commits", "fact_candidates", "continuity_snapshots"} <= tables
        timeline_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(timeline_events)")
        }
        assert {
            "event_mode",
            "realization_status",
            "realized_canon_commit_id",
            "realized_scene_id",
        } <= timeline_columns


    _migrate(
        path,
        PREVIOUS_HEAD,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        downgrade=True,
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == (PREVIOUS_HEAD,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "canon_commits" not in tables
        assert "fact_candidates" not in tables
        assert "continuity_snapshots" not in tables
        narrative_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(narrative_events)")
        }
        assert "authority_status" not in narrative_columns
        assert "canon_commit_id" not in narrative_columns
