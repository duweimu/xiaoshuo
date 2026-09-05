from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    SceneCard,
    SceneDraft,
    StoryProject,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService


def _project(project_id: str) -> StoryProject:
    return StoryProject(
        project_id=project_id,
        title=project_id,
        outline_text="outline",
    )


def _chapter(
    chapter_id: str,
    *,
    project_id: str,
    display_order: int,
    trashed_flag: int = 0,
) -> ChapterGoal:
    return ChapterGoal(
        chapter_id=chapter_id,
        project_id=project_id,
        chapter_goal=f"goal {chapter_id}",
        display_order=display_order,
        trashed_flag=trashed_flag,
    )


def _scene(
    scene_id: str,
    *,
    chapter_id: str,
    project_id: str,
    scene_seq: int,
    trashed_flag: int = 0,
) -> SceneCard:
    return SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=scene_seq,
        scene_goal=f"goal {scene_id}",
        trashed_flag=trashed_flag,
    )


def test_database_rejects_duplicate_active_chapter_and_scene_positions(session) -> None:
    session.add(_project("P_ORDER_UNIQUE"))
    session.commit()

    session.add_all(
        [
            _chapter("C_ORDER_1", project_id="P_ORDER_UNIQUE", display_order=1),
            _chapter("C_ORDER_2", project_id="P_ORDER_UNIQUE", display_order=1),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add_all(
        [
            _chapter("C_ORDER_1", project_id="P_ORDER_UNIQUE", display_order=1),
            _chapter(
                "C_ORDER_TRASHED",
                project_id="P_ORDER_UNIQUE",
                display_order=1,
                trashed_flag=1,
            ),
        ]
    )
    session.commit()

    session.add_all(
        [
            _scene(
                "S_ORDER_1",
                chapter_id="C_ORDER_1",
                project_id="P_ORDER_UNIQUE",
                scene_seq=1,
            ),
            _scene(
                "S_ORDER_2",
                chapter_id="C_ORDER_1",
                project_id="P_ORDER_UNIQUE",
                scene_seq=1,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add_all(
        [
            _scene(
                "S_ORDER_1",
                chapter_id="C_ORDER_1",
                project_id="P_ORDER_UNIQUE",
                scene_seq=1,
            ),
            _scene(
                "S_ORDER_TRASHED",
                chapter_id="C_ORDER_1",
                project_id="P_ORDER_UNIQUE",
                scene_seq=1,
                trashed_flag=1,
            ),
        ]
    )
    session.commit()


def test_database_rejects_invalid_positions_and_orphan_artifacts(session) -> None:
    session.add(_project("P_PARENT"))
    session.add(_chapter("C_PARENT", project_id="P_PARENT", display_order=1))
    session.add(
        _scene(
            "S_PARENT",
            chapter_id="C_PARENT",
            project_id="P_PARENT",
            scene_seq=1,
        )
    )
    session.commit()

    session.add(
        _scene(
            "S_BAD_SEQUENCE",
            chapter_id="C_PARENT",
            project_id="P_PARENT",
            scene_seq=0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(
        SceneDraft(
            row_id="draft-orphan",
            scene_id="S_MISSING",
            chapter_id="C_PARENT",
            stage="neutral",
            content="draft",
            source_bundle_id="bundle",
            source_bundle_hash="hash",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(
        SceneDraft(
            row_id="draft-valid",
            scene_id="S_PARENT",
            chapter_id="C_PARENT",
            stage="neutral",
            content="draft",
            source_bundle_id="bundle",
            source_bundle_hash="hash",
        )
    )
    session.commit()


def test_v1_upserts_return_domain_conflicts_before_unique_constraints(client, session) -> None:
    session.add(_project("P_API_ORDER"))
    session.commit()

    first_chapter = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "C_API_1",
            "project_id": "P_API_ORDER",
            "chapter_goal": "first",
            "display_order": 1,
        },
        headers={"X-Idempotency-Key": "create-c-api-1"},
    )
    assert first_chapter.status_code == 200, first_chapter.text
    duplicate_chapter = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "C_API_2",
            "project_id": "P_API_ORDER",
            "chapter_goal": "second",
            "display_order": 1,
        },
        headers={"X-Idempotency-Key": "create-c-api-2"},
    )
    assert duplicate_chapter.status_code == 409
    assert duplicate_chapter.json()["error"]["code"] == "CHAPTER_DISPLAY_ORDER_CONFLICT"

    first_scene = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "S_API_1",
            "chapter_id": "C_API_1",
            "project_id": "P_API_ORDER",
            "scene_goal": "first",
            "scene_seq": 1,
        },
        headers={"X-Idempotency-Key": "create-s-api-1"},
    )
    assert first_scene.status_code == 200, first_scene.text
    duplicate_scene = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "S_API_2",
            "chapter_id": "C_API_1",
            "project_id": "P_API_ORDER",
            "scene_goal": "second",
            "scene_seq": 1,
        },
        headers={"X-Idempotency-Key": "create-s-api-2"},
    )
    assert duplicate_scene.status_code == 409
    assert duplicate_scene.json()["error"]["code"] == "SCENE_SEQUENCE_CONFLICT"


def test_catalog_chapter_swap_uses_collision_free_two_phase_ordering(client, session) -> None:
    session.add(_project("P_SWAP"))
    session.add_all(
        [
            _chapter("C_SWAP_1", project_id="P_SWAP", display_order=1),
            _chapter("C_SWAP_2", project_id="P_SWAP", display_order=2),
            _chapter("C_SWAP_3", project_id="P_SWAP", display_order=3),
        ]
    )
    session.commit()

    response = client.post(
        "/api/v2/projects/P_SWAP/catalog/chapter-order",
        json={"chapter_ids": ["C_SWAP_3", "C_SWAP_2", "C_SWAP_1"]},
        headers={"X-Idempotency-Key": "swap-three-chapters"},
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    rows = session.execute(
        select(ChapterGoal)
        .where(ChapterGoal.project_id == "P_SWAP")
        .order_by(ChapterGoal.display_order.asc())
    ).scalars().all()
    assert [(row.chapter_id, row.display_order) for row in rows] == [
        ("C_SWAP_3", 1),
        ("C_SWAP_2", 2),
        ("C_SWAP_1", 3),
    ]


def test_chapter_restore_preserves_position_and_shifts_active_collision(session) -> None:
    session.add(_project("P_RESTORE_ORDER"))
    session.add_all(
        [
            _chapter("C_RESTORE_1", project_id="P_RESTORE_ORDER", display_order=1),
            _chapter("C_RESTORE_2", project_id="P_RESTORE_ORDER", display_order=2),
        ]
    )
    session.commit()

    service = AuthorLifecycleService(session)
    assert service.trash_chapters(["C_RESTORE_1"], "tester")["blocked"] == []
    session.commit()
    session.get(ChapterGoal, "C_RESTORE_2").display_order = 1
    session.commit()

    result = service.restore_chapters(["C_RESTORE_1"])
    session.commit()
    assert result["blocked"] == []
    rows = session.execute(
        select(ChapterGoal)
        .where(ChapterGoal.project_id == "P_RESTORE_ORDER")
        .order_by(ChapterGoal.display_order.asc())
    ).scalars().all()
    assert [(row.chapter_id, row.display_order) for row in rows] == [
        ("C_RESTORE_1", 1),
        ("C_RESTORE_2", 2),
    ]


def test_scene_purge_is_blocked_by_run_job_instead_of_failing_fk(client, session) -> None:
    session.add(_project("P_PURGE_JOB"))
    session.add(_chapter("C_PURGE_JOB", project_id="P_PURGE_JOB", display_order=1))
    session.add(
        _scene(
            "S_PURGE_JOB",
            chapter_id="C_PURGE_JOB",
            project_id="P_PURGE_JOB",
            scene_seq=1,
        )
    )
    session.add(
        ChapterRunJob(
            job_id="job-purge-block",
            chapter_id="C_PURGE_JOB",
            scene_id="S_PURGE_JOB",
            status="completed",
            job_type="scene_run_full",
        )
    )
    session.commit()

    trashed = client.post(
        "/api/v1/scenes/trash",
        json={"scene_ids": ["S_PURGE_JOB"]},
        headers={"X-Idempotency-Key": "trash-s-purge-job"},
    )
    assert trashed.status_code == 200, trashed.text
    from novel_system.services.author_lifecycle import AuthorLifecycleService

    purged = AuthorLifecycleService(session).purge_scenes(["S_PURGE_JOB"])
    assert purged["processed"] == []
    assert purged["blocked"][0]["code"] == "SCENE_PURGE_BLOCKED_RUNTIME_ARTIFACTS"


