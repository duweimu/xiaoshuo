"""LLM 节点注册表 ↔ prompts.yaml 对齐守卫。

背景（健康审计 · 架构契约）：`prompt_builder.py` 用 `self._templates[template_name]`
裸字典索引取模板，缺键即在运行期抛 KeyError；而 `llm_node_registry.py` 的
`LLMNodeSpec.template_name` 元数据与真实模板集此前没有任何对齐测试——新增节点若
误填 template_name，CI 不会捕获，只在该节点首次执行时炸。

窄约束关键：并非所有 `template_name` 都经 PromptBuilder.build() 索引。有三类例外
**不会**触发 KeyError，因此即便不在 prompts.yaml 也属正常，必须显式豁免（否则 naive
守卫会误报）：
  1) 任务路由别名（style_draft/style_patch 节点声明 template_name="stylize"，
     但实际 .build() 用 "style_draft"，由 llm_task_runner 路由到 task_routing["stylize"]）；
  2) 内联构造 prompt（scene_quality.py 直接拼 prompt，不经 PromptBuilder）；
  3) run_task + _AD_HOC_ROUTE_ALIASES 等非 PromptBuilder 路径，或当前无调用方的保留节点。

本守卫做两件事，互为自清理：
  A. 每个节点的 template_name 必须 ∈ prompts.yaml 或 ∈ 文档化豁免集；
  B. 豁免集成员必须确实不在 prompts.yaml（一旦某项被补进 prompts.yaml，应从豁免集
     移除，让它重新受 A 守护）——防止豁免集腐烂成长期掩盖真缺失的地毯。
"""
from __future__ import annotations

from pathlib import Path

from novel_system.services.llm_client import load_model_routing_config
from novel_system.services.llm_node_registry import get_llm_node_spec, llm_node_specs
from novel_system.services.prompt_builder import load_prompt_templates


# 不经 PromptBuilder.build() 直接索引的 template_name —— 缺席 prompts.yaml 属正常。
# 每项都注明豁免理由；新增豁免必须同步说明为何不会触发 KeyError。
_NON_PROMPTBUILDER_TEMPLATE_NAMES = {
    # 任务路由别名：节点 template_name="stylize"，实际 .build() 用 "style_draft"
    # （llm_task_runner.py 把 style_draft/style_patch 路由到 task_routing["stylize"]）。
    "stylize",
    # 内联构造 prompt（scene_quality.py），不经 PromptBuilder。
    "scene_auto_rewrite",
    # run_task + _AD_HOC_ROUTE_ALIASES（narrative_event_extract→extraction）路径。
    "extraction",
    # 当前无 PromptBuilder 调用方（保留 / 旧元数据）。
    "snowflake_step_generate",
}


def test_node_template_names_resolve_or_are_documented_non_builder():
    """每个节点的 template_name 必须在 prompts.yaml，或登记为非 PromptBuilder 路径。"""
    templates = load_prompt_templates()
    missing = [
        (spec.node_id, spec.template_name)
        for spec in llm_node_specs()
        if spec.template_name
        and spec.template_name not in templates
        and spec.template_name not in _NON_PROMPTBUILDER_TEMPLATE_NAMES
    ]
    assert not missing, (
        "这些节点的 template_name 既不在 config/prompts.yaml，也未登记为非 PromptBuilder 路径："
        f"{missing}。新增 LLM 节点时：若走 PromptBuilder.build()，在 prompts.yaml 加同名模板；"
        "若走内联/别名/run_task，把它加入 _NON_PROMPTBUILDER_TEMPLATE_NAMES 并注明为何不会触发 KeyError。"
    )


def test_documented_non_builder_names_are_actually_absent_from_prompts():
    """自清理：豁免集成员一旦进了 prompts.yaml，应移出豁免集（恢复 A 守护）。"""
    templates = load_prompt_templates()
    leaked = sorted(n for n in _NON_PROMPTBUILDER_TEMPLATE_NAMES if n in templates)
    assert not leaked, (
        f"{leaked} 现已在 prompts.yaml 中定义，应从 _NON_PROMPTBUILDER_TEMPLATE_NAMES 移除，"
        "让它们重新受 test_node_template_names_resolve_or_are_documented_non_builder 守护。"
    )


def test_prompts_yaml_templates_are_well_formed():
    """prompts.yaml 每个模板都应成功解析且带非空 system/task prompt（半成品模板拦在 CI）。"""
    templates = load_prompt_templates()
    assert templates, "prompts.yaml 未解析出任何模板"
    malformed = [
        name
        for name, tpl in templates.items()
        if not (getattr(tpl, "system_prompt", "") or "").strip()
        or not (getattr(tpl, "task_prompt", "") or "").strip()
    ]
    assert not malformed, f"这些 prompts.yaml 模板缺少非空 system_prompt / task_prompt：{malformed}"


def test_style_analysis_defaults_are_stable_and_have_verified_output_headroom():
    """仓库 YAML 与系统设置的节点默认值必须同时保持确定性，避免同步后静默回退。"""
    root = Path(__file__).resolve().parents[2]
    routing = load_model_routing_config(root / "config" / "models.yaml")
    expected = {
        "style_ref_paragraph_classify_bulk": 2000,
        "style_ref_extract_language": 6400,
        "style_ref_extract_narrative": 6400,
        "style_ref_extract_scene": 6400,
        "style_ref_extract_theme": 6400,
        "style_ref_supplement_evidence": 3000,
        "style_ref_synthesize_profile": 3500,
    }

    for node_id, max_output_tokens in expected.items():
        route = routing.task_routing[node_id]
        spec = get_llm_node_spec(node_id)
        assert spec is not None
        assert route.temperature == spec.temperature == 0.0
        assert route.max_output_tokens == spec.max_output_tokens == max_output_tokens
