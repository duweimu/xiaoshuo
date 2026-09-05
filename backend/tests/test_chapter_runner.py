from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    HumanReviewEvent,
    SceneCard,
    SceneRunState,
)
from novel_system.db.session import SessionLocal
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService


def _create_chapter(client, chapter_id: str) -> None:
    response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": chapter_id,
            "planned_scene_count": 3,
            "chapter_goal": f"goal {chapter_id}",
            "main_plot_push": f"push {chapter_id}",
            "emotional_target": f"emotion {chapter_id}",
            "ending_effect": f"ending {chapter_id}",
        },
        headers={"X-Idempotency-Key": f"create-chapter-{chapter_id}"},
    )
    assert response.status_code == 200


def _create_scene(client, chapter_id: str, scene_id: str, scene_seq: int, *, is_chapter_last: int = 0) -> None:
    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "scene_seq": scene_seq,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A"],
            "location": f"location {scene_id}",
            "scene_goal": f"goal {scene_id}",
            "beats_json": [f"beat {scene_id}"],
            "must_include_text": f"must {scene_id}",
            "target_length_band": "short",
            "scene_type": "bridge",
            "is_chapter_last": is_chapter_last,
        },
        headers={"X-Idempotency-Key": f"create-scene-{scene_id}"},
    )
    assert response.status_code == 200


def _add_job_parent(session, chapter_id: str) -> None:
    session.add(ChapterGoal(chapter_id=chapter_id, chapter_goal=f"goal {chapter_id}"))
    session.flush()


def _install_fake_runner(monkeypatch, *, blocked_scene: str | None = None, block_kind: str | None = None):
    shared = {
        "calls": [],
        "execution_contexts": [],
        "gate": {
            "chapter_id": "CH900",
            "chapter_passed_scene_count": 0,
            "chapter_backfill_pending_count": 0,
            "mid_aggregate_enabled_effective": 0,
            "aggregate_block_reason": "none",
            "manual_hold_reason": None,
            "last_interim_memory_row_id": None,
            "last_final_memory_row_id": None,
            "staged_backfill_items": [],
        },
    }

    class FakeOrchestrator:
        def __init__(self, session) -> None:
            self.session = session

        def run_scene(
            self,
            scene_id: str,
            *,
            execution_id: str | None = None,
            lease_renewer=None,
        ) -> dict:
            shared["calls"].append(scene_id)
            shared["execution_contexts"].append(
                {"execution_id": execution_id, "has_lease_renewer": callable(lease_renewer)}
            )
            state = self.session.get(SceneRunState, scene_id)
            assert state is not None
            if blocked_scene == scene_id and block_kind == "human_review":
                state.scene_status = "human_review_required"
                state.current_human_review_event_id = f"review_{scene_id}"
                self.session.flush()
                return {
                    "scene_status": "human_review_required",
                    "current_human_review_event_id": state.current_human_review_event_id,
                }
            if blocked_scene == scene_id and block_kind == "partial_rewrite":
                state.scene_status = "hard_qc_partial_rewrite_required"
                state.current_human_review_event_id = None
                state.current_final_scene_row_id = None
                self.session.flush()
                return {
                    "scene_status": "hard_qc_partial_rewrite_required",
                    "current_human_review_event_id": None,
                    "current_final_scene_row_id": None,
                }

            state.scene_status = "archived"
            state.current_human_review_event_id = None
            state.current_final_scene_row_id = f"final_scene_{scene_id}"
            self.session.flush()

            if blocked_scene == scene_id and block_kind == "backfill":
                shared["gate"] = {
                    **shared["gate"],
                    "chapter_backfill_pending_count": 1,
                    "aggregate_block_reason": "blocked_waiting_backfill",
                    "staged_backfill_items": [
                        {
                            "stage_id": f"stage_{scene_id}",
                            "chapter_id": "CH900",
                            "scene_id": scene_id,
                            "marker_id": "F001",
                            "marker_text": "marker text",
                            "marker_token": '{{backfill id=F001 text="marker text"}}',
                            "status": "pending",
                            "linked_tracker_row_id": None,
                            "last_strategy": None,
                        }
                    ],
                }
            return {
                "scene_status": "archived",
                "current_human_review_event_id": None,
                "current_final_scene_row_id": state.current_final_scene_row_id,
            }

    class FakeChapterRuntimeService:
        def __init__(self, session) -> None:
            self.session = session

        def chapter_state_payload(self, chapter_id: str) -> dict:
            return {**shared["gate"], "chapter_id": chapter_id}

    monkeypatch.setattr("novel_system.services.chapter_runner.Orchestrator", FakeOrchestrator)
    return shared


def test_chapter_job_detached_renewal_is_visible_to_other_sessions(session) -> None:
    _add_job_parent(session, "CH_RENEW")
    job = ChapterRunJob(
        job_id="chapter-renew-detached",
        chapter_id="CH_RENEW",
        status="pending",
        job_type="chapter_run_full",
        payload_json={"scene_ids": [], "completed_scene_ids": []},
        result_summary_json={"scene_ids": [], "completed_scene_ids": []},
        worker_id="local-process",
        attempt_no=0,
    )
    session.add(job)
    session.commit()
    owner = ChapterRunnerService(session)._claim_running(
        job,
        worker_id="worker-a",
        lease_seconds=1,
    )
    session.commit()
    before = owner.lease_expires_at

    renewed = owner.renew_detached(lease_seconds=120)

    with SessionLocal() as observer:
        persisted = observer.get(ChapterRunJob, "chapter-renew-detached")
        assert persisted is not None
        assert persisted.lease_expires_at == renewed
        assert persisted.lease_expires_at > before


def test_chapter_run_full_executes_scenes_in_order_and_reports_completed_status(client, session, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2)
    _create_scene(client, "CH900", "CH900_SC03", 3, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch)

    response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-complete"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "completed"
    assert data["current_scene_id"] == "CH900_SC03"
    assert data["completed_scene_ids"] == ["CH900_SC01", "CH900_SC02", "CH900_SC03"]
    assert data["blocked_scene_id"] is None
    assert data["latest_error"] is None
    assert shared["calls"] == ["CH900_SC01", "CH900_SC02", "CH900_SC03"]

    status_response = client.get("/api/v1/chapters/CH900/run-status")
    assert status_response.status_code == 200
    status_payload = status_response.json()["data"]
    assert status_payload["status"] == "completed"
    assert status_payload["scene_count"] == 3
    assert status_payload["completed_count"] == 3
    assert status_payload["progress_pct"] == 100
    assert status_payload["started_at"]
    assert status_payload["finished_at"]

    job = session.query(ChapterRunJob).filter_by(chapter_id="CH900").one()
    assert job.status == "completed"
    assert job.payload_json["completed_scene_ids"] == ["CH900_SC01", "CH900_SC02", "CH900_SC03"]
    assert shared["execution_contexts"] == [
        {"execution_id": f"{job.job_id}:CH900_SC01", "has_lease_renewer": True},
        {"execution_id": f"{job.job_id}:CH900_SC02", "has_lease_renewer": True},
        {"execution_id": f"{job.job_id}:CH900_SC03", "has_lease_renewer": True},
    ]


def test_chapter_job_owner_cas_reclaim_renewal_and_terminal_fence(session) -> None:
    _add_job_parent(session, "CH_OWNER")
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    session.add(
        ChapterRunJob(
            job_id="chapter-owner-cas",
            chapter_id="CH_OWNER",
            status="running",
            job_type="chapter_run_full",
            payload_json={"scene_ids": [], "completed_scene_ids": []},
            result_summary_json={"scene_ids": [], "completed_scene_ids": []},
            worker_id="dead-worker",
            attempt_no=1,
            heartbeat_at=expired,
            lease_expires_at=expired,
        )
    )
    session.commit()

    stale_session = SessionLocal()
    winner_session = SessionLocal()
    contender_session = SessionLocal()
    try:
        stale_service = ChapterRunnerService(stale_session)
        stale_job = stale_session.get(ChapterRunJob, "chapter-owner-cas")
        assert stale_job is not None
        stale_owner = stale_service._claim_running(
            stale_job,
            worker_id="worker-a",
            lease_seconds=1,
        )
        stale_service._active_owner = stale_owner
        stale_session.commit()

        before_renewal = stale_owner.lease_expires_at
        renewed = stale_owner.renew(lease_seconds=30)
        assert renewed > before_renewal
        stale_session.commit()

        # Simulate expiry, then prove exactly one later owner can reclaim.
        stale_job.lease_expires_at = expired
        stale_session.commit()
        winner_service = ChapterRunnerService(winner_session)
        winner_job = winner_session.get(ChapterRunJob, "chapter-owner-cas")
        assert winner_job is not None
        winner_owner = winner_service._claim_running(
            winner_job,
            worker_id="worker-b",
            lease_seconds=30,
        )
        winner_service._active_owner = winner_owner
        winner_session.commit()

        contender_job = contender_session.get(ChapterRunJob, "chapter-owner-cas")
        assert contender_job is not None
        with pytest.raises(DomainError) as active_loser:
            ChapterRunnerService(contender_session)._claim_running(
                contender_job,
                worker_id="worker-c",
                lease_seconds=30,
            )
        assert active_loser.value.code == "RUN_JOB_IN_PROGRESS"

        with pytest.raises(DomainError) as stale_renewal:
            stale_owner.renew(lease_seconds=30)
        assert stale_renewal.value.code == "RUN_OWNER_LEASE_LOST"

        # A superseded owner cannot perform the terminal write.
        with pytest.raises(DomainError) as stale_terminal:
            stale_service._mark_completed(stale_job)
        assert stale_terminal.value.code == "RUN_OWNER_LEASE_LOST"

        winner_service._mark_completed(winner_job)
        winner_session.commit()
        contender_session.expire_all()
        persisted = contender_session.get(ChapterRunJob, "chapter-owner-cas")
        assert persisted is not None
        assert persisted.status == "completed"
        assert persisted.worker_id == "worker-b"
        assert persisted.attempt_no == 3
    finally:
        stale_session.close()
        winner_session.close()
        contender_session.close()


@pytest.mark.parametrize("terminal_status", ["completed", "blocked", "failed"])
def test_terminal_chapter_job_claim_is_rejected_without_mutation(
    session,
    terminal_status: str,
) -> None:
    _add_job_parent(session, "CH_TERMINAL")
    job = ChapterRunJob(
        job_id=f"chapter-terminal-{terminal_status}",
        chapter_id="CH_TERMINAL",
        status=terminal_status,
        job_type="chapter_run_full",
        payload_json={"scene_ids": [], "completed_scene_ids": [], "current_step": terminal_status},
        result_summary_json={"scene_ids": [], "completed_scene_ids": [], "current_step": terminal_status},
        worker_id="terminal-worker",
        attempt_no=3,
        heartbeat_at="2026-07-10T01:02:03+00:00",
        lease_expires_at="2026-07-10T01:03:03+00:00",
        started_at="2026-07-10T01:00:00+00:00",
        finished_at="2026-07-10T01:02:03+00:00",
        error_code="TERMINAL_ERROR" if terminal_status != "completed" else None,
        error_text="terminal" if terminal_status != "completed" else None,
    )
    session.add(job)
    session.commit()
    before = {
        "status": job.status,
        "worker_id": job.worker_id,
        "attempt_no": job.attempt_no,
        "heartbeat_at": job.heartbeat_at,
        "lease_expires_at": job.lease_expires_at,
        "finished_at": job.finished_at,
        "error_code": job.error_code,
        "error_text": job.error_text,
        "payload_json": dict(job.payload_json or {}),
        "result_summary_json": dict(job.result_summary_json or {}),
    }

    with pytest.raises(DomainError) as rejected:
        ChapterRunnerService(session)._claim_running(
            job,
            worker_id="duplicate-worker",
            lease_seconds=30,
        )

    assert rejected.value.code == "RUN_JOB_NOT_CLAIMABLE"
    session.expire_all()
    unchanged = session.get(ChapterRunJob, job.job_id)
    assert unchanged is not None
    assert {
        "status": unchanged.status,
        "worker_id": unchanged.worker_id,
        "attempt_no": unchanged.attempt_no,
        "heartbeat_at": unchanged.heartbeat_at,
        "lease_expires_at": unchanged.lease_expires_at,
        "finished_at": unchanged.finished_at,
        "error_code": unchanged.error_code,
        "error_text": unchanged.error_text,
        "payload_json": dict(unchanged.payload_json or {}),
        "result_summary_json": dict(unchanged.result_summary_json or {}),
    } == before


def test_running_chapter_job_without_lease_is_reclaimable_by_cas(session) -> None:
    _add_job_parent(session, "CH_RUNNING")
    job = ChapterRunJob(
        job_id="chapter-running-no-lease",
        chapter_id="CH_RUNNING",
        status="running",
        job_type="chapter_run_full",
        payload_json={"scene_ids": [], "completed_scene_ids": []},
        result_summary_json={"scene_ids": [], "completed_scene_ids": []},
        worker_id="worker-with-missing-lease",
        attempt_no=2,
        heartbeat_at="2026-07-10T01:02:03+00:00",
        lease_expires_at=None,
    )
    session.add(job)
    session.commit()

    owner = ChapterRunnerService(session)._claim_running(
        job,
        worker_id="reclaimer",
        lease_seconds=30,
    )
    session.commit()

    assert owner.worker_id == "reclaimer"
    assert owner.attempt_no == 3
    session.expire_all()
    reclaimed = session.get(ChapterRunJob, job.job_id)
    assert reclaimed is not None
    assert reclaimed.status == "running"
    assert reclaimed.worker_id == "reclaimer"
    assert reclaimed.attempt_no == 3
    assert reclaimed.lease_expires_at is not None


def test_chapter_retry_reuses_scene_execution_checkpoint_without_recharging(
    client,
    session,
    monkeypatch,
) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1, is_chapter_last=1)
    _install_fake_runner(monkeypatch)
    observed_execution_ids: list[str] = []
    provider_dispatches = 0

    class _CheckpointingOrchestrator:
        def __init__(self, worker_session) -> None:
            self.session = worker_session

        def run_scene(self, scene_id: str, *, execution_id=None, lease_renewer=None) -> dict:
            nonlocal provider_dispatches
            observed_execution_ids.append(execution_id)
            checkpoints = SceneRunCheckpointService(self.session)
            claim = checkpoints.acquire_execution(scene_id, execution_id)
            state = self.session.get(SceneRunState, scene_id)
            assert state is not None
            if claim.last_node is None:
                provider_dispatches += 1
                state.scene_tokens_used = 21
                state.scene_tokens_reserved = 3
                state.provider_attempts_used = 1
                self.session.flush()
                checkpoints.save_checkpoint(
                    scene_id=scene_id,
                    execution_id=execution_id,
                    node_key="budget_ready",
                    artifact_refs={"provider_output": "durable"},
                )
                checkpoints.mark_failed(scene_id, execution_id)
                self.session.commit()
                raise RuntimeError("fail after durable chapter-scene checkpoint")
            assert claim.last_node == "budget_ready"
            state.scene_status = "archived"
            state.current_final_scene_row_id = f"final_{scene_id}"
            return {
                "scene_status": "archived",
                "current_final_scene_row_id": state.current_final_scene_row_id,
            }

    monkeypatch.setattr("novel_system.services.chapter_runner.Orchestrator", _CheckpointingOrchestrator)
    failed = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-checkpoint-fail"},
    )
    assert failed.status_code == 200
    assert failed.json()["data"]["status"] == "failed"
    assert "fail after durable chapter-scene checkpoint" not in failed.text
    job_id = failed.json()["data"]["job_id"]

    resumed = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-checkpoint-resume"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["data"]["status"] == "completed"
    assert resumed.json()["data"]["job_id"] == job_id

    session.expire_all()
    state = session.get(SceneRunState, "CH900_SC01")
    assert observed_execution_ids == [f"{job_id}:CH900_SC01", f"{job_id}:CH900_SC01"]
    assert provider_dispatches == 1
    assert state.run_checkpoint == "budget_ready"
    assert state.scene_tokens_used == 21
    assert state.scene_tokens_reserved == 3
    assert state.provider_attempts_used == 1


def test_chapter_run_full_blocks_on_human_review_and_resume_retries_blocked_scene(client, session, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch, blocked_scene="CH900_SC01", block_kind="human_review")

    blocked_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-human-review"},
    )

    assert blocked_response.status_code == 200
    blocked = blocked_response.json()["data"]
    assert blocked["status"] == "blocked"
    assert blocked["current_scene_id"] == "CH900_SC01"
    assert blocked["completed_scene_ids"] == []
    assert blocked["blocked_scene_id"] == "CH900_SC01"
    assert blocked["latest_error"] == {
        "code": "CHAPTER_RUN_HUMAN_REVIEW_REQUIRED",
        "message": "scene requires human review before chapter run can continue",
        "author_action": {
            "title": "场景需要人工审核",
            "message": "当前场景有一条待处理审核，处理完后章节起草会从这里继续。",
            "target_view": "review",
            "target_ref": "human_review_event:review_CH900_SC01",
            "primary_button_label": "去待处理建议",
            "evidence_summary": ["场景：CH900_SC01", "审核：review_CH900_SC01"],
        },
    }
    session.add(
        HumanReviewEvent(
            event_id="review_CH900_SC01",
            scene_id="CH900_SC01",
            chapter_id="CH900",
            object_ref="scene_card:CH900_SC01",
            event_source="scene_generation",
            priority="high",
            status="resolved",
            allowed_actions_json=["inspect"],
            result_status_map_json={"inspect": "needs_followup"},
            details_json={},
            default_action="inspect",
        )
    )
    session.commit()

    shared["gate"] = {
        **shared["gate"],
        "aggregate_block_reason": "none",
        "chapter_backfill_pending_count": 0,
        "staged_backfill_items": [],
    }
    shared["calls"].clear()
    _install_fake_runner(monkeypatch)

    resumed_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-human-review-resume"},
    )

    assert resumed_response.status_code == 200
    resumed = resumed_response.json()["data"]
    assert resumed["status"] == "completed"
    assert resumed["completed_scene_ids"] == ["CH900_SC01", "CH900_SC02"]


def test_chapter_run_full_blocks_when_scene_finishes_without_final_scene(client, session, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch, blocked_scene="CH900_SC01", block_kind="partial_rewrite")

    response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-partial-rewrite-block"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "blocked"
    assert data["current_scene_id"] == "CH900_SC01"
    assert data["completed_scene_ids"] == []
    assert data["blocked_scene_id"] == "CH900_SC01"
    assert data["latest_error"] == {
        "code": "CHAPTER_RUN_SCENE_NEEDS_REWRITE",
        "message": "当前场景停在硬质检返修，还没有形成可审阅终稿。",
        "author_action": {
            "title": "场景需要补修",
            "message": "这一场没有形成可审阅终稿，请先回到场景工作台处理返修，再继续章节起草。",
            "target_view": "workbench",
            "target_ref": "scene_card:CH900_SC01",
            "primary_button_label": "去场景工作台",
            "evidence_summary": ["场景：CH900_SC01", "状态：hard_qc_partial_rewrite_required"],
        },
    }
    assert shared["calls"] == ["CH900_SC01"]


def test_chapter_run_full_stays_blocked_until_human_review_resolves(client, session, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch, blocked_scene="CH900_SC01", block_kind="human_review")

    blocked_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-human-review-stays-blocked"},
    )

    assert blocked_response.status_code == 200
    blocked = blocked_response.json()["data"]
    assert blocked["status"] == "blocked"
    assert blocked["blocked_scene_id"] == "CH900_SC01"
    session.add(
        HumanReviewEvent(
            event_id="review_CH900_SC01",
            scene_id="CH900_SC01",
            chapter_id="CH900",
            object_ref="scene_card:CH900_SC01",
            event_source="scene_generation",
            priority="high",
            status="needs_followup",
            allowed_actions_json=["inspect"],
            result_status_map_json={"inspect": "needs_followup"},
            details_json={},
            default_action="inspect",
        )
    )
    session.commit()

    shared["calls"].clear()
    _install_fake_runner(monkeypatch)

    resumed_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-human-review-unresolved"},
    )

    assert resumed_response.status_code == 200
    resumed = resumed_response.json()["data"]
    assert resumed["status"] == "blocked"
    assert resumed["blocked_scene_id"] == "CH900_SC01"
    assert resumed["latest_error"] == {
        "code": "CHAPTER_RUN_HUMAN_REVIEW_REQUIRED",
        "message": "scene requires human review before chapter run can continue",
        "author_action": {
            "title": "场景需要人工审核",
            "message": "当前场景有一条待处理审核，处理完后章节起草会从这里继续。",
            "target_view": "review",
            "target_ref": "human_review_event:review_CH900_SC01",
            "primary_button_label": "去待处理建议",
            "evidence_summary": ["场景：CH900_SC01", "审核：review_CH900_SC01"],
        },
    }
    assert resumed["completed_scene_ids"] == []
    assert shared["calls"] == []

    review_event = session.get(HumanReviewEvent, "review_CH900_SC01")
    assert review_event is not None
    review_event.status = "resolved"
    session.commit()

    resumed_after_resolution = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-human-review-resolved"},
    )

    assert resumed_after_resolution.status_code == 200
    completed = resumed_after_resolution.json()["data"]
    assert completed["status"] == "completed"
    assert completed["completed_scene_ids"] == ["CH900_SC01", "CH900_SC02"]


def test_prepare_full_run_restarts_resolved_blocked_job(client, session, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    _install_fake_runner(monkeypatch, blocked_scene="CH900_SC01", block_kind="human_review")

    blocked_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-prepare-blocked"},
    )
    assert blocked_response.status_code == 200
    assert blocked_response.json()["data"]["status"] == "blocked"
    session.add(
        HumanReviewEvent(
            event_id="review_CH900_SC01",
            scene_id="CH900_SC01",
            chapter_id="CH900",
            object_ref="scene_card:CH900_SC01",
            event_source="scene_generation",
            priority="high",
            status="resolved",
            allowed_actions_json=["inspect"],
            result_status_map_json={"inspect": "resolved"},
            details_json={},
            default_action="inspect",
        )
    )
    session.commit()

    prepared, should_start = ChapterRunnerService(session).prepare_full_run("CH900")

    assert should_start is True
    assert prepared["status"] == "pending"
    assert prepared["blocked_scene_id"] is None
    assert prepared["latest_error"] is None


def test_chapter_run_full_reuses_completed_job_progress_when_new_scene_is_added(client, monkeypatch) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch)

    first_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-completed-progress-initial"},
    )

    assert first_response.status_code == 200
    first_run = first_response.json()["data"]
    assert first_run["status"] == "completed"
    assert shared["calls"] == ["CH900_SC01", "CH900_SC02"]

    _create_scene(client, "CH900", "CH900_SC03", 3, is_chapter_last=1)
    shared = _install_fake_runner(monkeypatch)

    resumed_response = client.post(
        "/api/v1/chapters/CH900/run/full",
        headers={"X-Idempotency-Key": "chapter-run-completed-progress-resume"},
    )

    assert resumed_response.status_code == 200
    resumed = resumed_response.json()["data"]
    assert resumed["job_id"] == first_run["job_id"]
    assert resumed["status"] == "completed"
    assert resumed["completed_scene_ids"] == ["CH900_SC01", "CH900_SC02", "CH900_SC03"]
    assert shared["calls"] == ["CH900_SC03"]


def test_chapter_run_status_preserves_failed_job_visibility(client, session) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1)
    _create_scene(client, "CH900", "CH900_SC02", 2, is_chapter_last=1)
    session.add(
        ChapterRunJob(
            job_id="chapter_run_CH900_failed",
            chapter_id="CH900",
            status="failed",
            job_type="chapter_run_full",
            payload_json={
                "scene_ids": ["CH900_SC01", "CH900_SC02"],
                "completed_scene_ids": ["CH900_SC01"],
                "current_scene_id": "CH900_SC02",
                "blocked_scene_id": None,
            },
            result_summary_json={
                "scene_ids": ["CH900_SC01", "CH900_SC02"],
                "completed_scene_ids": ["CH900_SC01"],
                "current_scene_id": "CH900_SC02",
                "blocked_scene_id": None,
                "latest_error": {
                    "code": "CHAPTER_RUN_FAILED",
                    "message": "scene execution crashed",
                },
            },
            worker_id="local-process",
            attempt_no=1,
            error_code="CHAPTER_RUN_FAILED",
            error_text="scene execution crashed",
        )
    )
    session.commit()

    status_response = client.get("/api/v1/chapters/CH900/run-status")

    assert status_response.status_code == 200
    status_payload = status_response.json()["data"]
    assert status_payload["status"] == "failed"
    assert status_payload["current_scene_id"] == "CH900_SC02"
    assert status_payload["completed_scene_ids"] == ["CH900_SC01"]
    assert status_payload["latest_error"] == {
        "code": "CHAPTER_RUN_FAILED",
        "message": "scene execution crashed",
    }


def test_chapter_run_status_reconciles_external_finalized_scene_progress(client, session) -> None:
    _create_chapter(client, "CH900")
    _create_scene(client, "CH900", "CH900_SC01", 1, is_chapter_last=1)
    state = session.get(SceneRunState, "CH900_SC01")
    assert state is not None
    state.scene_status = "archived"
    state.current_final_scene_row_id = "final_scene_CH900_SC01_v1"
    session.add(
        ChapterRunJob(
            job_id="chapter_run_CH900_stale_blocked",
            chapter_id="CH900",
            status="blocked",
            job_type="chapter_run_full",
            payload_json={
                "scene_ids": ["CH900_SC01"],
                "completed_scene_ids": [],
                "current_scene_id": "CH900_SC01",
                "blocked_scene_id": "CH900_SC01",
            },
            result_summary_json={
                "scene_ids": ["CH900_SC01"],
                "completed_scene_ids": [],
                "current_scene_id": "CH900_SC01",
                "blocked_scene_id": "CH900_SC01",
                "latest_error": {
                    "code": "CHAPTER_RUN_SCENE_INCOMPLETE",
                    "message": "scene run did not produce a final scene",
                },
            },
            worker_id="local-process",
            attempt_no=1,
            error_code="CHAPTER_RUN_SCENE_INCOMPLETE",
            error_text="scene run did not produce a final scene",
        )
    )
    session.commit()

    status_response = client.get("/api/v1/chapters/CH900/run-status")

    assert status_response.status_code == 200
    status_payload = status_response.json()["data"]
    assert status_payload["status"] == "completed"
    assert status_payload["completed_scene_ids"] == ["CH900_SC01"]
    assert status_payload["blocked_scene_id"] is None
    assert status_payload["current_scene_id"] == "CH900_SC01"
    assert status_payload["latest_error"] is None
