from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PREVIOUS_HEAD = "20260802_0080"
CURRENT_HEAD = "20260904_0083"


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def _migrate(path: Path, revision: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novel_system.db.session import reset_engine

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "style_reference_legacy_0081.json").write_text("[]", encoding="utf-8")
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(tmp_path))
        reset_engine()
        try:
            if revision.startswith("-"):
                command.downgrade(_config(), revision[1:])
            else:
                command.upgrade(_config(), revision)
        finally:
            reset_engine()


def _uncovered_foreign_keys(path: Path) -> list[str]:
    uncovered: list[str] = []
    with sqlite3.connect(path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table_name in tables:
            indexes = list(connection.execute(f'PRAGMA index_list("{table_name}")'))
            prefixes = {
                columns[0][2]
                for index in indexes
                if (columns := list(connection.execute(f'PRAGMA index_info("{index[1]}")')))
            }
            for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{table_name}")'):
                if foreign_key[3] not in prefixes:
                    uncovered.append(f"{table_name}.{foreign_key[3]}")
    return sorted(uncovered)


def test_0081_covers_all_foreign_key_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "foreign-key-indexes-0081.db"
    _migrate(path, PREVIOUS_HEAD, monkeypatch, tmp_path)
    assert len(_uncovered_foreign_keys(path)) == 38

    _migrate(path, "head", monkeypatch, tmp_path)
    assert _uncovered_foreign_keys(path) == []
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (CURRENT_HEAD,)

