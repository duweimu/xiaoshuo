from __future__ import annotations

import copy
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from novel_system.services.hash_engine import canonical_json, normalize
from novel_system.services.context_budget import (
    CONTINUITY_DROP_ORDER,
    SECTION_SPECS,
    PromptSection as _PromptSection,
    apply_context_budget,
    collect_prompt_sections as _collect_sections,
    estimate_tokens as _estimate_tokens,
)


class PromptConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: str
    input_token_budget: int
    system_prompt: str
    task_prompt: str
    structured_schema: dict[str, Any]


SUPPORTED_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}

# ── 场景 / 规划 / 章节三族的输入预算下限（量出来的，不是猜的）──
#
# 这些数字与 config/prompts.yaml 里对应模板的 input_token_budget 相同；代码侧再保
# 一份下限，是因为「库里有活动 prompts 快照时仓库文件不会被读」——已经在 系统配置
# 里保存过 prompts 的实装，快照里仍是估算器改成「一个汉字一 token」之前的旧值
# （1800–3600），不加这道地板，场景跑到 near_final_acceptance_review 就会以
# CONTINUITY_BUDGET_EXCEEDED fail-closed，900 字的稿子都过不去。
#
# 实测方法：用 backend/tests 夹具（seed_runtime_fixture + BundleBuilder）搭真实
# bundle，按各调用点的拼装函数拼出**最终** user prompt（基础 prompt + 稿件 + 调用点
# 追加的指令），再用 context_budget.estimate_tokens 估算 system+user。载荷：3000 字
# 中文场景稿、6 场共 15000 字的章节稿。三档 bundle：夹具原样 / 中等（蓝图、规划、
# 风格画像、作者偏好、记忆摘要等约半数槽位有中文材料）/ 全开（SECTION_SPECS 每个
# 槽位都有材料）。
#
#   场景族（bundle + 至多一份场景稿）    夹具 4.7k–6.7k  中等 11.3k–13.3k  全开 16.0k–18.1k
#   规划族（规划源快照，不带稿件）       12 场章 + 章架构 + 场景蓝图 4.3k
#   局部改写（writer_passage_patch）     摘录 + 1200/1400 字截断上下文 3.6k
#   章节族（整章正文 + 1600 字截断摘要）  15000 字章 17.3k–19.4k
#
# 取值：场景族 24000（中等 ×1.8、全开 ×1.33，与雪花族同一个数，产品里「一场规模」的
# 调用只有一条上限）；规划族与局部改写 8000（4.3k ×1.85）；章节族 30000（19.4k ×1.55，
# 覆盖到约 25000 字的章）。同族载荷成分相同，所以同族同值。
# 要为小上下文的本地模型收紧，用 NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET 覆盖整个
# 三族（见 _scene_input_token_budget_override），无须改这里。
SCENE_INPUT_TOKEN_BUDGET = 24000
PLANNING_INPUT_TOKEN_BUDGET = 8000
CHAPTER_INPUT_TOKEN_BUDGET = 30000
SCENE_INPUT_TOKEN_BUDGET_ENV = "NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET"
RUNTIME_MIN_INPUT_BUDGETS = {
    # 场景族
    "neutral_draft": SCENE_INPUT_TOKEN_BUDGET,
    "style_draft": SCENE_INPUT_TOKEN_BUDGET,
    "scene_literary_rewrite": SCENE_INPUT_TOKEN_BUDGET,
    "style_length_patch": SCENE_INPUT_TOKEN_BUDGET,
    "style_salvage_patch": SCENE_INPUT_TOKEN_BUDGET,
    "hard_qc": SCENE_INPUT_TOKEN_BUDGET,
    "soft_qc": SCENE_INPUT_TOKEN_BUDGET,
    "near_final_acceptance_review": SCENE_INPUT_TOKEN_BUDGET,
    # 规划族 + 局部改写（载荷有界）
    "scene_blueprint": PLANNING_INPUT_TOKEN_BUDGET,
    "chapter_story_architecture": PLANNING_INPUT_TOKEN_BUDGET,
    "character_pressure_blueprint": PLANNING_INPUT_TOKEN_BUDGET,
    "writer_passage_patch": PLANNING_INPUT_TOKEN_BUDGET,
    # 章节族（author_* 两个模板同时服务场景稿与整章稿，按最大者归章节族）
    "chapter_near_final_review": CHAPTER_INPUT_TOKEN_BUDGET,
    "writer_deep_review": CHAPTER_INPUT_TOKEN_BUDGET,
    "author_proposal_generate": CHAPTER_INPUT_TOKEN_BUDGET,
}
CHARACTER_CONTINUITY_INSTRUCTION = (
    "Preserve character identity and pronoun continuity across the scene. "
    "Do not change a character's gender, role, or name cues from the scene card, POV voice, "
    "relation digest, previous scene memory, or source draft. "
    "When pronouns are ambiguous, repeat the character name."
)
DRAFTING_TEMPLATE_NAMES = {
    "neutral_draft",
    "style_draft",
    "scene_literary_rewrite",
    "near_final_rewrite",
    "project_outline_plan",
    "scene_blueprint",
    "chapter_story_architecture",
    "character_pressure_blueprint",
    "snowflake_generate_logline",
    "snowflake_generate_one_paragraph",
    "snowflake_generate_character_lineup",
    "snowflake_generate_plot_beats",
    "snowflake_generate_scene_plan",
    "snowflake_generate_character_plan",
    "snowflake_workspace_assistant",
    "snowflake_scene_triage_suggest",
}
HARD_QC_TEMPLATE_NAMES = {
    "hard_qc",
    "soft_qc",
    "near_final_acceptance_review",
}
CHAPTER_REVIEW_TEMPLATE_NAMES = {
    "chapter_summary",
    "chapter_near_final_review",
    "writer_chapter_diagnosis",
    "writer_chapter_revision",
    "writer_deep_review",
}


class PromptBuilder:
    def __init__(self, template_path: str | Path | None = None) -> None:
        self._templates = load_prompt_templates(template_path)

    def build(
        self,
        bundle_snapshot: Mapping[str, Any],
        template_name: str,
        *,
        max_input_tokens: int | None = None,
    ) -> dict[str, Any]:
        template = self._templates[template_name]
        snapshot = _normalize_mapping(bundle_snapshot)
        sections = _collect_sections(snapshot)
        structured_schema = _clone_jsonish(template.structured_schema)
        task_prompt = _append_runtime_template_instruction(template.task_prompt, template.name)
        task_prompt = _append_schema_instruction(task_prompt, structured_schema)
        target_input_tokens = max_input_tokens
        if target_input_tokens is None:
            target_input_tokens = _default_input_token_budget(template)
        context_budget = apply_context_budget(
            system_prompt=template.system_prompt,
            task_prompt=task_prompt,
            bundle_snapshot=snapshot,
            sections=sections,
            max_input_tokens=target_input_tokens,
            task_kind=_task_kind_for_template(template.name),
        )
        budget = context_budget["budget"]
        system_prompt = template.system_prompt
        user_prompt = context_budget["user_prompt"]
        final_estimate = _estimate_tokens(system_prompt) + _estimate_tokens(user_prompt)
        budget["estimated_input_tokens"] = final_estimate
        budget["remaining_input_tokens"] = budget["target_input_tokens"] - final_estimate

        prompt_hash = hashlib.sha256(
            canonical_json(
                {
                    "template_name": template.name,
                    "template_version": template.version,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                    "structured_schema": structured_schema,
                }
            ).encode("utf-8")
        ).hexdigest()

        return {
            "template_name": template.name,
            "template_version": template.version,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "structured_schema": structured_schema,
            "prompt_hash": prompt_hash,
            "token_budget": budget,
            "continuity_warning": context_budget["continuity_warning"],
        }


def load_prompt_templates(path: str | Path | None = None) -> dict[str, PromptTemplate]:
    if path is None:
        from novel_system.services.config_snapshot_reader import load_active_config_payload

        raw_payload = load_active_config_payload("prompts")
        if raw_payload is None:
            config_path = _default_prompts_config_path()
            try:
                raw_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise PromptConfigurationError("prompts config could not be parsed") from exc
    else:
        config_path = Path(path)
        try:
            raw_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PromptConfigurationError("prompts config could not be parsed") from exc
    return parse_prompt_templates(raw_payload)


def _default_input_token_budget(template: PromptTemplate) -> int:
    floor = RUNTIME_MIN_INPUT_BUDGETS.get(template.name)
    if floor is None:
        return template.input_token_budget
    override = _scene_input_token_budget_override()
    if override > 0:
        # 作者为小上下文模型显式收紧：同时覆盖模板值与代码地板——否则地板会把
        # 收紧顶回去，环境变量就成了摆设。
        return override
    return max(template.input_token_budget, floor)


def _scene_input_token_budget_override() -> int:
    """NOVEL_SYSTEM_SCENE_INPUT_TOKEN_BUDGET：未设/0 = 模板值与地板取大；正数 = 三族统一上限。

    语义与 settings._get_quota_int_env 相同（非负整数，0 关闭）。直接读环境变量而不
    走 get_settings()，是因为这个旋钮只属于提示词装配层，且 PromptBuilder 在请求期被
    反复构造；非法值在这里就报配置错误，而不是留到运行时变成一次莫名其妙的超限。
    """
    raw_value = os.environ.get(SCENE_INPUT_TOKEN_BUDGET_ENV)
    if raw_value is None or not raw_value.strip():
        return 0
    message = f"{SCENE_INPUT_TOKEN_BUDGET_ENV} must be a non-negative integer (0 uses the template budgets)"
    try:
        value = int(raw_value.strip())
    except ValueError as exc:
        raise PromptConfigurationError(message) from exc
    if value < 0:
        raise PromptConfigurationError(message)
    return value


def _task_kind_for_template(template_name: str) -> str:
    if template_name == "neutral_draft":
        return "neutral_draft"
    if template_name in HARD_QC_TEMPLATE_NAMES:
        return "hard_qc"
    if template_name in CHAPTER_REVIEW_TEMPLATE_NAMES:
        return "chapter_review"
    if template_name in DRAFTING_TEMPLATE_NAMES:
        return "drafting"
    return "default"


def parse_prompt_templates(raw_payload: Any) -> dict[str, PromptTemplate]:
    if not isinstance(raw_payload, dict):
        raise PromptConfigurationError("prompts config must decode to a mapping")

    templates_payload = raw_payload.get("templates")
    if not isinstance(templates_payload, dict):
        raise PromptConfigurationError("prompts config must define a templates mapping")

    templates: dict[str, PromptTemplate] = {}
    for template_name, payload in templates_payload.items():
        if not isinstance(template_name, str):
            raise PromptConfigurationError("template names must be strings")
        templates[template_name] = _load_prompt_template(template_name, payload)
    return templates


def _default_prompts_config_path() -> Path:
    return Path(__file__).resolve().parents[4] / "config" / "prompts.yaml"


def _load_prompt_template(template_name: str, payload: Any) -> PromptTemplate:
    if not isinstance(payload, Mapping):
        raise PromptConfigurationError(f"template {template_name} must be a mapping")

    required_fields = (
        "version",
        "input_token_budget",
        "system_prompt",
        "task_prompt",
        "structured_schema",
    )
    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        raise PromptConfigurationError(
            f"template {template_name} is missing required fields: {', '.join(missing_fields)}"
        )

    version = _require_string_field(template_name, payload, "version")
    system_prompt = _require_string_field(template_name, payload, "system_prompt")
    task_prompt = _require_string_field(template_name, payload, "task_prompt")
    input_token_budget = _require_positive_int_field(template_name, payload, "input_token_budget")

    structured_schema = payload["structured_schema"]
    if not isinstance(structured_schema, Mapping):
        raise PromptConfigurationError(f"template {template_name}.structured_schema must be a mapping")
    normalized_schema = _normalize_mapping(structured_schema)
    _align_schema_with_runtime_contract(template_name, normalized_schema)
    _validate_structured_schema(normalized_schema, f"template {template_name}.structured_schema", top_level=True)

    return PromptTemplate(
        name=template_name,
        version=_normalize_text(version),
        input_token_budget=input_token_budget,
        system_prompt=_normalize_text(system_prompt),
        task_prompt=_normalize_text(task_prompt),
        structured_schema=_clone_jsonish(normalized_schema),
    )


def _require_string_field(template_name: str, payload: Mapping[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise PromptConfigurationError(f"template {template_name}.{field} must be a string")
    return value


def _require_positive_int_field(template_name: str, payload: Mapping[str, Any], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise PromptConfigurationError(f"template {template_name}.{field} must be an integer")
    if value <= 0:
        raise PromptConfigurationError(f"template {template_name}.{field} must be greater than 0")
    return value


def _normalize_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize(dict(payload))
    if not isinstance(normalized, dict):
        raise ValueError("payload must normalize to a mapping")
    return normalized


def _normalize_text(text: str) -> str:
    normalized = normalize(text)
    if not isinstance(normalized, str):
        raise ValueError("text must normalize to a string")
    return normalized


def _clone_jsonish(value: Any) -> Any:
    return copy.deepcopy(value)


def _append_schema_instruction(user_prompt: str, structured_schema: Mapping[str, Any]) -> str:
    required = structured_schema.get("required")
    required_keys = [item for item in required if isinstance(item, str)] if isinstance(required, list) else []
    enum_instruction = _enum_instruction(structured_schema)
    suffix_lines: list[str] = []
    if not required_keys:
        suffix_lines.append("Return only valid JSON. Do not wrap it in markdown fences.")
    else:
        suffix_lines.append(f"Required top-level JSON keys: {', '.join(required_keys)}.")
        if enum_instruction:
            suffix_lines.append(enum_instruction)
        suffix_lines.append("Return only valid JSON. Do not wrap it in markdown fences.")
    return f"{user_prompt}\n" + "\n".join(suffix_lines)


def _append_runtime_template_instruction(user_prompt: str, template_name: str) -> str:
    instructions = {
        "neutral_draft": (
            "Write prose in the same language as the chapter goal and scene card. "
            "If the chapter goal or scene card contains Chinese text, scene_text must be Chinese prose; "
            "do not translate Chinese settings, beats, or required text into English."
        ),
        "style_draft": (
            "Preserve the source draft language; do not translate the scene while styling it. "
            "If the draft or scene card is Chinese, scene_text must remain Chinese prose."
        ),
        "hard_qc": (
            "If the draft under review is Chinese, write issue messages and rewrite_brief in Chinese; "
            "preserve Chinese character names exactly and do not romanize or translate them."
        ),
        "soft_qc": (
            "If the draft under review is Chinese, write issue messages and rewrite_brief in Chinese; "
            "preserve Chinese character names exactly and do not romanize or translate them."
        ),
    }
    instruction = instructions.get(template_name)
    if instruction:
        instruction = f"{instruction} {CHARACTER_CONTINUITY_INSTRUCTION}"
    if not instruction or instruction in user_prompt:
        return user_prompt
    return f"{user_prompt}\n{instruction}"


def _align_schema_with_runtime_contract(template_name: str, schema: dict[str, Any]) -> None:
    if template_name not in {"hard_qc", "soft_qc"}:
        return
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        return

    if template_name == "hard_qc":
        properties.setdefault("rewrite_brief", {"type": "array", "items": {"type": "string"}})
        if "rewrite_brief" not in required:
            required.append("rewrite_brief")
        _merge_schema_property(
            properties,
            "resolution_code",
            {"enum": ["hard_pass", "hard_fail_partial", "hard_fail_full", "hard_block_human"]},
        )
        _merge_schema_property(
            properties,
            "next_action",
            {"enum": ["pass", "partial_rewrite", "full_rewrite", "human_review_required"]},
        )
        return

    _merge_schema_property(
        properties,
        "resolution_code",
        {"enum": ["soft_pass", "soft_patch", "soft_waive", "soft_block_human"]},
    )
    _merge_schema_property(
        properties,
        "next_action",
        {"enum": ["pass", "patch", "pass_with_notes", "human_review_required"]},
    )


def _merge_schema_property(properties: dict[str, Any], property_name: str, updates: dict[str, Any]) -> None:
    property_schema = properties.get(property_name)
    if isinstance(property_schema, dict):
        property_schema.update(updates)


def _enum_instruction(structured_schema: Mapping[str, Any]) -> str:
    properties = structured_schema.get("properties")
    if not isinstance(properties, Mapping):
        return ""
    instructions: list[str] = []
    for key, property_schema in properties.items():
        if not isinstance(key, str) or not isinstance(property_schema, Mapping):
            continue
        enum_values = property_schema.get("enum")
        if isinstance(enum_values, list) and enum_values and all(isinstance(item, str) for item in enum_values):
            instructions.append(f"{key} must be one of: {', '.join(enum_values)}")
    if not instructions:
        return ""
    return "Allowed JSON enum values: " + "; ".join(instructions) + "."


def _validate_structured_schema(schema: Mapping[str, Any], context: str, *, top_level: bool = False) -> None:
    schema_type = schema.get("type")
    if schema_type is not None and not isinstance(schema_type, str):
        raise PromptConfigurationError(f"{context}.type must be a string")
    if isinstance(schema_type, str) and schema_type not in SUPPORTED_SCHEMA_TYPES:
        raise PromptConfigurationError(f"{context}.type has unsupported value {schema_type}")
    if top_level and schema_type != "object":
        raise PromptConfigurationError(f"{context}.type must be 'object'")

    properties = schema.get("properties")
    if top_level and not isinstance(properties, Mapping):
        raise PromptConfigurationError(f"{context}.properties must be a mapping")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise PromptConfigurationError(f"{context}.properties must be a mapping")
        for property_name, property_schema in properties.items():
            if not isinstance(property_name, str):
                raise PromptConfigurationError(f"{context}.properties keys must be strings")
            if not isinstance(property_schema, Mapping):
                raise PromptConfigurationError(f"{context}.properties.{property_name} must be a mapping")
            normalized_property_schema = _normalize_mapping(property_schema)
            _validate_structured_schema(normalized_property_schema, f"{context}.properties.{property_name}")

    required = schema.get("required")
    if top_level and not isinstance(required, list):
        raise PromptConfigurationError(f"{context}.required must be a list of strings")
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise PromptConfigurationError(f"{context}.required must be a list of strings")

    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None and not isinstance(additional_properties, bool):
        raise PromptConfigurationError(f"{context}.additionalProperties must be a boolean")
    if (
        additional_properties is False
        and top_level
        and isinstance(required, list)
        and isinstance(properties, Mapping)
    ):
        undeclared_required = [item for item in required if item not in properties]
        if undeclared_required:
            raise PromptConfigurationError(
                f"{context}.required contains entries not declared in properties: "
                f"{', '.join(undeclared_required)}"
            )

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise PromptConfigurationError(f"{context}.items must be a mapping")
        normalized_items = _normalize_mapping(items)
        _validate_structured_schema(normalized_items, f"{context}.items")
