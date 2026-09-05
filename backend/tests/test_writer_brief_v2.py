from __future__ import annotations


def test_writer_brief_v2_round_trips_new_fields_and_keeps_legacy_fields(client, session) -> None:
    chapter_payload = {
        "chapter_id": "WBV2",
        "planned_scene_count": 1,
        "chapter_goal": "Make a promise and complicate it.",
        "writer_brief_json": {
            "core_promise": "legacy promise stays readable",
            "chapter_promise": "the chapter asks whether trust can survive evidence",
            "escalation_path": "warmth to doubt to action",
            "relationship_delta": "old allies become wary partners",
            "reveal_or_reversal": "the friend knew the name already",
            "payoff_target": "the lie must pay off before the chapter closes",
            "ending_question": "who is the friend protecting",
        },
    }
    assert client.post("/api/v1/chapters", json=chapter_payload, headers={"X-Idempotency-Key": "wbv2-ch"}).status_code == 200

    scene_payload = {
        "scene_id": "WBV2_SC01",
        "chapter_id": "WBV2",
        "scene_goal": "Force a choice under pressure.",
        "writer_brief_json": {
            "character_desire": "legacy desire stays readable",
            "choice_under_pressure": "ask again or protect the friendship",
            "power_shift": "the protagonist takes the leverage back",
            "new_information": "the friend recognizes the forbidden name",
            "emotional_turn": "nostalgia becomes doubt",
            "image_anchor": "a cup stops ringing",
            "reader_aftertaste": "closeness now feels unsafe",
        },
    }
    assert client.post("/api/v1/scenes", json=scene_payload, headers={"X-Idempotency-Key": "wbv2-sc"}).status_code == 200

    from novel_system.db.models import ChapterGoal, SceneCard
    from novel_system.services.writer_briefs import (
        normalize_chapter_writer_brief,
        normalize_scene_writer_brief,
    )

    session.expire_all()
    chapter_brief = normalize_chapter_writer_brief(session.get(ChapterGoal, "WBV2").writer_brief_json)
    scene_brief = normalize_scene_writer_brief(session.get(SceneCard, "WBV2_SC01").writer_brief_json)

    assert chapter_brief["schema_version"] == "writer_brief_v2"
    assert chapter_brief["core_promise"] == "legacy promise stays readable"
    assert chapter_brief["chapter_promise"] == "the chapter asks whether trust can survive evidence"
    assert chapter_brief["relationship_delta"] == "old allies become wary partners"
    assert scene_brief["schema_version"] == "writer_brief_v2"
    assert scene_brief["character_desire"] == "legacy desire stays readable"
    assert scene_brief["choice_under_pressure"] == "ask again or protect the friendship"
    assert scene_brief["reader_aftertaste"] == "closeness now feels unsafe"
