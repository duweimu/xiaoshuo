"""P0-4 · authoritative character entity — regression tests.

Covers the three behaviours the patch fixes:

1.  ``character_synopses`` (背景故事) now persists onto the canonical
    ``StoryCharacter`` entity (new ``synopsis_json`` column). Before the fix
    ``_sync_characters`` skipped this step entirely.
2.  Runtime impact analysis keys identity on ``character_id`` ONLY — never on a
    mutable ``display_name`` / ``name``. A character with no stable id yields a
    broad-invalidation signal instead of a mis-keyed identity.
3.  Renaming a character keeps the same id, so it reads as a *scoped change*
    (not an add+remove), keeping downstream invalidation precise.

Conventions mirror tests/test_snowflake_planner.py (``client`` + ``session``
fixtures from conftest.py).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from novel_system.db.models import StoryCharacter
from novel_system.services.project_runtime_invalidation import SnowflakeImpactAnalyzer


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _create_project(client) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={
            "title": "雨城残响",
            "genre": "都市悬疑",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": (
                "女主收到一封来自十年前的信。\n"
                "她回到雨城，发现旧案和家族秘密有关。\n"
                "结尾她决定公开真相。"
            ),
            "planning_mode": "snowflake",
        },
        headers={"X-Idempotency-Key": f"create-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _approve_step(session, project_id: str, step_key: str) -> dict:
    from novel_system.services.snowflake_planner import SnowflakePlannerService

    planner = SnowflakePlannerService(session)
    artifact = planner.generate(project_id, step_key, {})["artifact"]
    approved = planner.approve(project_id, artifact["artifact_id"])["artifact"]
    session.commit()
    return approved


# --------------------------------------------------------------------------- #
# 1. character_synopses now reaches the entity
# --------------------------------------------------------------------------- #
def test_character_synopses_syncs_to_entity(client, session) -> None:
    project = _create_project(client)
    project_id = project["project_id"]

    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
    ]:
        _approve_step(session, project_id, step_key)

    rows = session.execute(
        select(StoryCharacter).where(StoryCharacter.project_id == project_id)
    ).scalars().all()

    assert rows, "approving character steps should create StoryCharacter rows"
    # The 背景故事 layer is now persisted (was silently dropped before P0-4).
    assert any(row.synopsis_json for row in rows), (
        "character_synopses approval must populate StoryCharacter.synopsis_json"
    )
    # ...on the SAME entity that already carries the step-3 summary layer.
    assert any(row.summary_json for row in rows)


# --------------------------------------------------------------------------- #
# 2. identity keys on character_id only
# --------------------------------------------------------------------------- #
def test_impact_keys_on_character_id_only(session) -> None:
    analyzer = SnowflakeImpactAnalyzer(session)

    with_id = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚"}]}
    keyed = analyzer._characters_by_id(with_id)
    assert keyed is not None
    assert set(keyed) == {"P_CHAR_a1"}

    # No stable id → cannot scope precisely → signal broad invalidation (None),
    # instead of mis-keying identity on a mutable display name (old behaviour).
    without_id = {"characters": [{"display_name": "林岚"}]}
    assert analyzer._characters_by_id(without_id) is None


# --------------------------------------------------------------------------- #
# 3. rename is a scoped change, not an identity fork
# --------------------------------------------------------------------------- #
def test_rename_is_a_change_not_an_identity_fork(session) -> None:
    analyzer = SnowflakeImpactAnalyzer(session)
    previous = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚"}]}
    current = {"characters": [{"character_id": "P_CHAR_a1", "display_name": "林岚（化名）"}]}

    changed = analyzer._changed_character_ids(previous, current)
    # Same id on both sides → set equality holds → reported as a precise change,
    # so downstream invalidation stays scoped instead of degrading to broadcast.
    assert changed == {"P_CHAR_a1"}
