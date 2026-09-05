"""运行本章（章节编排）两处回归：

1. v2 目录（React ws-catalog.jsx 产品路径）建的场景只有 SceneCard，没有 SceneRunState；
   ChapterRunnerService._set_current_scene 直接 404 SCENE_NOT_FOUND，整章一起步就失败。
   修复：目录建场景时补建状态行，且 _set_current_scene 对缺行惰性补建（覆盖存量数据）。
2. run_full 在 claim（running + 租约已提交）之后、场景执行之外抛 DomainError 时，任务永远停在
   running：run-status 显示运行中 0%，run-job 命中 RUN_JOB_IN_PROGRESS，租约过期后
   prepare_full_run 也不会再拉起 worker。修复：claim 后任何异常都按错误码落 failed 并提交；
   租约过期的 running 在 reconcile 时回到 pending 并允许重新拉起 worker。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from novel_system.db.models import ChapterRunJob, FinalScene, SceneCard, SceneRunState
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError

_seq = 0


def _key(prefix: str) -> str:
    global _seq
    _seq += 1
    return f"{prefix}-{_seq}"


def _create_project(client) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "章节编排回归", "outline_text": "大纲", "genre": "悬疑"},
        headers={"X-Idempotency-Key": _key("fix-cr-project")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _create_catalog_chapter(client, project_id: str, *, title: str = "第一章", with_scene: bool = False) -> dict:
    # 载荷形状抄 frontend-react/src/ws-catalog.jsx catCreateChapterViaApi
    response = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters",
        json={
            "title": title,
            "state": "writing",
            "current": True,
            "words_target": None,
            "act": None,
            "tension": None,
            "pov": None,
            "time_label": None,
            "place": None,
            "entry": None,
            "exit": None,
            "align": None,
            "promise": None,
            "drama": {},
            "threads": [],
            "with_scene": with_scene,
        },
        headers={"X-Idempotency-Key": _key("fix-cr-chapter")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["chapter"]


def _create_catalog_scene(client, project_id: str, chapter_id: str, *, title: str, at: int, reactive: bool = False) -> dict:
    # 载荷形状抄 frontend-react/src/ws-catalog.jsx catSceneCreateBody
    body = {
        "title": title,
        "kind": "reactive" if reactive else "proactive",
        "at": at,
        "state": "writing" if at == 0 else "todo",
        "brief": (
            {"reaction": "反应", "dilemma": "两难", "decision": "决定"}
            if reactive
            else {"goal": "目标", "conflict": "冲突", "setback": "挫败"}
        ),
    }
    response = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        json=body,
        headers={"X-Idempotency-Key": _key("fix-cr-scene")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["scene"]


def _catalog_chapter_with_scenes(client, *, scene_count: int = 2) -> tuple[str, str, list[str]]:
    project_id = _create_project(client)
    chapter = _create_catalog_chapter(client, project_id)
    scene_ids = [
        _create_catalog_scene(client, project_id, chapter["chapter_id"], title=f"场景 {index + 1}", at=index)["scene_id"]
        for index in range(scene_count)
    ]
    return project_id, chapter["chapter_id"], scene_ids


def _install_fake_runner(monkeypatch):
    """替换 Orchestrator：断言状态行已存在，然后把场景直接归档。"""

    calls: list[str] = []

    class FakeOrchestrator:
        def __init__(self, session) -> None:
            self.session = session

        def run_scene(self, scene_id: str, *, execution_id=None, lease_renewer=None, run_job_id=None) -> dict:
            calls.append(scene_id)
            state = self.session.get(SceneRunState, scene_id)
            assert state is not None, "chapter runner must hand the orchestrator an existing SceneRunState"
            scene = self.session.get(SceneCard, scene_id)
            assert scene is not None
            # 真实 Orchestrator 归档时会写下带正文的 FinalScene；worker 路径在 run_full
            # 完成后还会用 ChapterManuscriptService.require_complete 校验每个场景都有
            # 非空定稿正文，夹具若只改状态行，任务会以
            # CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE 落 failed。
            final_row_id = f"final_scene_{scene_id}"
            self.session.add(
                FinalScene(
                    row_id=final_row_id,
                    scene_id=scene_id,
                    chapter_id=scene.chapter_id,
                    content=f"{scene_id} 的归档正文（回归夹具）",
                    status="archived",
                    source_bundle_id=f"fix-cr-bundle-{scene_id}",
                    source_bundle_hash="fix-cr-hash",
                )
            )
            state.scene_status = "archived"
            state.current_final_scene_row_id = final_row_id
            # 真实 Orchestrator 会自己提交持久检查点；这里同样提交，worker 路径的
            # 后续回滚才不会把"场景已归档"一起冲掉。
            self.session.commit()
            return {
                "scene_status": "archived",
                "current_human_review_event_id": None,
                "current_final_scene_row_id": state.current_final_scene_row_id,
            }

    class FakeChapterRuntimeService:
        def __init__(self, session) -> None:
            self.session = session

        def chapter_state_payload(self, chapter_id: str) -> dict:
            return {
                "chapter_id": chapter_id,
                "chapter_backfill_pending_count": 0,
                "aggregate_block_reason": "none",
                "manual_hold_reason": None,
            }

    monkeypatch.setattr("novel_system.services.chapter_runner.Orchestrator", FakeOrchestrator)
    return calls


def _drop_scene_run_states(session, scene_ids: list[str]) -> None:
    """模拟修复前目录建出来的存量场景（只有 SceneCard 没有状态行）。"""

    session.execute(delete(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids)))
    session.commit()


# ---------------------------------------------------------------- bug 1


def test_v2_catalog_scenes_get_scene_run_state_like_v1_scenes(client, session) -> None:
    project_id = _create_project(client)
    chapter = _create_catalog_chapter(client, project_id, with_scene=True)
    default_scene_id = chapter["scenes"][0]["scene_id"]
    extra_scene_id = _create_catalog_scene(client, project_id, chapter["chapter_id"], title="追加", at=1, reactive=True)["scene_id"]

    for scene_id in (default_scene_id, extra_scene_id):
        state = session.get(SceneRunState, scene_id)
        assert state is not None, f"catalog scene {scene_id} must own a SceneRunState"
        assert state.scene_status == "ready"
        assert state.active_run_job_id is None
        assert state.current_final_scene_row_id is None


def test_run_full_over_imported_catalog_scenes_uses_lazy_run_state(client, session, monkeypatch) -> None:
    """目录导入（迁移/夹具路径）不预建状态行：运行本章必须靠惰性补建起步，而不是 SCENE_NOT_FOUND。"""

    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    project_id = _create_project(client)
    response = client.post(
        f"/api/v2/projects/{project_id}/catalog/import",
        json={
            "chapters": [
                {
                    "title": "导入章",
                    "state": "writing",
                    "current": True,
                    "scenes": [
                        {"title": "导入场景一", "kind": "主动", "state": "writing"},
                        {"title": "导入场景二", "kind": "反应", "state": "todo"},
                    ],
                }
            ]
        },
        headers={"X-Idempotency-Key": _key("fix-cr-import"), "X-Admin-Token": "admin-token"},
    )
    assert response.status_code == 200, response.text
    scene_ids = session.execute(
        select(SceneCard.scene_id).where(SceneCard.project_id == project_id).order_by(SceneCard.scene_seq)
    ).scalars().all()
    assert len(scene_ids) == 2
    assert all(session.get(SceneRunState, scene_id) is None for scene_id in scene_ids)
    chapter_id = session.get(SceneCard, scene_ids[0]).chapter_id
    calls = _install_fake_runner(monkeypatch)

    run = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-import-run")},
    )

    assert run.status_code == 200, run.text
    assert run.json()["data"]["status"] == "completed"
    assert calls == scene_ids
    session.expire_all()
    for scene_id in scene_ids:
        state = session.get(SceneRunState, scene_id)
        assert state is not None
        assert state.active_run_job_id is None


def test_set_current_scene_lazily_creates_missing_run_state(client, session) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    _drop_scene_run_states(session, scene_ids)
    assert session.get(SceneRunState, scene_ids[0]) is None

    service = ChapterRunnerService(session)
    job = service._create_job(chapter_id, scene_ids)
    session.commit()
    owner = service._claim_running(job, worker_id="fix-cr-worker", lease_seconds=30)
    service._active_owner = owner
    session.commit()

    service._set_current_scene(job, scene_ids[0])

    state = session.get(SceneRunState, scene_ids[0])
    assert state is not None
    assert state.scene_status == "ready"
    assert state.active_run_job_id == job.job_id

    with pytest.raises(DomainError) as missing_card:
        service._set_current_scene(job, "no-such-scene")
    assert missing_card.value.code == "SCENE_NOT_FOUND"


def test_run_full_over_v2_catalog_scenes_reaches_orchestrator(client, session, monkeypatch) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=2)
    # 存量数据：修复前目录建的场景没有状态行，运行本章仍必须能起步
    _drop_scene_run_states(session, scene_ids)
    calls = _install_fake_runner(monkeypatch)

    response = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-run-full")},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "completed"
    assert data["completed_scene_ids"] == scene_ids
    assert data["latest_error"] is None
    assert calls == scene_ids


def test_run_job_over_v2_catalog_scenes_reaches_orchestrator(client, session, monkeypatch) -> None:
    """React 产品路径：POST run-job → worker → run_full。修复前 worker 立刻以 SCENE_NOT_FOUND 落 failed。"""

    project_id, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    _drop_scene_run_states(session, scene_ids)
    calls = _install_fake_runner(monkeypatch)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.projects import _run_project_chapter_job_worker

    # 同线程跑 worker，避免测试与后台线程竞争 SQLite
    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda pid, cid, job_id: _run_project_chapter_job_worker(pid, cid, job_id),
    )

    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job",
        json={},
        headers={"X-Idempotency-Key": _key("fix-cr-run-job")},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["run"]["job_id"]

    assert calls == scene_ids
    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job is not None
    assert job.error_code != "SCENE_NOT_FOUND"
    # 任务真实终态必须是 completed：run-status 不再从场景状态把 failed 任务反推成完成
    assert job.status == "completed", (job.error_code, job.error_text)
    state = session.get(SceneRunState, scene_ids[0])
    assert state is not None and state.current_final_scene_row_id
    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "completed"
    assert status["completed_scene_ids"] == scene_ids
    assert status["latest_error"] is None


def test_run_full_over_v2_catalog_scenes_without_llm_fails_closed_not_scene_not_found(client, session) -> None:
    """真实 Orchestrator + 未配置 LLM：必须走进场景执行（fail-closed 落 failed），而不是 SCENE_NOT_FOUND。

    目录建的裸场景在生成节点之前先被执行契约门挡下（观测到 SCENE_EXECUTION_CONTRACT_BLOCKED），
    这已经证明 _set_current_scene 放行了；不绑定具体错误码，避免和 orchestrator 的门序耦合。
    """

    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)

    response = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-run-full-llm-off")},
    )

    assert response.status_code == 200, response.text
    assert "SCENE_NOT_FOUND" not in response.text, response.text
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["current_scene_id"] == scene_ids[0], "the runner must have handed the scene to the orchestrator"
    assert data["latest_error"] is not None
    assert data["latest_error"]["code"] != "SCENE_NOT_FOUND"
    assert data["source"] is None
    # 不论以哪种形式失败，任务都不能卡在 running
    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "failed"
    assert status["latest_error"]["code"] == data["latest_error"]["code"]


# ---------------------------------------------------------------- bug 2 (a)


def test_domain_error_after_claim_marks_job_failed_and_stays_retryable(client, session, monkeypatch) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    calls = _install_fake_runner(monkeypatch)
    # 场景被另一个（单场景）run-job 占着：_set_current_scene 在 claim 之后抛 RUN_JOB_IN_PROGRESS
    state = session.get(SceneRunState, scene_ids[0])
    assert state is not None
    state.active_run_job_id = "scene_run_someone_else"
    session.commit()

    failed = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-claim-fail")},
    )
    assert failed.status_code == 409, failed.text
    assert failed.json()["error"]["code"] == "RUN_JOB_IN_PROGRESS"
    assert calls == []

    session.expire_all()
    job = session.execute(select(ChapterRunJob).where(ChapterRunJob.chapter_id == chapter_id)).scalars().one()
    assert job.status == "failed", "a DomainError after the claim must not leave the job stuck in running"
    assert job.error_code == "RUN_JOB_IN_PROGRESS"
    assert job.error_text
    assert job.finished_at
    assert job.result_summary_json["latest_error"] == {"code": "RUN_JOB_IN_PROGRESS", "message": job.error_text}
    # 场景侧归属没有被章任务抢走
    session.refresh(state)
    assert state.active_run_job_id == "scene_run_someone_else"

    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "failed"
    assert status["latest_error"]["code"] == "RUN_JOB_IN_PROGRESS"

    # 释放场景后，同一任务可以重跑到完成，而不是继续 409
    state.active_run_job_id = None
    session.commit()
    resumed = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-claim-retry")},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["status"] == "completed"
    assert resumed.json()["data"]["job_id"] == job.job_id
    assert calls == scene_ids


def test_unexpected_error_after_claim_marks_job_failed_with_public_code(client, session, monkeypatch) -> None:
    _, chapter_id, _scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    _install_fake_runner(monkeypatch)

    def explode(self, chapter_id, *, scene_id=None):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(ChapterRunnerService, "_chapter_gate_error", explode)

    with pytest.raises(RuntimeError):
        ChapterRunnerService(session).run_full(chapter_id)

    session.rollback()
    session.expire_all()
    job = session.execute(select(ChapterRunJob).where(ChapterRunJob.chapter_id == chapter_id)).scalars().one()
    assert job.status == "failed"
    assert job.error_code == "CHAPTER_RUN_FAILED"
    assert "secret internal detail" not in (job.error_text or "")


# ---------------------------------------------------------------- bug 2 (b)


def _insert_running_job(session, chapter_id: str, scene_ids: list[str], *, lease_expires_at: str | None) -> ChapterRunJob:
    job = ChapterRunJob(
        job_id=f"chapter_run_{chapter_id}_stale",
        chapter_id=chapter_id,
        status="running",
        job_type="chapter_run_full",
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
        worker_id="dead-worker",
        attempt_no=1,
        heartbeat_at=lease_expires_at,
        lease_expires_at=lease_expires_at,
        started_at=lease_expires_at,
    )
    session.add(job)
    session.commit()
    return job


def test_prepare_full_run_reclaims_running_job_with_expired_lease(client, session) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    job = _insert_running_job(session, chapter_id, scene_ids, lease_expires_at=expired)

    prepared, should_start = ChapterRunnerService(session).prepare_full_run(chapter_id)

    assert should_start is True, "an orphaned running job must restart a worker"
    assert prepared["job_id"] == job.job_id
    assert prepared["status"] == "pending"
    session.commit()
    session.expire_all()
    persisted = session.get(ChapterRunJob, job.job_id)
    assert persisted.status == "pending"
    assert persisted.lease_expires_at is None


def test_prepare_full_run_keeps_running_job_with_live_lease(client, session) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    live = (datetime.now(UTC) + timedelta(seconds=300)).isoformat()
    job = _insert_running_job(session, chapter_id, scene_ids, lease_expires_at=live)

    prepared, should_start = ChapterRunnerService(session).prepare_full_run(chapter_id)

    assert should_start is False
    assert prepared["job_id"] == job.job_id
    assert prepared["status"] == "running"


def test_run_status_does_not_report_expired_lease_as_running(client, session) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    _insert_running_job(session, chapter_id, scene_ids, lease_expires_at=expired)

    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]

    assert status["status"] == "pending"
    assert status["progress_pct"] == 0


def test_run_job_restarts_worker_for_running_job_with_expired_lease(client, session, monkeypatch) -> None:
    project_id, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    job = _insert_running_job(session, chapter_id, scene_ids, lease_expires_at=expired)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    started: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda *args: started.append(args),
    )

    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job",
        json={},
        headers={"X-Idempotency-Key": _key("fix-cr-run-job-stale")},
    )

    assert response.status_code == 200, response.text
    run = response.json()["data"]["run"]
    assert run["job_id"] == job.job_id
    assert run["status"] == "pending"
    assert started == [(project_id, chapter_id, job.job_id)]


def test_run_full_can_take_over_running_job_with_expired_lease(client, session, monkeypatch) -> None:
    _, chapter_id, scene_ids = _catalog_chapter_with_scenes(client, scene_count=1)
    expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    job = _insert_running_job(session, chapter_id, scene_ids, lease_expires_at=expired)
    calls = _install_fake_runner(monkeypatch)

    response = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-cr-run-full-stale")},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["job_id"] == job.job_id
    assert data["status"] == "completed"
    assert calls == scene_ids
