from __future__ import annotations

import base64
import json

from novel_system.db.models import ChapterGoal, HumanReviewEvent, ReviewItem, SceneCard


def _encode_test_payload(value) -> str:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _encode_test_cursor(values) -> str:
    return _encode_test_payload({"values": values})


def _seed_human_review_targets(
    session,
    chapter_id: str,
    scene_ids: list[str],
) -> None:
    session.add(ChapterGoal(chapter_id=chapter_id, chapter_goal="query filter fixture"))
    session.add_all(
        [
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                scene_seq=index,
                scene_goal="query filter fixture",
            )
            for index, scene_id in enumerate(scene_ids, start=1)
        ]
    )
    session.flush()


def test_review_items_support_page_and_cursor_pagination(client, session) -> None:
    session.add_all(
        [
            ReviewItem(
                review_id="review_page_001",
                scene_id="CH100_SC01",
                chapter_id="CH100",
                item_type="style_observation",
                status="pending",
                candidate_text="first",
                created_at="2026-04-11T09:00:00Z",
            ),
            ReviewItem(
                review_id="review_page_002",
                scene_id="CH100_SC01",
                chapter_id="CH100",
                item_type="style_observation",
                status="pending",
                candidate_text="second",
                created_at="2026-04-11T09:01:00Z",
            ),
            ReviewItem(
                review_id="review_page_003",
                scene_id="CH100_SC01",
                chapter_id="CH100",
                item_type="style_observation",
                status="pending",
                candidate_text="third",
                created_at="2026-04-11T09:02:00Z",
            ),
            ReviewItem(
                review_id="review_page_004",
                scene_id="CH100_SC01",
                chapter_id="CH100",
                item_type="style_observation",
                status="pending",
                candidate_text="fourth",
                created_at="2026-04-11T09:03:00Z",
            ),
        ]
    )
    session.commit()

    page_response = client.get("/api/v1/review-items", params={"page": 1, "page_size": 2})
    assert page_response.status_code == 200
    page_data = page_response.json()["data"]
    assert [item["review_id"] for item in page_data["items"]] == ["review_page_004", "review_page_003"]
    assert page_data["pagination"] == {
        "mode": "page",
        "limit": 2,
        "page": 1,
        "page_size": 2,
        "returned": 2,
        "total": 4,
        "has_next": True,
        "next_cursor": page_data["pagination"]["next_cursor"],
    }
    assert isinstance(page_data["pagination"]["next_cursor"], str)
    assert page_data["pagination"]["next_cursor"]

    cursor_response = client.get("/api/v1/review-items", params={"limit": 2})
    assert cursor_response.status_code == 200
    cursor_data = cursor_response.json()["data"]
    assert [item["review_id"] for item in cursor_data["items"]] == ["review_page_004", "review_page_003"]
    assert cursor_data["pagination"]["mode"] == "cursor"
    assert cursor_data["pagination"]["limit"] == 2
    assert cursor_data["pagination"]["page"] is None
    assert cursor_data["pagination"]["page_size"] is None
    assert cursor_data["pagination"]["returned"] == 2
    assert cursor_data["pagination"]["total"] == 4
    assert cursor_data["pagination"]["has_next"] is True
    assert isinstance(cursor_data["pagination"]["next_cursor"], str)
    assert cursor_data["pagination"]["next_cursor"]

    next_cursor_response = client.get(
        "/api/v1/review-items",
        params={"cursor": cursor_data["pagination"]["next_cursor"], "limit": 2},
    )
    assert next_cursor_response.status_code == 200
    next_cursor_data = next_cursor_response.json()["data"]
    assert [item["review_id"] for item in next_cursor_data["items"]] == ["review_page_002", "review_page_001"]
    assert next_cursor_data["pagination"]["mode"] == "cursor"
    assert next_cursor_data["pagination"]["has_next"] is False
    assert next_cursor_data["pagination"]["next_cursor"] is None

    invalid_cursor_response = client.get("/api/v1/review-items", params={"cursor": "not-a-real-cursor", "limit": 2})
    assert invalid_cursor_response.status_code == 200
    invalid_cursor_data = invalid_cursor_response.json()["data"]
    assert [item["review_id"] for item in invalid_cursor_data["items"]] == ["review_page_004", "review_page_003"]
    assert invalid_cursor_data["pagination"]["mode"] == "cursor"

    malformed_cursors = [
        _encode_test_cursor([None, None]),
        _encode_test_cursor([{}, []]),
        _encode_test_cursor([123, 456]),
        _encode_test_payload([]),
        "游标",
    ]
    for malformed_cursor in malformed_cursors:
        malformed_cursor_response = client.get(
            "/api/v1/review-items",
            params={"cursor": malformed_cursor, "limit": 2},
        )
        assert malformed_cursor_response.status_code == 200
        malformed_cursor_data = malformed_cursor_response.json()["data"]
        assert [item["review_id"] for item in malformed_cursor_data["items"]] == [
            "review_page_004",
            "review_page_003",
        ]


def test_review_items_filter_by_status_scene_and_chapter(client, session) -> None:
    session.add_all(
        [
            ReviewItem(
                review_id="review_status_pending",
                scene_id="CH001_SC01",
                chapter_id="CH001",
                item_type="style_observation",
                status="pending",
                candidate_text="status match",
            ),
            ReviewItem(
                review_id="review_status_approved",
                scene_id="CH001_SC01",
                chapter_id="CH001",
                item_type="style_observation",
                status="approved",
                candidate_text="status mismatch",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/review-items", params={"status": "pending"})
    assert response.status_code == 200
    assert [item["review_id"] for item in response.json()["data"]["items"]] == ["review_status_pending"]

    session.add_all(
        [
            ReviewItem(
                review_id="review_scene_match",
                scene_id="CH009_SC01",
                chapter_id="CH009",
                item_type="style_observation",
                status="approved",
                candidate_text="scene match",
            ),
            ReviewItem(
                review_id="review_scene_mismatch",
                scene_id="CH009_SC02",
                chapter_id="CH009",
                item_type="style_observation",
                status="approved",
                candidate_text="scene mismatch",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/review-items", params={"scene_id": "CH009_SC01"})
    assert response.status_code == 200
    assert [item["review_id"] for item in response.json()["data"]["items"]] == ["review_scene_match"]

    session.add_all(
        [
            ReviewItem(
                review_id="review_chapter_match",
                scene_id="CH010_SC01",
                chapter_id="CH010",
                item_type="style_observation",
                status="approved",
                candidate_text="chapter match",
            ),
            ReviewItem(
                review_id="review_chapter_mismatch",
                scene_id="CH010_SC01",
                chapter_id="CH011",
                item_type="style_observation",
                status="approved",
                candidate_text="chapter mismatch",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/review-items", params={"chapter_id": "CH010"})
    assert response.status_code == 200
    assert [item["review_id"] for item in response.json()["data"]["items"]] == ["review_chapter_match"]


def test_review_items_filter_by_item_type(client, session) -> None:
    session.add_all(
        [
            ReviewItem(
                review_id="review_item_type_scene_memory",
                scene_id="CH020_SC01",
                chapter_id="CH020",
                item_type="scene_memory",
                status="pending",
                candidate_text="item type match",
            ),
            ReviewItem(
                review_id="review_item_type_scene_summary",
                scene_id="CH020_SC01",
                chapter_id="CH020",
                item_type="scene_summary",
                status="pending",
                candidate_text="same collection but different item type",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/review-items", params={"item_type": "scene_memory"})
    assert response.status_code == 200
    assert [item["review_id"] for item in response.json()["data"]["items"]] == ["review_item_type_scene_memory"]


def test_review_items_filter_by_target_collection(client, session) -> None:
    session.add_all(
        [
            ReviewItem(
                review_id="review_collection_scene_memory",
                scene_id="CH030_SC01",
                chapter_id="CH030",
                item_type="scene_memory",
                status="pending",
                candidate_text="collection match one",
            ),
            ReviewItem(
                review_id="review_collection_scene_summary",
                scene_id="CH030_SC01",
                chapter_id="CH030",
                item_type="scene_summary",
                status="pending",
                candidate_text="collection match two",
            ),
            ReviewItem(
                review_id="review_collection_chapter_summary",
                scene_id="CH030_SC01",
                chapter_id="CH030",
                item_type="chapter_summary",
                status="pending",
                candidate_text="collection mismatch",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/review-items", params={"target_collection": "scene_memories"})
    assert response.status_code == 200
    assert sorted(item["review_id"] for item in response.json()["data"]["items"]) == [
        "review_collection_scene_memory",
        "review_collection_scene_summary",
    ]


