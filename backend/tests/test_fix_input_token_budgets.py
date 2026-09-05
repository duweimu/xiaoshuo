"""Input-token budgets for the scene / planning / chapter prompt families.

Regression for a systemic fail-closed: ``context_budget.estimate_tokens`` charges one
token per CJK character, but the scene-run / writer-review / author-draft templates kept
the pre-estimator ``input_token_budget`` values (1800–3600). Ordinary Chinese prose then
tripped ``CONTINUITY_BUDGET_EXCEEDED`` at the runner's ``finalize_request_budget`` gate:
a 900-char draft at ``near_final_acceptance_review`` (est 3109 > 3000), a 726-char
chapter at ``writer_chapter_revision`` (> 3600), any author draft over ~900 chars at
``author_structure_extract`` (> 2600). The budgets were re-measured with realistic Chinese
payloads (see the comment block above ``neutral_draft`` in ``config/prompts.yaml``) and the
same numbers are floored in ``prompt_builder.RUNTIME_MIN_INPUT_BUDGETS`` so a live install
whose DB prompts snapshot still carries the old numbers is protected too.

Every prompt here is composed the way the real call site composes it (base prompt from
``PromptBuilder`` + the draft + call-site instructions) and then checked with the very
function the runner uses, ``finalize_request_budget``.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    LlmCall,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services import author_drafts as author_drafts_module
from novel_system.services import near_final as near_final_module
from novel_system.services import qc_engine as qc_engine_module
from novel_system.services import writer_review as writer_review_module
from novel_system.services.context_budget import SECTION_SPECS, estimate_tokens, finalize_request_budget
from novel_system.services.llm_client import (
    LLMRequest,
    LLMResponse,
    ModelRoutingConfig,
    TaskModelConfig,
)
from novel_system.services.llm_task_runner import LLMNodeContinuityError, LLMNodeRunner
from novel_system.services.prompt_builder import (
    CHAPTER_INPUT_TOKEN_BUDGET,
    PLANNING_INPUT_TOKEN_BUDGET,
    RUNTIME_MIN_INPUT_BUDGETS,
    SCENE_INPUT_TOKEN_BUDGET,
    SCENE_INPUT_TOKEN_BUDGET_ENV,
    PromptBuilder,
    PromptConfigurationError,
    load_prompt_templates,
)
from novel_system.services.scene_generation import SceneGenerationService
from tests.accounted_llm_fakes import AccountedGenerateMixin

REPO_PROMPTS = Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"

# 旧值：估算器改成「一个汉字一 token」之前，这些模板在 prompts.yaml 里的 input_token_budget。
# 库里有活动 prompts 快照的实装现在仍带着它们——地板必须把这些数字顶上去。
STALE_BUDGETS = {
    "near_final_acceptance_review": 2600,
    "writer_scene_revision": 2600,
    "writer_chapter_revision": 3600,
    "author_structure_extract": 2600,
    "hard_qc": 2200,
    "style_draft": 2600,
}

_SENTENCES = (
    "她把那封信摊在修复台上，纸角已经被潮气泡得发软，字迹却仍然倔强地站着。",
    "「你父亲当年也是这样，」老周说，「什么都不肯说，只把东西留给后来的人。」",
    "窗外的雨没有停，档案馆的走廊里回荡着水滴落进铁桶的声音，一下，又一下。",
    "林岑没有回答，她的指尖在残片边缘停了很久，像是在确认某种只有她自己能读出的纹路。",
    "「盐钟箱的湿度是谁调的？」她终于开口，声音比自己预想的要稳。",
    "老周把眼镜摘下来，慢慢地擦，擦到镜片上再没有一点雾气才重新戴上。",
    "走廊尽头的灯闪了两下，然后彻底暗了，只剩下修复台上那盏台灯还亮着。",
    "她把残片翻过来，背面有一道极细的划痕，像是有人用指甲刻过一个字，又擦掉了。",
    "门在他身后合上，没有发出声音，只有台灯的光晃了一下。",
)
DRAFT_MARKER = "这一句只在稿件正文里出现一次，用来数稿件被放进提示词的次数。"


def _prose(target_chars: int, *, seed: int) -> str:
    """现实体量的中文正文：多段、有对话，字符数≈估算 token 数。"""
    rng = random.Random(seed)
    paragraphs: list[str] = []
    total = 0
    while total < target_chars:
        paragraph = "".join(rng.choice(_SENTENCES) for _ in range(rng.randint(2, 5)))
        paragraphs.append(paragraph)
        total += len(paragraph) + 1
    return "\n".join(paragraphs)[:target_chars]


def _zh(target_chars: int, *, seed: int) -> str:
    return _prose(target_chars, seed=seed).replace("\n", " ")


# 稿件开头放一句标记句，用来数稿件被放进提示词的次数。
SCENE_DRAFT = DRAFT_MARKER + "\n" + _prose(3000, seed=1)
CHAPTER_TEXT = DRAFT_MARKER + "\n" + "\n\n".join(_prose(2500, seed=10 + index) for index in range(6))

# 中等 bundle：一个已跑过蓝图与规划、有风格画像与作者偏好、有记忆摘要的在写项目
# （实测 11.3k–13.3k tok 的那一档）。
_RICH_SECTION_CHARS = {
    "chapter_goal": 60,
    "scene_card": 300,
    "chapter_writer_brief": 260,
    "scene_writer_brief": 150,
    "author_instruction": 120,
    "scene_blueprint": 700,
    "character_pressure": 500,
    "chapter_story_architecture": 800,
    "character_contract": 400,
    "voice_card": 150,
    "style_profile": 800,
    "author_preference_profile": 400,
    "style_rule": 300,
    "banned_rule": 200,
    "foreshadow": 200,
    "similar_scene": 600,
    "style_observation": 300,
    "calibration_line": 200,
    "relation_card": 300,
    "world_rule": 300,
    "scene_memory": 500,
    "scene_summary": 300,
    "chapter_summary": 400,
}


def _rich_scene_snapshot() -> dict:
    digests: dict[str, str] = {}
    seed = 500
    for _name, _label, digest_keys in SECTION_SPECS:
        key = digest_keys[0]
        size = _RICH_SECTION_CHARS.get(key)
        if size:
            seed += 1
            digests[key] = _zh(size, seed=seed)
    return {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "CH001_SC02",
        "chapter_id": "CH001",
        "source_version_refs": {"chapter_goal": "CH001", "scene_card": "CH001_SC02"},
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "chapter_goal", "ref_id": "CH001", "digest_key": "chapter_goal"},
            {"slot": "scene_card", "ref_id": "CH001_SC02", "digest_key": "scene_card"},
        ],
        "inline_digests": digests,
    }


def _chapter_review_snapshot(content: str) -> dict:
    """与 WriterReviewService._chapter_review_bundle 同形：章目标 + 章 brief + 1600 字截断摘要。"""
    return {
        "contract_version": "WRITER_CHAPTER_REVIEW_v1",
        "stage_allowlist_name": "writer_chapter_review",
        "scene_id": "",
        "chapter_id": "CH001",
        "source_version_refs": {"chapter_goal": "CH001", "chapter_writer_brief": "CH001", "source_text_ref": "chapter_memory:x"},
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "chapter_goal", "ref_id": "CH001", "digest_key": "chapter_goal"},
            {"slot": "chapter_writer_brief", "ref_id": "CH001", "digest_key": "chapter_writer_brief"},
            {"slot": "chapter_summary", "ref_id": "chapter_memory:x", "digest_key": "chapter_summary"},
        ],
        "inline_digests": {
            "chapter_goal": _zh(60, seed=901),
            "chapter_writer_brief": json.dumps(
                {key: _zh(60, seed=910 + index) for index, key in enumerate(("core_promise", "plot_movement", "character_shift", "chapter_question", "ending_aftertaste"))},
                ensure_ascii=False,
                sort_keys=True,
            ),
            "chapter_summary": writer_review_module._compact_source_for_prompt(content),
        },
    }


def _diagnosis_payload() -> dict:
    dimensions = list(writer_review_module.ALL_WRITER_REVIEW_DIMENSIONS)
    return {
        "scores": {dimension: 0.62 for dimension in dimensions},
        "findings": [
            {
                "dimension": dimension,
                "severity": "major" if index % 2 else "minor",
                "issue": _zh(60, seed=200 + index),
                "recommendation": _zh(60, seed=220 + index),
                "evidence_excerpt": _zh(40, seed=240 + index),
                "evidence_location": f"第{index + 2}段",
                "why_it_matters": _zh(50, seed=260 + index),
            }
            for index, dimension in enumerate(dimensions[:6])
        ],
        "revision_brief": [
            {"priority": index + 1, "instruction": _zh(70, seed=300 + index), "target_dimension": dimension}
            for index, dimension in enumerate(dimensions[:4])
        ],
        "overall_score": 0.62,
        "requires_human_review": False,
    }


def _final_budget(prompt: dict, final_user_prompt: str) -> dict:
    return finalize_request_budget(
        system_prompt=prompt["system_prompt"],
        user_prompt=final_user_prompt,
        base_budget=prompt["token_budget"],
    )["budget"]


def _assert_fits(prompt: dict, final_user_prompt: str, *, stale_budget: int) -> dict:
    budget = _final_budget(prompt, final_user_prompt)
    # 可证伪：同一份提示词在旧预算下确实会超限，新预算下不再超限。
    assert budget["estimated_input_tokens"] > stale_budget
    assert budget["continuity_warning"] is None, budget["continuity_warning"]
    assert budget["estimated_input_tokens"] <= budget["target_input_tokens"]
    return budget


def _write_stale_prompts(tmp_path: Path) -> Path:
    """一份「界面存过、估算器改版前」的快照：正文取仓库模板，预算是旧值。"""
    repo = load_prompt_templates(REPO_PROMPTS)
    payload = {"templates": {}}
    for name, stale in STALE_BUDGETS.items():
        template = repo[name]
        payload["templates"][name] = {
            "version": template.version,
            "input_token_budget": stale,
            "system_prompt": template.system_prompt,
            "task_prompt": template.task_prompt,
            "structured_schema": template.structured_schema,
        }
    payload["templates"]["project_outline_plan"] = {
        "version": repo["project_outline_plan"].version,
        "input_token_budget": 3000,
        "system_prompt": repo["project_outline_plan"].system_prompt,
        "task_prompt": repo["project_outline_plan"].task_prompt,
        "structured_schema": repo["project_outline_plan"].structured_schema,
    }
    import yaml

    path = tmp_path / "stale_prompts.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 数字本身：仓库文件与代码地板一致；三族各自一个值
# ---------------------------------------------------------------------------


def test_repo_prompt_budgets_match_runtime_floors() -> None:
    templates = load_prompt_templates(REPO_PROMPTS)
    mismatched = {
        name: (templates[name].input_token_budget, floor)
        for name, floor in RUNTIME_MIN_INPUT_BUDGETS.items()
        if templates[name].input_token_budget != floor
    }
    assert mismatched == {}
    assert SCENE_INPUT_TOKEN_BUDGET == 24000
    assert PLANNING_INPUT_TOKEN_BUDGET == 8000
    assert CHAPTER_INPUT_TOKEN_BUDGET == 30000
    for name in ("neutral_draft", "style_draft", "hard_qc", "soft_qc", "near_final_acceptance_review"):
        assert RUNTIME_MIN_INPUT_BUDGETS[name] == SCENE_INPUT_TOKEN_BUDGET
    for name in ("chapter_near_final_review", "writer_deep_review"):
        assert RUNTIME_MIN_INPUT_BUDGETS[name] == CHAPTER_INPUT_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# 场景族：3000 字中文稿 + 中等 bundle
# ---------------------------------------------------------------------------


def test_near_final_acceptance_review_fits_a_3000_char_chinese_draft() -> None:
    prompt = PromptBuilder().build(_rich_scene_snapshot(), "near_final_acceptance_review")
    final_user_prompt = near_final_module._acceptance_user_prompt(prompt["user_prompt"], source_content=SCENE_DRAFT)
    budget = _assert_fits(prompt, final_user_prompt, stale_budget=STALE_BUDGETS["near_final_acceptance_review"])
    assert budget["target_input_tokens"] == SCENE_INPUT_TOKEN_BUDGET
    # 稿件本身就接近旧预算：这正是 900 字都过不去的原因
    assert estimate_tokens(SCENE_DRAFT) > 2900


def test_hard_qc_and_style_draft_fit_a_3000_char_chinese_draft() -> None:
    builder = PromptBuilder()
    snapshot = _rich_scene_snapshot()

    hard_qc = builder.build(snapshot, "hard_qc")
    _assert_fits(
        hard_qc,
        qc_engine_module._qc_build_user_prompt(hard_qc["user_prompt"], SCENE_DRAFT),
        stale_budget=STALE_BUDGETS["hard_qc"],
    )

    style_draft = builder.build(snapshot, "style_draft")
    _assert_fits(
        style_draft,
        SceneGenerationService._build_style_user_prompt(
            style_draft["user_prompt"],
            neutral_content=SCENE_DRAFT,
            source_label="Approved Neutral Draft",
            source_row_id="draft_neutral_CH001_SC02_v3",
            extra_instruction="",
        ),
        stale_budget=STALE_BUDGETS["style_draft"],
    )


# ---------------------------------------------------------------------------
# 作者稿结构提取：场景稿与整章稿都要能过，且稿件只进提示词一次
# ---------------------------------------------------------------------------


def _author_target(object_type: str) -> dict:
    return {
        "object_type": object_type,
        "object_id": "CH001_SC02" if object_type == "scene" else "CH001",
        "project_id": "PRJ",
        "chapter_id": "CH001",
        "scene_id": "CH001_SC02" if object_type == "scene" else None,
        "chapter_goal": _zh(60, seed=950),
        "chapter_writer_brief": {"core_promise": _zh(60, seed=951)},
        "scene_card": {"scene_goal": _zh(40, seed=952), "beats": [_zh(20, seed=953)], "location": "档案馆", "exit_change": "", "hook": ""}
        if object_type == "scene"
        else {},
        "current_writer_brief": {"goal": _zh(40, seed=954)},
    }


# ---------------------------------------------------------------------------
# 章节族：15000 字章
# ---------------------------------------------------------------------------


def test_chapter_near_final_review_fits_a_15000_char_chapter() -> None:
    prompt = PromptBuilder().build(_chapter_review_snapshot(CHAPTER_TEXT), "chapter_near_final_review")
    final_user_prompt = near_final_module._acceptance_user_prompt(prompt["user_prompt"], source_content=CHAPTER_TEXT)
    _assert_fits(prompt, final_user_prompt, stale_budget=3200)


# ---------------------------------------------------------------------------
# 环境变量覆盖：小上下文模型可以整体收紧
# ---------------------------------------------------------------------------


def test_env_override_tightens_every_measured_family(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _rich_scene_snapshot()
    monkeypatch.setenv(SCENE_INPUT_TOKEN_BUDGET_ENV, "6000")
    builder = PromptBuilder()

    assert builder.build(snapshot, "hard_qc")["token_budget"]["target_input_tokens"] == 6000
    assert builder.build(snapshot, "chapter_near_final_review")["token_budget"]["target_input_tokens"] == 6000
    assert builder.build(snapshot, "scene_blueprint")["token_budget"]["target_input_tokens"] == 6000
    # 显式 max_input_tokens 仍然最优先；族外模板不受影响；雪花族不走这里
    assert builder.build(snapshot, "hard_qc", max_input_tokens=60)["token_budget"]["target_input_tokens"] == 60
    assert builder.build(snapshot, "snowflake_generate_book_brief")["token_budget"]["target_input_tokens"] == 24000

    monkeypatch.setenv(SCENE_INPUT_TOKEN_BUDGET_ENV, "0")
    assert builder.build(snapshot, "hard_qc")["token_budget"]["target_input_tokens"] == SCENE_INPUT_TOKEN_BUDGET

    monkeypatch.setenv(SCENE_INPUT_TOKEN_BUDGET_ENV, "lots")
    with pytest.raises(PromptConfigurationError):
        builder.build(snapshot, "hard_qc")
    monkeypatch.setenv(SCENE_INPUT_TOKEN_BUDGET_ENV, "-1")
    with pytest.raises(PromptConfigurationError):
        builder.build(snapshot, "hard_qc")


# ---------------------------------------------------------------------------
# 端到端到运行器：near_final_acceptance_review 真正派发，不再在预算闸前被拒
# ---------------------------------------------------------------------------


class _RecordingClient(AccountedGenerateMixin):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = {"near_final_status": "accepted", "issues": [], "rewrite_brief": []}
        return LLMResponse(
            request_id="resp_budget",
            provider="fake-provider",
            model="fake-model",
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_budget", "model": "fake-model"},
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            finish_reason="stop",
            attempt_count=1,
            max_retries=1,
        )


def _routing_config(node_id: str) -> ModelRoutingConfig:
    task_config = TaskModelConfig(
        provider="openai_compatible",
        model="fake-model",
        temperature=0.2,
        max_output_tokens=1200,
        response_format="json_object",
        provider_id="provider_primary",
        account_id="account_a",
        reasoning_level="medium",
        api_mode="responses",
        credential_mode="api_key",
    )
    return ModelRoutingConfig(
        node_routing={node_id: task_config},
        task_routing={node_id: task_config},
        retry_budget={},
        job_runtime={},
    )


def _seed_runner_scene(session) -> None:
    session.add(StoryProject(project_id="PRJ_BUDGET", title="budget", outline_text="outline"))
    session.add(ChapterGoal(chapter_id="CH001", project_id="PRJ_BUDGET", planned_scene_count=1, chapter_goal="预算回归"))
    session.add(
        SceneCard(
            scene_id="CH001_SC02",
            chapter_id="CH001",
            project_id="PRJ_BUDGET",
            scene_seq=2,
            scene_goal="把旧信中的矛盾线索抬到台面上",
            beats_json=["核对笔迹", "暴露缺口"],
        )
    )
    session.add(
        SceneRunState(
            scene_id="CH001_SC02",
            scene_status="ready",
            scene_token_budget=200_000,
            provider_attempt_budget=8,
        )
    )
    session.commit()


def test_runner_dispatches_near_final_review_for_a_3000_char_draft(session) -> None:
    _seed_runner_scene(session)
    client = _RecordingClient()
    runner = LLMNodeRunner(session, llm_client=client, routing_config=_routing_config("near_final_acceptance_review"))
    prompt = PromptBuilder().build(_rich_scene_snapshot(), "near_final_acceptance_review")

    runner.run(
        scene_id="CH001_SC02",
        chapter_id="CH001",
        bundle_id="bundle_CH001_SC02",
        bundle_hash="bundle_hash_CH001_SC02",
        node_id="near_final_acceptance_review",
        step="near_final_acceptance_review",
        prompt=prompt,
        user_prompt=near_final_module._acceptance_user_prompt(prompt["user_prompt"], source_content=SCENE_DRAFT),
        source_draft_row_id="final_scene:x",
        source_draft_content=SCENE_DRAFT,
    )
    session.commit()

    assert len(client.requests) == 1
    stored_call = session.execute(select(LlmCall)).scalars().one()
    assert stored_call.error_code is None
    summary = stored_call.request_payload_summary["token_budget"]
    assert summary["target_input_tokens"] == SCENE_INPUT_TOKEN_BUDGET
    assert STALE_BUDGETS["near_final_acceptance_review"] < summary["estimated_input_tokens"] <= SCENE_INPUT_TOKEN_BUDGET


def test_runner_still_fails_closed_when_the_tightened_override_is_exceeded(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """收紧后的预算仍是硬闸：超限就在派发前以 CONTINUITY_BUDGET_EXCEEDED 拒绝。"""
    _seed_runner_scene(session)
    monkeypatch.setenv(SCENE_INPUT_TOKEN_BUDGET_ENV, "2000")
    client = _RecordingClient()
    runner = LLMNodeRunner(session, llm_client=client, routing_config=_routing_config("near_final_acceptance_review"))
    prompt = PromptBuilder().build(_rich_scene_snapshot(), "near_final_acceptance_review")

    with pytest.raises(LLMNodeContinuityError) as exc_info:
        runner.run(
            scene_id="CH001_SC02",
            chapter_id="CH001",
            bundle_id="bundle_CH001_SC02",
            bundle_hash="bundle_hash_CH001_SC02",
            node_id="near_final_acceptance_review",
            step="near_final_acceptance_review",
            prompt=prompt,
            user_prompt=near_final_module._acceptance_user_prompt(prompt["user_prompt"], source_content=SCENE_DRAFT),
        )
    assert client.requests == []
    assert exc_info.value.continuity_warning["target_input_tokens"] == 2000
