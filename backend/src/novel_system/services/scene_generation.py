from __future__ import annotations

import hashlib
import logging
import math
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.author_instructions import render_author_note_instruction
from novel_system.services.hash_engine import canonical_json
from novel_system.services.literary_quality import (
    DIMENSION_WEIGHTS,
    analyze_literary_quality,
)
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.llm_client import LLMResponse
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_task_runner import (
    CONTINUITY_BUDGET_ERROR_CODE,
    CONTINUITY_BUDGET_MESSAGE,
    SCENE_SPLIT_RECOMMENDATION,
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.qc_constraints import (
    constraint_terms,
    contains_forbidden_term,
    source_field_satisfied,
)
from novel_system.services.style_reference.injection import (
    InjectionService,
    fit_fragments_to_input_budget,
    ordered_character_ids,
)
from novel_system.services.style_reference.runtime_contract import (
    contract_profile_objects,
    extract_style_generation_context,
    resolve_style_runtime_contract_state,
)

_LOGGER = logging.getLogger(__name__)
_PRE_DISPATCH_ACCOUNTING_REJECTIONS = frozenset(
    {
        "LLM_SCENE_TOKEN_BUDGET_UNINITIALIZED",
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_SCENE_CALL_IN_FLIGHT",
        "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
    }
)


def _counts_as_business_attempt(exc: Exception) -> bool:
    """A pre-dispatch accounting rejection is evidence, not a generation attempt."""
    original = getattr(exc, "original_error", None)
    code = str(getattr(exc, "error_code", None) or getattr(exc, "code", None) or "")
    original_code = str(
        getattr(original, "error_code", None) or getattr(original, "code", None) or ""
    )
    return not (
        isinstance(exc, LLMAccountingRejected)
        or isinstance(original, LLMAccountingRejected)
        or code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
        or original_code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
    )


class SceneGenerationPostprocessError(ValueError):
    """Stable typed failure emitted after a provider call settled successfully."""

    def __init__(self, *, llm_call_id: str | None, message: str) -> None:
        super().__init__(message)
        self.llm_call_id = llm_call_id
        self.code = "SCENE_GENERATION_RESPONSE_INVALID"
        self.error_code = self.code


@dataclass(slots=True)
class NeutralGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None


@dataclass(slots=True)
class StyleGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None
    ranking_audit: dict[str, Any] | None = None


JSON_SCHEMA_INSTRUCTION = "Return JSON that matches the structured schema exactly."
_STYLE_SAFETY_REPAIR_TASK_PROMPT = (
    "Edit the labeled rejected style draft directly. This is a local safety repair, not a new composition. "
    "Preserve its wording, paragraph architecture, reusable style, facts, chronology, and ending wherever they "
    "already pass; change only the exact hard-constraint failures listed below. Return one complete replacement "
    "scene_text and no commentary."
)
_STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT = (
    "Edit the labeled style draft directly. Apply only the listed de-template corrections while preserving its "
    "facts, chronology, functional paragraph architecture, broad style distribution, distinctive wording, and ending function. "
    "Do not restart the scene, recompose it from a blank page, or rewrite unaffected passages. Return one complete "
    "replacement scene_text and no commentary."
)
ANTI_TEMPLATE_GATE_DIMENSIONS = {
    "model_voice",
    "image_homogeneity",
    "repetitive_action",
    "template_action_reuse",
    "image_field_reuse",
    "syntax_monotony",
    "false_clarity",
    "summary_ending",
    "expository_dialogue",
    "decorative_imagery",
    "dialogue_as_report",
    "over_explained_motive",
    "false_poetic_closure",
    "self_repetition",
}
_STYLE_REWRITE_REGRESSION_TOLERANCE = 0.01


# §6.3 multi-strategy diversification prompts for low-dispersion retry
_DIVERSIFICATION_PROMPT = (
    "[DIVERSIFICATION] 前一轮生成的候选在表达上高度相似。请刻意尝试不同的叙述入口：\n"
    "换一种感官开场（如果之前用了视觉，试听觉或触觉）、\n"
    "换一种时间结构（如果之前是顺叙，试倒叙或插叙的片段）、\n"
    "换一种节奏（如果之前是长句铺陈，试短句切入）。\n"
    "保持场景spec的所有结构要求不变，只改变'怎么去'。\n\n"
)
# §6.3 style emphasis rotation prefixes — rotate which style dimension the LLM focuses on
_STYLE_EMPHASIS_ROTATION: list[str] = [
    (
        "[风格强调·禁忌优先] 本次生成请特别关注参考风格中的禁忌模式——"
        "绝对避开被标记为禁忌的表达方式,并让'不做什么'成为本次风格选择的首要约束。\n\n"
    ),
    (
        "[风格强调·节奏分布优先] 本次生成关注风格参考中的整体节奏倾向——"
        "句群长短、段落功能与停顿习惯应自然呈现；不要为任何统计数字机械增删标点或拆段。\n\n"
    ),
]


def _progressive_top_up_variants(
    base_temp: float,
) -> list[tuple[float, str | None, str]]:
    """Wave 3（§5.5）渐进补候选的变体轮换：温度加宽 → 发散提示 → 风格侧重轮换。

    返回 (temperature, extra_system_prefix, strategy_label) 序列；补候选按序取用，
    每次只补 1 个。
    """
    variants: list[tuple[float, str | None, str]] = [
        (round(min(2.0, base_temp + 0.15), 3), None, "temperature_widen"),
        (
            round(min(2.0, base_temp + 0.10), 3),
            _DIVERSIFICATION_PROMPT,
            "prompt_variation",
        ),
    ]
    for idx, prefix in enumerate(_STYLE_EMPHASIS_ROTATION):
        variants.append(
            (
                round(min(2.0, base_temp + 0.05 * (idx + 1)), 3),
                prefix,
                f"style_emphasis_{idx}",
            )
        )
    return variants


def versioned_scene_artifact_id(
    prefix: str, scene_id: str, bundle: dict[str, Any]
) -> str:
    bundle_id = str(bundle.get("bundle_id") or "")
    bundle_prefix = f"bundle_{scene_id}_"
    if bundle_id.startswith(bundle_prefix):
        return f"{prefix}_{scene_id}_{bundle_id[len(bundle_prefix):]}"
    if bundle_id == f"bundle_{scene_id}":
        return f"{prefix}_{scene_id}"
    bundle_hash = str(bundle.get("bundle_snapshot_hash") or "")
    suffix = (
        bundle_hash[:12]
        if bundle_hash
        else hashlib.sha256(canonical_json(bundle).encode("utf-8")).hexdigest()[:12]
    )
    return f"{prefix}_{scene_id}_{suffix}"


def author_note_instruction(author_note: str | None) -> str:
    """Backward-compatible renderer; bundle injection now carries it to every stage."""
    return render_author_note_instruction(author_note)


def _author_note_instruction_for_bundle(
    bundle: dict[str, Any],
    author_note: str | None,
) -> str:
    note = str(author_note or "").strip()
    frozen = str(
        ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
            "author_instruction"
        )
        or ""
    )
    return "" if note == frozen else author_note_instruction(author_note)


class SceneGenerationService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
    ) -> None:
        self.session = session
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)
        self._prompt_builder_instance: PromptBuilder | None = None

    def generate_neutral_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        author_note: str | None = None,
    ) -> NeutralGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        fallback_llm_call_id = f"llm_call_{scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None

        try:
            prompt = self._prompt_builder().build(bundle["snapshot"], "neutral_draft")
        except Exception as exc:
            self._persist_generation_failure(
                scene=scene,
                state=state,
                bundle=bundle,
                llm_call_id=fallback_llm_call_id,
                step="neutral_draft",
                execution_step_key="neutral_draft",
                started_at=started_at,
                task_config=None,
                prompt=prompt,
                request_summary={},
                exc=exc,
            )
            raise

        # neutral_draft 的职责是固定事件、因果与连续性。风格参考只在后续
        # style_draft / rewrite 阶段注入；否则会同时收到“保持中性”和“贴合
        # 参考风格”两组冲突指令，并让同一风格在两阶段重复施压。

        base_user_prompt = prompt["user_prompt"] + _author_note_instruction_for_bundle(
            bundle, author_note
        )
        user_prompt = base_user_prompt + _neutral_length_instruction(scene)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="neutral_draft",
                step="neutral_draft",
                prompt=prompt,
                user_prompt=user_prompt,
            )
            response = node_result.response
            neutral_content = _extract_scene_text(response)
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="neutral_draft",
                prompt=prompt,
                exc=exc,
            )
            if isinstance(exc, LLMNodeExecutionError):
                self._raise_original_runner_error(exc)
            raise

        neutral_row_id = versioned_scene_artifact_id("draft_neutral", scene_id, bundle)
        neutral_assessment = _assess_neutral_draft(scene, neutral_content)
        repair_audit: dict[str, Any] | None = None
        if not neutral_assessment["accepted"]:
            original_content = neutral_content
            original_result = node_result
            repair_prompt = "\n".join(
                [
                    base_user_prompt,
                    "",
                    "## Rejected Neutral Draft Requiring One Deterministic Repair",
                    original_content,
                    "",
                    "## Deterministic Neutral Repair Brief",
                    _neutral_repair_brief(
                        scene,
                        source_content=original_content,
                        assessment=neutral_assessment,
                    ),
                    _neutral_length_instruction(
                        scene,
                        previous_length=_visible_char_count(original_content),
                        retry=True,
                    ),
                ]
            ).strip()
            try:
                repaired_result = self._llm_runner.run(
                    scene_id=scene_id,
                    chapter_id=scene.chapter_id,
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    node_id="neutral_draft",
                    step="neutral_draft_repair",
                    prompt=prompt,
                    user_prompt=repair_prompt,
                    # 修复是受约束的局部编辑，不是第二次创作采样。降低随机性可显著
                    # 减少“补回一个事实，却把合格长度扩写出界”的连带回退。
                    temperature_override=0.1,
                )
                repaired_content = _extract_scene_text(repaired_result.response)
                repaired_assessment = _assess_neutral_draft(scene, repaired_content)
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                self._record_runner_failure_attempt(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    step="neutral_draft_repair",
                    prompt=prompt,
                    exc=exc,
                )
                # 第一遍 provider 调用已经真实消耗了一次业务尝试。若修复在
                # provider dispatch 前被预算/连续性门拒绝，通用失败记录不会计数，
                # 这里补记一次；无论哪类失败都不能把原始不合格稿伪装成完成稿。
                if not _counts_as_business_attempt(exc):
                    state.total_attempt_count += 1
                    self.session.flush()
                if isinstance(exc, LLMNodeExecutionError):
                    self._raise_original_runner_error(exc)
                raise
            else:
                repair_accepted = bool(repaired_assessment["accepted"])
                rejected_content = (
                    original_content if repair_accepted else repaired_content
                )
                rejected_result = (
                    original_result if repair_accepted else repaired_result
                )
                rejected_hash = hashlib.sha256(
                    rejected_content.encode("utf-8")
                ).hexdigest()[:10]
                rejected_row_id = (
                    f"{neutral_row_id}_rejected_{rejected_hash}"
                )
                self.session.add(
                    SceneDraft(
                        row_id=rejected_row_id,
                        scene_id=scene_id,
                        chapter_id=scene.chapter_id,
                        stage="neutral_rejected",
                        status="rejected",
                        content=rejected_content,
                        source_bundle_id=bundle["bundle_id"],
                        source_bundle_hash=bundle["bundle_snapshot_hash"],
                        generation_llm_call_id=rejected_result.llm_call_id,
                    )
                )
                if repair_accepted:
                    neutral_content = repaired_content
                    node_result = repaired_result
                    neutral_assessment = repaired_assessment
                repair_audit = {
                    "attempted": True,
                    "accepted": repair_accepted,
                    "original_llm_call_id": original_result.llm_call_id,
                    "repair_llm_call_id": repaired_result.llm_call_id,
                    "rejected_row_id": rejected_row_id,
                    "original_assessment": _assess_neutral_draft(
                        scene, original_content
                    ),
                    "repair_assessment": repaired_assessment,
                }
                if not repair_accepted:
                    self.session.add(
                        AttemptTracker(
                            scene_id=scene_id,
                            chapter_id=scene.chapter_id,
                            step="neutral_draft",
                            status="failed",
                            source_bundle_id=bundle["bundle_id"],
                            details_json={
                                "llm_call_id": repaired_result.llm_call_id,
                                "error_code": "NEUTRAL_DRAFT_REPAIR_INVALID",
                                "rejected_row_id": rejected_row_id,
                                "validation": repaired_assessment,
                                "repair": repair_audit,
                                "business_attempt_consumed": True,
                            },
                        )
                    )
                    state.current_bundle_id = bundle["bundle_id"]
                    state.current_bundle_hash = bundle["bundle_snapshot_hash"]
                    state.total_attempt_count += 1
                    self.session.flush()
                    raise DomainError(
                        "NEUTRAL_DRAFT_REPAIR_INVALID",
                        "neutral draft remained invalid after its single deterministic repair",
                        status_code=422,
                        details={
                            "reasons": list(repaired_assessment.get("reasons") or []),
                            "target_length_range": repaired_assessment.get(
                                "target_length_range"
                            ),
                            "visible_chars": repaired_assessment.get("visible_chars"),
                        },
                    )

        self.session.add(
            SceneDraft(
                row_id=neutral_row_id,
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                stage="neutral_draft",
                content=neutral_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()

        attempt_details: dict[str, Any] = {
            "row_id": neutral_row_id,
            "llm_call_id": node_result.llm_call_id,
        }
        if repair_audit is not None:
            attempt_details["validation"] = neutral_assessment
            attempt_details["repair"] = repair_audit
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                step="neutral_draft",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=attempt_details,
            )
        )
        self.session.flush()

        state.current_neutral_draft_row_id = neutral_row_id
        # 治理 §4.3：latest_valid 与 current_* 分轨——重写/失败路径清 current_* 时该指针保留
        state.latest_valid_draft_row_id = neutral_row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        state.total_attempt_count += 1
        self.session.flush()

        return NeutralGenerationResult(
            row_id=neutral_row_id,
            content=neutral_content,
            llm_call_id=node_result.llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key="neutral_draft",
        )

    def generate_style_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        return self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style", scene_id, bundle),
            stage="style_draft",
            llm_step="style_draft",
            neutral_content=neutral_content,
            source_label="Approved Neutral Draft",
            source_row_id=neutral_draft_row_id,
            extra_instruction=(
                "Apply the style prompt template without changing the approved facts."
                + _author_note_instruction_for_bundle(bundle, author_note)
            ),
            source_draft_row_id=neutral_draft_row_id,
            source_draft_content=neutral_content,
            client_kind="style",
            execution_step_key="style_draft:0",
            attempt_details_extra={"source_neutral_draft_row_id": neutral_draft_row_id},
            product_slot_key="initial:0",
            product_slot_order=0,
            resume_base=resume_base,
            product_callback=product_callback,
            step_reconciler=step_reconciler,
        )

    def generate_style_draft_candidates(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        n_candidates: int = 3,
        max_candidates: int | None = None,
        resume_candidates: list[StyleGenerationResult] | None = None,
        candidate_checkpoint: (
            Callable[[int, StyleGenerationResult], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
        resume_bases: dict[str, StyleGenerationResult] | None = None,
        resume_products: dict[str, StyleGenerationResult] | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
    ) -> list[StyleGenerationResult]:
        """Generate N style-draft candidates with evidence-gated style reranking.

        Wave 3（治理 §5.5）：低分散补救为**渐进补候选**——初始 n_candidates，
        分散度 <0.15 时在预算允许下逐个补到 max_candidates（关键 3→5、标准
        2→3），不再一次生成后整批无上限重试。

        风格评分默认 shadow，仅落可审计诊断；只有冻结的人评证据授权后，才可在
        adversarial 质量差距受限的候选间改序。连续复刻参考原文的候选由独立硬
        guard 后置，不依赖未校准的风格分数。
        """
        from novel_system.services.literary_quality import (
            adversarial_rank_score,
            get_dimension_weights,
        )

        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)

        # §6 dynamic quality weights — project-level style profile can shift
        # which adversarial dimensions matter most for this particular work.
        _project_weights = (
            get_dimension_weights(
                scene.project_id,
                self.session,
            )
            if scene and scene.project_id
            else None
        )
        # Evidence-gated per-cell strategy policies were retired; ranking always
        # uses the project/built-in dimension weights.
        quality_strategy_audit: dict[str, Any] = {
            "status": "project_or_builtin_weights",
            "matched_policy_id": None,
        }

        try:
            task_config = self._llm_runner.task_config("style_draft")
            base_temp = task_config.temperature
        except KeyError:
            base_temp = 0.7

        if n_candidates <= 1:
            temperatures = [base_temp]
        else:
            spread = 0.05
            temperatures = [
                round(base_temp + spread * (2 * i / (n_candidates - 1) - 1), 3)
                for i in range(n_candidates)
            ]
            temperatures = [max(0.0, min(2.0, t)) for t in temperatures]

        durable_products = dict(resume_products or {})
        durable_bases = dict(resume_bases or {})
        if not durable_products:
            durable_products.update(
                (f"initial:{index}", candidate)
                for index, candidate in enumerate(resume_candidates or [])
            )
        candidates: list[tuple[StyleGenerationResult, float]] = [
            (
                candidate,
                adversarial_rank_score(candidate.content, weights=_project_weights),
            )
            for candidate in durable_products.values()
        ]
        for idx, temp in enumerate(temperatures):
            slot_key = f"initial:{idx}"
            if slot_key in durable_products:
                continue
            cand_row_id = (
                versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
                + f"_{idx}"
            )
            try:
                if step_reconciler is not None and slot_key not in durable_bases:
                    step_reconciler(f"style_draft:{idx}")
                result = self._run_style_generation(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    row_id=cand_row_id,
                    stage="style_draft",
                    llm_step="style_draft",
                    neutral_content=neutral_content,
                    source_label="Approved Neutral Draft",
                    source_row_id=neutral_draft_row_id,
                    extra_instruction=(
                        "Apply the style prompt template without changing the approved facts."
                        + _author_note_instruction_for_bundle(bundle, author_note)
                    ),
                    source_draft_row_id=neutral_draft_row_id,
                    source_draft_content=neutral_content,
                    client_kind="style",
                    temperature_override=temp,
                    execution_step_key=f"style_draft:{idx}",
                    attempt_details_extra={
                        "source_neutral_draft_row_id": neutral_draft_row_id,
                        "candidate_index": idx,
                        "temperature_override": temp,
                        "n_candidates": n_candidates,
                        "quality_strategy": quality_strategy_audit,
                    },
                    product_slot_key=slot_key,
                    product_slot_order=idx,
                    resume_base=durable_bases.get(slot_key),
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
                score = adversarial_rank_score(result.content, weights=_project_weights)
                candidates.append((result, score))
                if candidate_checkpoint is not None:
                    candidate_checkpoint(idx, result)
            except (DomainError, LLMNodeExecutionError):
                _LOGGER.warning(
                    "candidate %d/%d failed for scene %s",
                    idx + 1,
                    n_candidates,
                    scene_id,
                )
                if candidate_checkpoint is not None or product_callback is not None:
                    raise
                continue

        if not candidates:
            return [
                self.generate_style_draft(
                    scene_id,
                    bundle,
                    neutral_draft_row_id=neutral_draft_row_id,
                    neutral_content=neutral_content,
                    author_note=author_note,
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
            ]

        candidates.sort(key=lambda pair: pair[1], reverse=True)

        # Wave 3（§5.5）：渐进补候选——每次只补 1 个（温度加宽 / 发散提示 /
        # 风格侧重轮换作为逐个变体来源），每步过预算闸，补到上限或分散达标即停。
        candidate_cap = max(n_candidates, max_candidates or n_candidates)
        if len(candidates) >= 2 and candidate_cap > len(candidates):
            from novel_system.services.scene_budget import budget_unit, can_spend

            variants = _progressive_top_up_variants(base_temp)
            known_top_up_indices = {
                int(slot_key.rsplit(":", 1)[-1])
                for slot_key in {*durable_products, *durable_bases}
                if slot_key.startswith("topup:")
                and slot_key.rsplit(":", 1)[-1].isdigit()
            }
            pending_top_up_indices = sorted(
                index
                for index in known_top_up_indices
                if f"topup:{index}" in durable_bases
                and f"topup:{index}" not in durable_products
            )
            top_up_index = max(known_top_up_indices, default=0)
            while len(candidates) < candidate_cap:
                dispersion = _candidate_dispersion([c.content for c, _ in candidates])
                pending_top_up_index = (
                    pending_top_up_indices.pop(0) if pending_top_up_indices else None
                )
                if pending_top_up_index is None and dispersion >= 0.15:
                    break
                if pending_top_up_index is None and not can_spend(
                    state, budget_unit(state)
                ):
                    _LOGGER.warning(
                        "budget exhausted — stop progressive candidate top-up for scene %s "
                        "(dispersion=%.3f, %d candidates)",
                        scene_id,
                        dispersion,
                        len(candidates),
                    )
                    break
                if pending_top_up_index is None:
                    top_up_index += 1
                else:
                    top_up_index = pending_top_up_index
                temp, prefix, strategy = variants[(top_up_index - 1) % len(variants)]
                _LOGGER.warning(
                    "low candidate dispersion (%.3f) for scene %s — progressive top-up #%d via %s (§5.5)",
                    dispersion,
                    scene_id,
                    top_up_index,
                    strategy,
                )
                top_up_row_id = (
                    versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
                    + f"_topup_{top_up_index}"
                )
                slot_key = f"topup:{top_up_index}"
                try:
                    if step_reconciler is not None and slot_key not in durable_bases:
                        step_reconciler(f"style_draft:topup:{top_up_index}")
                    result = self._run_style_generation(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        row_id=top_up_row_id,
                        stage="style_draft",
                        llm_step="style_draft",
                        neutral_content=neutral_content,
                        source_label="Approved Neutral Draft",
                        source_row_id=neutral_draft_row_id,
                        extra_instruction=(
                            "Apply the style prompt template without changing the approved facts."
                            + _author_note_instruction_for_bundle(bundle, author_note)
                        ),
                        source_draft_row_id=neutral_draft_row_id,
                        source_draft_content=neutral_content,
                        client_kind="style",
                        temperature_override=temp,
                        execution_step_key=f"style_draft:topup:{top_up_index}",
                        extra_system_prefix=prefix,
                        attempt_details_extra={
                            "source_neutral_draft_row_id": neutral_draft_row_id,
                            "candidate_index": f"topup_{top_up_index}",
                            "temperature_override": temp,
                            "n_candidates": n_candidates,
                            "max_candidates": candidate_cap,
                            "diversification_strategy": strategy,
                            "progressive_top_up": True,
                            "quality_strategy": quality_strategy_audit,
                        },
                        product_slot_key=slot_key,
                        product_slot_order=n_candidates + top_up_index - 1,
                        resume_base=durable_bases.get(slot_key),
                        product_callback=product_callback,
                        step_reconciler=step_reconciler,
                    )
                    candidates.append(
                        (
                            result,
                            adversarial_rank_score(
                                result.content, weights=_project_weights
                            ),
                        )
                    )
                    if candidate_checkpoint is not None:
                        candidate_checkpoint(len(candidates) - 1, result)
                except (DomainError, LLMNodeExecutionError):
                    # 失败即停：不无上限重试（Wave 3 项 5）
                    _LOGGER.warning(
                        "progressive top-up #%d failed for scene %s — stop",
                        top_up_index,
                        scene_id,
                    )
                    if candidate_checkpoint is not None or product_callback is not None:
                        raise
                    break
            candidates.sort(key=lambda pair: pair[1], reverse=True)

        quality_scores = {result.row_id: score for result, score in candidates}
        try:
            from novel_system.services.style_reference.candidate_rerank import (
                StyleCandidateReranker,
            )

            rerank = StyleCandidateReranker(self.session).rerank(
                scene,
                bundle,
                [result for result, _score in candidates],
                quality_scores=quality_scores,
            )
            for result in rerank.ordered_candidates:
                assessment = rerank.assessments[result.row_id].to_audit_dict()
                result.ranking_audit = {**assessment, "rerank": rerank.audit}
            candidates = [
                (result, quality_scores[result.row_id])
                for result in rerank.ordered_candidates
            ]
        except Exception as exc:
            # Candidate generation must remain deliverable if the optional local
            # scorer degrades.  Preserve the established quality order and expose
            # a typed, non-sensitive audit reason instead of silently changing it.
            _LOGGER.warning(
                "style candidate reranking degraded for scene %s",
                scene_id,
                exc_info=True,
            )
            for rank, (result, score) in enumerate(candidates):
                result.ranking_audit = {
                    "row_id": result.row_id,
                    "quality_score": round(float(score), 6),
                    "style_score": None,
                    "rank": rank,
                    "selected": rank == 0,
                    "selection_reason": "quality_order_rerank_degraded",
                    "rerank": {
                        "applied_mode": "off",
                        "reason": "reranker_internal_error",
                        "error_code": getattr(exc, "code", exc.__class__.__name__),
                    },
                }

        best_result = candidates[0][0]
        state.current_style_draft_row_id = best_result.row_id
        state.latest_valid_draft_row_id = best_result.row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        # §6 Defect D: persist dispersion score for author-facing quality signal
        if len(candidates) >= 2:
            final_dispersion = _candidate_dispersion([c.content for c, _ in candidates])
            state.candidate_dispersion_score = round(final_dispersion, 4)
        self.session.flush()

        return [result for result, _ in candidates]

    def generate_style_patch(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_style_draft_row_id: str,
        source_style_content: str,
        rewrite_brief: list[str],
        source_qc_report_id: str,
        execution_step_key: str = "soft_patch:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        result = self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style_patch", scene_id, bundle),
            stage="style_patch",
            llm_step="soft_patch",
            neutral_content=source_style_content,
            source_label="Current Style Draft",
            source_row_id=source_style_draft_row_id,
            extra_instruction="Apply only the controlled patch brief; do not rewrite the full scene.",
            patch_brief=rewrite_brief,
            source_draft_row_id=source_style_draft_row_id,
            source_draft_content=source_style_content,
            client_kind="patch",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_qc_report_id": source_qc_report_id,
                "source_style_draft_row_id": source_style_draft_row_id,
                "rewrite_brief": rewrite_brief,
            },
        )
        state.soft_patch_count += 1
        return result

    def generate_near_final_rewrite(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_draft_row_id: str,
        source_content: str,
        revision_brief: list[str],
        source_evaluation_id: str,
        execution_step_key: str = "near_final_rewrite:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        return self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id(
                "draft_near_final_rewrite", scene_id, bundle
            ),
            stage="near_final_rewrite",
            llm_step="scene_literary_rewrite",
            neutral_content=source_content,
            source_label="Near-Final Draft Under Review",
            source_row_id=source_draft_row_id,
            extra_instruction=(
                "Rewrite the full scene under the same facts. Treat the brief below as a literary rewrite brief, "
                "not a local patch request."
            ),
            patch_brief=revision_brief,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_content,
            client_kind="style",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_evaluation_id": source_evaluation_id,
                "source_style_draft_row_id": source_draft_row_id,
                "rewrite_brief": revision_brief,
            },
        )

    def _run_style_generation(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        row_id: str,
        stage: str,
        llm_step: str,
        neutral_content: str,
        source_label: str,
        source_row_id: str,
        extra_instruction: str,
        source_draft_row_id: str,
        source_draft_content: str,
        client_kind: str,
        patch_brief: list[str] | None = None,
        attempt_details_extra: dict[str, Any] | None = None,
        temperature_override: float | None = None,
        extra_system_prefix: str | None = None,
        execution_step_key: str | None = None,
        product_slot_key: str | None = None,
        product_slot_order: int | None = None,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
    ) -> StyleGenerationResult:
        fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None

        try:
            template_name = (
                "scene_literary_rewrite"
                if llm_step == "scene_literary_rewrite"
                else "style_draft"
            )
            prompt = self._prompt_builder().build(bundle["snapshot"], template_name)
        except Exception as exc:
            self._persist_generation_failure(
                scene=scene,
                state=state,
                bundle=bundle,
                llm_call_id=fallback_llm_call_id,
                step=llm_step,
                execution_step_key=execution_step_key,
                started_at=started_at,
                task_config=None,
                prompt=prompt,
                request_summary={},
                exc=exc,
                source_draft_row_id=source_draft_row_id,
            )
            raise

        # §6.3 diversification: prepend caller-supplied system prefix (prompt variation / style emphasis)
        if extra_system_prefix and prompt is not None:
            injected = dict(prompt)
            injected["system_prompt"] = extra_system_prefix + (
                prompt.get("system_prompt") or ""
            )
            prompt = injected
        base_prompt = prompt

        if stage == "style_draft":
            extra_instruction += _style_length_instruction(
                scene,
                source_length=_visible_char_count(neutral_content),
            )

        user_prompt = self._build_style_user_prompt(
            base_prompt["user_prompt"],
            neutral_content=neutral_content,
            source_label=source_label,
            source_row_id=source_row_id,
            extra_instruction=extra_instruction,
            patch_brief=patch_brief,
        )
        prompt = self._inject_style_reference(
            base_prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=neutral_content,
            final_user_prompt=user_prompt,
        )
        if resume_base is None:
            node_id = "style_patch" if llm_step == "soft_patch" else llm_step
            try:
                node_result = self._llm_runner.run(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    node_id=node_id,
                    step=llm_step,
                    prompt=prompt,
                    user_prompt=user_prompt,
                    source_draft_row_id=source_draft_row_id,
                    source_draft_content=source_draft_content,
                    temperature_override=temperature_override,
                    execution_step_key=execution_step_key,
                )
                style_content = _extract_scene_text(node_result.response)
                paragraph_shape_audit: dict[str, Any] | None = None
                if stage == "style_draft":
                    style_content, paragraph_shape_audit = (
                        _normalize_style_paragraph_shape(
                            bundle=bundle,
                            text=style_content,
                        )
                    )
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                self._record_runner_failure_attempt(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    step=llm_step,
                    prompt=prompt,
                    exc=exc,
                    source_draft_row_id=source_draft_row_id,
                )
                if isinstance(exc, LLMNodeExecutionError):
                    self._raise_original_runner_error(exc)
                raise

            base_safety = _assess_style_base_rewrite(
                scene=scene,
                source_content=neutral_content,
                rewritten_content=style_content,
            )
            rejected_candidate_row_id: str | None = None
            repair_source_row_id = row_id
            repair_source_content = style_content
            if stage == "style_draft" and not base_safety["accepted"]:
                rejected_hash = hashlib.sha256(
                    style_content.encode("utf-8")
                ).hexdigest()[:10]
                rejected_candidate_row_id = f"{row_id}_rejected_{rejected_hash}"
                self.session.add(
                    SceneDraft(
                        row_id=rejected_candidate_row_id,
                        scene_id=scene.scene_id,
                        chapter_id=scene.chapter_id,
                        stage="style_rejected",
                        status="rejected",
                        content=style_content,
                        source_bundle_id=bundle["bundle_id"],
                        source_bundle_hash=bundle["bundle_snapshot_hash"],
                        generation_llm_call_id=node_result.llm_call_id,
                    )
                )
                repair_source_row_id = rejected_candidate_row_id
                repair_source_content = style_content
                # 已批准的中性稿是安全降级真源。保留 provider 原稿为独立 rejected
                # 行，主 style_draft 行只承载可继续进入 QC/候选选择的安全文本。
                style_content = neutral_content
            self.session.add(
                SceneDraft(
                    row_id=row_id,
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    stage=stage,
                    content=style_content,
                    source_bundle_id=bundle["bundle_id"],
                    source_bundle_hash=bundle["bundle_snapshot_hash"],
                    generation_llm_call_id=node_result.llm_call_id,
                )
            )
            self.session.flush()

            self.session.add(
                AttemptTracker(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    step=llm_step,
                    status="completed",
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "row_id": row_id,
                        "llm_call_id": node_result.llm_call_id,
                        "source_draft_row_id": source_draft_row_id,
                        "base_safety": base_safety,
                        "rejected_candidate_row_id": rejected_candidate_row_id,
                        "content_source": (
                            "approved_neutral_fallback"
                            if rejected_candidate_row_id is not None
                            else "provider_style_output"
                        ),
                        **(
                            {
                                "paragraph_shape_normalization": (
                                    paragraph_shape_audit
                                )
                            }
                            if paragraph_shape_audit is not None
                            else {}
                        ),
                        **(
                            {
                                "style_reference_runtime": deepcopy(
                                    prompt["_style_reference_runtime_audit"]
                                )
                            }
                            if isinstance(
                                prompt.get("_style_reference_runtime_audit"), dict
                            )
                            else {}
                        ),
                        **(attempt_details_extra or {}),
                    },
                )
            )
            self.session.flush()

            state.current_style_draft_row_id = row_id
            state.latest_valid_draft_row_id = row_id
            state.current_bundle_id = bundle["bundle_id"]
            state.current_bundle_hash = bundle["bundle_snapshot_hash"]
            self.session.flush()
            base_result = StyleGenerationResult(
                row_id=row_id,
                content=style_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
            )
            if product_callback is not None and product_slot_key is not None:
                product_callback(
                    product_slot_key,
                    "base",
                    base_result,
                    {
                        "slot_order": product_slot_order,
                        "source_neutral_draft_row_id": source_draft_row_id,
                        "gate_decision": None,
                        "source_base_row_id": None,
                    },
                )
        else:
            if (
                resume_base.row_id != row_id
                or resume_base.bundle_id != bundle["bundle_id"]
                or resume_base.bundle_hash != bundle["bundle_snapshot_hash"]
                or resume_base.execution_step_key != execution_step_key
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "resumed style base does not match its locked work item",
                    status_code=409,
                )
            base_result = resume_base
            style_content = resume_base.content
            base_safety = _resume_base_safety(
                self.session,
                scene_id=scene.scene_id,
                row_id=resume_base.row_id,
                fallback=_assess_style_base_rewrite(
                    scene=scene,
                    source_content=neutral_content,
                    rewritten_content=style_content,
                ),
            )
            repair_source_row_id, repair_source_content = (
                _resume_style_repair_source(
                    self.session,
                    scene_id=scene.scene_id,
                    row_id=resume_base.row_id,
                    fallback_row_id=resume_base.row_id,
                    fallback_content=style_content,
                )
            )

        if stage == "style_draft":
            quality_source_content = (
                repair_source_content
                if not base_safety["accepted"]
                else style_content
            )
            quality_gate = _anti_template_quality_gate(
                quality_source_content,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
            )
            quality_gate["base_safety"] = base_safety
            style_anchor_audit = _assess_style_anchor_conformance(
                bundle=bundle,
                text=quality_source_content,
            )
            quality_gate["style_anchor_audit"] = style_anchor_audit
            if base_safety["accepted"] and style_anchor_audit.get("requires_repair"):
                quality_gate["triggered"] = True
                quality_gate["rewrite_pass"] = 1
                anchor_signal_id = f"quality:scene:{scene.scene_id}:style_structure"
                if "style_structure" not in quality_gate["risk_dimensions"]:
                    quality_gate["risk_dimensions"].insert(0, "style_structure")
                if anchor_signal_id not in quality_gate["quality_signal_ids"]:
                    quality_gate["quality_signal_ids"].insert(0, anchor_signal_id)
                quality_gate["findings"].insert(
                    0,
                    {
                        "dimension": "style_structure",
                        "severity": "taste",
                        "issue": "The safe style draft is outside a high-confidence reference-derived prose-shape envelope.",
                        "evidence_excerpt": "",
                        "recommendation": " ".join(
                            str(item)
                            for item in style_anchor_audit.get("repair_directions", [])
                            if str(item).strip()
                        ),
                        "quality_signal_id": anchor_signal_id,
                        "scene_id": scene.scene_id,
                        "chapter_id": scene.chapter_id,
                    },
                )
            if not base_safety["accepted"]:
                quality_gate["triggered"] = True
                quality_gate["rewrite_pass"] = 1
                if "style_safety" not in quality_gate["risk_dimensions"]:
                    quality_gate["risk_dimensions"].append("style_safety")
                safety_signal_id = f"quality:scene:{scene.scene_id}:style_safety"
                if safety_signal_id not in quality_gate["quality_signal_ids"]:
                    quality_gate["quality_signal_ids"].append(safety_signal_id)
                quality_gate["findings"].append(
                    {
                        "dimension": "style_safety",
                        "severity": "blocking",
                        "issue": "The first style rewrite violated deterministic fact, length, or text-integrity constraints.",
                        "evidence_excerpt": "",
                        "recommendation": (
                            "Repair the rejected style draft once: restore every required beat, stay inside the "
                            "scene target length band, and retain its safe style choices."
                        ),
                        "quality_signal_id": safety_signal_id,
                        "scene_id": scene.scene_id,
                        "chapter_id": scene.chapter_id,
                    }
                )
            if quality_gate["triggered"]:
                use_style_salvage = bool(
                    not base_safety["accepted"]
                    and _requires_style_salvage(base_safety)
                )
                de_template_step_key = (
                    (
                        f"{execution_step_key}:style_salvage"
                        if use_style_salvage
                        else f"{execution_step_key}:de_template"
                    )
                    if execution_step_key
                    else None
                )
                if step_reconciler is not None and de_template_step_key is not None:
                    step_reconciler(de_template_step_key)
                if use_style_salvage:
                    (
                        de_template_result,
                        de_template_outcome,
                    ) = self._run_style_salvage_pass(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        checkpoint_base_row_id=row_id,
                        rejected_style_row_id=repair_source_row_id,
                        rejected_style_content=repair_source_content,
                        neutral_row_id=source_draft_row_id,
                        neutral_content=neutral_content,
                        quality_gate=quality_gate,
                        execution_step_key=de_template_step_key,
                    )
                else:
                    (
                        de_template_result,
                        de_template_outcome,
                    ) = self._run_de_template_pass(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        base_prompt=base_prompt,
                        checkpoint_base_row_id=row_id,
                        source_row_id=(
                            repair_source_row_id
                            if not base_safety["accepted"]
                            else row_id
                        ),
                        source_content=quality_source_content,
                        authoritative_row_id=(
                            source_draft_row_id
                            if not base_safety["accepted"]
                            else None
                        ),
                        authoritative_content=(
                            neutral_content if not base_safety["accepted"] else None
                        ),
                        quality_gate=quality_gate,
                        execution_step_key=de_template_step_key,
                    )
                remaining_reasons = set(
                    (de_template_outcome.get("acceptance") or {}).get("reasons")
                    or []
                )
                if (
                    de_template_result is None
                    and not base_safety["accepted"]
                    and not use_style_salvage
                    and remaining_reasons == {"target_length_not_met"}
                ):
                    # 第一遍整篇安全修复可能已恢复事实/完整性，
                    # 但仍略超出长度带。此时问题已收敛为纯长度，只允许
                    # 再走一次编号式局部补丁，不再整篇重写。
                    followup_row_id = de_template_outcome.get("row_id")
                    followup_source = (
                        self.session.get(SceneDraft, followup_row_id)
                        if isinstance(followup_row_id, str) and followup_row_id
                        else None
                    )
                    if followup_source is not None:
                        followup_step_key = (
                            f"{de_template_step_key}:length_patch_followup"
                            if de_template_step_key
                            else None
                        )
                        if (
                            step_reconciler is not None
                            and followup_step_key is not None
                        ):
                            step_reconciler(followup_step_key)
                        followup_quality_gate = deepcopy(quality_gate)
                        followup_quality_gate["base_safety"] = deepcopy(
                            de_template_outcome["acceptance"]
                        )
                        prior_repair_outcome = de_template_outcome
                        (
                            de_template_result,
                            de_template_outcome,
                        ) = self._run_de_template_pass(
                            scene=scene,
                            state=state,
                            bundle=bundle,
                            base_prompt=base_prompt,
                            checkpoint_base_row_id=row_id,
                            source_row_id=followup_source.row_id,
                            source_content=followup_source.content,
                            authoritative_row_id=source_draft_row_id,
                            authoritative_content=neutral_content,
                            quality_gate=followup_quality_gate,
                            execution_step_key=followup_step_key,
                        )
                        de_template_outcome["prior_repair_outcome"] = (
                            prior_repair_outcome
                        )
                if de_template_result is not None:
                    if product_callback is not None and product_slot_key is not None:
                        product_callback(
                            product_slot_key,
                            "final",
                            de_template_result,
                            {
                                "slot_order": product_slot_order,
                                "source_neutral_draft_row_id": source_draft_row_id,
                                "gate_decision": quality_gate,
                                "source_base_row_id": base_result.row_id,
                                "de_template_outcome": de_template_outcome,
                            },
                        )
                    return de_template_result
            if product_callback is not None and product_slot_key is not None:
                product_callback(
                    product_slot_key,
                    "final",
                    base_result,
                    {
                        "slot_order": product_slot_order,
                        "source_neutral_draft_row_id": source_draft_row_id,
                        "gate_decision": quality_gate,
                        "source_base_row_id": base_result.row_id,
                        "de_template_outcome": (
                            de_template_outcome
                            if quality_gate["triggered"]
                            else {"status": "not_required"}
                        ),
                    },
                )

        return base_result

    def _run_style_salvage_pass(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        checkpoint_base_row_id: str,
        rejected_style_row_id: str,
        rejected_style_content: str,
        neutral_row_id: str,
        neutral_content: str,
        quality_gate: dict[str, Any],
        execution_step_key: str | None,
    ) -> tuple[StyleGenerationResult | None, dict[str, Any]]:
        source_suffix = hashlib.sha1(
            rejected_style_row_id.encode("utf-8")
        ).hexdigest()[:10]
        row_id = (
            versioned_scene_artifact_id(
                "draft_style_salvage",
                scene.scene_id,
                bundle,
            )
            + f"_{source_suffix}"
        )
        prompt = self._prompt_builder().build(
            bundle["snapshot"],
            "style_salvage_patch",
        )
        editable_segment_ids = _style_salvage_editable_segment_ids(neutral_content)
        annotated_source, _ = _annotate_style_length_patch_source(
            neutral_content,
            editable_segment_ids=editable_segment_ids,
        )
        _constrain_style_salvage_schema(
            prompt,
            editable_segment_ids=editable_segment_ids,
        )
        user_prompt = self._build_style_user_prompt(
            prompt["user_prompt"],
            neutral_content=annotated_source,
            source_label="Segment-addressed Approved Neutral Draft for Style Salvage",
            source_row_id=neutral_row_id,
            extra_instruction=_style_salvage_instruction(
                scene,
                source_content=neutral_content,
                editable_segment_ids=editable_segment_ids,
            ),
        )
        prompt = self._inject_style_reference(
            prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=neutral_content,
            final_user_prompt=user_prompt,
        )
        salvage_audit: dict[str, Any]
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="style_patch",
                step="style_salvage_patch",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=neutral_row_id,
                source_draft_content=neutral_content,
                temperature_override=0.2,
                execution_step_key=execution_step_key,
            )
            try:
                rewritten_content, salvage_audit = _apply_style_salvage_patch(
                    source_content=neutral_content,
                    response=node_result.response,
                    scene=scene,
                    llm_call_id=node_result.llm_call_id,
                )
            except SceneGenerationPostprocessError as patch_exc:
                rewritten_content = neutral_content
                salvage_audit = {
                    "version": "style_salvage_patch_v1",
                    "valid": False,
                    "reason": str(patch_exc).removeprefix(
                        "style salvage patch invalid: "
                    ),
                }
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="style_salvage_patch",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=neutral_row_id,
            )
            return None, {
                "status": "failed",
                "llm_call_id": exc.llm_call_id,
                "execution_step_key": execution_step_key,
                "error_code": exc.error_code,
            }

        rewritten_content, paragraph_shape_audit = _normalize_style_paragraph_shape(
            bundle=bundle,
            text=rewritten_content,
        )
        conformance = _assess_style_rewrite_conformance(
            bundle=bundle,
            source_content=neutral_content,
            rewritten_content=rewritten_content,
        )
        acceptance = _assess_de_template_rewrite(
            scene=scene,
            source_content=neutral_content,
            authoritative_content=neutral_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=conformance,
        )
        salvage_reasons: list[str] = []
        if not salvage_audit.get("valid"):
            salvage_reasons.append("style_salvage_patch_invalid")
        if conformance.get("comparable") is not True:
            salvage_reasons.append("style_salvage_conformance_unavailable")
        elif float(conformance.get("score_delta") or 0.0) < -0.01:
            salvage_reasons.append("style_salvage_conformance_regressed")
        if salvage_reasons:
            acceptance["reasons"] = list(
                dict.fromkeys([*acceptance.get("reasons", []), *salvage_reasons])
            )
            acceptance["accepted"] = False
        acceptance["style_salvage_non_regression_enforced"] = True
        acceptance["style_salvage_regression_tolerance"] = 0.01

        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="style_salvage",
                status="active" if acceptance["accepted"] else "rejected",
                content=rewritten_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()
        details = {
            "row_id": row_id,
            "llm_call_id": node_result.llm_call_id,
            "source_style_draft_row_id": checkpoint_base_row_id,
            "rejected_style_seed_row_id": rejected_style_row_id,
            "rejected_style_seed_visible_chars": _visible_char_count(
                rejected_style_content
            ),
            "authoritative_source_row_id": neutral_row_id,
            "quality_gate": quality_gate,
            "acceptance": acceptance,
            "style_salvage": salvage_audit,
            "paragraph_shape_normalization": paragraph_shape_audit,
            **(
                {
                    "style_reference_runtime": deepcopy(
                        prompt["_style_reference_runtime_audit"]
                    )
                }
                if isinstance(prompt.get("_style_reference_runtime_audit"), dict)
                else {}
            ),
        }
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="style_salvage_patch",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details,
            )
        )
        self.session.flush()
        outcome = {
            "status": "completed" if acceptance["accepted"] else "rejected",
            "llm_call_id": node_result.llm_call_id,
            "execution_step_key": execution_step_key,
            "artifact_execution_id": current_llm_execution_id(),
            "accounting_status": "settled",
            "row_id": row_id,
            "acceptance": acceptance,
            "style_salvage": salvage_audit,
            "paragraph_shape_normalization": paragraph_shape_audit,
        }
        if not acceptance["accepted"]:
            return None, outcome
        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()
        return (
            StyleGenerationResult(
                row_id=row_id,
                content=rewritten_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
                artifact_execution_id=current_llm_execution_id(),
            ),
            outcome,
        )

    def _run_de_template_pass(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        base_prompt: dict[str, Any],
        checkpoint_base_row_id: str,
        source_row_id: str,
        source_content: str,
        authoritative_row_id: str | None,
        authoritative_content: str | None,
        quality_gate: dict[str, Any],
        execution_step_key: str | None,
    ) -> tuple[StyleGenerationResult | None, dict[str, Any]]:
        # 每个触发去模板的候选（source_row_id 已带 _{idx}/_retry_{idx}）必须派生唯一的去模板稿 row_id，
        # 否则 Best-of-N 下 ≥2 个候选都触发反模板闸时，第二条 SceneDraft 撞主键 → IntegrityError → 整跑崩溃。
        # SceneDraft.row_id 为 opaque 主键、不被下游解析，故追加 source_row_id 的短哈希后缀即可（唯一且长度有界）。
        source_suffix = hashlib.sha1(source_row_id.encode("utf-8")).hexdigest()[:10]
        row_id = f"{versioned_scene_artifact_id('draft_style_de_template', scene.scene_id, bundle)}_{source_suffix}"
        is_safety_repair = authoritative_content is not None
        base_safety_reasons = set(
            (quality_gate.get("base_safety") or {}).get("reasons") or []
        )
        is_length_patch = bool(
            is_safety_repair
            and base_safety_reasons == {"target_length_not_met"}
            and _parse_numeric_length_band(scene.target_length_band) is not None
        )
        length_patch_audit: dict[str, Any] | None = None
        if is_length_patch:
            # 整篇“修长度”在真实模型上会稳定退化成摘要。程序先给原文分段编号，
            # 模型只提交 segment_id + new_text；原文定位和套用不依赖模型复制精度。
            prompt = self._prompt_builder().build(
                bundle["snapshot"],
                "style_length_patch",
            )
            editable_segment_ids = _style_length_patch_editable_segment_ids(
                source_content,
                scene,
            )
            annotated_source, _ = _annotate_style_length_patch_source(
                source_content,
                editable_segment_ids=editable_segment_ids,
            )
            _constrain_style_length_patch_schema(
                prompt,
                editable_segment_ids=editable_segment_ids,
                scene=scene,
                source_length=_visible_char_count(source_content),
            )
            user_prompt = self._build_style_user_prompt(
                prompt["user_prompt"],
                neutral_content=annotated_source,
                source_label="Segment-addressed Length-only Rejected Style Draft",
                source_row_id=source_row_id,
                extra_instruction=_style_length_patch_instruction(
                    scene,
                    source_length=_visible_char_count(source_content),
                    editable_segment_ids=editable_segment_ids,
                ),
            )
        else:
            if is_safety_repair:
                repair_brief = _style_safety_repair_brief(
                    scene=scene,
                    source_content=source_content,
                    authoritative_content=authoritative_content,
                )
            else:
                repair_brief = _de_template_rewrite_brief(quality_gate)
            repair_length_instruction = _style_repair_length_instruction(
                scene,
                source_length=_visible_char_count(source_content),
            )
            user_prompt = self._build_style_user_prompt(
                (
                    _STYLE_SAFETY_REPAIR_TASK_PROMPT
                    if is_safety_repair
                    else _STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT
                ),
                neutral_content=source_content,
                source_label=(
                    "Rejected Style Draft Requiring One Safety Repair"
                    if authoritative_content is not None
                    else "Style Draft Requiring De-template Pass"
                ),
                source_row_id=source_row_id,
                extra_instruction=(
                    (
                        "Apply exactly one controlled safety repair. Preserve the rejected draft's reusable style; "
                        "fix only deterministic fact, length, forbidden-content, or text-integrity violations. "
                        "Do not perform a separate de-template rewrite or flatten the prose back to a neutral draft."
                        if is_safety_repair
                        else
                        "Apply exactly one controlled de-template repair. Preserve facts, names, chronology, "
                        "required objects, ending function, and the draft's reusable style. Fix only the listed "
                        "quality violations; preserve the reference-derived distribution tendencies without "
                        "turning them into counts or punctuation quotas, and do not flatten the prose back to a neutral draft."
                    )
                    + repair_length_instruction
                ),
                patch_brief=repair_brief,
                patch_heading=(
                    "Safety Repair Brief"
                    if is_safety_repair
                    else "De-template Rewrite Brief"
                ),
            )
        if is_safety_repair and not is_length_patch:
            # 这一遍只负责把已生成的风格稿恢复到事实、长度与正文完整性硬约束内。
            # 再注入完整画像会按“不合格源稿”的异常篇幅重算段数/分号目标，并把
            # 一个局部修复重新变成风格重写；真实基准中这会诱发过度压缩与事实丢失。
            # 被拒稿本身已承载可复用风格，故安全修复只使用冻结的原始 style 模板。
            prompt = dict(base_prompt)
        elif not is_length_patch:
            prompt = self._inject_style_reference(
                base_prompt,
                scene,
                task_type="scene_generation",
                bundle=bundle,
                context_text=source_content,
                final_user_prompt=user_prompt,
            )
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="style_patch",
                step="de_template",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=source_row_id,
                source_draft_content=source_content,
                # 二改是受约束的局部修补，不应继续用创作采样温度。安全
                # 修复最保守；普通去模板仍留少量改写空间。
                temperature_override=0.1 if is_safety_repair else 0.3,
                execution_step_key=execution_step_key,
            )
            if is_length_patch:
                try:
                    rewritten_content, length_patch_audit = (
                        _apply_style_length_patch(
                            source_content=source_content,
                            response=node_result.response,
                            scene=scene,
                            llm_call_id=node_result.llm_call_id,
                        )
                    )
                except SceneGenerationPostprocessError as patch_exc:
                    # Provider 已成功结算，但 replacement 本身不可安全套用。
                    # 保留原拒稿形成 settled+rejected 审计产物，不能伪报成
                    # provider failed，也不能让无效局部补丁触碰正文。
                    rewritten_content = source_content
                    length_patch_audit = {
                        "version": "style_length_patch_v3",
                        "valid": False,
                        "reason": str(patch_exc).removeprefix(
                            "style length patch invalid: "
                        ),
                    }
            else:
                rewritten_content = _extract_scene_text(node_result.response)
            rewritten_content, paragraph_shape_audit = (
                _normalize_style_paragraph_shape(
                    bundle=bundle,
                    text=rewritten_content,
                )
            )
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="de_template",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=source_row_id,
            )
            call = (
                self.session.get(LlmCall, exc.llm_call_id)
                if exc.llm_call_id
                else None
            )
            return None, {
                "status": "failed",
                "llm_call_id": exc.llm_call_id,
                "execution_step_key": execution_step_key,
                "artifact_execution_id": (
                    call.execution_id
                    if call is not None
                    else current_llm_execution_id()
                ),
                "accounting_status": (
                    call.accounting_status if call is not None else None
                ),
                "error_code": exc.error_code,
            }

        acceptance = _assess_de_template_rewrite(
            scene=scene,
            source_content=source_content,
            authoritative_content=authoritative_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=_assess_style_rewrite_conformance(
                bundle=bundle,
                source_content=source_content,
                rewritten_content=rewritten_content,
            ),
        )

        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="de_template",
                status="active" if acceptance["accepted"] else "rejected",
                content=rewritten_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()

        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="de_template",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "row_id": row_id,
                    "llm_call_id": node_result.llm_call_id,
                    # Durable work-item checkpoint 的既有契约以 base row 为父项；
                    # 安全修复实际读取的 rejected provider row 另列，避免伪装血缘。
                    "source_style_draft_row_id": checkpoint_base_row_id,
                    "repair_source_style_draft_row_id": source_row_id,
                    "authoritative_source_row_id": authoritative_row_id,
                    "quality_gate": quality_gate,
                    "acceptance": acceptance,
                    "paragraph_shape_normalization": paragraph_shape_audit,
                    **(
                        {"length_patch": length_patch_audit}
                        if length_patch_audit is not None
                        else {}
                    ),
                    **(
                        {
                            "style_reference_runtime": deepcopy(
                                prompt["_style_reference_runtime_audit"]
                            )
                        }
                        if isinstance(
                            prompt.get("_style_reference_runtime_audit"), dict
                        )
                        else {}
                    ),
                },
            )
        )
        self.session.flush()

        outcome = {
            "status": "completed" if acceptance["accepted"] else "rejected",
            "llm_call_id": node_result.llm_call_id,
            "execution_step_key": execution_step_key,
            "artifact_execution_id": current_llm_execution_id(),
            "accounting_status": "settled",
            "row_id": row_id,
            "acceptance": acceptance,
            "repair_source_style_draft_row_id": source_row_id,
            "paragraph_shape_normalization": paragraph_shape_audit,
            **(
                {"length_patch": length_patch_audit}
                if length_patch_audit is not None
                else {}
            ),
        }
        if not acceptance["accepted"]:
            # 改写稿作为审计证据保留，但不能覆盖已验证的 base 指针。调用方收到
            # None 后会继续返回 base_result，并把 rejected outcome 写入 checkpoint。
            return None, outcome

        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()

        return (
            StyleGenerationResult(
                row_id=row_id,
                content=rewritten_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
            ),
            outcome,
        )

    @staticmethod
    def _build_style_user_prompt(
        base_prompt: str,
        *,
        neutral_content: str,
        source_label: str,
        source_row_id: str,
        extra_instruction: str,
        patch_brief: list[str] | None = None,
        patch_heading: str = "Patch Brief",
    ) -> str:
        prompt_parts = [
            base_prompt,
            "",
            f"## {source_label}",
            neutral_content,
            "",
            f"Source Draft Row ID: {source_row_id}",
            extra_instruction,
        ]
        if patch_brief:
            prompt_parts.extend(
                [
                    "",
                    f"## {patch_heading}",
                    "\n".join(f"- {item}" for item in patch_brief),
                ]
            )
        if JSON_SCHEMA_INSTRUCTION not in base_prompt:
            prompt_parts.extend(["", JSON_SCHEMA_INSTRUCTION])
        return "\n".join(prompt_parts).strip()

    def _prompt_builder(self) -> PromptBuilder:
        if self._prompt_builder_instance is None:
            self._prompt_builder_instance = PromptBuilder()
        return self._prompt_builder_instance

    def _inject_style_reference(
        self,
        prompt: dict[str, Any] | None,
        scene: SceneCard | None,
        *,
        task_type: str = "scene_generation",
        bundle: dict[str, Any] | None = None,
        context_text: str | None = None,
        final_user_prompt: str | None = None,
    ) -> dict[str, Any] | None:
        """PR-8 §5.1 — 把 active StyleProfile 注入到 prompt["system_prompt"] 头部。

        无 binding / project_id / profile 时 no-op；有候选 binding 但召回/渲染失败时
        吞掉异常、回退基础 prompt（风格注入是可选增强，不阻断 LLM 生成流程）。

        立项 C §12 — ``context_text``(续写最新正文)透传给 Strategy C(RAG),
        作为三粒度检索 query;长文续写循环按 refresh 周期刷新此值 → 召回随上下文变化
        (防漂移)。其余策略忽略此参数。
        """
        if prompt is None or scene is None:
            return prompt
        project_id = getattr(scene, "project_id", None)
        # PR-14/18 — character scope 用 pov ∪ onstage 匹配集(pov 优先)
        character_ids = ordered_character_ids(
            getattr(scene, "pov_character_id", None),
            getattr(scene, "onstage_chars_json", None),
        )
        # PR-15 — scene scope 用 scene_id 匹配(优先级最高)
        scene_id = getattr(scene, "scene_id", None)
        if not project_id and not character_ids and not scene_id:
            return prompt
        svc = InjectionService(self.session)
        # §9 Defect B: read drift_ptype_priority from bundle (set by bundle_builder
        # when drift guidance includes structured dimension data) so the few-shot
        # selection prioritizes exemplars relevant to drifted dimensions ("show > tell")
        snapshot = (
            bundle.get("snapshot")
            if bundle
            and isinstance(bundle, dict)
            and isinstance(bundle.get("snapshot"), dict)
            else bundle
        )
        if isinstance(snapshot, dict):
            drift_priority = (snapshot.get("inline_digests") or {}).get(
                "_drift_ptype_priority"
            )
            if drift_priority and isinstance(drift_priority, list):
                svc.drift_ptype_priority = drift_priority
        # All callers now share one bounded prose-context extractor. The initial
        # style pass supplies the neutral draft; continuation calls supply the
        # latest accumulated prose.
        from novel_system.services.style_reference.rag import load_rag_config

        context = extract_style_generation_context(
            context_text,
            source_kind="generation_source" if context_text else "profile_fallback",
            max_chars=int(load_rag_config().get("rag_context_query_max_chars", 2000)),
        )
        runtime_contract = None
        contract_state = None
        try:
            contract_state = resolve_style_runtime_contract_state(
                bundle,
                task_type=task_type,
            )
            runtime_contract = contract_state.contract
            if contract_state.error_code is not None:
                raise ValueError(contract_state.error_code)
            if runtime_contract is not None:
                fragments = svc.fragments_for_contract(
                    runtime_contract,
                    project_id=project_id,
                    context=context,
                    drift_ptype_priority=svc.drift_ptype_priority,
                )
            elif contract_state.mode == "absent":
                # This new bundle explicitly froze "no style binding". A binding
                # added later must not alter replay of the already-built scene.
                return prompt
            else:
                # Backward compatibility for old bundles created before the frozen
                # runtime contract. New bundles never re-resolve live bindings here.
                svc.context_text = context.query_text
                fragments = svc.fragments_for(
                    project_id,
                    task_type,
                    character_ids=character_ids,
                    scene_id=scene_id,
                )
            budget_fit_audit = None
            token_budget = prompt.get("token_budget") or {}
            target_input_tokens = token_budget.get("target_input_tokens")
            if final_user_prompt is not None and target_input_tokens is not None:
                fragments, budget_fit_audit = fit_fragments_to_input_budget(
                    fragments,
                    base_system_prompt=str(prompt.get("system_prompt") or ""),
                    user_prompt=final_user_prompt,
                    target_input_tokens=int(target_input_tokens),
                )
            prefix = fragments.to_system_prompt_prefix()
        except Exception as exc:  # noqa: BLE001
            # 风格注入是可选增强：召回/渲染失败时吞掉并回退到基础 prompt，不阻断 LLM 生成
            # 流程（顾问型降级，与离线退役无关）。
            _LOGGER.warning(
                "style_reference injection skipped for scene %s task %s: %s",
                getattr(scene, "scene_id", None),
                task_type,
                exc,
            )
            degraded = dict(prompt)
            degraded["_style_reference_runtime_audit"] = {
                "outcome": "degraded",
                "task_type": task_type,
                "context": context.audit_dict(),
                "runtime_contract_status": (
                    contract_state.status if contract_state is not None else None
                ),
                "runtime_contract_mode": (
                    contract_state.mode if contract_state is not None else None
                ),
                "error_code": (
                    contract_state.error_code
                    if contract_state is not None
                    and contract_state.error_code is not None
                    else getattr(exc, "code", exc.__class__.__name__)
                ),
            }
            return degraded
        if (
            not prefix
            and runtime_contract is None
            and not (budget_fit_audit or {}).get("compacted")
        ):
            # Preserve the established strict no-op contract for legacy scenes
            # with no applicable binding. The miss is already recorded by the
            # injection metric; there is no frozen lineage to attach to the LLM
            # request or attempt record.
            return prompt
        injected = dict(prompt)
        if prefix:
            injected["system_prompt"] = prefix + (prompt.get("system_prompt") or "")
        if svc.last_runtime_audit is not None:
            assert contract_state is not None
            injected["_style_reference_runtime_audit"] = {
                **svc.last_runtime_audit,
                "runtime_contract_status": contract_state.status,
                "runtime_contract_mode": contract_state.mode,
            }
            if budget_fit_audit is not None:
                injected["_style_reference_runtime_audit"].update(
                    {
                        "outcome": (
                            "hit"
                            if prefix
                            else "degraded_budget"
                        ),
                        "prefix_chars": len(prefix),
                        "prefix_sha256": hashlib.sha256(
                            prefix.encode("utf-8")
                        ).hexdigest(),
                        "budget_fit": budget_fit_audit,
                    }
                )
        return injected

    def _record_runner_failure_attempt(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        step: str,
        prompt: dict[str, Any],
        exc: LLMNodeExecutionError | SceneGenerationPostprocessError,
        source_draft_row_id: str | None = None,
    ) -> None:
        details_json: dict[str, Any] = {
            "llm_call_id": exc.llm_call_id,
            "error_code": exc.error_code,
            "message": str(getattr(exc, "message", None) or str(exc)),
            "retryable": bool(getattr(exc, "retryable", False)),
            "business_attempt_consumed": _counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
            if isinstance(prompt.get("_style_reference_runtime_audit"), dict):
                details_json["style_reference_runtime"] = deepcopy(
                    prompt["_style_reference_runtime_audit"]
                )
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        if isinstance(exc, LLMNodeContinuityError):
            details_json["continuity_warning"] = exc.continuity_warning
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step=step,
                status="failed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details_json,
            )
        )
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if _counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()

    @staticmethod
    def _raise_original_runner_error(exc: LLMNodeExecutionError) -> None:
        if isinstance(exc, LLMNodeContinuityError):
            raise DomainError(
                CONTINUITY_BUDGET_ERROR_CODE,
                CONTINUITY_BUDGET_MESSAGE,
                status_code=409,
                details={
                    "continuity_warning": exc.continuity_warning,
                    "recommended_action": SCENE_SPLIT_RECOMMENDATION,
                },
            ) from exc
        if exc.original_error is not None:
            raise exc.original_error
        raise exc

    def _persist_generation_failure(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        llm_call_id: str,
        step: str,
        execution_step_key: str | None = None,
        started_at: float,
        task_config: Any | None,
        prompt: dict[str, Any] | None,
        request_summary: dict[str, Any],
        exc: Exception,
        source_draft_row_id: str | None = None,
    ) -> None:
        error_code = getattr(exc, "code", exc.__class__.__name__)
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                scope_type="scene",
                scope_id=scene.scene_id,
                provider=getattr(task_config, "provider", None),
                provider_id=getattr(task_config, "provider_id", None),
                account_id=getattr(task_config, "account_id", None),
                model=getattr(task_config, "model", None),
                node_id=step,
                reasoning_level=getattr(task_config, "reasoning_level", None),
                native_reasoning_json=None,
                credential_mode=getattr(task_config, "credential_mode", None),
                prompt_hash=(
                    prompt.get("prompt_hash") if isinstance(prompt, dict) else None
                ),
                step=step,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                execution_id=current_llm_execution_id(),
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="rejected",
                request_payload_summary=sanitize_audit_summary(request_summary),
                # error_audit_summary 已做过 sanitize，勿再包一层（重复 sanitize 幂等但多余）。
                response_payload_summary=error_audit_summary(exc),
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                finish_reason=None,
                error_code=error_code,
            )
        )
        self.session.flush()
        details_json: dict[str, Any] = {
            "llm_call_id": llm_call_id,
            "error_code": error_code,
            "message": str(exc),
            "execution_step_key": execution_step_key,
            "business_attempt_consumed": _counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step=step,
                status="failed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details_json,
            )
        )
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if _counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()


def _candidate_dispersion(texts: list[str]) -> float:
    """Measure pairwise surface dissimilarity of candidate texts (0=identical, 1=fully disjoint).

    Blueprint §6.3: dispersion is a necessary condition for surprise — if candidates
    are highly similar, sampling hasn't explored the tail.
    Uses character-level 4-gram Jaccard distance averaged over all pairs.
    """
    if len(texts) < 2:
        return 1.0

    def _char_ngrams(text: str, n: int = 4) -> set[str]:
        return {text[i : i + n] for i in range(max(0, len(text) - n + 1))}

    ngram_sets = [_char_ngrams(t) for t in texts]
    distances: list[float] = []
    for i in range(len(ngram_sets)):
        for j in range(i + 1, len(ngram_sets)):
            a, b = ngram_sets[i], ngram_sets[j]
            union = len(a | b)
            if union == 0:
                distances.append(0.0)
            else:
                distances.append(1.0 - len(a & b) / union)
    return sum(distances) / len(distances) if distances else 0.0


def _extract_scene_text(response: LLMResponse) -> str:
    structured_output = response.structured_output or {}
    scene_text = structured_output.get("scene_text")
    if isinstance(scene_text, str) and scene_text.strip():
        return _normalize_literal_unicode_escapes(scene_text.strip())
    # 中性/风格/补丁/续写各路径共用此提取器，消息不指认具体 stage（审计 P-17）
    raise SceneGenerationPostprocessError(
        llm_call_id=getattr(response, "llm_call_id", None),
        message="llm generation response missing scene_text",
    )


def _apply_style_length_patch(
    *,
    source_content: str,
    response: LLMResponse,
    scene: SceneCard,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """验证并套用模型提交的分段编号 replacement；任何歧义都整批拒绝。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style length patch invalid: {reason}",
        )

    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        reject("target_length_range_unavailable")
    assert length_range is not None
    minimum, maximum = length_range
    source_length = _visible_char_count(source_content)
    if minimum <= source_length <= maximum:
        reject("source_length_already_valid")
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    direction = "expand" if source_length < minimum else "compress"

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or not 1 <= len(raw_edits) <= 6:
        reject("edit_count_invalid")

    segments = _style_length_patch_segments(source_content)
    editable_segment_ids = _style_length_patch_editable_segment_ids(
        source_content,
        scene,
    )
    editable_segments = {
        segment["segment_id"]: segment
        for segment in segments
        if segment["segment_id"] in editable_segment_ids
    }
    if not editable_segments:
        reject("editable_segments_unavailable")
    edits: list[tuple[int, int, str, int, int, str]] = []
    submitted_segment_ids: list[str] = []
    for raw in raw_edits:
        if not isinstance(raw, dict):
            reject("edit_shape_invalid")
        segment_id = raw.get("segment_id")
        new_text = raw.get("new_text")
        if not isinstance(segment_id, str) or not segment_id.strip():
            reject("segment_id_invalid")
        if not isinstance(new_text, str):
            reject("new_text_invalid")
        segment_id = segment_id.strip().upper()
        segment = editable_segments.get(segment_id)
        if segment is None:
            reject("segment_id_not_editable")
        if segment_id in submitted_segment_ids:
            reject("segment_id_repeated")
        new_text = _normalize_literal_unicode_escapes(new_text)
        if "⟦S" in new_text or "SEGMENT" in new_text.upper():
            reject("segment_marker_leaked_into_new_text")
        if direction == "expand":
            start = int(segment["end"])
            end = start
            old_chars = 0
        else:
            start = int(segment["start"])
            end = int(segment["end"])
            old_chars = int(segment["visible_chars"])
        new_chars = _visible_char_count(new_text)
        delta = new_chars - old_chars
        if direction == "expand":
            if delta <= 0:
                reject("expansion_insertion_must_add_text")
        elif delta >= 0:
            reject("compression_segment_replacement_must_be_shorter")
        submitted_segment_ids.append(segment_id)
        edits.append((start, end, new_text, delta, old_chars, segment_id))

    edits.sort(key=lambda item: item[0])
    if any(
        left[1] > right[0]
        or (left[0] == left[1] == right[0] == right[1])
        for left, right in zip(edits, edits[1:])
    ):
        reject("edits_overlap")
    scope_limit = max(600, source_length // 2)
    best_choice: tuple[
        tuple[int, int, int, tuple[str, ...]],
        list[tuple[int, int, str, int, int, str]],
    ] | None = None
    for mask in range(1, 1 << len(edits)):
        selected = [
            edit for index, edit in enumerate(edits) if mask & (1 << index)
        ]
        selected_old_chars = sum(edit[4] for edit in selected)
        selected_delta = sum(edit[3] for edit in selected)
        if max(selected_old_chars, abs(selected_delta)) > scope_limit:
            continue
        candidate_length = source_length + selected_delta
        if not minimum <= candidate_length <= maximum:
            continue
        score = (
            0 if local_minimum <= candidate_length <= local_maximum else 1,
            abs(candidate_length - target),
            len(selected),
            tuple(edit[5] for edit in selected),
        )
        if best_choice is None or score < best_choice[0]:
            best_choice = (score, selected)
    if best_choice is None:
        reject("no_safe_edit_subset_reaches_target_range")
    selected_edits = best_choice[1]
    total_old_chars = sum(edit[4] for edit in selected_edits)
    total_delta = sum(edit[3] for edit in selected_edits)
    applied_segment_ids = [edit[5] for edit in selected_edits]

    patched = source_content
    for start, end, new_text, _delta, _old_chars, _segment_id in reversed(
        selected_edits
    ):
        patched = patched[:start] + new_text + patched[end:]
    output_length = _visible_char_count(patched)
    if not minimum <= output_length <= maximum:
        reject("patched_length_outside_absolute_range")
    if output_length - source_length != total_delta:
        reject("visible_delta_mismatch")

    return patched, {
        "version": "style_length_patch_v3",
        "valid": True,
        "mode": direction,
        "edit_count": len(selected_edits),
        "submitted_edit_count": len(edits),
        "omitted_edit_count": len(edits) - len(selected_edits),
        "segment_ids": applied_segment_ids,
        "source_visible_chars": source_length,
        "patched_visible_chars": output_length,
        "visible_delta": total_delta,
        "absolute_range": [minimum, maximum],
        "correction_window": [local_minimum, local_maximum],
        "correction_window_hit": local_minimum <= output_length <= local_maximum,
        "correction_target": target,
        "edited_source_visible_chars": total_old_chars,
        "deterministic_segment_address_validation": True,
        "non_overlapping": True,
    }


def _apply_style_salvage_patch(
    *,
    source_content: str,
    response: LLMResponse,
    scene: SceneCard,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """只替换一个预编号中段，保留中性安全稿的其余文字与结尾。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style salvage patch invalid: {reason}",
        )

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or len(raw_edits) != 1:
        reject("exactly_one_edit_required")
    raw = raw_edits[0]
    if not isinstance(raw, dict):
        reject("edit_shape_invalid")
    segment_id = raw.get("segment_id")
    new_text = raw.get("new_text")
    if not isinstance(segment_id, str) or not segment_id.strip():
        reject("segment_id_invalid")
    if not isinstance(new_text, str):
        reject("new_text_invalid")
    segment_id = segment_id.strip().upper()
    new_text = _normalize_literal_unicode_escapes(new_text).strip()
    editable_ids = _style_salvage_editable_segment_ids(source_content)
    segment_by_id = {
        str(segment["segment_id"]): segment
        for segment in _style_length_patch_segments(source_content)
    }
    segment = segment_by_id.get(segment_id)
    if segment_id not in editable_ids or segment is None:
        reject("segment_id_not_editable")
    if "⟦S" in new_text or "SEGMENT" in new_text.upper():
        reject("segment_marker_leaked_into_new_text")
    old_text = source_content[int(segment["start"]) : int(segment["end"])]
    old_chars = int(segment["visible_chars"])
    new_chars = _visible_char_count(new_text)
    minimum_chars = max(20, math.floor(old_chars * 0.50))
    maximum_chars = max(minimum_chars, math.ceil(old_chars * 1.35))
    if not minimum_chars <= new_chars <= maximum_chars:
        reject("replacement_length_outside_local_window")
    from novel_system.services.style_reference.validation.plagiarism import (
        normalize_text_for_matching,
    )

    if normalize_text_for_matching(old_text) == normalize_text_for_matching(new_text):
        reject("replacement_not_substantively_changed")
    start = int(segment["start"])
    end = int(segment["end"])
    patched = source_content[:start] + new_text + source_content[end:]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    output_chars = _visible_char_count(patched)
    if length_range is not None and not length_range[0] <= output_chars <= length_range[1]:
        reject("patched_length_outside_absolute_range")
    return patched, {
        "version": "style_salvage_patch_v1",
        "valid": True,
        "segment_id": segment_id,
        "source_visible_chars": _visible_char_count(source_content),
        "old_segment_visible_chars": old_chars,
        "new_segment_visible_chars": new_chars,
        "patched_visible_chars": output_chars,
        "replacement_window": [minimum_chars, maximum_chars],
        "substantive_change": True,
        "protected_ending": True,
    }


def _style_length_patch_segments(source_content: str) -> list[dict[str, Any]]:
    """将正文切成稳定的可定位段；最后一段由调用方固定保护。"""

    line_spans = [
        (match.start(), match.end())
        for match in re.finditer(r"[^\r\n]*\S[^\r\n]*", source_content)
    ]
    spans = line_spans
    if len(line_spans) == 1:
        line_start, line_end = line_spans[0]
        line_text = source_content[line_start:line_end]
        sentence_spans = [
            (line_start + match.start(), line_start + match.end())
            for match in re.finditer(
                r".+?(?:[。！？!?]”?|。?$)",
                line_text,
            )
            if match.group(0).strip()
        ]
        if len(sentence_spans) >= 2:
            spans = sentence_spans
    return [
        {
            "segment_id": f"S{index:03d}",
            "start": start,
            "end": end,
            "visible_chars": _visible_char_count(source_content[start:end]),
        }
        for index, (start, end) in enumerate(spans, start=1)
    ]


def _style_length_patch_editable_segment_ids(
    source_content: str,
    scene: SceneCard,
) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    candidates = segments[:-1]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    source_length = _visible_char_count(source_content)
    if length_range is None or source_length < length_range[0]:
        return [str(segment["segment_id"]) for segment in candidates]

    minimum, maximum = length_range
    _local_minimum, local_maximum, _target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    desired_reduction = max(1, source_length - local_maximum)
    minimum_useful_chars = max(24, min(80, math.ceil(desired_reduction / 6)))
    useful = [
        segment
        for segment in candidates
        if int(segment["visible_chars"]) >= minimum_useful_chars
    ]
    if not useful:
        useful = [max(candidates, key=lambda segment: int(segment["visible_chars"]))]
    return [str(segment["segment_id"]) for segment in useful]


def _style_salvage_editable_segment_ids(source_content: str) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    source_length = max(1, _visible_char_count(source_content))
    candidates = [
        segment
        for segment in segments[:-1]
        if int(segment["visible_chars"]) >= 60
        and 0.10
        <= int(segment["visible_chars"]) / source_length
        <= 0.35
    ]
    if not candidates:
        candidates = [
            segment
            for segment in segments[:-1]
            if int(segment["visible_chars"]) >= 40
            and int(segment["visible_chars"]) / source_length <= 0.45
        ]
    candidates = sorted(
        candidates,
        key=lambda segment: (
            abs(int(segment["visible_chars"]) / source_length - 0.22),
            int(str(segment["segment_id"])[1:]),
        ),
    )[:4]
    candidate_ids = {str(segment["segment_id"]) for segment in candidates}
    return [
        str(segment["segment_id"])
        for segment in segments
        if str(segment["segment_id"]) in candidate_ids
    ]


def _annotate_style_length_patch_source(
    source_content: str,
    *,
    editable_segment_ids: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    segments = _style_length_patch_segments(source_content)
    if not segments:
        return source_content, []
    editable_ids = (
        [str(value) for value in editable_segment_ids]
        if editable_segment_ids is not None
        else [str(segment["segment_id"]) for segment in segments[:-1]]
    )
    editable_set = set(editable_ids)
    parts: list[str] = []
    cursor = 0
    for index, segment in enumerate(segments):
        start = int(segment["start"])
        end = int(segment["end"])
        segment_id = str(segment["segment_id"])
        parts.append(source_content[cursor:start])
        marker = (
            f"⟦{segment_id}:PROTECTED_ENDING⟧"
            if index == len(segments) - 1
            else (
                f"⟦{segment_id}⟧"
                if segment_id in editable_set
                else f"⟦{segment_id}:PROTECTED⟧"
            )
        )
        parts.append(marker)
        parts.append(source_content[start:end])
        cursor = end
    parts.append(source_content[cursor:])
    return "".join(parts), editable_ids


def _constrain_style_length_patch_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
    scene: SceneCard,
    source_length: int,
) -> None:
    """把本次可编辑 ID 收紧为 JSON Schema enum，并刷新审计 hash。"""

    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        item_schema = edits_schema["items"]
        properties = item_schema["properties"]
        segment_schema = properties["segment_id"]
        new_text_schema = properties["new_text"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
        edits_schema["maxItems"] = min(6, len(editable_segment_ids))
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is not None and source_length < length_range[0]:
        local_minimum, local_maximum, _target = _style_repair_working_window(
            *length_range,
            source_length=source_length,
        )
        minimum_delta = max(1, local_minimum - source_length)
        maximum_delta = max(minimum_delta, local_maximum - source_length)
        item_count = min(
            len(editable_segment_ids),
            6,
            max(1, math.ceil(minimum_delta / 240)),
        )
        if item_count > 0:
            edits_schema["minItems"] = item_count
            edits_schema["maxItems"] = item_count
            new_text_schema["minLength"] = math.ceil(
                minimum_delta / item_count
            )
            new_text_schema["maxLength"] = max(
                new_text_schema["minLength"],
                maximum_delta // item_count,
            )
            new_text_schema["description"] = (
                f"One of exactly {item_count} insertions; all insertions together "
                f"must add {minimum_delta}-{maximum_delta} visible characters."
            )
    prompt["prompt_hash"] = hashlib.sha256(
        canonical_json(
            {
                "template_name": prompt.get("template_name"),
                "template_version": prompt.get("template_version"),
                "system_prompt": prompt.get("system_prompt"),
                "user_prompt": prompt.get("user_prompt"),
                "structured_schema": schema,
            }
        ).encode("utf-8")
    ).hexdigest()


def _constrain_style_salvage_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
) -> None:
    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        segment_schema = edits_schema["items"]["properties"]["segment_id"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
    edits_schema["minItems"] = 1
    edits_schema["maxItems"] = 1
    prompt["prompt_hash"] = hashlib.sha256(
        canonical_json(
            {
                "template_name": prompt.get("template_name"),
                "template_version": prompt.get("template_version"),
                "system_prompt": prompt.get("system_prompt"),
                "user_prompt": prompt.get("user_prompt"),
                "structured_schema": schema,
            }
        ).encode("utf-8")
    ).hexdigest()


_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_BARE_CJK_UNICODE_ESCAPE_RE = re.compile(
    r"(?<=[\u3400-\u9fff])u([0-9a-fA-F]{4})(?=$|[\s\u3000-\u303f\u3400-\u9fff，。！？；：、])"
)
_C1_CONTROL_RE = re.compile(r"[\u0080-\u009f]")
_ORPHAN_LOWERCASE_CJK_RE = re.compile(
    r"(?m)(?:^|[。！？!?；;：:]\s*)[A-Za-z](?=[\u3400-\u9fff])"
)
_MODEL_RESPONSE_ARTIFACT_RE = re.compile(
    r"(?i)(?:```(?:json)?|<ctrl\d+>|\blet me (?:refine|construct|rewrite|check)|"
    r"\bthe (?:actual json|draft looks|final json)|source draft row id|"
    r"[\"']scene_text[\"']\s*:)"
)
_NUMERIC_LENGTH_BAND_RE = re.compile(
    r"(?P<minimum>\d{2,6})\s*(?:-|–|—|~|～|至|到)\s*(?P<maximum>\d{2,6})"
)


def _normalize_literal_unicode_escapes(text: str) -> str:
    """只还原可无歧义识别的 Unicode 转义残片，不做通用 escape 解码。"""

    def replace_escaped(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        # 单个 surrogate 不是合法正文字符；保留给完整性门拒绝，避免制造坏串。
        if 0xD800 <= codepoint <= 0xDFFF:
            return match.group(0)
        return chr(codepoint)

    normalized = _LITERAL_UNICODE_ESCAPE_RE.sub(replace_escaped, text)

    def replace_bare_cjk(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        if 0x3000 <= codepoint <= 0x303F or 0x3400 <= codepoint <= 0x9FFF:
            return chr(codepoint)
        return match.group(0)

    return _BARE_CJK_UNICODE_ESCAPE_RE.sub(replace_bare_cjk, normalized)


def _scene_text_integrity_markers(text: str) -> list[str]:
    """返回不含正文内容的完整性标记，供改写验收和审计使用。"""
    markers: list[str] = []
    if "\ufffd" in text:
        markers.append("replacement_character")
    if "???" in text:
        markers.append("question_mark_placeholder")
    if _C1_CONTROL_RE.search(text):
        markers.append("c1_control_character")
    if _LITERAL_UNICODE_ESCAPE_RE.search(text):
        markers.append("literal_unicode_escape")
    if re.search(r"(?<![A-Za-z0-9_])u[0-9a-fA-F]{4}(?![A-Za-z0-9_])", text):
        markers.append("bare_unicode_escape")
    if _ORPHAN_LOWERCASE_CJK_RE.search(text):
        markers.append("orphan_ascii_before_cjk")
    if _MODEL_RESPONSE_ARTIFACT_RE.search(text):
        markers.append("model_response_artifact")
    return markers


def _assess_de_template_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str | None = None,
    rewritten_content: str,
    source_quality_gate: dict[str, Any],
    style_conformance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """确定性验收一次去模板改写；只阻止可证明的回退，不猜测作者审美。"""
    source_length = _visible_char_count(source_content)
    rewritten_length = _visible_char_count(rewritten_content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    source_length_score = _length_fitness(source_length, length_range)
    rewritten_length_score = _length_fitness(rewritten_length, length_range)

    safety_source_content = authoritative_content or source_content
    required_terms = constraint_terms(scene.must_include_text or "")
    source_required = {
        term
        for term in required_terms
        if source_field_satisfied(term, safety_source_content)
    }
    rewritten_required = {
        term for term in required_terms if source_field_satisfied(term, rewritten_content)
    }
    lost_required = sorted(source_required - rewritten_required)
    missing_required = sorted(set(required_terms) - rewritten_required)
    missing_without_regression = sorted(set(missing_required) - set(lost_required))

    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    source_forbidden = {
        term
        for term in forbidden_terms
        if contains_forbidden_term(term, safety_source_content)
    }
    rewritten_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, rewritten_content)
    }
    new_forbidden = sorted(rewritten_forbidden - source_forbidden)

    source_integrity = _scene_text_integrity_markers(safety_source_content)
    rewritten_integrity = _scene_text_integrity_markers(rewritten_content)
    new_integrity = sorted(set(rewritten_integrity) - set(source_integrity))

    rewritten_quality_gate = _anti_template_quality_gate(
        rewritten_content,
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
    )
    source_quality_score = float(source_quality_gate.get("score") or 0.0)
    rewritten_quality_score = float(rewritten_quality_gate.get("score") or 0.0)
    source_risk_count = len(source_quality_gate.get("findings") or [])
    rewritten_risk_count = len(rewritten_quality_gate.get("findings") or [])
    source_target_counts = _quality_gate_dimension_counts(source_quality_gate)
    rewritten_target_counts = _quality_gate_dimension_counts(rewritten_quality_gate)
    source_target_evidence_available = any(
        isinstance(finding, dict)
        and str(finding.get("dimension") or "").strip()
        in ANTI_TEMPLATE_GATE_DIMENSIONS
        for finding in (source_quality_gate.get("findings") or [])
    )
    resolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) < count
    )
    unresolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) >= count
    )
    new_quality_risk_dimensions = sorted(
        set(rewritten_target_counts) - set(source_target_counts)
    )
    worsened_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) > count
    )

    reasons: list[str] = []
    if rewritten_length < 20:
        reasons.append("rewrite_too_short")
    if lost_required:
        reasons.append("required_facts_regressed")
    if missing_without_regression:
        reasons.append("required_facts_missing")
    if new_forbidden:
        reasons.append("forbidden_content_added")
    if new_integrity or len(rewritten_integrity) > len(source_integrity):
        reasons.append("text_integrity_regressed")
    if length_range is not None:
        if rewritten_length_score < 1.0:
            reasons.append("target_length_not_met")
    elif source_length > 0:
        length_ratio = rewritten_length / source_length
        if length_ratio < 0.6:
            reasons.append("rewrite_collapsed")
        elif length_ratio > 1.8:
            reasons.append("rewrite_bloated")
    # 安全修复的唯一职责是把被拒风格稿恢复到事实/长度/禁词/文本完整性硬约束内。
    # 此时 source_quality_gate 还人为追加了 style_safety finding，且原稿本身不可交付；
    # 再要求启发式去模板分数不下降，会把已经安全、仍保留风格的修复稿错误退回中性稿。
    # 普通 de-template 改写仍维持严格非回退门。
    enforce_quality_non_regression = authoritative_content is None
    if enforce_quality_non_regression:
        if rewritten_quality_score + 0.005 < source_quality_score:
            reasons.append("anti_template_quality_regressed")
        if rewritten_risk_count > source_risk_count:
            reasons.append("anti_template_risks_increased")
        # A repair must demonstrably remove at least one of the exact dimensions
        # that triggered it.  A flat total-risk count previously accepted edits
        # that merely exchanged one defect for another or left every requested
        # defect untouched.
        # Only demand dimension-by-dimension proof when the source gate carries
        # its actionable findings.  Older checkpoints persisted only a compact
        # ``risk_dimensions`` list; treating that compatibility fallback as
        # full evidence would reject a valid completed rewrite on resume even
        # though the old record cannot support a before/after comparison.
        if source_target_evidence_available:
            if source_target_counts and not resolved_target_dimensions:
                reasons.append("target_quality_defects_not_reduced")
            if worsened_target_dimensions or new_quality_risk_dimensions:
                reasons.append("target_quality_defects_worsened")

    # 普通去模板改写只是对已安全风格稿做局部修补，不能用通用质量收益交换
    # 对冻结风格画像的可观测偏离。仅在两稿均达到候选评分的最低文本量、指标
    # 覆盖率和置信度时启用；安全修复仍以事实/长度/禁词/文本完整性为最高优先级。
    conformance = dict(style_conformance or {})
    enforce_style_non_regression = bool(
        authoritative_content is None and conformance.get("comparable") is True
    )
    if enforce_style_non_regression and conformance.get("regressed") is True:
        reasons.append("style_conformance_regressed")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "source_required_fact_matches": len(source_required),
        "rewritten_required_fact_matches": len(rewritten_required),
        "lost_required_fact_count": len(lost_required),
        "missing_required_fact_count": len(missing_required),
        "new_forbidden_count": len(new_forbidden),
        "source_visible_chars": source_length,
        "rewritten_visible_chars": rewritten_length,
        "target_length_range": list(length_range) if length_range is not None else None,
        "source_length_score": round(source_length_score, 4),
        "rewritten_length_score": round(rewritten_length_score, 4),
        "source_quality_score": round(source_quality_score, 4),
        "rewritten_quality_score": round(rewritten_quality_score, 4),
        "source_risk_count": source_risk_count,
        "rewritten_risk_count": rewritten_risk_count,
        "source_target_risk_counts": source_target_counts,
        "rewritten_target_risk_counts": rewritten_target_counts,
        "resolved_target_dimensions": resolved_target_dimensions,
        "unresolved_target_dimensions": unresolved_target_dimensions,
        "new_quality_risk_dimensions": new_quality_risk_dimensions,
        "worsened_target_dimensions": worsened_target_dimensions,
        "source_target_evidence_available": source_target_evidence_available,
        "quality_non_regression_enforced": enforce_quality_non_regression,
        "style_non_regression_enforced": enforce_style_non_regression,
        "style_conformance": conformance,
        "source_integrity_markers": source_integrity,
        "rewritten_integrity_markers": rewritten_integrity,
        "authoritative_source_used": authoritative_content is not None,
    }


def _quality_gate_dimension_counts(quality_gate: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    findings = quality_gate.get("findings") or []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        dimension = str(finding.get("dimension") or "").strip()
        if dimension not in ANTI_TEMPLATE_GATE_DIMENSIONS:
            continue
        counts[dimension] = counts.get(dimension, 0) + 1
    # Some recovery tests and old checkpoints persisted only risk_dimensions.
    # Use them as a one-count fallback without double-counting full findings.
    for raw_dimension in quality_gate.get("risk_dimensions") or []:
        dimension = str(raw_dimension or "").strip()
        if dimension in ANTI_TEMPLATE_GATE_DIMENSIONS and dimension not in counts:
            counts[dimension] = 1
    return dict(sorted(counts.items()))


def _assess_style_rewrite_conformance(
    *,
    bundle: dict[str, Any] | None,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """用冻结画像审计二次改写是否显著偏离，失败时保守地不启用门禁。

    这里复用候选重排的分组、容差和最低置信度，但不改变候选重排的 shadow
    发布状态：它只保护同一稿件的一次局部修补不发生可证明的风格回退。
    """

    base_audit: dict[str, Any] = {
        "version": "style_rewrite_non_regression_v1",
        "available": False,
        "comparable": False,
        "regressed": False,
        "regression_tolerance": _STYLE_REWRITE_REGRESSION_TOLERANCE,
    }
    try:
        contract_state = resolve_style_runtime_contract_state(bundle)
        base_audit.update(
            {
                "runtime_contract_status": contract_state.status,
                "runtime_contract_mode": contract_state.mode,
            }
        )
        if contract_state.error_code is not None:
            return {
                **base_audit,
                "unavailable_reason": contract_state.error_code,
            }
        if contract_state.contract is None:
            return {
                **base_audit,
                "unavailable_reason": "frozen_runtime_contract_unavailable",
            }

        # 局部导入保持 scene_generation 的基础导入路径轻量，并复用已经审计过的
        # 纯文本评分器；不读取当前活动画像，不使用作者身份、主题词或隐藏评测。
        from novel_system.services.style_reference.candidate_rerank import (
            CandidateRerankPolicy,
            assess_candidate_text,
            build_style_target,
        )

        profiles = contract_profile_objects(contract_state.contract)
        target = build_style_target(profiles)
        if target is None:
            return {
                **base_audit,
                "unavailable_reason": "frozen_metric_target_unavailable",
            }
        policy = CandidateRerankPolicy()
        source = assess_candidate_text(
            "source",
            source_content,
            0.0,
            target,
            policy,
        )
        rewritten = assess_candidate_text(
            "rewritten",
            rewritten_content,
            0.0,
            target,
            policy,
        )
        source_score = source.style_score
        rewritten_score = rewritten.style_score
        comparable = bool(
            source.style_eligible
            and rewritten.style_eligible
            and source_score is not None
            and rewritten_score is not None
        )
        delta = (
            float(rewritten_score) - float(source_score)
            if source_score is not None and rewritten_score is not None
            else None
        )

        def score_audit(assessment: Any) -> dict[str, Any]:
            return {
                "style_score": (
                    None
                    if assessment.style_score is None
                    else round(float(assessment.style_score), 6)
                ),
                "style_confidence": round(float(assessment.style_confidence), 6),
                "metric_count": int(assessment.metric_count),
                "substantive_chars": int(assessment.substantive_chars),
                "group_scores": {
                    key: round(float(value), 6)
                    for key, value in sorted(assessment.group_scores.items())
                },
                "top_deviations": list(assessment.top_deviations),
                "eligible": bool(assessment.style_eligible),
            }

        return {
            **base_audit,
            "available": True,
            "comparable": comparable,
            "target_hash": target.target_hash,
            "source": score_audit(source),
            "rewritten": score_audit(rewritten),
            "score_delta": None if delta is None else round(delta, 6),
            "regressed": bool(
                comparable
                and delta is not None
                and delta < -_STYLE_REWRITE_REGRESSION_TOLERANCE
            ),
            **(
                {"unavailable_reason": "minimum_evidence_not_met"}
                if not comparable
                else {}
            ),
        }
    except Exception:  # noqa: BLE001 — optional evidence gate must fail open
        _LOGGER.warning("style rewrite conformance audit degraded", exc_info=True)
        return {
            **base_audit,
            "unavailable_reason": "style_conformance_internal_error",
        }


def _merge_adjacent_style_paragraphs(
    paragraphs: list[str],
    target_count: int,
) -> str:
    """按累计可见字数选择相邻边界；只删除段间空白，不改正文序列。"""

    if target_count >= len(paragraphs):
        return "\n\n".join(paragraphs)
    target_count = max(1, target_count)
    lengths = [_visible_char_count(paragraph) for paragraph in paragraphs]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    total = prefix[-1]

    boundaries: list[int] = []
    previous = 0
    for group_index in range(1, target_count):
        minimum_boundary = previous + 1
        maximum_boundary = len(paragraphs) - (target_count - group_index)
        ideal_cumulative = total * group_index / target_count
        boundary = min(
            range(minimum_boundary, maximum_boundary + 1),
            key=lambda index: (abs(prefix[index] - ideal_cumulative), index),
        )
        boundaries.append(boundary)
        previous = boundary

    groups: list[str] = []
    start = 0
    for end in [*boundaries, len(paragraphs)]:
        groups.append("".join(paragraphs[start:end]))
        start = end
    return "\n\n".join(groups)


def _normalize_style_paragraph_shape(
    *,
    bundle: dict[str, Any] | None,
    text: str,
) -> tuple[str, dict[str, Any]]:
    """只在冻结画像明确要求时合并过密段落；从不自动拆段或改字。"""

    audit: dict[str, Any] = {
        "version": "style_paragraph_normalization_v1",
        "available": False,
        "applied": False,
        "operation": "merge_adjacent_only",
    }
    try:
        contract_state = resolve_style_runtime_contract_state(bundle)
        audit.update(
            {
                "runtime_contract_status": contract_state.status,
                "runtime_contract_mode": contract_state.mode,
            }
        )
        if contract_state.error_code is not None:
            return text, {**audit, "reason": contract_state.error_code}
        if contract_state.contract is None:
            return text, {
                **audit,
                "reason": "frozen_runtime_contract_unavailable",
            }

        from novel_system.services.style_reference.candidate_rerank import (
            build_style_target,
        )

        target = build_style_target(contract_profile_objects(contract_state.contract))
        if target is None:
            return text, {**audit, "reason": "style_target_unavailable"}
        paragraph_target = target.metrics.get("paragraphs_per_1k")
        if paragraph_target is None:
            return text, {
                **audit,
                "reason": "paragraph_target_unavailable",
                "target_hash": target.target_hash,
            }

        visible_chars = _visible_char_count(text)
        paragraphs = [
            part.strip()
            for part in re.split(r"\n\s*\n", text)
            if part.strip()
        ]
        current_count = len(paragraphs)
        audit.update(
            {
                "available": True,
                "target_hash": target.target_hash,
                "visible_chars": visible_chars,
                "before_paragraph_count": current_count,
                "target_rate": round(paragraph_target.mean, 4),
                "target_tolerance": round(paragraph_target.tolerance, 4),
            }
        )
        if visible_chars < 300 or current_count < 2:
            return text, {**audit, "reason": "minimum_evidence_not_met"}

        lower_rate = max(0.0, paragraph_target.mean - paragraph_target.tolerance)
        upper_rate = paragraph_target.mean + paragraph_target.tolerance
        current_rate = current_count * 1000.0 / visible_chars
        minimum_count = max(1, math.ceil(visible_chars * lower_rate / 1000.0))
        maximum_count = max(
            minimum_count,
            math.floor(visible_chars * upper_rate / 1000.0),
        )
        preferred_count = max(
            minimum_count,
            min(
                maximum_count,
                max(1, round(visible_chars * paragraph_target.mean / 1000.0)),
            ),
        )
        audit.update(
            {
                "before_rate": round(current_rate, 4),
                "acceptable_count_range": [minimum_count, maximum_count],
                "preferred_count": preferred_count,
            }
        )
        if current_rate <= upper_rate or preferred_count >= current_count:
            return text, {**audit, "reason": "not_over_segmented"}

        normalized = _merge_adjacent_style_paragraphs(
            paragraphs,
            preferred_count,
        )
        sequence_preserved = re.sub(r"\s+", "", normalized) == re.sub(
            r"\s+", "", text
        )
        if not sequence_preserved:
            return text, {**audit, "reason": "content_sequence_guard_failed"}
        after_count = len(
            [part for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        )
        return normalized, {
            **audit,
            "applied": True,
            "reason": "over_segmented_merged",
            "after_paragraph_count": after_count,
            "after_rate": round(after_count * 1000.0 / visible_chars, 4),
            "content_sequence_preserved": True,
        }
    except Exception:  # noqa: BLE001 — 可选形态整理必须 fail-open
        _LOGGER.warning("style paragraph normalization degraded", exc_info=True)
        return text, {**audit, "reason": "paragraph_normalization_internal_error"}


def _assess_style_anchor_conformance(
    *,
    bundle: dict[str, Any] | None,
    text: str,
) -> dict[str, Any]:
    """用隐藏统计识别明显形态偏差，只向二改暴露定性修复方向。"""

    audit: dict[str, Any] = {
        "version": "style_distribution_repair_v2",
        "available": False,
        "requires_repair": False,
        "violations": [],
        "repair_directions": [],
    }
    try:
        contract_state = resolve_style_runtime_contract_state(bundle)
        audit.update(
            {
                "runtime_contract_status": contract_state.status,
                "runtime_contract_mode": contract_state.mode,
            }
        )
        if contract_state.error_code is not None:
            return {**audit, "unavailable_reason": contract_state.error_code}
        if contract_state.contract is None:
            return {
                **audit,
                "unavailable_reason": "frozen_runtime_contract_unavailable",
            }

        from novel_system.services.style_reference.candidate_rerank import (
            build_style_target,
        )
        from novel_system.services.style_reference.validation.quantitative import (
            compute_generated_metrics,
        )

        target = build_style_target(contract_profile_objects(contract_state.contract))
        actual = compute_generated_metrics(text)
        visible_chars = _visible_char_count(text)
        if target is None or visible_chars < 300 or not actual:
            return {
                **audit,
                "unavailable_reason": "minimum_evidence_not_met",
                "visible_chars": visible_chars,
            }

        violations: list[dict[str, Any]] = []
        directions: list[str] = []

        paragraph_target = target.metrics.get("paragraphs_per_1k")
        if paragraph_target is not None and "paragraphs_per_1k" in actual:
            current_rate = float(actual["paragraphs_per_1k"])
            lower_rate = max(0.0, paragraph_target.mean - paragraph_target.tolerance)
            upper_rate = paragraph_target.mean + paragraph_target.tolerance
            if current_rate < lower_rate or current_rate > upper_rate:
                directions.append(
                    (
                        "Paragraph structure is substantially more fragmented than the reference tendency. "
                        "Merge adjacent fragments that perform the same narrative function; never merge across "
                        "a POV, action, time, or information-release boundary."
                    )
                    if current_rate > upper_rate
                    else (
                        "Paragraph structure is substantially denser than the reference tendency. "
                        "Split only where POV, action, time, or information function genuinely changes; "
                        "do not chase a paragraph count."
                    )
                )
                violations.append(
                    {
                        "metric": "paragraphs_per_1k",
                        "actual": round(current_rate, 4),
                        "target": round(paragraph_target.mean, 4),
                        "tolerance": round(paragraph_target.tolerance, 4),
                    }
                )

        semicolon_target = target.metrics.get("semicolon_density_per_1k")
        if semicolon_target is not None and "semicolon_density_per_1k" in actual:
            current_rate = float(actual["semicolon_density_per_1k"])
            upper_rate = semicolon_target.mean + semicolon_target.tolerance
            # “分号不足”不是文学缺陷。主动补足标点最容易导致统计投机和机械腔；
            # 仅在明显过量时要求删除无语义依据的分号。
            if current_rate > upper_rate:
                directions.append(
                    "Semicolon rhythm is substantially denser than the reference tendency. "
                    "Keep semicolons only between genuinely parallel or progressive clauses; "
                    "do not replace them with another repeated punctuation pattern."
                )
                violations.append(
                    {
                        "metric": "semicolon_density_per_1k",
                        "actual": round(current_rate, 4),
                        "target": round(semicolon_target.mean, 4),
                        "tolerance": round(semicolon_target.tolerance, 4),
                    }
                )

        return {
            **audit,
            "available": True,
            "requires_repair": bool(violations),
            "target_hash": target.target_hash,
            "visible_chars": visible_chars,
            "violations": violations,
            "repair_directions": directions,
        }
    except Exception:  # noqa: BLE001 — optional prompt guidance must fail open
        _LOGGER.warning("style anchor conformance audit degraded", exc_info=True)
        return {
            **audit,
            "unavailable_reason": "style_anchor_internal_error",
        }


def _assess_style_base_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """第一遍风格改写的硬安全门；审美质量留给后续质量门。"""
    required_terms = constraint_terms(scene.must_include_text or "")
    source_required = {
        term for term in required_terms if source_field_satisfied(term, source_content)
    }
    rewritten_required = {
        term for term in required_terms if source_field_satisfied(term, rewritten_content)
    }
    lost_required = sorted(source_required - rewritten_required)
    missing_required = sorted(set(required_terms) - rewritten_required)
    missing_without_regression = sorted(set(missing_required) - set(lost_required))

    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    source_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, source_content)
    }
    rewritten_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, rewritten_content)
    }
    new_forbidden = sorted(rewritten_forbidden - source_forbidden)

    source_integrity = _scene_text_integrity_markers(source_content)
    rewritten_integrity = _scene_text_integrity_markers(rewritten_content)
    new_integrity = sorted(set(rewritten_integrity) - set(source_integrity))
    rewritten_length = _visible_char_count(rewritten_content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    length_score = _length_fitness(rewritten_length, length_range)

    reasons: list[str] = []
    if rewritten_length < 20:
        reasons.append("rewrite_too_short")
    if lost_required:
        reasons.append("required_facts_regressed")
    if missing_without_regression:
        reasons.append("required_facts_missing")
    if new_forbidden:
        reasons.append("forbidden_content_added")
    if new_integrity or len(rewritten_integrity) > len(source_integrity):
        reasons.append("text_integrity_regressed")
    if length_range is not None and length_score < 1.0:
        reasons.append("target_length_not_met")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "source_required_fact_matches": len(source_required),
        "rewritten_required_fact_matches": len(rewritten_required),
        "lost_required_fact_count": len(lost_required),
        "missing_required_fact_count": len(missing_required),
        "new_forbidden_count": len(new_forbidden),
        "rewritten_visible_chars": rewritten_length,
        "target_length_range": list(length_range) if length_range is not None else None,
        "rewritten_length_score": round(length_score, 4),
        "source_integrity_markers": source_integrity,
        "rewritten_integrity_markers": rewritten_integrity,
    }


def _resume_base_safety(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") == row_id and isinstance(
            details.get("base_safety"), dict
        ):
            return dict(details["base_safety"])
    return fallback


def _resume_style_repair_source(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback_row_id: str,
    fallback_content: str,
) -> tuple[str, str]:
    """恢复 base checkpoint 时找回被拒绝的 provider 风格稿供一次定向修复。"""
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") != row_id:
            continue
        rejected_row_id = details.get("rejected_candidate_row_id")
        if not isinstance(rejected_row_id, str) or not rejected_row_id:
            break
        rejected = session.get(SceneDraft, rejected_row_id)
        if (
            rejected is not None
            and rejected.stage == "style_rejected"
            and rejected.status == "rejected"
            and rejected.content
        ):
            return rejected.row_id, rejected.content
        break
    return fallback_row_id, fallback_content


def _visible_char_count(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _parse_numeric_length_band(value: str | None) -> tuple[int, int] | None:
    match = _NUMERIC_LENGTH_BAND_RE.search(value or "")
    if match is None:
        return None
    minimum = int(match.group("minimum"))
    maximum = int(match.group("maximum"))
    if minimum <= 0 or maximum < minimum:
        return None
    return minimum, maximum


def _length_fitness(length: int, target: tuple[int, int] | None) -> float:
    if target is None:
        return 1.0
    minimum, maximum = target
    if minimum <= length <= maximum:
        return 1.0
    if length < minimum:
        return length / minimum
    return maximum / length


def _safe_length_window(minimum: int, maximum: int) -> tuple[int, int, int]:
    width = maximum - minimum
    margin = min(50, max(10, width // 10)) if width >= 40 else 0
    safe_minimum = minimum + margin
    safe_maximum = maximum - margin
    if safe_minimum > safe_maximum:
        safe_minimum, safe_maximum = minimum, maximum
    target = round((safe_minimum + safe_maximum) / 2)
    return safe_minimum, safe_maximum, target


def _style_repair_working_window(
    minimum: int,
    maximum: int,
    *,
    source_length: int,
) -> tuple[int, int, int]:
    """长度不合格时贴近最近安全边界修，不把局部校正变成整篇伸缩。"""

    safe_minimum, safe_maximum, safe_target = _safe_length_window(
        minimum,
        maximum,
    )
    if minimum <= source_length <= maximum:
        local_minimum = max(minimum, source_length * 9 // 10)
        local_maximum = min(maximum, (source_length * 11 + 9) // 10)
        if local_minimum <= local_maximum:
            return local_minimum, local_maximum, source_length
        return minimum, maximum, min(max(source_length, minimum), maximum)

    safe_width = max(0, safe_maximum - safe_minimum)
    correction_span = min(120, max(80, safe_width // 8))
    if source_length < minimum:
        local_minimum = safe_minimum
        local_maximum = min(safe_maximum, safe_minimum + correction_span)
    else:
        local_maximum = safe_maximum
        local_minimum = max(safe_minimum, safe_maximum - correction_span)
    target = round((local_minimum + local_maximum) / 2)
    if local_minimum > local_maximum:
        return safe_minimum, safe_maximum, safe_target
    return local_minimum, local_maximum, target


def _requires_style_salvage(base_safety: dict[str, Any]) -> bool:
    """只有事实安全但极端过短的风格稿才改为局部风格挽救。"""

    if set(base_safety.get("reasons") or []) != {"target_length_not_met"}:
        return False
    length_range = base_safety.get("target_length_range")
    rewritten_length = base_safety.get("rewritten_visible_chars")
    if (
        not isinstance(length_range, list)
        or len(length_range) != 2
        or not isinstance(length_range[0], int)
        or not isinstance(rewritten_length, int)
    ):
        return False
    return rewritten_length < math.ceil(length_range[0] * 0.6)


def _neutral_repair_brief(
    scene: SceneCard,
    *,
    source_content: str,
    assessment: dict[str, Any],
) -> str:
    """把中性稿的确定性失败逐项翻译成一次有界修复，不再误称为长度重试。"""

    required_terms = constraint_terms(scene.must_include_text or "")
    missing_terms = [
        term
        for term in required_terms
        if not source_field_satisfied(term, source_content)
    ]
    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    forbidden_hits = [
        term for term in forbidden_terms if contains_forbidden_term(term, source_content)
    ]
    integrity_markers = _scene_text_integrity_markers(source_content)
    lines = [
        "This is the only deterministic repair attempt. Edit the labeled draft directly and return one complete replacement scene only.",
        "Preserve every already-correct fact, causal step, character identity, chronology, and ending function.",
    ]
    if missing_terms:
        lines.append(
            "Restore each missing required constraint explicitly. A vertical bar means alternatives; include at least one literal alternative from every listed group: "
            + "；".join(missing_terms)
            + "。"
        )
    if forbidden_hits:
        lines.append(
            "Remove every currently present forbidden constraint without replacing it with a spelling variant: "
            + "；".join(forbidden_hits)
            + "。"
        )
    if integrity_markers:
        lines.append(
            "Remove response-format commentary, markdown/JSON wrappers, control tokens, malformed Unicode, and encoding artifacts; output Chinese scene prose only."
        )
    if "target_length_not_met" not in set(assessment.get("reasons") or []):
        lines.append(
            "The current length is already acceptable; do not broadly expand or compress it while fixing the listed issue."
        )
    return "\n".join(f"- {line}" for line in lines)


def _neutral_length_instruction(
    scene: SceneCard,
    *,
    previous_length: int | None = None,
    retry: bool = False,
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    prior = (
        f" The previous attempt was about {previous_length} visible characters and was rejected."
        if previous_length is not None
        else ""
    )
    retry_rule = (
        " Edit the labeled rejected draft directly and return one complete replacement scene, not commentary, a continuation, or a synopsis. Preserve every required fact, causal step, and ending function."
        if retry
        else ""
    )
    delta_rule = ""
    if retry and previous_length is not None:
        if previous_length < safe_minimum:
            delta_rule = (
                f" Add at least {safe_minimum - previous_length} visible characters inside existing action-reaction, blocking, perception, or consequence; do not add a new event."
            )
        elif previous_length > safe_maximum:
            delta_rule = (
                f" Remove at least {previous_length - safe_maximum} visible characters by compressing repetition and decorative description only; do not remove a required fact."
            )
        else:
            local_minimum = max(safe_minimum, previous_length * 9 // 10)
            local_maximum = min(
                safe_maximum,
                (previous_length * 11 + 9) // 10,
            )
            delta_rule = (
                f" The previous length already passed. Keep the repaired scene within {local_minimum}-{local_maximum} visible characters, make the smallest localized edits needed, and do not restage or broadly rewrite unchanged paragraphs."
            )
    return (
        "\n\n[Deterministic Scene Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters."
        f" Aim near {target}; use {safe_minimum}-{safe_maximum} as the working window so minor counting differences cannot cross the hard boundary."
        f"{prior}{retry_rule}{delta_rule} Before returning, count once and compress or expand existing action-reaction beats; preserve every required fact and do not add a new event."
    )


def _style_length_instruction(
    scene: SceneCard,
    *,
    source_length: int,
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    return (
        "\n\n[Deterministic Style Rewrite Length Guard]\n"
        f"The approved source is about {source_length} visible characters. The complete final rewrite must be "
        f"{minimum}-{maximum}; aim near {target} and keep {safe_minimum}-{safe_maximum} as the working window. "
        "Count once before returning. Style compression is not permission to drop a required beat or fall below "
        "the lower bound; expand or compress only existing action-reaction, blocking, perception, and consequence."
    )


def _style_repair_length_instruction(
    scene: SceneCard,
    *,
    source_length: int,
) -> str:
    """二改使用局部长度窗，防止修一个问题却把合格稿整体扩写或压缩。"""

    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        if source_length <= 0:
            return ""
        local_minimum = max(20, source_length * 9 // 10)
        local_maximum = max(
            local_minimum,
            (source_length * 11 + 9) // 10,
        )
        return (
            "\n\n[Deterministic Style Repair Length Guard]\n"
            f"Keep the complete repaired scene within {local_minimum}-{local_maximum} visible non-whitespace "
            f"characters (the source is about {source_length}). Make the smallest localized edits needed; "
            "do not restage, summarize, or broadly rewrite unchanged paragraphs."
        )

    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if minimum <= source_length <= maximum:
        local_rule = (
            f"The source already passes at about {source_length}; keep the repaired scene within "
            f"the local {local_minimum}-{local_maximum} window and make the smallest localized edits needed."
        )
    else:
        if source_length < minimum:
            delta_rule = (
                f"add {local_minimum - source_length}-{local_maximum - source_length} visible characters"
            )
        else:
            delta_rule = (
                f"remove {source_length - local_maximum}-{source_length - local_minimum} visible characters"
            )
        local_rule = (
            f"The source is about {source_length} and is outside the hard range; {delta_rule}, finish inside "
            f"the narrow {local_minimum}-{local_maximum} correction window, and aim near {target}. Change only "
            "existing action-reaction, blocking, perception, consequence, or removable repetition."
        )
    return (
        "\n\n[Deterministic Style Repair Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters. "
        f"{local_rule} Preserve every required fact, causal step, and ending function; count once before returning."
    )


def _style_length_patch_instruction(
    scene: SceneCard,
    *,
    source_length: int,
    editable_segment_ids: Sequence[str],
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if source_length < minimum:
        direction = (
            f"Expansion only: the combined replacements must add "
            f"{local_minimum - source_length}-{local_maximum - source_length} visible characters. "
            "For each selected segment_id, new_text is inserted immediately after that immutable source segment."
        )
    else:
        direction = (
            f"Compression only: the combined replacements must remove "
            f"{source_length - local_maximum}-{source_length - local_minimum} visible characters. "
            "For each selected segment_id, new_text replaces that one source segment and must be shorter. "
            "Delete only repetition or decorative description; do not replace omitted text with an ellipsis."
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Do not alter or remove any required constraint group: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Local Length Patch Contract]\n"
        f"The immutable source has about {source_length} visible characters. The final text after applying all edits "
        f"must be {local_minimum}-{local_maximum}, aiming near {target}; the absolute scene range is "
        f"{minimum}-{maximum}. {direction} Editable segment IDs: "
        f"{', '.join(editable_segment_ids) if editable_segment_ids else '(none)'}. "
        "The final source segment marked PROTECTED_ENDING is forbidden. Segment markers are addresses and must "
        "never appear in new_text."
        f"{required_rule} Return edits only, never scene_text or the complete scene."
    )


def _style_salvage_instruction(
    scene: SceneCard,
    *,
    source_content: str,
    editable_segment_ids: Sequence[str],
) -> str:
    segments = {
        str(segment["segment_id"]): int(segment["visible_chars"])
        for segment in _style_length_patch_segments(source_content)
    }
    windows = []
    for segment_id in editable_segment_ids:
        visible_chars = segments.get(segment_id, 0)
        windows.append(
            f"{segment_id}={max(20, math.floor(visible_chars * 0.50))}-"
            f"{max(20, math.ceil(visible_chars * 1.35))} visible characters"
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Preserve every required constraint group wherever it appears: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Bounded Style Salvage Contract]\n"
        "Replace exactly one editable segment; all other source characters and the protected ending remain "
        "immutable. Allowed segment windows: "
        + ("; ".join(windows) if windows else "(none)")
        + ". Make a substantive lexical/syntactic rewrite using the injected reusable style mechanisms, not a "
        "punctuation-only or whitespace-only change."
        + required_rule
        + " Return edits only, never scene_text or the complete scene."
    )


def _assess_neutral_draft(scene: SceneCard, content: str) -> dict[str, Any]:
    required_terms = constraint_terms(scene.must_include_text or "")
    missing_required = [
        term for term in required_terms if not source_field_satisfied(term, content)
    ]
    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    forbidden_hits = [
        term for term in forbidden_terms if contains_forbidden_term(term, content)
    ]
    integrity = _scene_text_integrity_markers(content)
    visible_chars = _visible_char_count(content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    length_score = _length_fitness(visible_chars, length_range)
    reasons: list[str] = []
    if visible_chars < 20:
        reasons.append("draft_too_short")
    if missing_required:
        reasons.append("required_facts_missing")
    if forbidden_hits:
        reasons.append("forbidden_content_present")
    if integrity:
        reasons.append("text_integrity_invalid")
    if length_range is not None and length_score < 1.0:
        reasons.append("target_length_not_met")
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "missing_required_fact_count": len(missing_required),
        "forbidden_hit_count": len(forbidden_hits),
        "visible_chars": visible_chars,
        "target_length_range": list(length_range) if length_range else None,
        "length_score": round(length_score, 4),
        "integrity_markers": integrity,
    }


def _anti_template_quality_gate(
    text: str, *, scene_id: str, chapter_id: str
) -> dict[str, Any]:
    signals, findings = analyze_literary_quality(text)
    gate_weight_total = sum(
        DIMENSION_WEIGHTS[dimension]
        for dimension in ANTI_TEMPLATE_GATE_DIMENSIONS
    )
    score = round(
        sum(
            signals[dimension]["score"] * DIMENSION_WEIGHTS[dimension]
            for dimension in ANTI_TEMPLATE_GATE_DIMENSIONS
        )
        / gate_weight_total,
        4,
    )
    risky_findings = [
        {
            **finding,
            "quality_signal_id": f"quality:scene:{scene_id}:{finding.get('dimension')}",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
        }
        for finding in findings
        if finding.get("dimension") in ANTI_TEMPLATE_GATE_DIMENSIONS
    ]
    triggered = bool(risky_findings)
    return {
        "triggered": triggered,
        "rewrite_pass": 1 if triggered else 0,
        "score": score,
        "risk_dimensions": [finding["dimension"] for finding in risky_findings],
        "quality_signal_ids": [
            finding["quality_signal_id"] for finding in risky_findings
        ],
        "findings": risky_findings,
    }


def _de_template_rewrite_brief(quality_gate: dict[str, Any]) -> list[str]:
    brief = [
        "Run no more than this one de-template pass; do not add another rewrite loop.",
        "Keep the same plot facts, speaker identities, core choice, cost, and final hook.",
        "Preserve the reference-derived broad rhythm and paragraph tendencies, but never keep or add an awkward sentence merely to match punctuation or length statistics.",
    ]
    for finding in quality_gate.get("findings", [])[:5]:
        signal_id = finding.get("quality_signal_id", "quality:unknown")
        issue = finding.get("issue") or "anti-template risk"
        evidence = finding.get("evidence_excerpt") or ""
        recommendation = finding.get("recommendation") or ""
        brief.append(f"{signal_id}: {issue}")
        if evidence:
            brief.append(f"Evidence: {evidence}")
        if recommendation:
            brief.append(f"Fix: {recommendation}")
    return brief


def _style_safety_repair_brief(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str,
) -> list[str]:
    """把确定性失败翻译成一次可执行、无正文泄漏的修复清单。"""
    del authoritative_content  # 仅表明调用方已提供可信事实基线；正文不进入提示。
    required_terms = constraint_terms(scene.must_include_text or "")
    missing_terms = [
        term
        for term in required_terms
        if not source_field_satisfied(term, source_content)
    ]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    current_length = _visible_char_count(source_content)
    brief = [
        "This is the only safety repair attempt. Edit the labeled rejected draft directly, keep its distinctive reusable style, and change only what the hard constraints require.",
        "Return only the complete replacement scene_text prose: no reasoning, markdown fence, JSON wrapper, schema label, or commentary.",
    ]
    if required_terms:
        brief.append(
            "Every final required constraint must be explicit. A vertical bar means alternatives; include at least one literal alternative from each group: "
            + "；".join(required_terms)
            + "。"
        )
    if missing_terms:
        brief.append(
            "Restore the currently missing required constraints: "
            + "；".join(missing_terms)
            + "。"
        )
    if length_range is not None:
        minimum, maximum = length_range
        local_minimum, local_maximum, target = _style_repair_working_window(
            minimum,
            maximum,
            source_length=current_length,
        )
        brief.append(
            f"Final visible Chinese prose length must be {minimum}-{maximum} characters; "
            f"the rejected draft is about {current_length}. Aim near {target} and keep the working "
            f"window at {local_minimum}-{local_maximum}; never use the absolute maximum as the target."
        )
        if current_length < minimum:
            brief.append(
                f"Add {local_minimum - current_length}-{local_maximum - current_length} visible characters; do not return fewer or more than that correction range. "
                "Keep every existing factual beat in order; add concrete action-reaction, blocking, perception, or consequence "
                f"inside the same event until {local_minimum}-{local_maximum} visible characters are present."
            )
        elif current_length > maximum:
            brief.append(
                f"Remove {current_length - local_maximum}-{current_length - local_minimum} visible characters by compressing repetition only, "
                f"then stop inside {local_minimum}-{local_maximum}; do not remove any required fact, causal step, or ending hook."
            )
    if _scene_text_integrity_markers(source_content):
        brief.append(
            "Remove malformed Unicode escapes, placeholder controls, and encoding artifacts while preserving the intended Chinese prose."
        )
    brief.append(
        "Use the Scene Card as factual authority. Do not invent a new event, change chronology, or replace the ending hook."
    )
    return brief
