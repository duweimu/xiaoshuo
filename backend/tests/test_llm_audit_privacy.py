from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select

from novel_system.db.models import IdempotencyKey, LlmCall, LlmCallAttempt, OperationLog
from novel_system.services.idempotency import execute_with_idempotency
from novel_system.services.llm_accounting import (
    LLMAccountingRejected,
    LLMCallContext,
    record_rejected_call,
)
from novel_system.services.llm_audit import (
    AUDIT_SUMMARY_BYTE_CAP,
    audit_error_text,
    bounded_identifier,
    fingerprint_identifier,
    sanitize_audit_summary,
)
from novel_system.services.llm_client import LLMRequest


SECRET = "绝密作者正文-DO-NOT-PERSIST"
ASCII_API_KEY_LIKE_SECRET = "sk-proj-ULTRA_SECRET_MANUSCRIPT_42"


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_legacy_audit_rows(
    session,
    *,
    suffix: str,
    provider_request_id: str = SECRET,
) -> tuple[str, str, str]:
    call_id = f"legacy-privacy-call-{suffix}"
    attempt_id = f"legacy-privacy-attempt-{suffix}"
    operation_ref = f"legacy-privacy-{suffix}"
    parent = LlmCall(
        llm_call_id=call_id,
        provider="test",
        model="test",
        node_id="privacy",
        prompt_hash="a" * 64,
        step="privacy",
        project_id="privacy-project",
        request_payload_summary={
            "messages": [{"role": "user", "content": SECRET}],
            "source_draft_content": SECRET,
        },
        response_payload_summary={
            "request_id": provider_request_id,
            "structured_output": {"scene_text": SECRET},
        },
        native_reasoning_json={"reasoning_text": SECRET},
        scope_type="project",
        scope_id="privacy-project",
        estimated_tokens=0,
        reserved_tokens=0,
        budget_charged_tokens=0,
        accounting_status="failed",
    )
    session.add(parent)
    session.add(
        LlmCallAttempt(
            attempt_id=attempt_id,
            llm_call_id=parent.llm_call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=0,
            provider_request_id=provider_request_id,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_tokens=0,
            reserved_tokens=0,
            budget_charged_tokens=0,
            accounting_status="failed",
            latency_ms=0,
            error_code="PROVIDER_ERROR",
            error_text=SECRET,
        )
    )
    session.add(
        OperationLog(
            event_type="idempotency_started",
            object_type="idempotency_key",
            object_ref=operation_ref,
            payload_json={
                "request_hash": "b" * 64,
                "request_payload": {
                    "review_id": "review-recovery-safe",
                    "author_note": SECRET,
                    "content": SECRET,
                },
            },
        )
    )
    session.commit()
    return call_id, attempt_id, operation_ref


def test_audit_summary_is_content_free_and_strictly_bounded() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": SECRET * 1_000},
            {"role": "user", "content": f"author_note={SECRET}" * 1_000},
        ],
        "source_draft_content": SECRET * 2_000,
        "structured_output": {"scene_text": SECRET * 2_000},
        **{f"untrusted_{index}": SECRET * 100 for index in range(2_000)},
    }

    summary = sanitize_audit_summary(payload)
    rendered = _serialized(summary)

    assert SECRET not in rendered
    assert len(rendered.encode("utf-8")) <= AUDIT_SUMMARY_BYTE_CAP
    assert summary["messages"]["count"] == 2
    assert summary["messages"]["items"][1]["role"] == "user"
    assert summary["source_draft_content"]["kind"] == "text_fingerprint"
    assert summary["structured_output"]["kind"] == "json_fingerprint"
    assert sanitize_audit_summary(summary)["messages"] == summary["messages"]


def test_redacted_identifiers_and_errors_are_idempotent() -> None:
    identifier = bounded_identifier(SECRET)
    private_identifier = fingerprint_identifier(ASCII_API_KEY_LIKE_SECRET)
    error = audit_error_text(SECRET, error_code="PROVIDER_ERROR")

    assert identifier is not None
    assert SECRET not in identifier
    assert bounded_identifier(identifier) == identifier
    assert private_identifier is not None
    assert ASCII_API_KEY_LIKE_SECRET not in private_identifier
    assert fingerprint_identifier(private_identifier) == private_identifier
    assert error is not None
    assert SECRET not in error
    assert audit_error_text(error, error_code="PROVIDER_ERROR") == error


def test_external_request_ids_are_fingerprinted_inside_audit_summaries() -> None:
    summary = sanitize_audit_summary(
        {
            "request_id": ASCII_API_KEY_LIKE_SECRET,
            "provider_request_id": ASCII_API_KEY_LIKE_SECRET,
        }
    )

    expected = fingerprint_identifier(ASCII_API_KEY_LIKE_SECRET)
    assert summary["request_id"] == expected
    assert summary["provider_request_id"] == expected
    assert ASCII_API_KEY_LIKE_SECRET not in _serialized(summary)
    assert sanitize_audit_summary(summary) == summary


def test_rejected_llm_call_persists_only_fingerprints(session) -> None:
    request = LLMRequest(
        model="privacy-test",
        messages=[
            {"role": "system", "content": SECRET * 100},
            {"role": "user", "content": f"author_note={SECRET}" * 100},
        ],
        temperature=0.2,
        max_output_tokens=64,
        response_format="json_object",
        provider="openai_compatible",
        node_id="privacy_test",
    )
    call_id = record_rejected_call(
        session,
        request,
        LLMCallContext(
            scope_type="project",
            scope_id="privacy-project",
            project_id="privacy-project",
            node_id="privacy_test",
            step="privacy_test",
        ),
        LLMAccountingRejected(
            "PRIVACY_TEST_REJECTION",
            f"provider echoed {SECRET}",
            details={"provider_body": SECRET * 1_000},
        ),
        request_payload_summary={
            "source_draft_content": SECRET * 1_000,
            "nested": {"author_note": SECRET * 1_000},
        },
        response_payload_summary={
            "structured_output": {"scene_text": SECRET * 1_000},
            "message": SECRET * 1_000,
        },
    )

    stored = session.get(LlmCall, call_id)
    assert stored is not None
    rendered = _serialized(
        {
            "request": stored.request_payload_summary,
            "response": stored.response_payload_summary,
        }
    )
    assert SECRET not in rendered
    request_bytes = len(_serialized(stored.request_payload_summary).encode("utf-8"))
    response_bytes = len(_serialized(stored.response_payload_summary).encode("utf-8"))
    assert request_bytes <= AUDIT_SUMMARY_BYTE_CAP
    assert response_bytes <= AUDIT_SUMMARY_BYTE_CAP
    assert stored.prompt_hash
    assert session.query(LlmCallAttempt).count() == 0


def test_idempotency_log_omits_request_body_and_replay_still_works(session) -> None:
    payload = {
        "scene_id": "SC_PRIVACY",
        "author_note": SECRET * 500,
        "content": SECRET * 500,
    }

    first, status = execute_with_idempotency(
        session,
        idempotency_key="privacy-idempotency",
        method="POST",
        path_template="/privacy-test",
        payload=payload,
        action=lambda: {"ok": True},
    )
    replay, replay_status = execute_with_idempotency(
        session,
        idempotency_key="privacy-idempotency",
        method="POST",
        path_template="/privacy-test",
        payload=payload,
        action=lambda: {"ok": False},
    )

    started = session.scalars(
        select(OperationLog).where(OperationLog.event_type == "idempotency_started")
    ).one()
    rendered = _serialized(started.payload_json)
    assert SECRET not in rendered
    assert "request_payload" not in started.payload_json
    assert started.payload_json["_request_payload_audit_version"] == 2
    assert started.payload_json["request_payload_summary"]["kind"] == "json_fingerprint"
    assert first["ok"] is True
    assert status is None
    assert replay["ok"] is True
    assert replay_status == "replayed"
    # response_json is the authoritative replay value, not a diagnostic audit copy.
    assert session.get(IdempotencyKey, "privacy-idempotency").response_json == first


