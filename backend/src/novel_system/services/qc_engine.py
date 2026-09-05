from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.contracts.qc import SoftQCOutput
from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    LlmCallAttempt,
    QcReport,
    SceneBundle,
    SceneCard,
    SceneRunState,
)
from novel_system.services.human_review_manager import HumanReviewManager
from novel_system.services.llm_task_runner import (
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.character_continuity import (
    detect_character_pronoun_drift,
    detect_mechanical_required_beat_listing,
)
from novel_system.services.quality_classifier import (
    classify_issue,
    classify_issues,
    has_blocking,
)
from novel_system.services.qc_constraints import (
    contains_forbidden_term,
    issue_mentions_source,
    named_scene_card_sources,
    source_field_satisfied,
)
from novel_system.services.qc_validator import QCValidationError, validate_qc_report
from novel_system.services.scene_ownership import require_scene_project_id


_LOGGER = logging.getLogger(__name__)

CONTINUITY_BUDGET_ISSUE_KEY = "continuity_budget_exceeded"
CONTINUITY_BUDGET_MESSAGE = "Prompt still exceeds the safe input budget after deterministic continuity compaction."
CONTINUITY_BUDGET_REWRITE = (
    "Split the scene and retry QC with a smaller continuity scope."
)
HARD_QC_REQUIRED_ISSUE_KEYS = {"missing_required_text", "missing_hard_constraint"}
HARD_QC_STYLE_ONLY_ISSUE_KEYS = {
    "style_compliance",
    "style_rule_violation",
}
HARD_QC_NON_BLOCKING_LLM_ISSUE_KEYS = {"character_role_inconsistency"}
UNSUBSTANTIATED_PRONOUN_CONTINUITY_KEYS = {
    "character_pronoun_ambiguity",
    "character_pronoun_continuity",
}

_QC_CONTROL_PLANE_ERROR_CODES = {
    "CONTINUITY_BUDGET_EXCEEDED",
    "LLM_USAGE_EXCEEDS_RESERVATION",
}


def _is_proven_dispatched_provider_failure(
    session: Session,
    *,
    error: LLMNodeExecutionError,
    scene: SceneCard,
    state: SceneRunState,
    expected_step: str,
    execution_step_key: str,
) -> bool:
    """Allow QC degradation only for an exact, durable provider-failure ledger."""
    error_code = str(error.error_code or "")
    if (
        not error_code
        or error_code.startswith("RUN_")
        or error_code.startswith("LLM_ACCOUNTING_")
        or error_code.startswith("LLM_SCENE_")
        or error_code.startswith("LLM_PROVIDER_ATTEMPT_")
        or error_code in _QC_CONTROL_PLANE_ERROR_CODES
    ):
        return False
    current_execution_id = str(state.active_execution_id or "").strip()
    if not current_execution_id:
        return False
    parent = session.get(LlmCall, error.llm_call_id)
    if parent is None:
        return False
    if (
        parent.scope_type != "scene"
        or parent.scope_id != scene.scene_id
        or parent.scene_id != scene.scene_id
        or parent.chapter_id != scene.chapter_id
        or parent.execution_id != current_execution_id
        or parent.execution_step_key != execution_step_key
        or parent.step != expected_step
        or parent.node_id != expected_step
        or parent.accounting_status != "failed"
        or parent.error_code != error_code
        or parent.request_dispatched_at is None
        or parent.settled_at is None
    ):
        return False
    attempts = list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.llm_call_id == parent.llm_call_id)
            .order_by(LlmCallAttempt.provider_attempt_no)
        )
    )
    if not attempts or [row.provider_attempt_no for row in attempts] != list(
        range(len(attempts))
    ):
        return False

    aggregate_fields = (
        "estimated_tokens",
        "reserved_tokens",
        "budget_charged_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
    )
    for attempt in attempts:
        numeric_values = (
            attempt.provider_attempt_no,
            attempt.request_max_output_tokens,
            *(getattr(attempt, field) for field in aggregate_fields),
        )
        if any(not isinstance(value, int) or value < 0 for value in numeric_values):
            return False
        if (
            attempt.accounting_status != "failed"
            or attempt.request_dispatched_at is None
            or attempt.settled_at is None
            or attempt.estimated_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens > attempt.reserved_tokens
            or attempt.budget_charged_tokens
            != min(attempt.total_tokens, attempt.reserved_tokens)
            or attempt.total_tokens != attempt.prompt_tokens + attempt.completion_tokens
            or not str(attempt.error_code or "").strip()
        ):
            return False

    for field in aggregate_fields:
        parent_value = getattr(parent, field)
        if (
            not isinstance(parent_value, int)
            or parent_value < 0
            or parent_value != sum(getattr(attempt, field) for attempt in attempts)
        ):
            return False
    if parent.usage_is_estimate != any(
        attempt.usage_is_estimate for attempt in attempts
    ):
        return False
    if (
        parent.estimated_tokens > parent.reserved_tokens
        or parent.budget_charged_tokens
        != min(parent.total_tokens, parent.reserved_tokens)
    ):
        return False

    final_attempt = attempts[-1]
    return bool(
        final_attempt.error_code == error_code
        and any(attempt.error_code == error_code for attempt in attempts)
    )


def _is_proven_undispatched_continuity_rejection(
    session: Session,
    *,
    error: LLMNodeContinuityError,
    scene: SceneCard,
    state: SceneRunState,
    expected_step: str,
    execution_step_key: str,
) -> bool:
    """Allow continuity degradation only from the exact pre-dispatch rejection ledger."""
    current_execution_id = str(state.active_execution_id or "").strip()
    if not current_execution_id or error.error_code != "CONTINUITY_BUDGET_EXCEEDED":
        return False
    parent = session.get(LlmCall, error.llm_call_id)
    if parent is None:
        return False
    if (
        parent.scope_type != "scene"
        or parent.scope_id != scene.scene_id
        or parent.scene_id != scene.scene_id
        or parent.chapter_id != scene.chapter_id
        or parent.execution_id != current_execution_id
        or parent.execution_step_key != execution_step_key
        or parent.step != expected_step
        or parent.node_id != expected_step
        or parent.accounting_status != "rejected"
        or parent.error_code != "CONTINUITY_BUDGET_EXCEEDED"
        or parent.request_dispatched_at is not None
        or parent.settled_at is None
        or parent.usage_is_estimate is not True
        or any(
            getattr(parent, field) != 0
            for field in (
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "latency_ms",
            )
        )
    ):
        return False
    return (
        session.scalar(
            select(LlmCallAttempt.attempt_id)
            .where(
                LlmCallAttempt.llm_call_id == parent.llm_call_id,
            )
            .limit(1)
        )
        is None
    )


def _build_qc_report_id(
    scene_id: str,
    *,
    timestamp: str | None = None,
    random_hex: str | None = None,
) -> str:
    stamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = random_hex or uuid.uuid4().hex[:12]
    return f"qc_report_{scene_id}_{stamp}_{suffix}"


@dataclass(slots=True)
class QcDecision:
    branch: str
    qc_report_id: str
    human_review_event_id: str | None
    resolution_code: str
    next_action: str
    should_continue: bool
    stop_reason: str | None = None
    llm_call_id: str | None = None
    execution_step_key: str | None = None


# 兼容别名：调用方（orchestrator/checkpoint 测试）按 hard/soft 名 import，字段契约同一。
HardQcDecision = QcDecision
SoftQcDecision = QcDecision


def _continuity_warning_message(continuity_warning: Any) -> str:
    if isinstance(continuity_warning, dict):
        message = continuity_warning.get("message")
        if isinstance(message, str) and message:
            return message
    return CONTINUITY_BUDGET_MESSAGE


def _continuity_warning_issue_key(continuity_warning: Any) -> str:
    if isinstance(continuity_warning, dict):
        code = continuity_warning.get("code")
        if isinstance(code, str) and code:
            return code
    return CONTINUITY_BUDGET_ISSUE_KEY


def _issue_blob(issues: list[Any], rewrite_brief: list[Any]) -> str:
    parts: list[str] = []
    for issue in issues:
        if isinstance(issue, dict):
            parts.append(str(issue.get("issue_key") or ""))
            parts.append(str(issue.get("message") or ""))
    parts.extend(str(item) for item in rewrite_brief)
    return "\n".join(parts)


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _scene_card_source_texts(scene: SceneCard) -> list[str]:
    texts = [
        scene.must_include_text,
        scene.hook,
        scene.exit_change,
        scene.scene_goal,
        scene.location,
    ]
    beats = scene.beats_json if isinstance(scene.beats_json, list) else []
    texts.extend(item for item in beats if isinstance(item, str))
    return [text for text in texts if isinstance(text, str) and text.strip()]


def _named_scene_card_source_texts(scene: SceneCard) -> list[tuple[str, str]]:
    # 字段顺序即冲突归因优先级（与 preflight 侧不同：QC 侧含 location 且 hook 优先）。
    return named_scene_card_sources(
        scene, ("hook", "must_include_text", "exit_change", "scene_goal", "location")
    )


def _terms_from_qc_text(text: str) -> list[str]:
    terms: list[str] = []
    terms.extend(
        match.strip()
        for match in re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,40})[\"'“”‘’]", text)
    )
    terms.extend(match.strip() for match in re.findall(r"[\u4e00-\u9fff]{2,12}", text))
    terms.extend(
        match.strip() for match in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,40}", text)
    )
    seen: set[str] = set()
    unique: list[str] = []
    for term in terms:
        normalized = term.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        unique.append(normalized)
    return unique


QC_TERM_CHANGE_MARKERS = (
    "replace",
    "remove",
    "delete",
    "avoid",
    "forbid",
    "forbidden",
    "rename",
    "change",
    "cut",
    "neutral clue",
    "neutralize",
    "substitute",
    "替换",
    "删除",
    "去掉",
    "拿掉",
    "避免",
    "不要",
    "不得",
    "禁用",
    "改成",
    "改掉",
    "改写",
    "换成",
)


def _qc_text_requests_term_change(text: str, term: str) -> bool:
    if not text or not term:
        return False
    lowered = text.lower()
    normalized_term = term.lower()
    start = 0
    while True:
        index = lowered.find(normalized_term, start)
        if index < 0:
            return False
        window = lowered[max(0, index - 48) : index + len(normalized_term) + 48]
        if any(marker in window for marker in QC_TERM_CHANGE_MARKERS):
            return True
        start = index + len(normalized_term)


def _constraint_conflicts_for_text(scene: SceneCard, text: str) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for term in _terms_from_qc_text(text):
        if not _qc_text_requests_term_change(text, term):
            continue
        for source_name, source_text in _named_scene_card_source_texts(scene):
            if term in source_text:
                conflicts.append(
                    {
                        "term": term,
                        "constraint_source": source_name,
                        "conflicts_with": "hard_qc",
                        "human_readable_reason": "QC requests changing a term that the scene card requires.",
                    }
                )
                break
    return conflicts


def _evidence_spans_for_text(content: str, text: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for term in _terms_from_qc_text(text):
        start = content.find(term)
        if start < 0:
            continue
        spans.append({"text": term, "start": start, "end": start + len(term)})
        if len(spans) >= 5:
            break
    return spans


def _annotate_qc_issues(
    scene: SceneCard, source_content: str, payload: dict[str, Any]
) -> dict[str, Any]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload
    annotated: list[Any] = []
    for issue in issues:
        if not isinstance(issue, dict):
            annotated.append(issue)
            continue
        blob = " ".join(str(issue.get(key) or "") for key in ("issue_key", "message"))
        conflicts = _constraint_conflicts_for_text(scene, blob)
        evidence_spans = _evidence_spans_for_text(source_content, blob)
        severity = (
            "high"
            if conflicts
            else ("medium" if not payload.get("pass_flag") else "low")
        )
        annotated.append(
            {
                **issue,
                "evidence_spans": issue.get("evidence_spans") or evidence_spans,
                "constraint_source": (
                    conflicts[0]["constraint_source"]
                    if conflicts
                    else issue.get("constraint_source", "source_draft")
                ),
                "conflicts_with": issue.get("conflicts_with") or conflicts,
                "severity": issue.get("severity") or severity,
                "human_readable_reason": issue.get("human_readable_reason")
                or issue.get("message")
                or issue.get("issue_key")
                or "QC issue",
            }
        )
    return {**payload, "issues": annotated}


def _promote_constraint_conflicts_to_human_review(
    payload: dict[str, Any]
) -> dict[str, Any]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload
    has_conflict = any(
        isinstance(issue, dict) and bool(issue.get("conflicts_with"))
        for issue in issues
    )
    if not has_conflict or payload.get("next_action") == "human_review_required":
        return payload
    rewrite_brief = (
        payload.get("rewrite_brief")
        if isinstance(payload.get("rewrite_brief"), list)
        else []
    )
    return {
        **payload,
        "resolution_code": "hard_block_human",
        "pass_flag": False,
        "next_action": "human_review_required",
        "rewrite_brief": [
            *rewrite_brief,
            "Constraint conflict detected: choose whether to keep the scene-card term or revise the conflicting QC instruction.",
        ],
    }


def _reported_duplicate_appears_once(issue_blob: str, content: str) -> bool:
    quoted = re.findall(r"['‘“\"]([^'’”\"]{3,})['’”\"]", issue_blob)
    return bool(quoted) and all(content.count(fragment) <= 1 for fragment in quoted)


def _deterministic_quality_issues(
    scene: SceneCard, bundle: dict[str, Any], content: str
) -> list[dict[str, Any]]:
    inline_digests = bundle.get("snapshot", {}).get("inline_digests", {})
    character_contract = (
        inline_digests.get("character_contract")
        if isinstance(inline_digests, dict)
        else None
    )
    issues = detect_character_pronoun_drift(content, character_contract)
    listing_issue = detect_mechanical_required_beat_listing(
        content=content,
        must_include_text=scene.must_include_text,
    )
    if listing_issue is not None:
        issues.append(listing_issue)
    issues.extend(_event_log_consistency_issues(scene, content))
    # Wave 2（§5.4 提案—复核）：确定性检测器的产出打上来源标——检测器即复核器，
    # 分类器据此允许 Q0/Q1 升级；LLM 提案没有这个标，只能走内联复核或降 Q2。
    for issue in issues:
        if isinstance(issue, dict):
            issue.setdefault("source", "deterministic")
    return issues


def _event_log_consistency_issues(
    scene: SceneCard, content: str
) -> list[dict[str, Any]]:
    """Blueprint §15/§13 Step 6: check hard facts from event log against generated text."""
    try:
        from novel_system.services.narrative_event_log import NarrativeEventLog
        from novel_system.db.session import SessionLocal

        session = SessionLocal()
        try:
            log = NarrativeEventLog(session)
            project_id = require_scene_project_id(session, scene)
            issues: list[dict[str, Any]] = []

            # --- Event log fact consistency (§15: hard facts only) ---
            report = log.check_consistency(
                content,
                project_id,
                scene.scene_id,
                character_ids=scene.onstage_chars_json or [],
            )
            if report.violations:
                # §15: keyword violations block (high); advisory LLM flags inform (medium).
                issues.extend(
                    {
                        "issue_key": (
                            "event_log_consistency_violation"
                            if getattr(v, "source", "keyword") == "keyword"
                            else "event_log_consistency_llm_flag"
                        ),
                        "severity": (
                            "high"
                            if getattr(v, "source", "keyword") == "keyword"
                            else "medium"
                        ),
                        "message": (
                            f"Event log contradiction: {v.entity_id}.{v.fact_key} "
                            f"expected '{v.expected}' but text suggests '{v.actual}'"
                            + (
                                ""
                                if getattr(v, "source", "keyword") == "keyword"
                                else " (LLM advisory — human spot-check)"
                            )
                        ),
                        "details": {
                            "entity_id": v.entity_id,
                            "fact_key": v.fact_key,
                            "expected": v.expected,
                            "actual": v.actual,
                            "evidence": v.evidence,
                            "source": getattr(v, "source", "keyword"),
                        },
                    }
                    for v in report.violations
                )

            return issues
        finally:
            session.close()
    except (
        Exception
    ) as exc:  # noqa: BLE001 - surface availability without leaking details
        return [
            {
                "issue_key": "continuity_validation_unavailable",
                "severity": "medium",
                "message": "Narrative continuity validation was unavailable; review this scene manually.",
                "source": "system_diagnostic",
                "details": {"error_type": type(exc).__name__, "retryable": True},
            }
        ]


def _dedupe_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        issue_key = str(issue.get("issue_key") or "")
        message = str(issue.get("message") or "")
        key = (issue_key, message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)
    return deduped


def _drop_unsubstantiated_pronoun_continuity_issue(
    *,
    payload: dict[str, Any],
    deterministic_issues: list[dict[str, Any]],
    qc_type: str,
) -> dict[str, Any]:
    if any(
        issue.get("issue_key") == "character_pronoun_drift"
        for issue in deterministic_issues
    ):
        return payload

    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload

    kept_issues: list[Any] = []
    removed = False
    for issue in issues:
        if (
            isinstance(issue, dict)
            and str(issue.get("issue_key") or "").strip()
            in UNSUBSTANTIATED_PRONOUN_CONTINUITY_KEYS
        ):
            removed = True
            continue
        kept_issues.append(issue)
    if not removed:
        return payload

    cleaned = {**payload, "issues": kept_issues}
    if kept_issues or payload.get("next_action") == "pass":
        return cleaned

    if qc_type == "hard_qc":
        return {
            **cleaned,
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "rewrite_brief": [],
        }
    if qc_type == "soft_qc":
        return {
            **cleaned,
            "resolution_code": "soft_pass",
            "pass_flag": True,
            "next_action": "pass",
            "rewrite_brief": [],
            "carry_forward_note": False,
            "note_scope": None,
            "carry_note_text": None,
        }
    return cleaned


def _rewrite_briefs_for_deterministic_issues(issues: list[dict[str, Any]]) -> list[str]:
    briefs: list[str] = []
    for issue in issues:
        issue_key = issue.get("issue_key")
        if issue_key == "character_pronoun_drift":
            display_name = issue.get("display_name") or "角色"
            expected = issue.get("expected_pronoun") or "既定代词"
            briefs.append(
                f"修正{display_name}的代词连续性，保持使用{expected}；若指代不清，请重复角色姓名。"
            )
        elif issue_key == "mechanical_required_beat_listing":
            briefs.append(
                "将必须出现的剧情节拍自然织入动作和因果，不要在段尾追加清单。"
            )
    return briefs


def _append_unique_rewrite_briefs(
    existing: list[Any], additions: list[str]
) -> list[Any]:
    merged = list(existing)
    seen = {
        str(item).strip() for item in merged if isinstance(item, str) and item.strip()
    }
    for addition in additions:
        if addition.strip() and addition.strip() not in seen:
            merged.append(addition.strip())
            seen.add(addition.strip())
    return merged


# ---- Hard/Soft QC 共享实现（两引擎逐字相同的私有方法统一收敛到这里） ----


def _qc_build_user_prompt(base_prompt: str, draft_content: str) -> str:
    return f"{base_prompt}\n\n## Draft Under Review\n{draft_content}".strip()


def _qc_primary_issue_key(issues: list[dict[str, Any]]) -> str | None:
    for issue in issues:
        issue_key = issue.get("issue_key")
        if isinstance(issue_key, str) and issue_key:
            return issue_key
    return None


def _qc_apply_issue_tracking(
    state: SceneRunState, issues: list[dict[str, Any]]
) -> None:
    issue_key = _qc_primary_issue_key(issues)
    if issue_key is None:
        state.repeat_issue_key = None
        state.repeat_issue_count = 0
        return
    if state.repeat_issue_key == issue_key:
        state.repeat_issue_count += 1
    else:
        state.repeat_issue_key = issue_key
        state.repeat_issue_count = 1


def _qc_clear_downstream_outputs(state: SceneRunState) -> None:
    state.current_style_draft_row_id = None
    state.current_final_scene_row_id = None


def _qc_apply_deterministic_quality_gates(
    scene: SceneCard,
    bundle: dict[str, Any],
    draft_content: str,
    payload: dict[str, Any],
    *,
    qc_type: str,
) -> dict[str, Any]:
    deterministic_issues = _deterministic_quality_issues(scene, bundle, draft_content)
    payload = _drop_unsubstantiated_pronoun_continuity_issue(
        payload=payload,
        deterministic_issues=deterministic_issues,
        qc_type=qc_type,
    )
    if not deterministic_issues:
        return payload
    # Wave 2：gate 只做合并——是否改判分支/触发补丁由分级器统一裁决（硬 QC 侧
    # 只有 verified Q0/Q1 才升级为 partial_rewrite；theme/tension 等 Q2/Q3 不再
    # 强制重写；软 QC 侧同样交由分级器,gate 不做任何升级判断）。
    existing_issues = (
        payload.get("issues") if isinstance(payload.get("issues"), list) else []
    )
    rewrite_brief = (
        payload.get("rewrite_brief")
        if isinstance(payload.get("rewrite_brief"), list)
        else []
    )
    merged_issues = _dedupe_issues([*existing_issues, *deterministic_issues])
    return {
        **payload,
        "issues": merged_issues,
        "rewrite_brief": _append_unique_rewrite_briefs(
            rewrite_brief,
            _rewrite_briefs_for_deterministic_issues(deterministic_issues),
        ),
    }


def _qc_run_node_with_degradation(
    session: Session,
    *,
    prompt_builder: PromptBuilder,
    llm_runner: LLMNodeRunner,
    scene: SceneCard,
    state: SceneRunState,
    scene_id: str,
    bundle: dict[str, Any],
    source_draft_row_id: str,
    source_draft_content: str,
    execution_step_key: str,
    step: str,
    message_prefix: str,
    degraded_payload_factory: Callable[..., dict[str, Any]],
) -> tuple[str | None, str | None, dict[str, Any]]:
    """QC 节点执行 + 三级受控降级（continuity 预拒 / 已派发失败 / payload 非法）。

    降级只在对应账本证据成立时发生，否则原异常照抛；降级形状由各引擎的
    payload 工厂决定（hard=pass、soft=waive）。返回 (llm_call_id,
    degraded_reason, payload)。
    """
    llm_call_id: str | None = None
    degraded_reason: str | None = None
    try:
        prompt = prompt_builder.build(bundle["snapshot"], step)
        final_user_prompt = _qc_build_user_prompt(
            prompt["user_prompt"], source_draft_content
        )
        node_result = llm_runner.run(
            scene_id=scene_id,
            chapter_id=scene.chapter_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            node_id=step,
            step=step,
            prompt=prompt,
            user_prompt=final_user_prompt,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_draft_content,
            execution_step_key=execution_step_key,
        )
        llm_call_id = node_result.llm_call_id
        payload = node_result.response.structured_output or {}
    except LLMNodeContinuityError as exc:
        if not _is_proven_undispatched_continuity_rejection(
            session,
            error=exc,
            scene=scene,
            state=state,
            expected_step=step,
            execution_step_key=execution_step_key,
        ):
            raise
        llm_call_id = exc.llm_call_id
        degraded_reason = f"{step}_continuity_budget_exceeded"
        payload = degraded_payload_factory(
            issue_key=_continuity_warning_issue_key(exc.continuity_warning),
            message=_continuity_warning_message(exc.continuity_warning),
            continuity_warning=exc.continuity_warning,
        )
    except LLMNodeExecutionError as exc:
        if not _is_proven_dispatched_provider_failure(
            session,
            error=exc,
            scene=scene,
            state=state,
            expected_step=step,
            execution_step_key=execution_step_key,
        ):
            raise
        llm_call_id = exc.llm_call_id
        degraded_reason = f"{step}_execution_failed"
        payload = degraded_payload_factory(
            issue_key=f"{step}_execution_failed",
            message=f"{message_prefix} execution failed: {exc.message}",
        )
    if degraded_reason is None:
        try:
            # 只对真实 LLM payload 做 normalize（dump 会把 issue 重建为
            # issue_key+message）；此后管线内部字段（source/quality_level 等）
            # 不得再经 validate→dump 往返，否则分级契约被剥掉。
            report = validate_qc_report(step, payload)
            payload = report.model_dump()
        except (QCValidationError, ValidationError) as exc:
            degraded_reason = f"invalid_{step}_payload"
            payload = degraded_payload_factory(
                issue_key=f"invalid_{step}_payload",
                message=f"{message_prefix} payload validation failed: {exc}",
            )
    return llm_call_id, degraded_reason, payload


def _qc_record_attempt(
    session: Session,
    *,
    step: str,
    scene_id: str,
    chapter_id: str,
    source_bundle_id: str,
    branch: str,
    qc_report_id: str,
    resolution_code: str,
    next_action: str,
    human_review_event_id: str | None,
    execution_step_key: str,
    llm_call_id: str | None = None,
    error_code: str | None = None,
    retryable: bool | None = None,
    continuity_warning: dict[str, Any] | None = None,
    details_extra: dict[str, Any] | None = None,
) -> None:
    """details_json 键名是 checkpoint 契约：公共键在此固定，引擎差异键
    （soft 侧 source_draft_row_id/rewrite_brief）经 details_extra 注入。"""
    details_json: dict[str, Any] = {
        "qc_report_id": qc_report_id,
        "resolution_code": resolution_code,
        "next_action": next_action,
        "human_review_event_id": human_review_event_id,
        "execution_step_key": execution_step_key,
        **(details_extra or {}),
    }
    if llm_call_id is not None:
        details_json["llm_call_id"] = llm_call_id
    if error_code is not None:
        details_json["error_code"] = error_code
    if retryable is not None:
        details_json["retryable"] = retryable
    if continuity_warning is not None:
        details_json["continuity_warning"] = continuity_warning
    session.add(
        AttemptTracker(
            scene_id=scene_id,
            chapter_id=chapter_id,
            step=step,
            status=branch,
            source_bundle_id=source_bundle_id,
            details_json=details_json,
        )
    )


class HardQcEngine:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
        human_review_manager: HumanReviewManager | None = None,
    ) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)
        self.human_review_manager = human_review_manager or HumanReviewManager(session)

    def evaluate(
        self,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        neutral_content: str,
        execution_step_key: str = "hard_qc:0",
    ) -> HardQcDecision:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # Wave 2（§5.4/§7.7）：QC 自身执行失败不再撤销正文交付——降级为 pass +
        # Q2 警告 issue 继续管线；确定性 gates 照跑，verified Q0/Q1 仍能阻断。
        llm_call_id, degraded_reason, payload = _qc_run_node_with_degradation(
            self.session,
            prompt_builder=self.prompt_builder,
            llm_runner=self._llm_runner,
            scene=scene,
            state=state,
            scene_id=scene_id,
            bundle=bundle,
            source_draft_row_id=neutral_draft_row_id,
            source_draft_content=neutral_content,
            execution_step_key=execution_step_key,
            step="hard_qc",
            message_prefix="QC",
            degraded_payload_factory=self._degraded_pass_payload,
        )

        payload = self._apply_deterministic_sanity(scene, neutral_content, payload)
        payload = _qc_apply_deterministic_quality_gates(
            scene, bundle, neutral_content, payload, qc_type="hard_qc"
        )
        payload = _annotate_qc_issues(scene, neutral_content, payload)
        payload = _promote_constraint_conflicts_to_human_review(payload)
        payload = self._apply_quality_grading(scene, neutral_content, payload)
        validate_qc_report("hard_qc", payload)  # 组合合法性校验（不回写 dump）
        qc_report = self._persist_qc_report(
            scene=scene,
            state=state,
            bundle=bundle,
            neutral_draft_row_id=neutral_draft_row_id,
            neutral_content=neutral_content,
            payload=payload,
        )
        branch = self._branch_for(payload["next_action"])

        # PR-8 §6.6 — style_reference validation gate(qc pass 时二次裁决)
        # Wave 2（§5.4）：只有确定性 n-gram 抄袭命中（Q0）保留阻断权；
        # fail/partial 是量化容差/语义评审（Q3 风格层），降为诊断警告不断头。
        if branch == "continue":
            style_verdict = self._apply_style_validation_gate(scene, neutral_content)
            if style_verdict == "plagiarism":
                plagiarism_issue = classify_issue(
                    {
                        "issue_key": "style_plagiarism",
                        "message": "style_reference plagiarism check hit (deterministic n-gram overlap)",
                        "source": "deterministic",
                    },
                    scene=scene,
                    content=neutral_content,
                )
                qc_report.resolution_code = "style_validation_plagiarism"
                qc_report.next_action = "human_review_required"
                qc_report.issues_json = [
                    *(qc_report.issues_json or []),
                    plagiarism_issue,
                ]
                self.session.flush()
                _qc_apply_issue_tracking(state, qc_report.issues_json)
                self._apply_branch_counters(state, "human_review_required")
                _qc_clear_downstream_outputs(state)
                return self._escalate_existing_report(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    neutral_draft_row_id=neutral_draft_row_id,
                    qc_report=qc_report,
                    branch="human_review_required",
                    failure_reason="style_reference validation found deterministic plagiarism evidence; human review is required.",
                    trigger_reason="style_validation_plagiarism",
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                )
            if style_verdict in ("fail", "partial"):
                style_issue = classify_issue(
                    {
                        "issue_key": f"style_validation_{style_verdict}",
                        "message": f"style_reference validation verdict: {style_verdict}（风格层诊断，不阻断交付）",
                        "source": "deterministic",
                    },
                    scene=scene,
                    content=neutral_content,
                )
                qc_report.issues_json = [*(qc_report.issues_json or []), style_issue]
                self.session.flush()

        _qc_apply_issue_tracking(state, payload["issues"])
        self._apply_branch_counters(state, branch)

        circuit_breaker_reason = self._circuit_breaker_reason(state, branch)
        if circuit_breaker_reason is not None:
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason=self._failure_reason_for_circuit_breaker(
                    circuit_breaker_reason, branch
                ),
                trigger_reason=circuit_breaker_reason,
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
            )

        if branch == "human_review_required":
            _qc_clear_downstream_outputs(state)
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                neutral_draft_row_id=neutral_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason="hard_qc explicitly requested human review before style generation.",
                trigger_reason="hard_qc_requested_human_review",
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
            )

        if branch == "rewrite_partial":
            _qc_clear_downstream_outputs(state)
            state.scene_status = "hard_qc_partial_rewrite_required"
        elif branch == "rewrite_full":
            _qc_clear_downstream_outputs(state)
            state.scene_status = "hard_qc_full_rewrite_required"

        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=None,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )
        self.session.flush()
        return HardQcDecision(
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=None,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=branch == "continue",
            stop_reason=degraded_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )

    @staticmethod
    def _branch_for(next_action: str) -> str:
        return {
            "pass": "continue",
            "partial_rewrite": "rewrite_partial",
            "full_rewrite": "rewrite_full",
            "human_review_required": "human_review_required",
        }[next_action]

    @staticmethod
    def _degraded_pass_payload(
        *,
        issue_key: str,
        message: str,
        continuity_warning: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """QC 执行失败的降级形状：pass + Q2 警告 issue（§5.4——不撤销已有正文）。"""
        issue: dict[str, Any] = {"issue_key": issue_key, "message": message}
        if continuity_warning is not None:
            issue["continuity_warning"] = continuity_warning
        return {
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [issue],
            "rewrite_brief": [],
        }

    def _apply_quality_grading(
        self, scene: SceneCard, neutral_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2（§5.4）：统一分级 + 阻断裁决单一来源。

        全部 issue 过分类器（LLM 提案无确定性复核自动降 Q2）。存在 verified
        Q0/Q1 → 保留阻断分支（LLM 说 pass 也升级为 partial_rewrite）；否则任何
        非 pass 意见降级为 pass，原意见以 Q2/Q3 警告随报告交付（G-03：软性
        意见不再让作者无稿可用）。
        """
        classified = classify_issues(
            payload.get("issues") or [], scene=scene, content=neutral_content
        )
        graded = {**payload, "issues": classified}
        if has_blocking(classified):
            if graded.get("next_action") == "pass":
                rewrite_brief = (
                    graded.get("rewrite_brief")
                    if isinstance(graded.get("rewrite_brief"), list)
                    else []
                )
                return {
                    **graded,
                    "resolution_code": "hard_fail_partial",
                    "pass_flag": False,
                    "next_action": "partial_rewrite",
                    "rewrite_brief": rewrite_brief
                    or ["Resolve the verified hard-fact issue before continuing."],
                }
            return graded
        if graded.get("next_action") != "pass":
            return {
                **graded,
                "resolution_code": "hard_pass",
                "pass_flag": True,
                "next_action": "pass",
            }
        return graded

    @staticmethod
    def _serialize_rewrite_brief(
        rewrite_brief: list[str],
        *,
        scene: SceneCard | None = None,
        source_content: str = "",
        issues: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        issue_blob = _issue_blob(issues or [], rewrite_brief)
        entries: list[dict[str, Any]] = []
        for item in rewrite_brief:
            entry: dict[str, Any] = {"instruction": item}
            if scene is not None:
                blob = f"{item}\n{issue_blob}"
                evidence_spans = _evidence_spans_for_text(source_content, blob)
                conflicts = _constraint_conflicts_for_text(scene, blob)
                if evidence_spans or conflicts:
                    entry["constraint_source"] = "hard_qc"
                    entry["severity"] = "high" if conflicts else "medium"
                    entry["evidence_spans"] = evidence_spans
                    entry["conflicts_with"] = conflicts
            entries.append(entry)
        return entries

    def _apply_deterministic_sanity(
        self, scene: SceneCard, neutral_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        issues = payload.get("issues")
        if not isinstance(issues, list) or not issues:
            return payload
        rewrite_brief = payload.get("rewrite_brief")
        issue_blob = _issue_blob(
            issues, rewrite_brief if isinstance(rewrite_brief, list) else []
        )
        filtered = [
            issue
            for issue in issues
            if not (
                isinstance(issue, dict)
                and self._issue_contradicts_deterministic_scene_card(
                    scene, neutral_content, issue, issue_blob
                )
            )
        ]
        if len(filtered) == len(issues):
            return payload
        if filtered:
            return {**payload, "issues": filtered}
        return {
            **payload,
            "resolution_code": "hard_pass",
            "pass_flag": True,
            "next_action": "pass",
            "issues": [],
            "rewrite_brief": [],
        }

    def _issue_contradicts_deterministic_scene_card(
        self,
        scene: SceneCard,
        neutral_content: str,
        issue: dict[str, Any],
        issue_blob: str,
    ) -> bool:
        issue_key = str(issue.get("issue_key") or "").strip()
        if issue_key == "forbidden_text":
            return not contains_forbidden_term(scene.forbidden_text, neutral_content)
        if (
            issue_key in HARD_QC_STYLE_ONLY_ISSUE_KEYS
            or issue_key in HARD_QC_NON_BLOCKING_LLM_ISSUE_KEYS
            or issue_key.startswith("style_")
        ):
            return True
        if issue_key in HARD_QC_REQUIRED_ISSUE_KEYS:
            return self._source_field_satisfies_reported_issue(
                scene.must_include_text, neutral_content, issue_blob
            ) or any(
                self._source_field_satisfies_reported_issue(
                    source_text, neutral_content, issue_blob
                )
                for source_text in _scene_card_source_texts(scene)
            )
        if issue_key == "unsupported_event":
            return any(
                self._source_field_satisfies_reported_issue(
                    source_text, neutral_content, issue_blob
                )
                for source_text in _scene_card_source_texts(scene)
            )
        if issue_key == "duplicate_text":
            return _reported_duplicate_appears_once(issue_blob, neutral_content)
        return False

    @staticmethod
    def _source_field_satisfies_reported_issue(
        source_text: Any, neutral_content: str, issue_blob: str
    ) -> bool:
        if not isinstance(source_text, str) or not source_text.strip():
            return False
        return source_field_satisfied(
            source_text, neutral_content
        ) and issue_mentions_source(issue_blob, source_text)

    def _apply_style_validation_gate(
        self, scene: SceneCard, neutral_content: str
    ) -> str | None:
        """PR-8 §6.6 — sync_only style validation gate。

        scene 无 project_id / 无 active binding / 调用失败 → 返 None(qc 结论直通)。
        否则返 "pass" / "partial" / "fail" / "plagiarism"(小写字串)。
        """
        import time as _time

        from novel_system.services.style_reference.injection import (
            InjectionService,
            ordered_character_ids,
        )
        from novel_system.services.style_reference.metrics_recorder import (
            MetricsRecorder,
        )
        from novel_system.services.style_reference.schemas import (
            ValidateRequest,
            ValidationMode,
            ValidationTargetKind,
        )
        from novel_system.services.style_reference.validation import (
            ValidationOrchestrator,
        )

        project_id = getattr(scene, "project_id", None)
        # PR-14/18 — character scope 用 pov ∪ onstage 匹配集(pov 优先)
        character_ids = ordered_character_ids(
            getattr(scene, "pov_character_id", None),
            getattr(scene, "onstage_chars_json", None),
        )
        # PR-15 — scene scope 用 scene_id 匹配(优先级最高)
        scene_id = getattr(scene, "scene_id", None)
        if not neutral_content or (
            not project_id and not character_ids and not scene_id
        ):
            return None
        started_at = _time.perf_counter()
        verdict: str | None = None
        profile_id: str | None = None
        binding_id: str | None = None
        runtime_contract_hash: str | None = None
        try:
            from novel_system.services.style_reference.runtime_contract import (
                contract_profile_objects,
                resolve_style_runtime_contract_state,
            )
            from novel_system.services.style_reference.validation import (
                run_sync_validate_profiles,
            )

            state = self.session.get(SceneRunState, scene.scene_id)
            bundle_row = (
                self.session.get(SceneBundle, state.current_bundle_id)
                if state is not None and state.current_bundle_id
                else None
            )
            frozen_snapshot = (
                bundle_row.frozen_snapshot_json if bundle_row is not None else None
            )
            contract_state = resolve_style_runtime_contract_state(
                frozen_snapshot
            )
            runtime_contract = contract_state.contract
            if contract_state.error_code is not None:
                raise ValueError(contract_state.error_code)
            if runtime_contract is not None:
                profiles = contract_profile_objects(runtime_contract)
                profile_id = str(runtime_contract["profile_ids"][-1])
                binding_id = str(runtime_contract["binding_ids"][-1])
                runtime_contract_hash = str(runtime_contract["contract_hash"])
                response_report = run_sync_validate_profiles(
                    neutral_content,
                    profiles,
                    self.session,
                )
                verdict = response_report.verdict.value
                return verdict
            if contract_state.mode == "absent":
                return None

            # PR-14/15/18 — 复用 InjectionService 单点选取(scene > character > project > global)
            active = InjectionService(self.session).resolve_active_binding(
                project_id,
                "scene_generation",
                character_ids=character_ids,
                scene_id=scene_id,
            )
            if active is None:
                return None
            profile_id = active.profile_id
            binding_id = active.binding_id
            orchestrator = ValidationOrchestrator(self.session, llm_enabled=False)
            response = orchestrator.validate(
                active.profile_id,
                ValidateRequest(
                    generated_text=neutral_content,
                    target_kind=ValidationTargetKind.SCENE,
                    target_ref_id=scene.scene_id,
                    mode=ValidationMode.SYNC_ONLY,
                ),
            )
            if response.sync_result is None:
                return None
            verdict = response.sync_result.verdict.value
            return verdict
        except (
            Exception
        ):  # noqa: BLE001 — gate 不阻塞主流程，但降级必须可见（审计 P-11）
            _LOGGER.warning(
                "style validation gate degraded for scene %s",
                scene.scene_id,
                exc_info=True,
            )
            return None
        finally:
            # PR-10 §13 — 记录 qc gate 决策事件;无 active binding 时不记录
            if profile_id is not None:
                MetricsRecorder.record(
                    self.session,
                    "qc_gate_decided",
                    target_kind="scene",
                    target_ref_id=scene.scene_id,
                    profile_id=profile_id,
                    binding_id=binding_id,
                    outcome=verdict or "error",
                    latency_ms=int((_time.perf_counter() - started_at) * 1000),
                    context={"runtime_contract_hash": runtime_contract_hash},
                )

    def _persist_qc_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        payload: dict[str, Any],
        neutral_content: str = "",
    ) -> QcReport:
        qc_report = QcReport(
            qc_report_id=_build_qc_report_id(scene.scene_id),
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            qc_type="hard_qc",
            source_draft_row_id=neutral_draft_row_id,
            source_bundle_id=bundle["bundle_id"],
            resolution_code=payload["resolution_code"],
            pass_flag=1 if payload["pass_flag"] else 0,
            next_action=payload["next_action"],
            issues_json=payload["issues"],
            rewrite_brief_json=self._serialize_rewrite_brief(
                payload["rewrite_brief"],
                scene=scene,
                source_content=neutral_content,
                issues=payload["issues"],
            ),
        )
        self.session.add(qc_report)
        self.session.flush()
        state.current_qc_report_id = qc_report.qc_report_id
        return qc_report

    @staticmethod
    def _apply_branch_counters(state: SceneRunState, branch: str) -> None:
        if branch == "rewrite_partial":
            state.hard_partial_rewrite_count += 1
        elif branch == "rewrite_full":
            state.hard_full_rewrite_count += 1

    @staticmethod
    def _circuit_breaker_reason(state: SceneRunState, branch: str) -> str | None:
        if state.repeat_issue_key and state.repeat_issue_count >= 2:
            return "repeat_issue_key_limit"
        if branch == "rewrite_partial" and state.hard_partial_rewrite_count > 2:
            return "hard_partial_rewrite_limit"
        if branch == "rewrite_full" and state.hard_full_rewrite_count > 1:
            return "hard_full_rewrite_limit"
        if state.total_attempt_count >= state.attempt_budget:
            return "attempt_budget_exhausted"
        return None

    @staticmethod
    def _failure_reason_for_circuit_breaker(trigger_reason: str, branch: str) -> str:
        if trigger_reason == "repeat_issue_key_limit":
            return "hard_qc surfaced the same issue key at least twice; human review is required."
        if trigger_reason == "hard_partial_rewrite_limit":
            return (
                "hard_qc exceeded the partial rewrite limit; human review is required."
            )
        if trigger_reason == "hard_full_rewrite_limit":
            return "hard_qc exceeded the full rewrite limit; human review is required."
        if trigger_reason == "attempt_budget_exhausted":
            return "scene generation exhausted the configured total attempt budget; human review is required."
        return f"hard_qc branch {branch} triggered the generation circuit breaker."

    def _record_attempt(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        source_bundle_id: str,
        branch: str,
        qc_report_id: str,
        resolution_code: str,
        next_action: str,
        human_review_event_id: str | None,
        llm_call_id: str | None = None,
        execution_step_key: str = "hard_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        continuity_warning: dict[str, Any] | None = None,
    ) -> None:
        _qc_record_attempt(
            self.session,
            step="hard_qc",
            scene_id=scene_id,
            chapter_id=chapter_id,
            source_bundle_id=source_bundle_id,
            branch=branch,
            qc_report_id=qc_report_id,
            resolution_code=resolution_code,
            next_action=next_action,
            human_review_event_id=human_review_event_id,
            execution_step_key=execution_step_key,
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )

    def _escalate_existing_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        neutral_draft_row_id: str,
        qc_report: QcReport,
        branch: str,
        failure_reason: str,
        trigger_reason: str,
        continuity_warning: dict[str, Any] | None = None,
        llm_call_id: str | None = None,
        execution_step_key: str = "hard_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
    ) -> HardQcDecision:
        replay_context = {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "source_bundle_id": bundle["bundle_id"],
            "source_bundle_hash": bundle["bundle_snapshot_hash"],
            "neutral_draft_row_id": neutral_draft_row_id,
            "current_qc_report_id": qc_report.qc_report_id,
            "scene_status_before_block": state.scene_status,
            "total_attempt_count": state.total_attempt_count,
        }
        if llm_call_id is not None:
            replay_context["llm_call_id"] = llm_call_id
        if error_code is not None:
            replay_context["error_code"] = error_code
        if retryable is not None:
            replay_context["retryable"] = retryable
        if continuity_warning is not None:
            replay_context["continuity_warning"] = continuity_warning
        event = self.human_review_manager.create_generation_blocker_event(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            object_ref=neutral_draft_row_id,
            target_type="scene_draft",
            target_id=neutral_draft_row_id,
            target_ref=f"scene_draft:{neutral_draft_row_id}",
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            recommended_action="human_review_required",
            replay_context=replay_context,
        )
        state.current_human_review_event_id = event.event_id
        state.scene_status = "human_review_required"
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=event.event_id,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )
        self.session.flush()
        return HardQcDecision(
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=event.event_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=False,
            stop_reason=trigger_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


class SoftQcEngine:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
        human_review_manager: HumanReviewManager | None = None,
    ) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)
        self.human_review_manager = human_review_manager or HumanReviewManager(session)

    def evaluate(
        self,
        *,
        scene_id: str,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        source_draft_content: str,
        execution_step_key: str = "soft_qc:0",
    ) -> SoftQcDecision:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # Wave 2（§5.4/§7.7）：软 QC 执行失败不再断头——降级为 waive + Q2 警告
        # 继续交付；确定性 gates 照跑，verified Q0/Q1 仍能阻断。
        llm_call_id, degraded_reason, payload = _qc_run_node_with_degradation(
            self.session,
            prompt_builder=self.prompt_builder,
            llm_runner=self._llm_runner,
            scene=scene,
            state=state,
            scene_id=scene_id,
            bundle=bundle,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_draft_content,
            execution_step_key=execution_step_key,
            step="soft_qc",
            message_prefix="soft QC",
            degraded_payload_factory=self._degraded_waive_payload,
        )

        payload = _qc_apply_deterministic_quality_gates(
            scene, bundle, source_draft_content, payload, qc_type="soft_qc"
        )
        payload = self._apply_quality_grading(scene, source_draft_content, payload)
        validate_qc_report("soft_qc", payload)  # 组合合法性校验（不回写 dump）
        branch = self._branch_for(payload["next_action"])
        if branch == "patch" and state.soft_patch_count >= 1:
            if has_blocking(payload.get("issues", [])):
                payload = self._block_repeat_patch_payload(payload)
            else:
                payload = self._waive_repeat_patch_payload(payload)
            validate_qc_report("soft_qc", payload)
            branch = self._branch_for(payload["next_action"])
        elif branch == "waive" and has_blocking(payload.get("issues", [])):
            payload = self._block_repeat_patch_payload(payload)
            validate_qc_report("soft_qc", payload)
            branch = self._branch_for(payload["next_action"])

        qc_report = self._persist_qc_report(
            scene=scene,
            state=state,
            bundle=bundle,
            source_draft_row_id=source_draft_row_id,
            payload=payload,
        )

        if branch == "human_review_required":
            blocking_issue = has_blocking(payload.get("issues", []))
            trigger_reason = (
                "blocking_soft_qc_issue"
                if blocking_issue
                else "soft_qc_requested_human_review"
            )
            source_draft_content_hash = _content_hash(source_draft_content)
            accepted_waiver = self.human_review_manager.accepted_soft_risk_waiver(
                scene_id=scene.scene_id,
                trigger_reason=trigger_reason,
                source_draft_content_hash=source_draft_content_hash,
            )
            if accepted_waiver is not None:
                state.current_human_review_event_id = None
                state.scene_status = "soft_qc_passed_with_author_acceptance"
                _qc_apply_issue_tracking(state, payload["issues"])
                self._record_attempt(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    source_bundle_id=bundle["bundle_id"],
                    source_draft_row_id=source_draft_row_id,
                    branch="accepted_soft_risk",
                    qc_report_id=qc_report.qc_report_id,
                    resolution_code=qc_report.resolution_code or "",
                    next_action=qc_report.next_action or "",
                    human_review_event_id=accepted_waiver["event_id"],
                    rewrite_brief=payload["rewrite_brief"],
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                )
                self.session.flush()
                return SoftQcDecision(
                    branch="waive",
                    qc_report_id=qc_report.qc_report_id,
                    human_review_event_id=accepted_waiver["event_id"],
                    resolution_code=qc_report.resolution_code or "",
                    next_action=qc_report.next_action or "",
                    should_continue=True,
                    stop_reason=f"accepted_soft_risk:{accepted_waiver['event_id']}",
                    llm_call_id=llm_call_id,
                    execution_step_key=execution_step_key,
                )
            _qc_clear_downstream_outputs(state)
            return self._escalate_existing_report(
                scene=scene,
                state=state,
                bundle=bundle,
                source_draft_row_id=source_draft_row_id,
                qc_report=qc_report,
                branch=branch,
                failure_reason=(
                    "blocking soft_qc issue prevents finalization."
                    if blocking_issue
                    else "soft_qc explicitly requested human review before finalization."
                ),
                trigger_reason=trigger_reason,
                source_draft_content_hash=source_draft_content_hash,
                llm_call_id=llm_call_id,
                execution_step_key=execution_step_key,
            )

        _qc_apply_issue_tracking(state, payload["issues"])
        if branch == "patch":
            state.scene_status = "soft_qc_patch_required"
        elif branch == "waive":
            state.scene_status = "soft_qc_passed_with_notes"
        else:
            state.scene_status = "soft_qc_passed"

        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_draft_row_id,
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=None,
            rewrite_brief=payload["rewrite_brief"],
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )
        self.session.flush()
        return SoftQcDecision(
            branch=branch,
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=None,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=branch in {"continue", "waive"},
            stop_reason=degraded_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )

    @staticmethod
    def _branch_for(next_action: str) -> str:
        return {
            "pass": "continue",
            "patch": "patch",
            "pass_with_notes": "waive",
            "human_review_required": "human_review_required",
        }[next_action]

    @staticmethod
    def _degraded_waive_payload(
        *,
        issue_key: str,
        message: str,
        continuity_warning: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """软 QC 执行失败的降级形状：waive + Q2 警告 issue（§5.4——不撤销已有正文）。"""
        issue: dict[str, Any] = {"issue_key": issue_key, "message": message}
        if continuity_warning is not None:
            issue["continuity_warning"] = continuity_warning
        return {
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "issues": [issue],
            "rewrite_brief": [],
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": f"soft QC degraded ({issue_key}): {message}"[:500],
        }

    def _apply_quality_grading(
        self, scene: SceneCard, source_draft_content: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2（§5.4）：统一分级 + 阻断裁决单一来源（软 QC 侧）。

        - 全部 issue 过分类器；确定性 Q1/Q2 在 LLM 说 pass 时仍触发一次受控补丁
          （自动修订 ≤2 的第一次），Q3（tension/theme 之外的风格层）不再强制补丁。
        - LLM 主动要求人工审阅但无 verified Q0/Q1 → 降级为 waive 携带 carry note，
          正文照常交付（G-03）。
        """
        classified = classify_issues(
            payload.get("issues") or [], scene=scene, content=source_draft_content
        )
        graded = {**payload, "issues": classified}
        if graded.get("next_action") == "pass" and any(
            issue.get("source") == "deterministic"
            and issue.get("quality_level") in ("Q1", "Q2")
            for issue in classified
        ):
            rewrite_brief = [
                item
                for item in graded.get("rewrite_brief", [])
                if isinstance(item, str) and item.strip()
            ]
            rewrite_brief = _append_unique_rewrite_briefs(
                rewrite_brief, _rewrite_briefs_for_deterministic_issues(classified)
            ) or ["修复确定性质检发现的问题后重检。"]
            graded = {
                **graded,
                "resolution_code": "soft_patch",
                "pass_flag": False,
                "next_action": "patch",
                "rewrite_brief": rewrite_brief,
                "carry_forward_note": False,
                "note_scope": None,
                "carry_note_text": None,
            }
        if graded.get("next_action") == "human_review_required" and not has_blocking(
            classified
        ):
            graded = self._waive_no_blocking_payload(graded)
        return graded

    @staticmethod
    def _waive_no_blocking_payload(payload: dict[str, Any]) -> dict[str, Any]:
        briefs = [
            item.strip()
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        if not briefs:
            briefs = [
                str(issue.get("message") or issue.get("issue_key") or "").strip()
                for issue in payload.get("issues", [])
                if isinstance(issue, dict)
            ][:3]
        summary = (
            "; ".join(item for item in briefs if item) or "soft QC advisory retained"
        )
        return {
            **payload,
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": f"软性质检意见无确定性 Q0/Q1 佐证，正文照常交付；意见随行：{summary}"[
                :500
            ],
        }

    @staticmethod
    def _block_repeat_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rewrite_brief = [
            item
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        if not rewrite_brief:
            rewrite_brief = ["阻塞级质量问题仍未解决，请人工复核后再归档。"]
        return {
            **payload,
            "resolution_code": "soft_block_human",
            "pass_flag": False,
            "next_action": "human_review_required",
            "rewrite_brief": rewrite_brief,
            "carry_forward_note": False,
            "note_scope": None,
            "carry_note_text": None,
        }

    @staticmethod
    def _waive_repeat_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
        rewrite_brief = [
            item
            for item in payload.get("rewrite_brief", [])
            if isinstance(item, str) and item.strip()
        ]
        carry_note_text = (
            "Repeated soft QC patch request after one controlled patch pass."
        )
        if rewrite_brief:
            carry_note_text = f"{carry_note_text} Carry forward: {'; '.join(item.strip() for item in rewrite_brief)}"
        return {
            **payload,
            "resolution_code": "soft_waive",
            "pass_flag": True,
            "next_action": "pass_with_notes",
            "carry_forward_note": True,
            "note_scope": "scene_memory",
            "carry_note_text": carry_note_text,
        }

    @staticmethod
    def _serialize_rewrite_brief(report: Any) -> list[dict[str, Any]]:
        entries = [{"instruction": item} for item in report.rewrite_brief]
        if report.resolution_code == "soft_waive" and report.carry_forward_note:
            entries.append(
                {
                    "kind": "carry_forward_note",
                    "note_scope": report.note_scope,
                    "carry_note_text": report.carry_note_text,
                }
            )
        return entries

    def _persist_qc_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        payload: dict[str, Any],
    ) -> QcReport:
        report = SoftQCOutput.model_validate(
            {
                **payload,
                "issues": [
                    {
                        "issue_key": issue.get("issue_key", "ok"),
                        "message": issue.get("message", ""),
                    }
                    for issue in payload["issues"]
                ],
            }
        )
        qc_report = QcReport(
            qc_report_id=_build_qc_report_id(scene.scene_id),
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            qc_type="soft_qc",
            source_draft_row_id=source_draft_row_id,
            source_bundle_id=bundle["bundle_id"],
            resolution_code=payload["resolution_code"],
            pass_flag=1 if payload["pass_flag"] else 0,
            next_action=payload["next_action"],
            issues_json=payload["issues"],
            rewrite_brief_json=self._serialize_rewrite_brief(report=report),
        )
        self.session.add(qc_report)
        self.session.flush()
        state.current_qc_report_id = qc_report.qc_report_id
        return qc_report

    def _record_attempt(
        self,
        *,
        scene_id: str,
        chapter_id: str,
        source_bundle_id: str,
        source_draft_row_id: str,
        branch: str,
        qc_report_id: str,
        resolution_code: str,
        next_action: str,
        human_review_event_id: str | None,
        rewrite_brief: list[str],
        llm_call_id: str | None = None,
        execution_step_key: str = "soft_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        continuity_warning: dict[str, Any] | None = None,
    ) -> None:
        _qc_record_attempt(
            self.session,
            step="soft_qc",
            scene_id=scene_id,
            chapter_id=chapter_id,
            source_bundle_id=source_bundle_id,
            branch=branch,
            qc_report_id=qc_report_id,
            resolution_code=resolution_code,
            next_action=next_action,
            human_review_event_id=human_review_event_id,
            execution_step_key=execution_step_key,
            llm_call_id=llm_call_id,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
            # soft 侧独有的 details_json 键（checkpoint 契约键名不变）
            details_extra={
                "source_draft_row_id": source_draft_row_id,
                "rewrite_brief": rewrite_brief,
            },
        )

    def _escalate_existing_report(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        source_draft_row_id: str,
        qc_report: QcReport,
        branch: str,
        failure_reason: str,
        trigger_reason: str,
        continuity_warning: dict[str, Any] | None = None,
        llm_call_id: str | None = None,
        execution_step_key: str = "soft_qc:0",
        error_code: str | None = None,
        retryable: bool | None = None,
        source_draft_content_hash: str | None = None,
    ) -> SoftQcDecision:
        replay_context = {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "source_bundle_id": bundle["bundle_id"],
            "source_bundle_hash": bundle["bundle_snapshot_hash"],
            "source_draft_row_id": source_draft_row_id,
            "current_qc_report_id": qc_report.qc_report_id,
            "scene_status_before_block": state.scene_status,
            "soft_patch_count": state.soft_patch_count,
        }
        if source_draft_content_hash is not None:
            replay_context["source_draft_content_hash"] = source_draft_content_hash
        if llm_call_id is not None:
            replay_context["llm_call_id"] = llm_call_id
        if error_code is not None:
            replay_context["error_code"] = error_code
        if retryable is not None:
            replay_context["retryable"] = retryable
        if continuity_warning is not None:
            replay_context["continuity_warning"] = continuity_warning
        allow_soft_risk_acceptance = (
            source_draft_content_hash is not None
            and trigger_reason
            in {"blocking_soft_qc_issue", "soft_qc_requested_human_review"}
        )
        event = self.human_review_manager.create_generation_blocker_event(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            object_ref=source_draft_row_id,
            target_type="scene_draft",
            target_id=source_draft_row_id,
            target_ref=f"scene_draft:{source_draft_row_id}",
            failure_reason=failure_reason,
            trigger_reason=trigger_reason,
            recommended_action="human_review_required",
            replay_context=replay_context,
            allow_soft_risk_acceptance=allow_soft_risk_acceptance,
        )
        state.current_human_review_event_id = event.event_id
        state.scene_status = "human_review_required"
        self._record_attempt(
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            source_bundle_id=bundle["bundle_id"],
            source_draft_row_id=source_draft_row_id,
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            human_review_event_id=event.event_id,
            rewrite_brief=qc_report.rewrite_brief_json or [],
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
            error_code=error_code,
            retryable=retryable,
            continuity_warning=continuity_warning,
        )
        self.session.flush()
        return SoftQcDecision(
            branch="human_review_required",
            qc_report_id=qc_report.qc_report_id,
            human_review_event_id=event.event_id,
            resolution_code=qc_report.resolution_code or "",
            next_action=qc_report.next_action or "",
            should_continue=False,
            stop_reason=trigger_reason,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )
