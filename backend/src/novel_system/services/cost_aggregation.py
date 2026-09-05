"""token/费用聚合（结果闭环治理设计 §5.8/§10，Wave 6）。

基于现有 ``LlmCall``（token/延迟）+ ``SceneRunState``（5× 预算）建立聚合，
**不复制调用日志、不新增列**。价格层在 ``services/pricing.py``。

完成门：任意场景可解释——
- 总成本 + 币种（跨 provider 汇总以**费用**为准，§5.8）；
- 各阶段占比（候选生成 / QC / 修订 / 评审）；
- 是否超预算（5× 上限使用率，源自 SceneRunState）；
- 评审是否独立（observed → config 回退，见 model_independence）。

口径纪律（§5.8）：
- 跨 provider 的 token 不直接相加（分词器不同）——``tokens_by_provider`` 分列；
- 三口径 estimate / provider_actual / budget_charged，父调用只汇总一次；
- 额外成本可归因（失败重试 / 重复 QC / 低分散补候选）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
)
from novel_system.services import pricing

_LOGGER = logging.getLogger(__name__)

BUDGET_MULTIPLIER = 5

PHASE_CANDIDATE = "candidate_generation"
PHASE_QC = "quality_check"
PHASE_REVISION = "revision"
PHASE_REVIEW = "review"
PHASE_OTHER = "other"
_PHASES = (PHASE_CANDIDATE, PHASE_QC, PHASE_REVISION, PHASE_REVIEW, PHASE_OTHER)

# criticality → Best-of-N 初始候选数（与 scene_criticality 语义一致；超出即补候选）
_INITIAL_CANDIDATES = {"critical": 3, "standard": 2, "transition": 1}


def classify_phase(node_id: str | None, step: str | None = None) -> str:
    """把一次 LLM 调用归入四阶段之一（§5.8 候选生成/QC/修订/评审）+ other。

    候选生成不限于场景管线：雪花构思（`snowflake_step_generate` /
    `snowflake_step_candidates` / `snowflake_workspace_assistant`）、案头提案
    （`author_proposal_generate`）、大纲（`project_outline_plan`）等生成类节点
    同样计入——否则真实项目的成本大头全落在「其他」，阶段占比失去解释力。
    风格参考子系统 / 汇总压缩类节点仍归 other。
    """
    key = (node_id or step or "").lower()
    if not key:
        return PHASE_OTHER
    # revision 优先于 candidate：`scene_auto_rewrite`/`style_patch` 含 draft 语义但属修订
    if any(t in key for t in ("patch", "rewrite", "revision")):
        return PHASE_REVISION
    if any(
        t in key
        for t in ("qc", "near_final", "quality_contract", "validate")
    ):
        return PHASE_QC
    if any(
        t in key
        for t in ("deep_review", "diagnosis", "adjudicate", "critique", "literary_eval", "triage")
    ):
        return PHASE_REVIEW
    if any(
        t in key
        for t in (
            "draft", "blueprint", "continuation", "de_template", "architecture", "running",
            "generate", "generation", "candidates", "proposal", "assistant", "outline",
        )
    ):
        return PHASE_CANDIDATE
    return PHASE_OTHER


def _call_cost(call: LlmCall) -> dict[str, Any]:
    return pricing.compute_cost(
        call.provider,
        call.model,
        call.prompt_tokens,
        call.completion_tokens,
        at=call.created_at,
    )


def _attempt_cost(call: LlmCall, attempt: LlmCallAttempt) -> dict[str, Any]:
    return pricing.compute_cost(
        call.provider,
        call.model,
        attempt.prompt_tokens,
        attempt.completion_tokens,
        at=call.created_at,
    )


def _empty_phase_breakdown() -> dict[str, dict[str, Any]]:
    return {p: {"tokens": 0, "cost": 0.0, "share": 0.0, "call_count": 0} for p in _PHASES}


def _aggregate_calls(session: Session, calls: list[LlmCall]) -> dict[str, Any]:
    """把一组 LlmCall 折算为费用/阶段/provider 维度的通用聚合（scene/chapter/project 复用）。"""
    total_cost = 0.0
    total_tokens = 0
    is_estimate = False
    currency: str | None = None
    phase = _empty_phase_breakdown()
    tokens_by_provider: dict[str, int] = {}
    cost_by_provider: dict[str, float] = {}
    attempts_by_call: dict[str, list[LlmCallAttempt]] = {}
    if calls:
        attempts = session.execute(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id.in_([call.llm_call_id for call in calls]))
            .order_by(
                LlmCallAttempt.llm_call_id.asc(),
                LlmCallAttempt.provider_attempt_no.asc(),
            )
        ).scalars().all()
        for attempt in attempts:
            attempts_by_call.setdefault(attempt.llm_call_id, []).append(attempt)

    estimated_tokens = 0
    provider_actual_tokens = 0
    budget_charged_tokens = 0
    attempt_row_count = 0
    physical_attempt_count = 0
    pre_dispatch_attempt_count = 0
    usage_estimate_count = 0
    exception_count = 0
    retry_attempt_count = 0
    transport_retry_attempt_count = 0
    response_parse_retry_attempt_count = 0
    degrade_attempt_count = 0
    legacy_parent_without_attempt_count = 0
    legacy_unreconstructable_tokens = 0
    for call in calls:
        cost = _call_cost(call)
        tokens = int(call.total_tokens or 0)
        total_cost += cost["cost"]
        total_tokens += tokens
        is_estimate = (
            is_estimate
            or bool(cost["is_estimate"])
            or bool(call.usage_is_estimate)
        )
        currency = currency or cost["currency"]
        ph = classify_phase(call.node_id, call.step)
        phase[ph]["tokens"] += tokens
        phase[ph]["cost"] += cost["cost"]
        phase[ph]["call_count"] += 1
        prov = call.provider or "unknown"
        tokens_by_provider[prov] = tokens_by_provider.get(prov, 0) + tokens
        cost_by_provider[prov] = cost_by_provider.get(prov, 0.0) + cost["cost"]

        call_attempts = attempts_by_call.get(call.llm_call_id, [])
        if call_attempts:
            # 父调用是唯一逻辑/报表层；它的账目字段已是物理尝试之和。
            estimated_tokens += int(call.estimated_tokens or 0)
            budget_charged_tokens += int(call.budget_charged_tokens or 0)
            attempt_row_count += len(call_attempts)
            for attempt in call_attempts:
                dispatched = attempt.request_dispatched_at is not None
                if dispatched:
                    physical_attempt_count += 1
                else:
                    pre_dispatch_attempt_count += 1
                if dispatched and bool(attempt.usage_is_estimate):
                    usage_estimate_count += 1
                elif dispatched:
                    provider_actual_tokens += int(attempt.total_tokens or 0)
                if attempt.error_code:
                    exception_count += 1
                if dispatched and int(attempt.provider_attempt_no or 0) > 0:
                    retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind == "transport_retry":
                    # transport_retry 同时承载普通重试和连接能力降级，不能猜测拆分。
                    transport_retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind == "response_parse_retry":
                    response_parse_retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind in {
                    "api_mode_degrade",
                    "structured_output_degrade",
                    "missing_text_degrade",
                }:
                    degrade_attempt_count += 1
            continue

        request_summary = call.request_payload_summary or {}
        is_legacy_parent = "_accounting_provider_execution_mode" not in request_summary
        if is_legacy_parent:
            # 0064 前的父调用没有物理尝试行和新账目字段；显式兼容读取。
            legacy_parent_without_attempt_count += 1
            legacy_unreconstructable_tokens += tokens
            estimated_tokens += int(call.estimated_tokens or tokens)
            if call.usage_is_estimate is False:
                provider_actual_tokens += tokens
            # 0065 将 legacy charge 明确回填为 0；零值不能回退成 total。
            budget_charged_tokens += int(call.budget_charged_tokens or 0)
            usage_estimate_count += int(bool(call.usage_is_estimate))
            exception_count += int(bool(call.error_code))
        else:
            estimated_tokens += int(call.estimated_tokens or 0)
            budget_charged_tokens += int(call.budget_charged_tokens or 0)
    for ph in phase.values():
        ph["share"] = (ph["cost"] / total_cost) if total_cost > 0 else 0.0
    estimate_legacy_suffix = (
        "_with_legacy_total_tokens_fallback"
        if legacy_parent_without_attempt_count
        else ""
    )
    actual_legacy_suffix = (
        "_with_legacy_parent_usage_fallback"
        if legacy_parent_without_attempt_count
        else ""
    )
    return {
        "total_cost": total_cost,
        "total_tokens": total_tokens,
        "currency": currency or "USD",
        "is_estimate": is_estimate,
        "cross_provider": len(tokens_by_provider) > 1,
        "tokens_by_provider": tokens_by_provider,
        "cost_by_provider": cost_by_provider,
        "phase_breakdown": phase,
        "call_count": len(calls),
        "calibers": {
            "estimate": {
                "tokens": estimated_tokens,
                "source": f"llm_calls.estimated_tokens{estimate_legacy_suffix}",
            },
            "provider_actual": {
                "tokens": provider_actual_tokens,
                "source": (
                    "llm_call_attempts.total_tokens_with_provider_usage"
                    f"{actual_legacy_suffix}"
                ),
            },
            "budget_charged": {
                "tokens": budget_charged_tokens,
                "source": "llm_calls.budget_charged_tokens",
            },
        },
        "attempt_observability": {
            "attempt_row_count": attempt_row_count,
            "physical_attempt_count": physical_attempt_count,
            "pre_dispatch_attempt_count": pre_dispatch_attempt_count,
            "usage_estimate_count": usage_estimate_count,
            "exception_count": exception_count,
            "retry_attempt_count": retry_attempt_count,
            "transport_retry_attempt_count": transport_retry_attempt_count,
            "response_parse_retry_attempt_count": response_parse_retry_attempt_count,
            "degrade_attempt_count": degrade_attempt_count,
            "legacy_parent_without_attempt_count": legacy_parent_without_attempt_count,
            "legacy_unreconstructable_tokens": legacy_unreconstructable_tokens,
        },
    }


def _budget_view(state: SceneRunState | None) -> dict[str, Any]:
    if state is None or state.scene_token_budget is None:
        return {
            "budget": None,
            "used": int(getattr(state, "scene_tokens_used", 0) or 0) if state else 0,
            "remaining": None,
            "over_budget": False,
            "usage_ratio": None,
            "baseline": None,
            "multiplier_used": None,
            "run_policy": getattr(state, "run_policy", None) if state else None,
        }
    budget = int(state.scene_token_budget)
    used = int(state.scene_tokens_used or 0)
    from novel_system.services.scene_budget import is_scene_budget_disarmed

    if is_scene_budget_disarmed(state):
        # 解除武装的场景没有真实上限——用 budget=None 表达「不限」，避免把哨兵天文数字
        # 当成预算展示（前端 cost 视图对 budget===null 已优雅隐藏预算卡）。记账值照常透出。
        return {
            "budget": None,
            "used": used,
            "remaining": None,
            "over_budget": False,
            "usage_ratio": None,
            "baseline": None,
            "multiplier_used": None,
            "disarmed": True,
            "run_policy": state.run_policy,
        }
    baseline = max(1, budget // BUDGET_MULTIPLIER)
    return {
        "budget": budget,
        "used": used,
        "remaining": max(0, budget - used),
        "over_budget": used > budget,
        "usage_ratio": round(used / budget, 4) if budget else None,
        "baseline": baseline,
        "multiplier_used": round(used / baseline, 4),
        "run_policy": state.run_policy,
    }


def _extra_cost(session: Session, scene_id: str, calls: list[LlmCall], total_cost: float,
                state: SceneRunState | None) -> dict[str, Any]:
    """额外成本归因（§5.8：低分散补候选 / 失败重试 / 重复 QC）。启发式、口径已注释。"""
    ordered = sorted(calls, key=lambda c: (c.created_at or "", c.llm_call_id))
    attempts_by_call: dict[str, list[LlmCallAttempt]] = {}
    if ordered:
        attempts = session.execute(
            select(LlmCallAttempt).where(
                LlmCallAttempt.llm_call_id.in_([call.llm_call_id for call in ordered])
            )
        ).scalars().all()
        for attempt in attempts:
            attempts_by_call.setdefault(attempt.llm_call_id, []).append(attempt)
    # 新账本按失败物理尝试归因；legacy 无子账时回退父 error_code。
    failed_cost = 0.0
    for call in ordered:
        call_attempts = attempts_by_call.get(call.llm_call_id, [])
        if call_attempts:
            failed_cost += sum(
                _attempt_cost(call, attempt)["cost"]
                for attempt in call_attempts
                if attempt.request_dispatched_at is not None
                and (
                    attempt.error_code
                    or attempt.accounting_status
                    in {"failed", "usage_exceeds_reservation"}
                )
            )
        elif call.error_code:
            failed_cost += _call_cost(call)["cost"]
    # 重复 QC：QC 阶段第 2 次起
    qc_calls = [c for c in ordered if classify_phase(c.node_id, c.step) == PHASE_QC]
    repeat_qc_cost = sum(_call_cost(c)["cost"] for c in qc_calls[1:])
    # 低分散补候选：候选阶段超出 criticality 初始 N 的部分
    cand_calls = [c for c in ordered if classify_phase(c.node_id, c.step) == PHASE_CANDIDATE]
    initial = _INITIAL_CANDIDATES.get((state.criticality_level or "").lower(), 1) if state else 1
    topup_cost = sum(_call_cost(c)["cost"] for c in cand_calls[initial:])
    total = failed_cost + repeat_qc_cost + topup_cost
    return {
        "failed_call_cost": failed_cost,
        "repeat_qc_cost": repeat_qc_cost,
        "low_dispersion_topup_cost": topup_cost,
        "total": total,
        "retry_cost_ratio": round(total / total_cost, 4) if total_cost > 0 else 0.0,
    }


def scene_cost(session: Session, scene_id: str) -> dict[str, Any]:
    calls = list(
        session.execute(select(LlmCall).where(LlmCall.scene_id == scene_id)).scalars().all()
    )
    state = session.get(SceneRunState, scene_id)
    agg = _aggregate_calls(session, calls)
    budget = _budget_view(state)
    result = {
        "scene_id": scene_id,
        "total_cost": agg["total_cost"],
        "currency": agg["currency"],
        "is_estimate": agg["is_estimate"],
        "total_tokens": agg["total_tokens"],
        "cross_provider": agg["cross_provider"],
        "tokens_by_provider": agg["tokens_by_provider"],
        "cost_by_provider": agg["cost_by_provider"],
        "phase_breakdown": agg["phase_breakdown"],
        "call_count": agg["call_count"],
        "budget": budget,
        "calibers": agg["calibers"],
        "attempt_observability": agg["attempt_observability"],
        "extra_cost": _extra_cost(session, scene_id, calls, agg["total_cost"], state),
    }
    return result


def _archived_scene_ids(session: Session, *, chapter_id: str | None = None,
                        scene_ids: set[str] | None = None) -> set[str]:
    stmt = select(FinalScene.scene_id).where(FinalScene.status == "archived")
    if chapter_id is not None:
        stmt = stmt.where(FinalScene.chapter_id == chapter_id)
    rows = set(session.execute(stmt).scalars().all())
    if scene_ids is not None:
        rows &= scene_ids
    return rows


def chapter_cost(session: Session, chapter_id: str) -> dict[str, Any]:
    calls = list(
        session.execute(select(LlmCall).where(LlmCall.chapter_id == chapter_id)).scalars().all()
    )
    agg = _aggregate_calls(session, calls)
    archived = _archived_scene_ids(session, chapter_id=chapter_id)
    archived_count = len(archived)
    return {
        "chapter_id": chapter_id,
        "total_cost": agg["total_cost"],
        "currency": agg["currency"],
        "is_estimate": agg["is_estimate"],
        "total_tokens": agg["total_tokens"],
        "cross_provider": agg["cross_provider"],
        "tokens_by_provider": agg["tokens_by_provider"],
        "cost_by_provider": agg["cost_by_provider"],
        "phase_breakdown": agg["phase_breakdown"],
        "call_count": agg["call_count"],
        "calibers": agg["calibers"],
        "attempt_observability": agg["attempt_observability"],
        "archived_scene_count": archived_count,
        "tokens_per_archived_scene": (
            round(agg["total_tokens"] / archived_count) if archived_count else None
        ),
        "cost_per_archived_chapter": agg["total_cost"] if archived_count else None,
    }


def _project_calls(session: Session, project_id: str, scene_ids: set[str]) -> list[LlmCall]:
    """项目命中的全部调用：project_id 直接命中 或 scene_id ∈ 项目场景（LlmCall.project_id 可能为空）。"""
    calls = list(
        session.execute(select(LlmCall).where(LlmCall.project_id == project_id)).scalars().all()
    )
    seen = {c.llm_call_id for c in calls}
    if scene_ids:
        for call in session.execute(
            select(LlmCall).where(LlmCall.scene_id.in_(scene_ids))
        ).scalars().all():
            if call.llm_call_id not in seen:
                calls.append(call)
                seen.add(call.llm_call_id)
    return calls


def _project_scope(session: Session, project_id: str) -> tuple[set[str], set[str], list[LlmCall]]:
    scene_ids = set(
        session.execute(
            select(SceneCard.scene_id).where(SceneCard.project_id == project_id)
        ).scalars().all()
    )
    chapter_ids = set(
        session.execute(
            select(SceneCard.chapter_id).where(SceneCard.project_id == project_id)
        ).scalars().all()
    )
    calls = _project_calls(session, project_id, scene_ids)
    return scene_ids, chapter_ids, calls


def project_cost(session: Session, project_id: str) -> dict[str, Any]:
    scene_ids, chapter_ids, calls = _project_scope(session, project_id)
    return _project_summary(session, project_id, scene_ids, chapter_ids, calls)


def _project_summary(
    session: Session,
    project_id: str,
    scene_ids: set[str],
    chapter_ids: set[str],
    calls: list[LlmCall],
) -> dict[str, Any]:
    agg = _aggregate_calls(session, calls)
    archived = _archived_scene_ids(session, scene_ids=scene_ids) if scene_ids else set()
    archived_count = len(archived)
    archived_chapters = {
        cid for cid in chapter_ids
        if _archived_scene_ids(session, chapter_id=cid)
    }
    return {
        "project_id": project_id,
        "total_cost": agg["total_cost"],
        "currency": agg["currency"],
        "is_estimate": agg["is_estimate"],
        "total_tokens": agg["total_tokens"],
        "cross_provider": agg["cross_provider"],
        "tokens_by_provider": agg["tokens_by_provider"],
        "cost_by_provider": agg["cost_by_provider"],
        "phase_breakdown": agg["phase_breakdown"],
        "call_count": agg["call_count"],
        "calibers": agg["calibers"],
        "attempt_observability": agg["attempt_observability"],
        "chapter_count": len(chapter_ids),
        "scene_count": len(scene_ids),
        "archived_scene_count": archived_count,
        "archived_chapter_count": len(archived_chapters),
        "tokens_per_archived_scene": (
            round(agg["total_tokens"] / archived_count) if archived_count else None
        ),
        "cost_per_archived_chapter": (
            round(agg["total_cost"] / len(archived_chapters), 6) if archived_chapters else None
        ),
    }


# ---------------------------------------------------------------------------
# 项目成本看板（cost-dashboard）：summary 之上补趋势 / 构成 / 明细，一读拿全。
# ---------------------------------------------------------------------------

DASHBOARD_DEFAULT_DAYS = 30
DASHBOARD_MAX_DAYS = 365
DASHBOARD_NODE_LIMIT = 10
DASHBOARD_CALL_LIMIT = 10


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DASHBOARD_DEFAULT_DAYS
    return max(1, min(DASHBOARD_MAX_DAYS, value))


def _call_day(call: LlmCall) -> str:
    """created_at 是 UTC ISO 字符串，前 10 位即日桶；缺失归空串（不进趋势）。"""
    return (call.created_at or "")[:10]


def _trend(rows: list[tuple[LlmCall, dict[str, Any]]], days: int) -> dict[str, Any]:
    """近 ``days`` 天（含今天，UTC）逐日费用/token/调用数，稠密序列——缺日补零。"""
    today = datetime.now(UTC).date()
    window = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    by_day: dict[str, dict[str, Any]] = {
        day: {"date": day, "cost": 0.0, "tokens": 0, "call_count": 0} for day in window
    }
    window_start = window[0]
    for call, cost in rows:
        day = _call_day(call)
        bucket = by_day.get(day)
        if bucket is None:
            continue
        bucket["cost"] += cost["cost"]
        bucket["tokens"] += int(call.total_tokens or 0)
        bucket["call_count"] += 1
    series = [by_day[day] for day in window]
    return {
        "days": days,
        "window_start": window_start,
        "window_end": window[-1],
        "series": series,
        "window_cost": sum(item["cost"] for item in series),
        "window_tokens": sum(item["tokens"] for item in series),
        "window_call_count": sum(item["call_count"] for item in series),
    }


def _by_model(rows: list[tuple[LlmCall, dict[str, Any]]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for call, cost in rows:
        key = (call.provider or "unknown", call.model or "unknown")
        bucket = buckets.setdefault(
            key,
            {
                "provider": key[0],
                "model": key[1],
                "cost": 0.0,
                "tokens": 0,
                "call_count": 0,
                "is_estimate": False,
            },
        )
        bucket["cost"] += cost["cost"]
        bucket["tokens"] += int(call.total_tokens or 0)
        bucket["call_count"] += 1
        bucket["is_estimate"] = (
            bucket["is_estimate"] or bool(cost["is_estimate"]) or bool(call.usage_is_estimate)
        )
    return sorted(buckets.values(), key=lambda b: (-b["cost"], b["provider"], b["model"]))


def _by_node(
    rows: list[tuple[LlmCall, dict[str, Any]]], limit: int
) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    for call, cost in rows:
        node = call.node_id or call.step or "unknown"
        bucket = buckets.setdefault(
            node,
            {
                "node_id": node,
                "phase": classify_phase(call.node_id, call.step),
                "cost": 0.0,
                "tokens": 0,
                "call_count": 0,
            },
        )
        bucket["cost"] += cost["cost"]
        bucket["tokens"] += int(call.total_tokens or 0)
        bucket["call_count"] += 1
    ordered = sorted(buckets.values(), key=lambda b: (-b["cost"], b["node_id"]))
    top, rest = ordered[:limit], ordered[limit:]
    remainder = None
    if rest:
        remainder = {
            "node_count": len(rest),
            "cost": sum(b["cost"] for b in rest),
            "tokens": sum(b["tokens"] for b in rest),
            "call_count": sum(b["call_count"] for b in rest),
        }
    return {"top": top, "remainder": remainder}


def _by_chapter(rows: list[tuple[LlmCall, dict[str, Any]]]) -> list[dict[str, Any]]:
    buckets: dict[str | None, dict[str, Any]] = {}
    for call, cost in rows:
        key = call.chapter_id or None
        bucket = buckets.setdefault(
            key,
            {"chapter_id": key, "cost": 0.0, "tokens": 0, "call_count": 0, "scene_ids": set()},
        )
        bucket["cost"] += cost["cost"]
        bucket["tokens"] += int(call.total_tokens or 0)
        bucket["call_count"] += 1
        if call.scene_id:
            bucket["scene_ids"].add(call.scene_id)
    result = []
    for bucket in buckets.values():
        bucket["scene_count"] = len(bucket.pop("scene_ids"))
        result.append(bucket)
    # 未关联章节的调用（项目级节点等）排最后，其余按费用降序
    return sorted(result, key=lambda b: (b["chapter_id"] is None, -b["cost"], b["chapter_id"] or ""))


def _top_calls(rows: list[tuple[LlmCall, dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda r: (-r[1]["cost"], r[0].created_at or "", r[0].llm_call_id))
    return [
        {
            "llm_call_id": call.llm_call_id,
            "created_at": call.created_at,
            "node_id": call.node_id or call.step,
            "phase": classify_phase(call.node_id, call.step),
            "provider": call.provider,
            "model": call.model,
            "total_tokens": int(call.total_tokens or 0),
            "cost": cost["cost"],
            "currency": cost["currency"],
            "is_estimate": bool(cost["is_estimate"]) or bool(call.usage_is_estimate),
            "latency_ms": call.latency_ms,
            "error_code": call.error_code,
            "accounting_status": call.accounting_status,
            "scene_id": call.scene_id,
            "chapter_id": call.chapter_id,
        }
        for call, cost in ordered[:limit]
    ]


def project_cost_dashboard(
    session: Session,
    project_id: str,
    *,
    days: Any = DASHBOARD_DEFAULT_DAYS,
    node_limit: int = DASHBOARD_NODE_LIMIT,
    call_limit: int = DASHBOARD_CALL_LIMIT,
) -> dict[str, Any]:
    """成本看板一读聚合：summary + 趋势 + 模型/节点/章节构成 + Top 调用。

    只读、复用 ``project_cost`` 的调用命中口径；每条调用只折算一次价格。
    趋势按 UTC 日桶且稠密补零，前端可直接画图；空项目返回空构成不 500。
    """
    days = _clamp_days(days)
    scene_ids, chapter_ids, calls = _project_scope(session, project_id)
    rows = [(call, _call_cost(call)) for call in calls]
    return {
        "project_id": project_id,
        "summary": _project_summary(session, project_id, scene_ids, chapter_ids, calls),
        "trend": _trend(rows, days),
        "by_model": _by_model(rows),
        "by_node": _by_node(rows, max(1, int(node_limit))),
        "by_chapter": _by_chapter(rows),
        "top_calls": _top_calls(rows, max(1, int(call_limit))),
    }
