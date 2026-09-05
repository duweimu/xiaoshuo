from __future__ import annotations

import uuid

from novel_system.db.models import (
    OutlinePlan,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneRunState,
    SnowflakeArtifact,
    StoryCharacter,
)
from novel_system.services.project_runtime_invalidation import SnowflakeImpactAnalyzer


def _create_project(client, *, planning_mode: str | None = "snowflake", key: str = "snowflake") -> dict:
    payload = {
        "title": "雨城残响",
        "genre": "都市悬疑",
        "target_chapter_count": 2,
        "target_word_count": 120000,
        "outline_text": "女主收到一封来自十年前的信。\n她回到雨城，发现旧案和家族秘密有关。\n结尾她决定公开真相。",
    }
    if planning_mode is not None:
        payload["planning_mode"] = planning_mode
    response = client.post(
        "/api/v1/projects",
        json=payload,
        headers={"X-Idempotency-Key": f"create-project-{key}"},
    )
    assert response.status_code == 200
    return response.json()["data"]["project"]


def _generate_step(client, project_id: str, step_key: str, payload: dict | None = None) -> dict:
    key_suffix = "payload" if payload else "default"
    response = client.post(
        f"/api/v1/projects/{project_id}/snowflake/steps/{step_key}/generate",
        json=payload or {},
        headers={"X-Idempotency-Key": f"generate-{project_id}-{step_key}-{key_suffix}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["artifact"]


def _approve_artifact(client, project_id: str, artifact_id: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-{artifact_id}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["artifact"]


def _approve_step(client, project_id: str, step_key: str, payload: dict | None = None) -> dict:
    artifact = _generate_step(client, project_id, step_key, payload)
    if artifact["status"] == "skipped":
        return artifact
    return _approve_artifact(client, project_id, artifact["artifact_id"])


def _approve_required_snowflake(client, project_id: str) -> None:
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
    ]:
        _approve_step(client, project_id, step_key)
    _approve_step(client, project_id, "long_synopsis", {"skip": True, "skip_reason": "短篇结构足够清晰"})
    _approve_step(client, project_id, "character_bibles")
    _approve_step(client, project_id, "scene_list")
    _approve_step(client, project_id, "scene_details")


def test_snowflake_project_defaults_to_step_gate_and_keeps_outline_compatibility(client) -> None:
    outline_project = _create_project(client, planning_mode=None, key="outline-compatible")
    snowflake_project = _create_project(client, planning_mode="snowflake", key="snowflake-mode")

    assert outline_project["planning_mode"] == "outline_driven"
    assert snowflake_project["planning_mode"] == "snowflake"

    state_response = client.get(f"/api/v1/projects/{snowflake_project['project_id']}/snowflake")
    assert state_response.status_code == 200
    state = state_response.json()["data"]
    assert state["current_step_key"] == "book_brief"
    assert state["next_action"] == "generate_snowflake_step"
    assert state["blocking_reason"] is None
    assert [step["step_key"] for step in state["steps"]][:3] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]


def test_snowflake_step_generation_requires_previous_approval_and_allows_author_edit(client) -> None:
    project = _create_project(client, key="step-gate")

    early_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/steps/one_sentence_summary/generate",
        json={},
        headers={"X-Idempotency-Key": "early-one-sentence"},
    )
    assert early_response.status_code == 409
    assert early_response.json()["error"]["code"] == "SNOWFLAKE_PREVIOUS_STEP_REQUIRED"

    artifact = _generate_step(client, project["project_id"], "book_brief")
    assert artifact["status"] == "pending_review"
    assert artifact["artifact_json"]["target_reader"]

    edited = {
        **artifact["artifact_json"],
        "target_reader": "喜欢雨城旧案、女性成长和家族秘密的读者",
    }
    update_response = client.patch(
        f"/api/v1/projects/{project['project_id']}/snowflake/artifacts/{artifact['artifact_id']}",
        json={"artifact_json": edited},
    )
    assert update_response.status_code == 200
    updated = update_response.json()["data"]["artifact"]
    assert updated["artifact_json"]["target_reader"] == edited["target_reader"]

    approved = _approve_artifact(client, project["project_id"], artifact["artifact_id"])
    assert approved["status"] == "approved"
    assert approved["approved_at"]

    state = client.get(f"/api/v1/projects/{project['project_id']}/snowflake").json()["data"]
    assert state["current_step_key"] == "one_sentence_summary"


def test_snowflake_downstream_versions_become_stale_when_earlier_step_changes(client, session) -> None:
    project = _create_project(client, key="stale")
    first = _approve_step(client, project["project_id"], "book_brief")
    second = _approve_step(client, project["project_id"], "one_sentence_summary")

    # P0-3: staleness is now diff-aware — a revision that actually changes book_brief's
    # content invalidates the downstream step that consumed it.
    replacement = _generate_step(client, project["project_id"], "book_brief", {"force_new": True})
    client.patch(
        f"/api/v1/projects/{project['project_id']}/snowflake/artifacts/{replacement['artifact_id']}",
        json={
            "artifact_json": {
                **replacement["artifact_json"],
                "target_reader": "一个与初稿截然不同、更聚焦的读者群体。",
            }
        },
    )
    _approve_artifact(client, project["project_id"], replacement["artifact_id"])

    session.expire_all()
    stale_second = session.get(SnowflakeArtifact, second["artifact_id"])
    fresh_first = session.get(SnowflakeArtifact, first["artifact_id"])
    assert stale_second.status == "stale"
    assert fresh_first.status == "superseded"

    state = client.get(f"/api/v1/projects/{project['project_id']}/snowflake").json()["data"]
    assert state["current_step_key"] == "one_sentence_summary"


def test_snowflake_long_synopsis_can_be_skipped_with_reason_and_characters_are_synced(client, session) -> None:
    project = _create_project(client, key="skip")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
    ]:
        _approve_step(client, project["project_id"], step_key)

    skipped = _generate_step(
        client,
        project["project_id"],
        "long_synopsis",
        {"skip": True, "skip_reason": "短篇故事，一页梗概已足够生成场景清单"},
    )
    assert skipped["status"] == "skipped"
    assert skipped["artifact_json"]["skip_reason"] == "短篇故事，一页梗概已足够生成场景清单"

    _approve_step(client, project["project_id"], "character_bibles")
    session.expire_all()
    characters = session.query(StoryCharacter).filter_by(project_id=project["project_id"]).all()
    display_names = {character.display_name for character in characters}
    assert len(display_names) >= 2
    assert display_names.isdisjoint({"林岚", "陈渡"})
    assert all(character.status == "approved" for character in characters)
    assert any((character.bible_json.get("psychological_profile") or {}).get("deepest_fear") for character in characters)


def test_snowflake_materializes_outline_plan_and_scene_details_into_runtime_cards(client, session) -> None:
    project = _create_project(client, key="materialize")
    _approve_required_snowflake(client, project["project_id"])

    materialize_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/materialize-outline-plan",
        json={},
        headers={"X-Idempotency-Key": "materialize-snowflake"},
    )
    assert materialize_response.status_code == 200
    plan = materialize_response.json()["data"]["plan"]
    assert plan["status"] == "pending_review"
    assert plan["plan_json"]["source"] == "snowflake_method"
    assert len(plan["plan_json"]["chapters"]) == 2
    first_scene = plan["plan_json"]["chapters"][0]["scenes"][0]
    assert first_scene["scene_type"] == "proactive"
    assert first_scene["writer_brief_json"]["goal"]
    assert first_scene["writer_brief_json"]["conflict"]
    assert first_scene["writer_brief_json"]["setback"]

    approve_response = client.post(
        f"/api/v1/projects/{project['project_id']}/outline-plan/{plan['plan_id']}/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-materialized-plan"},
    )
    assert approve_response.status_code == 200

    session.expire_all()
    db_plan = session.get(OutlinePlan, plan["plan_id"])
    scene = session.get(SceneCard, first_scene["scene_id"])
    assert db_plan.plan_json["source"] == "snowflake_method"
    assert scene.scene_type == "proactive"
    assert scene.writer_brief_json["source"] == "snowflake_method"
    assert scene.writer_brief_json["goal"] == first_scene["writer_brief_json"]["goal"]
    assert scene.writer_brief_json["scene_crucible"]


def test_reapproving_snowflake_scene_details_marks_runtime_contracts_and_current_artifacts_stale(client, session) -> None:
    project = _create_project(client, key="runtime-stale")
    _approve_required_snowflake(client, project["project_id"])

    materialize_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/materialize-outline-plan",
        json={},
        headers={"X-Idempotency-Key": "runtime-stale-materialize"},
    )
    plan = materialize_response.json()["data"]["plan"]
    approve_response = client.post(
        f"/api/v1/projects/{project['project_id']}/outline-plan/{plan['plan_id']}/approve",
        json={},
        headers={"X-Idempotency-Key": "runtime-stale-approve-plan"},
    )
    assert approve_response.status_code == 200

    first_scene_id = plan["plan_json"]["chapters"][0]["scenes"][0]["scene_id"]
    from novel_system.services.scene_execution import SceneExecutionContractService

    contract_id = SceneExecutionContractService(session).generate(first_scene_id).contract_id
    session.commit()

    session.add(
        SceneDraft(
            row_id="scene_draft_runtime_stale",
            scene_id=first_scene_id,
            chapter_id=plan["plan_json"]["chapters"][0]["chapter_id"],
            stage="neutral_draft",
            content="旧版本草稿",
            source_bundle_id="bundle_runtime_stale",
            source_bundle_hash="hash_runtime_stale",
        )
    )
    session.add(
        QcReport(
            qc_report_id="qc_runtime_stale",
            scene_id=first_scene_id,
            chapter_id=plan["plan_json"]["chapters"][0]["chapter_id"],
            qc_type="soft_qc",
            source_draft_row_id="scene_draft_runtime_stale",
            source_bundle_id="bundle_runtime_stale",
            resolution_code="soft_pass",
            pass_flag=1,
            next_action="pass",
            issues_json=[],
            rewrite_brief_json=[],
        )
    )
    state = session.get(SceneRunState, first_scene_id)
    assert state is not None
    state.scene_status = "archived"
    state.current_neutral_draft_row_id = "scene_draft_runtime_stale"
    state.current_qc_report_id = "qc_runtime_stale"
    session.commit()

    latest_scene_details = (
        session.query(SnowflakeArtifact)
        .filter(
            SnowflakeArtifact.project_id == project["project_id"],
            SnowflakeArtifact.step_key == "scene_details",
            SnowflakeArtifact.status == "approved",
        )
        .order_by(SnowflakeArtifact.version.desc())
        .first()
    )
    edited_payload = {
        **(latest_scene_details.artifact_json or {}),
        "scenes": [dict(scene) for scene in latest_scene_details.artifact_json["scenes"]],
    }
    replacement_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/steps/scene_details/generate",
        json={"force_new": True},
        headers={"X-Idempotency-Key": f"runtime-stale-replacement-{uuid.uuid4().hex}"},
    )
    assert replacement_response.status_code == 200, replacement_response.text
    replacement = replacement_response.json()["data"]["artifact"]
    edited_payload["scenes"][0]["scene_id"] = first_scene_id
    edited_payload["scenes"][0]["summary"] = f"Runtime invalidation change {replacement['artifact_id']}"
    update_response = client.patch(
        f"/api/v1/projects/{project['project_id']}/snowflake/artifacts/{replacement['artifact_id']}",
        json={"artifact_json": edited_payload},
    )
    assert update_response.status_code == 200
    assert update_response.json()["data"]["artifact"]["artifact_json"]["scenes"][0]["summary"].startswith("Runtime invalidation change")
    _approve_artifact(client, project["project_id"], replacement["artifact_id"])

    session.expire_all()
    state = session.get(SceneRunState, first_scene_id)
    assert state is not None
    assert session.get(SceneExecutionContract, contract_id).status == "stale"
    assert session.get(SceneDraft, "scene_draft_runtime_stale").status == "stale"
    assert session.get(QcReport, "qc_runtime_stale").status == "stale"
    assert state.scene_status == "needs_replan"
    assert state.current_neutral_draft_row_id is None
    assert state.current_qc_report_id is None


def test_reapproving_one_scene_detail_only_invalidates_that_scene_runtime(client, session) -> None:
    project = _create_project(client, key="runtime-stale-scoped")
    _approve_required_snowflake(client, project["project_id"])

    materialize_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/materialize-outline-plan",
        json={},
        headers={"X-Idempotency-Key": "runtime-stale-scoped-materialize"},
    )
    plan = materialize_response.json()["data"]["plan"]
    approve_response = client.post(
        f"/api/v1/projects/{project['project_id']}/outline-plan/{plan['plan_id']}/approve",
        json={},
        headers={"X-Idempotency-Key": "runtime-stale-scoped-approve-plan"},
    )
    assert approve_response.status_code == 200

    scenes = plan["plan_json"]["chapters"][0]["scenes"][:2]
    assert len(scenes) == 2
    contract_ids: dict[str, str] = {}
    for index, scene in enumerate(scenes, start=1):
        scene_id = scene["scene_id"]
        from novel_system.services.scene_execution import SceneExecutionContractService

        contract_ids[scene_id] = SceneExecutionContractService(session).generate(scene_id).contract_id
        session.commit()
        session.add(
            SceneDraft(
                row_id=f"scene_draft_runtime_stale_scoped_{index}",
                scene_id=scene_id,
                chapter_id=scene["chapter_id"],
                stage="neutral_draft",
                content=f"old draft {index}",
                source_bundle_id=f"bundle_runtime_stale_scoped_{index}",
                source_bundle_hash=f"hash_runtime_stale_scoped_{index}",
            )
        )
        session.add(
            QcReport(
                qc_report_id=f"qc_runtime_stale_scoped_{index}",
                scene_id=scene_id,
                chapter_id=scene["chapter_id"],
                qc_type="soft_qc",
                source_draft_row_id=f"scene_draft_runtime_stale_scoped_{index}",
                source_bundle_id=f"bundle_runtime_stale_scoped_{index}",
                resolution_code="soft_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
            )
        )
        state = session.get(SceneRunState, scene_id)
        assert state is not None
        state.scene_status = "archived"
        state.current_neutral_draft_row_id = f"scene_draft_runtime_stale_scoped_{index}"
        state.current_qc_report_id = f"qc_runtime_stale_scoped_{index}"
    session.commit()

    latest_scene_details = (
        session.query(SnowflakeArtifact)
        .filter(
            SnowflakeArtifact.project_id == project["project_id"],
            SnowflakeArtifact.step_key == "scene_details",
            SnowflakeArtifact.status == "approved",
        )
        .order_by(SnowflakeArtifact.version.desc())
        .first()
    )
    edited_payload = {
        **(latest_scene_details.artifact_json or {}),
        "scenes": [dict(scene) for scene in latest_scene_details.artifact_json["scenes"]],
    }
    changed_scene_id = scenes[0]["scene_id"]
    unchanged_scene_id = scenes[1]["scene_id"]
    edited_payload["scenes"][0]["scene_id"] = changed_scene_id
    edited_payload["scenes"][1]["scene_id"] = unchanged_scene_id

    replacement_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/steps/scene_details/generate",
        json={"force_new": True},
        headers={"X-Idempotency-Key": f"runtime-stale-scoped-replacement-{uuid.uuid4().hex}"},
    )
    assert replacement_response.status_code == 200, replacement_response.text
    replacement = replacement_response.json()["data"]["artifact"]
    edited_payload["scenes"][0]["summary"] = f"Scoped runtime invalidation {replacement['artifact_id']}"
    update_response = client.patch(
        f"/api/v1/projects/{project['project_id']}/snowflake/artifacts/{replacement['artifact_id']}",
        json={"artifact_json": edited_payload},
    )
    assert update_response.status_code == 200
    assert update_response.json()["data"]["artifact"]["artifact_json"]["scenes"][0]["summary"].startswith("Scoped runtime invalidation")
    direct_impact = SnowflakeImpactAnalyzer(session).analyze(
        project["project_id"],
        "scene_details",
        previous_payload=latest_scene_details.artifact_json,
        current_payload=update_response.json()["data"]["artifact"]["artifact_json"],
    )
    assert direct_impact["affected_count"] == 1, direct_impact
    approve_response = client.post(
        f"/api/v1/projects/{project['project_id']}/snowflake/artifacts/{replacement['artifact_id']}/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-{replacement['artifact_id']}"},
    )
    assert approve_response.status_code == 200, approve_response.text
    approve_payload = approve_response.json()["data"]

    session.expire_all()
    assert approve_payload["impact"]["affected_count"] == 1, (
        approve_payload["impact"],
        changed_scene_id,
        [row.scene_id for row in session.query(SceneCard).filter(SceneCard.project_id == project["project_id"]).all()],
    )
    assert changed_scene_id in approve_payload["impact"]["affected_scene_ids"]
    assert session.get(SceneExecutionContract, contract_ids[changed_scene_id]).status == "stale"
    assert session.get(SceneDraft, "scene_draft_runtime_stale_scoped_1").status == "stale"
    assert session.get(QcReport, "qc_runtime_stale_scoped_1").status == "stale"
    assert session.get(SceneRunState, changed_scene_id).scene_status == "needs_replan"

    assert session.get(SceneExecutionContract, contract_ids[unchanged_scene_id]).status == "active"
    assert session.get(SceneDraft, "scene_draft_runtime_stale_scoped_2").status != "stale"
    assert session.get(QcReport, "qc_runtime_stale_scoped_2").status != "stale"
    assert session.get(SceneRunState, unchanged_scene_id).scene_status == "archived"
