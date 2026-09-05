"""运行本章在归档步假报 RUN_CHECKPOINT_OUTPUT_MISSING、以及 run-status 假报 completed 两处回归。

产品路径：React 章节编排（v2 目录建章 + 场景）→ 起草台跑单场景 → 点「运行本章」。
章尾场景（is_chapter_last=1）走到归档步时要做章级准定稿评审（chapter_near_final_review），
该节点以 chapter scope 记账，记账层要求 SceneRunState.active_run_job_id == 本章任务 id。

bug 1（根因）：ChapterRunnerService._set_current_scene 只在 ORM 对象上赋 active_run_job_id，
没有 flush；Orchestrator.run_scene → SceneRunCheckpointService.acquire_execution →
session.refresh(state) 把这条未落库的归属改动直接丢掉（scene 级节点容忍 None，所以整条
场景管线照常跑完，只有章级节点被拒）。拒绝发生在记账台账落行之前，LLMNodeRunner.run 仍把
一个从未写入的 llm_call_id 塞进 LLMNodeExecutionError，evaluate_chapter 的兜底分支把它
持久化成 evaluator_llm_call_id，归档步随即以 RUN_CHECKPOINT_OUTPUT_MISSING 失败——
真实原因（LLM_ACCOUNTING_CONTEXT_INVALID: chapter run job has no active current scene）被吞掉。

bug 2：run-status / prepare_full_run 的 _reconcile_job 只看 SceneRunState 是否有定稿行就把
failed 任务重新推导成 completed / 100%，latest_error 清空；作者看到绿色 100%，且再点
运行本章也不会重跑（prepare_full_run 直接返回 completed）。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    ChapterRunJob,
    LlmCall,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_task_runner import LLMNodeExecutionError, LLMNodeRunner
from novel_system.services.near_final import NearFinalAcceptanceService
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService, chapter_scene_execution_id
from tests.real_llm_fakes import (
    _SCENE_PIPELINE_RUNNER_MODULES,
    ScenePipelineOnlineFake,
    install_online_pipeline,
)

_seq = 0


def _key(prefix: str) -> str:
    global _seq
    _seq += 1
    return f"{prefix}-{_seq}"


# ---------------------------------------------------------------- 产品路径夹具（抄 ws-catalog.jsx 载荷）


def _create_project(client) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": "归档检查点回归", "outline_text": "大纲", "genre": "悬疑"},
        headers={"X-Idempotency-Key": _key("fix-ac-project")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _create_catalog_chapter(client, project_id: str) -> str:
    response = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters",
        json={
            "title": "复测章",
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
            "with_scene": False,
        },
        headers={"X-Idempotency-Key": _key("fix-ac-chapter")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["chapter"]["chapter_id"]


def _create_catalog_scene(client, project_id: str, chapter_id: str) -> str:
    response = client.post(
        f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}/scenes",
        json={
            "title": "河边的信封",
            "kind": "proactive",
            "at": 0,
            "state": "writing",
            "brief": {
                "goal": "主角想在天亮前拿到信封。",
                "conflict": "对方要求先交出录音。",
                "setback": "信封是空的，录音已经送出。",
            },
            "goal": None,
            "conflict": None,
            "setback": None,
            "reaction": None,
            "dilemma": None,
            "decision": None,
            "exit_change": "主角意识到自己被调包。",
            "hook": "河面上漂来第二只信封。",
        },
        headers={"X-Idempotency-Key": _key("fix-ac-scene")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["scene"]["scene_id"]


def _catalog_chapter_ready_to_run(client) -> tuple[str, str, str]:
    """v2 目录建章 + 唯一场景（目录按序号把最后一场标成 is_chapter_last=1）→ 设 POV → 预检建卡。"""

    project_id = _create_project(client)
    chapter_id = _create_catalog_chapter(client, project_id)
    scene_id = _create_catalog_scene(client, project_id, chapter_id)
    patched = client.patch(
        f"/api/v2/projects/{project_id}/catalog/scenes/{scene_id}",
        json={"pov_character_name": "阿一"},
        headers={"X-Idempotency-Key": _key("fix-ac-scene-pov")},
    )
    assert patched.status_code == 200, patched.text
    cards = client.post(
        f"/api/v1/scenes/{scene_id}/preflight/create-cards",
        headers={"X-Idempotency-Key": _key("fix-ac-cards")},
    )
    assert cards.status_code == 200, cards.text
    return project_id, chapter_id, scene_id


class _StrictStopPipelineFake(ScenePipelineOnlineFake):
    """软 QC 通过但带一条 Q2 级建议（产品观测形状：llm_advisory / 非阻断）。

    起草台按 strict 跑单场景时，编排层据此停在 quality_warning_pending_acceptance，
    不写定稿行；随后「运行本章」（reliable）会重新跑整条场景管线直到归档步。"""

    def generate(self, request):
        response = super().generate(request)
        if request.node_id != "soft_qc":
            return response
        payload = {
            **dict(response.structured_output),
            "issues": [{"issue_key": "pacing_soft_spot", "message": "中段节奏略平，可选修。"}],
        }
        return replace(response, structured_output=payload, text=json.dumps(payload, ensure_ascii=False))


def _install_strict_stop_pipeline(monkeypatch) -> _StrictStopPipelineFake:
    fake = _StrictStopPipelineFake()

    def _runner_factory(session, *, llm_client=None, **kwargs):
        return LLMNodeRunner(session, llm_client=llm_client or fake, **kwargs)

    for module in _SCENE_PIPELINE_RUNNER_MODULES:
        monkeypatch.setattr(f"{module}.LLMNodeRunner", _runner_factory)
    return fake


def _pending_chapter_job(session, chapter_id: str, scene_id: str) -> ChapterRunJob:
    job = ChapterRunJob(
        job_id=f"chapter_run_{chapter_id}_test",
        chapter_id=chapter_id,
        status="pending",
        job_type="chapter_run_full",
        payload_json={
            "scene_ids": [scene_id],
            "completed_scene_ids": [],
            "current_scene_id": None,
            "blocked_scene_id": None,
        },
        result_summary_json={
            "scene_ids": [scene_id],
            "completed_scene_ids": [],
            "current_scene_id": None,
            "blocked_scene_id": None,
            "latest_error": None,
        },
        worker_id="local-process",
        attempt_no=0,
    )
    session.add(job)
    session.commit()
    return job


# ---------------------------------------------------------------- bug 1：归属没有落库


def test_set_current_scene_ownership_survives_checkpoint_refresh(client, session) -> None:
    """acquire_execution 走 session.refresh(state)；未 flush 的归属赋值会被它丢掉。"""

    _, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)
    job = _pending_chapter_job(session, chapter_id, scene_id)
    service = ChapterRunnerService(session)

    service._set_current_scene(job, scene_id)

    state = SceneRunCheckpointService(session)._state(scene_id, refresh=True)
    assert state.active_run_job_id == job.job_id, "chapter run ownership must be durable before scene execution"


def test_run_full_over_catalog_chapter_last_scene_runs_chapter_near_final_review(client, session, monkeypatch) -> None:
    """修复前：整条场景管线跑完，归档步以 RUN_CHECKPOINT_OUTPUT_MISSING 假失败，
    llm_calls 里根本没有 chapter_near_final_review 这一行。"""

    install_online_pipeline(monkeypatch)
    _, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)

    response = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-ac-run-full")},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["latest_error"] or {}).get("code") != "RUN_CHECKPOINT_OUTPUT_MISSING", data
    assert data["status"] == "completed", data
    assert data["completed_scene_ids"] == [scene_id]
    job_id = data["job_id"]

    session.expire_all()
    review_calls = session.execute(
        select(LlmCall).where(
            LlmCall.node_id == "chapter_near_final_review",
            LlmCall.chapter_id == chapter_id,
        )
    ).scalars().all()
    assert len(review_calls) == 1, "the chapter near-final review must be a real ledgered provider call"
    review_call = review_calls[0]
    assert review_call.scope_type == "chapter"
    assert review_call.run_job_id == job_id
    assert review_call.execution_id == chapter_scene_execution_id(job_id, scene_id)
    assert review_call.execution_step_key == "archive:chapter_near_final:0"
    assert review_call.accounting_status == "settled"

    evaluation = session.execute(
        select(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == chapter_id,
        )
    ).scalars().one()
    assert evaluation.evaluator_llm_call_id == review_call.llm_call_id

    state = session.get(SceneRunState, scene_id)
    assert state.scene_status == "archived"
    assert state.active_run_job_id is None

    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "completed"
    assert status["progress_pct"] == 100
    assert status["latest_error"] is None


def test_run_job_after_scene_job_runs_chapter_near_final_review(client, session, monkeypatch) -> None:
    """产品顺序：起草台先按 strict 跑单场景 run-job（停在 quality_warning_pending_acceptance，
    无定稿行），再从章节编排点运行本章（run-job → worker → run_full）。

    修复前：章任务重新跑完整条场景管线，在归档步以 RUN_CHECKPOINT_OUTPUT_MISSING 落 failed，
    llm_calls 里没有任何 chapter_near_final_review 行。"""

    fake = _install_strict_stop_pipeline(monkeypatch)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    project_id, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)

    from novel_system.services.projects import _run_project_chapter_job_worker
    from novel_system.services.scene_run_jobs import _run_scene_job_worker

    # 同线程跑 worker，避免测试与后台线程竞争 SQLite
    monkeypatch.setattr("novel_system.api.routes.scenes.start_scene_run_job_worker", _run_scene_job_worker)
    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda pid, cid, job_id: _run_project_chapter_job_worker(pid, cid, job_id),
    )

    scene_job = client.post(
        f"/api/v1/scenes/{scene_id}/run/jobs",
        json={"run_policy": "strict"},
        headers={"X-Idempotency-Key": _key("fix-ac-scene-job")},
    )
    assert scene_job.status_code == 200, scene_job.text
    latest = client.get(f"/api/v1/scenes/{scene_id}/run/jobs/latest").json()["data"]
    assert latest["status"] == "completed", latest
    assert latest["current_step"] == "awaiting_author_acceptance", latest
    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert state.scene_status == "quality_warning_pending_acceptance"
    assert state.current_final_scene_row_id is None, "strict stop must not have written final text"
    assert state.active_run_job_id is None
    scene_pass_requests = len(fake.requests)
    assert not session.execute(
        select(LlmCall).where(LlmCall.node_id == "chapter_near_final_review", LlmCall.chapter_id == chapter_id)
    ).scalars().all(), "the strict stop happens before the archive step, so no chapter review has run yet"

    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job",
        json={},
        headers={"X-Idempotency-Key": _key("fix-ac-run-job")},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["run"]["job_id"]

    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job.error_code != "RUN_CHECKPOINT_OUTPUT_MISSING", (job.error_code, job.error_text)
    assert job.status == "completed", (job.status, job.error_code, job.error_text)
    assert len(fake.requests) > scene_pass_requests, "the chapter run must have executed the scene pipeline itself"
    review_calls = session.execute(
        select(LlmCall).where(
            LlmCall.node_id == "chapter_near_final_review",
            LlmCall.chapter_id == chapter_id,
        )
    ).scalars().all()
    assert [(call.accounting_status, call.run_job_id, call.execution_step_key) for call in review_calls] == [
        ("settled", job_id, "archive:chapter_near_final:0")
    ]
    state = session.get(SceneRunState, scene_id)
    assert state.scene_status == "archived"
    assert state.current_final_scene_row_id
    assert state.active_run_job_id is None
    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "completed"
    assert status["completed_scene_ids"] == [scene_id]
    assert status["latest_error"] is None


# ---------------------------------------------------------------- bug 1（配套）：台账之前被拒不能伪装成评审结果


class _RejectedBeforeLedgerRunner:
    provider_execution_mode = "online"

    def run(self, **kwargs):
        rejection = LLMAccountingRejected(
            "LLM_ACCOUNTING_CONTEXT_INVALID",
            "chapter run job has no active current scene",
            details={"missing_or_invalid_field": "run_job_id"},
        )
        raise LLMNodeExecutionError(
            llm_call_id=f"llm_call_{kwargs['chapter_id']}_never_written",
            error_code=rejection.code,
            message=str(rejection),
            request_summary={},
            response_summary={},
            original_error=rejection,
            retryable=False,
        )


def _seed_chapter_with_final_memory(session, *, chapter_id: str = "CH_AC01") -> str:
    session.add(StoryProject(project_id="PRJ_AC01", title="归档回归", outline_text=""))
    session.add(ChapterGoal(chapter_id=chapter_id, project_id="PRJ_AC01", planned_scene_count=1, chapter_goal="章目标"))
    session.add(
        ChapterMemory(
            row_id=f"chapter_memory_final_{chapter_id}_v1",
            chapter_id=chapter_id,
            aggregate_stage="final",
            content="她把信封拆成两半，一半留给河。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.commit()
    return chapter_id


def test_evaluate_chapter_rejected_before_ledger_fails_closed_with_real_code(session) -> None:
    chapter_id = _seed_chapter_with_final_memory(session)
    service = NearFinalAcceptanceService(session, llm_runner=_RejectedBeforeLedgerRunner())

    with pytest.raises(DomainError) as raised:
        service.evaluate_chapter(chapter_id, execution_step_key="archive:chapter_near_final:0")

    assert raised.value.code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert "no active current scene" in raised.value.message
    assert raised.value.details["node_id"] == "chapter_near_final_review"
    assert session.execute(
        select(WriterEvaluation).where(WriterEvaluation.object_type == "chapter")
    ).scalars().all() == [], "a dangling evaluator_llm_call_id must never be persisted"


# ---------------------------------------------------------------- bug 2：run-status 把 failed 假报成 completed


def _seed_failed_archive_job(session, chapter_id: str, scene_id: str) -> ChapterRunJob:
    """模拟归档步失败后的真实落库形状：near-final 已写下定稿行，执行 failed，任务 failed。"""

    state = session.get(SceneRunState, scene_id)
    assert state is not None
    state.scene_status = "soft_qc_passed"
    state.current_final_scene_row_id = f"final_scene_{scene_id}_v1"
    state.run_execution_status = "failed"
    job = ChapterRunJob(
        job_id=f"chapter_run_{chapter_id}_failed",
        chapter_id=chapter_id,
        status="failed",
        job_type="chapter_run_full",
        payload_json={
            "scene_ids": [scene_id],
            "completed_scene_ids": [],
            "current_scene_id": scene_id,
            "blocked_scene_id": None,
        },
        result_summary_json={
            "scene_ids": [scene_id],
            "completed_scene_ids": [],
            "current_scene_id": scene_id,
            "blocked_scene_id": None,
            "latest_error": {
                "code": "RUN_CHECKPOINT_OUTPUT_MISSING",
                "message": "checkpoint references a committed call/output that is missing",
            },
        },
        worker_id="local-process",
        attempt_no=1,
        error_code="RUN_CHECKPOINT_OUTPUT_MISSING",
        error_text="checkpoint references a committed call/output that is missing",
    )
    session.add(job)
    session.commit()
    return job


def test_run_status_keeps_failed_job_failed_when_failed_scene_left_final_text(client, session) -> None:
    _, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)
    _seed_failed_archive_job(session, chapter_id, scene_id)

    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]

    assert status["status"] == "failed", status
    assert status["completed_scene_ids"] == []
    assert status["progress_pct"] == 0
    assert status["latest_error"] == {
        "code": "RUN_CHECKPOINT_OUTPUT_MISSING",
        "message": "checkpoint references a committed call/output that is missing",
    }
    session.expire_all()
    job = session.execute(select(ChapterRunJob).where(ChapterRunJob.chapter_id == chapter_id)).scalars().one()
    assert job.status == "failed"
    assert job.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"


def test_prepare_full_run_retries_failed_job_instead_of_reporting_completed(client, session) -> None:
    _, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)
    _seed_failed_archive_job(session, chapter_id, scene_id)

    payload, should_start_worker = ChapterRunnerService(session).prepare_full_run(chapter_id)

    assert payload["status"] == "pending", payload
    assert should_start_worker is True
    assert payload["completed_scene_ids"] == []


def test_run_status_reports_archive_step_failure_with_its_code(client, session, monkeypatch) -> None:
    """真实管线：near-final 已产出定稿行后归档步失败——作者必须看到 failed + 错误码，而不是绿色 100%。"""

    install_online_pipeline(monkeypatch)
    _, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)

    def _archive_failure(*_args, **_kwargs):
        raise DomainError(
            "CHAPTER_NEAR_FINAL_REVIEW_FAILED",
            "chapter near-final review failed: provider unavailable",
            status_code=502,
            details={
                "author_action": {
                    "title": "章级评审失败",
                    "message": "模型服务暂不可用，请稍后重新运行本章。",
                }
            },
        )

    monkeypatch.setattr(
        "novel_system.services.orchestrator.Orchestrator._run_archive_chapter_evaluation",
        _archive_failure,
    )

    response = client.post(
        f"/api/v1/chapters/{chapter_id}/run/full",
        headers={"X-Idempotency-Key": _key("fix-ac-run-full-archive-fail")},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "failed", data
    assert data["latest_error"]["code"] == "CHAPTER_NEAR_FINAL_REVIEW_FAILED"
    assert data["latest_error"]["author_action"]["message"] == "模型服务暂不可用，请稍后重新运行本章。"

    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert state.current_final_scene_row_id, "near-final must already have produced final text before the archive step"

    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "failed", status
    assert status["progress_pct"] == 0
    assert status["completed_scene_ids"] == []
    assert status["latest_error"]["code"] == "CHAPTER_NEAR_FINAL_REVIEW_FAILED"
    assert status["latest_error"]["author_action"]["message"] == "模型服务暂不可用，请稍后重新运行本章。"


def test_run_job_worker_keeps_author_action_when_claimed_job_fails_outside_scene_execution(
    client, session, monkeypatch
) -> None:
    """React 产品路径（run-job → worker）：claim 之后、场景执行之外抛出的 DomainError 由
    ChapterRunnerService 先按错误码 + author_action 落 failed 并提交，随后 worker 再次
    落库同一错误——第二次落库不能把作者指引覆盖成只剩 code/message。"""

    project_id, chapter_id, scene_id = _catalog_chapter_ready_to_run(client)
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    from novel_system.services.projects import _run_project_chapter_job_worker

    monkeypatch.setattr(
        "novel_system.api.routes.projects.start_project_chapter_run_job_worker",
        lambda pid, cid, job_id: _run_project_chapter_job_worker(pid, cid, job_id),
    )

    def _gate_failure(self, chapter_id, *, scene_id=None):
        raise DomainError(
            "CHAPTER_GATE_UNAVAILABLE",
            "chapter gate evaluation failed",
            status_code=502,
            details={
                "author_action": {
                    "title": "章节门禁不可用",
                    "message": "请稍后重新运行本章。",
                }
            },
        )

    monkeypatch.setattr(
        "novel_system.services.chapter_runner.ChapterRunnerService._chapter_gate_error",
        _gate_failure,
    )

    response = client.post(
        f"/api/v1/projects/{project_id}/chapters/{chapter_id}/run-job",
        json={},
        headers={"X-Idempotency-Key": _key("fix-ac-run-job-gate-fail")},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["run"]["job_id"]

    session.expire_all()
    job = session.get(ChapterRunJob, job_id)
    assert job.status == "failed", (job.status, job.error_code, job.error_text)
    assert job.error_code == "CHAPTER_GATE_UNAVAILABLE"
    latest_error = (job.result_summary_json or {}).get("latest_error") or {}
    assert latest_error["code"] == "CHAPTER_GATE_UNAVAILABLE"
    assert latest_error["author_action"]["message"] == "请稍后重新运行本章。"

    monkeypatch.undo()
    status = client.get(f"/api/v1/chapters/{chapter_id}/run-status").json()["data"]
    assert status["status"] == "failed", status
    assert status["latest_error"]["code"] == "CHAPTER_GATE_UNAVAILABLE"
    assert status["latest_error"]["author_action"]["message"] == "请稍后重新运行本章。"
    state = session.get(SceneRunState, scene_id)
    assert state.active_run_job_id is None, "failed claimed job must release scene ownership"
