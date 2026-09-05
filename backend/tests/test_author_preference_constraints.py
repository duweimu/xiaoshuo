from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from novel_system.db.models import AuthorPreferenceProfile


REVISION_0071 = "20260716_0071"
REVISION_0072 = "20260716_0072"


def _migrate_database(
    path: Path,
    revision: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config

    from novel_system.db.session import reset_engine

    backend_dir = Path(__file__).resolve().parents[1]
    fake_root = tmp_path / f"preference-constraint-migration-{revision}"
    backups_dir = fake_root / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    (backups_dir / "style_reference_legacy_preflight.json").write_text(
        "[]",
        encoding="utf-8",
    )
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(fake_root))
        reset_engine()
        try:
            command.upgrade(config, revision)
        finally:
            reset_engine()


def _downgrade_database(
    path: Path,
    revision: str,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config

    from novel_system.db.session import reset_engine

    backend_dir = Path(__file__).resolve().parents[1]
    fake_root = tmp_path / f"preference-constraint-downgrade-{revision}"
    fake_root.mkdir(parents=True)
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    with monkeypatch.context() as migration_env:
        migration_env.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
        migration_env.setenv("STYLE_REFERENCE_REPO_ROOT", str(fake_root))
        reset_engine()
        try:
            command.downgrade(config, revision)
        finally:
            reset_engine()


@pytest.mark.parametrize(
    ("scope_type", "runtime_eligible"),
    [
        ("workspace", 0),
        ("global", -1),
        ("project", 2),
    ],
)
def test_author_preference_model_rejects_invalid_scope_and_runtime_flag(
    session,
    scope_type: str,
    runtime_eligible: int,
) -> None:
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                AuthorPreferenceProfile(
                    profile_id=f"invalid_{scope_type}_{runtime_eligible}",
                    scope_type=scope_type,
                    scope_ref_id="test",
                    status="draft",
                    runtime_eligible=runtime_eligible,
                    summary_json={},
                    source_patch_ids_json=[],
                )
            )
            session.flush()


def test_author_preference_model_accepts_supported_scopes_and_boolean_flags(session) -> None:
    profiles = [
        AuthorPreferenceProfile(
            profile_id=f"valid_{scope_type}",
            scope_type=scope_type,
            scope_ref_id="global" if scope_type == "global" else f"{scope_type}_1",
            status="approved" if runtime_eligible else "draft",
            runtime_eligible=runtime_eligible,
            summary_json={},
            source_patch_ids_json=[],
        )
        for scope_type, runtime_eligible in (
            ("global", 1),
            ("genre", 0),
            ("project", 1),
            ("chapter", 0),
        )
    ]
    session.add_all(profiles)
    session.flush()

    assert {profile.scope_type for profile in profiles} == {
        "global",
        "genre",
        "project",
        "chapter",
    }


def test_0072_migration_fails_closed_instead_of_relabeling_invalid_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "invalid-preference-values.db"
    _migrate_database(
        database_path,
        REVISION_0072,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    _downgrade_database(
        database_path,
        REVISION_0071,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO author_preference_profiles (
                profile_id, scope_type, scope_ref_id, status,
                runtime_eligible, summary_json, source_patch_ids_json,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "invalid_legacy_profile",
                "workspace",
                "legacy",
                "draft",
                2,
                "{}",
                "[]",
                "migration_test",
                "2026-07-16T00:00:00Z",
                "2026-07-16T00:00:00Z",
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="invalid_scope_rows=1, invalid_runtime_eligible_rows=1",
    ):
        _migrate_database(
            database_path,
            REVISION_0072,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
        )
