"""Crash-safe startup recovery for in-process background workers.

Workers still own the authoritative CAS.  The startup scan only discovers
durable candidates and submits them, which makes duplicate scans from multiple
ASGI workers harmless: at most one worker can move a row into an owned running
state.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from novel_system.db.models import (
    BackgroundRecoveryLease,
    ChapterGoal,
    ChapterRunJob,
    StyleReferenceRun,
    StyleReferenceValidationReport,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError


logger = logging.getLogger(__name__)

STYLE_REFERENCE_RUN_STALE_MINUTES = 60
VALIDATION_STARTUP_GRACE_SECONDS = 30
STARTUP_RECOVERY_LEASE_SECONDS = 30

SceneDispatch = Callable[[str], None]
ChapterDispatch = Callable[[str, str, str | None], None]
StyleDispatch = Callable[[str, str, list[str], Any], None]


def recover_run_job_dispatches(
    session: Session,
    *,
    now: datetime | None = None,
    scene_dispatch: SceneDispatch,
    chapter_dispatch: ChapterDispatch,
) -> dict[str, list[str]]:
    """Re-submit queued/pending jobs and abandoned RUNNING leases."""

    current = now or datetime.now(UTC)
    current_iso = current.isoformat()
    rows = list(
        session.scalars(
            select(ChapterRunJob).where(
                ChapterRunJob.job_type.in_(("scene_run_full", "chapter_run_full")),
                ChapterRunJob.status.in_(("queued", "pending", "running")),
            )
        )
    )
    candidates: list[tuple[str, str, str | None, str | None]] = []
    skipped_active: list[str] = []
    for job in rows:
        if job.status == "running" and _lease_is_active(job.lease_expires_at, current_iso):
            skipped_active.append(job.job_id)
            continue
        if job.job_type == "scene_run_full" and job.status not in {"queued", "running"}:
            continue
        if job.job_type == "chapter_run_full" and job.status not in {"pending", "running"}:
            continue
        project_id: str | None = None
        if job.chapter_id:
            chapter = session.get(ChapterGoal, job.chapter_id)
            project_id = str(chapter.project_id) if chapter and chapter.project_id else None
        candidates.append((job.job_type, job.job_id, job.chapter_id, project_id))
    session.rollback()

    dispatched_scene: list[str] = []
    dispatched_chapter: list[str] = []
    for job_type, job_id, chapter_id, project_id in candidates:
        if job_type == "scene_run_full":
            scene_dispatch(job_id)
            dispatched_scene.append(job_id)
        elif chapter_id:
            chapter_dispatch(job_id, str(chapter_id), project_id)
            dispatched_chapter.append(job_id)
    return {
        "scene_dispatched": dispatched_scene,
        "chapter_dispatched": dispatched_chapter,
        "active_lease_skipped": skipped_active,
    }


def recover_style_reference_dispatches(
    session: Session,
    *,
    llm_client: Any | None,
    llm_enabled: bool,
    style_dispatch: StyleDispatch,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Re-dispatch never-started extraction runs; fail unsafe partial runs."""

    current = now or datetime.now(UTC)
    stale_before = current - timedelta(minutes=STYLE_REFERENCE_RUN_STALE_MINUTES)
    rows = list(
        session.scalars(
            select(StyleReferenceRun).where(
                StyleReferenceRun.status == "running",
                StyleReferenceRun.dispatch_state.in_(("queued", "running")),
            )
        )
    )
    queued: list[tuple[str, str, list[str]]] = []
    failed: list[str] = []
    skipped_active: list[str] = []
    for run in rows:
        if run.dispatch_state == "queued":
            layers = [str(value) for value in (run.requested_layers_json or []) if str(value)]
            if not layers:
                changed = _fail_style_run(
                    session,
                    run,
                    code="STYLE_REFERENCE_RUN_RECOVERY_METADATA_MISSING",
                    message="queued extraction is missing requested layers; start a new run",
                )
                if changed:
                    failed.append(run.run_id)
            elif not llm_enabled or llm_client is None:
                changed = _fail_style_run(
                    session,
                    run,
                    code="STYLE_REFERENCE_LLM_REQUIRED_AFTER_RESTART",
                    message="queued extraction could not restart because no LLM is configured",
                )
                if changed:
                    failed.append(run.run_id)
            else:
                queued.append((run.run_id, run.book_id, layers))
            continue

        heartbeat = _parse_iso(run.heartbeat_at)
        if heartbeat is not None and heartbeat > stale_before:
            skipped_active.append(run.run_id)
            continue
        changed = _fail_style_run(
            session,
            run,
            code="STYLE_REFERENCE_RUN_INTERRUPTED",
            message="background extraction was interrupted; start a new run to retry",
        )
        if changed:
            failed.append(run.run_id)
    session.commit()

    dispatched: list[str] = []
    for run_id, book_id, layers in queued:
        style_dispatch(run_id, book_id, layers, llm_client)
        dispatched.append(run_id)
    return {
        "queued_dispatched": dispatched,
        "interrupted_failed": failed,
        "active_heartbeat_skipped": skipped_active,
    }


def recover_validation_reports(
    session: Session,
    *,
    now: datetime | None = None,
    grace_seconds: int = VALIDATION_STARTUP_GRACE_SECONDS,
) -> list[str]:
    """Fail orphaned async validations without retaining their input prose.

    Validation text intentionally exists only in worker memory, so a process
    restart cannot safely resume the job.  A short grace protects an active
    peer process during multi-worker startup.
    """

    current = now or datetime.now(UTC)
    cutoff = current - timedelta(seconds=max(0, grace_seconds))
    rows = list(
        session.scalars(
            select(StyleReferenceValidationReport).where(
                StyleReferenceValidationReport.status.in_(("queued", "running"))
            )
        )
    )
    failed: list[str] = []
    for report in rows:
        last_seen = _parse_iso(report.heartbeat_at or report.started_at or report.created_at)
        if last_seen is not None and last_seen > cutoff:
            continue
        finished_at = utcnow()
        changed = session.execute(
            update(StyleReferenceValidationReport)
            .where(
                StyleReferenceValidationReport.report_id == report.report_id,
                StyleReferenceValidationReport.status == report.status,
                (
                    StyleReferenceValidationReport.heartbeat_at.is_(None)
                    if report.heartbeat_at is None
                    else StyleReferenceValidationReport.heartbeat_at == report.heartbeat_at
                ),
            )
            .values(
                verdict="fail",
                status="failed",
                error_code="STYLE_REFERENCE_VALIDATION_INTERRUPTED",
                error_text="async validation was interrupted; submit the text again to retry",
                retryable=True,
                heartbeat_at=finished_at,
                finished_at=finished_at,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount == 1:
            failed.append(report.report_id)
    session.commit()
    return failed


def run_startup_recovery() -> dict[str, Any]:
    """FastAPI lifespan entry point; failures are isolated by job family."""

    owner_id = f"startup:{uuid4().hex}"
    try:
        with SessionLocal() as session:
            if not acquire_startup_recovery_lease(session, owner_id=owner_id):
                return {"skipped": "another_startup_worker_owns_recovery"}
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery lease acquisition failed")
        return {"skipped": "recovery_lease_unavailable"}

    summary: dict[str, Any] = {}
    try:
        from novel_system.services.llm_accounting import (
            recover_stale_legacy_reservations,
        )

        with SessionLocal() as session:
            summary["llm_legacy_reservations"] = recover_stale_legacy_reservations(
                session
            )
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while reconciling legacy LLM reservations")
        summary["llm_legacy_reservations"] = {"error": "scan_failed"}

    try:
        with SessionLocal() as session:
            summary["run_jobs"] = recover_run_job_dispatches(
                session,
                scene_dispatch=_dispatch_scene,
                chapter_dispatch=_dispatch_chapter,
            )
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while scanning scene/chapter jobs")
        summary["run_jobs"] = {"error": "scan_failed"}

    try:
        from novel_system.services.scene_run_jobs import recover_expired_cancel_requested_jobs

        with SessionLocal() as session:
            summary["runtime_sweep"] = {
                "expired_cancel_requests": recover_expired_cancel_requested_jobs(
                    session, worker_id="startup_recovery"
                )
            }
            session.commit()
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while sweeping expired cancel requests")
        summary["runtime_sweep"] = {"error": "scan_failed"}

    llm_client, llm_enabled = _build_style_reference_llm_client()
    try:
        with SessionLocal() as session:
            summary["style_reference_runs"] = recover_style_reference_dispatches(
                session,
                llm_client=llm_client,
                llm_enabled=llm_enabled,
                style_dispatch=_dispatch_style_reference,
            )
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while scanning style-reference runs")
        summary["style_reference_runs"] = {"error": "scan_failed"}

    try:
        with SessionLocal() as session:
            summary["validation_reports_failed"] = recover_validation_reports(session)
    except Exception:  # pragma: no cover - startup boundary
        logger.exception("startup recovery failed while scanning validation reports")
        summary["validation_reports_failed"] = []
    logger.info("startup background recovery summary=%s", summary)
    return summary


def acquire_startup_recovery_lease(
    session: Session,
    *,
    owner_id: str,
    now: datetime | None = None,
    lease_seconds: int = STARTUP_RECOVERY_LEASE_SECONDS,
) -> bool:
    """Elect one scanner across processes using insert-or-expired-CAS."""

    current = now or datetime.now(UTC)
    expires_at = (current + timedelta(seconds=max(1, lease_seconds))).isoformat()
    values = {
        "lease_key": "application_startup_recovery",
        "owner_id": owner_id,
        "lease_expires_at": expires_at,
        "created_at": current.isoformat(),
        "updated_at": current.isoformat(),
    }
    try:
        session.execute(insert(BackgroundRecoveryLease).values(**values))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()

    current_row = session.get(BackgroundRecoveryLease, values["lease_key"])
    if current_row is None:
        session.rollback()
        return False
    if _lease_is_active(current_row.lease_expires_at, current.isoformat()):
        session.rollback()
        return False
    previous_owner = current_row.owner_id
    previous_expiry = current_row.lease_expires_at
    session.rollback()
    won = session.execute(
        update(BackgroundRecoveryLease)
        .where(
            BackgroundRecoveryLease.lease_key == values["lease_key"],
            BackgroundRecoveryLease.owner_id == previous_owner,
            BackgroundRecoveryLease.lease_expires_at == previous_expiry,
        )
        .values(
            owner_id=owner_id,
            lease_expires_at=expires_at,
            updated_at=current.isoformat(),
        )
        .execution_options(synchronize_session=False)
    )
    if won.rowcount == 1:
        session.commit()
        return True
    session.rollback()
    return False


def _dispatch_scene(job_id: str) -> None:
    from novel_system.services.scene_run_jobs import start_scene_run_job_worker

    start_scene_run_job_worker(job_id)


def _dispatch_chapter(job_id: str, chapter_id: str, project_id: str | None) -> None:
    if project_id:
        from novel_system.services.projects import start_project_chapter_run_job_worker

        start_project_chapter_run_job_worker(project_id, chapter_id, job_id)
        return
    thread = threading.Thread(
        target=_run_unscoped_chapter_job,
        args=(job_id, chapter_id),
        daemon=True,
        name=f"chapter-recovery:{job_id}",
    )
    thread.start()


def _run_unscoped_chapter_job(job_id: str, chapter_id: str) -> None:
    from novel_system.services.chapter_runner import ChapterRunnerService

    try:
        with SessionLocal() as session:
            ChapterRunnerService(session).run_full(chapter_id)
            session.commit()
    except DomainError as exc:
        if exc.code not in {"RUN_JOB_IN_PROGRESS", "RUN_JOB_NOT_CLAIMABLE"}:
            logger.exception("recovered chapter job %s failed: %s", job_id, exc.code)
    except Exception:  # pragma: no cover - worker boundary
        logger.exception("recovered chapter job %s failed", job_id)


def _dispatch_style_reference(
    run_id: str,
    book_id: str,
    layers: list[str],
    llm_client: Any,
) -> None:
    from novel_system.services.style_reference.run_orchestrator import (
        start_style_reference_run_worker,
    )

    start_style_reference_run_worker(
        run_id=run_id,
        book_id=book_id,
        layer_values=layers,
        llm_client=llm_client,
    )


def _build_style_reference_llm_client() -> tuple[Any | None, bool]:
    from novel_system.settings import get_settings

    # get_settings 留在 try 外:settings 读取失败必须向上抛,只有构造失败才降级。
    settings = get_settings()
    if not settings.llm_enabled:
        return None, False
    try:
        from novel_system.services.system_config import build_runtime_llm_client

        return build_runtime_llm_client(settings=settings)
    except Exception:  # pragma: no cover - invalid runtime configuration
        logger.exception("style-reference recovery could not build its LLM client")
        return None, False


def _fail_style_run(
    session: Session,
    run: StyleReferenceRun,
    *,
    code: str,
    message: str,
) -> bool:
    coverage = dict(run.coverage_json or {})
    coverage["failure_reason"] = code
    coverage["retryable"] = True
    finished_at = utcnow()
    changed = session.execute(
        update(StyleReferenceRun)
        .where(
            StyleReferenceRun.run_id == run.run_id,
            StyleReferenceRun.status == "running",
            StyleReferenceRun.dispatch_state == run.dispatch_state,
            (
                StyleReferenceRun.heartbeat_at.is_(None)
                if run.heartbeat_at is None
                else StyleReferenceRun.heartbeat_at == run.heartbeat_at
            ),
        )
        .values(
            status="failed",
            dispatch_state="failed",
            coverage_json=coverage,
            heartbeat_at=finished_at,
            finished_at=finished_at,
            error_code=code,
            error_text=message,
            retryable=True,
        )
        .execution_options(synchronize_session=False)
    )
    return changed.rowcount == 1


def _lease_is_active(value: str | None, now_iso: str) -> bool:
    # ISO-8601 timestamps produced by the service are fixed-width UTC values;
    # parse malformed/legacy values as abandoned rather than immortal.
    parsed = _parse_iso(value)
    if parsed is None:
        return False
    now = _parse_iso(now_iso)
    return bool(now is not None and parsed > now)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
