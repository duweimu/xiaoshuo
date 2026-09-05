from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    SceneBlueprint,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.bundle_builder import BundleBuilder


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


CHAPTER_ID = "BP100"
SCENE_ID = "BP100_SC01"
PROJECT_ID = "P_BP100"


def _seed_scene(session) -> None:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="Blueprint Fixture",
            outline_text="Blueprint fixture outline.",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A quiet reunion must turn into a choice.",
            main_plot_push="move from suspicion to action",
            emotional_target="trust becomes costly",
            ending_effect="leave the reader asking what was hidden",
            writer_brief_json={
                "chapter_promise": "a reunion reveals a dangerous silence",
                "escalation_path": "warmth, evasion, decision",
                "ending_question": "why does the friend hide the name",
            },
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="The protagonist asks for the missing name and must decide whether to trust an old friend.",
            beats_json=["ask for the name", "old friend deflects", "protagonist chooses to investigate"],
            exit_change="The old friend becomes a suspect.",
            hook="The teacup stills when the name is spoken.",
            writer_brief_json={
                "character_desire": "get the truth",
                "obstacle": "the friend answers with charm instead of facts",
                "choice_under_pressure": "trust the friend or investigate alone",
                "power_shift": "the protagonist stops asking permission",
                "new_information": "the friend recognizes the missing name",
                "emotional_turn": "warmth becomes suspicion",
                "image_anchor": "the still teacup",
                "reader_aftertaste": "affection now feels dangerous",
            },
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()


def test_legacy_v1_blueprint_rows_still_surface_in_workbench_and_bundle(client, session) -> None:
    _seed_scene(session)
    legacy = SceneBlueprint(
        row_id="scene_blueprint_legacy_v1",
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        source_bundle_id="legacy_bundle",
        source_bundle_hash="legacy_hash",
        blueprint_json={
            "choice_under_pressure": "trust the friend or investigate alone",
            "ending_reader_question": "why the name was hidden",
            "image_promise": "the still teacup",
        },
        status="accepted",
    )
    session.add(legacy)
    session.commit()

    workbench = client.get(f"/api/v1/scenes/{SCENE_ID}/workbench").json()["data"]
    assert workbench["literary_blueprint"]["row_id"] == legacy.row_id
    assert workbench["literary_blueprint"]["blueprint_json"]["choice_under_pressure"] == "trust the friend or investigate alone"

    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert snapshot["source_version_refs"]["scene_blueprint_row_id"] == legacy.row_id
    assert "choice_under_pressure" in snapshot["inline_digests"]["scene_blueprint"]
