from __future__ import annotations

import json
import inspect
import ast
from dataclasses import fields, replace
from pathlib import Path

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import (
    LLMHTTPError,
    LLMRequest,
    LLMResponse,
    ModelRoutingConfig,
    OnlineAccountedExecution,
    TaskModelConfig,
)
from novel_system.services.llm_accounting import LLMAccountingRejected, LLMCallContext
from novel_system.services.llm_audit import fingerprint_identifier
from novel_system.services.errors import DomainError
from novel_system.services.llm_task_runner import (
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService
from novel_system.settings import Settings


def _prompt(target_input_tokens: int = 400) -> dict:
    return {
        "template_name": "neutral_draft",
        "template_version": "test",
        "system_prompt": "system prompt",
        "user_prompt": "Scene ID: CH100_SC01\nReturn JSON.",
        "structured_schema": {"type": "object"},
        "prompt_hash": "prompt_hash_test",
        "token_budget": {
            "target_input_tokens": target_input_tokens,
            "estimated_input_tokens": 0,
            "remaining_input_tokens": target_input_tokens,
            "included_sections": [],
            "compressed_sections": [],
            "omitted_sections": [],
            "section_status": {},
            "continuity_policy": [],
            "split_scene_recommended": False,
            "stop_reason": None,
            "continuity_warning": None,
        },
    }


def _routing_config() -> ModelRoutingConfig:
    task_config = TaskModelConfig(
        provider="openai_compatible",
        model="fake-model",
        temperature=0.2,
        max_output_tokens=120,
        response_format="json_object",
        provider_id="provider_primary",
        account_id="account_a",
        reasoning_level="medium",
        api_mode="responses",
        credential_mode="api_key",
    )
    return ModelRoutingConfig(
        node_routing={"neutral_draft": task_config},
        task_routing={"neutral_draft": task_config},
        retry_budget={},
        job_runtime={},
    )


def _live_settings() -> Settings:
    return Settings(
        database_url="sqlite:///test.db",
        vector_backend="memory",
        vector_store_dir=__import__("pathlib").Path(".vector_store_test"),
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:8080/v1",
        llm_api_key=None,
        llm_timeout_seconds=30.0,
        llm_enabled=True,
    )


class RecordingClient(OnlineAccountedExecution):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = {"scene_text": "generated scene"}
        return LLMResponse(
            request_id="resp_success",
            provider="fake-provider",
            model="fake-model",
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_success", "model": "fake-model"},
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            finish_reason="stop",
            attempt_count=2,
            max_retries=3,
            retryable=False,
            raw_usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            usage_present=True,
            usage_complete=True,
        )

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        response = self.generate(request)
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


class FailingClient(OnlineAccountedExecution):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMHTTPError(
            "LLM_HTTP_REQUEST_FAILED",
            "provider connection failed",
            retryable=True,
            details={"attempt_count": 3, "max_retries": 2},
        )

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        try:
            return self.generate(request)
        except Exception as exc:
            accounting_hook.after_error(
                handle,
                request=request,
                error=exc,
                raw_response=None,
                provider_request_id=None,
                latency_ms=1,
            )
            raise


class _SimulatedProcessCrash(BaseException):
    pass


class _AccountedRecordingClient(OnlineAccountedExecution):
    def __init__(self) -> None:
        self.post_count = 0

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        self.post_count += 1
        payload = {"scene_text": f"accounted scene {self.post_count}"}
        response = LLMResponse(
            request_id=f"accounted-{self.post_count}",
            provider="fake-provider",
            model="fake-model",
            text=json.dumps(payload),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": f"accounted-{self.post_count}", "model": "fake-model"},
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            raw_usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            usage_present=True,
            usage_complete=True,
            finish_reason="stop",
        )
        accounting_hook.after_response(
            handle,
            request=request,
            response=response,
            latency_ms=1,
        )
        return response


class _AdvisoryAccountedClient(OnlineAccountedExecution):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.post_count = 0

    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:  # noqa: ANN001
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        self.post_count += 1
        response = LLMResponse(
            request_id=f"advisory-{self.post_count}",
            provider="fake-provider",
            model="fake-model",
            text=json.dumps(self.payload),
            structured_output=self.payload,
            response_format="json_object",
            raw_response={"id": f"advisory-{self.post_count}"},
            usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            raw_usage={"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
            usage_present=True,
            usage_complete=True,
            finish_reason="stop",
        )
        accounting_hook.after_response(
            handle,
            request=request,
            response=response,
            latency_ms=1,
        )
        return response


class _UnsupportedDurableOnlineClient:
    def __init__(self) -> None:
        self.provider_io_count = 0

    def generate(self, _request: LLMRequest) -> LLMResponse:
        self.provider_io_count += 1
        raise AssertionError("unsupported client must be rejected before provider I/O")


def _seed_durable_runner_scene(session) -> None:
    session.add(StoryProject(project_id="PROJECT_RUNNER", title="runner", outline_text="outline"))
    session.add(
        ChapterGoal(
            chapter_id="CH_RUNNER",
            project_id="PROJECT_RUNNER",
            planned_scene_count=1,
            chapter_goal="durable accounting",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH_RUNNER_SC01",
            chapter_id="CH_RUNNER",
            project_id="PROJECT_RUNNER",
            scene_seq=1,
            scene_goal="survive a process crash",
            beats_json=["reserve", "dispatch", "settle"],
        )
    )
    session.add(
        SceneRunState(
            scene_id="CH_RUNNER_SC01",
            scene_status="ready",
            active_execution_id="exec-runner",
            run_execution_status="active",
            scene_token_budget=10_000,
            provider_attempt_budget=5,
        )
    )
    session.commit()


def _seed_legacy_runner_scene(session) -> None:
    session.add(StoryProject(project_id="PROJECT100", title="runner", outline_text="outline"))
    session.add(
        ChapterGoal(
            chapter_id="CH100",
            project_id="PROJECT100",
            planned_scene_count=1,
            chapter_goal="runner unit test",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH100_SC01",
            chapter_id="CH100",
            project_id="PROJECT100",
            scene_seq=1,
            scene_goal="exercise the runner",
            beats_json=[],
        )
    )
    session.add(
        SceneRunState(
            scene_id="CH100_SC01",
            scene_status="ready",
            scene_token_budget=50_000,
            provider_attempt_budget=8,
        )
    )
    session.commit()


def _run_durable_runner(
    session,
    client,
    *,
    execution_id: str = "exec-runner",
    run_job_id: str | None = None,
):
    token = begin_llm_execution(execution_id, run_job_id=run_job_id)
    try:
        return LLMNodeRunner(
            session,
            llm_client=client,
            routing_config=_routing_config(),
            settings=_live_settings(),
        ).run(
            scene_id="CH_RUNNER_SC01",
            chapter_id="CH_RUNNER",
            bundle_id="bundle-runner",
            bundle_hash="sha256:runner",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(),
            user_prompt="Scene ID: CH_RUNNER_SC01\nReturn JSON.",
            execution_step_key="neutral_draft",
        )
    finally:
        end_llm_execution(token)


def test_durable_runner_recovers_crash_after_reservation_and_retries_once(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    def crash_after_reservation(stage: str, _attempt_id: str) -> None:
        if stage == "reservation_committed":
            raise _SimulatedProcessCrash()

    runner._accounting_lifecycle_observer = crash_after_reservation
    token = begin_llm_execution("exec-runner")
    try:
        with pytest.raises(_SimulatedProcessCrash):
            runner.run(
                scene_id="CH_RUNNER_SC01",
                chapter_id="CH_RUNNER",
                bundle_id="bundle-runner",
                bundle_hash="sha256:runner",
                node_id="neutral_draft",
                step="neutral_draft",
                prompt=_prompt(),
                user_prompt="Scene ID: CH_RUNNER_SC01\nReturn JSON.",
                execution_step_key="neutral_draft",
            )
    finally:
        end_llm_execution(token)

    recovery = SessionLocal()
    try:
        parent = recovery.execute(select(LlmCall)).scalar_one()
        attempt = recovery.execute(select(LlmCallAttempt)).scalar_one()
        state = recovery.get(SceneRunState, "CH_RUNNER_SC01")
        assert client.post_count == 0
        assert parent.accounting_status == "reserved"
        assert attempt.accounting_status == "reserved"
        assert attempt.request_dispatched_at is None
        assert state.scene_tokens_reserved == attempt.reserved_tokens > 0

        outcome = SceneRunCheckpointService(recovery).reconcile_step_output(
            scene_id="CH_RUNNER_SC01",
            execution_id="exec-runner",
            execution_step_key="neutral_draft",
            output_exists=False,
        )
        recovery.expire_all()
        parent = recovery.get(LlmCall, parent.llm_call_id)
        attempt = recovery.get(LlmCallAttempt, attempt.attempt_id)
        state = recovery.get(SceneRunState, "CH_RUNNER_SC01")
        assert outcome == "retry"
        assert parent.accounting_status == "released"
        assert attempt.accounting_status == "released"
        assert state.scene_tokens_reserved == 0
        assert state.provider_attempts_used == 0
        assert state.scene_tokens_used == 0
    finally:
        recovery.close()

    retry_session = SessionLocal()
    try:
        result = _run_durable_runner(retry_session, client)
        assert result.response.structured_output == {"scene_text": "accounted scene 1"}
        assert client.post_count == 1
    finally:
        retry_session.close()


def test_durable_runner_recovers_dispatch_crash_and_blocks_resend(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    def crash_after_dispatch(stage: str, _attempt_id: str) -> None:
        if stage == "dispatch_committed":
            raise _SimulatedProcessCrash()

    runner._accounting_lifecycle_observer = crash_after_dispatch
    token = begin_llm_execution("exec-runner")
    try:
        with pytest.raises(_SimulatedProcessCrash):
            runner.run(
                scene_id="CH_RUNNER_SC01",
                chapter_id="CH_RUNNER",
                bundle_id="bundle-runner",
                bundle_hash="sha256:runner",
                node_id="neutral_draft",
                step="neutral_draft",
                prompt=_prompt(),
                user_prompt="Scene ID: CH_RUNNER_SC01\nReturn JSON.",
                execution_step_key="neutral_draft",
            )
    finally:
        end_llm_execution(token)

    recovery = SessionLocal()
    try:
        parent = recovery.execute(select(LlmCall)).scalar_one()
        attempt = recovery.execute(select(LlmCallAttempt)).scalar_one()
        state = recovery.get(SceneRunState, "CH_RUNNER_SC01")
        assert parent.accounting_status == "reserved"
        assert attempt.request_dispatched_at is not None
        assert state.scene_tokens_reserved == attempt.reserved_tokens > 0
        assert state.provider_attempts_used == 1

        with pytest.raises(DomainError) as missing:
            SceneRunCheckpointService(recovery).reconcile_step_output(
                scene_id="CH_RUNNER_SC01",
                execution_id="exec-runner",
                execution_step_key="neutral_draft",
                output_exists=False,
            )
        assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
        recovery.expire_all()
        parent = recovery.get(LlmCall, parent.llm_call_id)
        attempt = recovery.get(LlmCallAttempt, attempt.attempt_id)
        state = recovery.get(SceneRunState, "CH_RUNNER_SC01")
        assert parent.accounting_status == "failed"
        assert parent.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
        assert attempt.accounting_status == "failed"
        assert state.scene_tokens_reserved == 0
        assert state.provider_attempts_used == 1
        assert state.scene_tokens_used == attempt.estimated_tokens > 0
    finally:
        recovery.close()

    posts_before_retry = client.post_count
    retry_session = SessionLocal()
    try:
        with pytest.raises(LLMNodeExecutionError) as blocked:
            _run_durable_runner(retry_session, client)
        assert blocked.value.error_code == "RUN_CHECKPOINT_OUTPUT_MISSING"
        assert blocked.value.llm_call_id == parent.llm_call_id
        assert client.post_count == posts_before_retry
    finally:
        retry_session.close()


def test_durable_runner_settled_parent_blocks_resend_before_output_checkpoint(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    with pytest.raises(_SimulatedProcessCrash):
        result = _run_durable_runner(session, client)
        assert result.response.structured_output == {"scene_text": "accounted scene 1"}
        raise _SimulatedProcessCrash()

    recovery = SessionLocal()
    try:
        parent = recovery.execute(select(LlmCall)).scalar_one()
        attempt = recovery.execute(select(LlmCallAttempt)).scalar_one()
        state = recovery.get(SceneRunState, "CH_RUNNER_SC01")
        assert parent.accounting_status == "settled"
        assert attempt.accounting_status == "settled"
        assert state.scene_tokens_reserved == 0
        assert state.provider_attempts_used == 1
        assert state.scene_tokens_used == 18
        with pytest.raises(DomainError) as missing:
            SceneRunCheckpointService(recovery).reconcile_step_output(
                scene_id="CH_RUNNER_SC01",
                execution_id="exec-runner",
                execution_step_key="neutral_draft",
                output_exists=False,
            )
        assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    finally:
        recovery.close()

    retry_session = SessionLocal()
    try:
        with pytest.raises(LLMNodeExecutionError) as blocked:
            _run_durable_runner(retry_session, client)
        assert blocked.value.error_code == "LLM_ACCOUNTING_EXECUTION_STEP_EXISTS"
        assert blocked.value.llm_call_id == parent.llm_call_id
        assert client.post_count == 1
    finally:
        retry_session.close()


def test_durable_runner_rejects_unsupported_online_client_before_provider_io(session) -> None:
    _seed_durable_runner_scene(session)
    client = _UnsupportedDurableOnlineClient()

    with pytest.raises(LLMNodeExecutionError) as rejected:
        _run_durable_runner(session, client)

    assert rejected.value.error_code == "LLM_ACCOUNTING_HOOK_UNSUPPORTED"
    assert client.provider_io_count == 0
    parent = session.execute(select(LlmCall)).scalar_one()
    assert parent.llm_call_id == rejected.value.llm_call_id
    assert parent.request_dispatched_at is None
    assert parent.accounting_status == "rejected"


def test_online_run_without_runtime_still_uses_parent_and_physical_attempt_ledger(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    result = runner.run(
        scene_id="CH_RUNNER_SC01",
        chapter_id="CH_RUNNER",
        bundle_id="bundle-runner",
        bundle_hash="sha256:runner",
        node_id="neutral_draft",
        step="neutral_draft",
        prompt=_prompt(),
        user_prompt="Return JSON.",
        execution_step_key="ignored-without-runtime",
    )

    parent = session.execute(select(LlmCall)).scalar_one()
    attempt = session.execute(select(LlmCallAttempt)).scalar_one()
    assert result.llm_call_id == parent.llm_call_id == result.response.llm_call_id
    assert client.post_count == 1
    assert parent.accounting_status == "settled"
    assert parent.execution_id is None
    assert parent.execution_step_key is None
    assert attempt.accounting_status == "settled"
    assert attempt.request_dispatched_at is not None


def test_online_run_without_runtime_rejects_hookless_client_before_provider_io(session) -> None:
    _seed_durable_runner_scene(session)
    client = _UnsupportedDurableOnlineClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    with pytest.raises(LLMNodeExecutionError) as rejected:
        runner.run(
            scene_id="CH_RUNNER_SC01",
            chapter_id="CH_RUNNER",
            bundle_id="bundle-runner",
            bundle_hash="sha256:runner",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(),
            user_prompt="Return JSON.",
        )

    assert rejected.value.error_code == "LLM_ACCOUNTING_HOOK_UNSUPPORTED"
    assert client.provider_io_count == 0
    parent = session.execute(select(LlmCall)).scalar_one()
    assert parent.accounting_status == "rejected"
    assert parent.request_dispatched_at is None
    assert session.execute(select(LlmCallAttempt)).scalars().all() == []


def test_run_task_requires_explicit_context_without_a_default() -> None:
    parameter = inspect.signature(LLMNodeRunner.run_task).parameters["context"]
    assert parameter.default is inspect.Parameter.empty


def test_real_only_runner_contract_has_no_local_execution_escape_hatch() -> None:
    run_parameters = inspect.signature(LLMNodeRunner.run).parameters
    init_parameters = inspect.signature(LLMNodeRunner.__init__).parameters

    assert "offline_client_factory" not in run_parameters
    assert "offline_client_factory" not in init_parameters
    assert all("offline" not in name and "demo" not in name for name in run_parameters)
    assert all("offline" not in name and "demo" not in name for name in init_parameters)


def test_settings_contract_has_no_local_llm_execution_switch() -> None:
    settings = _live_settings()
    field_names = {field.name for field in fields(Settings)}

    assert "llm_offline_demo_enabled" not in field_names
    assert "allow_offline_demo" not in field_names
    assert not hasattr(settings, "llm_offline_demo_enabled")
    assert not hasattr(settings, "allow_offline_demo")


def test_all_production_run_task_calls_pass_explicit_context() -> None:
    source_root = Path(__file__).parents[1] / "src" / "novel_system"
    calls: list[tuple[str, int]] = []
    offenders: list[tuple[str, int]] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run_task"
            ):
                continue
            location = (str(path.relative_to(source_root)), node.lineno)
            calls.append(location)
            if not any(keyword.arg == "context" for keyword in node.keywords):
                offenders.append(location)

    assert len(calls) == 3
    assert offenders == []


def test_legacy_accounting_bypasses_are_removed_from_production() -> None:
    assert not hasattr(LLMNodeRunner, "_persist_call")

    source_root = Path(__file__).parents[1] / "src" / "novel_system"
    offenders: list[tuple[str, int]] = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "record_usage":
                offenders.append((str(path.relative_to(source_root)), node.lineno))
            if isinstance(node.func, ast.Attribute) and node.func.attr == "record_usage":
                offenders.append((str(path.relative_to(source_root)), node.lineno))

    assert offenders == []


def test_only_the_eleven_verified_scene_run_calls_may_derive_context() -> None:
    source_root = Path(__file__).parents[1] / "src" / "novel_system"
    allowed_without_context = {
        ("services/near_final.py", "NearFinalPlanningService", "_generate_chapter_architecture"),
        ("services/near_final.py", "NearFinalPlanningService", "_generate_character_pressure"),
        ("services/near_final.py", "NearFinalAcceptanceService", "evaluate_scene"),
        ("services/scene_generation.py", "SceneGenerationService", "generate_neutral_draft"),
        ("services/scene_generation.py", "SceneGenerationService", "_run_style_generation"),
        ("services/scene_generation.py", "SceneGenerationService", "_run_de_template_pass"),
        ("services/scene_generation.py", "SceneGenerationService", "_run_style_salvage_pass"),
        ("services/scene_blueprint.py", "SceneBlueprintService", "generate"),
        # Hard/Soft QC 的 LLM 调用已收敛为模块级统一降级出口（两引擎共用一个 .run( 调用点）
        ("services/qc_engine.py", "", "_qc_run_node_with_degradation"),
    }
    calls: list[tuple[str, str, str, bool]] = []

    class RunVisitor(ast.NodeVisitor):
        def __init__(self, relative_path: str) -> None:
            self.relative_path = relative_path
            self.class_name = ""
            self.function_name = ""

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous = self.class_name
            self.class_name = node.name
            self.generic_visit(node)
            self.class_name = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous = self.function_name
            self.function_name = node.name
            self.generic_visit(node)
            self.function_name = previous

        def visit_Call(self, node: ast.Call) -> None:
            keyword_names = {keyword.arg for keyword in node.keywords}
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and {
                    "scene_id",
                    "chapter_id",
                    "bundle_id",
                    "bundle_hash",
                    "node_id",
                    "step",
                    "prompt",
                    "user_prompt",
                }
                <= keyword_names
            ):
                calls.append(
                    (
                        self.relative_path,
                        self.class_name,
                        self.function_name,
                        "context" in keyword_names,
                    )
                )
            self.generic_visit(node)

    for path in source_root.rglob("*.py"):
        relative_path = path.relative_to(source_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        RunVisitor(relative_path).visit(tree)

    assert len(calls) == 14
    actual_without_context = {(path, class_name, function_name) for path, class_name, function_name, has_context in calls if not has_context}
    assert actual_without_context == allowed_without_context


def test_online_run_task_uses_parent_and_physical_attempt_ledger(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="neutral_draft",
        step="advisory:test",
    )

    response = runner.run_task(
        task_name="neutral_draft",
        prompt_text="Return JSON.",
        system_prompt="You are a critic.",
        context=context,
    )

    parent = session.get(LlmCall, response.llm_call_id)
    attempt = session.execute(select(LlmCallAttempt)).scalar_one()
    assert client.post_count == 1
    assert parent.scope_type == "scene"
    assert parent.step == "advisory:test"
    assert parent.accounting_status == "settled"
    assert attempt.llm_call_id == parent.llm_call_id
    assert attempt.accounting_status == "settled"


@pytest.mark.parametrize(
    ("kind", "node_id", "payload"),
    [
        (
            "critique",
            "soft_qc",
            {
                "should_rewrite": True,
                "issues": [
                    {
                        "dimension": "pacing",
                        "directive": "tighten the turn",
                        "evidence": "the turn arrives late",
                    }
                ],
            },
        ),
        (
            "prose",
            "extraction",
            {
                "events": [
                    {
                        "event_type": "character_state",
                        "entity_id": "林远",
                        "fact_key": "injury",
                        "fact_value": "右臂截断",
                        "evidence": "右臂被斩断",
                    }
                ]
            },
        ),
    ],
)
def test_advisory_helper_real_online_boundary_persists_strict_product_ledger(
    session,
    kind: str,
    node_id: str,
    payload: dict,
) -> None:
    _seed_durable_runner_scene(session)
    task_config = _routing_config().node_routing["neutral_draft"]
    routing = ModelRoutingConfig(
        node_routing={node_id: task_config},
        task_routing={node_id: task_config},
        retry_budget={},
        job_runtime={},
    )
    client = _AdvisoryAccountedClient(payload)
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=routing,
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id=node_id,
        step=f"advisory:{kind}",
    )

    if kind == "critique":
        from novel_system.services.auto_critique import llm_auto_critique

        product = llm_auto_critique(
            "clean prose",
            session=session,
            llm_runner=runner,
            llm_context=context,
        )
        assert product.outcome == "completed"
    else:
        from novel_system.services.prose_event_extractor import extract_events_from_prose

        product = extract_events_from_prose(
            "林远的右臂被斩断。",
            session=session,
            llm_runner=runner,
            llm_context=context,
        )
        assert product.outcome == "completed_events"

    parent = session.get(LlmCall, product.llm_call_id)
    attempts = session.execute(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == product.llm_call_id)
    ).scalars().all()
    state = session.get(SceneRunState, "CH_RUNNER_SC01")
    assert client.post_count == 1
    assert parent.accounting_status == "settled"
    assert parent.request_payload_summary["_accounting_provider_execution_mode"] == "online"
    assert len(attempts) == 1
    assert attempts[0].accounting_status == "settled"
    assert state.scene_tokens_reserved == 0


def test_auto_critique_real_transport_failure_returns_durable_failed_product(session) -> None:
    from novel_system.services.auto_critique import llm_auto_critique

    _seed_durable_runner_scene(session)
    task_config = _routing_config().node_routing["neutral_draft"]
    runner = LLMNodeRunner(
        session,
        llm_client=FailingClient(),
        routing_config=ModelRoutingConfig(
            node_routing={"soft_qc": task_config},
            task_routing={"soft_qc": task_config},
            retry_budget={},
            job_runtime={},
        ),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="soft_qc",
        step="advisory:critique-failure",
    )

    product = llm_auto_critique(
        "prose",
        session=session,
        llm_runner=runner,
        llm_context=context,
    )

    parent = session.get(LlmCall, product.llm_call_id)
    attempt = session.execute(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == product.llm_call_id)
    ).scalar_one()
    state = session.get(SceneRunState, "CH_RUNNER_SC01")
    assert product.outcome == "provider_failed"
    assert product.error_code == "LLM_HTTP_REQUEST_FAILED"
    assert parent.accounting_status == "failed"
    assert attempt.accounting_status == "failed"
    assert attempt.request_dispatched_at is not None
    assert state.scene_tokens_reserved == 0


def test_prose_helper_real_physical_gate_rejection_has_zero_provider_io(
    session, monkeypatch
) -> None:
    from novel_system.services.prose_event_extractor import extract_events_from_prose

    _seed_durable_runner_scene(session)
    state = session.get(SceneRunState, "CH_RUNNER_SC01")
    monkeypatch.setattr(
        "novel_system.services.llm_accounting._consume_scene_provider_attempt",
        lambda *_args, **_kwargs: False,
    )
    task_config = _routing_config().node_routing["neutral_draft"]
    client = _AdvisoryAccountedClient({"events": []})
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=ModelRoutingConfig(
            node_routing={"extraction": task_config},
            task_routing={"extraction": task_config},
            retry_budget={},
            job_runtime={},
        ),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="extraction",
        step="advisory:prose-gate",
    )

    product = extract_events_from_prose(
        "prose",
        session=session,
        llm_runner=runner,
        llm_context=context,
    )

    parent = session.get(LlmCall, product.llm_call_id)
    attempt = session.execute(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == product.llm_call_id)
    ).scalar_one()
    session.refresh(state)
    assert product.outcome == "rejected_before_dispatch"
    assert product.error_code == "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED"
    assert client.post_count == 0
    assert parent.accounting_status == "rejected"
    assert attempt.accounting_status == "rejected"
    assert attempt.request_dispatched_at is None
    assert state.scene_tokens_reserved == 0


def test_run_task_business_attempt_budget_exhaustion_has_zero_provider_io(session) -> None:
    _seed_durable_runner_scene(session)
    state = session.get(SceneRunState, "CH_RUNNER_SC01")
    state.total_attempt_count = state.attempt_budget
    session.commit()
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="neutral_draft",
        step="advisory:attempt-exhausted",
    )

    with pytest.raises(LLMNodeExecutionError) as rejected:
        runner.run_task(
            task_name="neutral_draft",
            prompt_text="Return JSON.",
            system_prompt="You are a critic.",
            context=context,
        )

    assert rejected.value.error_code == "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED"
    assert client.post_count == 0
    assert session.execute(select(LlmCallAttempt)).scalars().all() == []
    assert session.execute(select(LlmCall)).scalar_one().accounting_status == "rejected"


def test_run_task_rejects_context_node_that_does_not_match_resolved_route(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="tampered_node",
        step="advisory:wrong-node",
    )

    with pytest.raises(LLMAccountingRejected) as rejected:
        runner.run_task(
            task_name="neutral_draft",
            prompt_text="Return JSON.",
            system_prompt="You are a critic.",
            context=context,
        )

    assert rejected.value.code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert client.post_count == 0
    assert session.execute(select(LlmCall)).scalars().all() == []


def test_run_task_provider_failure_exposes_the_real_parent_call_id(session) -> None:
    _seed_durable_runner_scene(session)
    runner = LLMNodeRunner(
        session,
        llm_client=FailingClient(),
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="neutral_draft",
        step="advisory:provider-failure",
    )

    with pytest.raises(LLMNodeExecutionError) as failed:
        runner.run_task(
            task_name="neutral_draft",
            prompt_text="Return JSON.",
            system_prompt="You are a critic.",
            context=context,
        )

    parent = session.get(LlmCall, failed.value.llm_call_id)
    assert parent is not None
    assert parent.error_code == "LLM_HTTP_REQUEST_FAILED"
    assert parent.accounting_status == "failed"
    assert session.execute(select(LlmCallAttempt)).scalar_one().llm_call_id == parent.llm_call_id


@pytest.mark.parametrize(
    "tampered_context",
    [
        {"project_id": "PROJECT_TAMPERED"},
    ],
)
def test_explicit_scene_context_tampering_is_rejected_before_provider_io(
    session,
    tampered_context: dict[str, str],
) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = replace(
        LLMCallContext(
            scope_type="scene",
            scope_id="CH_RUNNER_SC01",
            project_id="PROJECT_RUNNER",
            chapter_id="CH_RUNNER",
            scene_id="CH_RUNNER_SC01",
            node_id="neutral_draft",
            step="neutral_draft",
        ),
        **tampered_context,
    )

    with pytest.raises(LLMNodeExecutionError) as rejected:
        runner.run(
            scene_id="CH_RUNNER_SC01",
            chapter_id="CH_RUNNER",
            bundle_id="bundle-runner",
            bundle_hash="sha256:runner",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(),
            user_prompt="Return JSON.",
            context=context,
        )

    assert rejected.value.error_code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert client.post_count == 0
    assert session.execute(select(LlmCall)).scalars().all() == []


def test_explicit_chapter_context_records_chapter_owned_parent_without_synthetic_scene(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="chapter",
        scope_id="CH_RUNNER",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        node_id="neutral_draft",
        step="chapter_evaluate",
    )

    result = runner.run(
        scene_id=None,
        chapter_id="CH_RUNNER",
        bundle_id="bundle-chapter",
        bundle_hash="sha256:chapter",
        node_id="neutral_draft",
        step="chapter_evaluate",
        prompt=_prompt(),
        user_prompt="Evaluate the chapter.",
        context=context,
    )

    parent = session.get(LlmCall, result.llm_call_id)
    assert client.post_count == 1
    assert (parent.scope_type, parent.scope_id) == ("chapter", "CH_RUNNER")
    assert parent.scene_id is None
    assert parent.chapter_id == "CH_RUNNER"


def test_explicit_scene_context_rejects_none_scene_before_provider_io(session) -> None:
    _seed_durable_runner_scene(session)
    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="scene",
        scope_id="CH_RUNNER_SC01",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        scene_id="CH_RUNNER_SC01",
        node_id="neutral_draft",
        step="neutral_draft",
    )

    with pytest.raises(LLMNodeExecutionError) as rejected:
        runner.run(
            scene_id=None,
            chapter_id="CH_RUNNER",
            bundle_id="bundle-runner",
            bundle_hash="sha256:runner",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(),
            user_prompt="Return JSON.",
            context=context,
        )

    assert rejected.value.error_code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert client.post_count == 0
    assert session.execute(select(LlmCall)).scalars().all() == []


def test_durable_online_intent_with_missing_scene_fails_before_provider_io(session) -> None:
    client = RecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    token = begin_llm_execution("exec-missing-scene")
    try:
        with pytest.raises(LLMNodeExecutionError) as rejected:
            runner.run(
                scene_id="MISSING_SCENE",
                chapter_id="MISSING_CHAPTER",
                bundle_id="bundle-runner",
                bundle_hash="sha256:runner",
                node_id="neutral_draft",
                step="neutral_draft",
                prompt=_prompt(),
                user_prompt="Return JSON.",
                execution_step_key="neutral_draft",
            )
    finally:
        end_llm_execution(token)

    assert rejected.value.error_code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert client.requests == []
    assert session.execute(select(LlmCall)).scalars().all() == []


def test_active_scene_job_is_recorded_and_cannot_be_omitted_before_provider(session) -> None:
    _seed_durable_runner_scene(session)
    job_id = "scene-job-runner"
    session.add(
        ChapterRunJob(
            job_id=job_id,
            chapter_id="CH_RUNNER",
            scene_id="CH_RUNNER_SC01",
            status="running",
            job_type="scene_run_full",
            payload_json={"scene_id": "CH_RUNNER_SC01"},
        )
    )
    state = session.get(SceneRunState, "CH_RUNNER_SC01")
    state.active_run_job_id = job_id
    session.commit()

    client = _AccountedRecordingClient()
    result = _run_durable_runner(session, client, run_job_id=job_id)
    assert session.get(LlmCall, result.llm_call_id).run_job_id == job_id
    assert client.post_count == 1

    chapter_client = _AccountedRecordingClient()
    chapter_runner = LLMNodeRunner(
        session,
        llm_client=chapter_client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    chapter_context = LLMCallContext(
        scope_type="chapter",
        scope_id="CH_RUNNER",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        node_id="neutral_draft",
        step="chapter_evaluate",
        execution_id="exec-scene-job-chapter-evaluate",
        execution_step_key="chapter_evaluate",
        run_job_id=job_id,
    )
    token = begin_llm_execution("exec-scene-job-chapter-evaluate", run_job_id=job_id)
    try:
        chapter_result = chapter_runner.run(
            scene_id="chapter_eval_CH_RUNNER",
            chapter_id="CH_RUNNER",
            bundle_id="bundle-chapter",
            bundle_hash="sha256:chapter",
            node_id="neutral_draft",
            step="chapter_evaluate",
            prompt=_prompt(),
            user_prompt="Evaluate the chapter.",
            context=chapter_context,
            execution_step_key="chapter_evaluate",
        )
    finally:
        end_llm_execution(token)
    assert session.get(LlmCall, chapter_result.llm_call_id).run_job_id == job_id
    assert chapter_client.post_count == 1

    omitted_client = _AccountedRecordingClient()
    with pytest.raises(LLMNodeExecutionError) as omitted:
        _run_durable_runner(
            session,
            omitted_client,
            execution_id="exec-runner-omitted-job",
        )
    assert omitted.value.error_code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert omitted_client.post_count == 0


def test_chapter_job_owns_scene_and_chapter_scoped_calls(session) -> None:
    _seed_durable_runner_scene(session)
    job_id = "chapter-job-runner"
    session.add(
        ChapterRunJob(
            job_id=job_id,
            chapter_id="CH_RUNNER",
            status="running",
            job_type="chapter_run_full",
            payload_json={
                "scene_ids": ["CH_RUNNER_SC01"],
                "current_scene_id": "CH_RUNNER_SC01",
            },
        )
    )
    session.get(SceneRunState, "CH_RUNNER_SC01").active_run_job_id = job_id
    session.commit()

    scene_client = _AccountedRecordingClient()
    scene_result = _run_durable_runner(session, scene_client, run_job_id=job_id)
    assert session.get(LlmCall, scene_result.llm_call_id).run_job_id == job_id

    chapter_client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=chapter_client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    context = LLMCallContext(
        scope_type="chapter",
        scope_id="CH_RUNNER",
        project_id="PROJECT_RUNNER",
        chapter_id="CH_RUNNER",
        node_id="neutral_draft",
        step="chapter_evaluate",
        execution_id="exec-chapter-evaluate",
        execution_step_key="chapter_evaluate",
        run_job_id=job_id,
    )
    token = begin_llm_execution("exec-chapter-evaluate", run_job_id=job_id)
    try:
        chapter_result = runner.run(
            scene_id="chapter_eval_CH_RUNNER",
            chapter_id="CH_RUNNER",
            bundle_id="bundle-chapter",
            bundle_hash="sha256:chapter",
            node_id="neutral_draft",
            step="chapter_evaluate",
            prompt=_prompt(),
            user_prompt="Evaluate the chapter.",
            context=context,
            execution_step_key="chapter_evaluate",
        )
    finally:
        end_llm_execution(token)
    assert session.get(LlmCall, chapter_result.llm_call_id).run_job_id == job_id
    assert chapter_client.post_count == 1


@pytest.mark.parametrize("missing_field", ["project", "execution", "step"])
def test_durable_online_intent_rejects_incomplete_accounting_context_before_provider(
    session,
    missing_field: str,
) -> None:
    if missing_field == "project":
        session.add(
            ChapterGoal(
                chapter_id="CH_NO_PROJECT",
                planned_scene_count=1,
                chapter_goal="missing project",
            )
        )
        session.add(
            SceneCard(
                scene_id="CH_NO_PROJECT_SC01",
                chapter_id="CH_NO_PROJECT",
                scene_seq=1,
                scene_goal="must reject",
                beats_json=[],
            )
        )
        session.add(
            SceneRunState(
                scene_id="CH_NO_PROJECT_SC01",
                scene_status="ready",
                active_execution_id="exec-context",
                run_execution_status="active",
                scene_token_budget=10_000,
                provider_attempt_budget=5,
            )
        )
        session.commit()
        scene_id = "CH_NO_PROJECT_SC01"
        chapter_id = "CH_NO_PROJECT"
    else:
        _seed_durable_runner_scene(session)
        scene_id = "CH_RUNNER_SC01"
        chapter_id = "CH_RUNNER"

    client = _AccountedRecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )
    execution_id = "" if missing_field == "execution" else "exec-context"
    step = "" if missing_field == "step" else "neutral_draft"
    token = begin_llm_execution(execution_id)
    try:
        with pytest.raises(LLMNodeExecutionError) as rejected:
            runner.run(
                scene_id=scene_id,
                chapter_id=chapter_id,
                bundle_id="bundle-runner",
                bundle_hash="sha256:runner",
                node_id="neutral_draft",
                step=step,
                prompt=_prompt(),
                user_prompt="Return JSON.",
                execution_step_key=step,
            )
    finally:
        end_llm_execution(token)

    assert rejected.value.error_code == "LLM_ACCOUNTING_CONTEXT_INVALID"
    assert client.post_count == 0
    assert session.execute(select(LlmCall)).scalars().all() == []


def test_unexpected_request_build_failure_is_not_masked_by_online_accounting(
    session,
    monkeypatch,
) -> None:
    _seed_legacy_runner_scene(session)
    client = RecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    def fail_request_build(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("unexpected request construction failure")

    monkeypatch.setattr(runner, "_build_request", fail_request_build)
    token = begin_llm_execution("exec-request-build-fault")
    try:
        with pytest.raises(LLMNodeExecutionError) as failed:
            runner.run(
                scene_id="CH100_SC01",
                chapter_id="CH100",
                bundle_id="bundle-runner",
                bundle_hash="sha256:runner",
                node_id="neutral_draft",
                step="neutral_draft",
                prompt=_prompt(),
                user_prompt="Return JSON.",
                execution_step_key="neutral_draft",
            )
    finally:
        end_llm_execution(token)

    assert failed.value.error_code == "RuntimeError"
    assert isinstance(failed.value.original_error, RuntimeError)
    assert str(failed.value.original_error) == "unexpected request construction failure"
    assert client.requests == []


def test_llm_node_runner_builds_request_and_persists_successful_call(session) -> None:
    _seed_legacy_runner_scene(session)
    client = RecordingClient()
    runner = LLMNodeRunner(session, llm_client=client, routing_config=_routing_config())

    result = runner.run(
        scene_id="CH100_SC01",
        chapter_id="CH100",
        bundle_id="bundle_CH100_SC01",
        bundle_hash="bundle_hash_CH100_SC01",
        node_id="neutral_draft",
        step="neutral_draft",
        prompt=_prompt(),
        user_prompt="Scene ID: CH100_SC01\nReturn JSON.",
    )
    session.commit()

    stored_call = session.execute(select(LlmCall)).scalars().one()

    assert result.llm_call_id == stored_call.llm_call_id
    assert client.requests[0].node_id == "neutral_draft"
    assert client.requests[0].provider_id == "provider_primary"
    assert stored_call.error_code is None
    assert stored_call.request_payload_summary["template_name"] == "neutral_draft"
    assert stored_call.request_payload_summary["token_budget"]["estimated_input_tokens"] > 0
    assert stored_call.request_payload_summary["bundle_id"] == "bundle_CH100_SC01"
    assert stored_call.request_payload_summary["messages"]["count"] == 2
    assert stored_call.request_payload_summary["messages"]["items"][1]["role"] == "user"
    assert (
        stored_call.request_payload_summary["messages"]["items"][1]["content"]["kind"]
        == "text_fingerprint"
    )
    assert "Scene ID: CH100_SC01" not in json.dumps(
        stored_call.request_payload_summary,
        ensure_ascii=False,
    )
    assert stored_call.response_payload_summary["request_id"] == fingerprint_identifier(
        "resp_success"
    )
    assert stored_call.response_payload_summary["attempt_count"] == 2
    assert stored_call.response_payload_summary["max_retries"] == 3
    assert stored_call.response_payload_summary["retryable"] is False


def test_llm_node_runner_never_borrows_scene_blueprint_route_from_other_node(session) -> None:
    runner = LLMNodeRunner(session, routing_config=_routing_config(), settings=_live_settings())

    with pytest.raises(KeyError, match="scene_blueprint"):
        runner.task_config("scene_blueprint")


def test_llm_node_runner_live_mode_blocks_missing_direct_node_route(session) -> None:
    _seed_legacy_runner_scene(session)
    client = RecordingClient()
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=_routing_config(),
        settings=_live_settings(),
    )

    with pytest.raises(LLMNodeExecutionError) as exc_info:
        runner.run(
            scene_id="CH100_SC01",
            chapter_id="CH100",
            bundle_id="scene_blueprint_source_CH100_SC01",
            bundle_hash="bundle_hash_CH100_SC01",
            node_id="scene_blueprint",
            step="scene_blueprint",
            prompt={**_prompt(), "template_name": "scene_blueprint"},
            user_prompt="Scene ID: CH100_SC01\nReturn a scene blueprint JSON object.",
        )

    assert client.requests == []
    assert exc_info.value.error_code == "LLM_ROUTE_NOT_CONFIGURED"
    assert "scene_blueprint" in exc_info.value.message


def test_llm_node_runner_live_mode_does_not_inherit_legacy_stylize_route(session) -> None:
    _seed_legacy_runner_scene(session)
    client = RecordingClient()
    stylize_config = TaskModelConfig(
        provider="openai_compatible",
        model="fake-style-model",
        temperature=0.7,
        max_output_tokens=120,
        response_format="json_object",
        provider_id="provider_primary",
        reasoning_level="medium",
        api_mode="responses",
    )
    runner = LLMNodeRunner(
        session,
        llm_client=client,
        routing_config=ModelRoutingConfig(
            node_routing={},
            task_routing={"stylize": stylize_config},
            retry_budget={},
            job_runtime={},
        ),
        settings=_live_settings(),
    )

    with pytest.raises(LLMNodeExecutionError) as exc_info:
        runner.run(
            scene_id="CH100_SC01",
            chapter_id="CH100",
            bundle_id="bundle_CH100_SC01",
            bundle_hash="bundle_hash_CH100_SC01",
            node_id="style_draft",
            step="style_draft",
            prompt={**_prompt(), "template_name": "style_draft"},
            user_prompt="Scene ID: CH100_SC01\nReturn a stylized scene JSON object.",
        )

    assert client.requests == []
    assert exc_info.value.error_code == "LLM_ROUTE_NOT_CONFIGURED"
    assert "style_draft" in exc_info.value.message


def test_llm_node_runner_never_borrows_rewrite_route_from_other_node(session) -> None:
    runner = LLMNodeRunner(session, routing_config=_routing_config(), settings=_live_settings())

    with pytest.raises(KeyError, match="scene_literary_rewrite"):
        runner.task_config("scene_literary_rewrite")


def test_llm_node_runner_persists_failed_provider_call_with_retry_metadata(session) -> None:
    _seed_legacy_runner_scene(session)
    runner = LLMNodeRunner(session, llm_client=FailingClient(), routing_config=_routing_config())

    with pytest.raises(LLMNodeExecutionError) as exc_info:
        runner.run(
            scene_id="CH100_SC01",
            chapter_id="CH100",
            bundle_id="bundle_CH100_SC01",
            bundle_hash="bundle_hash_CH100_SC01",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(),
            user_prompt="Scene ID: CH100_SC01\nReturn JSON.",
        )
    session.commit()

    stored_call = session.execute(select(LlmCall)).scalars().one()

    assert exc_info.value.llm_call_id == stored_call.llm_call_id
    assert exc_info.value.error_code == "LLM_HTTP_REQUEST_FAILED"
    assert exc_info.value.retryable is True
    assert stored_call.error_code == "LLM_HTTP_REQUEST_FAILED"
    assert stored_call.response_payload_summary["retryable"] is True
    assert stored_call.response_payload_summary["attempt_count"] == 3
    assert stored_call.response_payload_summary["max_retries"] == 2
    assert stored_call.response_payload_summary["details"]["attempt_count"] == 3
    assert stored_call.response_payload_summary["details"]["max_retries"] == 2


def test_llm_node_runner_persists_continuity_failure_before_provider_call(session) -> None:
    _seed_legacy_runner_scene(session)
    client = RecordingClient()
    runner = LLMNodeRunner(session, llm_client=client, routing_config=_routing_config())

    with pytest.raises(LLMNodeContinuityError) as exc_info:
        runner.run(
            scene_id="CH100_SC01",
            chapter_id="CH100",
            bundle_id="bundle_CH100_SC01",
            bundle_hash="bundle_hash_CH100_SC01",
            node_id="neutral_draft",
            step="neutral_draft",
            prompt=_prompt(target_input_tokens=20),
            user_prompt=" ".join(["oversized prompt"] * 100),
        )
    session.commit()

    stored_call = session.execute(select(LlmCall)).scalars().one()

    assert client.requests == []
    assert exc_info.value.llm_call_id == stored_call.llm_call_id
    assert exc_info.value.continuity_warning["requires_scene_split"] is True
    assert stored_call.error_code == "CONTINUITY_BUDGET_EXCEEDED"
    assert stored_call.request_payload_summary["continuity_warning"]["requires_scene_split"] is True
    assert stored_call.response_payload_summary["attempt_count"] == 0
    assert stored_call.response_payload_summary["max_retries"] == 0
    assert stored_call.response_payload_summary["retryable"] is False
    assert stored_call.response_payload_summary["continuity_warning"]["requires_scene_split"] is True
