from __future__ import annotations

import json
from itertools import count

from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    OperationLog,
    OutlinePlan,
    SceneCard,
    SnowflakeArtifact,
    SnowflakeCharacterPlan,
    SnowflakeRevisionLink,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
)
from novel_system.services.llm_client import LLMResponse
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService


_INTENT_SEQUENCE = count()


def _intent_key(prefix: str) -> str:
    """Each helper call represents a new user intent, not a transport retry."""

    return f"{prefix}-{next(_INTENT_SEQUENCE)}"


import pytest as _pytest_sk
from tests.real_llm_fakes import install_skeleton_snowflake as _install_skeleton_snowflake


import pytest as _pytest_au
from tests.real_llm_fakes import install_online_author_pipeline as _install_online_author_pipeline


@_pytest_au.fixture(autouse=True)
def _auto_online_author(monkeypatch):
    """假生成已退役：作者稿 AI 建议 / 结构反提取走在线记账替身（按 node_id 派发）。"""
    _install_online_author_pipeline(monkeypatch)


@_pytest_sk.fixture(autouse=True)
def _auto_skeleton_snowflake(monkeypatch):
    """假生成已退役：雪花 generate_step 走规划器骨架直通（仅回归物化/失效/收口链路）。"""
    _install_skeleton_snowflake(monkeypatch)


def _patch_accounted_generate(monkeypatch, generate):
    def generate_accounted(self, request, *, accounting_hook):  # noqa: ANN001
        handle = accounting_hook.before_dispatch(
            request=request,
            dispatch_kind="initial",
        )
        try:
            response = generate(self, request)
        except Exception as exc:
            accounting_hook.after_error(
                handle,
                request=request,
                error=exc,
                raw_response=None,
                provider_request_id=None,
                latency_ms=1,
            )
            raise
        accounting_hook.after_response(
            handle,
            request=request,
            response=response,
            latency_ms=1,
        )
        return response

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        generate_accounted,
    )


def _create_project(
    client,
    *,
    key: str = "workspace-v2",
    title: str = "Rain City Signal",
    genre: str = "Urban Mystery",
    outline_text: str | None = None,
) -> dict:
    outline = outline_text or (
        "An old letter pulls the heroine back to Rain City.\n"
        "The cold case turns out to be tied to her family.\n"
        "She must decide whether the truth is worth the cost."
    )
    response = client.post(
        "/api/v2/projects",
        json={
            "title": title,
            "genre": genre,
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": outline,
        },
        headers={"X-Idempotency-Key": f"create-v2-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _generate_step(client, project_id: str, step_key: str, payload: dict | None = None) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json=payload or {},
        headers={"X-Idempotency-Key": _intent_key(f"generate-v2-{project_id}-{step_key}")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve_step(client, project_id: str, step_key: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": _intent_key(f"approve-v2-{project_id}-{step_key}")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve_generated_step(client, project_id: str, step_key: str) -> None:
    _generate_step(client, project_id, step_key)
    _approve_step(client, project_id, step_key)


def test_workspace_v2_creates_snowflake_project_and_exposes_structured_steps(client) -> None:
    project = _create_project(client)
    assert project["planning_mode"] == "snowflake"
    assert project["snowflake_workflow_mode"] == "explore"

    response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    assert response.status_code == 200
    workspace = response.json()["data"]

    assert workspace["project"]["project_id"] == project["project_id"]
    assert workspace["method_version"] == "2026-04-29.v2"
    assert workspace["quality_policy"]["flow_mode"] == "coach_flexible"
    assert workspace["materialization_requirements"]["hard_required_steps"] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "scene_list",
        "scene_details",
    ]
    assert workspace["current_step_key"] == "book_brief"
    assert workspace["ready_to_materialize"] is False
    assert workspace["latest_plan"] is None
    assert [step["step_key"] for step in workspace["steps"]][:3] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]
    first_step = workspace["steps"][0]
    assert first_step["label"] == "读者定位"
    assert first_step["can_confirm"] is False
    assert first_step["approval_blockers"] == []
    assert first_step["english_label"] == "Target Audience"
    assert first_step["phase"] == "基础准备"
    assert first_step["description"] == "明确你的小说类型和目标读者群体。你写作的核心目标是取悦你的读者——先知道为谁写，再决定写什么。"
    assert first_step["editor"]["kind"] == "form"
    assert any(field["key"] == "target_reader" for field in first_step["editor"]["fields"])
    assert first_step["guidance"]["instruction"] == (
        "回答以下三个问题：\n\n"
        "① 我的故事类型/流派是什么？\n"
        "② 这类故事的魅力在哪里？为何读者会喜欢？\n"
        "③ 我理想中的读者是谁？他们的特征是？"
    )
    assert first_step["guidance"]["timebox_minutes"] > 0
    assert first_step["guidance"]["source"] == "snowflake_method_summary"
    assert first_step["guidance"]["checklist"]
    assert first_step["guidance"]["rubric"]
    assert first_step["guidance"]["required_for_materialization"] is True
    one_sentence_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert one_sentence_step["can_confirm"] is False
    assert one_sentence_step["approval_blockers"][0]["step_key"] == "book_brief"
    assert one_sentence_step["approval_blockers"][0]["label"] == "读者定位"
    one_sentence_guidance_text = json.dumps(one_sentence_step["guidance"], ensure_ascii=False)
    assert "在一句因果句里保留主角、目标、阻力和代价。" in one_sentence_step["guidance"]["checklist"]
    assert "场景可写性" in one_sentence_step["guidance"]["rubric"]
    assert "Preserves protagonist" not in one_sentence_guidance_text
    assert "scene_writability" not in one_sentence_guidance_text
    assert [step["label"] for step in workspace["steps"]] == [
        "读者定位",
        "一句话概括",
        "一段话概括",
        "角色摘要表",
        "一页梗概",
        "角色背景故事",
        "长篇大纲",
        "角色全档案",
        "场景列表",
        "场景规划",
    ]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    scene_field = next(field for field in scene_step["editor"]["fields"] if field["key"] == "scenes")
    assert scene_field["template"]["primary_form"] == "proactive"
    proactive_mode = next(mode for mode in scene_field["scene_modes"] if mode["value"] == "proactive")
    reactive_mode = next(mode for mode in scene_field["scene_modes"] if mode["value"] == "reactive")
    proactive_goal = next(field for field in proactive_mode["fields"] if field["key"] == "goal")
    proactive_conflict = next(field for field in proactive_mode["fields"] if field["key"] == "conflict")
    reactive_crucible = next(field for field in reactive_mode["fields"] if field["key"] == "crucible")
    assert proactive_goal["label"] == "目标"
    assert proactive_goal["hint"] == "角色想达成什么？（可拍摄/量化）"
    assert proactive_goal["rows"] == 2
    assert proactive_goal["placeholder"].startswith("例：在审讯结束前")
    assert proactive_conflict["hint"] == "写出2-3轮「尝试→受阻」的循环"
    assert "警探拿出监控截图否定" in proactive_conflict["placeholder"]
    assert reactive_crucible["hint"] == "是什么让角色无法回避这个困境？"
    assert reactive_crucible["placeholder"] == "例：真凶今晚就要行动，主角却被关着，而且没有人相信他的话"
    assert first_step["completeness"]["total_count"] >= 1
    assert "target_reader" in first_step["completeness"]["missing_fields"]
    target_reader_field = next(field for field in first_step["editor"]["fields"] if field["key"] == "target_reader")
    assert target_reader_field["hint"]
    assert target_reader_field["placeholder"]
    short_step = next(step for step in workspace["steps"] if step["step_key"] == "short_synopsis")
    assert len(short_step["draft"]["paragraphs"]) == 5
    bible_step = next(step for step in workspace["steps"] if step["step_key"] == "character_bibles")
    bible_template = next(field for field in bible_step["editor"]["fields"] if field["key"] == "characters")["template"]
    assert {"physical_profile", "personality_profile", "environment_profile", "psychological_profile"} <= set(bible_template)
    assert workspace["materialization_gate"]["status"] == "blocked"
    assert workspace["materialization_gate"]["blockers"]
    assert workspace["materialization_gate"]["items"]
    first_gate_item = workspace["materialization_gate"]["items"][0]
    assert first_gate_item["severity"] == "blocker"
    assert first_gate_item["step_key"] == "book_brief"
    assert first_gate_item["target_view"] == "snowflake-workbench"
    assert first_gate_item["primary_action"]["type"] == "jump_to_step"
    assert first_gate_item["primary_action"]["label"]
    assert workspace["scene_board"] == {"chapters": [], "scenes": []}


def test_workspace_v2_explore_mode_allows_later_drafts_but_confirmation_stays_gated(client, session) -> None:
    project = _create_project(client, key="explore-draft-gates")
    scene_details_patch = {
        "draft": {
            "scenes": [
                {
                    "scene_id": f"{project['project_id']}_CH01_SC01",
                    "chapter_id": f"{project['project_id']}_CH01",
                    "chapter_title": "第一章",
                    "chapter_goal": "主角必须决定是否公开录音。",
                    "scene_seq": 1,
                    "title": "录音袋",
                    "summary": "她把录音袋推到桌沿。",
                    "primary_form": "proactive",
                    "scene_crucible": "证据一公开，幸存者就会暴露。",
                    "goal": "确认录音是否足够公开。",
                    "conflict": "盟友要求她先保护幸存者。",
                    "setback": "她只能拆开证据，失去完整公开机会。",
                    "hook": "门外有人听见了录音。",
                }
            ]
        }
    }

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json=scene_details_patch,
    )

    assert save_response.status_code == 200, save_response.text
    workspace = save_response.json()["data"]["workspace"]
    assert workspace["project"]["snowflake_workflow_mode"] == "explore"
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    assert scene_step["artifact"]["status"] == "pending_review"
    assert scene_step["draft"]["scenes"][0]["scene_crucible"] == "证据一公开，幸存者就会暴露。"
    assert scene_step["can_confirm"] is False
    assert scene_step["approval_blockers"][0]["step_key"] == "book_brief"

    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-explore-scene-details-too-early"},
    )
    assert approve_response.status_code == 409
    error = approve_response.json()["error"]
    assert error["code"] == "SNOWFLAKE_PREVIOUS_STEP_REQUIRED"
    assert error["details"]["author_action"]["target_view"] == "snowflake-workbench"
    assert error["details"]["author_action"]["primary_button_label"] == "去补读者定位"

    session.expire_all()
    run = session.execute(
        select(SnowflakeStepRun).where(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "scene_details",
        )
    ).scalars().one()
    assert run.status == "pending_review"

    strict_response = client.post(
        "/api/v2/projects",
        json={
            "title": "Strict Snowflake",
            "genre": "Mystery",
            "target_chapter_count": 1,
            "target_word_count": 50000,
            "outline_text": "A strict project should still gate draft saves.",
            "snowflake_workflow_mode": "strict",
        },
        headers={"X-Idempotency-Key": "create-v2-strict-mode"},
    )
    assert strict_response.status_code == 200
    strict_project = strict_response.json()["data"]["project"]
    assert strict_project["snowflake_workflow_mode"] == "strict"

    strict_save = client.patch(
        f"/api/v2/projects/{strict_project['project_id']}/snowflake-workspace/steps/scene_details",
        json=scene_details_patch,
    )
    assert strict_save.status_code == 409
    assert strict_save.json()["error"]["code"] == "SNOWFLAKE_PREVIOUS_STEP_REQUIRED"


def test_workspace_v2_required_steps_are_not_skippable_even_with_reason(client) -> None:
    project = _create_project(client, key="required-skip-honesty")

    workspace_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    workspace = workspace_response.json()["data"]
    assert workspace["quality_policy"]["hard_required_steps"] == [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "scene_list",
        "scene_details",
    ]
    assert "all_steps_skippable_with_reason" not in workspace["quality_policy"]
    book_brief = next(step for step in workspace["steps"] if step["step_key"] == "book_brief")
    assert book_brief["can_skip"] is False

    skip_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={"skip": True, "skip_reason": "I want to decide this later."},
    )

    assert skip_response.status_code == 400
    assert skip_response.json()["error"]["code"] == "SNOWFLAKE_STEP_NOT_SKIPPABLE"


def test_workspace_v2_step_history_and_restore_keep_author_approval_gate(client, session) -> None:
    project = _create_project(client, key="history-restore")

    first_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who want old cases and family cost.",
                "story_kind": "A mystery about choosing truth over comfort.",
            }
        },
    )
    assert first_response.status_code == 200, first_response.text
    first_run_id = first_response.json()["data"]["step"]["artifact"]["step_run_id"]
    _approve_step(client, project["project_id"], "book_brief")

    second_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Noir Mystery",
                "target_reader": "Readers who want harder-edged city pressure.",
                "story_kind": "A noir mystery with escalating public cost.",
            }
        },
    )
    assert second_response.status_code == 200, second_response.text

    history_response = client.get(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/history"
    )
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()["data"]
    assert [item["version"] for item in history["items"]] == [2, 1]
    assert history["items"][0]["draft_summary"]
    assert "draft" not in history["items"][0]

    detail_response = client.get(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/history?include_draft=true"
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["items"][1]["draft"]["category"] == "Urban Mystery"

    restore_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/restore",
        json={"step_run_id": first_run_id},
    )
    assert restore_response.status_code == 200, restore_response.text
    restored = restore_response.json()["data"]
    assert restored["step"]["artifact"]["status"] == "pending_review"
    assert restored["step"]["artifact"]["version"] == 3
    assert restored["step"]["draft"]["category"] == "Urban Mystery"
    assert restored["step_run"]["restored_from_step_run_id"] == first_run_id

    session.expire_all()
    runs = (
        session.query(SnowflakeStepRun)
        .filter(SnowflakeStepRun.project_id == project["project_id"], SnowflakeStepRun.step_key == "book_brief")
        .order_by(SnowflakeStepRun.version.asc())
        .all()
    )
    assert [run.status for run in runs] == ["approved", "pending_review", "pending_review"]
    assert session.query(SnowflakeRevisionLink).filter_by(project_id=project["project_id"]).count() == 0


def test_workspace_v2_step_history_restore_rejects_cross_project_runs(client) -> None:
    first_project = _create_project(client, key="history-cross-a")
    second_project = _create_project(client, key="history-cross-b")
    second_run = _generate_step(client, second_project["project_id"], "book_brief")["step"]["artifact"]["step_run_id"]

    response = client.post(
        f"/api/v2/projects/{first_project['project_id']}/snowflake-workspace/steps/book_brief/restore",
        json={"step_run_id": second_run},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SNOWFLAKE_STEP_RUN_NOT_FOUND"


def test_discovery_import_accepts_character_steps_and_merges_by_character_id(client, session) -> None:
    project = _create_project(client, key="discovery-character-import")
    service = SnowflakeWorkspaceService(session)

    first = service.import_discovery_steps(
        project["project_id"],
        {
            "character_sheets": {
                "characters": [
                    {
                        "character_id": "MARA",
                        "display_name": "Mara Vale",
                        "role": "protagonist",
                        "goal": "Expose the rail crime.",
                    }
                ]
            }
        },
    )
    second = service.import_discovery_steps(
        project["project_id"],
        {
            "character_sheets": {
                "characters": [
                    {
                        "character_id": "MARA",
                        "display_name": "Mara Vale",
                        "ambition": "Clear her father's name.",
                    }
                ]
            },
            "character_synopses": {
                "characters": [
                    {
                        "character_id": "MARA",
                        "display_name": "Mara Vale",
                        "synopsis": "She learned to distrust official stories.",
                    }
                ]
            },
        },
    )
    session.commit()

    assert first["imported_step_keys"] == ["character_sheets"]
    assert second["imported_step_keys"] == ["character_sheets", "character_synopses"]
    latest_sheet = (
        session.query(SnowflakeStepRun)
        .filter(SnowflakeStepRun.project_id == project["project_id"], SnowflakeStepRun.step_key == "character_sheets")
        .order_by(SnowflakeStepRun.version.desc())
        .first()
    )
    assert latest_sheet is not None
    character = latest_sheet.draft_json["characters"][0]
    assert character["character_id"] == "MARA"
    assert character["role"] == "protagonist"
    assert character["ambition"] == "Clear her father's name."
    plan = session.get(SnowflakeCharacterPlan, f"snowflake_character_plan_{project['project_id']}_MARA")
    assert plan is not None
    assert plan.summary_json["role"] == "protagonist"
    assert plan.synopsis_json["synopsis"].startswith("She learned")


def test_workspace_v2_uses_structured_scene_plans_and_applies_triage_repair(client, session) -> None:
    project = _create_project(client, key="structured-scene-plans")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    _approve_generated_step(client, project["project_id"], "scene_list")
    _approve_generated_step(client, project["project_id"], "scene_details")

    session.expire_all()
    assert (
        session.query(SnowflakeArtifact)
        .filter(SnowflakeArtifact.project_id == project["project_id"])
        .count()
        == 0
    )
    assert (
        session.query(SnowflakeStepRun)
        .filter(SnowflakeStepRun.project_id == project["project_id"])
        .count()
        == 10
    )
    scene_plan = (
        session.query(SnowflakeScenePlan)
        .filter(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.chapter_id.asc(), SnowflakeScenePlan.scene_seq.asc())
        .first()
    )
    assert scene_plan is not None
    assert scene_plan.scene_plan_id

    broken_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scenes/{scene_plan.scene_plan_id}",
        json={"setback": "", "goal": "Get the witness statement before the train leaves."},
    )
    assert broken_response.status_code == 200, broken_response.text
    broken_workspace = broken_response.json()["data"]["workspace"]
    broken_scene = next(item for item in broken_workspace["scene_board"]["scenes"] if item["scene_plan_id"] == scene_plan.scene_plan_id)
    assert broken_scene["goal"].startswith("Get the witness")
    assert broken_scene["setback"] == ""

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_plan_id": scene_plan.scene_plan_id,
                    "scene_id": scene_plan.scene_id,
                    "status": "maybe",
                    "notes": "The scene can work if the ending leaves a stronger cost.",
                    "missing_fields": ["setback"],
                    "fix_steps": ["Make the ending leave the protagonist worse off."],
                    "repair_patch": {"setback": "The witness gives proof, but it publicly implicates the heroine's family."},
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    triage_payload = triage_response.json()["data"]
    triage_item = triage_payload["items"][0]
    assert triage_item["triage_id"]
    assert triage_item["repair_patch"]["setback"].startswith("The witness gives proof")

    apply_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage/{triage_item['triage_id']}/apply",
        json={},
    )
    assert apply_response.status_code == 200, apply_response.text
    applied_scene = next(
        item for item in apply_response.json()["data"]["workspace"]["scene_board"]["scenes"] if item["scene_plan_id"] == scene_plan.scene_plan_id
    )
    assert applied_scene["setback"].startswith("The witness gives proof")

    session.expire_all()
    stored_triage = session.get(SnowflakeSceneTriageItem, triage_item["triage_id"])
    assert stored_triage is not None
    assert stored_triage.repair_patch_json["setback"].startswith("The witness gives proof")
    stored_scene = session.get(SnowflakeScenePlan, scene_plan.scene_plan_id)
    assert stored_scene.setback.startswith("The witness gives proof")


def test_workspace_v2_records_revision_links_when_upstream_step_is_reapproved(client, session) -> None:
    project = _create_project(client, key="structured-revision-links")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    replacement = _generate_step(client, project["project_id"], "book_brief", {"force_new": True})
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-v2-{project['project_id']}-book-brief-replacement"},
    )
    assert response.status_code == 200, response.text
    workspace = response.json()["data"]["workspace"]
    stale_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["status"] == "stale"
    assert stale_step["stale_reason"]
    assert replacement["step"]["version"] == 2

    session.expire_all()
    stale_run = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "one_sentence_summary",
            SnowflakeStepRun.status == "stale",
        )
        .one()
    )
    link = (
        session.query(SnowflakeRevisionLink)
        .filter(
            SnowflakeRevisionLink.project_id == project["project_id"],
            SnowflakeRevisionLink.source_step_key == "book_brief",
            SnowflakeRevisionLink.affected_kind == "step_run",
            SnowflakeRevisionLink.affected_id == stale_run.step_run_id,
        )
        .one()
    )
    assert link.status == "open"


def test_workspace_v2_supports_structured_save_assistant_and_step_approval(client, session) -> None:
    project = _create_project(client, key="save-approve")

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who want old cases, family cost, and a determined heroine.",
                "story_kind": "A truth-chasing mystery driven by action and relational cost.",
                "delight_reason": "Each clue narrows the truth but raises the emotional price.",
                "genre_promise": "The closer she gets to the truth, the more she stands to lose.",
                "expected_reader_emotion": "Pressure, doubt, and immediate page-turn urgency.",
                "safety_rules": [
                    "Only borrow abstract craft patterns and pacing.",
                    "Do not copy characters, settings, plot beats, or signature phrasing.",
                ],
            }
        },
    )
    assert save_response.status_code == 200, save_response.text
    saved = save_response.json()["data"]
    assert saved["step"]["draft"]["target_reader"].startswith("Readers who want")
    assert saved["step"]["artifact"]["status"] == "pending_review"

    assistant_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/assistant",
        json={
            "step_key": "book_brief",
            "message": "Can you narrow the target reader a little more?",
        },
    )
    assert assistant_response.status_code == 200, assistant_response.text
    assistant = assistant_response.json()["data"]
    assert assistant["reply"]
    assert assistant["step_key"] == "book_brief"
    assert assistant["source"] == "fallback"
    assert assistant["candidate_patch"] is None
    assert assistant["llm_call_id"] is None
    assert assistant["assistant_history"][0]["message"].startswith("Can you narrow")
    assert assistant["assistant_history"][0]["reply"] == assistant["reply"]

    history_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    assert history_response.status_code == 200, history_response.text
    history = history_response.json()["data"]["assistant_history"]
    assert len(history) == 1
    assert history[0]["step_key"] == "book_brief"
    assert history[0]["message"] == "Can you narrow the target reader a little more?"
    assert history[0]["reply"] == assistant["reply"]

    approve_response = _approve_step(client, project["project_id"], "book_brief")
    workspace = approve_response["workspace"]
    assert workspace["current_step_key"] == "one_sentence_summary"

    step_run_id = saved["step"]["artifact"]["step_run_id"]
    session.expire_all()
    step_run = session.get(SnowflakeStepRun, step_run_id)
    assert step_run is not None
    assert step_run.status == "approved"
    assert step_run.draft_json["category"] == "Urban Mystery"


def test_workspace_v2_step_health_reports_structural_pressure_gaps(client) -> None:
    project = _create_project(client, key="pressure-health")

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={
            "draft": {
                "category": "Urban Mystery",
                "target_reader": "Readers who like mysteries.",
                "story_kind": "A mystery.",
            }
        },
    )
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    health = step["health"]

    assert isinstance(health["pressure_score"], int)
    assert health["pressure_status"] in {"maybe", "rewrite"}
    assert "reader_promise_too_generic" in health["pressure_flags"]
    assert "story_pressure_too_generic" in health["pressure_flags"]
    assert health["fix_steps"]
    assert isinstance(health["strengths"], list)
    assert health["score"] == health["pressure_score"]
    assert health["status"] == health["pressure_status"]
    assert health["gaps"] == health["pressure_flags"]
    assert health["next_actions"] == health["fix_steps"]
    assert isinstance(health["hard_blockers"], list)
    assert step["artifact"]["diagnosis_json"]["pressure_score"] == health["pressure_score"]


def test_workspace_v2_marks_downstream_steps_stale_after_upstream_regeneration(client) -> None:
    project = _create_project(client, key="stale")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    # P0-3: staleness is diff-aware, so the upstream revision must actually change
    # book_brief's content to invalidate the step that consumed it.
    replacement = _generate_step(client, project["project_id"], "book_brief", {"force_new": True})
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    workspace = _approve_step(client, project["project_id"], "book_brief")["workspace"]

    assert workspace["current_step_key"] == "one_sentence_summary"
    stale_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["artifact"]["status"] == "stale"


def test_workspace_v2_accepts_stale_step_with_audit_trail(client, session) -> None:
    project = _create_project(client, key="accept-stale-step")
    _approve_generated_step(client, project["project_id"], "book_brief")
    _approve_generated_step(client, project["project_id"], "one_sentence_summary")

    replacement = _generate_step(client, project["project_id"], "book_brief", {"force_new": True})
    client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief",
        json={"draft": {**replacement["step"]["draft"], "target_reader": "改稿后聚焦的全新读者群体。"}},
    )
    stale_workspace = _approve_step(client, project["project_id"], "book_brief")["workspace"]
    stale_step = next(step for step in stale_workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert stale_step["status"] == "stale"
    assert stale_workspace["current_step_key"] == "one_sentence_summary"

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/one_sentence_summary/accept-stale",
        json={"note": "The one-sentence promise still matches the revised audience."},
        headers={"X-Operator-Ref": "author-a"},
    )

    assert response.status_code == 200, response.text
    workspace = response.json()["data"]["workspace"]
    accepted_step = next(step for step in workspace["steps"] if step["step_key"] == "one_sentence_summary")
    assert accepted_step["status"] == "stale"
    assert accepted_step["stale_accepted_at"]
    assert accepted_step["stale_accepted_by"] == "author-a"
    assert accepted_step["gate_satisfied"] is True
    assert workspace["current_step_key"] == "one_paragraph_summary"

    session.expire_all()
    stale_run = (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "one_sentence_summary",
            SnowflakeStepRun.status == "stale",
        )
        .one()
    )
    assert stale_run.stale_accepted_by == "author-a"
    log = (
        session.query(OperationLog)
        .filter_by(event_type="snowflake_step_stale_accepted", object_type="snowflake_step_run", object_ref=stale_run.step_run_id)
        .one()
    )
    assert log.payload_json["note"] == "The one-sentence promise still matches the revised audience."


def test_workspace_v2_accepts_stale_scenes_for_materialization(client, session) -> None:
    project = _create_project(client, key="accept-stale-scenes")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    scene_plans = (
        session.query(SnowflakeScenePlan)
        .filter(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.scene_plan_id.asc())
        .all()
    )
    assert scene_plans
    for scene in scene_plans:
        scene.status = "stale"
        scene.stale_reason = "book_brief was revised; review dependent scene work."
    session.commit()

    blocked = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    assert blocked["materialization_gate"]["status"] == "blocked"

    accept_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scenes/accept-stale",
        json={"scene_plan_ids": [scene.scene_plan_id for scene in scene_plans], "note": "Reviewed after premise update."},
        headers={"X-Operator-Ref": "author-b"},
    )
    assert accept_response.status_code == 200, accept_response.text
    accepted_workspace = accept_response.json()["data"]["workspace"]
    assert accepted_workspace["materialization_gate"]["status"] != "blocked"
    accepted_scene = accepted_workspace["scene_board"]["scenes"][0]
    assert accepted_scene["status"] == "stale"
    assert accepted_scene["stale_accepted_at"]
    assert accepted_scene["stale_accepted_by"] == "author-b"

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-accepted-stale-scenes"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    assert materialize_response.json()["data"]["plan"]["status"] == "pending_review"

    session.expire_all()
    logs = (
        session.query(OperationLog)
        .filter(OperationLog.event_type == "snowflake_scene_stale_accepted", OperationLog.object_type == "snowflake_scene_plan")
        .all()
    )
    assert len(logs) == len(scene_plans)


def test_workspace_v2_persists_scene_triage_and_approves_materialized_outline(client, session) -> None:
    project = _create_project(client, key="triage-materialize")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    _approve_generated_step(client, project["project_id"], "scene_list")
    _approve_generated_step(client, project["project_id"], "scene_details")

    workspace_response = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace")
    workspace = workspace_response.json()["data"]
    scene_details = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    first_scene_id = scene_details["draft"]["scenes"][0]["scene_id"]

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene_id,
                    "status": "maybe",
                    "notes": "Raise the cost inside the scene conflict.",
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    triage = triage_response.json()["data"]
    assert triage["items"][0]["status"] == "maybe"
    assert triage["items"][0]["notes"].startswith("Raise the cost")
    assert triage["workspace"]["materialization_gate"]["status"] == "warning"
    assert triage["workspace"]["materialization_gate"]["warnings"]

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-workspace-v2"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    preview = materialize_response.json()["data"]
    assert preview["plan"]["status"] == "pending_review"
    assert preview["workspace"]["latest_plan"]["plan_json"]["source"] == "snowflake_method"

    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-workspace-v2-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text
    approved = approve_response.json()["data"]
    assert approved["workspace"]["project"]["status"] in {"chapter_ready", "chapter_running", "chapter_blocked", "chapter_final_review"}

    session.expire_all()
    plan_id = approved["plan"]["plan_id"]
    db_plan = session.get(OutlinePlan, plan_id)
    first_scene = db_plan.plan_json["chapters"][0]["scenes"][0]
    scene = session.get(SceneCard, first_scene["scene_id"])
    assert scene is not None
    assert scene.writer_brief_json["source"] == "snowflake_method"
    assert scene.writer_brief_json["scene_crucible"]


def test_workspace_v2_cost_requirement_clears_missing_flag_and_persists_through_materialization(client, session) -> None:
    """回归守护：cost_requirement 曾经在编辑器模板/PATCH 白名单/场景序列化/LLM 清洗里
    全链路缺失——填了也存不住、诊断永远报 missing_cost_requirement。这里验证：填入后
    诊断标记消失、分数提升，且值能一路存活到 SnowflakeScenePlan 和物化后的 SceneCard。"""
    project = _create_project(client, key="cost-requirement")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    scenes = [dict(s) for s in scene_step["draft"]["scenes"]]
    assert len(scenes) >= 2, "需要至少两个场景来对比有/无 cost_requirement 的诊断差异"

    baseline_items = workspace["triage_items"]
    baseline_item = next(item for item in baseline_items if item["scene_id"] == scenes[0]["scene_id"])
    assert "missing_cost_requirement" in baseline_item["pressure_flags"], baseline_item
    baseline_score = baseline_item["score"]

    cost_text = "拿到线索的代价是永久失去这个线人的信任。"
    scenes[0] = {**scenes[0], "cost_requirement": cost_text}

    patch_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": scenes}},
    )
    assert patch_response.status_code == 200, patch_response.text
    triage_items = patch_response.json()["data"]["workspace"]["triage_items"]
    filled_item = next(item for item in triage_items if item["scene_id"] == scenes[0]["scene_id"])
    bare_item = next(item for item in triage_items if item["scene_id"] == scenes[1]["scene_id"])
    # 同一场景填前/填后对比：只应该是 cost_requirement 这一个变量的效应。
    assert "missing_cost_requirement" not in filled_item["pressure_flags"], filled_item
    assert filled_item["score"] > baseline_score
    # 没碰过的场景（对照组）应该保持原样，继续报缺失。
    assert "missing_cost_requirement" in bare_item["pressure_flags"], bare_item

    # 编辑已确认的 scene_details 会把它打回 pending_review，需要重新确认才能物化。
    _approve_step(client, project["project_id"], "scene_details")

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-cost-requirement"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-cost-requirement-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text

    session.expire_all()
    plan = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project["project_id"],
            SnowflakeScenePlan.scene_id == scenes[0]["scene_id"],
        )
    ).scalars().first()
    assert plan is not None
    assert plan.cost_requirement == cost_text

    scene_card = session.get(SceneCard, scenes[0]["scene_id"])
    assert scene_card is not None
    assert scene_card.writer_brief_json["cost_requirement"] == cost_text


def test_workspace_v2_resync_previews_and_updates_materialized_scene_cards_without_overwriting_drafts(
    client,
    session,
) -> None:
    project = _create_project(client, key="selective-resync")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)
    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-resync"},
    )
    assert materialize_response.status_code == 200, materialize_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-resync-outline"},
    )
    assert approve_response.status_code == 200, approve_response.text

    session.expire_all()
    scene_plan = session.execute(
        select(SnowflakeScenePlan)
        .where(SnowflakeScenePlan.project_id == project["project_id"])
        .order_by(SnowflakeScenePlan.scene_plan_id.asc())
    ).scalars().first()
    assert scene_plan is not None
    scene = session.get(SceneCard, scene_plan.scene_id)
    assert scene is not None
    original_goal = scene.scene_goal
    session.add(
        FinalScene(
            row_id="final_scene_resync_keep",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content="作者已经批准的终稿不能被雪花同步覆盖。",
            status="approved",
            source_bundle_id="bundle_resync_keep",
            source_bundle_hash="hash_resync_keep",
        )
    )
    session.add(
        AuthorDraft(
            draft_id="author_draft_resync_keep",
            object_type="scene",
            object_id=scene.scene_id,
            source_text_ref="final_scene:final_scene_resync_keep",
            content="作者正在小修的草稿也不能被覆盖。",
            revision_no=3,
            status="current",
        )
    )
    session.add(
        LlmCall(
            llm_call_id="llm_call_resync_keep",
            scope_type="scene",
            scope_id=scene.scene_id,
            project_id=project["project_id"],
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            node_id="scene_finalize",
            response_payload_summary={"kept": True},
        )
    )
    scene_plan.summary = "她把新证据袋推到桌沿。"
    scene_plan.scene_crucible = "只要她现在公开，新证人就会被找到。"
    scene_plan.goal = "逼盟友承认证据来源。"
    scene_plan.conflict = "盟友用幸存者安全反压她。"
    scene_plan.setback = "她保住证人，却暂时失去完整录音。"
    scene_plan.hook = "第二个录音袋出现在门缝。"
    session.commit()

    preview_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/resync",
        json={"scene_plan_ids": [scene_plan.scene_plan_id], "dry_run": True},
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()["data"]
    assert preview["dry_run"] is True
    assert preview["results"][0]["scene_id"] == scene.scene_id
    assert "scene_goal" in preview["results"][0]["diff"]

    session.expire_all()
    assert session.get(SceneCard, scene.scene_id).scene_goal == original_goal

    sync_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/resync",
        json={"scene_plan_ids": [scene_plan.scene_plan_id]},
        headers={"X-Operator-Ref": "author-resync"},
    )
    assert sync_response.status_code == 200, sync_response.text
    synced = sync_response.json()["data"]
    assert synced["dry_run"] is False
    assert synced["results"][0]["synced"] is True
    assert synced["affected_runtime"]["final_scene_count"] == 1
    assert synced["affected_runtime"]["author_draft_count"] == 1
    assert synced["affected_runtime"]["llm_call_count"] == 1

    session.expire_all()
    scene = session.get(SceneCard, scene_plan.scene_id)
    assert scene.scene_goal == "她把新证据袋推到桌沿。"
    assert scene.writer_brief_json["scene_plan_id"] == scene_plan.scene_plan_id
    assert scene.writer_brief_json["scene_crucible"] == "只要她现在公开，新证人就会被找到。"
    assert session.get(FinalScene, "final_scene_resync_keep").content == "作者已经批准的终稿不能被雪花同步覆盖。"
    assert session.get(AuthorDraft, "author_draft_resync_keep").content == "作者正在小修的草稿也不能被覆盖。"
    assert session.get(LlmCall, "llm_call_resync_keep").response_payload_summary == {"kept": True}


def test_workspace_v2_step_generation_uses_llm_and_persists_project_scoped_call(client, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "category": "Urban Mystery",
            "target_reader": "Readers who want old cases, family cost, and a tightly pressured heroine.",
            "story_kind": "A cost-heavy mystery that drags a heroine back into family truth.",
            "delight_reason": "Every clue closes distance to the truth and increases the personal price.",
            "genre_promise": "The clearer the truth becomes, the more the heroine risks losing.",
            "expected_reader_emotion": "Pressure, doubt, and urgent forward pull.",
            "safety_rules": [
                "Only borrow abstract craft patterns and pacing.",
                "Do not copy characters, settings, plot beats, or signature phrasing.",
            ],
        }
        return LLMResponse(
            request_id="resp_snowflake_generate",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_snowflake_generate"},
            usage={"input_tokens": 111, "output_tokens": 222, "total_tokens": 333},
            raw_usage={"input_tokens": 111, "output_tokens": 222, "total_tokens": 333},
            usage_present=True,
            usage_complete=True,
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="llm-step")

    result = _generate_step(client, project["project_id"], "book_brief")
    step = result["step"]

    assert step["draft"]["category"] == "Urban Mystery"
    assert step["last_generation_source"] == "llm"
    assert step["last_llm_call_id"]
    assert step["artifact"]["llm_call_id"] == step["last_llm_call_id"]

    session.expire_all()
    stored_call = session.get(LlmCall, step["last_llm_call_id"])
    assert stored_call is not None
    assert stored_call.project_id == project["project_id"]
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    assert stored_call.request_payload_summary["step_key"] == "book_brief"
    assert stored_call.request_payload_summary["step_label"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["step_english_label"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["step_instruction"]["kind"] == "text_fingerprint"
    assert stored_call.request_payload_summary["pressure_rubric"]["goal"]["kind"] == "text_fingerprint"
    assert "book_brief" in stored_call.request_payload_summary["current_pressure_diagnosis"]["step_key"]
    assert stored_call.response_payload_summary["structured_output"]["kind"] == "json_fingerprint"
    assert "category" in stored_call.response_payload_summary["structured_output"]["top_level_fields"]
    assert stored_call.accounting_status == "settled"
    assert stored_call.usage_is_estimate is False
    attempt = session.scalars(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == stored_call.llm_call_id
        )
    ).one()
    assert attempt.accounting_status == "settled"
    assert attempt.total_tokens == 333

    step_run = session.get(SnowflakeStepRun, step["artifact"]["step_run_id"])
    assert step_run is not None
    assert step_run.llm_call_id == stored_call.llm_call_id


def test_workspace_v2_live_rejects_blank_character_sheet_llm_candidates(client, session, monkeypatch) -> None:
    project = _create_project(client, key="blank-character-sheet")
    for step_key in ["book_brief", "one_sentence_summary", "one_paragraph_summary"]:
        _approve_generated_step(client, project["project_id"], step_key)

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "characters": [
                {
                    "character_id": f"{project['project_id']}_CHAR01",
                    "display_name": f"{project['project_id']}_CHAR01",
                    "role": "",
                    "goal": "",
                    "ambition": "",
                    "values": [],
                    "conflict": "",
                    "epiphany": "",
                    "one_sentence_summary": "",
                    "one_paragraph_summary": "",
                }
            ]
        }
        return LLMResponse(
            request_id="resp_blank_character_sheet",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_blank_character_sheet"},
            usage={"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/character_sheets/generate",
        json={},
        headers={"X-Idempotency-Key": "blank-character-sheet"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA"
    assert "character_sheets" in error["message"]
    assert "role" in error["message"]
    assert error["details"]["node_id"] == "snowflake_step_generate"

    session.expire_all()
    assert (
        session.query(SnowflakeStepRun)
        .filter(
            SnowflakeStepRun.project_id == project["project_id"],
            SnowflakeStepRun.step_key == "character_sheets",
        )
        .count()
        == 0
    )
    failed_call = session.scalars(
        select(LlmCall).where(
            LlmCall.project_id == project["project_id"],
            LlmCall.node_id == "snowflake_step_generate",
        )
    ).one()
    assert failed_call.accounting_status == "failed"
    assert failed_call.error_code == "LLM_RESPONSE_INVALID_SCHEMA"
    failed_attempt = session.scalars(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == failed_call.llm_call_id
        )
    ).one()
    assert failed_attempt.accounting_status == "settled"


def test_workspace_v2_live_missing_snowflake_route_explains_node_route_gap(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.llm_client import ModelRoutingConfig

    monkeypatch.setattr(
        "novel_system.services.snowflake_workspace_llm.load_model_routing_config",
        lambda: ModelRoutingConfig(
            node_routing={},
            task_routing={},
            retry_budget={},
            job_runtime={},
        ),
    )

    project = _create_project(client, key="missing-snowflake-route")
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={},
        headers={"X-Idempotency-Key": "missing-snowflake-route"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING"
    assert "模型已接入" in error["message"]
    assert "snowflake_step_generate" in error["message"]
    assert "一键补齐" in error["message"]
    assert error["details"]["node_id"] == "snowflake_step_generate"
    assert error["details"]["next_action"] == "sync_missing_llm_node_routes"


def test_workspace_v2_live_responses_404_explains_protocol_mismatch(
    client, session, monkeypatch
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.llm_client import LLMHTTPError

    def fake_generate(self, request):  # noqa: ANN001
        raise LLMHTTPError(
            "LLM_HTTP_FAILURE",
            "llm request failed with status 404",
            status_code=404,
            details={
                "provider_id": "gcli2api",
                "provider_type": "openai",
                "model": "gemini-3.1-pro-preview",
                "api_mode": "responses",
                "endpoint": "/responses",
                "next_action": "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider",
            },
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="responses-404-hint")
    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/book_brief/generate",
        json={},
        headers={"X-Idempotency-Key": "responses-404-hint"},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "SNOWFLAKE_LLM_CALL_FAILED"
    assert "Responses API" in error["message"]
    assert "chat" in error["message"]
    assert "gcli2api" in error["message"]
    assert error["details"]["response_summary"]["details"]["endpoint"] == "/responses"
    assert (
        error["details"]["response_summary"]["details"]["next_action"]
        == "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider"
    )
    session.expire_all()
    parent = session.scalars(
        select(LlmCall).where(
            LlmCall.project_id == project["project_id"],
            LlmCall.node_id == "snowflake_step_generate",
        )
    ).one()
    assert parent.accounting_status == "failed"
    assert parent.error_code == "LLM_HTTP_FAILURE"
    assert parent.step == "book_brief"
    child = session.scalars(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == parent.llm_call_id
        )
    ).one()
    assert child.accounting_status == "failed"


def test_workspace_v2_assistant_uses_draft_override_and_returns_candidate_patch(client, session, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    override_reader = "Unsaved reader focus: readers who want cold cases and family-cost pressure."

    def fake_generate(self, request):  # noqa: ANN001
        assert override_reader in request.messages[-1]["content"]
        payload = {
            "reply": "Tighten the reader promise around old cases plus family cost.",
            "suggestions": ["Lead with the cost first, then the genre pleasure."],
            "candidate_label": "Narrow target reader",
            "candidate_patch": {
                "target_reader": "Readers who want old cases and unresolved family cost.",
                "genre_promise": "The clearer the truth becomes, the more family debt it exposes.",
                "illegal_field": "This must not be written back.",
            },
        }
        return LLMResponse(
            request_id="resp_snowflake_assistant",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_snowflake_assistant"},
            usage={"input_tokens": 90, "output_tokens": 120, "total_tokens": 210},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)
    project = _create_project(client, key="assistant-override")

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/assistant",
        json={
            "step_key": "book_brief",
            "message": "Help me narrow the reader promise.",
            "draft_override": {
                "category": "Urban Mystery",
                "target_reader": override_reader,
            },
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]

    assert payload["source"] == "llm"
    assert payload["candidate_label"] == "Narrow target reader"
    assert payload["candidate_patch"]["target_reader"].startswith("Readers who want old cases")
    assert "illegal_field" not in payload["candidate_patch"]
    assert payload["llm_call_id"]

    session.expire_all()
    stored_call = session.get(LlmCall, payload["llm_call_id"])
    assert stored_call is not None
    assert stored_call.project_id == project["project_id"]
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    assert stored_call.request_payload_summary["draft"]["kind"] == "json_fingerprint"
    assert stored_call.request_payload_summary["pressure_rubric"]["dimensions"]
    assert stored_call.request_payload_summary["current_pressure_diagnosis"]["pressure_flags"]
    assert stored_call.accounting_status == "settled"
    assert stored_call.usage_is_estimate is True
    assert stored_call.total_tokens > 0


def test_workspace_v2_scene_triage_suggest_returns_non_persistent_suggestions(client, session, monkeypatch) -> None:
    project = _create_project(client, key="triage-suggest")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def fake_generate(self, request):  # noqa: ANN001
        payload = {
            "items": [
                {
                    "scene_id": f"{project['project_id']}_CH01_SC01",
                    "status": "maybe",
                    "notes": "The scene has desire, but the conflict cost should climb further.",
                    "missing_fields": ["setback"],
                    "fix_steps": ["Make the final turn leave the protagonist worse off."],
                }
            ]
        }
        return LLMResponse(
            request_id="resp_scene_triage_suggest",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_scene_triage_suggest"},
            usage={"input_tokens": 55, "output_tokens": 77, "total_tokens": 132},
            finish_reason="stop",
        )

    _patch_accounted_generate(monkeypatch, fake_generate)

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage/suggest",
        json={},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["source"] == "llm"
    assert payload["items"][0]["status"] == "maybe"
    assert payload["items"][0]["missing_fields"] == ["setback"]
    assert payload["items"][0]["fix_steps"][0].startswith("Make the final turn")
    assert payload["llm_call_id"]

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    assert workspace["triage_items"][0]["status"] == ""
    assert workspace["triage_items"][0]["notes"] == ""

    session.expire_all()
    stored_call = session.get(LlmCall, payload["llm_call_id"])
    assert stored_call is not None
    assert (stored_call.scope_type, stored_call.scope_id) == (
        "project",
        project["project_id"],
    )
    proactive = stored_call.request_payload_summary["pressure_rubric"]["scene_rules"]["proactive"]
    assert len(proactive) == 3
    assert all(item["kind"] == "text_fingerprint" for item in proactive)
    assert stored_call.request_payload_summary["current_pressure_diagnosis"]["step_key"] == "scene_details"
    assert (
        session.query(SnowflakeSceneTriageItem)
        .filter(SnowflakeSceneTriageItem.project_id == project["project_id"])
        .count()
        == 0
    )


def test_workspace_v2_scene_triage_fallback_uses_chinese_coaching_copy(client, monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    project = _create_project(client, key="triage-fallback-cn")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    weak_scene = {
        **scene_step["draft"]["scenes"][0],
        "scene_type": "proactive",
        "scene_crucible": "A room.",
        "goal": "Talk to the witness.",
        "conflict": "They argue.",
        "setback": "",
    }
    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [weak_scene]}},
    )
    assert save_response.status_code == 200, save_response.text

    response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage/suggest",
        json={},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    item = payload["items"][0]

    assert payload["source"] == "fallback"
    assert "修复场景压力" in item["notes"]
    assert "Repair scene pressure" not in item["notes"]
    assert item["fix_steps"]
    assert all("setback" not in step for step in item["fix_steps"])


def test_workspace_v2_persists_triage_repair_metadata_and_blocks_rewrite_materialization(client, session) -> None:
    project = _create_project(client, key="triage-blocks-materialize")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    first_scene_id = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")["draft"]["scenes"][0]["scene_id"]
    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene_id,
                    "status": "rewrite",
                    "notes": "This scene has no recoverable pressure.",
                    "missing_fields": ["goal", "conflict", "setback"],
                    "fix_steps": ["Rebuild the scene premise before materializing."],
                }
            ]
        },
    )

    assert triage_response.status_code == 200, triage_response.text
    triaged = triage_response.json()["data"]
    assert triaged["items"][0]["missing_fields"] == ["goal", "conflict", "setback"]
    assert triaged["items"][0]["fix_steps"][0].startswith("Rebuild")
    assert triaged["workspace"]["materialization_gate"]["status"] == "blocked"

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-rewrite-blocked"},
    )

    assert materialize_response.status_code == 409
    error = materialize_response.json()["error"]
    assert error["code"] == "SNOWFLAKE_TRIAGE_BLOCKED"
    assert error["details"]["materialization_gate"]["status"] == "blocked"
    # 回归守护：这条 message 曾经硬编码英文，直接被 React 前端 window.alert 原样展示给
    # 中文用户（ws-snow.jsx::materializeFromHeader）。锁定为中文，避免再次退化。
    assert "重写" in error["message"], error["message"]
    assert all(ch.isascii() is False or not ch.isalpha() for ch in error["message"]), error["message"]

    session.expire_all()
    first_triage = (
        session.query(SnowflakeSceneTriageItem)
        .filter(SnowflakeSceneTriageItem.project_id == project["project_id"])
        .first()
    )
    assert first_triage is not None
    assert first_triage.missing_fields_json == ["goal", "conflict", "setback"]
    assert first_triage.fix_steps_json[0].startswith("Rebuild")


def test_workspace_v2_computes_rule_first_scene_diagnostics_and_blocks_auto_rewrite(client) -> None:
    project = _create_project(client, key="auto-diagnosis")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    first_scene = scene_step["draft"]["scenes"][0]
    broken_scene = {
        **first_scene,
        "title": "",
        "summary": "",
        "primary_form": "reactive",
        "scene_type": "reactive",
        "crucible": "",
        "scene_crucible": "",
        "reaction": "",
        "dilemma": "",
        "decision": "",
    }
    partial_scene = {
        **scene_step["draft"]["scenes"][1],
        "primary_form": "proactive",
        "scene_type": "proactive",
        "crucible": "The heroine cannot leave without losing the only witness.",
        "goal": "Get the witness statement before the train leaves.",
        "conflict": "The witness keeps changing the story as family pressure rises.",
        "setback": "",
    }

    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [broken_scene, partial_scene]}},
    )
    assert save_response.status_code == 200, save_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-auto-diagnosis-scenes"},
    )
    assert approve_response.status_code == 200, approve_response.text

    diagnosed = approve_response.json()["data"]["workspace"]
    triage_items = diagnosed["triage_items"]
    rewrite_item = next(item for item in triage_items if item["scene_id"] == broken_scene["scene_id"])
    maybe_item = next(item for item in triage_items if item["scene_id"] == partial_scene["scene_id"])

    assert rewrite_item["status"] == ""
    assert rewrite_item["recommended_status"] == "rewrite"
    assert rewrite_item["triage_source"] == "auto_diagnosis"
    assert rewrite_item["effective_status"] == "unreviewed"
    assert rewrite_item["blocking"] is False
    assert rewrite_item["score"] < 40
    assert rewrite_item["missing_fields"] == ["crucible", "reaction", "dilemma", "decision"]
    assert "scene_core_empty" in rewrite_item["pressure_flags"]
    assert rewrite_item["fix_steps"]

    assert maybe_item["recommended_status"] == "maybe"
    assert maybe_item["triage_source"] == "auto_diagnosis"
    assert maybe_item["effective_status"] == "unreviewed"
    assert maybe_item["blocking"] is False
    assert maybe_item["score"] < 100
    assert maybe_item["missing_fields"] == ["setback"]

    gate = diagnosed["materialization_gate"]
    assert gate["status"] == "blocked"
    assert any(broken_scene["scene_id"] in blocker for blocker in gate["blockers"])
    assert any(item["kind"] == "triage_confirmation_required" for item in gate["items"])
    assert all("marked rewrite" not in blocker for blocker in gate["blockers"])

    materialize_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "materialize-auto-rewrite-blocked"},
    )
    assert materialize_response.status_code == 409
    assert materialize_response.json()["error"]["details"]["materialization_gate"]["status"] == "blocked"


def test_workspace_v2_flags_weak_scene_pressure_even_when_required_fields_are_present(client) -> None:
    project = _create_project(client, key="weak-scene-pressure")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    weak_proactive = {
        **scene_step["draft"]["scenes"][0],
        "scene_type": "proactive",
        "scene_crucible": "A room.",
        "goal": "Talk to the witness.",
        "conflict": "They argue.",
        "setback": "She succeeds.",
        "cost_requirement": "She loses something.",
    }
    weak_reactive = {
        **scene_step["draft"]["scenes"][1],
        "scene_type": "reactive",
        "scene_crucible": "She feels bad.",
        "reaction": "She is upset.",
        "dilemma": "Stay or leave.",
        "decision": "She decides.",
        "cost_requirement": "She gives up something.",
    }

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [weak_proactive, weak_reactive]}},
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["workspace"]["triage_items"]
    proactive_item = next(item for item in items if item["scene_id"] == weak_proactive["scene_id"])
    reactive_item = next(item for item in items if item["scene_id"] == weak_reactive["scene_id"])

    assert proactive_item["recommended_status"] == "maybe"
    assert "weak_conflict_escalation" in proactive_item["pressure_flags"]
    assert "weak_setback_cost" in proactive_item["pressure_flags"]
    assert proactive_item["score"] < 100
    assert proactive_item["fix_steps"]

    assert reactive_item["recommended_status"] == "maybe"
    assert "fake_dilemma" in reactive_item["pressure_flags"]
    assert "weak_decision_next_goal" in reactive_item["pressure_flags"]
    assert reactive_item["score"] < 100
    assert reactive_item["fix_steps"]


def test_workspace_v2_manual_triage_override_of_auto_rewrite_becomes_gate_warning(client) -> None:
    project = _create_project(client, key="auto-diagnosis-override")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    first_scene = {
        **scene_step["draft"]["scenes"][0],
        "title": "",
        "summary": "",
        "scene_type": "proactive",
        "crucible": "",
        "scene_crucible": "",
        "goal": "",
        "conflict": "",
        "setback": "",
    }
    save_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [first_scene]}},
    )
    assert save_response.status_code == 200, save_response.text
    approve_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details/approve",
        json={},
        headers={"X-Idempotency-Key": "approve-auto-diagnosis-override-scenes"},
    )
    assert approve_response.status_code == 200, approve_response.text
    assert approve_response.json()["data"]["workspace"]["materialization_gate"]["status"] == "blocked"

    triage_response = client.post(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/scene-triage",
        json={
            "items": [
                {
                    "scene_id": first_scene["scene_id"],
                    "status": "maybe",
                    "notes": "I will keep the scene but repair the pressure before drafting.",
                }
            ]
        },
    )
    assert triage_response.status_code == 200, triage_response.text
    payload = triage_response.json()["data"]
    item = payload["items"][0]

    assert item["recommended_status"] == "rewrite"
    assert item["effective_status"] == "maybe"
    assert item["blocking"] is False
    assert item["manual_override"] is True
    assert payload["workspace"]["materialization_gate"]["status"] == "warning"
    warnings = payload["workspace"]["materialization_gate"]["warnings"]
    assert any("人工覆盖了自动废除重写诊断" in warning for warning in warnings)
    assert all("overrides an automatic rewrite diagnosis" not in warning for warning in warnings)


def test_workspace_v2_normalizes_legacy_drafts_to_method_v2(client) -> None:
    project = _create_project(client, key="legacy-normalization")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    short_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/short_synopsis",
        json={"draft": {"paragraphs": ["legacy setup", "legacy turn", "legacy ending"]}},
    )
    assert short_response.status_code == 200, short_response.text
    short_step = short_response.json()["data"]["step"]
    assert short_step["draft"]["paragraphs"][:3] == ["legacy setup", "legacy turn", "legacy ending"]
    assert len(short_step["draft"]["paragraphs"]) == 5

    _approve_step(client, project["project_id"], "short_synopsis")
    _approve_generated_step(client, project["project_id"], "character_synopses")
    _approve_generated_step(client, project["project_id"], "long_synopsis")

    bible_response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/character_bibles",
        json={
            "draft": {
                "characters": [
                    {
                        "character_id": "LEGACY_CHAR",
                        "display_name": "Legacy Lead",
                        "role": "lead",
                        "age": "32",
                        "home": "Dock district",
                        "strongest_trait": "Acts under pressure",
                        "weakest_trait": "Confuses silence with loyalty",
                        "deepest_fear": "The truth will cost the family",
                        "how_character_changes": "Learns to choose truth with a cost",
                    }
                ]
            }
        },
    )
    assert bible_response.status_code == 200, bible_response.text
    character = bible_response.json()["data"]["step"]["draft"]["characters"][0]
    assert character["physical_profile"]["age"] == "32"
    assert character["personality_profile"]["strongest_trait"] == "Acts under pressure"
    assert character["environment_profile"]["home"] == "Dock district"
    assert character["psychological_profile"]["deepest_fear"] == "The truth will cost the family"
    assert character["psychological_profile"]["character_arc"] == "Learns to choose truth with a cost"


def test_workspace_v2_allows_optional_skips_but_blocks_skipped_materialization_steps(client) -> None:
    optional_project = _create_project(client, key="optional-skips")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]:
        _approve_generated_step(client, optional_project["project_id"], step_key)
    for step_key in [
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _generate_step(
            client,
            optional_project["project_id"],
            step_key,
            {"skip": True, "skip_reason": f"not needed for this quality pass: {step_key}"},
        )
    _approve_generated_step(client, optional_project["project_id"], "scene_list")
    _approve_generated_step(client, optional_project["project_id"], "scene_details")

    optional_workspace = client.get(
        f"/api/v2/projects/{optional_project['project_id']}/snowflake-workspace"
    ).json()["data"]
    assert optional_workspace["current_step_key"] is None
    assert optional_workspace["materialization_gate"]["status"] == "warning"
    assert optional_workspace["materialization_gate"]["blockers"] == []
    assert optional_workspace["materialization_gate"]["warnings"]
    assert any("已跳过" in warning for warning in optional_workspace["materialization_gate"]["warnings"])
    assert all("was skipped" not in warning for warning in optional_workspace["materialization_gate"]["warnings"])

    hard_project = _create_project(client, key="hard-skips")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
    ]:
        _approve_generated_step(client, hard_project["project_id"], step_key)
    for step_key in [
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
    ]:
        _generate_step(
            client,
            hard_project["project_id"],
            step_key,
            {"skip": True, "skip_reason": f"skip for gate test: {step_key}"},
        )
    required_skip = client.post(
        f"/api/v2/projects/{hard_project['project_id']}/snowflake-workspace/steps/scene_list/generate",
        json={"skip": True, "skip_reason": "required steps must be authored"},
        headers={"X-Idempotency-Key": f"generate-v2-{hard_project['project_id']}-scene-list-required-skip"},
    )
    assert required_skip.status_code == 400
    assert required_skip.json()["error"]["code"] == "SNOWFLAKE_STEP_NOT_SKIPPABLE"

    hard_workspace = client.get(f"/api/v2/projects/{hard_project['project_id']}/snowflake-workspace").json()["data"]
    assert hard_workspace["current_step_key"] == "scene_list"
    assert hard_workspace["materialization_gate"]["status"] == "blocked"
    assert any("场景列表" in blocker for blocker in hard_workspace["materialization_gate"]["blockers"])
    assert all("scene_list" not in blocker for blocker in hard_workspace["materialization_gate"]["blockers"])


def test_workspace_v2_fallback_generation_uses_project_outline_instead_of_fixed_fixture(client) -> None:
    project = _create_project(
        client,
        key="outline-fallback",
        title="Glass Orchard Pact",
        genre="Eco Thriller",
        outline_text=(
            "Mira protects the glass orchard that stores the last clean rain.\n"
            "The Orchard Council poisons the water archive to hide an old pact.\n"
            "Mira must expose the water ledger before the orchard burns."
        ),
    )

    _approve_generated_step(client, project["project_id"], "book_brief")
    one_sentence = _generate_step(client, project["project_id"], "one_sentence_summary")["step"]["draft"]["summary"]
    assert "glass orchard" in one_sentence.lower() or "water archive" in one_sentence.lower()

    _approve_step(client, project["project_id"], "one_sentence_summary")
    _approve_generated_step(client, project["project_id"], "one_paragraph_summary")
    _generate_step(
        client,
        project["project_id"],
        "character_sheets",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "short_synopsis",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "character_synopses",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "long_synopsis",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    _generate_step(
        client,
        project["project_id"],
        "character_bibles",
        {"skip": True, "skip_reason": "testing outline-driven fallback"},
    )
    scene_list = _generate_step(client, project["project_id"], "scene_list")["step"]["draft"]["scenes"]
    scene_text = json.dumps(scene_list, ensure_ascii=False).lower()
    assert "glass orchard" in scene_text or "water archive" in scene_text


def test_workspace_v2_scene_details_support_primary_form_with_optional_followup_fields(client) -> None:
    project = _create_project(client, key="primary-form")
    for step_key in [
        "book_brief",
        "one_sentence_summary",
        "one_paragraph_summary",
        "character_sheets",
        "short_synopsis",
        "character_synopses",
        "long_synopsis",
        "character_bibles",
        "scene_list",
        "scene_details",
    ]:
        _approve_generated_step(client, project["project_id"], step_key)

    workspace = client.get(f"/api/v2/projects/{project['project_id']}/snowflake-workspace").json()["data"]
    scene_step = next(step for step in workspace["steps"] if step["step_key"] == "scene_details")
    scene = {
        **scene_step["draft"]["scenes"][0],
        "primary_form": "proactive",
        "scene_type": "reactive",
        "goal": "Get the sealed witness record before the archive closes.",
        "conflict": "The clerk refuses, then calls security, then reveals the file was moved.",
        "setback": "She gets the box but it points to her father, making the win costlier.",
        "reaction": "Her hands shake when she sees the family seal.",
        "dilemma": "Expose the seal and lose her family, or hide it and let the case die.",
        "decision": "She decides to meet the archivist who moved the file next.",
    }

    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": [scene]}},
    )
    assert response.status_code == 200, response.text
    saved_scene = response.json()["data"]["step"]["draft"]["scenes"][0]
    assert saved_scene["primary_form"] == "proactive"
    assert saved_scene["scene_type"] == "proactive"
    assert saved_scene["reaction"].startswith("Her hands shake")
    assert saved_scene["dilemma"].startswith("Expose the seal")
