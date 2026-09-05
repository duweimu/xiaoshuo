from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from novel_system.api.routes.scenes import _serialize_generation_summary, _serialize_qc_summary
from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    OperationLog,
    QcReport,
    RelationProfile,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.human_review_manager import HumanReviewManager
from novel_system.services.errors import DomainError
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.llm_task_runner import (
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services import scene_generation as scene_generation_module
from novel_system.services.scene_generation import SceneGenerationService
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.near_final import NearFinalAcceptanceService, NearFinalPlanningService
from novel_system.services.qc_validator import QCValidationError, validate_qc_report
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake


QC_REPORT_ID_RE = re.compile(r"^qc_report_CH100_SC01_\d{8}T\d{12}Z_[0-9a-f]{12}$")


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self, *, satisfied_source: bool = True) -> None:
        self.requests: list[LLMRequest] = []
        self.satisfied_source = satisfied_source

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            scene_text = "Provider-generated neutral scene text."
            if self.satisfied_source:
                scene_text += " A red envelope changes hands."
            payload = {
                "scene_text": scene_text,
                "continuity_notes": ["kept the reunion tense"],
            }
            request_id = "resp_neutral_001"
            model = "fake-neutral-model"
            usage = {"input_tokens": 111, "output_tokens": 29, "total_tokens": 140}
        elif len(self.requests) == 2:
            payload = {
                "scene_text": "Provider-generated style scene text. A red envelope changes hands.",
                "style_notes": ["leaned harder into rhythm and inner tension"],
            }
            request_id = "resp_style_001"
            model = "fake-style-model"
            usage = {"input_tokens": 121, "output_tokens": 33, "total_tokens": 154}
        else:
            payload = {
                "scene_text": "Provider-generated patched scene text. A red envelope changes hands.",
                "style_notes": ["applied one controlled patch pass"],
            }
            request_id = "resp_patch_001"
            model = "fake-patch-model"
            usage = {"input_tokens": 131, "output_tokens": 37, "total_tokens": 168}

        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": model,
                "usage": usage,
                "finish_reason": "stop",
            },
            usage=usage,
            finish_reason="stop",
        )


class FakeFixedSceneClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected scene generation request")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"resp_scene_{len(self.requests):03d}",
            provider="fake-provider",
            model="fake-scene-model",
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": f"resp_scene_{len(self.requests):03d}",
                "model": "fake-scene-model",
                "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            finish_reason="stop",
        )


class FakeSoftQcClient(AccountedGenerateMixin):
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError("unexpected soft_qc request")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"resp_soft_qc_{len(self.requests):03d}",
            provider="fake-provider",
            model="fake-soft-qc-model",
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": f"resp_soft_qc_{len(self.requests):03d}",
                "model": "fake-soft-qc-model",
                "usage": {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 60, "output_tokens": 18, "total_tokens": 78},
            finish_reason="stop",
        )


class FakeQcClient(AccountedGenerateMixin):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            request_id="resp_hard_qc_001",
            provider="fake-provider",
            model="fake-hard-qc-model",
            text=json.dumps(self.payload),
            structured_output=self.payload,
            response_format="json_object",
            raw_response={
                "id": "resp_hard_qc_001",
                "model": "fake-hard-qc-model",
                "usage": {"input_tokens": 77, "output_tokens": 21, "total_tokens": 98},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 77, "output_tokens": 21, "total_tokens": 98},
            finish_reason="stop",
        )


class FakeQcRuntimeFailureClient(AccountedGenerateMixin):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("qc transport timed out before a response was returned")


class FakeAccountedQcRuntimeFailureClient(OnlineAccountedExecution):
    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        error = RuntimeError("qc transport timed out before a response was returned")
        accounting_hook.after_error(
            handle,
            request=request,
            error=error,
            raw_response=None,
            provider_request_id=None,
            latency_ms=7,
        )
        raise error


def _seed_scene(session) -> None:
    session.add(StoryProject(project_id="PROJECT_QC", title="QC", outline_text="QC"))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT_QC",
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id="CH100", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            project_id="PROJECT_QC",
            chapter_id="CH100",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text="A red envelope changes hands.",
            target_length_band="short",
            scene_type="reunion",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id="CH100_SC01", scene_status="ready"))
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


def _make_orchestrator(
    session,
    *,
    hard_qc_payload: dict,
    soft_qc_payloads: list[dict] | None = None,
    scene_client: FakeSceneClient | None = None,
) -> Orchestrator:
    # 假生成退役后，蓝图 / 章节架构 / 角色压力 / 准定稿验收等支撑节点不再有离线
    # 兜底，必须显式注入在线记账替身；场景正文与 QC 仍由各自的 Fake 客户端提供。
    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client or FakeSceneClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=FakeQcClient(hard_qc_payload)),
        soft_qc_engine=SoftQcEngine(
            session,
            llm_client=FakeSoftQcClient(soft_qc_payloads or []),
        ),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)
    return orchestrator


def _allow_legacy_neutral_required_fact_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a pre-validation neutral draft so Hard-QC remains defense in depth."""

    original = scene_generation_module._assess_neutral_draft

    def assess(scene, content):  # noqa: ANN001, ANN202
        result = original(scene, content)
        if set(result.get("reasons") or []) == {"required_facts_missing"}:
            return {**result, "accepted": True, "reasons": []}
        return result

    monkeypatch.setattr(scene_generation_module, "_assess_neutral_draft", assess)


def _base_qc_payload(*, resolution_code: str, next_action: str, issues: list[dict] | None = None) -> dict:
    return {
        "resolution_code": resolution_code,
        "pass_flag": resolution_code == "hard_pass",
        "next_action": next_action,
        "issues": issues or [],
        "rewrite_brief": ["Repair the continuity issue before style generation."] if next_action != "pass" else [],
    }


def _base_soft_qc_payload(
    *,
    resolution_code: str,
    next_action: str,
    issues: list[dict] | None = None,
    rewrite_brief: list[str] | None = None,
    carry_forward_note: bool = False,
    note_scope: str | None = None,
    carry_note_text: str | None = None,
    style_score: float | None = None,
    style_dimensions: list[dict] | None = None,
    style_deviations: list[dict] | None = None,
) -> dict:
    payload = {
        "resolution_code": resolution_code,
        "pass_flag": resolution_code in {"soft_pass", "soft_waive"},
        "next_action": next_action,
        "issues": issues or [],
        "rewrite_brief": rewrite_brief or [],
        "carry_forward_note": carry_forward_note,
        "note_scope": note_scope,
        "carry_note_text": carry_note_text,
    }
    if style_score is not None:
        payload["style_score"] = style_score
    if style_dimensions is not None:
        payload["style_dimensions"] = style_dimensions
    if style_deviations is not None:
        payload["style_deviations"] = style_deviations
    return payload


def test_soft_qc_validator_accepts_patch_and_waive_payloads() -> None:
    patch = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_patch",
            next_action="patch",
            issues=[{"issue_key": "cadence_flat", "message": "The opening needs a stronger pulse."}],
            rewrite_brief=["Tighten the first paragraph.", "Shift the line breaks earlier."],
        ),
    )
    waive = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_waive",
            next_action="pass_with_notes",
            carry_forward_note=True,
            note_scope="chapter_memory",
            carry_note_text="Keep the envelope as a recurring tension motif.",
        ),
    )

    assert patch.resolution_code == "soft_patch"
    assert patch.next_action == "patch"
    assert patch.pass_flag is False
    assert patch.rewrite_brief == ["Tighten the first paragraph.", "Shift the line breaks earlier."]
    assert waive.resolution_code == "soft_waive"
    assert waive.next_action == "pass_with_notes"
    assert waive.pass_flag is True
    assert waive.carry_forward_note is True
    assert waive.note_scope == "chapter_memory"
    assert waive.carry_note_text == "Keep the envelope as a recurring tension motif."


def test_soft_qc_validator_normalizes_string_issues_from_model_payload() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_pass",
            next_action="pass",
            issues=[
                "草稿精准执行了场景目标、强制节拍和结尾钩子。",
            ],
        ),
    )

    assert report.issues[0].issue_key == "local_model_issue"
    assert report.issues[0].message == "草稿精准执行了场景目标、强制节拍和结尾钩子。"


def test_soft_qc_validator_normalizes_dict_issues_from_model_payload() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_pass",
            next_action="pass",
            issues={
                "style_adherence": 0.95,
                "summary": {"message": "Draft is ready to archive."},
            },
        ),
    )

    assert report.issues[0].issue_key == "style_adherence"
    assert report.issues[0].message == "0.95"
    assert report.issues[1].issue_key == "summary"
    assert report.issues[1].message == "Draft is ready to archive."


def test_soft_qc_validator_accepts_style_score_contract_and_rejects_out_of_range() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_patch",
            next_action="patch",
            issues=[{"issue_key": "style_profile_drift", "message": "Dialogue ratio is too high."}],
            rewrite_brief=["Reduce dialogue and restore interior pressure."],
            style_score=0.62,
            style_dimensions=[
                {
                    "name": "rhythm",
                    "score": 0.7,
                    "evidence": "Several paragraph endings carry pressure.",
                },
                {
                    "name": "dialogue_ratio",
                    "score": 0.45,
                    "evidence": "Dialogue crowds out the requested interior distance.",
                },
            ],
            style_deviations=[
                {
                    "dimension": "dialogue_ratio",
                    "severity": "medium",
                    "patch_brief": "Cut two spoken lines and move one beat into narration.",
                }
            ],
        ),
    )

    assert report.style_score == 0.62
    assert report.style_dimensions[0].name == "rhythm"
    assert report.style_dimensions[1].score == 0.45
    assert report.style_deviations[0].patch_brief == "Cut two spoken lines and move one beat into narration."

    with pytest.raises((QCValidationError, ValidationError)):
        validate_qc_report(
            "soft_qc",
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
                style_score=1.2,
                style_dimensions=[{"name": "rhythm", "score": 1.3, "evidence": "too high"}],
            ),
        )


def test_soft_qc_validator_maps_style_scores_alias_from_model_payload() -> None:
    payload = _base_soft_qc_payload(
        resolution_code="soft_pass",
        next_action="pass",
    )
    payload["style_scores"] = {
        "rhythm": 0.9,
        "syntax": 0.8,
        "imagery": 1.0,
    }

    report = validate_qc_report("soft_qc", payload)

    assert round(report.style_score or 0, 4) == 0.9
    assert [item.name for item in report.style_dimensions] == ["rhythm", "syntax", "imagery"]
    assert report.style_dimensions[0].score == 0.9


def test_soft_qc_validator_drops_unknown_diagnostic_fields_from_model_payload() -> None:
    payload = _base_soft_qc_payload(
        resolution_code="soft_pass",
        next_action="pass",
    )
    payload["overall_comment"] = "Model-side diagnostic note."

    report = validate_qc_report("soft_qc", payload)

    assert report.resolution_code == "soft_pass"
    assert not hasattr(report, "overall_comment")


def test_soft_qc_validator_derives_waive_note_when_model_omits_it() -> None:
    report = validate_qc_report(
        "soft_qc",
        _base_soft_qc_payload(
            resolution_code="soft_waive",
            next_action="pass_with_notes",
            issues=["整体通过，但保留场景记忆提示。"],
            carry_forward_note=False,
        ),
    )

    assert report.carry_forward_note is True
    assert report.note_scope == "scene_memory"
    assert report.carry_note_text == "整体通过，但保留场景记忆提示。"


def test_build_qc_report_id_uses_sortable_timestamp_prefix() -> None:
    from novel_system.services import qc_engine as qc_engine_module

    first = qc_engine_module._build_qc_report_id(
        "CH100_SC01",
        timestamp="20260531T130000000000Z",
        random_hex="ffffffffffff",
    )
    second = qc_engine_module._build_qc_report_id(
        "CH100_SC01",
        timestamp="20260531T130000000001Z",
        random_hex="000000000000",
    )

    assert QC_REPORT_ID_RE.match(first)
    assert QC_REPORT_ID_RE.match(second)
    assert first < second


def test_run_scene_hard_qc_pass_persists_report_and_continues(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    reports = session.execute(select(QcReport).order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    hard_report = next(report for report in reports if report.qc_type == "hard_qc")
    soft_report = next(report for report in reports if report.qc_type == "soft_qc")
    style_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert result["soft_qc"]["branch"] == "continue"
    assert QC_REPORT_ID_RE.match(result["hard_qc"]["qc_report_id"])
    assert QC_REPORT_ID_RE.match(result["soft_qc"]["qc_report_id"])
    assert hard_report.qc_type == "hard_qc"
    assert hard_report.source_draft_row_id == state.current_neutral_draft_row_id
    assert hard_report.source_bundle_id == state.current_bundle_id
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.pass_flag == 1
    assert hard_report.next_action == "pass"
    assert soft_report.qc_type == "soft_qc"
    assert soft_report.source_draft_row_id == style_draft.row_id
    assert soft_report.resolution_code == "soft_pass"
    assert soft_report.pass_flag == 1
    assert soft_report.next_action == "pass"
    assert state.current_qc_report_id == soft_report.qc_report_id
    assert state.current_human_review_event_id is None
    assert style_draft.content == "Provider-generated style scene text. A red envelope changes hands."
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id
    assert state.current_style_draft_row_id == style_draft.row_id
    assert state.current_final_scene_row_id == final_scene.row_id
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "finalize"}] == [
        "style_draft",
        "soft_qc",
        "finalize",
    ]
    finalize_attempt = next(attempt for attempt in attempts if attempt.step == "finalize")
    assert finalize_attempt.details_json["source_style_draft_row_id"] == style_draft.row_id
    assert finalize_attempt.details_json["source_qc_report_id"] == soft_report.qc_report_id


def test_run_scene_soft_qc_waive_preserves_carry_note_details_and_finalizes(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_waive",
                next_action="pass_with_notes",
                carry_forward_note=True,
                note_scope="chapter_memory",
                carry_note_text="Keep the envelope motif in future callbacks.",
            )
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    style_draft = session.execute(select(SceneDraft).where(SceneDraft.stage == "style_draft")).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    report = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc")).scalars().one()
    scene_memory = session.execute(select(SceneMemory).where(SceneMemory.scene_id == "CH100_SC01")).scalars().one()

    assert result["soft_qc"]["branch"] == "waive"
    assert report.resolution_code == "soft_waive"
    assert report.next_action == "pass_with_notes"
    assert report.pass_flag == 1
    assert report.rewrite_brief_json == [
        {
            "kind": "carry_forward_note",
            "note_scope": "chapter_memory",
            "carry_note_text": "Keep the envelope motif in future callbacks.",
        }
    ]
    assert scene_memory.carry_notes_json == [
        {
            "kind": "carry_forward_note",
            "note_scope": "chapter_memory",
            "carry_note_text": "Keep the envelope motif in future callbacks.",
        }
    ]
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id


def test_run_scene_soft_qc_patch_rechecks_before_finalize(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph.", "Move the red envelope beat earlier."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    drafts = session.execute(select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())).scalars().all()
    style_draft = next(draft for draft in drafts if draft.stage == "style_draft")
    patch_draft = next(draft for draft in drafts if draft.stage == "style_patch")
    final_scene = session.execute(select(FinalScene)).scalars().one()
    reports = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc").order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()
    state = session.get(SceneRunState, "CH100_SC01")

    assert result["soft_qc"]["branch"] == "continue"
    assert len(reports) == 2
    assert reports[0].next_action == "patch"
    assert reports[1].next_action == "pass"
    assert patch_draft.content == "Provider-generated patched scene text. A red envelope changes hands."
    assert patch_draft.content != style_draft.content
    assert final_scene.content == patch_draft.content
    assert final_scene.generation_llm_call_id == patch_draft.generation_llm_call_id
    assert state.current_style_draft_row_id == patch_draft.row_id
    assert state.current_final_scene_row_id == final_scene.row_id
    assert state.soft_patch_count == 1
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "soft_patch", "finalize"}] == [
        "style_draft",
        "soft_qc",
        "soft_patch",
        "soft_qc",
        "finalize",
    ]
    patch_attempt = next(attempt for attempt in attempts if attempt.step == "soft_patch")
    assert patch_attempt.details_json["source_qc_report_id"] == reports[0].qc_report_id
    assert patch_attempt.details_json["source_style_draft_row_id"] == style_draft.row_id


def test_run_scene_soft_qc_patch_repeat_waives_with_carry_note(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening still feels flat."}],
                rewrite_brief=["Add sharper contrast in the first beats."],
            ),
        ],
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    final_scene = session.execute(select(FinalScene)).scalars().one()
    events = session.execute(select(HumanReviewEvent)).scalars().all()
    reports = session.execute(select(QcReport).where(QcReport.qc_type == "soft_qc").order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())).scalars().all()
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()

    assert result["scene_status"] == "archived"
    assert result["soft_qc"]["branch"] == "waive"
    assert state.current_final_scene_row_id == final_scene.row_id
    assert events == []
    assert reports[-1].resolution_code == "soft_waive"
    assert reports[-1].next_action == "pass_with_notes"
    assert reports[-1].pass_flag == 1
    assert any(entry.get("kind") == "carry_forward_note" for entry in reports[-1].rewrite_brief_json)
    assert [attempt.step for attempt in attempts if attempt.step in {"style_draft", "soft_qc", "soft_patch"}] == [
        "style_draft",
        "soft_qc",
        "soft_patch",
        "soft_qc",
    ]


def test_run_scene_blocks_hard_qc_when_character_pronoun_drifts(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.pov_character_id = "LIN_CEN"
    scene.onstage_chars_json = ["LIN_CEN"]
    scene.must_include_text = ""
    voice = session.get(VoiceProfile, "voice_profile_VOICE_CHAR_A_v1")
    voice.voice_profile_id = "VOICE_LIN_CEN"
    voice.character_id = "LIN_CEN"
    voice.content = "角色名：林岑\n代词：她\n角色职责：档案修复师"
    session.commit()

    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        scene_client=FakeFixedSceneClient(
            [
                {
                    "scene_text": "林岑把盐钟残片放在灯下。他确认刻痕被人改过，声音仍然很稳。",
                    "continuity_notes": ["provider missed pronoun contract"],
                }
            ]
        ),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()

    assert result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert result["hard_qc"]["branch"] == "rewrite_partial"
    assert state.current_final_scene_row_id is None
    assert report.resolution_code == "hard_fail_partial"
    assert report.next_action == "partial_rewrite"
    assert report.issues_json[0]["issue_key"] == "character_pronoun_drift"
    assert "林岑" in report.rewrite_brief_json[0]["instruction"]


def test_run_scene_ignores_unsubstantiated_unknown_pronoun_hard_qc(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.pov_character_id = "LIN_CEN"
    scene.onstage_chars_json = ["LIN_CEN", "许望", "幸存者阿砚"]
    scene.must_include_text = ""
    voice = session.get(VoiceProfile, "voice_profile_VOICE_CHAR_A_v1")
    voice.voice_profile_id = "VOICE_LIN_CEN"
    voice.character_id = "LIN_CEN"
    voice.content = "角色名：林岑\n代词：她\n角色职责：档案修复师"
    neutral_text = (
        "林岑把残片插入档案柜。许望站在她身后，记录潮声倒退的三秒。"
        "她按下播放键，听见幸存者阿砚的呼吸，然后把证据拆成两份。"
    )
    session.commit()

    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "character_pronoun_ambiguity",
                    "message": "许望的代词未明确指定，可能导致角色身份混淆。",
                },
                {
                    "issue_key": "character_role_inconsistency",
                    "message": "幸存者阿砚的角色职责未在场景中体现，需补充其存在感或行动线索。",
                },
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeFixedSceneClient(
            [
                {"scene_text": neutral_text, "continuity_notes": []},
                {"scene_text": neutral_text, "style_notes": []},
            ]
        ),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert state.current_final_scene_row_id is not None
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.issues_json == []


def _seed_pronoun_drift_setup(session) -> None:
    """Wave 2：软 QC 阻断需要确定性 Q1 证据——用「代词契约 她 / 文本 他」的
    确定性漂移贯穿 style 与 patch 稿（neutral 用 她，硬 QC 不拦）。"""
    scene = session.get(SceneCard, "CH100_SC01")
    scene.pov_character_id = "LIN_CEN"
    scene.onstage_chars_json = ["LIN_CEN"]
    scene.must_include_text = ""
    voice = session.get(VoiceProfile, "voice_profile_VOICE_CHAR_A_v1")
    voice.voice_profile_id = "VOICE_LIN_CEN"
    voice.character_id = "LIN_CEN"
    voice.content = "角色名：林岑\n代词：她\n角色职责：档案修复师"
    session.commit()


# 前缀保持 "Provider-generated "：near-final 的占位稿判定跳过内容 gate，
# 阻断证据只来自确定性代词漂移本身。
_DRIFT_SCENE_PAYLOADS = [
    {"scene_text": "Provider-generated 林岑站在灯下，她把盐钟残片放好。", "continuity_notes": []},
    {"scene_text": "Provider-generated 林岑站在灯下。他把刻痕对准光。", "style_notes": []},
    {"scene_text": "Provider-generated 林岑收起残片。他仍不说话。", "style_notes": []},
]


def test_run_scene_does_not_waive_blocking_soft_qc_repeat_patch(session) -> None:
    """Wave 2 语义：重复补丁后仍存在 verified Q1（确定性代词漂移）→ 阻断不豁免。"""
    _seed_scene(session)
    _seed_pronoun_drift_setup(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "cadence_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "cadence_flat", "message": "The rhythm is still too even."}],
                rewrite_brief=["修正节奏，必要时重复角色姓名。"],
            ),
        ],
        scene_client=FakeFixedSceneClient(list(_DRIFT_SCENE_PAYLOADS)),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    events = session.execute(select(HumanReviewEvent)).scalars().all()
    qc_report = session.get(QcReport, result["soft_qc"]["qc_report_id"])

    assert result["scene_status"] == "human_review_required"
    assert result["soft_qc"]["branch"] == "human_review_required"
    assert result["soft_qc"]["stop_reason"] == "blocking_soft_qc_issue"
    assert QC_REPORT_ID_RE.match(result["soft_qc"]["qc_report_id"])
    assert state.current_final_scene_row_id is None
    assert state.current_qc_report_id == result["soft_qc"]["qc_report_id"]
    assert len(events) == 1
    assert qc_report is not None
    assert qc_report.resolution_code == "soft_block_human"
    assert qc_report.next_action == "human_review_required"
    drift_issue = next(issue for issue in qc_report.issues_json if issue["issue_key"] == "character_pronoun_drift")
    assert drift_issue["quality_level"] == "Q1"
    assert drift_issue["blocking"] is True
    assert drift_issue["verified_by"]


def test_run_scene_hard_qc_rewrite_branch_updates_counters_and_stops_before_style_generation(
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            # Wave 2：阻断需 verified Q1——must_include 确实缺失（确定性复核成立）
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    report = session.execute(select(QcReport)).scalars().one()

    assert result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert result["hard_qc"]["branch"] == "rewrite_partial"
    assert report.resolution_code == "hard_fail_partial"
    assert report.issues_json[0]["quality_level"] == "Q1"
    assert report.issues_json[0]["verified_by"] == "scene_card_required_text"
    assert state.hard_partial_rewrite_count == 1
    assert state.hard_full_rewrite_count == 0
    assert state.repeat_issue_key == "missing_required_text"
    assert state.repeat_issue_count == 1
    assert state.current_final_scene_row_id is None
    assert session.execute(select(SceneDraft).where(SceneDraft.stage == "style_draft")).scalars().all() == []
    assert session.execute(select(FinalScene)).scalars().all() == []


def test_hard_qc_report_adds_evidence_and_constraint_conflict_metadata(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.hook = "以死亡证明作为雨夜钩子。"
    scene.must_include_text = "死亡证明必须出现在开场。"
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="雨水打湿死亡证明，灯光忽然熄灭。",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[
                    {
                        "issue_key": "unsafe_concrete_term",
                        "message": "Replace 死亡证明 with a neutral clue.",
                    }
                ],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="雨水打湿死亡证明，灯光忽然熄灭。",
    )
    session.commit()

    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]
    rewrite = report.rewrite_brief_json[0]

    assert decision.branch == "human_review_required"
    assert issue["severity"] == "high"
    assert issue["human_readable_reason"]
    assert issue["evidence_spans"][0]["text"] == "死亡证明"
    assert issue["conflicts_with"][0]["constraint_source"] == "scene_card.hook"
    assert issue["conflicts_with"][0]["term"] == "死亡证明"
    assert rewrite["constraint_source"] == "hard_qc"
    assert rewrite["conflicts_with"][0]["constraint_source"] == "scene_card.hook"


def test_hard_qc_required_term_evidence_does_not_force_human_review(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.must_include_text = "证人"
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="证人站在门边，主角做出了决定。",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[
                    {
                        "issue_key": "missing_relation_digest_argument",
                        "message": "Add the evidence-vs-speed argument while protecting the 证人.",
                    }
                ],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="证人站在门边，主角做出了决定。",
    )
    session.commit()

    report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    issue = report.issues_json[0]

    # Wave 2：must_include 已满足 → LLM 意见无确定性佐证 → 降 Q2 继续（不重写也不升审）
    assert decision.branch == "continue"
    assert report.next_action == "pass"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["evidence_spans"][0]["text"] == "证人"
    assert issue["conflicts_with"] == []


def test_run_scene_repeated_hard_qc_rewrite_escalates_to_human_review(
    session,
    monkeypatch,
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    first = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    first_result = first.run_scene("CH100_SC01")
    session.commit()

    second = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接仍未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    second_result = second.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    event = session.execute(select(HumanReviewEvent)).scalars().one()

    assert first_result["scene_status"] == "hard_qc_partial_rewrite_required"
    assert second_result["scene_status"] == "human_review_required"
    assert second_result["hard_qc"]["branch"] == "human_review_required"
    assert second_result["hard_qc"]["stop_reason"] == "repeat_issue_key_limit"
    assert state.repeat_issue_key == "missing_required_text"
    assert state.repeat_issue_count == 2
    assert state.hard_partial_rewrite_count == 2
    assert state.current_human_review_event_id == event.event_id
    assert state.current_final_scene_row_id is None


def test_run_scene_ignores_hard_qc_forbidden_false_positive_when_required_text_is_present(session) -> None:
    _seed_scene(session)
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "forbidden_text",
                    "message": "Remove the forbidden text 'A red envelope changes hands.' from the draft.",
                }
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeSceneClient(satisfied_source=True),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.pass_flag == 1
    assert hard_report.issues_json == []
    assert final_scene.content


def test_run_scene_ignores_hard_qc_hook_and_style_false_positives_when_source_is_satisfied(session) -> None:
    _seed_scene(session)
    scene = session.get(SceneCard, "CH100_SC01")
    scene.hook = "red envelope changes hands"
    session.commit()
    orchestrator = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[
                {
                    "issue_key": "unsupported_event",
                    "message": "The red envelope changes hands hook is unsupported by the bundle.",
                },
                {
                    "issue_key": "style_compliance",
                    "message": "The prose should be handled by soft QC instead.",
                },
            ],
        ),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
        scene_client=FakeSceneClient(satisfied_source=True),
    )

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    hard_report = session.execute(select(QcReport).where(QcReport.qc_type == "hard_qc")).scalars().one()

    assert result["scene_status"] == "archived"
    assert result["hard_qc"]["branch"] == "continue"
    assert hard_report.resolution_code == "hard_pass"
    assert hard_report.issues_json == []


def test_hard_qc_engine_escalates_repeated_issue_key_to_human_review(session) -> None:
    _seed_scene(session)
    state = session.get(SceneRunState, "CH100_SC01")
    state.repeat_issue_key = "same_issue"
    state.repeat_issue_count = 1
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient(
            _base_qc_payload(
                resolution_code="hard_fail_partial",
                next_action="partial_rewrite",
                issues=[{"issue_key": "same_issue", "message": "The same blocker appeared again."}],
            )
        ),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    event = session.execute(select(HumanReviewEvent)).scalars().one()

    assert decision.branch == "human_review_required"
    assert state.repeat_issue_key == "same_issue"
    assert state.repeat_issue_count == 2
    assert state.current_human_review_event_id == event.event_id
    assert event.event_source == "scene_generation"
    assert event.status == "needs_followup"
    assert event.details_json["trigger_reason"] == "repeat_issue_key_limit"
    assert event.details_json["recommended_action"] == "human_review_required"


def test_hard_qc_engine_sends_structured_response_schema(session) -> None:
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    hard_client = FakeQcClient(_base_qc_payload(resolution_code="hard_pass", next_action="pass"))
    engine = HardQcEngine(session, llm_client=hard_client)

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    request = hard_client.requests[0]
    assert decision.branch == "continue"
    assert request.response_schema["name"] == "hard_qc"
    assert request.response_schema["schema"]["required"] == [
        "resolution_code",
        "pass_flag",
        "next_action",
        "issues",
        "rewrite_brief",
    ]
    assert (
        "Required top-level JSON keys: resolution_code, pass_flag, next_action, issues, rewrite_brief"
        in request.messages[1]["content"]
    )


def test_hard_qc_engine_degrades_malformed_payload_to_continue_with_warning(session) -> None:
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    engine = HardQcEngine(
        session,
        llm_client=FakeQcClient({"passed": False, "issues": [{"severity": "hard", "message": "bad shape"}]}),
    )

    decision = engine.evaluate(
        scene_id="CH100_SC01",
        bundle={
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
        },
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Neutral draft under review.",
    )
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "hard_qc")).scalars().one()
    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "hard_qc")).scalars().one()

    # Wave 2（§5.4/§7.7）：payload 无效 = QC 自身失败——降级续跑 + Q2 警告，不再断头
    assert decision.branch == "continue"
    assert decision.should_continue is True
    assert decision.stop_reason == "invalid_hard_qc_payload"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code is None
    assert report.resolution_code == "hard_pass"
    assert report.next_action == "pass"
    assert report.pass_flag == 1
    warning = next(issue for issue in report.issues_json if issue["issue_key"] == "invalid_hard_qc_payload")
    assert warning["quality_level"] == "Q2"
    assert warning["blocking"] is False
    assert "validation failed" in warning["message"]
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_run_scene_clears_stale_pointers_across_blocked_and_successful_reruns(
    client, session, monkeypatch
) -> None:
    _seed_scene(session)
    _allow_legacy_neutral_required_fact_gap(monkeypatch)
    state = session.get(SceneRunState, "CH100_SC01")
    state.current_style_draft_row_id = "draft_style_old"
    state.current_final_scene_row_id = "final_scene_old"
    state.current_human_review_event_id = "human_review_old"
    session.commit()

    blocked = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(
            resolution_code="hard_fail_partial",
            next_action="partial_rewrite",
            issues=[{"issue_key": "missing_required_text", "message": "缺少必备元素：红包交接未在正文出现"}],
        ),
        scene_client=FakeSceneClient(satisfied_source=False),
    )

    blocked_result = blocked.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    blocked_event_id = state.current_human_review_event_id
    assert blocked_result["current_final_scene_row_id"] is None
    assert state.current_style_draft_row_id is None
    assert state.current_final_scene_row_id is None
    assert blocked_event_id is None

    state.current_human_review_event_id = "human_review_stale_from_previous_block"
    state.total_attempt_count = state.attempt_budget
    attempts_before_rerun = state.total_attempt_count
    session.commit()

    topup = client.post(
        "/api/v1/scenes/CH100_SC01/budget/topup",
        headers={"X-Idempotency-Key": "qc-stale-pointer-rerun-attempt-topup"},
        json={
            "extra_attempts": 10,
            "reason": "exercise the successful rerun after an exhausted lifecycle budget",
        },
    )
    assert topup.status_code == 200, topup.text
    session.expire_all()

    rerun = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")],
    )

    rerun_result = rerun.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    assert rerun_result["scene_status"] == "archived"
    assert rerun_result["current_final_scene_row_id"] == state.current_final_scene_row_id
    assert rerun_result["current_human_review_event_id"] is None
    assert state.current_style_draft_row_id.startswith("draft_style_CH100_SC01_v")
    assert state.current_final_scene_row_id.startswith("final_scene_CH100_SC01_v")
    assert state.current_human_review_event_id is None
    assert state.total_attempt_count == attempts_before_rerun + 1


def test_run_scene_resets_soft_patch_state_between_reruns(session) -> None:
    _seed_scene(session)
    first = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening needs more immediacy."}],
                rewrite_brief=["Tighten the first paragraph.", "Move the red envelope beat earlier."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    first_result = first.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    first_neutral_row_id = state.current_neutral_draft_row_id
    first_qc_report_id = state.current_qc_report_id

    rerun = _make_orchestrator(
        session,
        hard_qc_payload=_base_qc_payload(resolution_code="hard_pass", next_action="pass"),
        soft_qc_payloads=[
            _base_soft_qc_payload(
                resolution_code="soft_patch",
                next_action="patch",
                issues=[{"issue_key": "opening_flat", "message": "The opening still needs work."}],
                rewrite_brief=["Tighten the first paragraph again."],
            ),
            _base_soft_qc_payload(
                resolution_code="soft_pass",
                next_action="pass",
            ),
        ],
    )

    rerun_result = rerun.run_scene("CH100_SC01")
    session.commit()

    state = session.get(SceneRunState, "CH100_SC01")
    attempts = session.execute(select(AttemptTracker).order_by(AttemptTracker.attempt_id.asc())).scalars().all()
    human_reviews = session.execute(select(HumanReviewEvent)).scalars().all()

    assert first_result["scene_status"] == "archived"
    assert rerun_result["scene_status"] == "archived"
    assert state.soft_patch_count == 1
    assert state.current_neutral_draft_row_id != first_neutral_row_id
    assert session.get(SceneDraft, first_neutral_row_id) is not None
    assert session.get(SceneDraft, state.current_neutral_draft_row_id) is not None
    assert state.current_qc_report_id != first_qc_report_id
    assert state.current_human_review_event_id is None
    assert len([attempt for attempt in attempts if attempt.step == "neutral_draft"]) == 2
    assert len([attempt for attempt in attempts if attempt.step == "soft_patch"]) == 2
    assert len([attempt for attempt in attempts if attempt.step == "finalize"]) == 2
    assert human_reviews == []


class _RaisingQcRunner:
    def __init__(self, error: LLMNodeExecutionError) -> None:
        self.error = error

    def run(self, **_kwargs):  # noqa: ANN003, ANN201
        raise self.error


def _seed_qc_ledger_case(session, *, qc_type: str) -> tuple[dict, str, str]:
    _seed_scene(session)
    draft_row_id = f"draft_{qc_type}_CH100_SC01"
    session.add(
        SceneDraft(
            row_id=draft_row_id,
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft" if qc_type == "hard_qc" else "style_draft",
            content="Draft under ledger review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-qc-ledger"
    state.run_execution_status = "active"
    state.run_checkpoint = "neutral_ready" if qc_type == "hard_qc" else "style_ready"
    state.run_checkpoint_json = {
        "execution_id": "exec-qc-ledger",
        "node_key": state.run_checkpoint,
        "artifact_refs": {},
        "artifact_hashes": {},
    }
    session.commit()
    return (
        {
            "bundle_id": "bundle_CH100_SC01",
            "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
            "snapshot": {
                "scene_id": "CH100_SC01",
                "chapter_id": "CH100",
                "inline_digests": {"scene_card": "Goal"},
            },
        },
        draft_row_id,
        f"{qc_type}:0",
    )


def _qc_execution_error(*, call_id: str, error_code: str) -> LLMNodeExecutionError:
    return LLMNodeExecutionError(
        llm_call_id=call_id,
        error_code=error_code,
        message=f"{error_code} from injected QC runner",
        request_summary={},
        response_summary={"error_code": error_code},
    )


def _qc_continuity_error(*, call_id: str) -> LLMNodeContinuityError:
    warning = {
        "code": "continuity_budget_exceeded",
        "message": "Prompt still exceeds the safe input budget after deterministic continuity compaction.",
        "recommended_action": "split_scene",
        "requires_scene_split": True,
    }
    return LLMNodeContinuityError(
        llm_call_id=call_id,
        request_summary={},
        response_summary={"error_code": "CONTINUITY_BUDGET_EXCEEDED"},
        continuity_warning=warning,
    )


def _add_qc_failure_parent(
    session,
    *,
    qc_type: str,
    call_id: str,
    error_code: str,
    accounting_status: str = "failed",
    dispatched: bool = True,
    scene_id: str = "CH100_SC01",
    execution_id: str = "exec-qc-ledger",
    execution_step_key: str | None = None,
) -> LlmCall:
    parent = LlmCall(
        llm_call_id=call_id,
        provider="fake-provider",
        model="fake-model",
        node_id=qc_type,
        step=qc_type,
        scene_id=scene_id,
        chapter_id="CH100",
        scope_type="scene",
        scope_id=scene_id,
        execution_id=execution_id,
        execution_step_key=execution_step_key or f"{qc_type}:0",
        estimated_tokens=0,
        reserved_tokens=0,
        budget_charged_tokens=0,
        usage_is_estimate=True,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_ms=0,
        accounting_status=accounting_status,
        error_code=error_code,
        request_dispatched_at="2026-07-14T00:00:00+00:00" if dispatched else None,
        settled_at="2026-07-14T00:00:01+00:00",
    )
    session.add(parent)
    session.commit()
    return parent


def _add_valid_qc_provider_failure_ledger(
    session,
    *,
    qc_type: str,
    call_id: str,
    error_code: str = "LLM_HTTP_REQUEST_FAILED",
) -> tuple[LlmCall, LlmCallAttempt]:
    parent = _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
        dispatched=True,
    )
    child = LlmCallAttempt(
        attempt_id=f"attempt-{call_id}",
        llm_call_id=call_id,
        provider_attempt_no=0,
        dispatch_kind="initial",
        request_max_output_tokens=32,
        prompt_tokens=12,
        completion_tokens=8,
        total_tokens=20,
        estimated_tokens=24,
        reserved_tokens=24,
        budget_charged_tokens=20,
        accounting_status="failed",
        request_dispatched_at="2026-07-14T00:00:00+00:00",
        settled_at="2026-07-14T00:00:01+00:00",
        latency_ms=7,
        error_code=error_code,
        error_text="provider failed",
    )
    session.add(child)
    parent.estimated_tokens = child.estimated_tokens
    parent.reserved_tokens = child.reserved_tokens
    parent.budget_charged_tokens = child.budget_charged_tokens
    parent.prompt_tokens = child.prompt_tokens
    parent.completion_tokens = child.completion_tokens
    parent.total_tokens = child.total_tokens
    parent.latency_ms = child.latency_ms
    session.commit()
    return parent, child


def _evaluate_raising_qc(
    session,
    *,
    qc_type: str,
    error: LLMNodeExecutionError,
    bundle: dict,
    draft_row_id: str,
    execution_step_key: str,
):
    engine_cls = HardQcEngine if qc_type == "hard_qc" else SoftQcEngine
    engine = engine_cls(session, llm_runner=_RaisingQcRunner(error))
    if qc_type == "hard_qc":
        return engine.evaluate(
            scene_id="CH100_SC01",
            bundle=bundle,
            neutral_draft_row_id=draft_row_id,
            neutral_content="Draft under ledger review.",
            execution_step_key=execution_step_key,
        )
    return engine.evaluate(
        scene_id="CH100_SC01",
        bundle=bundle,
        source_draft_row_id=draft_row_id,
        source_draft_content="Draft under ledger review.",
        execution_step_key=execution_step_key,
    )


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_continuity_budget_rejection_degrades_only_from_exact_undispatched_ledger(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-continuity"
    _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code="CONTINUITY_BUDGET_EXCEEDED",
        accounting_status="rejected",
        dispatched=False,
    )
    error = _qc_continuity_error(call_id=call_id)

    decision = _evaluate_raising_qc(
        session,
        qc_type=qc_type,
        error=error,
        bundle=bundle,
        draft_row_id=draft_row_id,
        execution_step_key=step_key,
    )

    assert decision.branch == ("continue" if qc_type == "hard_qc" else "waive")
    assert decision.should_continue is True
    assert decision.stop_reason == f"{qc_type}_continuity_budget_exceeded"
    assert decision.llm_call_id == call_id
    report = session.execute(select(QcReport)).scalar_one()
    issue = report.issues_json[0]
    assert issue["issue_key"] == "continuity_budget_exceeded"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["continuity_warning"] == error.continuity_warning
    assert session.execute(select(LlmCallAttempt)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    "tamper",
    [
        "missing_parent",
        "scene",
        "chapter",
        "execution",
        "step",
        "node",
        "execution_step_key",
        "error_code",
        "status",
        "parent_dispatched",
        "parent_settled_missing",
        "estimated_nonzero",
        "reserved_nonzero",
        "charged_nonzero",
        "prompt_nonzero",
        "completion_nonzero",
        "total_nonzero",
        "latency_nonzero",
        "usage_false",
        "child_undispatched",
        "child_dispatched",
    ],
)
def test_qc_continuity_budget_ledger_tampering_rethrows_without_side_effects(
    session,
    qc_type: str,
    tamper: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-continuity-{tamper}"
    if tamper != "missing_parent":
        parent = _add_qc_failure_parent(
            session,
            qc_type=qc_type,
            call_id=call_id,
            error_code="CONTINUITY_BUDGET_EXCEEDED",
            accounting_status="rejected",
            dispatched=False,
        )
        if tamper == "scene":
            parent.scene_id = "OTHER_SCENE"
        elif tamper == "chapter":
            parent.chapter_id = "OTHER_CHAPTER"
        elif tamper == "execution":
            parent.execution_id = "other-execution"
        elif tamper == "step":
            parent.step = "other-step"
        elif tamper == "node":
            parent.node_id = "other-node"
        elif tamper == "execution_step_key":
            parent.execution_step_key = "other-step-key"
        elif tamper == "error_code":
            parent.error_code = "LLM_HTTP_REQUEST_FAILED"
        elif tamper == "status":
            parent.accounting_status = "failed"
        elif tamper == "parent_dispatched":
            parent.request_dispatched_at = "2026-07-14T00:00:00+00:00"
        elif tamper == "parent_settled_missing":
            parent.settled_at = None
        elif tamper == "estimated_nonzero":
            parent.estimated_tokens = 1
        elif tamper == "reserved_nonzero":
            parent.reserved_tokens = 1
        elif tamper == "charged_nonzero":
            parent.reserved_tokens = 1
            parent.budget_charged_tokens = 1
        elif tamper == "prompt_nonzero":
            parent.prompt_tokens = 1
        elif tamper == "completion_nonzero":
            parent.completion_tokens = 1
        elif tamper == "total_nonzero":
            parent.total_tokens = 1
        elif tamper == "latency_nonzero":
            parent.latency_ms = 1
        elif tamper == "usage_false":
            parent.usage_is_estimate = False
        elif tamper in {"child_undispatched", "child_dispatched"}:
            session.add(
                LlmCallAttempt(
                    attempt_id=f"attempt-{call_id}",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    accounting_status="rejected",
                    request_dispatched_at=(
                        "2026-07-14T00:00:00+00:00" if tamper == "child_dispatched" else None
                    ),
                    settled_at="2026-07-14T00:00:01+00:00",
                    error_code="CONTINUITY_BUDGET_EXCEEDED",
                )
            )
        session.commit()
    error = _qc_continuity_error(call_id=call_id)

    with pytest.raises(LLMNodeContinuityError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []
    assert session.get(SceneRunState, "CH100_SC01").current_qc_report_id is None


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    ("error_code", "ledger_mode"),
    [
        ("RUN_OWNER_LEASE_LOST", "none"),
        ("RUN_CHECKPOINT_OUTPUT_MISSING", "dispatched"),
        ("LLM_ACCOUNTING_HOOK_UNSUPPORTED", "rejected"),
        ("LLM_USAGE_EXCEEDS_RESERVATION", "overage"),
        ("LLM_HTTP_REQUEST_FAILED", "none"),
    ],
)
def test_qc_control_plane_or_unproven_failures_rethrow_without_side_effects(
    session,
    qc_type: str,
    error_code: str,
    ledger_mode: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-{ledger_mode}"
    if ledger_mode != "none":
        _add_qc_failure_parent(
            session,
            qc_type=qc_type,
            call_id=call_id,
            error_code=error_code,
            accounting_status=(
                "rejected"
                if ledger_mode == "rejected"
                else "usage_exceeds_reservation"
                if ledger_mode == "overage"
                else "failed"
            ),
            dispatched=ledger_mode != "rejected",
        )
    error = _qc_execution_error(call_id=call_id, error_code=error_code)
    state = session.get(SceneRunState, "CH100_SC01")
    checkpoint_before = (state.run_checkpoint, dict(state.run_checkpoint_json or {}))

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []
    state = session.get(SceneRunState, "CH100_SC01")
    assert (state.run_checkpoint, dict(state.run_checkpoint_json or {})) == checkpoint_before
    assert state.current_qc_report_id is None


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize("mismatch", ["scene", "execution", "step", "error_code"])
def test_qc_failure_ledger_binding_mismatch_never_degrades(
    session,
    qc_type: str,
    mismatch: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-wrong-{mismatch}"
    exception_code = "LLM_HTTP_REQUEST_FAILED"
    _add_qc_failure_parent(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code="LLM_TIMEOUT" if mismatch == "error_code" else exception_code,
        scene_id="OTHER_SCENE" if mismatch == "scene" else "CH100_SC01",
        execution_id="other-execution" if mismatch == "execution" else "exec-qc-ledger",
        execution_step_key="other-step" if mismatch == "step" else step_key,
    )
    error = _qc_execution_error(call_id=call_id, error_code=exception_code)

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_true_dispatched_provider_failure_degrades_from_child_ledger_fact(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-provider-failure"
    error_code = "LLM_HTTP_REQUEST_FAILED"
    _add_valid_qc_provider_failure_ledger(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
    )

    decision = _evaluate_raising_qc(
        session,
        qc_type=qc_type,
        error=_qc_execution_error(call_id=call_id, error_code=error_code),
        bundle=bundle,
        draft_row_id=draft_row_id,
        execution_step_key=step_key,
    )

    assert decision.should_continue is True
    assert decision.stop_reason == f"{qc_type}_execution_failed"
    report = session.execute(select(QcReport)).scalar_one()
    attempt = session.execute(select(AttemptTracker)).scalar_one()
    assert report.scene_id == "CH100_SC01"
    assert attempt.details_json["llm_call_id"] == call_id
    assert attempt.details_json["execution_step_key"] == step_key


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
@pytest.mark.parametrize(
    "tamper",
    [
        "no_child",
        "child_undispatched",
        "child_bad_status",
        "child_bad_error",
        "ordinal_gap",
        "child_total_mismatch",
        "final_settled",
        "parent_estimated",
        "parent_reserved",
        "parent_charged",
        "parent_prompt",
        "parent_completion",
        "parent_total",
        "parent_latency",
        "parent_dispatch_missing",
        "parent_settled_missing",
        "parent_usage_flag",
        "settled_then_failed",
        "child_estimated_exceeds_reserved",
        "child_charged_mismatch",
    ],
)
def test_qc_provider_failure_ledger_tampering_never_degrades(
    session,
    qc_type: str,
    tamper: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    call_id = f"call-{qc_type}-provider-tamper-{tamper}"
    error_code = "LLM_HTTP_REQUEST_FAILED"
    parent, child = _add_valid_qc_provider_failure_ledger(
        session,
        qc_type=qc_type,
        call_id=call_id,
        error_code=error_code,
    )
    if tamper == "no_child":
        session.delete(child)
    elif tamper == "child_undispatched":
        child.request_dispatched_at = None
    elif tamper == "child_bad_status":
        child.accounting_status = "released"
    elif tamper == "child_bad_error":
        child.error_code = "LLM_TIMEOUT"
    elif tamper == "ordinal_gap":
        child.provider_attempt_no = 1
    elif tamper == "child_total_mismatch":
        child.total_tokens += 1
        parent.total_tokens += 1
    elif tamper == "final_settled":
        final_child = LlmCallAttempt(
            attempt_id=f"attempt-{call_id}-final",
            llm_call_id=call_id,
            provider_attempt_no=1,
            dispatch_kind="transport_retry",
            request_max_output_tokens=8,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_tokens=2,
            reserved_tokens=2,
            budget_charged_tokens=2,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:02+00:00",
            settled_at="2026-07-14T00:00:03+00:00",
            latency_ms=3,
        )
        session.add(final_child)
        parent.estimated_tokens += 2
        parent.reserved_tokens += 2
        parent.budget_charged_tokens += 2
        parent.prompt_tokens += 1
        parent.completion_tokens += 1
        parent.total_tokens += 2
        parent.latency_ms += 3
    elif tamper == "parent_estimated":
        parent.estimated_tokens += 1
    elif tamper == "parent_reserved":
        parent.reserved_tokens += 1
    elif tamper == "parent_charged":
        parent.budget_charged_tokens += 1
    elif tamper == "parent_prompt":
        parent.prompt_tokens += 1
    elif tamper == "parent_completion":
        parent.completion_tokens += 1
    elif tamper == "parent_total":
        parent.total_tokens += 1
    elif tamper == "parent_latency":
        parent.latency_ms += 1
    elif tamper == "parent_dispatch_missing":
        parent.request_dispatched_at = None
    elif tamper == "parent_settled_missing":
        parent.settled_at = None
    elif tamper == "parent_usage_flag":
        parent.usage_is_estimate = not child.usage_is_estimate
    elif tamper == "settled_then_failed":
        child.accounting_status = "settled"
        child.error_code = None
        child.error_text = None
        final_child = LlmCallAttempt(
            attempt_id=f"attempt-{call_id}-final-failure",
            llm_call_id=call_id,
            provider_attempt_no=1,
            dispatch_kind="transport_retry",
            request_max_output_tokens=8,
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_tokens=2,
            reserved_tokens=2,
            budget_charged_tokens=2,
            accounting_status="failed",
            request_dispatched_at="2026-07-14T00:00:02+00:00",
            settled_at="2026-07-14T00:00:03+00:00",
            latency_ms=3,
            error_code=error_code,
            error_text="final provider failure",
        )
        session.add(final_child)
        parent.estimated_tokens += 2
        parent.reserved_tokens += 2
        parent.budget_charged_tokens += 2
        parent.prompt_tokens += 1
        parent.completion_tokens += 1
        parent.total_tokens += 2
        parent.latency_ms += 3
    elif tamper == "child_estimated_exceeds_reserved":
        child.estimated_tokens = child.reserved_tokens + 1
        parent.estimated_tokens = child.estimated_tokens
    elif tamper == "child_charged_mismatch":
        child.budget_charged_tokens -= 1
        parent.budget_charged_tokens = child.budget_charged_tokens
    session.commit()
    error = _qc_execution_error(call_id=call_id, error_code=error_code)

    with pytest.raises(LLMNodeExecutionError) as raised:
        _evaluate_raising_qc(
            session,
            qc_type=qc_type,
            error=error,
            bundle=bundle,
            draft_row_id=draft_row_id,
            execution_step_key=step_key,
        )

    assert raised.value is error
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


@pytest.mark.parametrize("qc_type", ["hard_qc", "soft_qc"])
def test_qc_owner_lease_prerenewal_failure_rethrows_before_provider_and_side_effects(
    session,
    qc_type: str,
) -> None:
    bundle, draft_row_id, step_key = _seed_qc_ledger_case(session, qc_type=qc_type)
    if qc_type == "hard_qc":
        client = FakeQcClient(_base_qc_payload(resolution_code="hard_pass", next_action="pass"))
        engine = HardQcEngine(session, llm_client=client)
    else:
        client = FakeSoftQcClient(
            [_base_soft_qc_payload(resolution_code="soft_pass", next_action="pass")]
        )
        engine = SoftQcEngine(session, llm_client=client)

    def lose_lease_before_provider(*, lease_seconds: int) -> None:
        del lease_seconds
        raise DomainError("RUN_OWNER_LEASE_LOST", "lost before QC provider", status_code=409)

    token = begin_llm_execution("exec-qc-ledger", lease_renewer=lose_lease_before_provider)
    try:
        with pytest.raises(LLMNodeExecutionError) as raised:
            if qc_type == "hard_qc":
                engine.evaluate(
                    scene_id="CH100_SC01",
                    bundle=bundle,
                    neutral_draft_row_id=draft_row_id,
                    neutral_content="Draft under ledger review.",
                    execution_step_key=step_key,
                )
            else:
                engine.evaluate(
                    scene_id="CH100_SC01",
                    bundle=bundle,
                    source_draft_row_id=draft_row_id,
                    source_draft_content="Draft under ledger review.",
                    execution_step_key=step_key,
                )
    finally:
        end_llm_execution(token)

    assert raised.value.error_code == "RUN_OWNER_LEASE_LOST"
    assert client.requests == []
    assert session.execute(select(LlmCall)).scalars().all() == []
    assert session.execute(select(QcReport)).scalars().all() == []
    assert session.execute(select(AttemptTracker)).scalars().all() == []


def test_hard_qc_engine_degrades_runtime_failure_to_continue_with_warning(session) -> None:
    _seed_scene(session)
    session.get(ChapterGoal, "CH100").project_id = "PROJECT_QC"
    session.add(
        SceneDraft(
            row_id="draft_neutral_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="neutral_draft",
            content="Neutral draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = "exec-hard-qc-runtime-failure"
    state.run_execution_status = "active"
    state.scene_token_budget = 100_000
    session.commit()

    engine = HardQcEngine(session, llm_client=FakeAccountedQcRuntimeFailureClient())

    token = begin_llm_execution("exec-hard-qc-runtime-failure")
    try:
        decision = engine.evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            neutral_draft_row_id="draft_neutral_CH100_SC01",
            neutral_content="Neutral draft under review.",
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "hard_qc")).scalars().one()
    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "hard_qc")).scalars().one()
    llm_attempt = session.execute(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == llm_call.llm_call_id)
    ).scalar_one()

    # Wave 2（§5.4/§7.7）：QC 运行时失败降级续跑；LlmCall 仍留错误审计
    assert decision.branch == "continue"
    assert decision.should_continue is True
    assert decision.stop_reason == "hard_qc_execution_failed"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code == "RuntimeError"
    assert llm_attempt.accounting_status == "failed"
    assert llm_attempt.request_dispatched_at is not None
    assert llm_attempt.error_code == llm_call.error_code
    assert llm_call.estimated_tokens == llm_attempt.estimated_tokens
    assert llm_call.reserved_tokens == llm_attempt.reserved_tokens
    assert llm_call.budget_charged_tokens == llm_attempt.budget_charged_tokens
    assert llm_call.prompt_tokens == llm_attempt.prompt_tokens
    assert llm_call.completion_tokens == llm_attempt.completion_tokens
    assert llm_call.total_tokens == llm_attempt.total_tokens
    assert llm_call.latency_ms == llm_attempt.latency_ms
    assert report.resolution_code == "hard_pass"
    warning = next(issue for issue in report.issues_json if issue["issue_key"] == "hard_qc_execution_failed")
    assert warning["quality_level"] == "Q2"
    assert "timed out" in warning["message"]
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []
