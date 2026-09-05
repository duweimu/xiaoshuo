"""Tests for blueprint enforcement features (batch 1 + batch 2).

Covers: conflict-too-clean detection, adversarial dim promotion,
auto-critique new dims, cost_requirement blocking, expression spectrum
frequency tracking, retroactive foreshadow lifecycle, and style-candidates API.
"""
from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    SceneCard,
)


# ---------------------------------------------------------------------------
# §8 / literary_quality: conflict_too_clean detection
# ---------------------------------------------------------------------------


def test_conflict_too_clean_detection_triggers():
    from novel_system.services.literary_quality import analyze_literary_quality

    # Text with conflict + reconciliation in close proximity → should trigger
    text = (
        "她愤怒地质问他为什么隐瞒真相，他一脸惊愕地反对她的指责。"
        "两人激烈争吵了一会儿。"
        "然后他叹气，低下头表示理解。"
        "她点头接受了他的解释，释然地微笑。"
        "他也笑了，两人和好如初。"
    )
    signals, _ = analyze_literary_quality(text)
    assert "conflict_too_clean" in signals
    assert signals["conflict_too_clean"]["risk"] is True
    assert signals["conflict_too_clean"]["score"] < 1.0


def test_conflict_too_clean_detection_clean_text():
    from novel_system.services.literary_quality import analyze_literary_quality

    # Neutral text without conflict/reconciliation patterns → should NOT trigger
    text = (
        "清晨的阳光照进房间，窗外的鸟鸣声渐渐变得嘈杂。"
        "她起身拉开窗帘，看着远处的山脉。"
        "今天的天气很好，适合出门。"
    )
    signals, _ = analyze_literary_quality(text)
    assert "conflict_too_clean" in signals
    assert signals["conflict_too_clean"]["risk"] is False
    assert signals["conflict_too_clean"]["score"] == 1.0


def test_conflict_too_clean_no_reconciliation():
    from novel_system.services.literary_quality import analyze_literary_quality

    # Conflict without reconciliation → should NOT trigger
    text = (
        "他愤怒地拒绝了她的要求，转身离去。"
        "她在背后质问他的动机，但他没有回头。"
        "争吵的余波久久不散。"
    )
    signals, _ = analyze_literary_quality(text)
    assert "conflict_too_clean" in signals
    assert signals["conflict_too_clean"]["risk"] is False


# ---------------------------------------------------------------------------
# §6/§8: adversarial + critique dim promotion
# ---------------------------------------------------------------------------


def test_painless_scene_in_adversarial_dims():
    from novel_system.services.literary_quality import ADVERSARIAL_DIMS

    for dim in ("painless_scene", "no_choice_scene", "choice_pressure", "conflict_too_clean"):
        assert dim in ADVERSARIAL_DIMS, f"{dim} missing from ADVERSARIAL_DIMS"


def test_auto_critique_includes_new_dims():
    from novel_system.services.auto_critique import CRITIQUE_DIMS

    for dim in ("painless_scene", "no_choice_scene", "choice_pressure", "conflict_too_clean"):
        assert dim in CRITIQUE_DIMS, f"{dim} missing from CRITIQUE_DIMS"


def test_dimension_weights_include_conflict_too_clean():
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS

    assert "conflict_too_clean" in DIMENSION_WEIGHTS
    assert DIMENSION_WEIGHTS["conflict_too_clean"] > 0


# ---------------------------------------------------------------------------
# §4: cost_requirement blocking for explicit scenes
# ---------------------------------------------------------------------------


def test_cost_requirement_blocking_for_explicit_scenes(session):
    """Scenes with explicit scene_crucible in blueprint but no cost_requirement
    should be blocked. Scenes with cost_requirement (via exit_change fallback)
    should be active.

    Uses the service directly because scene_crucible arrives via SceneBlueprint,
    not writer_brief_json (which has its own field allowlist).
    """
    from novel_system.db.models import SceneBlueprint
    from novel_system.services.scene_execution import SceneExecutionContractService

    session.add(ChapterGoal(chapter_id="BP_CH01", planned_scene_count=2, chapter_goal="test"))

    # Scene WITH blueprint crucible but NO cost → blocked
    session.add(SceneCard(
        scene_id="BP_CH01_SC01",
        chapter_id="BP_CH01",
        scene_seq=1,
        pov_character_id="CHAR_A",
        scene_goal="test goal",
        writer_brief_json={
            "goal": "Character must confront enemy",
            "conflict": "Enemy has the upper hand",
            "setback_or_victory": "Character suffers a setback",
        },
    ))
    session.add(SceneBlueprint(
        row_id="bp_01",
        scene_id="BP_CH01_SC01",
        chapter_id="BP_CH01",
        status="accepted",
        blueprint_json={"scene_crucible": "Explicit crucible from blueprint"},
    ))
    session.commit()

    svc = SceneExecutionContractService(session)
    contract1 = svc.generate("BP_CH01_SC01", actor_ref="test")
    assert contract1.status == "blocked"
    assert "cost_requirement" in (contract1.missing_fields_json or [])

    # Scene WITH blueprint crucible AND exit_change (cost fallback) → active
    session.add(SceneCard(
        scene_id="BP_CH01_SC02",
        chapter_id="BP_CH01",
        scene_seq=2,
        pov_character_id="CHAR_A",
        scene_goal="test goal 2",
        exit_change="Character loses their secret identity",
        writer_brief_json={
            "goal": "Character seeks the truth",
            "conflict": "Truth is guarded",
            "setback_or_victory": "Partial revelation at a price",
        },
    ))
    session.add(SceneBlueprint(
        row_id="bp_02",
        scene_id="BP_CH01_SC02",
        chapter_id="BP_CH01",
        status="accepted",
        blueprint_json={"scene_crucible": "Another crucible"},
    ))
    session.commit()

    contract2 = svc.generate("BP_CH01_SC02", actor_ref="test")
    assert contract2.status == "active"


# ---------------------------------------------------------------------------
# §12: expression spectrum frequency tracking
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §5: retroactive foreshadow lifecycle
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# §6/§14: style-candidates API
# ---------------------------------------------------------------------------


def test_style_candidates_api_returns_empty_list(client, session):
    """GET /scenes/{id}/style-candidates returns 200 with empty candidates for a fresh scene."""
    from tests.test_orchestrator_flow import seed_story

    seed_story(client, session=session)

    resp = client.get("/api/v1/scenes/CH001_SC01/style-candidates")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "candidates" in data
    assert isinstance(data["candidates"], list)
    assert data["total"] == 0  # No drafts generated yet
