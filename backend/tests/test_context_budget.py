from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import AttemptTracker, ChapterGoal, HumanReviewEvent, LlmCall, QcReport, SceneCard, SceneDraft, SceneRunState, StoryProject
from novel_system.services.context_budget import (
    CONTINUITY_DROP_ORDER,
    _compress_continuity_digest,
    _compress_style_observations,
    apply_context_budget,
    collect_prompt_sections,
    estimate_tokens,
    finalize_request_budget,
)
from novel_system.services.llm_client import LLMRequest
from novel_system.services.llm_task_runner import begin_llm_execution, end_llm_execution
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from tests.accounted_llm_fakes import AccountedGenerateMixin


def _bundle_snapshot() -> dict:
    return {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "CH001_SC01",
        "chapter_id": "CH001",
        "inline_digests": {
            "chapter_goal": " ".join(["Goal pressure"] * 80),
            "scene_card": " ".join(["Scene pressure"] * 80),
            "voice_card": "Short clipped lines; pressure makes the tone harder.",
            "style_rule": "Keep emotion in gesture and pause.",
            "banned_rule": "Do not explain the whole backstory at reunion time.",
            "style_observation": (
                "Gesture before explanation. Let silence carry accusation. "
                "End paragraphs on pressure, not exposition. Keep the emotional turn tactile."
            ),
            "calibration_line": "The door closed like a sentence left unfinished.",
            "relation_card": "Reunion tension; B knows slightly more than A.",
            "world_rule": "Public spellcasting inside the city is forbidden.",
            "foreshadow": "The old letter sender clue is now in play.",
            "scene_memory": "Previous scene memory digest about the hidden sender.",
            "scene_summary": "Current scene summary digest about the reunion beat.",
            "chapter_summary": "Chapter summary digest about guarded trust replacing suspicion.",
            "similar_scene": (
                "Similar-scene reference: another gate reunion leaned too heavily on explanation "
                "and lost pressure halfway through."
            ),
        },
    }


def test_token_estimator_is_conservative_for_cjk_and_keeps_latin_ratio() -> None:
    chinese = "雨落在旧城的石阶上，顾舟没有回头。"
    english = "Rain fell across the old stone steps while Gu Zhou kept walking."

    # CJK cannot use the Latin len/4 shortcut: each wide glyph is budgeted
    # approximately as a whole token, including full-width punctuation.
    assert estimate_tokens(chinese) >= len(chinese) - 2
    assert estimate_tokens(english) == (len(english) + 3) // 4
    assert estimate_tokens(f"{chinese} {english}") >= estimate_tokens(chinese)


def test_cjk_compression_shortens_unspaced_paragraphs() -> None:
    paragraph = "顾舟沿着废弃站台向前走，铜铃在袖口里一下一下撞着腕骨。" * 12

    style = _compress_style_observations(paragraph)
    continuity = _compress_continuity_digest(paragraph)

    assert len(style) < len(paragraph)
    assert len(continuity) < len(style)
    assert style.endswith("...")
    assert continuity.endswith("...")
    assert estimate_tokens(style) <= 50
    assert estimate_tokens(continuity) <= 26


def test_latin_compression_remains_word_readable() -> None:
    paragraph = " ".join(f"observation-{index}" for index in range(80))

    compressed = _compress_style_observations(paragraph)

    assert compressed.startswith("observation-0")
    assert compressed.endswith("...")
    assert len(compressed) < len(paragraph)


def _seed_scene(session) -> None:
    session.add(StoryProject(project_id="PROJECT100", title="Context budget", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT100",
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            project_id="PROJECT100",
            chapter_id="CH100",
            scene_seq=1,
            scene_goal="Force both characters to reveal what they know.",
        )
    )
    session.add(SceneRunState(scene_id="CH100_SC01", scene_status="ready"))
    session.commit()


def _begin_qc_execution(session, execution_id: str):
    state = session.get(SceneRunState, "CH100_SC01")
    state.active_execution_id = execution_id
    state.run_execution_status = "active"
    session.commit()
    return begin_llm_execution(execution_id)


class TrackingClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest):  # pragma: no cover - should never be called in these tests
        self.requests.append(request)
        raise AssertionError("LLM should not be called when continuity warning requires scene splitting")


def test_collect_prompt_sections_includes_author_preference_profile() -> None:
    snapshot = _bundle_snapshot()
    snapshot["inline_digests"]["author_preference_profile"] = (
        "Author prefers sharper rhetorical questions and rejects explanatory dialogue."
    )

    result = apply_context_budget(
        system_prompt="System prompt.",
        task_prompt="Task prompt.",
        bundle_snapshot=snapshot,
        sections=collect_prompt_sections(snapshot),
        max_input_tokens=900,
    )
    budget = result["budget"]

    assert budget["section_status"]["author_preference_profile"]["status"] == "included"
    assert "## Author Preference Profile" in result["user_prompt"]
    assert "sharper rhetorical questions" in result["user_prompt"]


def test_prompt_builder_surfaces_continuity_warning_into_token_budget() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "neutral_draft", max_input_tokens=120)

    assert payload["continuity_warning"] == payload["token_budget"]["continuity_warning"]
    assert payload["continuity_warning"]["code"] == "continuity_budget_exceeded"
    assert payload["continuity_warning"]["recommended_action"] == "split_scene"
    assert payload["token_budget"]["split_scene_recommended"] is True
    assert payload["token_budget"]["stop_reason"] == "split_scene_recommended"


def test_finalize_request_budget_marks_actual_final_prompt_overflow() -> None:
    base_budget = {
        "target_input_tokens": 80,
        "estimated_input_tokens": 40,
        "remaining_input_tokens": 40,
        "included_sections": ["scene_card"],
        "compressed_sections": [],
        "omitted_sections": [],
        "section_status": {"scene_card": {"label": "Scene Card", "status": "included", "estimated_tokens": 20}},
        "continuity_policy": [],
        "split_scene_recommended": False,
        "stop_reason": None,
        "continuity_warning": None,
    }

    result = finalize_request_budget(
        system_prompt="system prompt",
        user_prompt=" ".join(["expanded final prompt"] * 60),
        base_budget=base_budget,
    )

    assert result["budget"]["estimated_input_tokens"] > 80
    assert result["budget"]["split_scene_recommended"] is True
    assert result["continuity_warning"]["requires_scene_split"] is True
    assert result["continuity_warning"]["code"] == "continuity_budget_exceeded"


def test_hard_qc_engine_escalates_continuity_warning_before_llm_call(session) -> None:
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

    client = TrackingClient()
    engine = HardQcEngine(session, llm_client=client)
    continuity_warning = {
        "code": "continuity_budget_exceeded",
        "message": "Prompt still exceeds the safe input budget after deterministic continuity compaction.",
        "recommended_action": "split_scene",
        "requires_scene_split": True,
    }
    engine.prompt_builder.build = lambda *_args, **_kwargs: {
        "system_prompt": "system",
        "user_prompt": "user",
        "structured_schema": {},
        "token_budget": {
            "target_input_tokens": 60,
            "estimated_input_tokens": 80,
            "remaining_input_tokens": -20,
            "included_sections": [],
            "compressed_sections": [],
            "omitted_sections": [],
            "section_status": {},
            "continuity_policy": [],
            "split_scene_recommended": True,
            "stop_reason": "split_scene_recommended",
            "continuity_warning": continuity_warning,
        },
        "continuity_warning": continuity_warning,
    }

    token = _begin_qc_execution(session, "exec-hard-qc-continuity")
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

    # Wave 2（§5.4/§7.7）：continuity 预算超限 = QC 自身执行失败——降级续跑 +
    # Q2 警告（含完整 continuity_warning 载荷），不再断头撤销交付
    assert client.requests == []
    assert decision.branch == "continue"
    assert decision.should_continue is True
    assert decision.stop_reason == "hard_qc_continuity_budget_exceeded"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert report.next_action == "pass"
    issue = report.issues_json[0]
    assert issue["issue_key"] == "continuity_budget_exceeded"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["message"] == continuity_warning["message"]
    assert issue["continuity_warning"]["code"] == continuity_warning["code"]
    assert issue["continuity_warning"]["recommended_action"] == "split_scene"
    assert issue["continuity_warning"]["requires_scene_split"] is True
    assert issue["continuity_warning"]["target_input_tokens"] == 60
    assert isinstance(issue["continuity_warning"]["estimated_input_tokens"], int)
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_hard_qc_engine_recomputes_budget_for_final_prompt_before_llm_call(session) -> None:
    _seed_scene(session)
    client = TrackingClient()
    engine = HardQcEngine(session, llm_client=client)
    engine.prompt_builder.build = lambda *_args, **_kwargs: {
        "system_prompt": "system",
        "user_prompt": "user",
        "structured_schema": {},
        "token_budget": {
            "target_input_tokens": 60,
            "estimated_input_tokens": 10,
            "remaining_input_tokens": 50,
            "included_sections": [],
            "compressed_sections": [],
            "omitted_sections": [],
            "section_status": {},
            "continuity_policy": [],
            "split_scene_recommended": False,
            "stop_reason": None,
            "continuity_warning": None,
        },
        "continuity_warning": None,
    }

    token = _begin_qc_execution(session, "exec-hard-qc-final-prompt-continuity")
    try:
        decision = engine.evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            neutral_draft_row_id="draft_neutral_CH100_SC01",
            neutral_content=" ".join(["oversized neutral draft"] * 80),
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()

    assert client.requests == []
    assert decision.branch == "continue"
    assert report.issues_json[0]["issue_key"] == "continuity_budget_exceeded"
    assert report.issues_json[0]["blocking"] is False


def test_soft_qc_engine_escalates_continuity_warning_before_llm_call(session) -> None:
    _seed_scene(session)
    session.add(
        SceneDraft(
            row_id="draft_style_CH100_SC01",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            stage="style_draft",
            content="Styled draft under review.",
            source_bundle_id="bundle_CH100_SC01",
            source_bundle_hash="bundle_hash_CH100_SC01",
        )
    )
    session.commit()

    client = TrackingClient()
    engine = SoftQcEngine(session, llm_client=client)
    continuity_warning = {
        "code": "continuity_budget_exceeded",
        "message": "Prompt still exceeds the safe input budget after deterministic continuity compaction.",
        "recommended_action": "split_scene",
        "requires_scene_split": True,
    }
    engine.prompt_builder.build = lambda *_args, **_kwargs: {
        "system_prompt": "system",
        "user_prompt": "user",
        "structured_schema": {},
        "token_budget": {
            "target_input_tokens": 60,
            "estimated_input_tokens": 80,
            "remaining_input_tokens": -20,
            "included_sections": [],
            "compressed_sections": [],
            "omitted_sections": [],
            "section_status": {},
            "continuity_policy": [],
            "split_scene_recommended": True,
            "stop_reason": "split_scene_recommended",
            "continuity_warning": continuity_warning,
        },
        "continuity_warning": continuity_warning,
    }

    token = _begin_qc_execution(session, "exec-soft-qc-continuity")
    try:
        decision = engine.evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            source_draft_row_id="draft_style_CH100_SC01",
            source_draft_content="Styled draft under review.",
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "soft_qc")).scalars().one()
    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "soft_qc")).scalars().one()

    # Wave 2（§5.4/§7.7）：软 QC continuity 预算超限降级为 waive 续跑 + Q2 警告
    # + carry note 留痕，不再断头撤销交付
    assert client.requests == []
    assert decision.branch == "waive"
    assert decision.should_continue is True
    assert decision.stop_reason == "soft_qc_continuity_budget_exceeded"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert llm_call.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert report.resolution_code == "soft_waive"
    assert report.next_action == "pass_with_notes"
    issue = report.issues_json[0]
    assert issue["issue_key"] == "continuity_budget_exceeded"
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["message"] == continuity_warning["message"]
    assert issue["continuity_warning"]["code"] == continuity_warning["code"]
    assert issue["continuity_warning"]["recommended_action"] == "split_scene"
    assert issue["continuity_warning"]["requires_scene_split"] is True
    assert issue["continuity_warning"]["target_input_tokens"] == 60
    assert isinstance(issue["continuity_warning"]["estimated_input_tokens"], int)
    carry_notes = [entry for entry in report.rewrite_brief_json if entry.get("kind") == "carry_forward_note"]
    assert carry_notes and "degraded" in carry_notes[0]["carry_note_text"]
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_soft_qc_engine_recomputes_budget_for_final_prompt_before_llm_call(session) -> None:
    _seed_scene(session)
    client = TrackingClient()
    engine = SoftQcEngine(session, llm_client=client)
    engine.prompt_builder.build = lambda *_args, **_kwargs: {
        "system_prompt": "system",
        "user_prompt": "user",
        "structured_schema": {},
        "token_budget": {
            "target_input_tokens": 60,
            "estimated_input_tokens": 10,
            "remaining_input_tokens": 50,
            "included_sections": [],
            "compressed_sections": [],
            "omitted_sections": [],
            "section_status": {},
            "continuity_policy": [],
            "split_scene_recommended": False,
            "stop_reason": None,
            "continuity_warning": None,
        },
        "continuity_warning": None,
    }

    token = _begin_qc_execution(session, "exec-soft-qc-final-prompt-continuity")
    try:
        decision = engine.evaluate(
            scene_id="CH100_SC01",
            bundle={
                "bundle_id": "bundle_CH100_SC01",
                "bundle_snapshot_hash": "bundle_hash_CH100_SC01",
                "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
            },
            source_draft_row_id="draft_style_CH100_SC01",
            source_draft_content=" ".join(["oversized styled draft"] * 80),
        )
    finally:
        end_llm_execution(token)
    session.commit()

    report = session.execute(select(QcReport)).scalars().one()

    assert client.requests == []
    assert decision.branch == "waive"
    assert report.issues_json[0]["issue_key"] == "continuity_budget_exceeded"
    assert report.issues_json[0]["blocking"] is False
