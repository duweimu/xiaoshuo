from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, HumanReviewEvent, SceneCard, SceneRunState, utcnow
from novel_system.db.session import SessionLocal
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_actions import author_action
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.errors import DomainError
from novel_system.services.idempotency import owner_lease_ttl_seconds
from novel_system.services.scene_run_checkpoint import chapter_scene_execution_id

JOB_TYPE_CHAPTER_FULL = "chapter_run_full"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_BLOCKED = "blocked"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

logger = logging.getLogger(__name__)


@dataclass
class ChapterRunLease:
    job_id: str
    worker_id: str
    attempt_no: int
    lease_expires_at: str
    _service: "ChapterRunnerService" = field(repr=False, compare=False)

    def renew(self, *, lease_seconds: int) -> str:
        self.lease_expires_at = self._service._renew_lease(self, lease_seconds=lease_seconds)
        return self.lease_expires_at

    def renew_detached(self, *, lease_seconds: int) -> str:
        """Renew with an independent session for long provider calls."""

        with SessionLocal() as session:
            service = ChapterRunnerService(session)
            detached = ChapterRunLease(
                job_id=self.job_id,
                worker_id=self.worker_id,
                attempt_no=self.attempt_no,
                lease_expires_at=self.lease_expires_at,
                _service=service,
            )
            expires = service._renew_lease(detached, lease_seconds=lease_seconds)
            session.commit()
            return expires


@dataclass
class _CompositeLeaseRenewer:
    chapter_lease: ChapterRunLease
    request_lease: Any | None = None

    def __call__(self, *, lease_seconds: int) -> None:
        self.chapter_lease.renew(lease_seconds=lease_seconds)
        if self.request_lease is not None:
            self.request_lease.renew(lease_seconds=lease_seconds)

    def renew_detached(self, *, lease_seconds: int) -> None:
        self.chapter_lease.renew_detached(lease_seconds=lease_seconds)
        if self.request_lease is None:
            return
        renew = getattr(self.request_lease, "renew_detached", None)
        if callable(renew):
            renew(lease_seconds=lease_seconds)


class ChapterRunnerService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._active_owner: ChapterRunLease | None = None

    def run_full(self, chapter_id: str, *, restart: bool = False, request_lease=None) -> dict[str, Any]:
        AuthorLifecycleService(self.session).require_active_chapter(chapter_id)
        scene_ids = self._scene_ids(chapter_id)
        job = None if restart else self._resumeable_job(chapter_id)
        if job is None:
            job = self._create_job(chapter_id, scene_ids)
        else:
            self._reconcile_job(job, scene_ids)
            self._transition_explicit_failed_retry(job)
            if job.status in {JOB_STATUS_BLOCKED, JOB_STATUS_COMPLETED}:
                self.session.flush()
                return self._serialize_job(job)
            self.session.flush()

        owner = self._claim_running(
            job,
            worker_id=f"chapter-run:{uuid4().hex}",
            lease_seconds=owner_lease_ttl_seconds(),
        )
        self._active_owner = owner
        self.session.commit()

        renew_all = _CompositeLeaseRenewer(owner, request_lease)

        orchestrator = Orchestrator(self.session)
        # claim 已提交（running + 有效租约）。从这里到 worker 循环里场景执行之外的每一步
        # （预检门、_set_current_scene、终态写入）都可能抛 DomainError；若任其裸抛，
        # 任务会永远停在 running：run-status 显示运行中 0%、run-job 命中
        # RUN_JOB_IN_PROGRESS，租约到期后 prepare_full_run 也不会再拉起 worker。
        # 所以这里比照场景执行失败：释放归属、按错误码落 failed 并提交，再把原异常抛出。
        active_scene_id: str | None = None
        try:
            while True:
                active_scene_id = None
                next_scene_id = self._next_scene_id(job, scene_ids)
                if next_scene_id is None:
                    self._mark_completed(job)
                    self.session.flush()
                    return self._serialize_job(job)

                gate_error = self._chapter_gate_error(chapter_id, scene_id=next_scene_id)
                if gate_error is not None:
                    self._mark_blocked(job, blocked_scene_id=next_scene_id, latest_error=gate_error)
                    self.session.flush()
                    return self._serialize_job(job)

                active_scene_id = next_scene_id
                self._set_current_scene(job, next_scene_id)
                result = self._run_claimed_scene(
                    orchestrator,
                    job,
                    chapter_id=chapter_id,
                    scene_id=next_scene_id,
                    lease_renewer=renew_all,
                )
                if result is None:
                    return self._serialize_job(job)

                if self._scene_requires_human_review(result):
                    self._mark_blocked(
                        job,
                        blocked_scene_id=next_scene_id,
                        latest_error=self._human_review_error(next_scene_id, result),
                    )
                    self.session.flush()
                    return self._serialize_job(job)

                scene_incomplete_error = self._scene_incomplete_error(next_scene_id, result)
                if scene_incomplete_error is not None:
                    self._mark_blocked(job, blocked_scene_id=next_scene_id, latest_error=scene_incomplete_error)
                    self.session.flush()
                    return self._serialize_job(job)

                self._mark_scene_completed(job, next_scene_id)
                gate_error = self._chapter_gate_error(chapter_id, scene_id=next_scene_id)
                if gate_error is not None:
                    self._mark_blocked(job, blocked_scene_id=next_scene_id, latest_error=gate_error)
                    self.session.flush()
                    return self._serialize_job(job)
        except DomainError as exc:
            self._fail_claimed_job(
                job,
                scene_id=active_scene_id,
                error_code=exc.code,
                error_text=exc.message,
                author_action=self._domain_error_author_action(exc),
            )
            raise
        except Exception:  # safety net for runtime failures (covered by test_fix_chapter_runner_catalog_scenes)
            self._fail_claimed_job(
                job,
                scene_id=active_scene_id,
                error_code="CHAPTER_RUN_FAILED",
                error_text="chapter run failed; see server logs for the request details",
            )
            raise

    def _run_claimed_scene(
        self,
        orchestrator: Any,
        job: ChapterRunJob,
        *,
        chapter_id: str,
        scene_id: str,
        lease_renewer: _CompositeLeaseRenewer,
    ) -> dict[str, Any] | None:
        """执行一个场景；执行异常时按失败落库并返回 None（调用方直接序列化任务）。"""

        try:
            call_parameters = inspect.signature(orchestrator.run_scene).parameters
            if "execution_id" in call_parameters:
                call_kwargs = {
                    "execution_id": chapter_scene_execution_id(job.job_id, scene_id),
                    "lease_renewer": lease_renewer,
                }
                if "run_job_id" in call_parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in call_parameters.values()
                ):
                    call_kwargs["run_job_id"] = job.job_id
                result = orchestrator.run_scene(scene_id, **call_kwargs)
            else:  # compatibility for focused test doubles
                result = orchestrator.run_scene(scene_id)
        except Exception as exc:  # pragma: no cover - safety net for runtime failures
            self._release_scene_job_ownership(scene_id, job.job_id)
            logger.exception(
                "Chapter scene execution failed job_id=%s chapter_id=%s scene_id=%s",
                job.job_id,
                chapter_id,
                scene_id,
            )
            public_code = exc.code if isinstance(exc, DomainError) else "CHAPTER_RUN_FAILED"
            public_message = (
                exc.message
                if isinstance(exc, DomainError)
                else "scene execution failed; see server logs for the request details"
            )
            self._mark_failed(
                job,
                current_scene_id=scene_id,
                error_code=public_code,
                error_text=public_message,
                author_action=self._domain_error_author_action(exc),
            )
            self.session.flush()
            return None
        self._release_scene_job_ownership(scene_id, job.job_id)
        return result if isinstance(result, dict) else {}

    def _fail_claimed_job(
        self,
        job: ChapterRunJob,
        *,
        scene_id: str | None,
        error_code: str,
        error_text: str,
        author_action: dict[str, Any] | None = None,
    ) -> None:
        """claim 之后、场景执行之外的异常兜底：释放场景归属 → failed → commit。

        调用方随后会把原异常继续抛给路由（错误信封照常返回），所以这里必须自己提交，
        否则 failed 终态会跟着请求事务一起回滚，任务又回到卡死的 running。
        """

        if error_code == "RUN_OWNER_LEASE_LOST":
            # 归属已被别的 worker 接管：终态由新 owner 负责，这里再写只会被 fence 拒绝。
            return
        try:
            if scene_id:
                self._release_scene_job_ownership(scene_id, job.job_id)
            self._mark_failed(
                job,
                current_scene_id=scene_id,
                error_code=error_code,
                error_text=error_text,
                author_action=author_action,
            )
            self.session.commit()
        except Exception:
            logger.exception(
                "Failed to mark chapter run job as failed job_id=%s error_code=%s",
                job.job_id,
                error_code,
            )
            self.session.rollback()

    def run_status(self, chapter_id: str) -> dict[str, Any]:
        AuthorLifecycleService(self.session).require_active_chapter(chapter_id)
        job = self._latest_job(chapter_id)
        if job is None:
            scene_ids = self._scene_ids(chapter_id)
            return {
                "job_id": None,
                "chapter_id": chapter_id,
                "job_type": JOB_TYPE_CHAPTER_FULL,
                "status": "idle",
                "scene_ids": scene_ids,
                "current_scene_id": None,
                "completed_scene_ids": [],
                "blocked_scene_id": None,
                "latest_error": None,
                "scene_count": len(scene_ids),
                "completed_count": 0,
                "progress_pct": 0,
                "started_at": None,
                "finished_at": None,
                "source": None,
            }
        self._reconcile_job(job, self._scene_ids(chapter_id))
        self.session.flush()
        return self._serialize_job(job)

    def prepare_full_run(self, chapter_id: str) -> tuple[dict[str, Any], bool]:
        AuthorLifecycleService(self.session).require_active_chapter(chapter_id)
        scene_ids = self._scene_ids(chapter_id)
        job = self._resumeable_job(chapter_id)
        should_start_worker = False
        if job is None:
            job = self._create_job(chapter_id, scene_ids)
            should_start_worker = True
        else:
            previous_status = job.status
            # 租约已过期的 running 是没人接手的孤儿（worker 崩溃/进程重启/claim 后异常），
            # _reconcile_job 会把它放回 pending；这种情况必须重新拉起 worker，
            # 不能因为"之前是 running"就当作已有 worker 在跑。
            stale_running = self._is_stale_running(job)
            self._reconcile_job(job, scene_ids)
            self._transition_explicit_failed_retry(job)
            should_start_worker = job.status == JOB_STATUS_PENDING and (
                previous_status != JOB_STATUS_RUNNING or stale_running
            )
            if job.status == JOB_STATUS_BLOCKED:
                blocked_scene_id = self._blocked_scene_id(job, scene_ids)
                if self._chapter_gate_error(chapter_id, scene_id=blocked_scene_id) is None:
                    payload = self._payload(job)
                    payload["blocked_scene_id"] = None
                    job.payload_json = payload
                    job.status = JOB_STATUS_PENDING
                    job.error_code = None
                    job.error_text = None
                    job.finished_at = None
                    self._update_summary(job, blocked_scene_id=None, latest_error=None)
                    should_start_worker = True
        self.session.flush()
        return self._serialize_job(job), should_start_worker

    def _transition_explicit_failed_retry(self, job: ChapterRunJob) -> None:
        if job.status != JOB_STATUS_FAILED:
            return
        job.status = JOB_STATUS_PENDING
        job.finished_at = None
        job.error_code = None
        job.error_text = None
        self._update_summary(job, latest_error=None)
        self.session.flush()

    def _scene_ids(self, chapter_id: str) -> list[str]:
        scenes = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        return [scene.scene_id for scene in scenes]

    def _latest_job(self, chapter_id: str) -> ChapterRunJob | None:
        return self.session.execute(
            select(ChapterRunJob)
            .where(ChapterRunJob.chapter_id == chapter_id, ChapterRunJob.job_type == JOB_TYPE_CHAPTER_FULL)
            .order_by(ChapterRunJob.created_at.desc(), ChapterRunJob.job_id.desc())
        ).scalars().first()

    def _resumeable_job(self, chapter_id: str) -> ChapterRunJob | None:
        return self._latest_job(chapter_id)

    def _create_job(self, chapter_id: str, scene_ids: list[str]) -> ChapterRunJob:
        now = utcnow()
        job = ChapterRunJob(
            job_id=f"chapter_run_{chapter_id}_{uuid4().hex[:10]}",
            chapter_id=chapter_id,
            status=JOB_STATUS_PENDING,
            job_type=JOB_TYPE_CHAPTER_FULL,
            payload_json={
                "scene_ids": scene_ids,
                "completed_scene_ids": [],
                "current_scene_id": None,
                "blocked_scene_id": None,
            },
            result_summary_json={
                "scene_ids": scene_ids,
                "completed_scene_ids": [],
                "current_scene_id": None,
                "blocked_scene_id": None,
                "latest_error": None,
            },
            worker_id="local-process",
            attempt_no=0,
            started_at=now,
        )
        self.session.add(job)
        self.session.flush()
        return job

    @staticmethod
    def _is_stale_running(job: ChapterRunJob) -> bool:
        """running 但租约（非空）已过期：没有任何 worker 还持有它。

        与 `_claim_running` 的 CAS 判定同一口径（ISO 字符串比较）。租约为 None 的
        running 行不算过期——那是遗留/手工行，仍按"有 worker 在跑"处理。
        """

        if job.status != JOB_STATUS_RUNNING or not job.lease_expires_at:
            return False
        return job.lease_expires_at <= datetime.now(UTC).isoformat()

    def _reconcile_job(self, job: ChapterRunJob, scene_ids: list[str]) -> None:
        if self._is_stale_running(job):
            # 过期租约的 running 对外不能再报"运行中"：回到 pending 让 run-job 重新
            # 拉起 worker（_claim_running 本来就允许接管这种行，这里只是让状态与之一致）。
            job.status = JOB_STATUS_PENDING
            job.lease_expires_at = None
            job.finished_at = None
        payload = self._payload(job)
        # failed 是作者可见的终态：错误码 / author_action 必须一直保留到作者显式重试
        # （_transition_explicit_failed_retry）。归档步失败时 near-final 早已写下定稿行，
        # 若仍从场景状态反推"完成"，失败任务会被伪装成 completed / 100% 且错误被清空。
        failed = job.status == JOB_STATUS_FAILED
        finalized_scene_ids = set() if failed else self._finalized_scene_ids(scene_ids)
        completed_set = {
            scene_id
            for scene_id in payload.get("completed_scene_ids", [])
            if scene_id in scene_ids
        }
        completed = [scene_id for scene_id in scene_ids if scene_id in completed_set or scene_id in finalized_scene_ids]
        current_scene_id = payload.get("current_scene_id")
        if current_scene_id not in scene_ids:
            current_scene_id = completed[-1] if completed else None
        blocked_scene_id = payload.get("blocked_scene_id")
        if blocked_scene_id not in scene_ids:
            blocked_scene_id = None
        if (
            blocked_scene_id is not None
            and self._chapter_gate_error(job.chapter_id, scene_id=blocked_scene_id) is None
        ):
            blocked_scene_id = None
        if blocked_scene_id is not None:
            current_scene_id = blocked_scene_id
        next_scene_id = next((scene_id for scene_id in scene_ids if scene_id not in set(completed)), None)
        payload.update(
            {
                "scene_ids": scene_ids,
                "completed_scene_ids": completed,
                "blocked_scene_id": blocked_scene_id,
                "current_scene_id": current_scene_id,
            }
        )
        job.payload_json = payload
        summary = dict(job.result_summary_json or {})
        latest_error = summary.get("latest_error")
        if blocked_scene_id is None and not failed:
            if next_scene_id is None:
                latest_error = None
                job.error_code = None
                job.error_text = None
                job.status = JOB_STATUS_COMPLETED
                job.finished_at = job.finished_at or utcnow()
            elif job.status in {JOB_STATUS_BLOCKED, JOB_STATUS_COMPLETED}:
                latest_error = None
                job.error_code = None
                job.error_text = None
                job.status = JOB_STATUS_PENDING
                job.finished_at = None
        summary["scene_ids"] = scene_ids
        summary["completed_scene_ids"] = completed
        summary["blocked_scene_id"] = blocked_scene_id
        summary["current_scene_id"] = current_scene_id
        summary["latest_error"] = latest_error
        job.result_summary_json = summary

    def _finalized_scene_ids(self, scene_ids: list[str]) -> set[str]:
        """章任务可以跳过的场景：已归档且有定稿行。

        只有定稿行不算完成——near-final 在归档步之前就写下 FinalScene，归档步（章级准定稿
        评审等）失败或 worker 在这之间崩溃时，场景仍停在 soft_qc_passed 之类的中间态；
        把它当作完成会让重跑直接跳过归档步、把失败任务推导成 completed。
        """

        if not scene_ids:
            return set()
        states = self.session.execute(
            select(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids))
        ).scalars().all()
        return {
            state.scene_id
            for state in states
            if state.current_final_scene_row_id and state.scene_status == "archived"
        }

    def _claim_running(
        self,
        job: ChapterRunJob,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> ChapterRunLease:
        self.session.refresh(job)
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        running_without_active_lease = (
            job.status == JOB_STATUS_RUNNING
            and (
                job.lease_expires_at is None
                or job.lease_expires_at <= now_iso
            )
        )
        if job.status == JOB_STATUS_RUNNING and not running_without_active_lease:
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "chapter run is already owned by an active worker",
                status_code=409,
                details={"job_id": job.job_id, "worker_id": job.worker_id},
            )
        if job.status != JOB_STATUS_PENDING and not running_without_active_lease:
            raise DomainError(
                "RUN_JOB_NOT_CLAIMABLE",
                "chapter run status cannot be claimed by a worker",
                status_code=409,
                details={"job_id": job.job_id, "status": job.status},
            )
        old_status = job.status
        old_worker = job.worker_id
        old_attempt = int(job.attempt_no or 0)
        old_expiry = job.lease_expires_at
        conditions = [
            ChapterRunJob.job_id == job.job_id,
            ChapterRunJob.job_type == JOB_TYPE_CHAPTER_FULL,
            ChapterRunJob.status == old_status,
            ChapterRunJob.attempt_no == old_attempt,
            ChapterRunJob.worker_id.is_(None) if old_worker is None else ChapterRunJob.worker_id == old_worker,
            ChapterRunJob.lease_expires_at.is_(None) if old_expiry is None else ChapterRunJob.lease_expires_at == old_expiry,
        ]
        claimed = self.session.execute(
            update(ChapterRunJob)
            .where(*conditions)
            .values(
                status=JOB_STATUS_RUNNING,
                worker_id=worker_id,
                attempt_no=old_attempt + 1,
                started_at=job.started_at or now_iso,
                heartbeat_at=now_iso,
                lease_expires_at=expires,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            raise DomainError("RUN_JOB_IN_PROGRESS", "another worker won the chapter run claim", status_code=409)
        self.session.flush()
        self.session.refresh(job)
        self._update_summary(job, latest_error=None)
        self.session.flush()
        return ChapterRunLease(
            job_id=job.job_id,
            worker_id=worker_id,
            attempt_no=old_attempt + 1,
            lease_expires_at=expires,
            _service=self,
        )

    def _renew_lease(self, owner: ChapterRunLease, *, lease_seconds: int) -> str:
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        renewed = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.job_type == JOB_TYPE_CHAPTER_FULL,
                ChapterRunJob.status == JOB_STATUS_RUNNING,
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
            .values(heartbeat_at=now.isoformat(), lease_expires_at=expires)
            .execution_options(synchronize_session=False)
        )
        if renewed.rowcount != 1:
            self.session.rollback()
            raise DomainError("RUN_OWNER_LEASE_LOST", "chapter run owner lease was lost", status_code=409)
        self.session.flush()
        return expires

    def _fence_active_owner(self, job: ChapterRunJob) -> None:
        owner = self._active_owner
        if owner is None:
            return
        self.session.flush()
        fenced = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.status == JOB_STATUS_RUNNING,
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
            .values(heartbeat_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if fenced.rowcount != 1:
            self.session.rollback()
            raise DomainError("RUN_OWNER_LEASE_LOST", "chapter run owner was replaced", status_code=409)
        self.session.flush()
        self.session.refresh(job)

    def _set_current_scene(self, job: ChapterRunJob, scene_id: str) -> None:
        self._fence_active_owner(job)
        payload = self._payload(job)
        payload["current_scene_id"] = scene_id
        payload["blocked_scene_id"] = None
        job.payload_json = payload
        self._update_summary(job, current_scene_id=scene_id, blocked_scene_id=None, latest_error=None)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            # v2 目录（React 章节编排）建的场景只有 SceneCard 没有运行时状态行；
            # v1 scenes POST / orchestrator / scene_run_jobs 都按同一初始约定惰性补建，
            # 这里不能再当作 SCENE_NOT_FOUND 拒绝，否则整章一起步就失败。
            if self.session.get(SceneCard, scene_id) is None:
                raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()
        if state.active_run_job_id not in {None, job.job_id}:
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "scene is owned by another active run job",
                status_code=409,
            )
        # 归属必须在把场景交给 Orchestrator 之前落库，而不是只改 ORM 属性：
        # run_scene → SceneRunCheckpointService.acquire_execution 会 session.refresh(state)，
        # SQLAlchemy 先失效实例再自动 flush，未 flush 的赋值会被数据库里的旧值（None）覆盖。
        # 场景级节点容忍 active_run_job_id 为 None，所以整条场景管线照常跑完；只有章级节点
        # （章尾归档步的 chapter_near_final_review）要求它等于本章任务 id，于是在记账台账
        # 落行之前被拒（LLM_ACCOUNTING_CONTEXT_INVALID），对外表现为 RUN_CHECKPOINT_OUTPUT_MISSING。
        # 与 scene_run_jobs.claim_scene_active_job 同口径：CAS 语句 + flush，再刷新 ORM。
        claimed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                or_(
                    SceneRunState.active_run_job_id.is_(None),
                    SceneRunState.active_run_job_id == job.job_id,
                ),
            )
            .values(active_run_job_id=job.job_id)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "scene is owned by another active run job",
                status_code=409,
            )
        self.session.flush()
        self.session.refresh(state)

    def _release_scene_job_ownership(self, scene_id: str, job_id: str) -> None:
        self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_run_job_id == job_id,
            )
            .values(active_run_job_id=None)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()

    def _mark_scene_completed(self, job: ChapterRunJob, scene_id: str) -> None:
        self._fence_active_owner(job)
        payload = self._payload(job)
        completed_scene_ids = list(payload.get("completed_scene_ids", []))
        if scene_id not in completed_scene_ids:
            completed_scene_ids.append(scene_id)
        payload["completed_scene_ids"] = completed_scene_ids
        payload["current_scene_id"] = scene_id
        payload["blocked_scene_id"] = None
        job.payload_json = payload
        self._update_summary(
            job,
            current_scene_id=scene_id,
            completed_scene_ids=completed_scene_ids,
            blocked_scene_id=None,
            latest_error=None,
            source="llm",
        )

    def _mark_blocked(self, job: ChapterRunJob, *, blocked_scene_id: str | None, latest_error: dict[str, Any]) -> None:
        self._fence_active_owner(job)
        job.status = JOB_STATUS_BLOCKED
        job.error_code = latest_error["code"]
        job.error_text = latest_error["message"]
        job.finished_at = None
        payload = self._payload(job)
        payload["blocked_scene_id"] = blocked_scene_id
        if blocked_scene_id is not None:
            payload["current_scene_id"] = blocked_scene_id
        job.payload_json = payload
        self._update_summary(
            job,
            current_scene_id=payload.get("current_scene_id"),
            blocked_scene_id=blocked_scene_id,
            latest_error=latest_error,
        )

    def _mark_completed(self, job: ChapterRunJob) -> None:
        self._fence_active_owner(job)
        payload = self._payload(job)
        completed_scene_ids = list(payload.get("completed_scene_ids", []))
        job.status = JOB_STATUS_COMPLETED
        job.error_code = None
        job.error_text = None
        job.finished_at = utcnow()
        payload["blocked_scene_id"] = None
        payload["current_scene_id"] = completed_scene_ids[-1] if completed_scene_ids else None
        job.payload_json = payload
        self._update_summary(
            job,
            current_scene_id=payload.get("current_scene_id"),
            completed_scene_ids=completed_scene_ids,
            blocked_scene_id=None,
            latest_error=None,
        )

    def _mark_failed(
        self,
        job: ChapterRunJob,
        *,
        current_scene_id: str | None,
        error_code: str,
        error_text: str,
        author_action: dict[str, Any] | None = None,
    ) -> None:
        self._fence_active_owner(job)
        job.status = JOB_STATUS_FAILED
        job.error_code = error_code
        job.error_text = error_text
        job.finished_at = utcnow()
        payload = self._payload(job)
        payload["current_scene_id"] = current_scene_id
        job.payload_json = payload
        latest_error: dict[str, Any] = {"code": error_code, "message": error_text}
        if author_action:
            # 与 blocked 的 latest_error 同形：作者在 run-status 里直接看到该去哪、做什么。
            latest_error["author_action"] = author_action
        self._update_summary(
            job,
            current_scene_id=current_scene_id,
            blocked_scene_id=None,
            latest_error=latest_error,
        )

    @staticmethod
    def _domain_error_author_action(exc: BaseException) -> dict[str, Any] | None:
        if not isinstance(exc, DomainError) or not isinstance(exc.details, dict):
            return None
        action = exc.details.get("author_action")
        return dict(action) if isinstance(action, dict) else None

    def _chapter_gate_error(self, chapter_id: str, *, scene_id: str | None = None) -> dict[str, Any] | None:
        human_review_error = self._scene_human_review_error(scene_id)
        if human_review_error is not None:
            return human_review_error
        return None

    def _scene_human_review_error(self, scene_id: str | None) -> dict[str, Any] | None:
        if not scene_id:
            return None
        scene_state = self.session.get(SceneRunState, scene_id)
        if scene_state is None or not scene_state.current_human_review_event_id:
            return None
        event = self.session.get(HumanReviewEvent, scene_state.current_human_review_event_id)
        if event is None or event.status != "resolved":
            return self._human_review_error(scene_id, {"current_human_review_event_id": scene_state.current_human_review_event_id})
        return None

    def _next_scene_id(self, job: ChapterRunJob, scene_ids: list[str]) -> str | None:
        completed = set(self._payload(job).get("completed_scene_ids", []))
        for scene_id in scene_ids:
            if scene_id not in completed:
                return scene_id
        return None

    def _blocked_scene_id(self, job: ChapterRunJob, scene_ids: list[str]) -> str | None:
        payload = self._payload(job)
        blocked_scene_id = payload.get("blocked_scene_id")
        if blocked_scene_id in scene_ids:
            return blocked_scene_id
        current_scene_id = payload.get("current_scene_id")
        if current_scene_id in scene_ids:
            return current_scene_id
        return self._next_scene_id(job, scene_ids)

    @staticmethod
    def _scene_requires_human_review(result: dict[str, Any] | None) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("scene_status") == "human_review_required":
            return True
        return bool(result.get("current_human_review_event_id"))

    def _human_review_error(self, scene_id: str, result: dict[str, Any] | None) -> dict[str, Any]:
        event_id = str((result or {}).get("current_human_review_event_id") or "").strip()
        if not event_id:
            state = self.session.get(SceneRunState, scene_id)
            event_id = str(state.current_human_review_event_id or "").strip() if state is not None else ""
        target_ref = f"human_review_event:{event_id}" if event_id else f"scene_card:{scene_id}"
        evidence = [f"场景：{scene_id}"]
        if event_id:
            evidence.append(f"审核：{event_id}")
        return {
            "code": "CHAPTER_RUN_HUMAN_REVIEW_REQUIRED",
            "message": "scene requires human review before chapter run can continue",
            "author_action": author_action(
                "场景需要人工审核",
                "当前场景有一条待处理审核，处理完后章节起草会从这里继续。",
                target_view="review",
                target_ref=target_ref,
                primary_button_label="去待处理建议",
                evidence_summary=evidence,
            ),
        }

    def _scene_incomplete_error(self, scene_id: str, result: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(result, dict):
            return self._generic_scene_incomplete_error(scene_id)
        if result.get("current_final_scene_row_id"):
            return None
        state = self.session.get(SceneRunState, scene_id)
        if state is not None and state.current_final_scene_row_id:
            return None
        error_code = str(result.get("error_code") or result.get("code") or "").strip()
        scene_status = str(result.get("scene_status") or (state.scene_status if state is not None else "") or "").strip()
        if error_code == "LLM_ROUTE_NOT_CONFIGURED":
            return {
                "code": "LLM_ROUTE_NOT_CONFIGURED",
                "message": "model route is not configured for scene generation",
                "author_action": author_action(
                    "需要配置模型路由",
                    "当前场景起草找不到可用的模型路由。请先到系统配置完成 provider、模型和路由设置。",
                    target_view="config",
                    target_ref="system_config:llm",
                    primary_button_label="去系统配置",
                    evidence_summary=[f"场景：{scene_id}", "错误：LLM_ROUTE_NOT_CONFIGURED"],
                ),
            }
        if error_code == "CONTINUITY_BUDGET_EXCEEDED":
            return {
                "code": "CONTINUITY_BUDGET_EXCEEDED",
                "message": "continuity context budget was exceeded",
                "author_action": author_action(
                    "上下文太重，需要拆分",
                    "这一场承载的信息过多，建议拆分场景或降低连续性上下文后再跑。",
                    target_view="snowflake-workbench",
                    target_ref=f"scene_card:{scene_id}",
                    primary_button_label="拆分场景",
                    evidence_summary=[f"场景：{scene_id}", "错误：CONTINUITY_BUDGET_EXCEEDED"],
                ),
            }
        if scene_status in {"hard_qc_partial_rewrite_required", "hard_qc_full_rewrite_required", "near_final_revision_required"}:
            return {
                "code": "CHAPTER_RUN_SCENE_NEEDS_REWRITE",
                "message": "当前场景停在硬质检返修，还没有形成可审阅终稿。",
                "author_action": author_action(
                    "场景需要补修",
                    "这一场没有形成可审阅终稿，请先回到场景工作台处理返修，再继续章节起草。",
                    target_view="workbench",
                    target_ref=f"scene_card:{scene_id}",
                    primary_button_label="去场景工作台",
                    evidence_summary=[f"场景：{scene_id}", f"状态：{scene_status}"],
                ),
            }
        return self._generic_scene_incomplete_error(scene_id)

    @staticmethod
    def _generic_scene_incomplete_error(scene_id: str) -> dict[str, Any]:
        return {
            "code": "CHAPTER_RUN_SCENE_INCOMPLETE",
            "message": "scene run did not produce a final scene",
            "author_action": author_action(
                "场景还没有可审阅正文",
                "当前场景没有生成可审阅终稿。请先回到场景工作台检查缺字段、返修或运行失败原因。",
                target_view="workbench",
                target_ref=f"scene_card:{scene_id}",
                primary_button_label="去场景工作台",
                evidence_summary=[f"场景：{scene_id}"],
            ),
        }

    @staticmethod
    def _payload(job: ChapterRunJob) -> dict[str, Any]:
        return dict(job.payload_json or {})

    def _update_summary(self, job: ChapterRunJob, **updates: Any) -> None:
        summary = {
            "scene_ids": self._payload(job).get("scene_ids", []),
            "completed_scene_ids": self._payload(job).get("completed_scene_ids", []),
            "current_scene_id": self._payload(job).get("current_scene_id"),
            "blocked_scene_id": self._payload(job).get("blocked_scene_id"),
            "latest_error": None,
            **dict(job.result_summary_json or {}),
        }
        summary.update({key: value for key, value in updates.items() if key is not None})
        job.result_summary_json = summary

    def _serialize_job(self, job: ChapterRunJob) -> dict[str, Any]:
        summary = dict(job.result_summary_json or {})
        payload = self._payload(job)
        scene_ids = summary.get("scene_ids") or payload.get("scene_ids", [])
        completed_scene_ids = summary.get("completed_scene_ids") or []
        scene_count = len(scene_ids)
        completed_count = len(completed_scene_ids)
        progress_pct = 100 if scene_count == 0 and job.status == JOB_STATUS_COMPLETED else 0
        if scene_count:
            progress_pct = min(100, round((completed_count / scene_count) * 100))
        return {
            "job_id": job.job_id,
            "chapter_id": job.chapter_id,
            "job_type": job.job_type,
            "status": job.status,
            "scene_ids": scene_ids,
            "current_scene_id": summary.get("current_scene_id"),
            "completed_scene_ids": completed_scene_ids,
            "blocked_scene_id": summary.get("blocked_scene_id"),
            "latest_error": summary.get("latest_error"),
            "scene_count": scene_count,
            "completed_count": completed_count,
            "progress_pct": progress_pct,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            # `source` 是已经实际产出结果的来源，不是请求意图。排队、预检阻塞、
            # 首次模型调用即失败等路径都必须保持为空，不能伪装成 LLM 成功。
            "source": summary.get("source") or payload.get("source"),
        }
