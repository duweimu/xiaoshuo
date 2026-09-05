from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


NodeStatus = Literal["active", "reserved", "local"]


@dataclass(frozen=True, slots=True)
class LLMNodeSpec:
    node_id: str
    label: str
    group: str
    status: NodeStatus = "active"
    requires_llm: bool = True
    template_name: str | None = None
    provider: str = "openai_compatible"
    model: str = "gpt-5"
    temperature: float = 0.2
    max_output_tokens: int = 2200
    response_format: str = "json_object"
    reasoning_level: str = "medium"
    api_mode: str = "responses"
    model_profile: str | None = None
    fallback_route_ids: tuple[str, ...] = ()
    # PR-8 §5.1 — 分段续写场景专用:每 N 字符重新拉取 [STYLE_REFERENCE] 注入,
    # 防止风格漂移。0 = 不刷新(默认)。曾仅由已下线的 long_form_continuation
    # 节点启用;字段保留在 spec 契约里(catalog/route payload 消费方兼容)。
    refresh_every_chars: int = 0
    # §7 anti-mean sampling — decoding-level penalties carried into DB node routing so the
    # System-Config UI route keeps them instead of silently dropping to None on the DB path.
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    top_p: float | None = None

    def catalog_entry(self, order: int) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "group": self.group,
            "status": self.status,
            "requires_llm": self.requires_llm,
            "template_name": self.template_name,
            "default_provider": self.provider,
            "default_model": self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "response_format": self.response_format,
            "reasoning_level": self.reasoning_level,
            "api_mode": self.api_mode,
            "model_profile": self.model_profile,
            "fallback_route_ids": list(self.fallback_route_ids),
            "refresh_every_chars": self.refresh_every_chars,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "top_p": self.top_p,
            "order": order,
        }

    def route_payload(
        self,
        *,
        provider_id: str,
        provider: str | None = None,
        model: str | None = None,
        account_id: str | None = None,
        api_mode: str | None = None,
        credential_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": provider or self.provider,
            "provider_id": provider_id,
            "model": model or self.model,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "response_format": self.response_format,
            "reasoning_level": self.reasoning_level,
            "api_mode": api_mode or self.api_mode,
        }
        if account_id:
            payload["account_id"] = account_id
        if credential_mode:
            payload["credential_mode"] = credential_mode
        if self.model_profile:
            payload["model_profile"] = self.model_profile
        # §7 carry decoding-level sampling penalties into the route payload so DB-stored
        # node routing (System-Config UI) preserves them instead of dropping them to None.
        if self.frequency_penalty is not None:
            payload["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            payload["presence_penalty"] = self.presence_penalty
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        return payload


_NODE_SPECS: tuple[LLMNodeSpec, ...] = (
    LLMNodeSpec(
        "extraction",
        "Generic extraction",
        "reference",
        template_name="extraction",
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=1200,
    ),
    # FE-ALIGN P6: 资料库半自动派生（归档正文 → 候选实体/时间线事件 → 待办确认）
    # FE-ALIGN G5: 构思视图的步骤候选生成（3 条不同方向，原型契约形状）
    LLMNodeSpec(
        "snowflake_step_candidates",
        "Snowflake step candidates",
        "project",
        template_name="snowflake_step_candidates",
        temperature=0.7,
        max_output_tokens=1800,
    ),
    LLMNodeSpec(
        "style_ref_paragraph_classify_anchor",
        "Style Reference 段落分类(锚定集,quality_strong)",
        "style_reference",
        template_name="style_ref_paragraph_classify_anchor",
        model="gpt-5",
        temperature=0.1,
        max_output_tokens=2000,
    ),
    LLMNodeSpec(
        "style_ref_paragraph_classify_bulk",
        "Style Reference 段落分类(余下,local_fast)",
        "style_reference",
        template_name="style_ref_paragraph_classify_bulk",
        model="gpt-5-mini",
        # 分析节点必须可复算；创作随机性只留给生成节点。
        temperature=0.0,
        max_output_tokens=2000,
    ),
    LLMNodeSpec(
        "style_ref_extract_language",
        "Style Reference 抽取(语言层 4 sub_dim,quality_strong)",
        "style_reference",
        template_name="style_ref_extract_language",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=6400,
    ),
    LLMNodeSpec(
        "style_ref_extract_narrative",
        "Style Reference 抽取(叙事层 4 sub_dim,quality_strong)",
        "style_reference",
        template_name="style_ref_extract_narrative",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=6400,
    ),
    LLMNodeSpec(
        "style_ref_extract_scene",
        "Style Reference 抽取(场景层 4 sub_dim,quality_strong)",
        "style_reference",
        template_name="style_ref_extract_scene",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=6400,
    ),
    LLMNodeSpec(
        "style_ref_extract_theme",
        "Style Reference 抽取(主题层 4 sub_dim,quality_strong)",
        "style_reference",
        template_name="style_ref_extract_theme",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=6400,
    ),
    LLMNodeSpec(
        "style_ref_supplement_evidence",
        "Style Reference 单 obs 定向补抽 evidence(两级重试第一级)",
        "style_reference",
        template_name="style_ref_supplement_evidence",
        model="gpt-5-mini",
        temperature=0.0,
        max_output_tokens=3000,
    ),
    LLMNodeSpec(
        "style_ref_synthesize_profile",
        "Style Reference Profile 聚合(16 sub_dim → StyleProfile)",
        "style_reference",
        template_name="style_ref_synthesize_profile",
        model="gpt-5",
        temperature=0.0,
        max_output_tokens=3500,
    ),
    LLMNodeSpec(
        "style_ref_preview_generate",
        "Style Reference Preview 端点示例文本生成",
        "style_reference",
        template_name="style_ref_preview_generate",
        model="gpt-5-mini",
        temperature=0.7,
        max_output_tokens=1500,
    ),
    LLMNodeSpec(
        "style_ref_validate_semantic",
        "Style Reference Validation - Semantic critic(强制 quote)",
        "style_reference",
        template_name="style_ref_validate_semantic",
        model="gpt-5",
        temperature=0.2,
        max_output_tokens=2000,
    ),
    LLMNodeSpec(
        "style_ref_validate_forbidden",
        "Style Reference Validation - Forbidden pattern triggered judge",
        "style_reference",
        template_name="style_ref_validate_forbidden",
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=800,
    ),
    LLMNodeSpec(
        "style_ref_rag_rerank",
        "Style Reference Strategy C(RAG)候选重排(离线/预览增强 hook,inject 热路径用确定性 rerank)",
        "style_reference",
        template_name="style_ref_rag_rerank",
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=800,
        # 非热路径预留 hook(同其他 reserved 节点):不纳入 active 节点路由同步,
        # inject 热路径用确定性 rerank(§11 风险6 inject<50ms 无 LLM)。
        status="reserved",
    ),
    LLMNodeSpec(
        "snowflake_step_generate",
        "Snowflake step generate",
        "snowflake",
        template_name="snowflake_step_generate",
        temperature=0.25,
        max_output_tokens=3200,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "snowflake_workspace_assistant",
        "Snowflake workspace assistant",
        "snowflake",
        template_name="snowflake_workspace_assistant",
        temperature=0.35,
        max_output_tokens=2200,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "snowflake_scene_triage",
        "Snowflake scene triage",
        "snowflake",
        template_name="snowflake_scene_triage_suggest",
        temperature=0.15,
        max_output_tokens=2200,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "snowflake_chapter_plan",
        "Snowflake chapter plan suggestion",
        "snowflake",
        template_name="snowflake_chapter_plan_suggest",
        temperature=0.2,
        max_output_tokens=2600,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "scene_blueprint",
        "Scene blueprint",
        "scene_generation",
        template_name="scene_blueprint",
        temperature=0.25,
        max_output_tokens=1800,
        fallback_route_ids=("neutral_draft", "style_draft", "stylize"),
    ),
    LLMNodeSpec(
        "character_pressure_blueprint",
        "Character pressure blueprint",
        "scene_generation",
        template_name="character_pressure_blueprint",
        temperature=0.25,
        max_output_tokens=1800,
        fallback_route_ids=("scene_blueprint", "neutral_draft", "style_draft", "stylize"),
    ),
    LLMNodeSpec(
        "chapter_story_architecture",
        "Chapter story architecture",
        "scene_generation",
        template_name="chapter_story_architecture",
        temperature=0.25,
        max_output_tokens=2200,
        fallback_route_ids=("scene_blueprint", "neutral_draft", "style_draft", "stylize"),
    ),
    # 章节编排 LLM 规划三通道（docs/chapter-arrangement-llm-design-2026-07-16.md §4）
    LLMNodeSpec(
        "chapter_scene_plan_candidates",
        "Chapter scene plan candidates",
        "project",
        template_name="chapter_scene_plan_candidates",
        temperature=0.6,
        max_output_tokens=3200,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "chapter_scene_plan_fill",
        "Chapter scene plan fill",
        "project",
        template_name="chapter_scene_plan_fill",
        temperature=0.2,
        max_output_tokens=3200,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "chapter_plan_review",
        "Chapter plan review",
        "project",
        template_name="chapter_plan_review",
        temperature=0.15,
        max_output_tokens=2600,
        model_profile="quality_strong",
    ),
    LLMNodeSpec(
        "neutral_draft",
        "Neutral draft",
        "scene_generation",
        template_name="neutral_draft",
        temperature=0.6,
        max_output_tokens=6000,
    ),
    LLMNodeSpec(
        "style_draft",
        "Style draft",
        "scene_generation",
        template_name="stylize",
        temperature=0.8,
        max_output_tokens=6000,
        frequency_penalty=0.3,
        presence_penalty=0.15,
    ),
    LLMNodeSpec(
        "style_patch",
        "Style patch",
        "scene_generation",
        template_name="stylize",
        temperature=0.8,
        max_output_tokens=6000,
        frequency_penalty=0.3,
        presence_penalty=0.15,
    ),
    LLMNodeSpec(
        "scene_literary_rewrite",
        "Scene literary rewrite",
        "rewrite",
        template_name="scene_literary_rewrite",
        temperature=0.55,
        max_output_tokens=6000,
        model_profile="quality_strong",
        fallback_route_ids=("style_draft", "style_patch", "stylize", "neutral_draft"),
    ),
    LLMNodeSpec(
        "hard_qc",
        "Hard QC",
        "quality",
        template_name="hard_qc",
        temperature=0.2,
        max_output_tokens=2200,
    ),
    LLMNodeSpec(
        "soft_qc",
        "Soft QC",
        "quality",
        template_name="soft_qc",
        model="gpt-5-mini",
        temperature=0.2,
        max_output_tokens=1800,
    ),
    LLMNodeSpec(
        "near_final_acceptance_review",
        "Near-final acceptance review",
        "quality",
        template_name="near_final_acceptance_review",
        temperature=0.15,
        max_output_tokens=2600,
        fallback_route_ids=("soft_qc", "hard_qc", "style_draft", "neutral_draft"),
    ),
    LLMNodeSpec(
        "chapter_near_final_review",
        "Chapter near-final review",
        "quality",
        template_name="chapter_near_final_review",
        temperature=0.15,
        max_output_tokens=3200,
        fallback_route_ids=("soft_qc", "hard_qc", "style_draft", "neutral_draft"),
    ),
    LLMNodeSpec(
        "writer_passage_patch",
        "Writer passage patch",
        "rewrite",
        template_name="writer_passage_patch",
        temperature=0.45,
        max_output_tokens=2600,
    ),
    LLMNodeSpec(
        "writer_deep_review",
        "Writer deep review",
        "deep_review",
        template_name="writer_deep_review",
        temperature=0.15,
        max_output_tokens=3600,
    ),
    LLMNodeSpec(
        "author_proposal_generate",
        "Author proposal generate",
        "writer_review",
        template_name="author_proposal_generate",
        temperature=0.45,
        max_output_tokens=2600,
    ),
    LLMNodeSpec(
        "chapter_summary",
        "Chapter summary",
        "local",
        status="reserved",
        requires_llm=False,
        template_name=None,
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=1200,
    ),
    LLMNodeSpec(
        "continuity_compression",
        "Continuity compression",
        "local",
        status="reserved",
        requires_llm=False,
        template_name=None,
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=1200,
    ),
    LLMNodeSpec(
        "archive",
        "Archive and index",
        "local",
        status="reserved",
        requires_llm=False,
        template_name=None,
        model="gpt-5-mini",
        temperature=0.1,
        max_output_tokens=1200,
    ),
    LLMNodeSpec(
        "chapter_aggregate",
        "Chapter aggregate",
        "local",
        status="reserved",
        requires_llm=False,
        template_name=None,
        temperature=0.4,
        max_output_tokens=4000,
    ),
)


def llm_node_specs() -> tuple[LLMNodeSpec, ...]:
    return _NODE_SPECS


def llm_node_catalog() -> dict[str, dict[str, Any]]:
    return {
        spec.node_id: spec.catalog_entry(index)
        for index, spec in enumerate(_NODE_SPECS)
    }


def llm_node_statuses() -> dict[str, str]:
    return {spec.node_id: spec.status for spec in _NODE_SPECS}


def active_llm_node_ids() -> list[str]:
    return [
        spec.node_id
        for spec in _NODE_SPECS
        if spec.status == "active" and spec.requires_llm
    ]


def reserved_llm_node_ids() -> set[str]:
    return {
        spec.node_id
        for spec in _NODE_SPECS
        if spec.status != "active" or not spec.requires_llm
    }


def get_llm_node_spec(node_id: str) -> LLMNodeSpec | None:
    for spec in _NODE_SPECS:
        if spec.node_id == node_id:
            return spec
    return None


def default_task_config_payload(
    node_id: str,
    *,
    provider_id: str,
    provider: str | None = None,
    model: str | None = None,
    account_id: str | None = None,
    api_mode: str | None = None,
    credential_mode: str | None = None,
) -> dict[str, Any]:
    spec = get_llm_node_spec(node_id)
    if spec is None:
        raise KeyError(node_id)
    return spec.route_payload(
        provider_id=provider_id,
        provider=provider,
        model=model,
        account_id=account_id,
        api_mode=api_mode,
        credential_mode=credential_mode,
    )


# ---- 角色分工槽位(writer-facing routing) ---------------------------------
# 写作者视角的三个分工槽位,按节点分组批量路由;覆盖全部 active 节点
# (local 组 requires_llm=False 不入槽)。前端「设置 → AI 模型 → 分工」用。


@dataclass(frozen=True, slots=True)
class RoleSlotSpec:
    slot_id: str
    label_zh: str
    description_zh: str
    groups: tuple[str, ...]

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "label_zh": self.label_zh,
            "description_zh": self.description_zh,
            "groups": list(self.groups),
            "node_ids": role_slot_node_ids(self.slot_id),
        }


ROLE_SLOTS: tuple[RoleSlotSpec, ...] = (
    RoleSlotSpec(
        "drafting",
        "写作主力",
        "续写、初稿、风格化、改写与构思生成",
        ("scene_generation", "rewrite", "snowflake"),
    ),
    RoleSlotSpec(
        "review",
        "审稿质检",
        "QC、验收评审、文学评估与写作者诊断",
        ("quality", "evaluation", "writer_review", "deep_review"),
    ),
    RoleSlotSpec(
        "extraction",
        "提炼整理",
        "资料抽取、风格画像与项目级提炼",
        ("reference", "style_reference", "project"),
    ),
)


def get_role_slot_spec(slot_id: str) -> RoleSlotSpec | None:
    for slot in ROLE_SLOTS:
        if slot.slot_id == slot_id:
            return slot
    return None


def role_slot_node_ids(slot_id: str) -> list[str]:
    slot = get_role_slot_spec(slot_id)
    if slot is None:
        raise KeyError(slot_id)
    groups = set(slot.groups)
    return [
        spec.node_id
        for spec in _NODE_SPECS
        if spec.group in groups and spec.status == "active" and spec.requires_llm
    ]


def role_slot_catalog() -> list[dict[str, Any]]:
    return [slot.catalog_entry() for slot in ROLE_SLOTS]
