from __future__ import annotations

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, OperationalError

from novel_system.db.models import IdempotencyKey, OperationLog, SceneRunState
from novel_system.services.database_errors import is_database_busy_error
from novel_system.services.errors import DomainError
from novel_system.services.human_review_support import structured_target
from novel_system.services.llm_audit import (
    AUDIT_SCHEMA_VERSION,
    bounded_identifier,
    json_fingerprint,
)
from novel_system.services.scene_run_checkpoint import idempotency_execution_id
from novel_system.settings import get_settings


logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def owner_lease_ttl_seconds() -> int:
    """幂等租约 TTL：优先 models 配置 job_runtime.idempotency_claim_ttl_seconds。

    审计 P-8：此前该配置键无人消费（settings 硬编码 90s 是唯一生效值），
    LLM 场景 run 轻易超时 → 客户端重试把未完成的租约当过期回收 → 并发二次执行。
    读取失败时回退 settings 缺省。
    """
    try:
        from novel_system.services.llm_client import load_model_routing_config

        value = load_model_routing_config().job_runtime.get("idempotency_claim_ttl_seconds")
        if value is not None:
            return max(1, int(value))
    except Exception:
        logger.warning("Failed to load configured idempotency lease TTL; using settings fallback", exc_info=True)
    return get_settings().idempotency_ttl_seconds


def owner_lease_grace_seconds() -> int:
    try:
        from novel_system.services.llm_client import load_model_routing_config

        value = load_model_routing_config().job_runtime.get("heartbeat_interval_seconds")
        if value is not None:
            return max(1, int(value))
    except Exception:
        logger.warning("Failed to load configured idempotency heartbeat; using derived fallback", exc_info=True)
    return max(1, min(45, owner_lease_ttl_seconds() // 2))


def canonical_request_hash(method: str, path_template: str, payload: Any) -> str:
    body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{method.upper()}::{path_template}::{body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyLease:
    idempotency_key: str
    request_hash: str
    worker_id: str
    attempt_no: int
    lease_expires_at: str
    status: str = "started"
    response_json: dict[str, Any] | None = None
    reclaimed: bool = False
    _service: "IdempotencyLeaseService" = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    @property
    def execution_id(self) -> str:
        return idempotency_execution_id(self.idempotency_key)

    def renew(self, *, lease_seconds: int) -> str:
        self.lease_expires_at = self._service.renew(self, lease_seconds=lease_seconds)
        return self.lease_expires_at

    def renew_detached(self, *, lease_seconds: int) -> str:
        """Renew from a dedicated session, safe for provider heartbeat threads."""

        from novel_system.db.session import SessionLocal

        with SessionLocal() as session:
            service = IdempotencyLeaseService(session)
            detached = IdempotencyLease(
                idempotency_key=self.idempotency_key,
                request_hash=self.request_hash,
                worker_id=self.worker_id,
                attempt_no=self.attempt_no,
                lease_expires_at=self.lease_expires_at,
                status=self.status,
                response_json=self.response_json,
                reclaimed=self.reclaimed,
                _service=service,
            )
            expires = service.renew(detached, lease_seconds=lease_seconds)
            session.commit()
            return expires


class IdempotencyLeaseService:
    """Claims and renews an idempotency owner with conditional UPDATE fences."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def claim(
        self,
        *,
        idempotency_key: str,
        request_hash: str,
        worker_id: str,
        lease_seconds: int,
    ) -> IdempotencyLease:
        now = utcnow()
        now_iso = now.isoformat()
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        record = self.session.get(IdempotencyKey, idempotency_key)
        if record is None:
            record = IdempotencyKey(
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="started",
                worker_id=worker_id,
                attempt_no=1,
                heartbeat_at=now_iso,
                lease_expires_at=expires,
            )
            self.session.add(record)
            try:
                self.session.flush()
            except IntegrityError as exc:
                self.session.rollback()
                raise _in_progress_error() from exc
            return self._lease(record)

        self.session.refresh(record)
        if record.request_hash != request_hash:
            raise DomainError(
                "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
                "idempotency key reused with different payload",
                status_code=409,
            )
        if record.status == "succeeded":
            return self._lease(record)
        if record.status == "started" and record.lease_expires_at and record.lease_expires_at > now_iso:
            raise _in_progress_error()

        old_attempt = int(record.attempt_no or 0)
        old_status = record.status
        old_expiry = record.lease_expires_at
        conditions = [
            IdempotencyKey.idempotency_key == idempotency_key,
            IdempotencyKey.request_hash == request_hash,
            IdempotencyKey.status == old_status,
            IdempotencyKey.attempt_no == old_attempt,
        ]
        if record.worker_id is None:
            conditions.append(IdempotencyKey.worker_id.is_(None))
        else:
            conditions.append(IdempotencyKey.worker_id == record.worker_id)
        if old_expiry is None:
            conditions.append(IdempotencyKey.lease_expires_at.is_(None))
        else:
            conditions.append(IdempotencyKey.lease_expires_at == old_expiry)
        claimed = self.session.execute(
            update(IdempotencyKey)
            .where(*conditions)
            .values(
                status="started",
                response_json=None,
                worker_id=worker_id,
                attempt_no=old_attempt + 1,
                heartbeat_at=now_iso,
                lease_expires_at=expires,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            raise _in_progress_error()
        self.session.flush()
        record = self.session.get(IdempotencyKey, idempotency_key)
        assert record is not None
        self.session.refresh(record)
        lease = self._lease(record)
        lease.reclaimed = True
        return lease

    def renew(self, lease: IdempotencyLease, *, lease_seconds: int) -> str:
        now = utcnow()
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        renewed = self.session.execute(
            update(IdempotencyKey)
            .where(
                IdempotencyKey.idempotency_key == lease.idempotency_key,
                IdempotencyKey.request_hash == lease.request_hash,
                IdempotencyKey.status == "started",
                IdempotencyKey.worker_id == lease.worker_id,
                IdempotencyKey.attempt_no == lease.attempt_no,
            )
            .values(heartbeat_at=now.isoformat(), lease_expires_at=expires)
            .execution_options(synchronize_session=False)
        )
        if renewed.rowcount != 1:
            self.session.rollback()
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "idempotency execution owner lease was lost",
                status_code=409,
                details={
                    "idempotency_key": lease.idempotency_key,
                    "worker_id": lease.worker_id,
                    "attempt_no": lease.attempt_no,
                },
            )
        self.session.flush()
        return expires

    def _lease(self, record: IdempotencyKey) -> IdempotencyLease:
        return IdempotencyLease(
            idempotency_key=record.idempotency_key,
            request_hash=record.request_hash,
            worker_id=str(record.worker_id or ""),
            attempt_no=int(record.attempt_no or 0),
            lease_expires_at=str(record.lease_expires_at or ""),
            status=record.status,
            response_json=dict(record.response_json or {}) if record.response_json is not None else None,
            _service=self,
        )


def _in_progress_error() -> DomainError:
    return DomainError(
        "IDEMPOTENCY_REQUEST_IN_PROGRESS",
        "request with the same idempotency key is still running",
        status_code=409,
        details={
            "retryable": True,
            "next_action": "wait for the original request to finish, then retry or poll the related resource",
        },
    )




def execute_with_idempotency(
    session: Session,
    *,
    idempotency_key: str | None,
    method: str,
    path_template: str,
    payload: Any,
    action: Callable[..., dict],
    after_commit: Callable[[dict], None] | None = None,
    owned_failure_callback: Callable[[DomainError], None] | None = None,
    actor_ref: str = "operator",
    worker_id: str | None = None,
) -> tuple[dict, str | None]:
    if not idempotency_key:
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "missing X-Idempotency-Key", status_code=400)

    request_hash = canonical_request_hash(method, path_template, payload)
    operator_action_context = _prepare_operator_action_context(
        session,
        path_template=path_template,
        payload=payload,
    )
    lease = IdempotencyLeaseService(session).claim(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        worker_id=worker_id or f"http:{uuid4().hex}",
        lease_seconds=owner_lease_ttl_seconds(),
    )
    if lease.status == "succeeded":
        result = lease.response_json or {}
        if after_commit is not None:
            # A durable queued action may have committed immediately before the
            # process died. Replaying the callback is safe when the worker owns
            # a queued->running CAS, and closes that commit/dispatch gap.
            after_commit(result)
        return result, "replayed"

    session.add(
        OperationLog(
            event_type="idempotency_started",
            object_type="idempotency_key",
            object_ref=idempotency_key,
            payload_json={
                "request_hash": request_hash,
                "request_method": method.upper(),
                "request_path_template": path_template,
                **_operation_request_audit(payload),
                "attempt_no": lease.attempt_no,
                "worker_id": lease.worker_id,
                "execution_id": lease.execution_id,
                "actor_ref": actor_ref,
            },
        )
    )
    if lease.reclaimed:
        session.add(
            OperationLog(
                event_type="lease_reclaim",
                object_type="idempotency_key",
                object_ref=idempotency_key,
                payload_json={
                    "attempt_no": lease.attempt_no,
                    "worker_id": lease.worker_id,
                    "actor_ref": actor_ref,
                },
            )
        )
    # Publish the owner fence before entering a potentially long provider call.
    session.commit()

    try:
        result = _invoke_idempotent_action(action, lease)
        if "actor_ref" not in result:
            result = {**result, "actor_ref": actor_ref}
        operator_action_record = _build_operator_action_record(
            session,
            context=operator_action_context,
            method=method,
            path_template=path_template,
            payload=payload,
            result=result,
            actor_ref=actor_ref,
        )
        if operator_action_record is not None:
            session.add(operator_action_record)
        now_iso = utcnow().isoformat()
        completed = session.execute(
            update(IdempotencyKey)
            .where(
                IdempotencyKey.idempotency_key == idempotency_key,
                IdempotencyKey.request_hash == request_hash,
                IdempotencyKey.status == "started",
                IdempotencyKey.worker_id == lease.worker_id,
                IdempotencyKey.attempt_no == lease.attempt_no,
            )
            .values(status="succeeded", response_json=result, heartbeat_at=now_iso)
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount != 1:
            session.rollback()
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "idempotency owner was replaced before completion",
                status_code=409,
            )
        session.add(
            OperationLog(
                event_type="idempotency_succeeded",
                object_type="idempotency_key",
                object_ref=idempotency_key,
                payload_json={"request_hash": request_hash, "actor_ref": actor_ref},
            )
        )
        session.commit()
    except DomainError as exc:
        _mark_owned_idempotency_failed(
            session,
            lease=lease,
            actor_ref=actor_ref,
            error=exc,
            owned_failure_callback=owned_failure_callback,
        )
        raise
    except OperationalError as exc:
        _mark_owned_idempotency_failed(session, lease=lease, actor_ref=actor_ref)
        if is_database_busy_error(exc):
            raise DomainError(
                "DATABASE_BUSY",
                "database is busy; retry after the current long-running operation finishes",
                status_code=503,
                details={"retryable": True},
            ) from exc
        raise
    except Exception as exc:
        _mark_owned_idempotency_failed(session, lease=lease, actor_ref=actor_ref)
        logger.exception(
            "Idempotent action failed key=%s request_hash=%s execution_id=%s",
            lease.idempotency_key,
            lease.request_hash,
            lease.execution_id,
        )
        # Arbitrary provider/database exceptions can contain credentials, SQL,
        # local paths, or manuscript excerpts. Keep the detail in server logs and
        # return a stable, non-sensitive public message.
        raise DomainError(
            "INTERNAL_ERROR",
            "internal operation failed",
            status_code=500,
            details={"retryable": False},
        ) from exc

    # This deliberately lives outside the action exception boundary. The
    # business mutation and the succeeded idempotency record are already
    # committed; a dispatch error must not roll them back or downgrade the
    # durable idempotency state. Retrying the same key reruns only this callback.
    if after_commit is not None:
        after_commit(result)
    return result, None


def execute_with_optional_idempotency(
    session: Session,
    *,
    idempotency_key: str | None,
    method: str,
    path_template: str,
    payload: Any,
    action: Callable[..., dict],
    actor_ref: str = "operator",
) -> tuple[dict, str | None]:
    """Honor an idempotency key without breaking legacy callers that omit it.

    New browser clients attach ``X-Idempotency-Key`` to every mutation, while
    a few older API surfaces predate that contract. Those routes can use this
    compatibility wrapper during migration: keyed calls get durable
    claim/replay/conflict semantics, and unkeyed calls keep their historical
    single-execution behavior.

    The action must not commit its own transaction. On the unkeyed path this
    helper owns the commit/rollback just as ``execute_with_idempotency`` does on
    the keyed path.
    """

    if idempotency_key:
        return execute_with_idempotency(
            session,
            idempotency_key=idempotency_key,
            method=method,
            path_template=path_template,
            payload=payload,
            action=action,
            actor_ref=actor_ref,
        )

    try:
        result = _invoke_idempotent_action(action, _NoopIdempotencyLease())
        session.commit()
        return result, None
    except Exception:
        session.rollback()
        raise


@dataclass(frozen=True)
class _NoopIdempotencyLease:
    """Lease-shaped value for an optional action that accepts a lease."""

    idempotency_key: str = ""
    request_hash: str = ""
    worker_id: str = ""
    attempt_no: int = 0
    lease_expires_at: str = ""
    status: str = "unkeyed"
    response_json: dict[str, Any] | None = None
    reclaimed: bool = False

    @property
    def execution_id(self) -> str:
        return ""

    def renew(self, *, lease_seconds: int) -> str:
        del lease_seconds
        return ""


def _invoke_idempotent_action(action: Callable[..., dict], lease: IdempotencyLease) -> dict:
    try:
        signature = inspect.signature(action)
    except (TypeError, ValueError):
        return action()
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if positional:
        return action(lease)
    return action()


def _mark_owned_idempotency_failed(
    session: Session,
    *,
    lease: IdempotencyLease,
    actor_ref: str,
    error: DomainError | None = None,
    owned_failure_callback: Callable[[DomainError], None] | None = None,
) -> None:
    # Discard every write made by the failed action before trying to publish a
    # durable failure. This prevents a domain failure from committing unrelated
    # caller state that happened to be pending in the request session.
    session.rollback()
    now_iso = utcnow().isoformat()
    failed = session.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.idempotency_key == lease.idempotency_key,
            IdempotencyKey.request_hash == lease.request_hash,
            IdempotencyKey.status == "started",
            IdempotencyKey.worker_id == lease.worker_id,
            IdempotencyKey.attempt_no == lease.attempt_no,
        )
        .values(status="failed", heartbeat_at=now_iso)
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 1:
        try:
            # Only the current worker/attempt reaches this callback. It shares
            # the transaction with the owner CAS above and must not commit on
            # its own. A reclaimed (stale) worker misses the CAS and cannot
            # mutate domain failure state.
            if owned_failure_callback is not None and error is not None:
                owned_failure_callback(error)
            session.add(
                OperationLog(
                    event_type="idempotency_failed",
                    object_type="idempotency_key",
                    object_ref=lease.idempotency_key,
                    payload_json={
                        "request_hash": lease.request_hash,
                        "attempt_no": lease.attempt_no,
                        "worker_id": lease.worker_id,
                        "actor_ref": actor_ref,
                        "error_code": error.code if error is not None else None,
                    },
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    else:
        session.rollback()


def _prepare_operator_action_context(session: Session, *, path_template: str, payload: Any) -> dict[str, Any] | None:
    payload = payload or {}

    if path_template == "/api/v1/scenes/{scene_id}/run/full":
        scene_id = payload.get("scene_id")
        if not isinstance(scene_id, str) or not scene_id:
            return None
        state = session.get(SceneRunState, scene_id)
        return {
            "object_type": "scene_card",
            "object_ref": scene_id,
            "action": "run_scene",
            "status_before": state.scene_status if state else None,
            "target_refs": [_target("scene_card", scene_id)],
        }

    return None


def _build_operator_action_record(
    session: Session,
    *,
    context: dict[str, Any] | None,
    method: str,
    path_template: str,
    payload: Any,
    result: dict[str, Any],
    actor_ref: str,
) -> OperationLog | None:
    if context is None:
        return None

    object_type = context["object_type"]
    object_ref = context["object_ref"]
    action = context["action"]
    status_after, resolution_reason, extra_payload = _resolve_operator_action_outcome(
        session,
        action=action,
        object_ref=object_ref,
        result=result,
    )
    target_refs = _dedupe_targets([*context.get("target_refs", []), *extra_payload.pop("target_refs", [])])
    summary = resolution_reason

    return OperationLog(
        event_type="operator_action",
        object_type=object_type,
        object_ref=object_ref,
        payload_json={
            "actor_ref": actor_ref,
            "action": action,
            "status_before": context.get("status_before"),
            "status_after": status_after,
            "resolution_reason": resolution_reason,
            "summary": summary,
            "request_method": method.upper(),
            "request_path_template": path_template,
            **_operation_request_audit(payload),
            "target_refs": target_refs,
            **extra_payload,
        },
    )


def _operation_request_audit(payload: Any) -> dict[str, Any]:
    """Keep request lineage without copying arbitrary request bodies into logs.

    A bounded risk-confirmation reason remains readable because it is itself the
    operator's audit attestation, not a manuscript recovery copy.
    """

    normalized = {} if payload is None else payload
    result: dict[str, Any] = {
        "_request_payload_audit_version": AUDIT_SCHEMA_VERSION,
        "request_payload_summary": json_fingerprint(normalized),
    }
    if not isinstance(normalized, dict):
        return result
    recovery_payload: dict[str, Any] = {
        key: bounded_identifier(normalized.get(key))
        for key in ("review_id", "job_id")
        if normalized.get(key) is not None
    }
    confirmation = normalized.get("risk_confirmation")
    if isinstance(confirmation, dict):
        reason = confirmation.get("reason")
        if isinstance(reason, str) and reason.strip():
            reason = reason.strip()
            reason_cap = 512
            recovery_payload["risk_confirmation"] = {
                "acknowledged": confirmation.get("acknowledged") is True,
                "reason": reason[:reason_cap],
                "reason_sha256": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
                "reason_chars": len(reason),
                "reason_truncated": len(reason) > reason_cap,
                "severity": str(confirmation.get("severity") or "high")[:32],
            }
    if recovery_payload:
        result["request_payload"] = recovery_payload
    return result


def _resolve_operator_action_outcome(
    session: Session,
    *,
    action: str,
    object_ref: str,
    result: dict[str, Any],
) -> tuple[str | None, str, dict[str, Any]]:
    if action == "run_scene":
        return (
            result.get("scene_status"),
            "scene run completed and final scene archived",
            {
                "scene_id": object_ref,
                "current_bundle_id": result.get("current_bundle_id"),
                "current_bundle_hash": result.get("current_bundle_hash"),
                "current_final_scene_row_id": result.get("current_final_scene_row_id"),
            },
        )

    return (None, action, {})


def _target(target_type: str, target_id: str) -> dict[str, str]:
    target = structured_target(target_type, target_id)
    assert target is not None
    return target


def _dedupe_targets(targets: list[dict[str, str] | None]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen_refs: set[str] = set()
    for target in targets:
        if target is None:
            continue
        target_ref = target["target_ref"]
        if target_ref in seen_refs:
            continue
        seen_refs.add(target_ref)
        deduped.append(target)
    return deduped


def _result_targets(items: Any) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    targets: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        target = item.get("target")
        if not isinstance(target, dict):
            continue
        target_type = target.get("target_type")
        target_id = target.get("target_id")
        target_ref = target.get("target_ref")
        structured = structured_target(target_type, target_id, target_ref)
        if structured is not None:
            targets.append(structured)
    return targets
