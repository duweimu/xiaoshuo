from __future__ import annotations

import json

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    GenerationPlanningArtifact,
    LlmCall,
    RevisionCandidate,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    VoiceProfile,
    WriterEvaluation,
)
from novel_system.services.llm_client import LLMRequest, LLMResponse, OnlineAccountedExecution
from novel_system.services.near_final import (
    NEAR_FINAL_RUBRIC_ID,
    NearFinalAcceptanceService,
    NearFinalPlanningService,
)
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_generation import SceneGenerationService
from tests.real_llm_fakes import ScenePipelineOnlineFake


CHAPTER_ID = "CHNF01"
SCENE_ID = "CHNF01_SC01"
PROJECT_ID = "PROJECT_NF01"


class SequencedClient(OnlineAccountedExecution):
    def __init__(self, payloads: list[dict], *, provider: str = "test-provider", model: str = "test-model") -> None:
        self.payloads = list(payloads)
        self.provider = provider
        self.model = model
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self.payloads:
            raise AssertionError(f"unexpected request for {request.node_id}")
        self.requests.append(request)
        payload = self.payloads.pop(0)
        return LLMResponse(
            request_id=f"req_{request.node_id}_{len(self.requests)}",
            provider=self.provider,
            model=self.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format=request.response_format,
            raw_response={
                "id": f"req_{request.node_id}_{len(self.requests)}",
                "model": self.model,
                "usage": {"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
                "finish_reason": "stop",
            },
            usage={"input_tokens": 40, "output_tokens": 20, "total_tokens": 60},
            finish_reason="stop",
        )

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


def _seed_scene(session, *, is_chapter_last: int = 0) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="Near final test project", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="林岑必须决定是否公开盐钟证据，同时保护幸存者。",
            main_plot_push="把盐钟线索推进到可验证的公开风险。",
            emotional_target="让林岑从冷静修复者变成承担代价的人。",
            ending_effect="第二枚盐钟影子出现，留下追踪者问题。",
            writer_brief_json={
                "core_promise": "证据公开与保护活人不能同时成立。",
                "plot_movement": "录音带证明官方记录被篡改。",
                "character_shift": "林岑从只追真相转向承担保护代价。",
                "chapter_question": "谁在追踪幸存者？",
                "ending_aftertaste": "真相不是胜利，而是新的暴露风险。",
            },
        )
    )
    session.add(ChapterState(chapter_id=CHAPTER_ID, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            pov_character_id="林岑",
            onstage_chars_json=["林岑", "许望", "幸存者阿砚"],
            location="无灯船坞",
            scene_goal="林岑打开隐藏档案柜，发现公开证据会暴露幸存者。",
            beats_json=["盐钟残片开锁", "录音带揭示幸存者", "公开与保护的选择", "第二枚盐钟影子出现"],
            must_include_text="林岑把录音带分成两份。",
            exit_change="林岑选择先转移幸存者，再公开真相。",
            hook="雾墙上浮现第二枚盐钟的影子。",
            writer_brief_json={
                "character_desire": "林岑想立刻公开能证明篡改的录音。",
                "obstacle": "录音也会暴露幸存者阿砚的位置。",
                "stakes": "公开证据会换来真相，也可能让阿砚被追踪者找到。",
                "secret_or_misunderstanding": "许望知道追踪者离船坞更近。",
                "subtext": "两人争的不是证据，而是谁承担暴露活人的责任。",
                "irreversible_change": "林岑把证据拆分，亲手延后公开真相。",
                "reader_question": "第二枚盐钟是谁留下的？",
                "choice_under_pressure": "公开证据或保护阿砚，林岑不能同时做到。",
                "power_shift": "许望从提醒者变成被托付证据的人。",
                "new_information": "阿砚还活着且正在被追踪。",
                "emotional_turn": "林岑第一次把保护活人置于公开真相之前。",
                "image_anchor": "盐钟残片硌进掌心。",
                "reader_aftertaste": "真相被分成两份，危险也被分成两份。",
            },
            target_length_band="short",
            scene_type="revelation_scene",
            is_chapter_last=is_chapter_last,
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.add(
        VoiceProfile(
            row_id="voice_profile_lincen_v1",
            voice_profile_id="VOICE_林岑",
            version=1,
            character_id="林岑",
            content="林岑的叙述声线克制、冷静，但在选择代价时会显出迟疑。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.commit()


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


def _near_final_pass() -> dict:
    return {
        "near_final_status": "near_final_ready",
        "pass_flag": True,
        "overall_score": 0.86,
        "scores": {
            "story_necessity": 0.88,
            "character_pressure": 0.86,
            "dialogue_edge": 0.8,
            "information_release": 0.84,
            "prose_freshness": 0.82,
            "ending_drive": 0.9,
            "continuity": 0.92,
            "reference_safety": 1.0,
        },
        "findings": [],
        "revision_brief": [],
        "failure_class": None,
        "requires_human_review": False,
    }


def _near_final_fail(*, failure_class: str = "prose_model_voice") -> dict:
    return {
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": 0.58,
        "scores": {
            "story_necessity": 0.7,
            "character_pressure": 0.52,
            "dialogue_edge": 0.48,
            "information_release": 0.72,
            "prose_freshness": 0.42,
            "ending_drive": 0.5,
            "continuity": 0.9,
            "reference_safety": 1.0,
        },
        "findings": [
            {
                "dimension": "dialogue_edge",
                "severity": "revision",
                "issue": "对白只传达信息，没有互相试探。",
                "recommendation": "让许望用一句反问迫使林岑承认代价。",
                "evidence_excerpt": "我们得保护阿砚。",
                "evidence_location": "结尾前",
                "why_it_matters": "准定稿需要让关系压力落在话语和动作上。",
            }
        ],
        "revision_brief": [
            {
                "dimension": "dialogue_edge",
                "action": "重写最后三分之一，让选择、代价和关系转移同步发生。",
                "priority": "high",
            }
        ],
        "failure_class": failure_class,
        "requires_human_review": False,
    }


def test_prompt_builder_includes_near_final_planning_sections() -> None:
    snapshot = {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": SCENE_ID,
        "chapter_id": CHAPTER_ID,
        "source_version_refs": {},
        "resolved_ref_ids": {},
        "ordered_injections": [],
        "inline_digests": {
            "chapter_goal": "本章承诺公开证据与保护幸存者的冲突。",
            "scene_card": "林岑必须拆分录音带。",
            "character_pressure": json.dumps({"wrong_belief": "真相必须立刻公开"}, ensure_ascii=False),
            "chapter_story_architecture": json.dumps({"ending_question": "第二枚盐钟是谁留下的？"}, ensure_ascii=False),
        },
    }

    payload = PromptBuilder().build(snapshot, "neutral_draft")
    rewrite_payload = PromptBuilder().build(snapshot, "scene_literary_rewrite")

    assert "## Character Pressure Blueprint" in payload["user_prompt"]
    assert "真相必须立刻公开" in payload["user_prompt"]
    assert "## Chapter Story Architecture" in payload["user_prompt"]
    assert "第二枚盐钟是谁留下的？" in payload["user_prompt"]
    assert "Character Pressure Blueprint" in rewrite_payload["user_prompt"]


def test_planning_service_persists_character_pressure_and_chapter_architecture(session) -> None:
    _seed_scene(session)
    planning_client = SequencedClient(
        [
            {
                "chapter_promise": "公开证据与保护幸存者互相冲突。",
                "escalation_path": ["发现录音", "确认追踪者", "拆分证据"],
                "reveal_plan": ["阿砚还活着", "追踪者正在靠近"],
                "payoff_target": "林岑必须亲手延后公开。",
                "character_shift": "林岑从真相优先转向保护活人。",
                "ending_question": "第二枚盐钟是谁留下的？",
            },
            {
                "surface_goal": "公开录音证明篡改。",
                "hidden_fear": "如果延后公开，她会变成帮凶。",
                "wrong_belief": "真相只要公开就能保护所有人。",
                "shame_point": "她曾经只修复档案，没有保护档案里的人。",
                "avoidance_strategy": "用冷静术语避开活人的求救。",
                "relationship_debt": "她必须把一半证据交给许望并承担信任风险。",
                "current_mask": "档案修复师的冷静。",
            },
        ]
    )

    result = NearFinalPlanningService(session, llm_client=planning_client).ensure_scene_planning(SCENE_ID)
    session.commit()

    artifacts = session.execute(select(GenerationPlanningArtifact)).scalars().all()
    assert {artifact.artifact_type for artifact in artifacts} == {
        "chapter_story_architecture",
        "character_pressure_blueprint",
    }
    assert result["character_pressure"]["payload"]["wrong_belief"] == "真相只要公开就能保护所有人。"
    assert result["chapter_architecture"]["payload"]["ending_question"] == "第二枚盐钟是谁留下的？"
    assert [request.node_id for request in planning_client.requests] == [
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]


def test_near_final_acceptance_blocks_scene_without_choice_cost_or_ending_action(session) -> None:
    _seed_scene(session)
    service = NearFinalAcceptanceService(session, llm_client=SequencedClient([_near_final_pass()]))
    content = "林岑来到船坞。她知道真相很重要。事情从此不同。"

    result = service.evaluate_scene(
        SCENE_ID,
        bundle={"bundle_id": "bundle_nf", "bundle_snapshot_hash": "hash_nf", "snapshot": {"inline_digests": {}}},
        source_draft_row_id="draft_nf",
        source_content=content,
    )
    session.commit()

    evaluation = session.execute(select(WriterEvaluation)).scalars().one()
    candidate = session.execute(select(RevisionCandidate)).scalars().one()
    assert result["near_final_status"] == "revision_required"
    assert result["failure_class"] == "scene_structure_failure"
    assert evaluation.rubric_id == NEAR_FINAL_RUBRIC_ID
    assert evaluation.requires_human_review == 0
    assert candidate.revision_type == "near_final_scene_rewrite"
    assert candidate.apply_mode == "manual_or_regenerate"
    assert "选择" in candidate.instruction_json[0]["action"]


def test_near_final_acceptance_blocks_model_voice_even_when_scene_machinery_exists(session) -> None:
    _seed_scene(session)
    service = NearFinalAcceptanceService(session, llm_client=SequencedClient([_near_final_pass()]))
    content = (
        "林岑决定公开录音还是保护阿砚，她把证据分成两份交给许望，自己藏起另一份。"
        "某种意义上，这一切都变得非常重要。她知道真相必须被看见，于是解释了所有前因后果。"
        "最后她转身看见雾墙亮起。"
    )

    result = service.evaluate_scene(
        SCENE_ID,
        bundle={"bundle_id": "bundle_voice", "bundle_snapshot_hash": "hash_voice", "snapshot": {"inline_digests": {}}},
        source_draft_row_id="draft_voice",
        source_content=content,
    )
    session.commit()

    evaluation = session.execute(select(WriterEvaluation)).scalars().one()
    candidate = session.execute(select(RevisionCandidate)).scalars().one()
    assert result["near_final_status"] == "revision_required"
    assert result["failure_class"] == "prose_model_voice"
    assert result["pass_flag"] is False
    assert result["scores"]["model_voice_risk"] <= 0.4
    assert any(finding["dimension"] == "model_voice_risk" for finding in evaluation.findings_json)
    assert any(item["dimension"] == "model_voice_risk" for item in candidate.instruction_json)


def test_orchestrator_archives_only_after_near_final_acceptance(session) -> None:
    _seed_scene(session)
    planning_client = ScenePipelineOnlineFake()
    scene_client = SequencedClient(
        [
            {"scene_text": "林岑想公开录音，但录音会暴露阿砚。她把录音带分成两份，递给许望一份，转身藏起另一份。", "continuity_notes": []},
            {"scene_text": "林岑按住录音带。公开它能证明篡改，也会暴露阿砚。许望问：\"你要真相，还是要活人？\"她把录音带分成两份，一份交给许望，一份藏进船坞石缝，然后转身看见雾墙上的第二枚盐钟。", "style_notes": []},
        ]
    )
    near_final_client = SequencedClient([_near_final_pass()])
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client),
        hard_qc_engine=HardQcEngine(session, llm_client=SequencedClient([_hard_pass()])),
        soft_qc_engine=SoftQcEngine(session, llm_client=SequencedClient([_soft_pass()])),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
        near_final_service=NearFinalAcceptanceService(session, llm_client=near_final_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(
        session, llm_client=ScenePipelineOnlineFake()
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    final_scene = session.execute(select(FinalScene)).scalars().one()
    evaluation = session.execute(select(WriterEvaluation).where(WriterEvaluation.rubric_id == NEAR_FINAL_RUBRIC_ID)).scalars().one()
    llm_steps = [row.step for row in session.execute(select(LlmCall).order_by(LlmCall.created_at.asc())).scalars().all()]
    assert result["scene_status"] == "archived"
    assert result["near_final"]["near_final_status"] == "near_final_ready"
    # Wave 1 词表统一：归档事务把 FinalScene 置权威 archived 态（建行时为 near_final_ready）
    assert final_scene.status == "archived"
    assert evaluation.overall_score == 0.86
    assert "near_final_acceptance_review" in llm_steps
    assert "style_draft" in llm_steps


def test_orchestrator_runs_one_full_literary_rewrite_when_near_final_review_fails(session) -> None:
    _seed_scene(session)
    # This case intentionally exercises the full planning + QC + near-final
    # rewrite path.  Give it an explicit author-approved test budget so the
    # CJK-aware conservative estimator tests the workflow rather than the
    # separate budget-exhaustion branch.
    state = session.get(SceneRunState, SCENE_ID)
    state.scene_token_budget = 250_000
    state.attempt_budget = 20
    state.provider_attempt_budget = 20
    session.commit()
    planning_client = ScenePipelineOnlineFake()
    scene_client = SequencedClient(
        [
            {
                "scene_text": "林岑来到船坞，说明证据很重要。林岑把录音带分成两份。",
                "continuity_notes": [],
            },
            {
                "scene_text": "林岑必须选择：公开证据，还是隐瞒真相保护阿砚；两者不能同时做到。她决定承担隐瞒的代价。林岑把录音带分成两份。",
                "style_notes": [],
            },
            {
                "scene_text": "林岑按住录音带。公开它能证明篡改，也会暴露阿砚。许望问：\"你要真相，还是要活人？\"林岑把录音带分成两份。一份交给许望，一份藏进船坞石缝，然后转身看见雾墙上的第二枚盐钟。",
                "style_notes": [],
            },
        ]
    )
    near_final_client = SequencedClient([_near_final_fail(), _near_final_pass()])
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=scene_client),
        hard_qc_engine=HardQcEngine(session, llm_client=SequencedClient([_hard_pass()])),
        soft_qc_engine=SoftQcEngine(
            session,
            llm_client=SequencedClient([_soft_pass(), _soft_pass()]),
        ),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
        near_final_service=NearFinalAcceptanceService(session, llm_client=near_final_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(
        session, llm_client=ScenePipelineOnlineFake()
    )

    result = orchestrator.run_scene(SCENE_ID)
    session.commit()

    drafts = session.execute(select(SceneDraft).order_by(SceneDraft.created_at.asc(), SceneDraft.row_id.asc())).scalars().all()
    candidates = session.execute(select(RevisionCandidate).order_by(RevisionCandidate.created_at.asc())).scalars().all()
    evaluations = session.execute(
        select(WriterEvaluation).where(WriterEvaluation.rubric_id == NEAR_FINAL_RUBRIC_ID).order_by(WriterEvaluation.created_at.asc())
    ).scalars().all()
    assert result["scene_status"] == "archived"
    assert result["near_final"]["rewrite_count"] == 1
    assert [draft.stage for draft in drafts] == ["neutral_draft", "style_draft", "near_final_rewrite"]
    assert len(candidates) == 1
    assert candidates[0].revision_type == "near_final_scene_rewrite"
    assert candidates[0].status == "superseded"
    assert [evaluation.overall_score for evaluation in evaluations] == [0.58, 0.86]


def test_chapter_near_final_review_blocks_missing_payoff(session) -> None:
    _seed_scene(session, is_chapter_last=1)
    session.add(
        ChapterMemory(
            row_id=f"chapter_memory_final_{CHAPTER_ID}_v1",
            chapter_id=CHAPTER_ID,
            aggregate_stage="final",
            content="林岑公开了录音。没有回收第二枚盐钟，也没有回答阿砚为什么被追踪。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.commit()
    chapter_client = SequencedClient(
        [
            {
                "near_final_status": "revision_required",
                "pass_flag": False,
                "overall_score": 0.6,
                "scores": {
                    "chapter_promise": 0.52,
                    "escalation": 0.65,
                    "payoff_integrity": 0.4,
                    "character_shift": 0.62,
                    "ending_drive": 0.55,
                    "continuity": 0.9,
                },
                "findings": [
                    {
                        "dimension": "payoff_integrity",
                        "severity": "revision",
                        "issue": "第二枚盐钟没有回收或转化。",
                        "recommendation": "在章末给出新的行动钩子或明确延宕理由。",
                        "evidence_excerpt": "没有回收第二枚盐钟",
                        "evidence_location": "chapter ending",
                        "why_it_matters": "章节准定稿不能只让单场成立，还要兑现本章承诺。",
                    }
                ],
                "revision_brief": [
                    {"dimension": "payoff_integrity", "action": "补写第二枚盐钟的回收或延宕契约。", "priority": "high"}
                ],
                "failure_class": "chapter_payoff_gap",
                "requires_human_review": False,
            }
        ]
    )

    result = NearFinalAcceptanceService(session, llm_client=chapter_client).evaluate_chapter(CHAPTER_ID)
    session.commit()

    evaluation = session.execute(select(WriterEvaluation).where(WriterEvaluation.object_type == "chapter")).scalars().one()
    assert result["near_final_status"] == "revision_required"
    assert result["failure_class"] == "chapter_payoff_gap"
    assert evaluation.rubric_id == NEAR_FINAL_RUBRIC_ID
    assert evaluation.findings_json[0]["dimension"] == "payoff_integrity"
    llm_call = session.get(LlmCall, evaluation.evaluator_llm_call_id)
    assert llm_call.scope_type == "chapter"
    assert llm_call.scope_id == CHAPTER_ID
    assert llm_call.project_id == PROJECT_ID
    assert llm_call.chapter_id == CHAPTER_ID
    assert llm_call.scene_id is None
