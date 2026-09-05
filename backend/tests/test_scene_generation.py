from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AttemptTracker,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterState,
    FinalScene,
    GenerationPlanningArtifact,
    LlmCall,
    RelationProfile,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.errors import DomainError
from novel_system.services.context_budget import estimate_tokens
from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.near_final import (
    CHAPTER_ARCHITECTURE_ARTIFACT,
    CHARACTER_PRESSURE_ARTIFACT,
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.prompt_builder import PromptConfigurationError
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import (
    SceneGenerationService,
    StyleGenerationResult,
    _apply_style_length_patch,
    _apply_style_salvage_patch,
    _assess_de_template_rewrite,
    _assess_style_anchor_conformance,
    _extract_scene_text,
    _neutral_length_instruction,
    _normalize_style_paragraph_shape,
    _scene_text_integrity_markers,
    _style_repair_length_instruction,
)
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.real_llm_fakes import ScenePipelineOnlineFake


class FakeSceneClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            structured_output = {
                "scene_text": "Provider-generated neutral scene text.",
                "continuity_notes": ["kept the reunion tense"],
            }
            request_id = "resp_fake_neutral_001"
            model = "fake-neutral-model"
            usage = {"input_tokens": 111, "output_tokens": 29, "total_tokens": 140}
        elif len(self.requests) == 2:
            structured_output = {
                "scene_text": "Provider-generated style scene text.",
                "style_notes": ["leaned harder into rhythm and inner tension"],
            }
            request_id = "resp_fake_style_001"
            model = "fake-style-model"
            usage = {"input_tokens": 121, "output_tokens": 33, "total_tokens": 154}
        else:
            structured_output = {
                "scene_text": "Provider-generated patched scene text.",
                "style_notes": ["applied one controlled patch pass"],
            }
            request_id = "resp_fake_patch_001"
            model = "fake-patch-model"
            usage = {"input_tokens": 131, "output_tokens": 37, "total_tokens": 168}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=__import__("json").dumps(structured_output),
            structured_output=structured_output,
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


class FakeNeutralLengthRepairClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        scene_text = (
            "红色信封。" * 60
            if len(self.requests) == 1
            else "她接过红色信封，退到门边。脚步停在楼梯口，她没有拆信，只把信封压进掌心。"
        )
        payload = {"scene_text": scene_text}
        request_id = f"resp_neutral_length_{len(self.requests)}"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-neutral-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": "fake-neutral-model",
                "usage": {},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeNeutralRequiredFactRepairClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        scene_text = (
            "季青看见了那张还款记录，却没有追问。"
            if len(self.requests) == 1
            else "季青看见了那张还款记录，终于明白旧债是周伯代还的，却没有追问。"
        )
        payload = {"scene_text": scene_text}
        request_id = f"resp_neutral_fact_{len(self.requests)}"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-neutral-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": request_id,
                "model": "fake-neutral-model",
                "usage": {},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeNeutralInvalidRepairClient(FakeNeutralLengthRepairClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        response = super().generate(request)
        if len(self.requests) == 2:
            scene_text = "红色信封。" * 2
            payload = {"scene_text": scene_text}
            response = replace(
                response,
                text=__import__("json").dumps(payload, ensure_ascii=False),
                structured_output=payload,
            )
        return response


class FakeFailingClient(AccountedGenerateMixin):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise ValueError("malformed provider payload")


class FakeDeTemplateClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            structured_output = {
                "scene_text": (
                    "她低头看着钥匙，沉默了片刻。"
                    "他低头看着录音，沉默了片刻。"
                    "她低头看着门缝，沉默了片刻。"
                    "她知道真相必须公开。"
                ),
                "style_notes": ["kept an unsafe template"],
            }
            request_id = "resp_fake_style_template"
            model = "fake-style-model"
        else:
            structured_output = {
                "scene_text": "她把钥匙扣进掌心，转身拔掉录音线。门缝里的光灭了，外面的人开始敲门。",
                "style_notes": ["removed repeated action template"],
            }
            request_id = "resp_fake_de_template"
            model = "fake-patch-model"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=__import__("json").dumps(structured_output),
            structured_output=structured_output,
            response_format="json_object",
            raw_response={"id": request_id, "model": model, "usage": {}, "finish_reason": "stop"},
            usage={"input_tokens": 101, "output_tokens": 25, "total_tokens": 126},
            finish_reason="stop",
        )


class FakeRegressiveDeTemplateClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            scene_text = "她走了。"
            request_id = "resp_fake_de_template_regressed"
        else:
            scene_text = (
                "她低头看着红色信封，沉默了片刻。"
                "他低头看着录音，沉默了片刻。"
                "她低头看着门缝，沉默了片刻。"
                "她知道真相必须公开。"
            )
            request_id = "resp_fake_style_with_required_fact"
        payload = {"scene_text": scene_text}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeMissingSceneTextPatchClient(FakeDeTemplateClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        if request.node_id != "style_patch":
            return super().generate(request)
        self.requests.append(request)
        payload = {"style_notes": ["provider omitted scene_text"]}
        return LLMResponse(
            request_id="resp_fake_patch_missing_scene_text",
            provider="fake-provider",
            model="fake-patch-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={
                "id": "resp_fake_patch_missing_scene_text",
                "model": "fake-patch-model",
                "usage": {},
            },
            usage={"input_tokens": 80, "output_tokens": 5, "total_tokens": 85},
            finish_reason="stop",
        )


class FakeUnsafeBaseThenSafePatchClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            scene_text = "她接过红色信封，没有拆。门外脚步停住，她把信封压进抽屉，转身关灯。"
            request_id = "resp_safe_style_patch"
        else:
            scene_text = "她低头看着门缝，沉默了片刻。" * 20
            request_id = "resp_unsafe_style_base"
        payload = {"scene_text": scene_text}
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeLengthOnlyLocalPatchClient(AccountedGenerateMixin):
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信，只把它握在手里。"
    removable = (
        "窗外的雨水沿着窗框一遍又一遍地滑落，墙上的影子也一遍又一遍地晃动，"
        "同一阵脚步声被反复描写了许多次，除此之外没有发生任何新的事情。"
    )
    replacement = "窗外雨声未停。"
    ending = "她仍把信封握在手里。"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            payload = {
                "edits": [
                    {
                        "segment_id": "S003",
                        "new_text": self.replacement,
                    }
                ]
            }
            request_id = "resp_local_length_patch"
        else:
            payload = {"scene_text": self.neutral + self.removable + self.ending}
            request_id = "resp_length_only_style_base"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeFactRepairThenLengthPatchClient(AccountedGenerateMixin):
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信。"
    repaired_but_short = "她接过红色信封，没有拆。门外脚步忽然停住。"
    insertion = "雨水沿着门槛漫开，她后退半步，仍盯着楼梯口，直到那阵脚步再次逼近。"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        patch_count = sum(
            prior.node_id == "style_patch" for prior in self.requests
        )
        if request.node_id != "style_patch":
            payload = {"scene_text": "她在门边等。"}
            request_id = "resp_fact_and_length_unsafe_base"
        elif patch_count == 1:
            payload = {"scene_text": self.repaired_but_short}
            request_id = "resp_fact_repaired_length_short"
        else:
            payload = {
                "edits": [
                    {
                        "segment_id": "S001",
                        "new_text": self.insertion,
                    }
                ]
            }
            request_id = "resp_followup_local_length_patch"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


class FakeExtremeUnderlengthThenSalvageClient(AccountedGenerateMixin):
    paragraph_one = (
        "她接过红色信封，没有拆，只把封口对着灯光看了一遍。"
        "楼梯上的脚步停住，门外却没有人敲门。"
    )
    paragraph_two = (
        "她后退半步，将信封压在桌角，听见雨水沿着窗棂往下流。"
        "那阵脚步又响了一次，比先前更近。"
    )
    ending = "她仍没有拆信，伸手熄了灯，站在黑暗里等。"
    neutral = "\n\n".join((paragraph_one, paragraph_two, ending))
    extreme_short = "她接过红色信封，没有拆，倚门听着那阵越来越近的脚步。"
    replacement = (
        "红色信封到了她手里，还是一个小小的纸包；她不拆，"
        "只向灯下一照。楼梯上的脚步停了，门也很客气，并不响。"
    )

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            payload = {
                "edits": [
                    {
                        "segment_id": "S001",
                        "new_text": self.replacement,
                    }
                ]
            }
            request_id = "resp_bounded_style_salvage"
        else:
            payload = {"scene_text": self.extreme_short}
            request_id = "resp_extreme_underlength_style"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model="fake-model",
            text=__import__("json").dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": request_id, "model": "fake-model", "usage": {}},
            usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            finish_reason="stop",
        )


def _seed_scene(
    session,
    *,
    must_include_text: str | None = "A red envelope changes hands.",
    target_length_band: str = "short",
) -> None:
    session.add(StoryProject(project_id="PROJECT100", title="Scene generation", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT100",
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    session.add(ChapterState(chapter_id="CH100", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            project_id="PROJECT100",
            chapter_id="CH100",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="Clocktower Roof",
            scene_goal="Force both characters to reveal what they know.",
            beats_json=["arrival", "reveal", "standoff"],
            must_include_text=must_include_text,
            target_length_band=target_length_band,
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


def _seed_scene_blueprint(session) -> None:
    session.add(
        SceneBlueprint(
            row_id="scene_blueprint_CH100_SC01_seed",
            scene_id="CH100_SC01",
            chapter_id="CH100",
            source_bundle_id="seed_source_CH100_SC01",
            source_bundle_hash="seed_hash_CH100_SC01",
            blueprint_json={
                "character_current_desire": "CHAR_A wants the truth before CHAR_B can leave.",
                "concrete_obstacle": "CHAR_B controls the red envelope and refuses a straight answer.",
                "choice_under_pressure": "CHAR_A must choose whether to trust CHAR_B or expose the clue.",
                "information_release": "The envelope proves someone watched the reunion.",
                "power_shift": "CHAR_B begins with leverage; CHAR_A takes it back by naming the watcher.",
                "emotional_turn": "Suspicion hardens into reluctant alliance.",
                "irreversible_consequence": "Both characters know the secret is no longer private.",
                "ending_reader_question": "Who sent the red envelope?",
                "image_promise": "The red envelope returns with a changed meaning.",
            },
            status="accepted",
        )
    )
    session.commit()


def _seed_scene_planning(session) -> None:
    """预置章级架构 + 角色压力规划产物（status=active），让编排复用而非联网生成。

    这样 scene_blueprint（另由 _seed_scene_blueprint 预置）与规划两步都被跳过、
    不产生 LLM 调用，测试才能干净地落到 neutral_draft 的目标失败点。"""
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_chapter_arch_CH100_seed",
            artifact_type=CHAPTER_ARCHITECTURE_ARTIFACT,
            object_type="chapter",
            object_id="CH100",
            chapter_id="CH100",
            payload_json={
                "chapter_promise": "the scene must change the available choices",
                "escalation_path": ["pressure appears", "a choice narrows", "a cost lands"],
                "reveal_plan": ["the governing constraint is exposed"],
                "payoff_target": "the chosen action creates the next problem",
                "character_shift": "certainty gives way to costly resolve",
                "ending_question": "what will the choice cost next",
            },
            status="active",
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_char_pressure_CH100_SC01_seed",
            artifact_type=CHARACTER_PRESSURE_ARTIFACT,
            object_type="scene",
            object_id="CH100_SC01",
            chapter_id="CH100",
            scene_id="CH100_SC01",
            payload_json={
                "surface_goal": "finish the immediate task",
                "hidden_fear": "the choice will expose a weakness",
                "wrong_belief": "control can prevent every loss",
                "shame_point": "asking for help feels like surrender",
                "avoidance_strategy": "delay the irreversible choice",
                "relationship_debt": "an old promise remains unpaid",
                "current_mask": "measured confidence",
            },
            status="active",
        )
    )
    session.commit()


def test_run_scene_persists_provider_neutral_draft_and_bundle_linkage(session) -> None:
    # This test isolates persistence/lineage. Continuity blocking for a missing
    # must-include fact is exercised explicitly by the offline test below.
    _seed_scene(session, must_include_text=None)
    fake_client = FakeSceneClient()
    support = ScenePipelineOnlineFake()

    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=fake_client),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
        soft_qc_engine=SoftQcEngine(session, llm_client=support),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)

    result = orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_calls = session.execute(select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())).scalars().all()
    llm_calls_by_step = {llm_call.step: llm_call for llm_call in llm_calls}
    neutral_llm_call = llm_calls_by_step["neutral_draft"]
    style_llm_call = llm_calls_by_step["style_draft"]
    bundle = session.execute(select(SceneBundle)).scalars().one()
    neutral_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().one()
    style_draft = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalars().one()
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalars().one()
    final_scene = session.execute(select(FinalScene)).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")
    soft_qc = result["soft_qc"]

    assert len(fake_client.requests) == 2
    request = fake_client.requests[0]
    assert request.response_format == "json_object"
    assert request.response_schema == {
        "name": "neutral_draft",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scene_text"],
            "properties": {
                "scene_text": {"type": "string"},
                "continuity_notes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    assert request.node_id == "neutral_draft"
    assert request.reasoning_level == "medium"
    assert any("Scene ID: CH100_SC01" in message["content"] for message in request.messages)
    assert any("Return JSON that matches the structured schema exactly." in message["content"] for message in request.messages)
    style_request = fake_client.requests[1]
    assert style_request.model == "gpt-5"
    assert style_request.node_id == "style_draft"
    assert style_request.response_schema == {
        "name": "style_draft",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scene_text"],
            "properties": {
                "scene_text": {"type": "string"},
                "style_notes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    assert style_request.reasoning_level == "medium"
    assert any("Approved Neutral Draft" in message["content"] for message in style_request.messages)
    assert any("Provider-generated neutral scene text." in message["content"] for message in style_request.messages)
    assert any(
        "Recompose the supplied source draft" in message["content"]
        for message in style_request.messages
    )
    assert any(
        "immutable event-and-fact scaffold" in message["content"]
        for message in style_request.messages
    )
    assert sum(message["content"].count("Return JSON that matches the structured schema exactly.") for message in style_request.messages) == 1

    assert neutral_draft.content == "Provider-generated neutral scene text."
    assert "Clocktower Roof" not in neutral_draft.content
    assert neutral_draft.generation_llm_call_id == neutral_llm_call.llm_call_id
    assert neutral_draft.source_bundle_id == bundle.bundle_id
    assert neutral_draft.source_bundle_hash == bundle.bundle_snapshot_hash
    assert style_draft.content == "Provider-generated style scene text."
    assert style_draft.generation_llm_call_id == style_llm_call.llm_call_id
    assert style_draft.source_bundle_id == bundle.bundle_id
    assert style_draft.source_bundle_hash == bundle.bundle_snapshot_hash

    assert {"neutral_draft", "hard_qc", "style_draft", "soft_qc"}.issubset(llm_calls_by_step)
    assert neutral_llm_call.provider == "fake-provider"
    assert neutral_llm_call.node_id == "neutral_draft"
    assert neutral_llm_call.reasoning_level == "medium"
    assert neutral_llm_call.model == "fake-neutral-model"
    assert neutral_llm_call.step == "neutral_draft"
    assert neutral_llm_call.scene_id == "CH100_SC01"
    assert neutral_llm_call.chapter_id == "CH100"
    assert neutral_llm_call.prompt_hash
    assert neutral_llm_call.prompt_tokens == 111
    assert neutral_llm_call.completion_tokens == 29
    assert neutral_llm_call.total_tokens == 140
    assert neutral_llm_call.finish_reason == "stop"
    assert neutral_llm_call.error_code is None
    assert neutral_llm_call.request_payload_summary["token_budget"]["estimated_input_tokens"] == sum(
        estimate_tokens(message["content"]) for message in request.messages
    )
    assert style_llm_call.provider == "fake-provider"
    assert style_llm_call.node_id == "style_draft"
    assert style_llm_call.reasoning_level == "medium"
    assert style_llm_call.model == "fake-style-model"
    assert style_llm_call.step == "style_draft"
    assert style_llm_call.scene_id == "CH100_SC01"
    assert style_llm_call.chapter_id == "CH100"
    assert style_llm_call.prompt_hash
    assert style_llm_call.prompt_tokens == 121
    assert style_llm_call.completion_tokens == 33
    assert style_llm_call.total_tokens == 154
    assert style_llm_call.finish_reason == "stop"
    assert style_llm_call.error_code is None
    assert style_llm_call.request_payload_summary["token_budget"]["estimated_input_tokens"] == sum(
        estimate_tokens(message["content"]) for message in style_request.messages
    )

    assert attempt.source_bundle_id == bundle.bundle_id
    assert attempt.details_json == {"row_id": neutral_draft.row_id, "llm_call_id": neutral_llm_call.llm_call_id}
    assert state.current_neutral_draft_row_id == neutral_draft.row_id
    assert state.current_bundle_id == bundle.bundle_id
    assert state.current_bundle_hash == bundle.bundle_snapshot_hash
    assert state.current_style_draft_row_id == style_draft.row_id
    assert state.total_attempt_count == 1
    assert final_scene.source_bundle_id == bundle.bundle_id
    assert final_scene.source_bundle_hash == bundle.bundle_snapshot_hash
    assert final_scene.content == style_draft.content
    assert final_scene.generation_llm_call_id == style_draft.generation_llm_call_id
    assert style_draft.content != neutral_draft.content

    assert result["current_bundle_id"] == bundle.bundle_id
    assert result["current_bundle_hash"] == bundle.bundle_snapshot_hash
    assert soft_qc["branch"] == "continue"


def test_scene_generation_rejects_required_scene_text_when_provider_omits_it(session) -> None:
    _seed_scene(session)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Force both characters to reveal what they know."},
        },
    }
    service = SceneGenerationService(session, llm_client=FakeSceneClient())

    with pytest.raises(DomainError) as exc:
        service.generate_neutral_draft("CH100_SC01", bundle)

    assert exc.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []


def test_neutral_draft_retries_once_when_numeric_length_band_is_missed(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "红色信封必须交到她手中。"},
        },
    }
    client = FakeNeutralLengthRepairClient()

    result = SceneGenerationService(
        session, llm_client=client
    ).generate_neutral_draft("CH100_SC01", bundle)

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at, SceneDraft.row_id)
    ).scalars().all()
    rejected = next(draft for draft in drafts if draft.stage == "neutral_rejected")
    active = next(draft for draft in drafts if draft.stage == "neutral_draft")
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalar_one()
    calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at)
    ).scalars().all()

    assert len(client.requests) == 2
    assert [call.step for call in calls] == [
        "neutral_draft",
        "neutral_draft_repair",
    ]
    assert "Absolute final range: 30-100" in client.requests[0].messages[1]["content"]
    assert "previous attempt" in client.requests[1].messages[1]["content"]
    assert "Rejected Neutral Draft Requiring One Deterministic Repair" in client.requests[1].messages[1]["content"]
    assert "Deterministic Neutral Repair Brief" in client.requests[1].messages[1]["content"]
    assert "红色信封。" * 3 in client.requests[1].messages[1]["content"]
    assert "Remove at least 210 visible characters" in client.requests[1].messages[1]["content"]
    assert client.requests[1].temperature == 0.1
    assert rejected.status == "rejected"
    assert rejected.content == "红色信封。" * 60
    assert active.content == result.content
    assert result.content.startswith("她接过红色信封")
    assert attempt.details_json["repair"]["accepted"] is True
    assert attempt.details_json["validation"]["accepted"] is True
    assert session.get(SceneRunState, "CH100_SC01").total_attempt_count == 1


def test_neutral_repair_keeps_an_already_valid_source_in_a_local_length_window() -> None:
    scene = SimpleNamespace(target_length_band="700-1350 Chinese characters")

    instruction = _neutral_length_instruction(
        scene,
        previous_length=900,
        retry=True,
    )

    assert "previous length already passed" in instruction
    assert "within 810-990 visible characters" in instruction
    assert "smallest localized edits" in instruction


def test_neutral_draft_repairs_missing_alternative_even_without_length_band(session) -> None:
    _seed_scene(
        session,
        must_include_text="季青；代还|还清",
        target_length_band="",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "季青发现旧债由周伯代还。"},
        },
    }
    client = FakeNeutralRequiredFactRepairClient()

    result = SceneGenerationService(
        session, llm_client=client
    ).generate_neutral_draft("CH100_SC01", bundle)

    repair_prompt = client.requests[1].messages[1]["content"]
    assert len(client.requests) == 2
    assert "代还|还清" in repair_prompt
    assert "vertical bar means alternatives" in repair_prompt
    assert "代还" in result.content
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")
    ).scalar_one()
    assert attempt.details_json["repair"]["accepted"] is True
    assert attempt.details_json["validation"]["accepted"] is True


def test_neutral_draft_fails_closed_when_the_only_repair_is_still_invalid(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "红色信封必须交到她手中。"},
        },
    }

    with pytest.raises(DomainError) as exc:
        SceneGenerationService(
            session,
            llm_client=FakeNeutralInvalidRepairClient(),
        ).generate_neutral_draft("CH100_SC01", bundle)

    assert exc.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []
    rejected = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_rejected")
    ).scalar_one()
    assert rejected.status == "rejected"
    attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "neutral_draft",
            AttemptTracker.status == "failed",
        )
    ).scalar_one()
    assert attempt.details_json["error_code"] == "NEUTRAL_DRAFT_REPAIR_INVALID"
    assert attempt.details_json["validation"]["accepted"] is False
    assert session.get(SceneRunState, "CH100_SC01").current_neutral_draft_row_id is None


def test_bundle_and_style_prompt_include_only_approved_runtime_author_preference(session) -> None:
    _seed_scene(session)
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_draft_ignored",
            scope_type="global",
            scope_ref_id="global",
            status="draft",
            runtime_eligible=0,
            summary_json={"preferred_revision_moves": ["draft preference should stay out of runtime prompts"]},
            source_patch_ids_json=[],
        )
    )
    session.add(
        AuthorPreferenceProfile(
            profile_id="author_pref_approved_runtime",
            scope_type="global",
            scope_ref_id="global",
            status="approved",
            runtime_eligible=1,
            summary_json={
                "preferred_revision_moves": ["sharper rhetorical questions"],
                "rejected_revision_moves": ["expository dialogue"],
                "ai_trace_terms_to_watch": ["somehow meaningful"],
            },
            source_patch_ids_json=["patch_runtime_pref"],
        )
    )
    session.commit()

    bundle = BundleBuilder(session).build("CH100_SC01")
    snapshot = bundle["snapshot"]

    assert snapshot["source_version_refs"]["author_preference_profile_id"] == "author_pref_approved_runtime"
    assert "author_preference_profile" in snapshot["inline_digests"]
    assert "sharper rhetorical questions" in snapshot["inline_digests"]["author_preference_profile"]
    assert "draft preference should stay out" not in snapshot["inline_digests"]["author_preference_profile"]

    fake_client = FakeSceneClient()
    request = SceneGenerationService(session, llm_client=fake_client).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
    )

    style_prompt = fake_client.requests[0].messages[1]["content"]
    assert "sharper rhetorical questions" in style_prompt
    assert "expository dialogue" in style_prompt
    assert "draft preference should stay out" not in style_prompt
    style_call = session.get(LlmCall, request.llm_call_id)
    assert style_call is not None
    prompt_summary = style_call.request_payload_summary or {}
    assert "author_preference_profile" in prompt_summary["token_budget"]["included_sections"]


def test_author_instruction_is_frozen_into_bundle_and_reaches_neutral_prompt(session) -> None:
    _seed_scene(session, must_include_text=None)
    note = "把选择提前到第一段，结尾不要解释。"
    bundle = BundleBuilder(session).build("CH100_SC01", author_note=note)

    snapshot = bundle["snapshot"]
    assert snapshot["inline_digests"]["author_instruction"] == note
    assert snapshot["source_version_refs"]["author_instruction_hash"]
    assert any(
        item["slot"] == "author_instruction"
        for item in snapshot["ordered_injections"]
    )

    fake_client = FakeSceneClient()
    SceneGenerationService(session, llm_client=fake_client).generate_neutral_draft(
        "CH100_SC01",
        bundle,
        author_note=note,
    )
    prompt_text = "\n".join(message["content"] for message in fake_client.requests[0].messages)
    assert note in prompt_text


def test_generate_style_draft_runs_one_de_template_pass_for_high_risk_anti_template(session) -> None:
    _seed_scene(session, must_include_text=None)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }
    fake_client = FakeDeTemplateClient()

    result = SceneGenerationService(session, llm_client=fake_client).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
    )

    assert len(fake_client.requests) == 2
    assert fake_client.requests[1].node_id == "style_patch"
    assert fake_client.requests[1].temperature == 0.3
    assert "De-template Rewrite Brief" in fake_client.requests[1].messages[1]["content"]
    assert "Deterministic Style Repair Length Guard" in fake_client.requests[1].messages[1]["content"]
    assert "Edit the labeled style draft directly" in fake_client.requests[1].messages[1]["content"]
    assert "Recompose the supplied source draft" not in fake_client.requests[1].messages[1]["content"]
    assert "quality:scene:CH100_SC01:template_action_reuse" in fake_client.requests[1].messages[1]["content"]

    drafts = session.execute(select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())).scalars().all()
    assert [draft.stage for draft in drafts] == ["style_draft", "de_template"]
    assert drafts[0].content.startswith("她低头看着钥匙")
    assert drafts[1].content == result.content
    assert result.row_id == drafts[1].row_id

    llm_calls_by_step = {
        row.step: row for row in session.execute(select(LlmCall).order_by(LlmCall.created_at.asc())).scalars().all()
    }
    assert set(llm_calls_by_step) == {"style_draft", "de_template"}
    assert llm_calls_by_step["de_template"].node_id == "style_patch"

    attempts = {
        row.step: row for row in session.execute(select(AttemptTracker).order_by(AttemptTracker.created_at.asc())).scalars().all()
    }
    assert attempts["de_template"].details_json["quality_gate"]["triggered"] is True
    assert attempts["de_template"].details_json["quality_gate"]["rewrite_pass"] == 1
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == drafts[1].row_id


def test_de_template_rewrite_is_audited_but_rejected_when_required_fact_is_lost(session) -> None:
    _seed_scene(session, must_include_text="红色信封")
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    fake_client = FakeRegressiveDeTemplateClient()

    result = SceneGenerationService(
        session,
        llm_client=fake_client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="红色信封被递到她手里。",
    )

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())
    ).scalars().all()
    base = next(draft for draft in drafts if draft.stage == "style_draft")
    rejected = next(draft for draft in drafts if draft.stage == "de_template")
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()

    assert result.row_id == base.row_id
    assert result.content == base.content
    assert rejected.content == "她走了。"
    assert rejected.status == "rejected"
    assert attempt.details_json["acceptance"]["accepted"] is False
    assert "required_facts_regressed" in attempt.details_json["acceptance"]["reasons"]
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == base.row_id


def test_de_template_missing_scene_text_is_audited_and_falls_back_to_base(session) -> None:
    _seed_scene(session, must_include_text=None)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeMissingSceneTextPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
    )

    base = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "style_draft")
    ).scalar_one()
    failed = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "de_template",
            AttemptTracker.status == "failed",
        )
    ).scalar_one()
    assert result.row_id == base.row_id
    assert result.content == base.content
    assert failed.details_json["error_code"] == "SCENE_GENERATION_RESPONSE_INVALID"
    assert failed.details_json["business_attempt_consumed"] is True
    assert session.get(SceneRunState, "CH100_SC01").current_style_draft_row_id == base.row_id


def test_extract_scene_text_normalizes_double_encoded_unicode_fragments() -> None:
    payload = {"scene_text": "她走进雨\\u6ccc\\u4e2d，神色依u7136平静。"}
    response = LLMResponse(
        request_id="resp_unicode_normalize",
        provider="fake-provider",
        model="fake-model",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={"id": "resp_unicode_normalize", "usage": {}},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    assert _extract_scene_text(response) == "她走进雨泌中，神色依然平静。"


@pytest.mark.parametrize(
    "text,marker",
    [
        ("```json\n{\"scene_text\": \"她走了。\"}\n```", "model_response_artifact"),
        ("Let me refine the final JSON before returning it.", "model_response_artifact"),
        ("他停住；C季青却没有回头。", "orphan_ascii_before_cjk"),
    ],
)
def test_scene_text_integrity_rejects_provider_response_artifacts(
    text: str, marker: str
) -> None:
    assert marker in _scene_text_integrity_markers(text)


def test_unsafe_base_is_audited_then_retried_from_approved_neutral_fallback(
    session, monkeypatch
) -> None:
    _seed_scene(session, must_include_text="红色信封")
    scene = session.get(SceneCard, "CH100_SC01")
    scene.target_length_band = "30-100 Chinese characters"
    session.commit()
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    neutral = "她接过红色信封，站在门边等了片刻。楼梯上传来脚步，她没有拆信，只把它握在手里。"

    client = FakeUnsafeBaseThenSafePatchClient()
    service = SceneGenerationService(
        session,
        llm_client=client,
    )
    original_inject = service._inject_style_reference
    injection_calls: list[None] = []

    def recording_inject(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        injection_calls.append(None)
        return original_inject(*args, **kwargs)

    monkeypatch.setattr(service, "_inject_style_reference", recording_inject)
    result = service.generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=neutral,
    )

    drafts = session.execute(
        select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())
    ).scalars().all()
    rejected = next(draft for draft in drafts if draft.stage == "style_rejected")
    fallback = next(draft for draft in drafts if draft.stage == "style_draft")
    patched = next(draft for draft in drafts if draft.stage == "de_template")
    base_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "style_draft")
    ).scalar_one()

    assert rejected.status == "rejected"
    assert len(rejected.content) > 100
    assert fallback.content == neutral
    assert base_attempt.details_json["base_safety"]["accepted"] is False
    assert base_attempt.details_json["rejected_candidate_row_id"] == rejected.row_id
    assert "required_facts_regressed" in base_attempt.details_json["base_safety"]["reasons"]
    assert "target_length_not_met" in base_attempt.details_json["base_safety"]["reasons"]
    assert result.row_id == patched.row_id
    assert result.content == patched.content
    assert "红色信封" in result.content
    repair_prompt = client.requests[1].messages[1]["content"]
    assert "Rejected Style Draft Requiring One Safety Repair" in repair_prompt
    assert "Safety Repair Brief" in repair_prompt
    assert "De-template Rewrite Brief" not in repair_prompt
    assert "她低头看着门缝" in repair_prompt
    assert "Every final required constraint must be explicit." in repair_prompt
    assert "include at least one literal alternative from each group: 红色信封" in repair_prompt
    assert "working window at 40-90" in repair_prompt
    assert "Deterministic Style Repair Length Guard" in repair_prompt
    assert "Absolute final range: 30-100" in repair_prompt
    assert "Expand by at least" not in repair_prompt
    assert "Edit the labeled rejected style draft directly" in repair_prompt
    assert "Recompose the supplied source draft" not in repair_prompt
    assert client.requests[1].temperature == 0.1
    style_prompt = client.requests[0].messages[1]["content"]
    assert "Deterministic Style Rewrite Length Guard" in style_prompt
    # 安全修复编辑的是已经风格化的拒绝稿，不能按拒绝稿的新长度再次重算并叠加
    # 一份冲突的风格量化约束；完整风格注入只发生在首轮生成。
    assert len(injection_calls) == 1
    repair_attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()
    assert (
        repair_attempt.details_json["repair_source_style_draft_row_id"]
        == rejected.row_id
    )
    assert (
        repair_attempt.details_json["source_style_draft_row_id"]
        == fallback.row_id
    )


def test_length_only_unsafe_style_uses_exact_local_patch_instead_of_full_rewrite(
    session,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-80 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeLengthOnlyLocalPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 2
    assert result.content == client.neutral + client.replacement + client.ending
    assert 30 <= sum(not char.isspace() for char in result.content) <= 80
    patch_prompt = client.requests[1].messages[1]["content"]
    assert "Deterministic Local Length Patch Contract" in patch_prompt
    assert "Return edits only, never scene_text" in patch_prompt
    assert "Recompose the supplied source draft" not in patch_prompt
    attempt = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "de_template")
    ).scalar_one()
    assert attempt.details_json["acceptance"]["accepted"] is True
    assert attempt.details_json["length_patch"]["valid"] is True
    assert attempt.details_json["length_patch"]["mode"] == "compress"
    schema = client.requests[1].response_schema["schema"]
    allowed_ids = schema["properties"]["edits"]["items"]["properties"][
        "segment_id"
    ]["enum"]
    assert "S003" in allowed_ids
    assert "S004" not in allowed_ids


def test_fact_repair_can_finish_with_one_local_length_followup(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="45-100 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeFactRepairThenLengthPatchClient()

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 3
    assert "红色信封" in result.content
    assert client.insertion in result.content
    assert 45 <= sum(not char.isspace() for char in result.content) <= 100
    attempts = session.execute(
        select(AttemptTracker)
        .where(AttemptTracker.step == "de_template")
        .order_by(AttemptTracker.attempt_id.asc())
    ).scalars().all()
    assert len(attempts) == 2
    assert attempts[0].details_json["acceptance"]["reasons"] == [
        "target_length_not_met"
    ]
    assert attempts[1].details_json["acceptance"]["accepted"] is True
    assert attempts[1].details_json["length_patch"]["valid"] is True
    followup_schema = client.requests[2].response_schema["schema"]["properties"][
        "edits"
    ]
    assert followup_schema["minItems"] == followup_schema["maxItems"] == 1
    new_text_schema = followup_schema["items"]["properties"]["new_text"]
    assert new_text_schema["minLength"] > 1


def test_extreme_underlength_style_uses_bounded_neutral_salvage(
    session,
    monkeypatch,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="100-260 Chinese characters",
    )
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {
            "scene_id": "CH100_SC01",
            "chapter_id": "CH100",
            "inline_digests": {"scene_card": "Goal"},
        },
    }
    client = FakeExtremeUnderlengthThenSalvageClient()
    monkeypatch.setattr(
        "novel_system.services.scene_generation._assess_style_rewrite_conformance",
        lambda **_kwargs: {
            "available": True,
            "comparable": True,
            "score_delta": -0.002,
            "regressed": False,
        },
    )

    result = SceneGenerationService(
        session,
        llm_client=client,
    ).generate_style_draft(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content=client.neutral,
    )

    assert len(client.requests) == 2
    assert result.content != client.neutral
    assert client.replacement in result.content
    assert client.paragraph_two in result.content
    assert result.content.endswith(client.ending)
    assert 100 <= sum(not char.isspace() for char in result.content) <= 260
    salvage_attempt = session.execute(
        select(AttemptTracker).where(
            AttemptTracker.step == "style_salvage_patch"
        )
    ).scalar_one()
    assert salvage_attempt.details_json["acceptance"]["accepted"] is True
    assert salvage_attempt.details_json["style_salvage"]["valid"] is True
    assert salvage_attempt.details_json["style_salvage"]["segment_id"] == "S001"
    schema = client.requests[1].response_schema["schema"]
    allowed_ids = schema["properties"]["edits"]["items"]["properties"][
        "segment_id"
    ]["enum"]
    assert "S003" not in allowed_ids


def test_style_salvage_patch_rejects_protected_ending_segment() -> None:
    source = "\n".join(("甲" * 70, "乙" * 70, "丙" * 50))
    payload = {"edits": [{"segment_id": "S003", "new_text": "丁" * 50}]}
    response = LLMResponse(
        request_id="resp_salvage_protected_ending",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    with pytest.raises(Exception, match="segment_id_not_editable"):
        _apply_style_salvage_patch(
            source_content=source,
            response=response,
            scene=SimpleNamespace(target_length_band="120-260 Chinese characters"),
            llm_call_id="llm_salvage_protected_ending",
        )


def test_safety_repair_does_not_reject_safe_text_for_quality_score_drop(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    rewritten = (
        "她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听了片刻，"
        "随即关灯，把信封收进抽屉。"
    )

    assessment = _assess_de_template_rewrite(
        scene=scene,
        source_content="红色信封在她手中。",
        authoritative_content="她接过红色信封，确认门外有人，随后把信封收好。",
        rewritten_content=rewritten,
        source_quality_gate={"score": 1.0, "findings": []},
        style_conformance={
            "available": True,
            "comparable": True,
            "regressed": True,
            "score_delta": -0.2,
        },
    )

    assert assessment["accepted"] is True
    assert assessment["reasons"] == []
    assert assessment["quality_non_regression_enforced"] is False
    assert assessment["style_non_regression_enforced"] is False


@pytest.mark.parametrize(
    ("target_length_band", "source_length", "expected"),
    [
        (
            "700-1350 Chinese characters",
            642,
            ("add 108-188 visible characters", "narrow 750-830"),
        ),
        (
            "650-1250 Chinese characters",
            1500,
            ("remove 300-380 visible characters", "narrow 1120-1200"),
        ),
    ],
)
def test_style_repair_length_guard_targets_nearest_safe_boundary(
    target_length_band: str,
    source_length: int,
    expected: tuple[str, str],
) -> None:
    scene = SimpleNamespace(target_length_band=target_length_band)

    instruction = _style_repair_length_instruction(
        scene,
        source_length=source_length,
    )

    assert expected[0] in instruction
    assert expected[1] in instruction


def test_exact_style_length_patch_applies_non_overlapping_expansion() -> None:
    source = "甲" * 40 + "。\n" + "乙" * 40 + "。\n" + "己" * 8
    payload = {
        "edits": [
            {
                "segment_id": "S002",
                "new_text": "庚" * 30,
            }
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_expand",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        scene=SimpleNamespace(target_length_band="100-200 Chinese characters"),
        llm_call_id="llm_patch_expand",
    )

    assert sum(not char.isspace() for char in patched) == 120
    assert patched == "甲" * 40 + "。\n" + "乙" * 40 + "。" + "庚" * 30 + "\n" + "己" * 8
    assert audit["valid"] is True
    assert audit["mode"] == "expand"
    assert audit["visible_delta"] == 30


def test_exact_style_length_patch_uses_segment_id_to_disambiguate_repeated_span() -> None:
    repeated = "可删片段" * 10
    source = "\n".join(("甲" * 30, repeated, "乙" * 20, repeated, "丙" * 30))
    payload = {
        "edits": [
            {
                "segment_id": "S004",
                "new_text": "",
            }
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_disambiguated_compress",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        scene=SimpleNamespace(target_length_band="100-130 Chinese characters"),
        llm_call_id="llm_patch_disambiguated_compress",
    )

    assert patched == "\n".join(("甲" * 30, repeated, "乙" * 20, "", "丙" * 30))
    assert audit["valid"] is True
    assert audit["mode"] == "compress"
    assert audit["deterministic_segment_address_validation"] is True


def test_exact_style_length_patch_selects_safe_subset_of_oversized_insertions() -> None:
    source = "\n".join(("甲" * 200, "乙" * 220, "丙" * 222))
    payload = {
        "edits": [
            {"segment_id": "S001", "new_text": "丁" * 340},
            {"segment_id": "S002", "new_text": "戊" * 391},
        ]
    }
    response = LLMResponse(
        request_id="resp_patch_subset_expand",
        provider="fake",
        model="fake",
        text=__import__("json").dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        finish_reason="stop",
    )

    patched, audit = _apply_style_length_patch(
        source_content=source,
        response=response,
        scene=SimpleNamespace(target_length_band="700-1350 Chinese characters"),
        llm_call_id="llm_patch_subset_expand",
    )

    assert sum(not char.isspace() for char in patched) == 982
    assert audit["submitted_edit_count"] == 2
    assert audit["edit_count"] == 1
    assert audit["omitted_edit_count"] == 1
    assert audit["segment_ids"] == ["S001"]


def test_ordinary_de_template_rejects_measurable_frozen_style_regression(session) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    source = "她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关灯。"
    rewritten = "她把红色信封放在桌角。门外有人。她听了一会儿，然后关灯。"

    assessment = _assess_de_template_rewrite(
        scene=scene,
        source_content=source,
        rewritten_content=rewritten,
        source_quality_gate={"score": 0.0, "findings": []},
        style_conformance={
            "available": True,
            "comparable": True,
            "regressed": True,
            "score_delta": -0.010001,
        },
    )

    assert assessment["accepted"] is False
    assert "style_conformance_regressed" in assessment["reasons"]
    assert assessment["style_non_regression_enforced"] is True


def test_ordinary_de_template_requires_actionable_target_defect_reduction(
    session,
    monkeypatch,
) -> None:
    _seed_scene(
        session,
        must_include_text="红色信封",
        target_length_band="30-100 Chinese characters",
    )
    scene = session.get(SceneCard, "CH100_SC01")
    unchanged_gate = {
        "triggered": True,
        "score": 0.4,
        "risk_dimensions": ["model_voice"],
        "findings": [{"dimension": "model_voice"}],
    }
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: unchanged_gate,
    )

    assessment = _assess_de_template_rewrite(
        scene=scene,
        source_content="她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关灯。",
        rewritten_content="她把红色信封压在桌角，没有拆。门外脚步停住，她抬头听着，随后关了灯。",
        source_quality_gate=unchanged_gate,
    )

    assert assessment["accepted"] is False
    assert "target_quality_defects_not_reduced" in assessment["reasons"]
    assert assessment["source_target_evidence_available"] is True


def test_style_anchor_audit_exposes_soft_shape_repair_without_punctuation_quota(
    monkeypatch,
) -> None:
    profile = SimpleNamespace(
        profile_id="frozen_profile",
        profile_json={
            "metrics_baseline": {
                "paragraphs_per_1k": {"mean": 5.0, "std": 0.5},
                "semicolon_density_per_1k": {"mean": 10.0, "std": 1.0},
            }
        },
    )
    monkeypatch.setattr(
        "novel_system.services.scene_generation.resolve_style_runtime_contract_state",
        lambda _bundle: SimpleNamespace(
            status="frozen",
            mode="frozen",
            error_code=None,
            contract={"frozen": True},
        ),
    )
    monkeypatch.setattr(
        "novel_system.services.scene_generation.contract_profile_objects",
        lambda _contract: [profile],
    )
    text = "\n\n".join("他沿着走廊走到门边，又停下来看了一眼窗外的雨。" for _ in range(20))

    audit = _assess_style_anchor_conformance(bundle={"snapshot": {}}, text=text)

    assert audit["available"] is True
    assert audit["requires_repair"] is True
    # 段落明显碎裂需要修；分号少不是文学缺陷，不能为了拟合统计主动补分号。
    assert {item["metric"] for item in audit["violations"]} == {
        "paragraphs_per_1k",
    }
    assert any("Paragraph structure" in item for item in audit["repair_directions"])
    assert not any("Semicolon rhythm" in item for item in audit["repair_directions"])
    assert not any(char.isdigit() for item in audit["repair_directions"] for char in item)


def test_style_paragraph_normalization_only_merges_and_preserves_text_sequence(
    monkeypatch,
) -> None:
    target = SimpleNamespace(
        target_hash="target_hash_demo",
        metrics={
            "paragraphs_per_1k": SimpleNamespace(
                mean=5.0,
                tolerance=1.0,
            )
        },
    )
    monkeypatch.setattr(
        "novel_system.services.scene_generation.resolve_style_runtime_contract_state",
        lambda _bundle: SimpleNamespace(
            status="frozen",
            mode="frozen",
            error_code=None,
            contract={"frozen": True},
        ),
    )
    monkeypatch.setattr(
        "novel_system.services.scene_generation.contract_profile_objects",
        lambda _contract: [],
    )
    monkeypatch.setattr(
        "novel_system.services.style_reference.candidate_rerank.build_style_target",
        lambda _profiles: target,
    )
    text = "\n\n".join(
        f"第{index}盏灯沿着长廊依次暗下，他走到门边，又停住听了一会雨声。"
        for index in range(24)
    )

    normalized, audit = _normalize_style_paragraph_shape(
        bundle={"snapshot": {}},
        text=text,
    )

    assert audit["applied"] is True
    assert audit["operation"] == "merge_adjacent_only"
    assert audit["before_paragraph_count"] == 24
    assert audit["after_paragraph_count"] == audit["preferred_count"]
    assert "".join(normalized.split()) == "".join(text.split())
    assert audit["content_sequence_preserved"] is True


class _FakeBestOfNDeTemplateClient(AccountedGenerateMixin):
    """每个候选的 style 稿都返回触发反模板闸的模板文本，去模板稿返回各异的清理文本。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self._style_n = 0
        self._patch_n = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            self._patch_n += 1
            structured_output = {
                "scene_text": (
                    f"清理稿{self._patch_n}：她把钥匙扣进掌心，拔掉录音线；门缝里的光灭了，"
                    f"走廊尽头响起第{self._patch_n}声敲门，她没有回头。"
                ),
                "style_notes": ["removed repeated action template"],
            }
            request_id = f"resp_fake_de_template_{self._patch_n}"
            model = "fake-patch-model"
        else:
            self._style_n += 1
            structured_output = {
                "scene_text": (
                    "她低头看着钥匙，沉默了片刻。"
                    "他低头看着录音，沉默了片刻。"
                    "她低头看着门缝，沉默了片刻。"
                    f"她知道真相必须公开。候选{self._style_n}。"
                ),
                "style_notes": ["kept an unsafe template"],
            }
            request_id = f"resp_fake_style_template_{self._style_n}"
            model = "fake-style-model"
        return LLMResponse(
            request_id=request_id,
            provider="fake-provider",
            model=model,
            text=__import__("json").dumps(structured_output),
            structured_output=structured_output,
            response_format="json_object",
            raw_response={"id": request_id, "model": model, "usage": {}, "finish_reason": "stop"},
            usage={"input_tokens": 80, "output_tokens": 30, "total_tokens": 110},
            finish_reason="stop",
        )


def test_best_of_n_multiple_candidates_de_template_no_pk_collision(session) -> None:
    """QA3 回归：Best-of-N 下 ≥2 个候选都触发去模板时，去模板稿 row_id 必须互异，
    不得因共用 row_id 撞 SceneDraft 主键抛 IntegrityError 致整跑崩溃。"""
    _seed_scene(session)
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }
    fake_client = _FakeBestOfNDeTemplateClient()

    # 修复前：第二个候选的去模板稿与第一个共用 row_id → flush 抛 IntegrityError。
    results = SceneGenerationService(session, llm_client=fake_client).generate_style_draft_candidates(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id="draft_neutral_CH100_SC01",
        neutral_content="Approved neutral draft.",
        n_candidates=2,
    )
    assert results, "应至少产出一个候选"

    de_template_drafts = session.execute(
        select(SceneDraft).where(SceneDraft.stage == "de_template")
    ).scalars().all()
    assert len(de_template_drafts) >= 2, f"应有 ≥2 条去模板稿(每候选一条)，实得 {len(de_template_drafts)}"
    row_ids = [d.row_id for d in de_template_drafts]
    assert len(set(row_ids)) == len(row_ids), f"去模板稿 row_id 必须互异，实得 {row_ids}"


def test_generate_style_draft_blocks_provider_when_scene_must_split(session) -> None:
    _seed_scene(session)
    fake_client = FakeSceneClient()
    service = SceneGenerationService(session, llm_client=fake_client)

    class StubPromptBuilder:
        def build(self, *_args, **_kwargs):
            return {
                "template_name": "style_draft",
                "template_version": "test",
                "system_prompt": "system",
                "user_prompt": "user\n\nReturn JSON that matches the structured schema exactly.",
                "structured_schema": {},
                "prompt_hash": "prompt_hash_style_split",
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

    service._prompt_builder_instance = StubPromptBuilder()
    bundle = {
        "bundle_id": "bundle_CH100_SC01",
        "bundle_snapshot_hash": "bundle_hash_demo",
        "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Goal"}},
    }

    with pytest.raises(DomainError) as exc:
        service.generate_style_draft(
            "CH100_SC01",
            bundle,
            neutral_draft_row_id="draft_neutral_CH100_SC01",
            neutral_content=" ".join(["oversized neutral draft"] * 80),
        )

    assert exc.value.code == "CONTINUITY_BUDGET_EXCEEDED"
    assert fake_client.requests == []

    llm_call = session.execute(select(LlmCall).where(LlmCall.step == "style_draft")).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "style_draft")).scalars().one()

    assert llm_call.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert llm_call.request_payload_summary["continuity_warning"]["requires_scene_split"] is True
    assert attempt.status == "failed"


def test_generate_neutral_draft_records_failed_attempt_and_bumps_counter(session) -> None:
    _seed_scene(session)
    bundle = {"bundle_id": "bundle_CH100_SC01", "bundle_snapshot_hash": "bundle_hash_demo", "snapshot": {"scene_id": "CH100_SC01", "chapter_id": "CH100", "inline_digests": {"scene_card": "Force both characters to reveal what they know."}}}
    service = SceneGenerationService(session, llm_client=FakeFailingClient())

    state = session.get(SceneRunState, "CH100_SC01")
    state.current_bundle_id = bundle["bundle_id"]
    state.current_bundle_hash = bundle["bundle_snapshot_hash"]
    session.commit()

    try:
        service.generate_neutral_draft("CH100_SC01", bundle)
    except ValueError as exc:
        assert str(exc) == "malformed provider payload"
    else:
        raise AssertionError("expected generation failure")

    session.commit()

    llm_call = session.execute(select(LlmCall)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert llm_call.error_code == "ValueError"
    assert attempt.status == "failed"
    assert attempt.source_bundle_id == bundle["bundle_id"]
    assert attempt.details_json["error_code"] == "ValueError"
    assert attempt.details_json["llm_call_id"] == llm_call.llm_call_id
    assert state.total_attempt_count == 1
    assert state.current_bundle_id == bundle["bundle_id"]
    assert state.current_bundle_hash == bundle["bundle_snapshot_hash"]
    assert state.current_neutral_draft_row_id is None


def test_run_scene_records_neutral_prompt_builder_failure_and_clears_stale_state(session, monkeypatch) -> None:
    _seed_scene(session)
    _seed_scene_blueprint(session)
    _seed_scene_planning(session)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_BASE_URL", raising=False)

    state = session.get(SceneRunState, "CH100_SC01")
    state.current_neutral_draft_row_id = "stale_neutral"
    state.current_qc_report_id = "stale_qc"
    state.soft_patch_count = 1
    session.commit()

    def failing_prompt_builder(self):
        raise PromptConfigurationError("prompts config missing")

    monkeypatch.setattr(SceneGenerationService, "_prompt_builder", failing_prompt_builder)

    orchestrator = Orchestrator(session)
    with pytest.raises(PromptConfigurationError):
        orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_call = session.execute(select(LlmCall)).scalars().one()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "neutral_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert llm_call.step == "neutral_draft"
    assert llm_call.error_code == "PromptConfigurationError"
    assert attempt.status == "failed"
    assert state.current_neutral_draft_row_id is None
    assert state.current_qc_report_id is None
    assert state.soft_patch_count == 0


def test_run_scene_records_style_routing_failure(session, monkeypatch) -> None:
    _seed_scene(session, must_include_text=None)
    _seed_scene_blueprint(session)
    _seed_scene_planning(session)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "false")
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_API_KEY", raising=False)
    monkeypatch.delenv("NOVEL_SYSTEM_LLM_BASE_URL", raising=False)

    class FakeRoutingConfig:
        def __init__(self) -> None:
            self.task_routing = {
                "neutral_draft": type(
                    "TaskConfig",
                    (),
                    {
                        "provider": "offline_deterministic",
                        "model": "offline-neutral",
                        "temperature": 0.6,
                        "max_output_tokens": 6000,
                        "response_format": "json_object",
                    },
                )(),
                "hard_qc": type(
                    "TaskConfig",
                    (),
                    {
                        "provider": "offline_deterministic",
                        "model": "offline-hard-qc",
                        "temperature": 0.0,
                        "max_output_tokens": 4000,
                        "response_format": "json_object",
                    },
                )(),
            }

    monkeypatch.setattr(
        "novel_system.services.llm_task_runner.load_model_routing_config",
        lambda: FakeRoutingConfig(),
    )

    support = ScenePipelineOnlineFake()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=support),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
    )
    with pytest.raises(KeyError):
        orchestrator.run_scene("CH100_SC01")
    session.commit()

    llm_calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
    ).scalars().all()
    attempt = session.execute(select(AttemptTracker).where(AttemptTracker.step == "style_draft")).scalars().one()
    state = session.get(SceneRunState, "CH100_SC01")

    assert [llm_call.step for llm_call in llm_calls] == [
        "neutral_draft",
        "hard_qc",
        "style_draft",
    ]
    # 缺路由统一为引导性错误码(原为裸 "KeyError");原始 KeyError 仍向上抛(见 raises)
    assert llm_calls[-1].error_code == "LLM_ROUTE_NOT_CONFIGURED"
    assert attempt.status == "failed"
    assert attempt.details_json["llm_call_id"] == llm_calls[-1].llm_call_id
    assert attempt.details_json["error_code"] == "LLM_ROUTE_NOT_CONFIGURED"
    assert state.current_style_draft_row_id is None


def test_online_draft_cannot_advance_when_neutral_repair_still_misses_required_fact(session) -> None:
    # 中性稿是事实骨架。首次生成和唯一一次修复都缺失必含事实时，必须在进入
    # hard-QC/style 阶段前失败关闭，不能把已知不合格的原稿伪装成 active draft。
    _seed_scene(session)
    support = ScenePipelineOnlineFake()

    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=support),
        hard_qc_engine=HardQcEngine(session, llm_client=support),
        soft_qc_engine=SoftQcEngine(session, llm_client=support),
        planning_service=NearFinalPlanningService(session, llm_client=support),
        near_final_service=NearFinalAcceptanceService(session, llm_client=support),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=support)
    with pytest.raises(DomainError) as blocked:
        orchestrator.run_scene("CH100_SC01")
    assert blocked.value.code == "NEUTRAL_DRAFT_REPAIR_INVALID"
    session.commit()

    llm_calls = session.execute(
        select(LlmCall).order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
    ).scalars().all()
    generation_steps = [
        llm_call.step
        for llm_call in llm_calls
        if llm_call.step in {"neutral_draft", "neutral_draft_repair", "hard_qc", "style_draft"}
    ]
    assert generation_steps == ["neutral_draft", "neutral_draft_repair"]
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_draft")
    ).scalars().all() == []
    # orchestrator 对失败场景回滚业务草稿；LLM 记账仍独立保留。
    assert session.execute(
        select(SceneDraft).where(SceneDraft.stage == "neutral_rejected")
    ).scalars().all() == []
    assert session.execute(select(FinalScene)).scalars().all() == []
    assert all(llm_call.provider == "test-online-provider" for llm_call in llm_calls)
    assert all(llm_call.finish_reason == "stop" for llm_call in llm_calls)


def test_generate_style_draft_candidates_returns_sorted_list(session) -> None:
    _seed_scene(session, must_include_text=None)
    fake_client = FakeSceneClient()
    service = SceneGenerationService(session, llm_client=fake_client)
    bundle_builder = BundleBuilder(session)
    bundle = bundle_builder.build("CH100_SC01")

    neutral = service.generate_neutral_draft("CH100_SC01", bundle)
    candidates = service.generate_style_draft_candidates(
        "CH100_SC01",
        bundle,
        neutral_draft_row_id=neutral.row_id,
        neutral_content=neutral.content,
        n_candidates=3,
    )
    session.commit()

    assert len(candidates) >= 1
    assert all(isinstance(c, StyleGenerationResult) for c in candidates)
    assert all(c.content for c in candidates)

    attempts = session.execute(
        select(AttemptTracker).where(AttemptTracker.step == "style_draft")
    ).scalars().all()
    candidate_indices = [a.details_json.get("candidate_index") for a in attempts if a.details_json.get("candidate_index") is not None]
    assert len(candidate_indices) >= 1

    state = session.get(SceneRunState, "CH100_SC01")
    assert state.current_style_draft_row_id == candidates[0].row_id


def test_adversarial_rank_score_lower_for_ai_heavy_text() -> None:
    from novel_system.services.literary_quality import adversarial_rank_score

    clean_text = (
        "She opened the door. He must choose the archive or save the child. "
        "The cost was his position. He left."
    )
    ai_heavy_text = (
        "She suddenly realized the moon was somehow meaningful. "
        "Everything changed forever. As if fate."
    )
    assert adversarial_rank_score(clean_text) > adversarial_rank_score(ai_heavy_text)


def test_candidate_dispersion_detects_identical_vs_diverse() -> None:
    from novel_system.services.literary_quality import candidate_dispersion

    same = ["She opened the door." * 3, "She opened the door." * 3]
    assert candidate_dispersion(same) == 0.0

    diverse = [
        "She ran through the fog, choosing to reveal the hidden letters.",
        "He opened the safe and left the key on the windowsill.",
    ]
    assert candidate_dispersion(diverse) > 0.0
