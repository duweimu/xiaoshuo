"""Tests-only accounted online provider doubles for real-runtime workflows."""

from __future__ import annotations

import json

from novel_system.services.llm_client import LLMRequest, LLMResponse
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.near_final import (
    CHAPTER_ACCEPTANCE_SCORE_KEYS,
    SCENE_ACCEPTANCE_SCORE_KEYS,
)
from tests.accounted_llm_fakes import AccountedGenerateMixin

# 场景运行管线在这些模块里按 `LLMNodeRunner(session)`（无显式 client）取默认运行器。
# API 驱动的整链场景运行统一把这些默认运行器替换为在线记账替身。
_SCENE_PIPELINE_RUNNER_MODULES = (
    "novel_system.services.orchestrator",
    "novel_system.services.scene_blueprint",
    "novel_system.services.scene_generation",
    "novel_system.services.qc_engine",
    "novel_system.services.near_final",
)


def install_online_pipeline(monkeypatch) -> None:
    """假生成已退役：把场景管线各模块的默认 LLMNodeRunner 注入在线记账替身。

    注入 OnlineAccountedExecution 替身即绕过 llm_enabled 闸；显式注入了 client 的
    运行器不受影响（工厂只在 llm_client 缺省时兜底）。"""

    def _runner_factory(session, *, llm_client=None, **kwargs):
        if llm_client is None:
            llm_client = ScenePipelineOnlineFake()
        return LLMNodeRunner(session, llm_client=llm_client, **kwargs)

    for module in _SCENE_PIPELINE_RUNNER_MODULES:
        monkeypatch.setattr(f"{module}.LLMNodeRunner", _runner_factory)


class AuthorNodeOnlineFake(AccountedGenerateMixin):
    """作者稿 AI 建议 / 结构反提取的在线记账替身（按 node_id 派发）。

    单个 candidate_brief 同时带 scene/chapter 字段与 project 的 snowflake_steps——
    结构反提取归一化按 object_type 各取所需、忽略无关键，故三类 draft 共用同一替身。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        node_id = request.node_id
        if node_id == "author_proposal_generate":
            # 把作者指令上下文回显进 rationale，让「rationale 复述指令」类断言成立。
            user_text = " ".join(
                str(message.get("content", ""))
                for message in (request.messages or [])
                if message.get("role") == "user"
            )
            payload = {
                "content": "在线替身：保留作者的场景，但把选择的代价显性化。",
                "rationale": f"遵循作者指令并规避被否决的套路。作者指令上下文：{user_text}",
            }
        elif node_id == "author_structure_extract":
            step_body = {
                "summary": "样例步骤：占位内容。",
                "notes": ["占位要点一", "占位要点二"],
            }
            payload = {
                "candidate_brief": {
                    "character_desire": "主角想立刻查清那晚的真相。",
                    "reader_question": "袖口里的东西会不会被发现？",
                    "obstacle": "对方守着关键物件不肯松口。",
                    "choice_under_pressure": "是当场拆穿，还是暂时压下。",
                    "core_promise": "真相与保护不能同时兑现。",
                    "plot_movement": "旧信把主角带回事发地。",
                    "character_shift": "从回避转向承担代价。",
                    "chapter_question": "谁在暗处盯着？",
                    "ending_aftertaste": "真相是新的风险，而不是终点。",
                    "snowflake_steps": {
                        "book_brief": dict(step_body),
                        "one_sentence_summary": dict(step_body),
                        "one_paragraph_summary": dict(step_body),
                        "scene_list": dict(step_body),
                        "scene_details": dict(step_body),
                    },
                },
                "uncertainty_notes": [],
                "rationale": "从作者稿反向提取戏剧意图。",
            }
        else:
            raise AssertionError(f"unexpected author-node online request: {node_id}")
        return _response(request, payload, len(self.requests))


_AUTHOR_NODE_RUNNER_MODULES = (
    "novel_system.services.author_drafts",
    "novel_system.services.projects",
)


def install_online_author_pipeline(monkeypatch) -> None:
    """把作者稿相关模块的默认 LLMNodeRunner 注入 AuthorNodeOnlineFake。

    在线替身绕过 llm_enabled 闸，故与雪花骨架直通（保持 LLM 关闭）可共存于同一文件。"""

    def _runner_factory(session, *, llm_client=None, **kwargs):
        if llm_client is None:
            llm_client = AuthorNodeOnlineFake()
        return LLMNodeRunner(session, llm_client=llm_client, **kwargs)

    for module in _AUTHOR_NODE_RUNNER_MODULES:
        monkeypatch.setattr(f"{module}.LLMNodeRunner", _runner_factory)


class WriterNodeOnlineFake(AccountedGenerateMixin):
    """作家诊断/修订、深评、passage-patch 的在线记账替身，复刻退役离线载荷的既定形状。"""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        from novel_system.services.writer_deep_review import LITERARY_REVISION_DIMENSIONS
        from novel_system.services.writer_review import ALL_WRITER_REVIEW_DIMENSIONS

        self.requests.append(request)
        node_id = request.node_id or ""
        if node_id.endswith("_diagnosis"):
            scores = {dim: 0.5 for dim in ALL_WRITER_REVIEW_DIMENSIONS}
            scores.update({"continuity": 0.62, "scene_necessity": 0.58, "reader_hook": 0.56})
            target_label = "章节" if "_chapter_" in node_id else "场景"
            payload = {
                "overall_score": 0.54,
                "scores": scores,
                "findings": [
                    {
                        "dimension": "writer_diagnosis_payload",
                        "severity": "info",
                        "issue": f"在线记账{target_label}诊断（测试替身）。",
                        "recommendation": "以正文证据给出专业评审。",
                        "evidence_excerpt": "accounted online test diagnosis",
                        "evidence_location": "accounted online test diagnosis",
                        "why_it_matters": "确保作家评审链路闭环。",
                    }
                ],
                "revision_brief": [
                    {"dimension": "reader_hook", "action": "检查结尾是否留下新的选择、风险或追问。", "priority": "medium"}
                ],
                "requires_human_review": True,
            }
        elif node_id == "writer_scene_revision":
            payload = {
                "revised_text": "【作家修订候选】她把证据分成两份，把结尾问题留给读者。",
                "diff_summary": "在线记账候选（测试替身）。",
                "changed_dimensions": ["turn", "reader_hook"],
                "rewrite_strategy": "online_full_scene_rewrite",
            }
        elif node_id == "writer_chapter_revision":
            payload = {
                "revision_plan": ["检查每场是否都有明确选择、阻碍和结尾钩子。", "兑现本章承诺。"],
                "selected_rewrite_passages": [],
                "diff_summary": "在线记账章节候选（测试替身）。",
                "changed_dimensions": ["scene_necessity", "ending_drive"],
                "rewrite_strategy": "online_revision_plan",
            }
        elif node_id == "writer_deep_review":
            payload = {
                "overall_score": 0.65,
                "scores": {dim: 0.65 for dim in LITERARY_REVISION_DIMENSIONS},
                "findings": [],
                "revision_brief": [],
                "requires_human_review": False,
                "lens_evaluations": [],
            }
        elif node_id == "writer_passage_patch":
            source_excerpt = _extract_prompt_marker(request, "Source Excerpt:") or "占位原句。"
            target_ref = _extract_prompt_marker(request, "Target Text Ref:") or "ref-scene"
            patch_common = {
                "target_text_ref": target_ref,
                "source_excerpt": source_excerpt,
                "patch_type": "replace_excerpt",
            }
            payload = {
                "patches": [
                    {
                        **patch_common,
                        "tone": "shorter",
                        "label": "更短",
                        "replacement_text": "她按住证据，没有解释。",
                        "changed_dimensions": ["information_rhythm"],
                        "why_it_helps": "压掉解释余量，让动作自己承担压力。",
                    },
                    {
                        **patch_common,
                        "tone": "sharper",
                        "label": "更狠",
                        "replacement_text": "她收回手，话到嘴边又咽了回去。",
                        "changed_dimensions": ["relationship_tension"],
                        "why_it_helps": "让动作后果承担锋利感。",
                    },
                    {
                        **patch_common,
                        "tone": "subtler",
                        "label": "更含蓄",
                        "replacement_text": "她把证据分成两份，先看了一眼门缝。",
                        "changed_dimensions": ["dialogue_subtext"],
                        "why_it_helps": "把明说转为回避，留出读者判断空间。",
                    },
                ],
                "rationale": "在线记账 passage patch（测试替身）。",
                "manual_only": True,
            }
        else:
            raise AssertionError(f"unexpected writer-node online request: {node_id}")
        return _response(request, payload, len(self.requests))


def _extract_prompt_marker(request: LLMRequest, marker: str) -> str | None:
    for message in request.messages or []:
        content = str(message.get("content", ""))
        idx = content.find(marker)
        if idx < 0:
            continue
        tail = content[idx + len(marker):].lstrip("\n ")
        line = tail.split("\n", 1)[0].strip()
        if line:
            return line
    return None


_WRITER_NODE_RUNNER_MODULES = (
    "novel_system.services.writer_review",
    "novel_system.services.writer_deep_review",
)


def install_online_writer_pipeline(monkeypatch) -> None:
    """把作家评审/深评/passage-patch 模块的默认 LLMNodeRunner 注入 WriterNodeOnlineFake。"""

    def _runner_factory(session, *, llm_client=None, **kwargs):
        if llm_client is None:
            llm_client = WriterNodeOnlineFake()
        return LLMNodeRunner(session, llm_client=llm_client, **kwargs)

    for module in _WRITER_NODE_RUNNER_MODULES:
        monkeypatch.setattr(f"{module}.LLMNodeRunner", _runner_factory)


def install_skeleton_snowflake(monkeypatch) -> None:
    """假生成已退役：把雪花 generate_step 整体替换成「规划器骨架直通」。

    只回归物化/失效/收口链路、不关心生成质量的雪花用例用它。整体替换 generate_step
    即绕过内部 _run_structured_task 的 llm_enabled 闸，故**不**设 NOVEL_SYSTEM_LLM_ENABLED
    ——留 LLM 关闭，让同文件里的诚实回退用例（场景急救/驻场教练 fallback）继续走 fallback 分支。"""
    from novel_system.services.hash_engine import normalize
    from novel_system.services.snowflake_planner import SnowflakePlannerService
    from novel_system.services.snowflake_workspace_llm import (
        SnowflakeWorkspaceLLMService,
        WorkspaceLLMResult,
    )
    from novel_system.settings import get_settings

    original_generate_step = SnowflakeWorkspaceLLMService.generate_step

    def fake_generate_step(self, *, project, step_key, latest_by_step, **kwargs):
        # 显式设了 llm_enabled 的「live」用例自带 LLM 替身，委托真实 generate_step 走它们的
        # 设定；LLM 关闭的物化/失效用例才用规划器骨架直通。
        if get_settings().llm_enabled:
            return original_generate_step(
                self, project=project, step_key=step_key, latest_by_step=latest_by_step, **kwargs
            )
        payload = SnowflakePlannerService(self.session)._build_artifact_json(
            project, step_key, dict(latest_by_step)
        )
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=normalize(payload))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "generate_step", fake_generate_step)


def _response(request: LLMRequest, payload: dict, sequence: int) -> LLMResponse:
    request_id = f"test-online-{request.node_id}-{sequence}"
    # 与各测试常用 fake 的单发用量对齐（input 60 + output 18 = 78），
    # 使 test_scene_token_budget 的「每次调用都计入、总量为 CALL_TOKENS 整数倍」不变量成立。
    usage = {"input_tokens": 60, "output_tokens": 18, "total_tokens": 78}
    return LLMResponse(
        request_id=request_id,
        provider="test-online-provider",
        model="test-online-model",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "test-online-model",
            "usage": usage,
            "finish_reason": "stop",
        },
        usage=usage,
        finish_reason="stop",
    )


def _extract_required_terms(request: LLMRequest) -> str | None:
    """从重写 prompt 的 canonical_json 里取 preserve_required_terms（必含要素）。"""
    import re as _re

    for message in request.messages or []:
        content = str(message.get("content", ""))
        match = _re.search(r'"preserve_required_terms"\s*:\s*"([^"]*)"', content)
        if match and match.group(1).strip():
            return match.group(1)
    return None


class ScenePipelineOnlineFake(AccountedGenerateMixin):
    """Schema-valid online double; dispatch still crosses the accounting hook."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        node_id = request.node_id
        if node_id == "scene_blueprint":
            payload = {
                "visible_desire": "secure the immediate objective",
                "forced_choice": "advance or concede",
                "price_paid": "trust becomes harder to recover",
                "information_release": "the hidden constraint becomes visible",
                "relationship_turn": "caution shifts into conditional trust",
                "image_anchor": "a lamp reflected in wet stone",
                "ending_action": "the door closes behind them",
                "next_scene_pull": "the consequence follows immediately",
                "anti_summary_rule": "end on physical action",
            }
        elif node_id == "chapter_story_architecture":
            payload = {
                "chapter_promise": "the scene must change the available choices",
                "escalation_path": ["pressure appears", "a choice narrows", "a cost lands"],
                "reveal_plan": ["the governing constraint is exposed"],
                "payoff_target": "the chosen action creates the next problem",
                "character_shift": "certainty gives way to costly resolve",
                "ending_question": "what will the choice cost next",
            }
        elif node_id == "character_pressure_blueprint":
            payload = {
                "surface_goal": "finish the immediate task",
                "hidden_fear": "the choice will expose a weakness",
                "wrong_belief": "control can prevent every loss",
                "shame_point": "asking for help feels like surrender",
                "avoidance_strategy": "delay the irreversible choice",
                "relationship_debt": "an old promise remains unpaid",
                "current_mask": "measured confidence",
            }
        elif node_id in {"near_final_acceptance_review", "chapter_near_final_review"}:
            score_keys = (
                SCENE_ACCEPTANCE_SCORE_KEYS
                if node_id == "near_final_acceptance_review"
                else CHAPTER_ACCEPTANCE_SCORE_KEYS
            )
            payload = {
                "near_final_status": "near_final_ready",
                "pass_flag": True,
                "overall_score": 0.8,
                "scores": {key: 0.8 for key in score_keys},
                "findings": [],
                "revision_brief": [],
                "failure_class": "",
                "requires_human_review": False,
            }
        elif node_id == "hard_qc":
            payload = {
                "resolution_code": "hard_pass",
                "pass_flag": True,
                "next_action": "pass",
                "issues": [],
                "rewrite_brief": [],
            }
        elif node_id == "soft_qc":
            payload = {
                "resolution_code": "soft_pass",
                "pass_flag": True,
                "next_action": "pass",
                "issues": [],
                "rewrite_brief": [],
                "carry_forward_note": False,
                "note_scope": None,
                "carry_note_text": None,
            }
        elif node_id in {
            "neutral_draft",
            "style_draft",
            "style_patch",
            "scene_auto_rewrite",
            "scene_literary_rewrite",
        }:
            scene_text = (
                f"Accounted online test draft {len(self.requests)}: she chooses to open "
                "the sealed letter, risking the trust she meant to protect. She hands "
                "the evidence to her ally and leaves through the locked door."
            )
            if node_id in {"scene_auto_rewrite", "scene_literary_rewrite"}:
                # 重写节点必须保留必含要素（否则最终文本连续性门拦截候选）——从 prompt 的
                # preserve_required_terms 回显。只对重写节点做，neutral/style 保持不含，
                # 以免 test_scene_generation 的「缺必含→拦归档」用例被破坏。
                required = _extract_required_terms(request)
                if required:
                    scene_text = f"{scene_text} 保留要素：{required}。"
            payload = {"scene_text": scene_text, "continuity_notes": []}
        else:
            raise AssertionError(f"unexpected online test request: {node_id}")
        return _response(request, payload, len(self.requests))
