"""Wave 3（结果闭环治理 §5.5/§6.3）：Best-of-N 作者终选门。

完成门可复算证明：
- 关键场景候选生成后暂停编排（awaiting_candidate_selection），未选择前
  管线不归档、adopt-current 也拒绝（双入口封死）；
- 盲化视图：默认按 blinded_order 输出全文、剥离机器分数；主动展开不重排；
- 终选一次写入：同选幂等、异选 409，显式重开后方可改选（审计留痕）；
- 选择后 resume-after-selection 从批判修订/QC 继续，安全归档，终稿=选中稿。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RelationProfile,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.near_final import (
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


PROJECT_ID = "PROJECT300"
SCENE_ID = "CH300_SC01"
CHAPTER_ID = "CH300"


@pytest.fixture(autouse=True)
def _multi_candidate_authorization_for_candidate_gate_tests(monkeypatch) -> None:
    """Candidate-gate mechanics run with three authorized candidates.

    Production always drafts a single candidate; these tests exercise the
    downstream multi-candidate selection state machine.
    """

    from novel_system.services.orchestrator import Orchestrator

    def _three_candidates(self, contract, *, criticality=None):
        self._best_of_n_policy_cap = 3
        if criticality is not None:
            return max(1, min(3, int(criticality.initial_best_of_n)))
        return 3

    monkeypatch.setattr(Orchestrator, "_best_of_n_count", _three_candidates)


def _response(payload: dict, *, request_id: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "fake-model",
            "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
        finish_reason="stop",
    )


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests)
        payload = {
            "scene_text": f"Provider-generated draft #{index} for terminal selection.",
            "continuity_notes": [],
        }
        return _response(payload, request_id=f"resp_scene_{index:03d}")


class FakePassQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def generate(self, request: LLMRequest) -> LLMResponse:
        return _response(self.payload, request_id="resp_qc_001")


def _hard_pass() -> dict:
    return {
        "resolution_code": "hard_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
    }


def _soft_pass() -> dict:
    return {
        "resolution_code": "soft_pass",
        "pass_flag": True,
        "next_action": "pass",
        "issues": [],
        "rewrite_brief": [],
        "carry_forward_note": False,
        "note_scope": None,
        "carry_note_text": None,
    }


def _seed_scene(session, *, constraint_intensity: float | None = 0.9) -> None:
    session.add(
        StoryProject(project_id=PROJECT_ID, title="Selection gate", outline_text="")
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id=CHAPTER_ID, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            project_id=PROJECT_ID,
            chapter_id=CHAPTER_ID,
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text="",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
            constraint_intensity=constraint_intensity,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.add(
        VoiceProfile(
            row_id="voice_profile_VOICE_CHAR_A_v1",
            voice_profile_id="VOICE_CHAR_A",
            version=1,
            character_id="CHAR_A",
            content="tight internal narration",
            active_flag=1,
        )
    )
    session.add(
        RelationProfile(
            row_id="relation_profile_REL_CHAR_A_CHAR_B_v1",
            relation_profile_id="REL_CHAR_A_CHAR_B",
            left_character_id="CHAR_A",
            right_character_id="CHAR_B",
            version=1,
            content="they mistrust each other but still care",
            active_flag=1,
        )
    )
    session.commit()


def _make_orchestrator(session) -> Orchestrator:
    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session, llm_client=FakeSceneClient()
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=FakePassQcClient(_hard_pass())),
        soft_qc_engine=SoftQcEngine(session, llm_client=FakePassQcClient(_soft_pass())),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(
        session, llm_client=support
    )
    return orchestrator


def _selection_gate(session) -> HumanReviewEvent:
    events = (
        session.execute(
            select(HumanReviewEvent).order_by(HumanReviewEvent.created_at.desc())
        )
        .scalars()
        .all()
    )
    for event in events:
        if (event.details_json or {}).get("gate_type") == "style_candidate_selection":
            return event
    raise AssertionError("no style_candidate_selection gate event found")


# ---------- 暂停：关键场景未选择前不可归档 ----------


def test_critical_scene_pauses_before_selection(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(session)

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    state = session.get(SceneRunState, SCENE_ID)
    assert result["scene_status"] == "awaiting_candidate_selection"
    assert result["author_state"] == "awaiting_author_choice"
    assert result["can_archive"] is False
    assert result["latest_valid_draft_row_id"]
    assert state.current_final_scene_row_id is None
    assert session.execute(select(FinalScene)).scalars().all() == []

    gate = _selection_gate(session)
    details = gate.details_json
    assert state.run_checkpoint == "selection_wait"
    assert (
        state.run_checkpoint_json["artifact_refs"]["selection_event_id"]
        == gate.event_id
    )
    assert (
        state.run_checkpoint_json["artifact_refs"]["selection_candidate_row_ids"]
        == details["candidate_row_ids"]
    )
    rankings = state.run_checkpoint_json["artifact_refs"]["style_candidate_rankings"]
    assert len(rankings) == len(details["candidate_row_ids"])
    assert all(
        item["rerank"]["reason"] == "bundle_has_no_style_profile" for item in rankings
    )
    assert all(item["rerank"]["applied_mode"] == "shadow" for item in rankings)
    assert details["decision_status"] == "awaiting"
    assert details["candidate_row_ids"]
    assert sorted(details["blinded_order"]) == sorted(details["candidate_row_ids"])
    assert "tokens_used" in details
    assert details["style_feedback_snapshot"]["candidate_count"] == len(
        details["candidate_row_ids"]
    )
    assert "Provider-generated draft" not in json.dumps(
        details["style_feedback_snapshot"],
        ensure_ascii=False,
    )


def test_explicit_style_selection_reason_records_non_activating_feedback(
    client,
    session,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    selected_row_id = gate.details_json["candidate_row_ids"][0]

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{selected_row_id}/select",
        json={
            "no_clear_difference": False,
            "duration_ms": 1234,
            "preference_tags": ["style_match"],
        },
        headers={"X-Idempotency-Key": "w3-style-feedback-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["style_feedback_recorded"] is True
    session.refresh(gate)
    feedback = gate.details_json["style_feedback"]
    assert feedback["preference_tags"] == ["style_match"]
    assert feedback["style_attributed"] is True
    assert feedback["policy_evidence_eligible"] is False
    assert gate.status == "resolved"
    history = gate.details_json["decision_history"]
    assert history[-1]["duration_ms"] == 1234
    assert history[-1]["style_feedback_id"] == feedback["feedback_id"]


def test_selection_rejects_unknown_feedback_reason(client, session) -> None:
    _seed_scene(session)
    row_ids = ["w3_cand_reason"]
    _seed_manual_gate(session, row_ids, row_ids)

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{row_ids[0]}/select",
        json={"preference_tags": ["imitate_author_identity"]},
        headers={"X-Idempotency-Key": "w3-style-feedback-invalid"},
    )

    assert response.status_code == 422


def test_candidate_gate_excludes_a_candidate_already_proven_to_copy_reference(
    session,
) -> None:
    _seed_scene(session)
    copied = SimpleNamespace(
        row_id="copy",
        content="一段已经被本地连续重叠检测确认的候选正文。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": False},
    )
    safe = SimpleNamespace(
        row_id="safe",
        content="另一段没有复刻来源文本的候选正文。",
        ranking_audit={"plagiarism_checked": True, "plagiarism_passed": True},
    )

    offered = Orchestrator(session)._offer_candidates_for_selection(
        session.get(SceneCard, SCENE_ID),
        session.get(SceneRunState, SCENE_ID),
        {},
        [copied, safe],
    )

    assert offered == ["safe"]
    assert _selection_gate(session).details_json["candidate_row_ids"] == ["safe"]


def test_standard_scene_does_not_pause(session) -> None:
    _seed_scene(session, constraint_intensity=0.5)  # standard：机器下限自动选择
    orchestrator = _make_orchestrator(session)

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    assert result["scene_status"] == "archived"


def test_adopt_refuses_before_selection(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/adopt-current",
        json={},
        headers={"X-Idempotency-Key": "w3-adopt-await"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELECTION_REQUIRED"


# ---------- 盲化视图 ----------


def _seed_manual_gate(
    session, row_ids: list[str], blinded: list[str]
) -> HumanReviewEvent:
    for i, row_id in enumerate(row_ids):
        session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=SCENE_ID,
                chapter_id=CHAPTER_ID,
                stage="style_draft",
                content=f"候选正文 {i}：潮水退去，闸门上的名字露出来。",
                source_bundle_id="bundle_w3",
                source_bundle_hash="hash_w3",
            )
        )
    event = HumanReviewEvent(
        event_id="hre_sel_manual_1",
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        object_ref=f"candidate_selection:{SCENE_ID}",
        event_source="candidate_selection",
        priority="high",
        status="awaiting_review",
        allowed_actions_json=["select"],
        details_json={
            "gate_type": "style_candidate_selection",
            "candidate_row_ids": row_ids,
            "blinded_order": blinded,
            "decision_status": "awaiting",
            "selected_row_id": None,
            "tokens_used": 0,
        },
    )
    session.add(event)
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_status = "awaiting_candidate_selection"
    state.current_human_review_event_id = event.event_id
    session.commit()
    return event


def test_blinded_candidates_view_strips_scores_and_uses_blinded_order(
    client, session
) -> None:
    _seed_scene(session)
    row_ids = ["w3_cand_a", "w3_cand_b", "w3_cand_c"]
    blinded = ["w3_cand_b", "w3_cand_c", "w3_cand_a"]
    _seed_manual_gate(session, row_ids, blinded)

    data = client.get(f"/api/v1/scenes/{SCENE_ID}/style-candidates").json()["data"]

    assert data["blinded"] is True
    assert [c["row_id"] for c in data["candidates"]] == blinded
    for candidate in data["candidates"]:
        assert "adversarial_score" not in candidate  # 默认剥离机器分数（§5.5）
        assert "selected" not in candidate  # 盲化视图不泄漏机器预选
        assert candidate["content"]  # 展示完整正文，不只预览

    # 作者主动展开：附分数但不得重排（分数只做标注，不做默认排序）
    scored = client.get(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates?include_scores=true"
    ).json()["data"]
    assert [c["row_id"] for c in scored["candidates"]] == blinded
    assert all("adversarial_score" in c for c in scored["candidates"])


# ---------- 终选锁定与重开 ----------


def test_select_outside_gate_candidates_rejected(client, session) -> None:
    _seed_scene(session)
    _seed_manual_gate(session, ["w3_cand_a"], ["w3_cand_a"])
    session.add(
        SceneDraft(
            row_id="w3_outside",
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            stage="style_draft",
            content="不在终选清单里的旧稿。",
            source_bundle_id="bundle_w3",
            source_bundle_hash="hash_w3",
        )
    )
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/w3_outside/select",
        json={},
        headers={"X-Idempotency-Key": "w3-select-outside"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_IN_GATE"


# ---------- resume：选择后安全续跑 ----------


def test_resume_requires_selection(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    response = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-early"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELECTION_REQUIRED"


def test_select_then_resume_archives_the_chosen_candidate(client, session) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    chosen_content = session.get(SceneDraft, chosen_row_id).content

    selected = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
        json={"no_clear_difference": True},
        headers={"X-Idempotency-Key": "w3-select-resume-1"},
    )
    assert selected.status_code == 200

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-1"},
    )
    assert resumed.status_code == 200
    data = resumed.json()["data"]
    assert data["scene_status"] == "archived"
    assert data["author_state"] == "archived"

    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert state.active_execution_id == "idempotency:w3-resume-1"
    assert state.run_checkpoint_json["execution_id"] == "idempotency:w3-resume-1"
    final = session.get(FinalScene, state.current_final_scene_row_id)
    assert final is not None and final.status == "archived"
    # 终稿=作者选中稿，或其唯一一次批判修订稿（§5.5 允许；血缘必须指向选中稿）
    if final.content != chosen_content:
        from novel_system.db.models import AttemptTracker

        attempts = (
            session.execute(
                select(AttemptTracker).where(AttemptTracker.scene_id == SCENE_ID)
            )
            .scalars()
            .all()
        )
        assert any(
            (attempt.details_json or {}).get("source_style_draft_row_id")
            == chosen_row_id
            for attempt in attempts
        ), "修订稿的来源必须是作者选中稿"

    # 重复 resume 不重复归档
    again = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-2"},
    )
    assert again.status_code in (200, 409)
    finals = (
        session.execute(select(FinalScene).where(FinalScene.scene_id == SCENE_ID))
        .scalars()
        .all()
    )
    assert len(finals) == 1

    session.refresh(gate)
    assert gate.details_json["decision_status"] == "selected"
    assert gate.status != "awaiting_review"  # gate 已闭合


def test_selection_resume_surfaces_lifecycle_budget_boundary_as_recoverable_payload(
    client,
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-budget-boundary"},
        ).status_code
        == 200
    )

    def reject_at_budget(self, scene_id: str):  # noqa: ANN001, ANN202
        raise LLMAccountingRejected(
            "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
            "scene token budget exhausted before dispatch",
        )

    monkeypatch.setattr(
        Orchestrator, "_resume_after_selection_pipeline", reject_at_budget
    )
    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-budget-boundary"},
    )

    assert resumed.status_code == 200
    block = resumed.json()["data"]["lifecycle_budget_block"]
    assert block == {
        "code": "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "message": "scene token budget exhausted before dispatch",
        "resume_mode": "selection",
    }
    state = session.get(SceneRunState, SCENE_ID)
    assert state.run_execution_status == "waiting_selection"
    assert state.scene_status == "awaiting_candidate_selection"


def test_select_then_resume_accepts_completed_de_template_candidate_without_replaying_style_provider(
    client,
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:selection-resume-de-template"],
            "findings": [],
        },
    )
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    gate = _selection_gate(session)
    offered_row_ids = gate.details_json["candidate_row_ids"]
    chosen = next(
        session.get(SceneDraft, row_id)
        for row_id in offered_row_ids
        if session.get(SceneDraft, row_id).stage == "de_template"
    )
    state = session.get(SceneRunState, SCENE_ID)
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    chosen_item = next(
        item for item in work_items if item["final"]["row_id"] == chosen.row_id
    )
    assert chosen_item["de_template_outcome"]["status"] == "completed"
    assert chosen_item["base"]["row_id"] != chosen_item["final"]["row_id"]

    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen.row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-de-template"},
        ).status_code
        == 200
    )
    style_rows_before = list(
        session.execute(
            select(SceneDraft.row_id)
            .where(
                SceneDraft.scene_id == SCENE_ID,
                SceneDraft.stage.in_(("style_draft", "de_template")),
            )
            .order_by(SceneDraft.row_id)
        ).scalars()
    )
    style_call_ids_before = list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(
                LlmCall.scene_id == SCENE_ID,
                LlmCall.step.in_(("style_draft", "de_template")),
            )
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    )

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-de-template"},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["scene_status"] == "archived"
    assert (
        list(
            session.execute(
                select(SceneDraft.row_id)
                .where(
                    SceneDraft.scene_id == SCENE_ID,
                    SceneDraft.stage.in_(("style_draft", "de_template")),
                )
                .order_by(SceneDraft.row_id)
            ).scalars()
        )
        == style_rows_before
    )
    assert (
        list(
            session.execute(
                select(LlmCall.llm_call_id)
                .where(
                    LlmCall.scene_id == SCENE_ID,
                    LlmCall.step.in_(("style_draft", "de_template")),
                )
                .order_by(LlmCall.llm_call_id)
            ).scalars()
        )
        == style_call_ids_before
    )


def test_rejected_de_template_is_audited_and_checkpoint_resume_uses_base_fallback(
    client,
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:selection-rejected-de-template"],
            "findings": [],
        },
    )
    monkeypatch.setattr(
        "novel_system.services.scene_generation._assess_de_template_rewrite",
        lambda **kwargs: {
            "accepted": False,
            "reasons": ["test_rejection"],
        },
    )

    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()

    gate = _selection_gate(session)
    offered_row_ids = gate.details_json["candidate_row_ids"]
    state = session.get(SceneRunState, SCENE_ID)
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert work_items
    assert all(item["de_template_outcome"]["status"] == "rejected" for item in work_items)
    assert all(item["final"]["row_id"] == item["base"]["row_id"] for item in work_items)
    rejected_row_ids = {
        item["de_template_outcome"]["row_id"] for item in work_items
    }
    assert rejected_row_ids.isdisjoint(offered_row_ids)
    assert all(
        session.get(SceneDraft, row_id).stage == "de_template"
        and session.get(SceneDraft, row_id).status == "rejected"
        for row_id in rejected_row_ids
    )

    chosen_row_id = offered_row_ids[0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-rejected-de-template"},
        ).status_code
        == 200
    )
    style_call_ids_before = list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(
                LlmCall.scene_id == SCENE_ID,
                LlmCall.step.in_(("style_draft", "de_template")),
            )
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    )

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-rejected-de-template"},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["scene_status"] == "archived"
    assert (
        list(
            session.execute(
                select(LlmCall.llm_call_id)
                .where(
                    LlmCall.scene_id == SCENE_ID,
                    LlmCall.step.in_(("style_draft", "de_template")),
                )
                .order_by(LlmCall.llm_call_id)
            ).scalars()
        )
        == style_call_ids_before
    )


def test_resume_rejects_selected_candidate_with_non_style_lineage_stage_without_provider_replay(
    client,
    session,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-invalid-stage"},
        ).status_code
        == 200
    )
    chosen = session.get(SceneDraft, chosen_row_id)
    chosen.stage = "soft_patch"
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-invalid-stage"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


@pytest.mark.parametrize("mutation", ("budget", "basis", "counter"))
def test_selection_resume_validates_budget_checkpoint_before_provider_work(
    client,
    session,
    mutation: str,
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": f"w3-select-budget-{mutation}"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    if mutation == "budget":
        state.scene_token_budget += 1
    elif mutation == "basis":
        state.scene_budget_basis_json = {"tampered": True}
    else:
        state.total_attempt_count = state.attempt_budget + 1
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": f"w3-resume-budget-{mutation}"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_rejects_selected_candidate_whose_durable_source_was_tampered(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]

    selected = client.post(
        f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
        json={},
        headers={"X-Idempotency-Key": "w3-select-tampered"},
    )
    assert selected.status_code == 200
    draft = session.get(SceneDraft, chosen_row_id)
    draft.source_bundle_hash = "sha256:tampered"
    session.commit()

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-tampered"},
    )
    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"


def test_resume_reports_missing_checkpoint_candidate_without_provider_replay(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-missing-candidate"},
        ).status_code
        == 200
    )
    session.delete(session.get(SceneDraft, chosen_row_id))
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-missing-candidate"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_validates_neutral_prefix_before_post_selection_provider_work(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-missing-neutral"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    neutral_row_id = state.run_checkpoint_json["artifact_refs"]["neutral_draft_row_id"]
    session.delete(session.get(SceneDraft, neutral_row_id))
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-missing-neutral"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_validates_hard_qc_report_content_hash_before_provider_work(
    client, session
) -> None:
    _seed_scene(session)
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    chosen_row_id = gate.details_json["candidate_row_ids"][0]
    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-corrupt-hard-report"},
        ).status_code
        == 200
    )
    state = session.get(SceneRunState, SCENE_ID)
    report = session.get(
        QcReport, state.run_checkpoint_json["artifact_refs"]["qc_report_id"]
    )
    report.rewrite_brief_json = [{"instruction": "tampered after checkpoint"}]
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))

    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-corrupt-hard-report"},
    )

    assert resumed.status_code == 409
    assert resumed.json()["error"]["code"] == "RUN_CHECKPOINT_CORRUPT"
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls


def test_resume_uses_contiguous_hashes_when_first_generated_candidate_is_filtered(
    client,
    session,
    monkeypatch,
) -> None:
    from novel_system.services import source_safety

    _seed_scene(session)
    original_scan = source_safety.scan_source_safety

    def _filter_first_style_candidate(
        text: str, *args, **kwargs
    ):  # noqa: ANN002, ANN003, ANN202
        if "draft #2" in text:
            return {"safe": False, "matches": [{"rule": "test-filter"}]}
        return original_scan(text, *args, **kwargs)

    monkeypatch.setattr(
        source_safety, "scan_source_safety", _filter_first_style_candidate
    )
    monkeypatch.setattr(
        Orchestrator,
        "_best_of_n_count",
        staticmethod(lambda contract, criticality=None: 2),
    )
    _make_orchestrator(session).run_scene(SCENE_ID)
    session.commit()
    gate = _selection_gate(session)
    offered = gate.details_json["candidate_row_ids"]
    state = session.get(SceneRunState, SCENE_ID)
    all_candidates = state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]
    assert len(offered) < len(all_candidates)
    assert offered[0] != all_candidates[0]
    chosen_row_id = offered[0]

    assert (
        client.post(
            f"/api/v1/scenes/{SCENE_ID}/style-candidates/{chosen_row_id}/select",
            json={},
            headers={"X-Idempotency-Key": "w3-select-filtered-first"},
        ).status_code
        == 200
    )
    resumed = client.post(
        f"/api/v1/scenes/{SCENE_ID}/resume-after-selection",
        json={},
        headers={"X-Idempotency-Key": "w3-resume-filtered-first"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["scene_status"] == "archived"
