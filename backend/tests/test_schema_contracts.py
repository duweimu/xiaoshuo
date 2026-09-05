from __future__ import annotations

from sqlalchemy import inspect

from novel_system.db.models import ReviewItem


def test_schema_creates_required_tables(session) -> None:
    inspector = inspect(session.bind)
    table_names = set(inspector.get_table_names())

    assert "chapter_goals" in table_names
    assert "scene_run_states" in table_names
    assert "review_items" in table_names
    assert "idempotency_keys" in table_names
    assert "operation_logs" in table_names


def test_review_items_exposes_derived_target_collection(session) -> None:
    row = ReviewItem(
        review_id="review_style_1",
        scene_id="CH001_SC01",
        chapter_id="CH001",
        item_type="style_observation",
        status="pending",
        candidate_text="结尾偏向余波式收束。",
        candidate_payload_json={
            "scope": "global",
            "scope_ref_id": "global",
            "lineage_key": "STY_001",
            "text": "结尾偏向余波式收束。"
        },
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    assert row.target_collection == "style_observations"
