from __future__ import annotations

import json
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from novel_system.db.models import (
    AttemptTracker,
    ChapterGoal,
    ChapterMemory,
    ChapterRunJob,
    ChapterRollingNote,
    ChapterState,
    FinalScene,
    GenerationPlanningArtifact,
    HumanReviewEvent,
    LlmCall,
    LlmCallAttempt,
    NarrativeEvent,
    QcReport,
    RelationProfile,
    RevisionCandidate,
    SceneCard,
    SceneBlueprint,
    SceneBundle,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StoryProject,
    VoiceProfile,
    VolumeSummary,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.aggregator import Aggregator
from novel_system.db.session import SessionLocal
from novel_system.services.llm_client import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    OnlineAccountedExecution,
)
from novel_system.services.llm_accounting import LLMAccountingError
from novel_system.services.llm_task_runner import (
    UNBOUNDED_TIMEOUT_LEASE_SECONDS,
    _execution_owner_heartbeat,
    _execution_owner_lease_seconds,
    LLMNodeExecutionError,
    LLMNodeRunner,
    begin_llm_execution,
    end_llm_execution,
)
from novel_system.services.idempotency import owner_lease_grace_seconds, owner_lease_ttl_seconds
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.near_final import NearFinalPlanningService
from novel_system.services.qc_engine import HardQcEngine, SoftQcEngine
from novel_system.services.qc_engine import SoftQcDecision
from novel_system.services.scene_generation import SceneGenerationService, StyleGenerationResult
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_run_checkpoint import (
    RUN_CHECKPOINT_ORDER,
    SceneRunCheckpointService,
    chapter_scene_execution_id,
    idempotency_execution_id,
    scene_job_execution_id,
)
from tests.real_llm_fakes import ScenePipelineOnlineFake


def test_checkpoint_draft_invalid_ledger_returns_domain_error_instead_of_name_error(monkeypatch) -> None:
    orchestrator = object.__new__(Orchestrator)
    orchestrator.session = SimpleNamespace(get=lambda _model, _row_id: SimpleNamespace())
    refs = {
        "neutral_row": "draft-row",
        "neutral_llm_call_id": "llm-call",
        "neutral_execution_step_key": "neutral_draft",
        "neutral_artifact_execution_id": "execution-id",
    }
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_artifact",
        lambda key, **_kwargs: refs[key],
    )
    monkeypatch.setattr(orchestrator, "_load_checkpoint_bundle", lambda _scene_id: {"bundle_id": "bundle"})
    monkeypatch.setattr(orchestrator, "_validate_artifact_execution_owner", lambda value: value)
    monkeypatch.setattr(
        orchestrator,
        "_validate_checkpoint_llm_output",
        lambda **_kwargs: SimpleNamespace(),
    )

    def invalid_ledger(_parent) -> None:
        raise LLMAccountingError("LEDGER_INVALID", "ledger invalid")

    monkeypatch.setattr(orchestrator, "_validate_settled_parent_ledger", invalid_ledger)

    with pytest.raises(DomainError) as exc_info:
        orchestrator._load_checkpoint_draft(
            "scene-id",
            ref_key="neutral_row",
            expected_stage="neutral",
            expected_node_at_least="neutral_draft",
            result_type="neutral",
        )

    assert exc_info.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert exc_info.value.message == "neutral checkpoint generation attempt ledger is invalid"
    assert exc_info.value.details == {"llm_call_id": "llm-call", "error_code": "LEDGER_INVALID"}


@pytest.fixture(autouse=True)
def _accounted_online_default_orchestrator_runner(monkeypatch) -> None:
    """Exercise default pipeline nodes through an accounted online test provider."""

    monkeypatch.setattr(
        "novel_system.services.orchestrator.LLMNodeRunner",
        lambda session: LLMNodeRunner(
            session,
            llm_client=ScenePipelineOnlineFake(),
        ),
    )


def _state(session, *, scene_id: str = "SC_CHECKPOINT") -> SceneRunState:
    project_id = f"P_{scene_id}"
    chapter_id = f"CH_{scene_id}"
    session.add(StoryProject(project_id=project_id, title="Checkpoint", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            planned_scene_count=1,
            chapter_goal="checkpoint",
        )
    )
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="checkpoint",
        )
    )
    state = SceneRunState(scene_id=scene_id)
    session.add(state)
    session.commit()
    return state


def test_execution_ids_are_stable_at_each_entrypoint() -> None:
    assert idempotency_execution_id("request-123") == "idempotency:request-123"
    assert scene_job_execution_id("scene_job_123") == "scene_job_123"
    assert chapter_scene_execution_id("chapter_job_123", "SC01") == "chapter_job_123:SC01"


def test_same_execution_resumes_after_last_durable_checkpoint(session) -> None:
    _state(session)
    checkpoints = SceneRunCheckpointService(session)

    first = checkpoints.acquire_execution("SC_CHECKPOINT", "idempotency:req-1")
    assert first.resumed is False
    assert first.next_node == "budget_ready"

    checkpoints.save_checkpoint(
        scene_id="SC_CHECKPOINT",
        execution_id="idempotency:req-1",
        node_key="budget_ready",
        artifact_refs={"budget_basis_hash": "sha256:budget"},
    )
    session.commit()

    resumed = checkpoints.acquire_execution("SC_CHECKPOINT", "idempotency:req-1")
    assert resumed.resumed is True
    assert resumed.last_node == "budget_ready"
    assert resumed.next_node == "planning_ready"
    assert resumed.checkpoint_json["artifact_refs"] == {"budget_basis_hash": "sha256:budget"}


def test_active_execution_blocks_competitor_and_terminal_execution_can_be_superseded(session) -> None:
    state = _state(session)
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-a")
    session.commit()

    with pytest.raises(DomainError) as active_error:
        checkpoints.acquire_execution(state.scene_id, "exec-b")
    assert active_error.value.code == "RUN_EXECUTION_IN_PROGRESS"

    checkpoints.mark_failed(state.scene_id, "exec-a")
    session.commit()
    replacement = checkpoints.acquire_execution(state.scene_id, "exec-b")
    assert replacement.resumed is False
    session.commit()

    with pytest.raises(DomainError) as old_retry:
        checkpoints.acquire_execution(state.scene_id, "exec-a")
    assert old_retry.value.code == "RUN_EXECUTION_SUPERSEDED"


def test_concurrent_execution_cas_has_one_winner_and_old_retry_is_read_only(session) -> None:
    state = _state(session, scene_id="SC_EXECUTION_RACE")
    barrier = Barrier(2)

    def _contend(execution_id: str) -> tuple[str, str]:
        contender = SessionLocal()
        try:
            barrier.wait(timeout=5)
            try:
                SceneRunCheckpointService(contender).acquire_execution(state.scene_id, execution_id)
                contender.commit()
                return ("won", execution_id)
            except OperationalError:
                # SQLite may surface the losing simultaneous write as BUSY before
                # the winning commit becomes visible. Re-read after the lock clears;
                # the durable result must still be the execution-owner fence.
                contender.rollback()
                time.sleep(0.05)
                try:
                    SceneRunCheckpointService(contender).acquire_execution(state.scene_id, execution_id)
                    contender.commit()
                    return ("won", execution_id)
                except DomainError as exc:
                    contender.rollback()
                    return (exc.code, execution_id)
            except DomainError as exc:
                contender.rollback()
                return (exc.code, execution_id)
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(_contend, ("exec-race-a", "exec-race-b")))

    winners = [execution_id for outcome, execution_id in outcomes if outcome == "won"]
    losers = [outcome for outcome, _execution_id in outcomes if outcome != "won"]
    assert len(winners) == 1
    assert losers == ["RUN_EXECUTION_IN_PROGRESS"]
    winner = winners[0]

    session.expire_all()
    state = session.get(SceneRunState, state.scene_id)
    assert state is not None
    assert state.active_execution_id == winner
    state.scene_tokens_used = 31
    state.scene_tokens_reserved = 7
    state.provider_attempts_used = 2
    session.flush()
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.save_checkpoint(
        scene_id=state.scene_id,
        execution_id=winner,
        node_key="budget_ready",
        artifact_refs={"budget_basis_hash": "sha256:race"},
    )
    checkpoints.mark_failed(state.scene_id, winner)
    session.commit()

    replacement = "exec-race-next"
    checkpoints.acquire_execution(state.scene_id, replacement)
    session.commit()
    session.refresh(state)
    snapshot = {
        "active_execution_id": state.active_execution_id,
        "run_checkpoint": state.run_checkpoint,
        "run_checkpoint_json": dict(state.run_checkpoint_json or {}),
        "scene_tokens_used": state.scene_tokens_used,
        "scene_tokens_reserved": state.scene_tokens_reserved,
        "provider_attempts_used": state.provider_attempts_used,
    }

    with pytest.raises(DomainError) as old_retry:
        checkpoints.acquire_execution(state.scene_id, winner)
    assert old_retry.value.code == "RUN_EXECUTION_SUPERSEDED"
    session.rollback()
    session.refresh(state)
    assert {
        "active_execution_id": state.active_execution_id,
        "run_checkpoint": state.run_checkpoint,
        "run_checkpoint_json": dict(state.run_checkpoint_json or {}),
        "scene_tokens_used": state.scene_tokens_used,
        "scene_tokens_reserved": state.scene_tokens_reserved,
        "provider_attempts_used": state.provider_attempts_used,
    } == snapshot


def test_selection_resume_checkpoint_handoff_has_one_cas_owner(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    checkpoints = SceneRunCheckpointService(session)
    old_execution = "idempotency:selection-origin"
    checkpoints.acquire_execution(scene_id, old_execution)
    for node in (
        "budget_ready",
        "planning_ready",
        "bundle_ready",
        "neutral_ready",
        "hard_qc_ready",
        "style_ready",
        "selection_wait",
    ):
        checkpoints.save_checkpoint(
            scene_id=scene_id,
            execution_id=old_execution,
            node_key=node,
            artifact_refs={"selection_context": "durable"} if node == "selection_wait" else None,
        )
    checkpoints.mark_waiting_selection(scene_id, old_execution)
    session.commit()
    barrier = Barrier(2)

    def _resume_contender(execution_id: str) -> tuple[str, str]:
        contender = SessionLocal()
        try:
            barrier.wait(timeout=5)
            try:
                SceneRunCheckpointService(contender).acquire_selection_resume(scene_id, execution_id)
                contender.commit()
                return ("won", execution_id)
            except OperationalError:
                contender.rollback()
                time.sleep(0.05)
                try:
                    SceneRunCheckpointService(contender).acquire_selection_resume(scene_id, execution_id)
                    contender.commit()
                    return ("won", execution_id)
                except DomainError as exc:
                    contender.rollback()
                    return (exc.code, execution_id)
            except DomainError as exc:
                contender.rollback()
                return (exc.code, execution_id)
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                _resume_contender,
                ("idempotency:selection-resume-a", "idempotency:selection-resume-b"),
            )
        )
    winners = [execution_id for outcome, execution_id in outcomes if outcome == "won"]
    assert len(winners) == 1
    assert [outcome for outcome, _ in outcomes if outcome != "won"] == ["RUN_EXECUTION_IN_PROGRESS"]
    winner = winners[0]

    session.expire_all()
    state = session.get(SceneRunState, scene_id)
    assert state.active_execution_id == winner
    assert state.run_execution_status == "active"
    assert state.run_checkpoint == "selection_wait"
    assert state.run_checkpoint_json["execution_id"] == winner
    assert state.run_checkpoint_json["artifact_refs"]["selection_context"] == "durable"
    assert old_execution in state.run_checkpoint_json["superseded_execution_ids"]

    before = dict(state.run_checkpoint_json)
    with pytest.raises(DomainError) as stale_terminal:
        checkpoints.mark_failed(scene_id, old_execution)
    assert stale_terminal.value.code == "RUN_EXECUTION_SUPERSEDED"
    session.rollback()
    session.refresh(state)
    assert state.active_execution_id == winner
    assert state.run_execution_status == "active"
    assert state.run_checkpoint_json == before


def test_failed_post_selection_checkpoint_hands_off_to_a_fresh_resume_owner(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    checkpoints = SceneRunCheckpointService(session)
    origin = "idempotency:selection-origin-budget"
    first_resume = "idempotency:selection-resume-budget-a"
    second_resume = "idempotency:selection-resume-budget-b"
    checkpoints.acquire_execution(scene_id, origin)
    for node in RUN_CHECKPOINT_ORDER[: RUN_CHECKPOINT_ORDER.index("selection_wait") + 1]:
        checkpoints.save_checkpoint(
            scene_id=scene_id,
            execution_id=origin,
            node_key=node,
            artifact_refs={"selection_context": "durable"} if node == "selection_wait" else None,
        )
    checkpoints.mark_waiting_selection(scene_id, origin)
    checkpoints.acquire_selection_resume(scene_id, first_resume)
    checkpoints.save_checkpoint(
        scene_id=scene_id,
        execution_id=first_resume,
        node_key="soft_qc_ready",
        artifact_refs={"post_selection_product": "kept"},
    )
    checkpoints.mark_failed(scene_id, first_resume)

    claimed = checkpoints.acquire_selection_resume(scene_id, second_resume)

    state = session.get(SceneRunState, scene_id)
    assert claimed.resumed is True
    assert state.active_execution_id == second_resume
    assert state.run_execution_status == "active"
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["artifact_refs"]["post_selection_product"] == "kept"
    assert first_resume in state.run_checkpoint_json["artifact_execution_lineage_ids"]
    assert first_resume in state.run_checkpoint_json["superseded_execution_ids"]


def test_checkpoint_rejects_wrong_execution_and_out_of_order_node(session) -> None:
    state = _state(session)
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-a")

    with pytest.raises(DomainError) as wrong_owner:
        checkpoints.save_checkpoint(
            scene_id=state.scene_id,
            execution_id="exec-b",
            node_key="budget_ready",
        )
    assert wrong_owner.value.code == "RUN_EXECUTION_SUPERSEDED"

    with pytest.raises(DomainError) as out_of_order:
        checkpoints.save_checkpoint(
            scene_id=state.scene_id,
            execution_id="exec-a",
            node_key="bundle_ready",
        )
    assert out_of_order.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_cancelled_execution_has_durable_terminal_checkpoint_and_can_be_superseded(session) -> None:
    state = _state(session, scene_id="SC_CANCELLED_CHECKPOINT")
    checkpoints = SceneRunCheckpointService(session)
    checkpoints.acquire_execution(state.scene_id, "exec-cancelled")
    checkpoints.save_checkpoint(
        scene_id=state.scene_id,
        execution_id="exec-cancelled",
        node_key="budget_ready",
        artifact_refs={"scene_token_budget": 100},
    )
    checkpoints.mark_cancelled(state.scene_id, "exec-cancelled")
    session.commit()

    session.refresh(state)
    assert state.run_execution_status == "cancelled"
    assert state.run_checkpoint == "cancelled"
    assert state.run_checkpoint_json["node_key"] == "cancelled"
    assert state.run_checkpoint_json["cancelled_from_node"] == "budget_ready"

    with pytest.raises(DomainError) as same_execution:
        checkpoints.acquire_execution(state.scene_id, "exec-cancelled")
    assert same_execution.value.code == "RUN_EXECUTION_CANCELLED"

    claim = checkpoints.acquire_execution(state.scene_id, "exec-replacement")
    assert claim.resumed is False
    assert claim.last_node is None
    assert "exec-cancelled" in claim.checkpoint_json["superseded_execution_ids"]

    with pytest.raises(DomainError) as stale_owner:
        checkpoints.mark_failed(state.scene_id, "exec-cancelled")
    assert stale_owner.value.code == "RUN_EXECUTION_SUPERSEDED"


def test_settled_or_dispatched_ledger_without_output_is_blocked(session) -> None:
    state = _state(session, scene_id="SC_LEDGER_MISSING")
    state.scene_token_budget = 100
    state.scene_tokens_reserved = 0
    state.scene_tokens_used = 10
    session.add(
        LlmCall(
            llm_call_id="call-settled-missing",
            provider="fake",
            model="fake",
            step="neutral_draft",
            scene_id=state.scene_id,
            scope_type="scene",
            scope_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="neutral_draft",
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=10,
            accounting_status="settled",
            request_dispatched_at="2026-07-13T00:00:00+00:00",
            settled_at="2026-07-13T00:00:01+00:00",
        )
    )
    session.commit()

    with pytest.raises(DomainError) as exc_info:
        SceneRunCheckpointService(session).reconcile_step_output(
            scene_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="neutral_draft",
            output_exists=False,
        )
    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"


def test_undispatched_reservation_is_released_for_checkpoint_retry(session) -> None:
    state = _state(session, scene_id="SC_LEDGER_RELEASE")
    state.scene_token_budget = 100
    state.scene_tokens_reserved = 20
    session.add(
        LlmCall(
            llm_call_id="call-reserved-undispatched",
            provider="fake",
            model="fake",
            step="style_draft",
            scene_id=state.scene_id,
            scope_type="scene",
            scope_id=state.scene_id,
            execution_id="exec-ledger",
            execution_step_key="style_draft:1",
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=0,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-reserved-undispatched",
            llm_call_id="call-reserved-undispatched",
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=10,
            estimated_tokens=20,
            reserved_tokens=20,
            budget_charged_tokens=0,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.commit()

    outcome = SceneRunCheckpointService(session).reconcile_step_output(
        scene_id=state.scene_id,
        execution_id="exec-ledger",
        execution_step_key="style_draft:1",
        output_exists=False,
    )
    session.commit()

    session.refresh(state)
    call = session.get(LlmCall, "call-reserved-undispatched")
    attempt = session.get(LlmCallAttempt, "attempt-reserved-undispatched")
    assert outcome == "retry"
    assert call.accounting_status == "released"
    assert call.reserved_tokens == attempt.reserved_tokens == 20
    assert call.budget_charged_tokens == attempt.budget_charged_tokens == 0
    assert attempt.accounting_status == "released"
    assert attempt.settled_at is not None
    assert state.scene_tokens_reserved == 0


class _AccountedTestClient(OnlineAccountedExecution):
    def generate_accounted(self, request: LLMRequest, *, accounting_hook) -> LLMResponse:
        handle = accounting_hook.before_dispatch(request=request, dispatch_kind="initial")
        try:
            response = self.generate(request)
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
        accounting_hook.after_response(handle, request=request, response=response, latency_ms=1)
        return response


_DURABLE_SCENE_VARIANTS = (
    "门轴在雨声里轻响，值夜人收起账册，把最后一盏灯推到窗前。",
    "她没有立刻回答，只用指尖抹去杯沿的水痕，等走廊重新安静。",
    "钟声落下时，他已越过空院；纸页贴在胸前，被冷风吹得发颤。",
    "先传来钥匙碰撞，随后黑暗里亮起火星，照见墙角未干的泥印。",
    "旧信压在石块下面，孩子绕开积水，将约定的红绳系回门环。",
    "炉灰忽然塌陷。两个人同时停手，谁也没有去碰露出的铜片。",
)


def _durable_scene_text(request_count: int) -> str:
    return _DURABLE_SCENE_VARIANTS[(request_count - 1) % len(_DURABLE_SCENE_VARIANTS)]


class _CountingGenerationClient(_AccountedTestClient):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _PlanningCheckpointClient(_AccountedTestClient):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "scene_blueprint":
            payload = {
                "visible_desire": "prove the checkpoint",
                "forced_choice": "continue or retreat",
                "price_paid": "lose time",
                "information_release": "the ledger is durable",
                "relationship_turn": "trust shifts",
                "image_anchor": "a checkpoint lamp",
                "ending_action": "the lamp turns green",
                "next_scene_pull": "what survives the retry",
                "anti_summary_rule": "end on the lamp",
            }
        elif request.node_id == "chapter_story_architecture":
            payload = {
                "chapter_promise": "the checkpoint must preserve the accepted plan",
                "escalation_path": ["record the plan", "interrupt the run", "resume without replay"],
                "reveal_plan": ["the durable artifact survives the interruption"],
                "payoff_target": "resume from the next provider call",
                "character_shift": "the operator trusts durable state",
                "ending_question": "does the checkpoint survive",
            }
        elif request.node_id == "character_pressure_blueprint":
            payload = {
                "surface_goal": "finish the interrupted scene",
                "hidden_fear": "the accepted plan was lost",
                "wrong_belief": "a retry must start over",
                "shame_point": "replaying work would hide a broken checkpoint",
                "avoidance_strategy": "restart instead of checking durable state",
                "relationship_debt": "preserve the prior operator's accepted work",
                "current_mask": "calm operational certainty",
            }
        else:
            raise AssertionError(f"unexpected planning request {request.node_id}")
        return _response(payload, f"planning-{request.node_id}-{len(self.requests)}")


class _FailBundleAfterPlanning:
    def build(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop after planning checkpoint")


def _planning_checkpoint_orchestrator(session, client: _PlanningCheckpointClient) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=_FailBeforeNeutral(),
        planning_service=NearFinalPlanningService(session, llm_client=client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=client)
    return orchestrator


class _FailSecondCandidateOnceClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 3:
            raise ValueError("candidate two failed once")
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _FailDeTemplateClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            raise ValueError("de-template provider failed")
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _FailFourthGenerationClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 4:
            raise ValueError("next candidate failed after de-template")
        payload = {"scene_text": _durable_scene_text(len(self.requests))}
        return _response(payload, f"generation-{len(self.requests)}")


class _SettledButUnparseableGenerationClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return _response({"unexpected": "provider succeeded without scene_text"}, "generation-unparseable")


class _FailAutoCritiquePatchClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            raise ValueError("auto-critique patch provider failed")
        return _response(
            {"scene_text": _durable_scene_text(len(self.requests))},
            f"generation-{len(self.requests)}",
        )


class _UnparseableAutoCritiquePatchClient(_CountingGenerationClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.node_id == "style_patch":
            return _response(
                {"unexpected": "provider succeeded without scene_text"},
                "auto-patch-unparseable",
            )
        return _response(
            {"scene_text": _durable_scene_text(len(self.requests))},
            f"generation-{len(self.requests)}",
        )


class _HardPassClient(_AccountedTestClient):
    def generate(self, request: LLMRequest) -> LLMResponse:
        return _response(
            {
                "resolution_code": "hard_pass",
                "pass_flag": True,
                "next_action": "pass",
                "issues": [],
                "rewrite_brief": [],
            },
            "hard-pass",
        )


class _FailAfterStyle:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        raise RuntimeError("fail after style checkpoint")


class _UnexpectedHardPromptBuilder:
    def build(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("unexpected hard QC prompt failure")


class _UnexpectedSoftQcRunner:
    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("unexpected soft QC runner failure")


class _PassSoftQc:
    def __init__(self, session) -> None:
        self.session = session
        self.calls = 0

    def evaluate(
        self,
        *,
        scene_id,
        bundle,
        source_draft_row_id,
        source_draft_content,
        execution_step_key="soft_qc:0",
    ):  # noqa: ANN001, ANN201
        self.calls += 1
        report_id = f"qc_{scene_id}_soft_resume"
        report = self.session.get(QcReport, report_id)
        if report is None:
            report = QcReport(
                qc_report_id=report_id,
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                qc_type="soft_qc",
                source_draft_row_id=source_draft_row_id,
                source_bundle_id=bundle["bundle_id"],
                resolution_code="soft_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
            )
            self.session.add(report)
        state = self.session.get(SceneRunState, scene_id)
        state.current_qc_report_id = report_id
        llm_call_id = f"llm_{scene_id}_{execution_step_key}"
        if self.session.get(LlmCall, llm_call_id) is None:
            self.session.add(
                LlmCall(
                    llm_call_id=llm_call_id,
                    provider="fake",
                    model="fake",
                    step="soft_qc",
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    scope_type="scene",
                    scope_id=scene_id,
                    execution_id=state.active_execution_id,
                    execution_step_key=execution_step_key,
                    estimated_tokens=0,
                    reserved_tokens=0,
                    budget_charged_tokens=0,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-13T00:00:00Z",
                    settled_at="2026-07-13T00:00:01Z",
                )
            )
            self.session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    step="soft_qc",
                    status="continue",
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "qc_report_id": report_id,
                        "resolution_code": "soft_pass",
                        "next_action": "pass",
                        "source_draft_row_id": source_draft_row_id,
                        "human_review_event_id": None,
                        "rewrite_brief": [],
                        "llm_call_id": llm_call_id,
                        "execution_step_key": execution_step_key,
                    },
                )
            )
        return SoftQcDecision(
            branch="continue",
            qc_report_id=report_id,
            human_review_event_id=None,
            resolution_code="soft_pass",
            next_action="pass",
            should_continue=True,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


class _SequencedSoftQc:
    def __init__(self, session, branches: dict[str, str]) -> None:
        self.session = session
        self.branches = branches
        self.calls: list[str] = []

    def evaluate(
        self,
        *,
        scene_id,
        bundle,
        source_draft_row_id,
        source_draft_content,
        execution_step_key="soft_qc:0",
    ):  # noqa: ANN001, ANN201
        del source_draft_content
        self.calls.append(execution_step_key)
        branch = self.branches[execution_step_key]
        values = {
            "patch": ("soft_patch", 0, "patch", [{"instruction": "tighten the checkpoint"}]),
            "continue": ("soft_pass", 1, "pass", []),
            "waive": (
                "soft_waive",
                1,
                "pass_with_notes",
                [{"kind": "carry_forward_note", "note_scope": "scene_memory", "carry_note_text": "keep note"}],
            ),
            "human_review_required": (
                "soft_block_human",
                0,
                "human_review_required",
                [{"instruction": "author must review"}],
            ),
        }
        resolution_code, pass_flag, next_action, rewrite_brief = values[branch]
        suffix = execution_step_key.replace(":", "_")
        report_id = f"qc_{scene_id}_{suffix}"
        llm_call_id = f"llm_{scene_id}_{suffix}"
        state = self.session.get(SceneRunState, scene_id)
        if self.session.get(QcReport, report_id) is None:
            self.session.add(
                QcReport(
                    qc_report_id=report_id,
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    qc_type="soft_qc",
                    source_draft_row_id=source_draft_row_id,
                    source_bundle_id=bundle["bundle_id"],
                    resolution_code=resolution_code,
                    pass_flag=pass_flag,
                    next_action=next_action,
                    issues_json=[],
                    rewrite_brief_json=rewrite_brief,
                )
            )
            self.session.add(
                LlmCall(
                    llm_call_id=llm_call_id,
                    provider="fake",
                    model="fake",
                    step="soft_qc",
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    scope_type="scene",
                    scope_id=scene_id,
                    execution_id=state.active_execution_id,
                    execution_step_key=execution_step_key,
                    estimated_tokens=0,
                    reserved_tokens=0,
                    budget_charged_tokens=0,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-13T00:00:00Z",
                    settled_at="2026-07-13T00:00:01Z",
                )
            )
            self.session.add(
                AttemptTracker(
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    step="soft_qc",
                    status=branch,
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "qc_report_id": report_id,
                        "resolution_code": resolution_code,
                        "next_action": next_action,
                        "source_draft_row_id": source_draft_row_id,
                        "human_review_event_id": None,
                        "rewrite_brief": rewrite_brief,
                        "llm_call_id": llm_call_id,
                        "execution_step_key": execution_step_key,
                    },
                )
            )
        human_review_event_id = f"review_{scene_id}" if branch == "human_review_required" else None
        if human_review_event_id is not None and self.session.get(HumanReviewEvent, human_review_event_id) is None:
            self.session.add(
                HumanReviewEvent(
                    event_id=human_review_event_id,
                    scene_id=scene_id,
                    chapter_id="CH_RESUME",
                    object_ref=source_draft_row_id,
                    event_source="scene_generation",
                    priority="high",
                    status="needs_followup",
                    allowed_actions_json=["inspect"],
                    result_status_map_json={"inspect": "needs_followup"},
                    details_json={
                        "replay_context": {
                            "current_qc_report_id": report_id,
                            "source_draft_row_id": source_draft_row_id,
                            "source_bundle_id": bundle["bundle_id"],
                        }
                    },
                    default_action="inspect",
                )
            )
        state.current_qc_report_id = report_id
        state.current_human_review_event_id = human_review_event_id
        if branch == "human_review_required":
            state.scene_status = "human_review_required"
        return SoftQcDecision(
            branch=branch,
            qc_report_id=report_id,
            human_review_event_id=human_review_event_id,
            resolution_code=resolution_code,
            next_action=next_action,
            should_continue=branch in {"continue", "waive"},
            stop_reason="blocking_soft_qc_issue" if branch == "human_review_required" else None,
            llm_call_id=llm_call_id,
            execution_step_key=execution_step_key,
        )


class _FailNearFinal:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        raise RuntimeError("fail after soft checkpoint")


class _PassNearFinal:
    def __init__(self, session) -> None:
        self.session = session
        self.calls = 0

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        scene_id = args[0]
        bundle = kwargs["bundle"]
        source_draft_row_id = kwargs["source_draft_row_id"]
        execution_step_key = kwargs["execution_step_key"]
        state = self.session.get(SceneRunState, scene_id)
        llm_call_id = f"llm_{scene_id}_{execution_step_key}"
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                provider="fake",
                model="fake",
                step="near_final_acceptance_review",
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id=scene_id,
                execution_id=state.active_execution_id,
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="settled",
                request_dispatched_at="2026-07-13T00:00:00Z",
                settled_at="2026-07-13T00:00:01Z",
            )
        )
        self.session.add(
            WriterEvaluation(
                evaluation_id="near-final-resume-eval",
                object_type="scene",
                object_id=scene_id,
                chapter_id="CH_RESUME",
                scene_id=scene_id,
                rubric_id="near_final_acceptance_v1",
                source_text_ref=f"source_draft:{source_draft_row_id}",
                source_bundle_id=bundle["bundle_id"],
                evaluator_llm_call_id=llm_call_id,
                lens="near_final_acceptance",
                overall_score=1.0,
                scores_json={},
                findings_json=[],
                revision_brief_json=[],
                status="completed",
            )
        )
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                step="near_final_acceptance_review",
                status="near_final_ready",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "evaluation_id": "near-final-resume-eval",
                    "revision_candidate_id": None,
                    "source_draft_row_id": source_draft_row_id,
                    "llm_call_id": llm_call_id,
                    "failure_class": None,
                    "execution_step_key": execution_step_key,
                },
            )
        )
        return {
            "near_final_status": "near_final_ready",
            "pass_flag": True,
            "overall_score": 1.0,
            "failure_class": None,
            "requires_human_review": False,
            "evaluation_id": "near-final-resume-eval",
            "revision_candidate_id": None,
            "should_rewrite": False,
            "findings": [],
            "revision_brief": [],
        }


class _SequencedNearFinal:
    def __init__(self, session, outcomes: dict[str, str]) -> None:
        self.session = session
        self.outcomes = outcomes
        self.calls: list[str] = []

    def evaluate_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        scene_id = args[0]
        bundle = kwargs["bundle"]
        source_draft_row_id = kwargs["source_draft_row_id"]
        source_content = kwargs["source_content"]
        execution_step_key = kwargs["execution_step_key"]
        self.calls.append(execution_step_key)
        outcome = self.outcomes[execution_step_key]
        suffix = execution_step_key.replace(":", "_")
        evaluation_id = f"near_final_eval_{scene_id}_{suffix}"
        llm_call_id = f"llm_{scene_id}_{suffix}"
        candidate_id = None if outcome == "pass" else f"revision_{scene_id}_{suffix}"
        if outcome == "pass":
            near_final_status = "near_final_ready"
            pass_flag = True
            failure_class = None
            requires_human_review = False
            should_rewrite = False
            findings = []
            revision_brief = []
            overall_score = 0.9
        else:
            near_final_status = "human_review_required" if outcome == "human" else "revision_required"
            pass_flag = False
            failure_class = "reference_safety" if outcome == "human" else "prose_model_voice"
            requires_human_review = outcome == "human"
            should_rewrite = outcome == "rewrite"
            findings = [{"dimension": "prose_freshness", "issue": "needs revision"}]
            revision_brief = [{"dimension": "prose_freshness", "action": "rewrite once"}]
            overall_score = 0.5
        state = self.session.get(SceneRunState, scene_id)
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                provider="fake",
                model="fake",
                step="near_final_acceptance_review",
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id=scene_id,
                execution_id=state.active_execution_id,
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="settled",
                request_dispatched_at="2026-07-13T00:00:00Z",
                settled_at="2026-07-13T00:00:01Z",
            )
        )
        self.session.add(
            WriterEvaluation(
                evaluation_id=evaluation_id,
                object_type="scene",
                object_id=scene_id,
                chapter_id="CH_RESUME",
                scene_id=scene_id,
                rubric_id="near_final_acceptance_v1",
                source_text_ref=f"source_draft:{source_draft_row_id}",
                source_bundle_id=bundle["bundle_id"],
                evaluator_llm_call_id=llm_call_id,
                lens="near_final_acceptance",
                overall_score=overall_score,
                scores_json={"prose_freshness": overall_score},
                findings_json=findings,
                failure_class=failure_class,
                auto_rewrite_eligible=1 if should_rewrite else 0,
                contract_field_refs_json={},
                promotion_blockers_json=[] if should_rewrite or pass_flag else [failure_class],
                revision_brief_json=revision_brief,
                requires_human_review=1 if requires_human_review else 0,
                status="completed",
            )
        )
        if candidate_id is not None:
            self.session.add(
                RevisionCandidate(
                    revision_id=candidate_id,
                    evaluation_id=evaluation_id,
                    object_type="scene",
                    object_id=scene_id,
                    chapter_id="CH_RESUME",
                    scene_id=scene_id,
                    revision_type="near_final_scene_rewrite",
                    source_text_ref=f"source_draft:{source_draft_row_id}",
                    proposed_text=source_content,
                    instruction_json=revision_brief,
                    diff_summary_json={"failure_class": failure_class},
                    patches_json=[],
                    apply_mode="manual_or_regenerate",
                    target_text_ref=f"source_draft:{source_draft_row_id}",
                    status="candidate",
                    created_by="near_final_acceptance",
                )
            )
        if outcome == "pass":
            for candidate in self.session.execute(
                select(RevisionCandidate).where(
                    RevisionCandidate.scene_id == scene_id,
                    RevisionCandidate.status == "candidate",
                )
            ).scalars().all():
                candidate.status = "superseded"
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id="CH_RESUME",
                step="near_final_acceptance_review",
                status=near_final_status,
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "evaluation_id": evaluation_id,
                    "revision_candidate_id": candidate_id,
                    "source_draft_row_id": source_draft_row_id,
                    "llm_call_id": llm_call_id,
                    "failure_class": failure_class,
                    "execution_step_key": execution_step_key,
                },
            )
        )
        return {
            "near_final_status": near_final_status,
            "pass_flag": pass_flag,
            "overall_score": overall_score,
            "scores": {"prose_freshness": overall_score},
            "failure_class": failure_class,
            "requires_human_review": requires_human_review,
            "evaluation_id": evaluation_id,
            "revision_candidate_id": candidate_id,
            "should_rewrite": should_rewrite,
            "findings": findings,
            "revision_brief": revision_brief,
        }


class _FailArchiveOnce:
    def archive_final_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("fail after near-final checkpoint")


class _FailBeforeNeutral:
    def generate_neutral_draft(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop at bundle checkpoint")


class _FailBeforeHardQc:
    def evaluate(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise RuntimeError("stop after neutral retry")


def _response(payload: dict, request_id: str) -> LLMResponse:
    return LLMResponse(
        request_id=request_id,
        provider="fake-provider",
        model="fake-model",
        text=json.dumps(payload),
        structured_output=payload,
        response_format="json_object",
        raw_response={
            "id": request_id,
            "model": "fake-model",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "finish_reason": "stop",
        },
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        finish_reason="stop",
    )


def _seed_resume_scene(session) -> None:
    session.add(
        StoryProject(
            project_id="P_RESUME",
            title="Resume Project",
            outline_text="resume safely",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id="CH_RESUME",
            project_id="P_RESUME",
            planned_scene_count=1,
            chapter_goal="resume safely",
        )
    )
    session.add(ChapterState(chapter_id="CH_RESUME", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            project_id="P_RESUME",
            scene_seq=1,
            pov_character_id="CHAR_A",
            onstage_chars_json=["CHAR_A", "CHAR_B"],
            location="checkpoint room",
            scene_goal="prove the checkpoint",
            beats_json=["write", "fail", "resume"],
            must_include_text="",
            target_length_band="short",
            scene_type="transition",
            is_chapter_last=0,
        )
    )
    session.add(SceneRunState(scene_id="CH_RESUME_SC01", scene_status="ready"))
    session.add(
        VoiceProfile(
            row_id="voice_resume_v1",
            voice_profile_id="VOICE_CHAR_A",
            version=1,
            character_id="CHAR_A",
            content="concise",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.add(
        RelationProfile(
            row_id="relation_resume_v1",
            relation_profile_id="REL_CHAR_A_CHAR_B",
            left_character_id="CHAR_A",
            right_character_id="CHAR_B",
            version=1,
            content="uneasy allies",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.commit()


def _select_first_checkpoint_candidate(session, scene_id: str) -> tuple[str, str]:
    gate = session.execute(
        select(HumanReviewEvent)
        .where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
        .order_by(HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc())
    ).scalars().first()
    assert gate is not None
    details = dict(gate.details_json or {})
    selected_row_id = details["candidate_row_ids"][0]
    gate.details_json = {
        **details,
        "decision_status": "selected",
        "selected_row_id": selected_row_id,
    }
    state = session.get(SceneRunState, scene_id)
    state.current_style_draft_row_id = selected_row_id
    state.latest_valid_draft_row_id = selected_row_id
    session.commit()
    return gate.event_id, selected_row_id


def _selection_resume_orchestrator(
    session,
    *,
    generation_client,
    soft_qc,
    near_final,
) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )
    # These tests exercise the selection hand-off itself.  Production Best-of-N
    # remains evidence-gated and default-off; this dedicated harness explicitly
    # models an already-authorized two-candidate cell.
    orchestrator._best_of_n_count = lambda _contract, *, criticality=None: 2
    return orchestrator


def test_unexpected_hard_qc_prompt_failure_is_fail_closed_without_report_or_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    hard_qc = HardQcEngine(session, llm_client=_HardPassClient())
    hard_qc.prompt_builder = _UnexpectedHardPromptBuilder()
    planning_client = _PlanningCheckpointClient()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=hard_qc,
        soft_qc_engine=_FailAfterStyle(),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=planning_client)

    with pytest.raises(RuntimeError, match="unexpected hard QC prompt failure"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-qc-unexpected")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "neutral_ready"
    assert "qc_report_id" not in (state.run_checkpoint_json.get("artifact_refs") or {})
    assert session.scalar(
        select(func.count()).select_from(QcReport).where(
            QcReport.scene_id == "CH_RESUME_SC01",
            QcReport.qc_type == "hard_qc",
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.step == "hard_qc",
        )
    ) == 0
    assert len(generation_client.requests) == 1


def test_unexpected_soft_qc_runner_failure_is_fail_closed_without_report_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = SoftQcEngine(session, llm_runner=_UnexpectedSoftQcRunner())
    planning_client = _PlanningCheckpointClient()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=_FailNearFinal(),
        planning_service=NearFinalPlanningService(session, llm_client=planning_client),
    )
    orchestrator.scene_blueprint_service = SceneBlueprintService(session, llm_client=planning_client)

    with pytest.raises(RuntimeError, match="unexpected soft QC runner failure"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc-unexpected")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    refs = state.run_checkpoint_json.get("artifact_refs") or {}
    assert "soft_qc_report_id" not in refs
    assert "soft_qc_llm_call_id" not in refs
    assert session.scalar(
        select(func.count()).select_from(QcReport).where(
            QcReport.scene_id == "CH_RESUME_SC01",
            QcReport.qc_type == "soft_qc",
        )
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.step == "soft_qc",
        )
    ) == 0
    assert len(generation_client.requests) == 2


def test_failure_audit_snapshot_fault_persists_unrecoverable_fence_in_file_database(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    execution_id = "idempotency:audit-snapshot-fault"
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=late_failure,
    )

    def fail_snapshot(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("failure audit snapshot exploded")

    first._capture_failure_audits = fail_snapshot
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene(scene_id, execution_id=execution_id)

    verifier = SessionLocal()
    try:
        state = verifier.get(SceneRunState, scene_id)
        assert state is not None
        assert state.active_execution_id == execution_id
        assert state.run_execution_status == "cancelled"
        assert state.run_checkpoint == "cancelled"
        fence = state.run_checkpoint_json["unrecoverable_failure_audit"]
        assert fence["phase"] == "snapshot"
        assert fence["error_type"] == "RuntimeError"
    finally:
        verifier.close()

    provider_calls = len(generation_client.requests)
    session.expire_all()
    with pytest.raises(DomainError) as retry:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        ).run_scene(scene_id, execution_id=execution_id)
    assert retry.value.code == "RUN_EXECUTION_CANCELLED"
    assert len(generation_client.requests) == provider_calls


def test_selection_resume_audit_restore_fault_persists_unrecoverable_fence_in_file_database(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    failing_near_final = _FailNearFinal()
    paused = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    ).run_scene(scene_id, execution_id="idempotency:audit-restore-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    _select_first_checkpoint_candidate(session, scene_id)
    execution_id = "idempotency:audit-restore-resume"
    first = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    )

    def fail_restore(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("failure audit restore exploded")

    first._restore_failure_audits = fail_restore
    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        first.resume_after_selection(scene_id, execution_id=execution_id)

    verifier = SessionLocal()
    try:
        state = verifier.get(SceneRunState, scene_id)
        assert state is not None
        assert state.active_execution_id == execution_id
        assert state.run_execution_status == "cancelled"
        assert state.run_checkpoint == "cancelled"
        fence = state.run_checkpoint_json["unrecoverable_failure_audit"]
        assert fence["phase"] == "restore"
        assert fence["error_type"] == "RuntimeError"
    finally:
        verifier.close()

    provider_calls = len(generation_client.requests)
    soft_calls = soft_qc.calls
    near_calls = failing_near_final.calls
    session.expire_all()
    with pytest.raises(DomainError) as retry:
        _selection_resume_orchestrator(
            session,
            generation_client=generation_client,
            soft_qc=soft_qc,
            near_final=failing_near_final,
        ).resume_after_selection(scene_id, execution_id=execution_id)
    assert retry.value.code == "RUN_EXECUTION_CANCELLED"
    assert len(generation_client.requests) == provider_calls
    assert soft_qc.calls == soft_calls
    assert failing_near_final.calls == near_calls


def test_selection_resume_fresh_idempotency_execution_continues_after_complete_soft_subcursor(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    failing_near_final = _FailNearFinal()
    origin = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=failing_near_final,
    )
    paused = origin.run_scene(scene_id, execution_id="idempotency:selection-soft-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    gate_id, selected_row_id = _select_first_checkpoint_candidate(session, scene_id)
    resume_execution_id = "idempotency:selection-soft-resume"

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        _selection_resume_orchestrator(
            session,
            generation_client=generation_client,
            soft_qc=soft_qc,
            near_final=failing_near_final,
        ).resume_after_selection(scene_id, execution_id=resume_execution_id)

    state = session.get(SceneRunState, scene_id)
    assert state.run_execution_status == "failed"
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 3
    handoff_refs = state.run_checkpoint_json["artifact_refs"]
    gate = session.get(HumanReviewEvent, gate_id)
    assert handoff_refs["selected_row_id"] == selected_row_id
    assert gate.status == "resolved"
    assert gate.details_json["resumed"] is True
    assert handoff_refs["soft_input_source_draft_row_id"] == selected_row_id
    provider_calls = len(generation_client.requests)
    style_call_ids = list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(LlmCall.scene_id == scene_id, LlmCall.step.in_(("style_draft", "de_template")))
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    )

    # A real strict run publishes the recoverable author-facing state before a
    # later provider call reaches the lifecycle boundary.  The durable failed
    # soft checkpoint, rather than the old selection-wait label, owns resume.
    state.scene_status = "soft_qc_patch_required"
    session.commit()

    retry_execution_id = "idempotency:selection-soft-retry"
    result = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=_PassNearFinal(session),
    ).resume_after_selection(scene_id, execution_id=retry_execution_id)

    assert result["scene_status"] == "archived"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls
    assert list(
        session.execute(
            select(LlmCall.llm_call_id)
            .where(LlmCall.scene_id == scene_id, LlmCall.step.in_(("style_draft", "de_template")))
            .order_by(LlmCall.llm_call_id)
        ).scalars()
    ) == style_call_ids
    assert session.scalar(
        select(func.count()).select_from(HumanReviewEvent).where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
    ) == 1
    assert session.get(HumanReviewEvent, gate_id).details_json["resumed"] is True


def test_selection_resume_same_execution_continues_after_partial_near_final_subcursor(session) -> None:
    _seed_resume_scene(session)
    scene_id = "CH_RESUME_SC01"
    scene = session.get(SceneCard, scene_id)
    scene.constraint_intensity = 0.9
    session.commit()
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )
    origin = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    )
    paused = origin.run_scene(scene_id, execution_id="idempotency:selection-near-origin")
    assert paused["scene_status"] == "awaiting_candidate_selection"
    gate_id, selected_row_id = _select_first_checkpoint_candidate(session, scene_id)
    resume_execution_id = "idempotency:selection-near-resume"
    first = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    )
    original_reconcile = first._reconcile_execution_step

    def stop_before_rewrite(step_key: str) -> None:
        if step_key == "near_final_rewrite:0":
            raise RuntimeError("stop selection resume after near eval0")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_rewrite
    with pytest.raises(RuntimeError, match="stop selection resume after near eval0"):
        first.resume_after_selection(scene_id, execution_id=resume_execution_id)

    state = session.get(SceneRunState, scene_id)
    assert state.run_execution_status == "failed"
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    handoff_refs = state.run_checkpoint_json["artifact_refs"]
    gate = session.get(HumanReviewEvent, gate_id)
    assert handoff_refs["selected_row_id"] == selected_row_id
    assert gate.status == "resolved"
    assert gate.details_json["resumed"] is True
    assert handoff_refs["soft_input_source_draft_row_id"] == selected_row_id
    provider_calls = len(generation_client.requests)

    result = _selection_resume_orchestrator(
        session,
        generation_client=generation_client,
        soft_qc=soft_qc,
        near_final=near_final,
    ).resume_after_selection(scene_id, execution_id=resume_execution_id)

    assert result["scene_status"] == "archived"
    assert soft_qc.calls == 1
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert session.scalar(
        select(func.count()).select_from(HumanReviewEvent).where(
            HumanReviewEvent.scene_id == scene_id,
            HumanReviewEvent.event_source == "candidate_selection",
        )
    ) == 1


def test_committed_neutral_and_style_checkpoint_resume_without_new_call_or_charge(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-one")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    first_used = state.scene_tokens_used
    first_attempts = state.total_attempt_count
    assert len(session.execute(select(SceneDraft)).scalars().all()) == 2
    assert len(generation_client.requests) == 2
    execution_calls = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "idempotency:resume-one")
    ).scalars().all()
    assert {call.execution_step_key for call in execution_calls} >= {
        "neutral_draft",
        "hard_qc:0",
        "style_draft:0",
    }

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-one")

    session.refresh(state)
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.scene_tokens_used == first_used
    assert state.total_attempt_count == first_attempts
    assert len(session.execute(select(SceneDraft)).scalars().all()) == 2
    assert len(session.execute(select(LlmCall)).scalars().all()) >= 2
    assert len(generation_client.requests) == 2


@pytest.mark.parametrize(
    ("fail_before_step", "first_sub_index"),
    [
        ("planning:chapter_architecture", 0),
        ("planning:character_pressure", 1),
        ("bundle", 3),
    ],
)
def test_planning_subcheckpoints_resume_from_next_provider_without_replay(
    session,
    monkeypatch,
    fail_before_step: str,
    first_sub_index: int,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:planning-substep-{first_sub_index}"
    client = _PlanningCheckpointClient()
    planning_indices: list[int] = []
    original_save = SceneRunCheckpointService.save_checkpoint

    def observe_save(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if kwargs.get("node_key") == "planning_ready":
            planning_indices.append(kwargs.get("sub_index"))
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(SceneRunCheckpointService, "save_checkpoint", observe_save)
    first = _planning_checkpoint_orchestrator(session, client)
    if fail_before_step == "bundle":
        first.bundle_builder = _FailBundleAfterPlanning()
    else:
        original_reconcile = first._reconcile_execution_step
        failed = False

        def fail_once(step_key: str) -> None:
            nonlocal failed
            if step_key == fail_before_step and not failed:
                failed = True
                raise RuntimeError(f"stop before {step_key}")
            original_reconcile(step_key)

        first._reconcile_execution_step = fail_once

    expected_error = "stop after planning checkpoint" if fail_before_step == "bundle" else f"stop before {fail_before_step}"
    with pytest.raises(RuntimeError, match=expected_error):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "planning_ready"
    assert state.run_checkpoint_json["sub_index"] == first_sub_index
    requests_after_failure = [request.node_id for request in client.requests]

    resumed = _planning_checkpoint_orchestrator(session, client)
    resumed.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    assert state.run_checkpoint == "planning_ready"
    assert state.run_checkpoint_json["sub_index"] == 3
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]
    assert [request.node_id for request in client.requests[: len(requests_after_failure)]] == requests_after_failure
    assert planning_indices == [0, 1, 2, 3]
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    for prefix, step_key in (
        ("planning_scene_blueprint", "scene_blueprint"),
        ("planning_chapter_architecture", "planning:chapter_architecture"),
        ("planning_character_pressure", "planning:character_pressure"),
    ):
        assert refs[f"{prefix}_row_id"]
        assert refs[f"{prefix}_execution_step_key"] == step_key
        assert refs[f"{prefix}_llm_call_id"]
        assert refs[f"{prefix}_artifact_execution_id"] == execution_id
        assert hashes[prefix]
    assert hashes["planning"]
    assert refs["planning"]["chapter_architecture"]["row_id"] == refs["planning_chapter_architecture_row_id"]
    assert refs["planning"]["character_pressure"]["row_id"] == refs["planning_character_pressure_row_id"]


def test_missing_partial_planning_blueprint_blocks_before_next_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-partial-missing"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_architecture(step_key: str) -> None:
        if step_key == "planning:chapter_architecture":
            raise RuntimeError("stop before architecture")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_architecture
    with pytest.raises(RuntimeError, match="stop before architecture"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    row_id = state.run_checkpoint_json["artifact_refs"]["planning_scene_blueprint_row_id"]
    session.delete(session.get(SceneBlueprint, row_id))
    session.commit()
    provider_count = len(client.requests)

    with pytest.raises(DomainError) as missing:
        _planning_checkpoint_orchestrator(session, client).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(client.requests) == provider_count


def test_tampered_partial_chapter_architecture_blocks_before_character_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-partial-corrupt"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_character(step_key: str) -> None:
        if step_key == "planning:character_pressure":
            raise RuntimeError("stop before character pressure")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_character
    with pytest.raises(RuntimeError, match="stop before character pressure"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    row_id = state.run_checkpoint_json["artifact_refs"]["planning_chapter_architecture_row_id"]
    artifact = session.get(GenerationPlanningArtifact, row_id)
    artifact.payload_json = {"ending_question": "tampered"}
    session.commit()
    provider_count = len(client.requests)

    with pytest.raises(DomainError) as corrupt:
        _planning_checkpoint_orchestrator(session, client).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(client.requests) == provider_count


def test_new_execution_reuses_previous_active_planning_artifacts_with_fenced_provenance(session) -> None:
    _seed_resume_scene(session)
    client = _PlanningCheckpointClient()
    old_execution = "idempotency:planning-origin"
    first = _planning_checkpoint_orchestrator(session, client)
    first.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=old_execution)
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]

    new_execution = "idempotency:planning-reuser"
    for _attempt in range(2):
        reused = _planning_checkpoint_orchestrator(session, client)
        reused.bundle_builder = _FailBundleAfterPlanning()
        with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
            reused.run_scene("CH_RESUME_SC01", execution_id=new_execution)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    assert state.active_execution_id == new_execution
    assert state.run_checkpoint_json["sub_index"] == 3
    assert len(client.requests) == 3
    for prefix in (
        "planning_scene_blueprint",
        "planning_chapter_architecture",
        "planning_character_pressure",
    ):
        assert refs[f"{prefix}_reused"] is True
        assert refs[f"{prefix}_artifact_execution_id"] == old_execution
        assert hashes[f"{prefix}_provenance"]


def test_partial_planning_resume_prefers_checkpoint_row_over_newer_active_artifact(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:planning-checkpoint-row-wins"
    client = _PlanningCheckpointClient()
    first = _planning_checkpoint_orchestrator(session, client)
    original_reconcile = first._reconcile_execution_step

    def fail_before_character(step_key: str) -> None:
        if step_key == "planning:character_pressure":
            raise RuntimeError("stop before character pressure")
        original_reconcile(step_key)

    first._reconcile_execution_step = fail_before_character
    with pytest.raises(RuntimeError, match="stop before character pressure"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    checkpoint_row_id = state.run_checkpoint_json["artifact_refs"]["planning_chapter_architecture_row_id"]
    checkpoint_row = session.get(GenerationPlanningArtifact, checkpoint_row_id)
    checkpoint_row.status = "superseded"
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_chapter_story_architecture_CH_RESUME_zzzzzzzzzz",
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id="CH_RESUME",
            chapter_id="CH_RESUME",
            scene_id=None,
            payload_json={"ending_question": "newer unrelated architecture"},
            llm_call_id=None,
            source_bundle_id="newer-source",
            source_bundle_hash="newer-hash",
            status="active",
            created_by="other-scene",
            created_at="2099-01-01T00:00:00+00:00",
        )
    )
    session.commit()

    resumed = _planning_checkpoint_orchestrator(session, client)
    resumed.bundle_builder = _FailBundleAfterPlanning()
    with pytest.raises(RuntimeError, match="stop after planning checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    assert state.run_checkpoint_json["artifact_refs"]["planning"]["chapter_architecture"]["row_id"] == checkpoint_row_id
    assert [request.node_id for request in client.requests] == [
        "scene_blueprint",
        "chapter_story_architecture",
        "character_pressure_blueprint",
    ]


def test_settled_provider_parse_failure_restores_ledger_and_blocks_same_execution_retry(session) -> None:
    _seed_resume_scene(session)
    generation_client = _SettledButUnparseableGenerationClient()
    execution_id = "idempotency:settled-before-product"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    with pytest.raises(ValueError, match="missing scene_text"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    calls = session.execute(
        select(LlmCall).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "neutral_draft",
        )
    ).scalars().all()
    assert len(calls) == 1
    assert calls[0].accounting_status == "settled"
    assert calls[0].request_dispatched_at is not None
    assert session.execute(
        select(SceneDraft).where(SceneDraft.generation_llm_call_id == calls[0].llm_call_id)
    ).scalars().all() == []
    assert state.scene_tokens_used == session.scalar(
        select(func.sum(LlmCall.budget_charged_tokens)).where(
            LlmCall.scene_id == "CH_RESUME_SC01",
            LlmCall.execution_id == execution_id,
        )
    )
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(generation_client.requests) == provider_calls == 1


def test_same_execution_retry_before_first_checkpoint_is_resumed_and_preserves_current_pointer(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:failed-before-first-checkpoint"

    def fail_before_checkpoint(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("failed before first checkpoint")

    monkeypatch.setattr(Orchestrator, "_run_scene_pipeline", fail_before_checkpoint)
    with pytest.raises(RuntimeError, match="failed before first checkpoint"):
        Orchestrator(session).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint is None
    state.current_neutral_draft_row_id = "preserve-on-same-execution-retry"
    session.commit()

    observed: list[str | None] = []

    def observe_then_fail(self, scene_id, **kwargs):  # noqa: ANN001, ANN003
        observed.append(self.session.get(SceneRunState, scene_id).current_neutral_draft_row_id)
        raise RuntimeError("same execution retry")

    monkeypatch.setattr(Orchestrator, "_run_scene_pipeline", observe_then_fail)
    with pytest.raises(RuntimeError, match="same execution retry"):
        Orchestrator(session).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert observed == ["preserve-on-same-execution-retry"]


def test_best_of_n_blocks_dispatched_missing_second_candidate_without_repeating_provider(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    generation_client = _FailSecondCandidateOnceClient()
    late_failure = _FailAfterStyle()
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(ValueError, match="candidate two failed once"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-candidates")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    assert state.run_checkpoint_json["artifact_refs"]["style_candidate_row_ids"] == [
        "draft_style_cand_CH_RESUME_SC01_v1_0"
    ]
    assert len(generation_client.requests) == 3

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:resume-candidates")

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    session.refresh(state)
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["artifact_refs"]["style_candidate_row_ids"] == [
        "draft_style_cand_CH_RESUME_SC01_v1_0"
    ]
    assert len(generation_client.requests) == 3
    draft_ids = session.execute(select(SceneDraft.row_id).order_by(SceneDraft.row_id)).scalars().all()
    assert draft_ids == [
        "draft_neutral_CH_RESUME_SC01_v1",
        "draft_style_cand_CH_RESUME_SC01_v1_0",
    ]
    candidate_steps = session.execute(
        select(LlmCall.execution_step_key).where(
            LlmCall.execution_id == "idempotency:resume-candidates",
            LlmCall.step == "style_draft",
        )
    ).scalars().all()
    assert sorted(candidate_steps) == ["style_draft:0", "style_draft:1"]


def test_best_of_n_releases_undispatched_second_candidate_reservation_then_retries_once(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()
    execution_id = "idempotency:resume-undispatched-candidate"
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def crash_after_second_candidate_reservation(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key != "style_draft:1":
            return
        state = session.get(SceneRunState, "CH_RESUME_SC01")
        state.scene_tokens_reserved += 17
        session.add(
            LlmCall(
                llm_call_id="call-undispatched-style-candidate-1",
                provider="fake",
                model="fake",
                step="style_draft",
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                scope_type="scene",
                scope_id="CH_RESUME_SC01",
                execution_id=execution_id,
                execution_step_key="style_draft:1",
                estimated_tokens=17,
                reserved_tokens=17,
                budget_charged_tokens=0,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt-undispatched-style-candidate-1",
                llm_call_id="call-undispatched-style-candidate-1",
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                estimated_tokens=17,
                reserved_tokens=17,
                budget_charged_tokens=0,
                accounting_status="reserved",
            )
        )
        session.commit()
        raise RuntimeError("crash after candidate reservation")

    first._reconcile_execution_step = crash_after_second_candidate_reservation
    with pytest.raises(RuntimeError, match="crash after candidate reservation"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert len(generation_client.requests) == 2
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    released = session.get(LlmCall, "call-undispatched-style-candidate-1")
    assert released.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert len(generation_client.requests) == 3
    assert set(state.run_checkpoint_json["artifact_refs"]["candidate_row_ids"]) == {
        "draft_style_cand_CH_RESUME_SC01_v1_1",
        "draft_style_cand_CH_RESUME_SC01_v1_0",
    }


def test_missing_settled_style_output_blocks_before_any_new_provider_call(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:missing-output")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    style_row = session.get(SceneDraft, state.run_checkpoint_json["artifact_refs"]["style_draft_row_id"])
    assert style_row is not None
    session.delete(style_row)
    session.commit()
    before_calls = session.scalar(select(func.count()).select_from(LlmCall))
    before_tokens = state.scene_tokens_used
    before_provider = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:missing-output")

    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    session.refresh(state)
    assert len(generation_client.requests) == before_provider
    assert session.scalar(select(func.count()).select_from(LlmCall)) == before_calls
    assert state.scene_tokens_used == before_tokens


def test_successful_run_commits_terminal_macro_checkpoint(session) -> None:
    _seed_resume_scene(session)
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    )
    result = orchestrator.run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:terminal-checkpoint",
    )

    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"] == state.current_final_scene_row_id

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(session).run_scene(
            "CH_RESUME_SC01",
            author_note="换掉已经归档运行的作者指令",
            execution_id="idempotency:terminal-checkpoint",
        )
    assert exc_info.value.code == "RUN_INPUT_MISMATCH"


def test_soft_qc_checkpoint_resume_does_not_repeat_qc_or_generation(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "soft_qc_ready"
    assert soft_qc.calls == 1
    assert near_final.calls == 2
    assert len(generation_client.requests) == 2


def test_soft_qc0_checkpoint_resumes_at_patch_without_replaying_qc0(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_patch(execution_step_key: str) -> None:
        if execution_step_key == "soft_patch:soft_qc:0":
            raise RuntimeError("stop after soft QC0 checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_patch
    with pytest.raises(RuntimeError, match="stop after soft QC0 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    assert state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"] == "qc_CH_RESUME_SC01_soft_qc_0"
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-qc0-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_qc_round"] == 1
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert state.scene_tokens_used > tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_qc",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("execution_step_key") == "soft_qc:0"
        ]
    ) == 1


def test_soft_patch_checkpoint_resumes_at_qc1_without_replaying_patch(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_qc1(execution_step_key: str) -> None:
        if execution_step_key == "soft_qc:1":
            raise RuntimeError("stop after soft patch checkpoint")
        original_reconcile(execution_step_key)

    first._reconcile_execution_step = stop_before_qc1
    with pytest.raises(RuntimeError, match="stop after soft patch checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 2
    patch_row_id = state.run_checkpoint_json["artifact_refs"]["soft_patch_draft_row_id"]
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-patch-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["soft_final_draft_row_id"] == patch_row_id
    assert soft_qc.calls == ["soft_qc:0", "soft_qc:1"]
    assert len(generation_client.requests) == provider_calls
    assert state.scene_tokens_used == tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "soft_patch:soft_qc:0",
            )
        ).scalars().all()
    ) == 1
    assert len(
        [
            attempt
            for attempt in session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == "CH_RESUME_SC01",
                    AttemptTracker.step == "soft_patch",
                )
            ).scalars().all()
            if (attempt.details_json or {}).get("row_id") == patch_row_id
        ]
    ) == 1


def test_soft_qc_without_patch_advances_directly_to_complete_subcursor(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "continue"})
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    for _attempt in range(2):
        with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
            orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-no-patch")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "no_patch_requested"
    assert refs["soft_final_draft_row_id"] == refs["soft_input_draft_row_id"]
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == 2


def test_soft_qc_budget_skip_persists_branch_without_patch_call(session, monkeypatch) -> None:
    from novel_system.services import scene_budget

    _seed_resume_scene(session)
    monkeypatch.setattr(scene_budget, "can_spend", lambda *args, **kwargs: False)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "patch"})
    near_final = _FailNearFinal()
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=soft_qc,
        near_final_service=near_final,
    )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-budget-skip")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_qc0_control"] == {
        "patch_allowed": False,
        "skip_reason": "budget_or_candidate_cap",
    }
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_qc_branch"] == "patch"
    assert refs.get("soft_patch_draft_row_id") is None
    assert len(generation_client.requests) == 2


def test_soft_qc_human_review_branch_is_complete_and_resume_safe(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(session, {"soft_qc:0": "human_review_required"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
        )

    first = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )
    provider_calls = len(generation_client.requests)
    second = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:soft-human-review",
    )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert first["scene_status"] == second["scene_status"] == "human_review_required"
    assert first["soft_qc"]["branch"] == second["soft_qc"]["branch"] == "human_review_required"
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["soft_final_qc_round"] == 0
    assert refs["soft_completion_skip_reason"] == "human_review_required"
    assert soft_qc.calls == ["soft_qc:0"]
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_missing_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    session.delete(session.get(QcReport, report_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-missing")

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_soft_prefix_tampered_qc0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _SequencedSoftQc(
        session,
        {"soft_qc:0": "patch", "soft_qc:1": "continue"},
    )
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    report = session.get(QcReport, report_id)
    report.rewrite_brief_json = [{"instruction": "tampered QC0"}]
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(soft_qc.calls)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-prefix-tamper")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_auto_critique_patch_is_durable_soft_input_subcheckpoint(session, monkeypatch) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        lambda *args, **kwargs: replace(
            auto_critique(args[0]),
            outcome="not_invoked",
            execution_id=kwargs["llm_context"].execution_id,
            execution_step_key=kwargs["llm_context"].execution_step_key,
            run_job_id=kwargs["llm_context"].run_job_id,
            reason="feature_disabled",
        ),
    )
    generation_client = _CountingGenerationClient()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:auto-critique-input")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    input_draft = session.get(SceneDraft, refs["soft_input_draft_row_id"])
    assert state.run_checkpoint_json["sub_index"] == 0
    assert refs["soft_auto_critique_outcome"] == "patched"
    assert refs["soft_input_execution_step_key"] == "soft_patch:auto_critique:0"
    assert refs["soft_input_source_draft_row_id"] != refs["soft_input_draft_row_id"]
    assert input_draft.stage == "style_patch"
    assert len(generation_client.requests) == 3


def test_auto_critique_patch_control_plane_failure_is_not_degraded(session, monkeypatch) -> None:
    from novel_system.services.auto_critique import CritiqueResult

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        lambda *args, **kwargs: CritiqueResult(
            should_rewrite=True,
            directives=["tighten the opening"],
            dimension_scores={"syntax_monotony": 0.1},
            flagged_dimensions=["syntax_monotony"],
            outcome="not_invoked",
            execution_id=kwargs["llm_context"].execution_id,
            execution_step_key=kwargs["llm_context"].execution_step_key,
            run_job_id=kwargs["llm_context"].run_job_id,
            reason="feature_disabled",
        ),
    )
    error = LLMNodeExecutionError(
        llm_call_id="llmcall_patch_conflict",
        error_code="LLM_ACCOUNTING_EXECUTION_STEP_EXISTS",
        message="conflict",
        request_summary={},
        response_summary={},
    )
    generation_client = _CountingGenerationClient()
    generation_service = SceneGenerationService(session, llm_client=generation_client)
    monkeypatch.setattr(
        generation_service,
        "generate_style_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(LLMNodeExecutionError) as raised:
        Orchestrator(
            session,
            scene_generation_service=generation_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:auto-critique-patch-conflict",
        )
    assert raised.value is error


@pytest.mark.parametrize(
    ("client_factory", "expected_outcome", "expected_error"),
    [
        (_FailAutoCritiquePatchClient, "provider_failed", "ValueError"),
        (
            _UnparseableAutoCritiquePatchClient,
            "parse_failed",
            "SCENE_GENERATION_RESPONSE_INVALID",
        ),
    ],
)
def test_auto_critique_patch_failure_product_is_durable_and_recoverable(
    session,
    monkeypatch,
    client_factory,
    expected_outcome: str,
    expected_error: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-failure:{expected_outcome}"
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = client_factory()

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    assert refs["soft_auto_critique_outcome"] == "patch_failed"
    assert product["outcome"] == expected_outcome
    assert product["execution_id"] == execution_id
    assert product["execution_step_key"] == "soft_patch:auto_critique:0"
    assert product["error_code"] == expected_error

    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )


def test_auto_critique_patch_generic_error_without_parent_is_not_degraded(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = _CountingGenerationClient()
    generation_service = SceneGenerationService(session, llm_client=generation_client)
    monkeypatch.setattr(
        generation_service,
        "generate_style_patch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("untracked patch failure")
        ),
    )

    with pytest.raises(RuntimeError, match="untracked patch failure"):
        Orchestrator(
            session,
            scene_generation_service=generation_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:auto-patch-untracked",
        )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint != "soft_qc_ready"


def test_auto_critique_patch_failure_child_tamper_blocks_without_replay(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-failure-child-tamper"
    critique_calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(
            session,
            critique_calls,
            should_rewrite=True,
        ),
    )
    generation_client = _FailAutoCritiquePatchClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    child = session.scalar(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == product["llm_call_id"]
        )
    )
    session.delete(child)
    session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_patch_parse_failure_parent_owner_tamper_blocks(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-parse-owner-tamper"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_UnparseableAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    failure = refs["soft_auto_critique_patch_failure"]
    parent = session.get(LlmCall, failure["llm_call_id"])
    parent.scope_id = "SC_DETACHED"
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_patch_rejected_tombstone_recovers_before_gate_without_replay(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    class _CrashAfterRejectedPatch(BaseException):
        pass

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-rejected-recover:{gate_open}:online"

    def rule_no_call(content, *_args, **kwargs):
        context = kwargs["llm_context"]
        return replace(
            auto_critique(content),
            outcome="not_invoked",
            reason="feature_disabled",
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rule_no_call,
    )
    generation_client = _CountingGenerationClient()
    first_service = SceneGenerationService(session, llm_client=generation_client)

    def reject_then_crash(scene_id, *_args, **_kwargs):
        scene = session.get(SceneCard, scene_id)
        chapter = session.get(ChapterGoal, scene.chapter_id)
        session.add(
            LlmCall(
                llm_call_id="llmcall_auto_patch_rejected_before_sub0",
                provider="fake",
                model="fake",
                node_id="style_patch",
                step="soft_patch",
                project_id=scene.project_id or chapter.project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene_id,
                scope_type="scene",
                scope_id=scene_id,
                execution_id=execution_id,
                execution_step_key="soft_patch:auto_critique:0",
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:01Z",
                error_code="LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
            )
        )
        session.commit()
        raise _CrashAfterRejectedPatch()

    monkeypatch.setattr(first_service, "generate_style_patch", reject_then_crash)
    with pytest.raises(_CrashAfterRejectedPatch):
        Orchestrator(
            session,
            scene_generation_service=first_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    if not gate_open:
        state.scene_tokens_used = state.scene_token_budget
        session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before = _total_llm_budget_charged(session)

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    failure = refs["soft_auto_critique_patch_failure"]
    assert refs["soft_auto_critique_outcome"] == "patch_failed"
    assert failure["outcome"] == "rejected_before_dispatch"
    assert failure["llm_call_id"] == "llmcall_auto_patch_rejected_before_sub0"
    assert failure["provider_execution_mode"] == "online"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_patch_reserved_crash_reconciles_before_gate(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    class _CrashAfterPatchReservation(BaseException):
        pass

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-reserved-recover:{gate_open}"

    def rule_no_call(content, *_args, **kwargs):
        context = kwargs["llm_context"]
        return replace(
            auto_critique(content),
            outcome="not_invoked",
            reason="feature_disabled",
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rule_no_call,
    )
    generation_client = _CountingGenerationClient()
    first_service = SceneGenerationService(session, llm_client=generation_client)

    def reserve_then_crash(scene_id, *_args, **_kwargs):
        scene = session.get(SceneCard, scene_id)
        chapter = session.get(ChapterGoal, scene.chapter_id)
        call_id = "llmcall_auto_patch_reserved_before_sub0"
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id="style_patch",
                step="soft_patch",
                project_id=scene.project_id or chapter.project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene_id,
                scope_type="scene",
                scope_id=scene_id,
                execution_id=execution_id,
                execution_step_key="soft_patch:auto_critique:0",
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_auto_patch_reserved_before_sub0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=None,
            )
        )
        state = session.get(SceneRunState, scene_id)
        state.scene_tokens_reserved = 20
        session.commit()
        raise _CrashAfterPatchReservation()

    monkeypatch.setattr(first_service, "generate_style_patch", reserve_then_crash)
    with pytest.raises(_CrashAfterPatchReservation):
        Orchestrator(
            session,
            scene_generation_service=first_service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    if not gate_open:
        state.scene_tokens_used = state.scene_token_budget
        session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))

    retry = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session,
            llm_client=generation_client,
        ),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    if gate_open:
        with pytest.raises(RuntimeError, match="fail after style checkpoint"):
            retry.run_scene("CH_RESUME_SC01", execution_id=execution_id)
        assert len(generation_client.requests) == provider_calls + 1
        assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count + 1
    else:
        from novel_system.services.scene_criticality import classify_scene

        direct = _activate_checkpoint_orchestrator(session, execution_id)
        scene = session.get(SceneCard, "CH_RESUME_SC01")
        draft = session.get(SceneDraft, state.current_style_draft_row_id)
        draft_parent = session.get(LlmCall, draft.generation_llm_call_id)
        selected = StyleGenerationResult(
            row_id=draft.row_id,
            content=draft.content,
            llm_call_id=draft.generation_llm_call_id,
            bundle_id=draft.source_bundle_id,
            bundle_hash=draft.source_bundle_hash,
            execution_step_key=draft_parent.execution_step_key,
            artifact_execution_id=draft_parent.execution_id,
        )
        with pytest.raises(DomainError) as blocked:
            direct._ensure_soft_qc_subcheckpoints(
                scene=scene,
                contract=direct.execution_contract_service.get_or_create(
                    scene.scene_id,
                    actor_ref="orchestrator",
                ),
                bundle=direct._load_checkpoint_bundle(scene.scene_id),
                criticality=classify_scene(scene),
                selected_style_generation=selected,
                optional_spend_allowed=lambda: False,
            )
        assert blocked.value.code == "RUN_CHECKPOINT_CORRUPT"
        assert len(generation_client.requests) == provider_calls
        assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    session.refresh(state)
    released = session.get(LlmCall, "llmcall_auto_patch_reserved_before_sub0")
    assert released.accounting_status == "released"
    assert state.scene_tokens_reserved == 0


def _completed_auto_critique_stub(
    session,
    calls: list[str],
    *,
    should_rewrite: bool = False,
):
    from novel_system.services.auto_critique import (
        CritiqueResult,
        auto_critique,
        critique_llm_contribution_hash,
    )

    def run(*_args, **kwargs):
        context = kwargs["llm_context"]
        rule = auto_critique(str(_args[0]))
        calls.append(context.execution_step_key)
        call_id = "llmcall_auto_checkpoint"
        contribution = {
            "should_rewrite": should_rewrite,
            "issues": (
                [
                    {
                        "dimension": "pacing",
                        "directive": "tighten the opening",
                        "evidence": "delayed turn",
                    }
                ]
                if should_rewrite
                else []
            ),
        }
        if session.get(LlmCall, call_id) is None:
            session.add(
                LlmCall(
                    llm_call_id=call_id,
                    provider="fake",
                    model="fake",
                    node_id=context.node_id,
                    step=context.step,
                    project_id=context.project_id,
                    chapter_id=context.chapter_id,
                    scene_id=context.scene_id,
                    scope_type=context.scope_type,
                    scope_id=context.scope_id,
                    run_job_id=context.run_job_id,
                    execution_id=context.execution_id,
                    execution_step_key=context.execution_step_key,
                    request_payload_summary={
                        "_accounting_provider_execution_mode": "online"
                    },
                    response_payload_summary={
                        "auto_critique_parsed_llm_hash":
                        critique_llm_contribution_hash(contribution)
                    },
                    prompt_tokens=17,
                    completion_tokens=0,
                    total_tokens=17,
                    estimated_tokens=17,
                    reserved_tokens=17,
                    budget_charged_tokens=17,
                    latency_ms=3,
                    usage_is_estimate=True,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                )
            )
            session.add(
                LlmCallAttempt(
                    attempt_id="attempt_auto_checkpoint_0",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    prompt_tokens=17,
                    completion_tokens=0,
                    total_tokens=17,
                    estimated_tokens=17,
                    reserved_tokens=17,
                    budget_charged_tokens=17,
                    latency_ms=3,
                    usage_is_estimate=True,
                    accounting_status="settled",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                )
            )
            session.commit()
        return CritiqueResult(
            should_rewrite=rule.should_rewrite or should_rewrite,
            directives=list(rule.directives)
            + (
                ["[LLM路pacing] tighten the opening (evidence: delayed turn)"]
                if should_rewrite
                else []
            ),
            dimension_scores=dict(rule.dimension_scores),
            flagged_dimensions=list(rule.flagged_dimensions)
            + (["pacing"] if should_rewrite and "pacing" not in rule.flagged_dimensions else []),
            outcome="completed",
            rule_should_rewrite=rule.should_rewrite,
            rule_directives=list(rule.directives),
            rule_dimension_scores=dict(rule.dimension_scores),
            rule_flagged_dimensions=list(rule.flagged_dimensions),
            llm_contribution=contribution,
            llm_call_id=call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
        )

    return run


def _run_to_completed_auto_critique_sub0(session, monkeypatch, *, execution_id: str):
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls),
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    return generation_client, calls


def _run_to_patched_auto_critique_sub0(session, monkeypatch, *, execution_id: str):
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.run_checkpoint_json["artifact_refs"]["soft_auto_critique_outcome"] == "patched"
    return generation_client, calls


def _activate_checkpoint_orchestrator(session, execution_id: str) -> Orchestrator:
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(
            session,
            llm_client=_CountingGenerationClient(),
        ),
    )
    orchestrator._execution_id = execution_id
    orchestrator._run_job_id = None
    orchestrator._checkpoint_service = SceneRunCheckpointService(session)
    return orchestrator


def _total_llm_budget_charged(session) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(LlmCall.budget_charged_tokens), 0))
        )
        or 0
    )


def test_completed_auto_critique_sub0_resumes_without_replay(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-envelope-resume"
    generation_client, critique_calls = _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    resumed = _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    assert critique_calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert resumed.row_id == refs["soft_input_draft_row_id"]


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "ordinal",
        "status",
        "tokens",
        "parent_scope",
        "parent_node",
        "parent_chapter",
        "parent_run_job",
    ],
)
def test_auto_critique_patch_generation_child_tamper_blocks_without_replay(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-patch-child:{tamper}"
    generation_client, _critique_calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    parent = session.get(LlmCall, refs["soft_input_llm_call_id"])
    child = session.scalar(
        select(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == parent.llm_call_id
        )
    )
    if tamper == "parent_scope":
        parent.scope_id = "SC_DETACHED"
    elif tamper == "parent_node":
        parent.node_id = "soft_qc"
    elif tamper == "parent_chapter":
        parent.chapter_id = "CH_DETACHED"
    elif tamper == "parent_run_job":
        parent.run_job_id = "job-detached"
    elif tamper == "missing":
        session.delete(child)
    elif tamper == "ordinal":
        child.provider_attempt_no = 4
    elif tamper == "status":
        child.accounting_status = "failed"
        child.error_code = "LLM_HTTP_REQUEST_FAILED"
    else:
        child.prompt_tokens += 1
        child.total_tokens += 1
    session.commit()
    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


@pytest.mark.parametrize(
    "tamper",
    [
        "hash",
        "product_rehash",
        "semantic_rule_rehash",
        "field_matrix",
        "call_id",
        "owner",
        "step",
        "run_job",
        "parent_scope",
        "parent_node",
        "parent_scene",
        "parent_chapter",
        "parent_run_job",
        "child_missing",
        "child_ordinal",
        "child_dispatch",
        "child_status",
        "child_error_code",
        "child_tokens",
    ],
)
def test_auto_critique_sub0_tamper_blocks_before_provider_replay(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-tamper:{tamper}"
    generation_client, critique_calls = _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    parent = session.get(LlmCall, product["llm_call_id"])
    attempt = session.get(LlmCallAttempt, "attempt_auto_checkpoint_0")
    if tamper == "hash":
        product["should_rewrite"] = not product["should_rewrite"]
    elif tamper == "product_rehash":
        product["directives"] = ["tampered but locally rehashed"]
    elif tamper == "semantic_rule_rehash":
        product["rule_directives"] = ["tampered deterministic rule"]
    elif tamper == "field_matrix":
        product.pop("rule_directives")
    elif tamper == "call_id":
        product["llm_call_id"] = "llmcall_detached"
    elif tamper == "owner":
        product["execution_id"] = "exec-detached"
    elif tamper == "step":
        product["execution_step_key"] = "soft_qc:auto_critique:9"
    elif tamper == "run_job":
        product["run_job_id"] = "job-detached"
    elif tamper == "parent_scope":
        parent.scope_id = "SC_DETACHED"
    elif tamper == "parent_node":
        parent.node_id = "hard_qc"
    elif tamper == "parent_scene":
        parent.scene_id = "SC_DETACHED"
    elif tamper == "parent_chapter":
        parent.chapter_id = "CH_DETACHED"
    elif tamper == "parent_run_job":
        parent.run_job_id = "job-detached"
    elif tamper == "child_missing":
        session.delete(attempt)
    elif tamper == "child_ordinal":
        attempt.provider_attempt_no = 3
    elif tamper == "child_dispatch":
        attempt.request_dispatched_at = None
    elif tamper == "child_status":
        attempt.accounting_status = "failed"
        attempt.error_code = "LLM_HTTP_REQUEST_FAILED"
    elif tamper == "child_error_code":
        attempt.error_code = "LLM_HTTP_REQUEST_FAILED"
    elif tamper == "child_tokens":
        attempt.prompt_tokens += 1
        attempt.total_tokens += 1
        attempt.estimated_tokens += 1
        attempt.reserved_tokens += 1
        attempt.budget_charged_tokens += 1
    if tamper != "hash":
        payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(product)
    if tamper == "semantic_rule_rehash":
        parent.response_payload_summary = {
            **dict(parent.response_payload_summary or {}),
            "auto_critique_product_hash": Orchestrator._json_hash(product),
        }
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    refs = state.run_checkpoint_json["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert critique_calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_patch_semantic_tamper_with_synchronized_hashes_is_rejected(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-semantic-tamper"
    generation_client, _calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    product["should_rewrite"] = False
    payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(
        product
    )
    critique_parent = session.get(LlmCall, product["llm_call_id"])
    critique_parent.response_payload_summary = {
        **dict(critique_parent.response_payload_summary or {}),
        "auto_critique_product_hash": Orchestrator._json_hash(product),
    }
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_creation_rejects_invalid_llm_contribution_before_sub0_save(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-invalid-contribution-create"
    calls: list[str] = []
    completed = _completed_auto_critique_stub(
        session,
        calls,
        should_rewrite=True,
    )

    def invalid_contribution(*args, **kwargs):
        result = completed(*args, **kwargs)
        result.llm_contribution["issues"][0]["directive"] = ""
        return result

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        invalid_contribution,
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(DomainError) as corrupt:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert not (
        state.run_checkpoint == "soft_qc_ready"
        and state.run_checkpoint_json.get("sub_index") == 0
    )


@pytest.mark.parametrize(
    "tamper",
    ["parent_scope", "parent_node", "parent_chapter", "parent_run_job"],
)
def test_auto_critique_patch_creation_rejects_detached_generation_parent_before_sub0(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-create-owner:{tamper}"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    generation_client = _CountingGenerationClient()
    service = SceneGenerationService(session, llm_client=generation_client)
    original_patch = service.generate_style_patch

    def detached_parent(*args, **kwargs):
        result = original_patch(*args, **kwargs)
        parent = session.get(LlmCall, result.llm_call_id)
        if tamper == "parent_scope":
            parent.scope_id = "SC_DETACHED"
        elif tamper == "parent_node":
            parent.node_id = "soft_qc"
        elif tamper == "parent_chapter":
            parent.chapter_id = "CH_DETACHED"
        else:
            parent.run_job_id = "job-detached"
        session.flush()
        return result

    monkeypatch.setattr(service, "generate_style_patch", detached_parent)
    with pytest.raises(LLMAccountingError) as invalid:
        Orchestrator(
            session,
            scene_generation_service=service,
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert invalid.value.code == "LLM_ACCOUNTING_PRODUCT_LEDGER_INVALID"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert not (
        state.run_checkpoint == "soft_qc_ready"
        and state.run_checkpoint_json.get("sub_index") == 0
    )


def test_auto_critique_parsed_llm_anchor_blocks_synchronized_product_tamper(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-parsed-anchor"
    _generation_client, _calls = _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    product = payload["artifact_refs"]["soft_auto_critique_decision"]
    product["llm_contribution"]["issues"][0]["directive"] = "tampered directive"
    product["directives"][-1] = (
        "[LLM路pacing] tampered directive (evidence: delayed turn)"
    )
    product_hash = Orchestrator._json_hash(product)
    payload["artifact_hashes"]["soft_auto_critique_decision"] = product_hash
    parent = session.get(LlmCall, product["llm_call_id"])
    parent.response_payload_summary = {
        **dict(parent.response_payload_summary or {}),
        "auto_critique_product_hash": product_hash,
        "auto_critique_llm_merge_hash": "synchronized-obsolete-mirror",
    }
    state.run_checkpoint_json = payload
    session.commit()

    refs = payload["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_checkpoint_output_rejects_online_parent_forged_as_zero_attempt_offline(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:forged-offline-parent"
    _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    product = refs["soft_auto_critique_decision"]
    parent = session.get(LlmCall, product["llm_call_id"])
    attempt = session.scalar(
        select(LlmCallAttempt).where(LlmCallAttempt.llm_call_id == parent.llm_call_id)
    )
    session.delete(attempt)
    parent.provider = "offline_deterministic"
    parent.request_payload_summary = {
        **dict(parent.request_payload_summary or {}),
        "_accounting_provider_execution_mode": "offline_deterministic",
    }
    parent.request_dispatched_at = None
    parent.prompt_tokens = 0
    parent.completion_tokens = 0
    parent.total_tokens = 0
    parent.estimated_tokens = 0
    parent.reserved_tokens = 0
    parent.budget_charged_tokens = 0
    parent.latency_ms = 0
    parent.usage_is_estimate = False
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize(
    "tamper",
    ["snapshot", "marker", "coordinated_zero_attempt_offline"],
)
def test_auto_critique_patch_generation_mode_snapshot_blocks_tamper(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-mode-tamper:{tamper}"
    _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    parent = session.get(LlmCall, refs["soft_input_llm_call_id"])

    if tamper == "snapshot":
        refs["soft_input_provider_execution_mode"] = "offline_deterministic"
        payload["artifact_hashes"]["soft_input_provider_execution_mode"] = (
            Orchestrator._text_hash("offline_deterministic")
        )
        state.run_checkpoint_json = payload
    else:
        parent.request_payload_summary = {
            **dict(parent.request_payload_summary or {}),
            "_accounting_provider_execution_mode": "offline_deterministic",
        }
        if tamper == "coordinated_zero_attempt_offline":
            attempts = session.scalars(
                select(LlmCallAttempt).where(
                    LlmCallAttempt.llm_call_id == parent.llm_call_id
                )
            ).all()
            for attempt in attempts:
                session.delete(attempt)
            parent.provider = "offline_deterministic"
            parent.request_dispatched_at = None
            for field_name in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "estimated_tokens",
                "reserved_tokens",
                "budget_charged_tokens",
                "latency_ms",
            ):
                setattr(parent, field_name, 0)
            parent.error_code = None
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_auto_critique_patch_success_resumes_with_a_fresh_online_runner(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-fresh-online-runner"
    _run_to_patched_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_input_provider_execution_mode"] == "online"

    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_unchanged_resumes_with_a_fresh_online_runner(
    session,
    monkeypatch,
) -> None:
    from novel_system.services.auto_critique import CritiqueResult

    _seed_resume_scene(session)
    execution_id = "idempotency:auto-unchanged-mode-switch"
    monkeypatch.setattr(
        "novel_system.services.auto_critique.auto_critique",
        lambda *_args, **_kwargs: CritiqueResult(should_rewrite=False),
    )
    _run_to_completed_auto_critique_sub0(
        session,
        monkeypatch,
        execution_id=execution_id,
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_auto_critique_outcome"] == "unchanged"
    assert refs["soft_input_provider_execution_mode"] == "online"

    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_patch_success_resumes_after_online_client_switch(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-online-client-switch"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    first_online_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=first_online_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["soft_auto_critique_outcome"] == "patched"
    assert refs["soft_input_provider_execution_mode"] == "online"
    assert session.get(LlmCall, refs["soft_input_llm_call_id"]).provider == "fake-provider"

    generation = _activate_checkpoint_orchestrator(
        session,
        execution_id,
    )._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


@pytest.mark.parametrize("tamper", ["snapshot", "marker"])
def test_auto_critique_patch_failure_mode_snapshot_blocks_tamper(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-patch-failure-mode-tamper:{tamper}"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_FailAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    refs = payload["artifact_refs"]
    product = refs["soft_auto_critique_patch_failure"]
    parent = session.get(LlmCall, product["llm_call_id"])
    if tamper == "snapshot":
        product["provider_execution_mode"] = "offline_deterministic"
        product_hash = Orchestrator._json_hash(product)
        payload["artifact_hashes"]["soft_auto_critique_patch_failure"] = product_hash
        parent.response_payload_summary = {
            **dict(parent.response_payload_summary or {}),
            "auto_critique_patch_failure_hash": product_hash,
        }
        state.run_checkpoint_json = payload
    else:
        parent.request_payload_summary = {
            **dict(parent.request_payload_summary or {}),
            "_accounting_provider_execution_mode": "offline_deterministic",
        }
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_auto_critique_patch_failure_resumes_after_online_to_offline_config_switch(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-patch-failure-mode-switch"
    calls: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        _completed_auto_critique_stub(session, calls, should_rewrite=True),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_FailAutoCritiquePatchClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    resumed = _activate_checkpoint_orchestrator(session, execution_id)
    monkeypatch.setattr(
        resumed.scene_generation_service._llm_runner,
        "_provider_execution_mode",
        lambda: "offline_deterministic",
    )
    generation = resumed._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )
    assert generation.row_id == refs["soft_input_draft_row_id"]


def test_auto_critique_provider_success_without_sub0_blocks_resend(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-output-missing"
    calls: list[str] = []
    completed = _completed_auto_critique_stub(session, calls)

    def crash_after_provider(*args, **kwargs):
        completed(*args, **kwargs)
        raise RuntimeError("crash before auto critique checkpoint")

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        crash_after_provider,
    )
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="crash before auto critique checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    provider_calls = len(generation_client.requests)
    charged_before_resume = _total_llm_budget_charged(session)

    orchestrator = _activate_checkpoint_orchestrator(session, execution_id)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    state = session.get(SceneRunState, scene.scene_id)
    draft = session.get(SceneDraft, state.current_style_draft_row_id)
    parent = session.get(LlmCall, draft.generation_llm_call_id)
    selected = StyleGenerationResult(
        row_id=draft.row_id,
        content=draft.content,
        llm_call_id=draft.generation_llm_call_id,
        bundle_id=draft.source_bundle_id,
        bundle_hash=draft.source_bundle_hash,
        execution_step_key=parent.execution_step_key,
        artifact_execution_id=parent.execution_id,
    )
    from novel_system.services.scene_criticality import classify_scene

    with pytest.raises(DomainError) as missing:
        orchestrator._ensure_soft_qc_subcheckpoints(
            scene=scene,
            contract=orchestrator.execution_contract_service.get_or_create(
                scene.scene_id,
                actor_ref="orchestrator",
            ),
            bundle=orchestrator._load_checkpoint_bundle(scene.scene_id),
            criticality=classify_scene(scene),
            selected_style_generation=selected,
            optional_spend_allowed=lambda: True,
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert calls == ["soft_qc:auto_critique:0"]
    assert len(generation_client.requests) == provider_calls
    assert _total_llm_budget_charged(session) == charged_before_resume


def test_auto_critique_released_online_tombstone_blocks_current_offline_no_call(
    session,
    monkeypatch,
) -> None:
    from dataclasses import replace

    from novel_system.services.auto_critique import (
        llm_auto_critique as real_llm_auto_critique,
    )
    from novel_system.services.scene_criticality import classify_scene as real_classify_scene

    class _CrashAfterCritiqueReservation(BaseException):
        pass

    class _OfflineCritiqueMustNotRun:
        provider_execution_mode = "offline_deterministic"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def run_task(self, **_kwargs):
            self.calls.append("provider")
            raise AssertionError("offline critique must remain a no-call path")

    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_criticality.classify_scene",
        lambda *args, **kwargs: replace(
            real_classify_scene(*args, **kwargs),
            skip_critique=False,
        ),
    )
    execution_id = "idempotency:auto-critique-released-online-to-offline"
    call_id = "llmcall_auto_critique_released_before_sub0"

    def reserve_then_crash(*_args, **kwargs):
        context = kwargs["llm_context"]
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="reserved",
            )
        )
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_auto_critique_released_before_sub0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=10,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=20,
                reserved_tokens=20,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="reserved",
                request_dispatched_at=None,
            )
        )
        state = session.get(SceneRunState, context.scene_id)
        state.scene_tokens_reserved = 20
        session.commit()
        raise _CrashAfterCritiqueReservation()

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        reserve_then_crash,
    )
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(_CrashAfterCritiqueReservation):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        real_llm_auto_critique,
    )
    offline_runner = _OfflineCritiqueMustNotRun()
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_auto_critique_runner",
        lambda _self: offline_runner,
    )

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    parent = session.get(LlmCall, call_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert "released accounting tombstone" in corrupt.value.message
    assert parent.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert offline_runner.calls == []
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count
    assert _total_llm_budget_charged(session) == charged_before_resume


@pytest.mark.parametrize("gate_open", [False, True])
def test_auto_critique_rejected_parent_rebuilds_product_without_replay(
    session,
    monkeypatch,
    gate_open: bool,
) -> None:
    from novel_system.services.auto_critique import llm_auto_critique as real_llm_auto_critique

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-rejected-recover:{gate_open}"
    call_id = "llmcall_auto_rejected_before_sub0"
    error_code = "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"

    def rejected_then_crash(*_args, **kwargs):
        context = kwargs["llm_context"]
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                request_dispatched_at=None,
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
        session.commit()
        raise RuntimeError("crash after rejected critique before sub0")

    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        rejected_then_crash,
    )
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(RuntimeError, match="crash after rejected critique before sub0"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    provider_calls = len(generation_client.requests)
    parent_count = session.scalar(select(func.count()).select_from(LlmCall))
    charged_before_resume = _total_llm_budget_charged(session)
    monkeypatch.setattr(
        "novel_system.services.auto_critique.llm_auto_critique",
        real_llm_auto_critique,
    )
    recovered_runner_calls: list[str] = []

    class _MustNotRunRecoveredCritique:
        def run_task(self, **_kwargs):
            recovered_runner_calls.append("provider")
            raise AssertionError("recovered rejected product must prevent critique resend")

    recovered_runner = _MustNotRunRecoveredCritique() if gate_open else None
    monkeypatch.setattr(
        Orchestrator,
        "_resolve_auto_critique_runner",
        lambda _self: recovered_runner,
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    product = state.run_checkpoint_json["artifact_refs"]["soft_auto_critique_decision"]
    assert state.run_checkpoint == "soft_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert product["outcome"] == "rejected_before_dispatch"
    assert product["llm_call_id"] == call_id
    assert product["reason"] == "pre_dispatch_rejection"
    assert product["error_code"] == error_code

    refs = state.run_checkpoint_json["artifact_refs"]
    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    # The rejected critic is not replayed. Its deterministic rule product may still
    # authorize the first (non-replay) style patch.
    assert len(generation_client.requests) == provider_calls + 1
    assert recovered_runner_calls == []
    assert session.scalar(select(func.count()).select_from(LlmCall)) == parent_count + 1
    assert _total_llm_budget_charged(session) > charged_before_resume


@pytest.mark.parametrize("tamper", ["rule_product", "reason"])
def test_auto_critique_no_call_sub0_has_reason_and_revalidates_rule_product(
    session,
    monkeypatch,
    tamper: str,
) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:auto-critique-no-call"
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    product = payload["artifact_refs"]["soft_auto_critique_decision"]
    assert product["outcome"] == "not_invoked"
    assert product["reason"]
    assert product["llm_call_id"] is None
    assert product["execution_id"] == execution_id
    assert product["execution_step_key"] == "soft_qc:auto_critique:0"

    if tamper == "rule_product":
        product["directives"] = ["tampered no-call rule result"]
    else:
        product["reason"] = "arbitrary_no_call_reason"
    payload["artifact_hashes"]["soft_auto_critique_decision"] = Orchestrator._json_hash(product)
    state.run_checkpoint_json = payload
    session.commit()
    refs = payload["artifact_refs"]
    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


@pytest.mark.parametrize("outcome", ["rejected_before_dispatch", "provider_failed"])
def test_auto_critique_degraded_sub0_validates_outcome_specific_attempt_ledger(
    session,
    monkeypatch,
    outcome: str,
) -> None:
    from dataclasses import replace
    from novel_system.services.auto_critique import auto_critique

    _seed_resume_scene(session)
    execution_id = f"idempotency:auto-critique-{outcome}"
    call_id = f"llmcall_auto_{outcome}"
    error_code = (
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
        if outcome == "rejected_before_dispatch"
        else "LLM_HTTP_REQUEST_FAILED"
    )

    def degraded(*_args, **kwargs):
        context = kwargs["llm_context"]
        dispatched = outcome == "provider_failed"
        tokens = 19 if dispatched else 0
        session.add(
            LlmCall(
                llm_call_id=call_id,
                provider="fake",
                model="fake",
                node_id=context.node_id,
                step=context.step,
                project_id=context.project_id,
                chapter_id=context.chapter_id,
                scene_id=context.scene_id,
                scope_type=context.scope_type,
                scope_id=context.scope_id,
                run_job_id=context.run_job_id,
                execution_id=context.execution_id,
                execution_step_key=context.execution_step_key,
                request_payload_summary={
                    "_accounting_provider_execution_mode": "online"
                },
                prompt_tokens=tokens,
                completion_tokens=0,
                total_tokens=tokens,
                estimated_tokens=tokens,
                reserved_tokens=tokens,
                budget_charged_tokens=tokens,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="failed" if dispatched else "rejected",
                request_dispatched_at="2026-07-14T00:00:00Z" if dispatched else None,
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
        if dispatched:
            session.add(
                LlmCallAttempt(
                    attempt_id=f"attempt_auto_{outcome}_0",
                    llm_call_id=call_id,
                    provider_attempt_no=0,
                    dispatch_kind="initial",
                    request_max_output_tokens=0,
                    prompt_tokens=tokens,
                    completion_tokens=0,
                    total_tokens=tokens,
                    estimated_tokens=tokens,
                    reserved_tokens=tokens,
                    budget_charged_tokens=tokens,
                    latency_ms=0,
                    usage_is_estimate=True,
                    accounting_status="failed",
                    request_dispatched_at="2026-07-14T00:00:00Z",
                    settled_at="2026-07-14T00:00:01Z",
                    error_code=error_code,
                )
            )
        session.commit()
        return replace(
            auto_critique(_args[0]),
            outcome=outcome,
            llm_call_id=call_id,
            execution_id=context.execution_id,
            execution_step_key=context.execution_step_key,
            run_job_id=context.run_job_id,
            reason=(
                "pre_dispatch_rejection"
                if outcome == "rejected_before_dispatch"
                else "provider_call_failed"
            ),
            error_code=error_code,
        )

    monkeypatch.setattr("novel_system.services.auto_critique.llm_auto_critique", degraded)
    generation_client = _CountingGenerationClient()
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
        "CH_RESUME_SC01",
        prefix="soft_input",
        expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
        expected_stages={"style_draft", "de_template", "style_patch"},
    )

    if outcome == "rejected_before_dispatch":
        session.add(
            LlmCallAttempt(
                attempt_id="attempt_illegal_rejected_0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                request_max_output_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                latency_ms=0,
                usage_is_estimate=True,
                accounting_status="rejected",
                settled_at="2026-07-14T00:00:01Z",
                error_code=error_code,
            )
        )
    else:
        session.delete(session.get(LlmCallAttempt, f"attempt_auto_{outcome}_0"))
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        _activate_checkpoint_orchestrator(session, execution_id)._load_soft_draft_checkpoint(
            "CH_RESUME_SC01",
            prefix="soft_input",
            expected_source_draft_row_id=refs["soft_input_source_draft_row_id"],
            expected_stages={"style_draft", "de_template", "style_patch"},
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_de_template_selected_soft_input_resumes_from_sub0(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:test"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:de-template-soft-input")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    selected = session.get(SceneDraft, refs["soft_input_draft_row_id"])
    assert state.run_checkpoint_json["sub_index"] == 0
    assert selected.stage == "de_template"
    provider_calls = len(generation_client.requests)

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id="idempotency:de-template-soft-input")

    assert len(generation_client.requests) == provider_calls


def test_style_base_checkpoint_resumes_only_de_template_after_interruption(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:resume-base"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:style-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_before_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:0:de_template":
            raise RuntimeError("interrupt after durable style base")

    first._reconcile_execution_step = interrupt_before_de_template
    with pytest.raises(RuntimeError, match="interrupt after durable style base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint == "hard_qc_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert len(work_items) == 1
    assert work_items[0]["slot_key"] == "initial:0"
    assert work_items[0]["base"]["row_id"] == "draft_style_CH_RESUME_SC01_v1"
    assert work_items[0]["final"] is None
    assert [request.node_id for request in generation_client.requests] == ["neutral_draft", "style_draft"]
    base_call = session.get(LlmCall, work_items[0]["base"]["llm_call_id"])
    base_accounting = (
        base_call.accounting_status,
        base_call.reserved_tokens,
        base_call.budget_charged_tokens,
        base_call.total_tokens,
    )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    work_items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert work_items[0]["gate_decision"]["triggered"] is True
    assert work_items[0]["final"]["stage"] == "de_template"
    assert work_items[0]["final"]["source_base_row_id"] == work_items[0]["base"]["row_id"]
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 1
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 1
    session.refresh(base_call)
    assert (
        base_call.accounting_status,
        base_call.reserved_tokens,
        base_call.budget_charged_tokens,
        base_call.total_tokens,
    ) == base_accounting
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "style_draft",
            AttemptTracker.status == "completed",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(SceneDraft).where(
            SceneDraft.row_id == work_items[0]["base"]["row_id"]
        )
    ) == 1


def test_failed_de_template_is_a_durable_final_outcome_and_is_not_replayed(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:failed-de-template"],
            "findings": [],
        },
    )
    generation_client = _FailDeTemplateClient()
    execution_id = "idempotency:failed-de-template-final"

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    outcome = item["de_template_outcome"]
    assert outcome["status"] == "failed"
    assert outcome["execution_step_key"] == "style_draft:0:de_template"
    assert outcome["accounting_status"] == "failed"
    assert item["gate_decision"]["triggered"] is True
    assert item["final"]["row_id"] == item["base"]["row_id"]
    provider_calls = len(generation_client.requests)

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "de_template",
            AttemptTracker.status == "failed",
        )
    ) == 1


def test_failed_de_template_recovery_rejects_error_code_detached_from_parent_call(
    session,
    monkeypatch,
) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:failed-de-template-error-code"],
            "findings": [],
        },
    )
    generation_client = _FailDeTemplateClient()
    execution_id = "idempotency:failed-de-template-error-code"
    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    item = payload["artifact_refs"]["style_work_items"][0]
    outcome = item["de_template_outcome"]
    parent_call = session.get(LlmCall, outcome["llm_call_id"])
    assert parent_call.error_code == outcome["error_code"]
    tampered_error_code = "TAMPERED_DE_TEMPLATE_ERROR"
    outcome["error_code"] = tampered_error_code
    payload["artifact_hashes"]["style_work_items"] = Orchestrator._json_hash(
        payload["artifact_refs"]["style_work_items"]
    )
    state.run_checkpoint_json = payload
    failed_attempt = next(
        attempt
        for attempt in session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == "CH_RESUME_SC01",
                AttemptTracker.step == "de_template",
                AttemptTracker.status == "failed",
            )
        ).scalars()
        if (attempt.details_json or {}).get("llm_call_id") == parent_call.llm_call_id
    )
    failed_attempt.details_json = {
        **(failed_attempt.details_json or {}),
        "error_code": tampered_error_code,
    }
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=_FailNearFinal(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert exc_info.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls


def test_best_of_n_resumes_candidate_de_template_without_replaying_its_base(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:candidate-resume-base"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:candidate-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_first_candidate_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:0:de_template":
            raise RuntimeError("interrupt candidate after base")

    first._reconcile_execution_step = interrupt_first_candidate_de_template
    with pytest.raises(RuntimeError, match="interrupt candidate after base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 0
    assert [(item["slot_key"], item["final"]) for item in items] == [("initial:0", None)]

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert [item["slot_key"] for item in items] == ["initial:0", "initial:1"]
    assert all(item["de_template_outcome"]["status"] == "completed" for item in items)
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 2
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 2


def test_completed_candidate_de_template_survives_next_candidate_failure(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:next-candidate-failure"],
            "findings": [],
        },
    )
    generation_client = _FailFourthGenerationClient()
    execution_id = "idempotency:de-template-then-next-candidate-fails"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        )

    with pytest.raises(ValueError, match="next candidate failed after de-template"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 1
    assert len(items) == 1
    assert items[0]["de_template_outcome"]["status"] == "completed"
    completed_row_id = items[0]["final"]["row_id"]
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert exc_info.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert len(generation_client.requests) == provider_calls
    session.refresh(state)
    assert state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]["final"]["row_id"] == completed_row_id


def test_progressive_topup_resumes_its_locked_base_without_replay(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.constraint_intensity = 0.5
    session.commit()
    monkeypatch.setattr(Orchestrator, "_best_of_n_count", staticmethod(lambda contract, criticality=None: 2))
    monkeypatch.setattr("novel_system.services.scene_generation._candidate_dispersion", lambda contents: 0.0)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:topup-resume"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:topup-base-de-template-resume"
    first = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_FailAfterStyle(),
    )
    original_reconcile = first._reconcile_execution_step

    def interrupt_topup_de_template(step_key: str) -> None:
        original_reconcile(step_key)
        if step_key == "style_draft:topup:1:de_template":
            raise RuntimeError("interrupt topup after base")

    first._reconcile_execution_step = interrupt_topup_de_template
    with pytest.raises(RuntimeError, match="interrupt topup after base"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert state.run_checkpoint_json["sub_index"] == 4
    assert [item["slot_key"] for item in items] == ["initial:0", "initial:1", "topup:1"]
    assert items[-1]["final"] is None

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    items = state.run_checkpoint_json["artifact_refs"]["style_work_items"]
    assert items[-1]["de_template_outcome"]["status"] == "completed"
    assert [request.node_id for request in generation_client.requests].count("style_draft") == 3
    assert [request.node_id for request in generation_client.requests].count("style_patch") == 3


def test_no_anti_template_trigger_persists_base_equals_final(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": False,
            "rewrite_pass": 0,
            "score": 1.0,
            "risk_dimensions": [],
            "quality_signal_ids": [],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id="idempotency:no-de-template-required")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    assert item["gate_decision"]["triggered"] is False
    assert item["de_template_outcome"] == {"status": "not_required"}
    assert item["base"]["row_id"] == item["final"]["row_id"]
    assert item["base"]["content_hash"] == item["final"]["content_hash"]
    assert item["base"]["llm_call_id"] == item["final"]["llm_call_id"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [("delete", "RUN_CHECKPOINT_OUTPUT_MISSING"), ("tamper", "RUN_CHECKPOINT_CORRUPT")],
)
def test_completed_de_template_recovery_validates_its_base_lineage(
    session,
    monkeypatch,
    mutation: str,
    expected_code: str,
) -> None:
    _seed_resume_scene(session)
    monkeypatch.setattr(
        "novel_system.services.scene_generation._anti_template_quality_gate",
        lambda *args, **kwargs: {
            "triggered": True,
            "rewrite_pass": 1,
            "score": 0.0,
            "risk_dimensions": ["model_voice"],
            "quality_signal_ids": ["quality:lineage-validation"],
            "findings": [],
        },
    )
    generation_client = _CountingGenerationClient()
    execution_id = f"idempotency:de-template-lineage-{mutation}"

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    item = state.run_checkpoint_json["artifact_refs"]["style_work_items"][0]
    assert item["base"]["row_id"] != item["final"]["row_id"]
    base = session.get(SceneDraft, item["base"]["row_id"])
    if mutation == "delete":
        session.delete(base)
    else:
        base.content = "tampered durable style base"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as exc_info:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_FailAfterStyle(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert exc_info.value.code == expected_code
    assert len(generation_client.requests) == provider_calls


def test_near_final_checkpoint_resume_archives_without_repeating_prior_nodes(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-final-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "near_final_ready"
    final_row_id = state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"]
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:near-final-resume",
    )

    assert result["scene_status"] == "archived"
    session.refresh(state)
    assert state.run_checkpoint == "archived"
    assert state.current_final_scene_row_id == final_row_id
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_near_eval0_checkpoint_resumes_at_rewrite_without_replaying_eval0(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_rewrite(step_key: str) -> None:
        if step_key == "near_final_rewrite:0":
            raise RuntimeError("stop after near eval0 checkpoint")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_rewrite
    with pytest.raises(RuntimeError, match="stop after near eval0 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval0-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.run_checkpoint_json["artifact_refs"]["near_eval0_evaluation_id"]
    provider_calls = len(generation_client.requests)

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval0-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls + 1
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "near_final_acceptance:0",
            )
        ).scalars().all()
    ) == 1


def test_near_rewrite_checkpoint_resumes_at_eval1_without_replaying_rewrite(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    original_reconcile = first._reconcile_execution_step

    def stop_before_eval1(step_key: str) -> None:
        if step_key == "near_final_acceptance:1":
            raise RuntimeError("stop after near rewrite checkpoint")
        original_reconcile(step_key)

    first._reconcile_execution_step = stop_before_eval1
    with pytest.raises(RuntimeError, match="stop after near rewrite checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-rewrite-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 1
    rewrite_row_id = state.run_checkpoint_json["artifact_refs"]["near_rewrite_draft_row_id"]
    provider_calls = len(generation_client.requests)
    tokens_used = state.scene_tokens_used

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-rewrite-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert state.run_checkpoint_json["artifact_refs"]["near_final_source_draft_row_id"] == rewrite_row_id
    assert near_final.calls == ["near_final_acceptance:0", "near_final_acceptance:1"]
    assert len(generation_client.requests) == provider_calls
    assert state.scene_tokens_used == tokens_used
    assert len(
        session.execute(
            select(LlmCall).where(
                LlmCall.scene_id == "CH_RESUME_SC01",
                LlmCall.execution_step_key == "near_final_rewrite:0",
            )
        ).scalars().all()
    ) == 1


def test_near_eval1_checkpoint_resumes_at_finalization_without_replaying_eval1(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()

    def stop_after_eval1(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("stop after near eval1 checkpoint")

    first._near_final_warning_findings = stop_after_eval1
    with pytest.raises(RuntimeError, match="stop after near eval1 checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval1-resume")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 2
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    resumed = orchestrator()
    resumed.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        resumed.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-eval1-resume")

    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 3
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_near_final_budget_skip_completes_without_rewrite_or_eval_replay(session, monkeypatch) -> None:
    from novel_system.services import scene_budget

    _seed_resume_scene(session)
    monkeypatch.setattr(scene_budget, "can_spend", lambda *args, **kwargs: False)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "rewrite"})
    orchestrator = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        soft_qc_engine=_PassSoftQc(session),
        near_final_service=near_final,
    )
    orchestrator.archiver = _FailArchiveOnce()

    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        orchestrator.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-budget-skip")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint_json["sub_index"] == 3
    assert refs["near_final_rewrite_count"] == 0
    assert refs["near_final_skip_reason"] == "budget_or_candidate_cap"
    assert refs.get("near_rewrite_draft_row_id") is None
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == 2


def test_near_final_human_review_proposal_archives_without_eval_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "human"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-human-proposal")
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:near-human-proposal",
    )

    assert result["scene_status"] == "archived"
    assert result["near_final"]["requires_human_review"] is True
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == provider_calls


def test_strict_near_final_warning_resume_does_not_replay_eval(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(session, {"near_final_acceptance:0": "human"})

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator().run_scene(
        "CH_RESUME_SC01",
        run_policy="strict",
        execution_id="idempotency:near-strict-warning",
    )
    provider_calls = len(generation_client.requests)
    second = orchestrator().run_scene(
        "CH_RESUME_SC01",
        run_policy="strict",
        execution_id="idempotency:near-strict-warning",
    )

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert first["scene_status"] == second["scene_status"] == "quality_warning_pending_acceptance"
    assert state.run_execution_status == "completed"
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 0
    assert state.current_final_scene_row_id is None
    assert near_final.calls == ["near_final_acceptance:0"]
    assert len(generation_client.requests) == provider_calls


def _completed_near_rewrite_checkpoint(session, execution_id: str):  # noqa: ANN201
    generation_client = _CountingGenerationClient()
    near_final = _SequencedNearFinal(
        session,
        {"near_final_acceptance:0": "rewrite", "near_final_acceptance:1": "pass"},
    )

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    return generation_client, near_final, orchestrator


def test_complete_near_prefix_missing_eval0_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-eval-missing",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    eval0_id = state.run_checkpoint_json["artifact_refs"]["near_eval0_evaluation_id"]
    session.delete(session.get(WriterEvaluation, eval0_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-eval-missing",
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_near_prefix_tampered_rewrite_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-rewrite-tamper",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    rewrite_id = state.run_checkpoint_json["artifact_refs"]["near_rewrite_draft_row_id"]
    rewrite = session.get(SceneDraft, rewrite_id)
    rewrite.content += " tampered"
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-rewrite-tamper",
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_complete_near_prefix_missing_candidate_blocks_without_provider_replay(session) -> None:
    _seed_resume_scene(session)
    generation_client, near_final, orchestrator = _completed_near_rewrite_checkpoint(
        session,
        "idempotency:near-prefix-candidate-missing",
    )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    candidate_id = state.run_checkpoint_json["artifact_refs"]["near_eval0_revision_candidate_id"]
    session.delete(session.get(RevisionCandidate, candidate_id))
    session.commit()
    provider_calls = len(generation_client.requests)
    calls = list(near_final.calls)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(
            "CH_RESUME_SC01",
            execution_id="idempotency:near-prefix-candidate-missing",
        )

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == calls
    assert len(generation_client.requests) == provider_calls


def test_near_final_resume_revalidates_complete_soft_prefix(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=_PassSoftQc(session),
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-soft-prefix")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    soft_qc0_id = state.run_checkpoint_json["artifact_refs"]["soft_qc0_report_id"]
    session.delete(session.get(QcReport, soft_qc0_id))
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-soft-prefix")

    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_post_archive_failure_retries_missing_side_effects_before_archived_checkpoint(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    post_archive_attempts = 0

    def _fail_post_archive(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal post_archive_attempts
        post_archive_attempts += 1
        if post_archive_attempts == 1:
            raise RuntimeError("post archive failure")

    first._record_narrative_events = _fail_post_archive
    with pytest.raises(RuntimeError, match="post archive failure"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:archived-replay")

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state is not None
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 4
    assert state.run_execution_status == "failed"
    assert state.scene_status != "archived"
    assert session.scalar(
        select(func.count()).select_from(SceneMemory).where(
            SceneMemory.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(ChapterRollingNote).where(
            ChapterRollingNote.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "archive",
        )
    ) == 1
    provider_calls = len(generation_client.requests)

    resumed = orchestrator()
    resumed._record_narrative_events = _fail_post_archive
    replay = resumed.run_scene(
        "CH_RESUME_SC01",
        execution_id="idempotency:archived-replay",
    )

    assert replay["scene_status"] == "archived"
    session.refresh(state)
    assert state.run_checkpoint == "archived"
    assert state.run_execution_status == "completed"
    assert post_archive_attempts == 2
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(SceneMemory).where(
            SceneMemory.scene_id == "CH_RESUME_SC01"
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(AttemptTracker).where(
            AttemptTracker.scene_id == "CH_RESUME_SC01",
            AttemptTracker.step == "archive",
        )
    ) == 1


def test_archive_rule_events_checkpoint_prevents_replay_after_vector_failure(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    execution_id = "idempotency:archive-rule-events-prefix"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=generation_client,
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after rule event checkpoint")
    )
    with pytest.raises(RuntimeError, match="stop after rule event checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == 5
    event_ids = refs["archive_rule_event_ids"]
    assert event_ids
    assert {
        "causal_predecessor_id",
        "theme_tags",
        "obligation_ids",
        "created_at",
        "payload_json",
    }.issubset(refs["archive_rule_events"][0])
    event_count = session.scalar(
        select(func.count()).select_from(NarrativeEvent).where(
            NarrativeEvent.scene_id == "CH_RESUME_SC01",
            NarrativeEvent.confidence == "high",
        )
    )
    provider_calls = len(generation_client.requests)

    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    assert len(generation_client.requests) == provider_calls
    assert session.scalar(
        select(func.count()).select_from(NarrativeEvent).where(
            NarrativeEvent.scene_id == "CH_RESUME_SC01",
            NarrativeEvent.confidence == "high",
        )
    ) == event_count


def test_archive_prose_checkpoint_is_durable_and_tamper_blocks_before_next_step(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-tamper"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_CountingGenerationClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._index_scene_to_vector_store = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after prose checkpoint")
    )
    with pytest.raises(RuntimeError, match="stop after prose checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 6
    assert state.run_checkpoint_json["artifact_refs"]["archive_prose_product"]["outcome"] == "not_invoked"
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_prose_product"]["reason"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    resumed = orchestrator()
    resumed._index_scene_to_vector_store = lambda *_args, **_kwargs: pytest.fail(
        "tampered prose prefix must block before vector indexing"
    )
    with pytest.raises(DomainError) as corrupt:
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_prose_no_call_with_released_tombstone_never_advances_checkpoint(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-released-gate-closed"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(
                session,
                llm_client=_CountingGenerationClient(),
            ),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop at rule prefix")
    )
    with pytest.raises(RuntimeError, match="stop at rule prefix"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 5

    session.add(
        LlmCall(
            llm_call_id="llm_archive_prose_released",
            provider="fake",
            model="fake",
            node_id="extraction",
            step="archive:prose_event_extract:0",
            project_id="P_RESUME",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            execution_id=execution_id,
            execution_step_key="archive:prose_event_extract:0",
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=0,
            estimated_tokens=0,
            reserved_tokens=0,
            budget_charged_tokens=0,
            accounting_status="released",
            settled_at="2026-07-14T00:00:00Z",
        )
    )
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 5
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:prose_event_extract:0",
        )
    ) == 1


def _add_settled_archive_parent(
    session,
    *,
    call_id: str,
    execution_id: str,
    step_key: str,
    node_id: str,
    scope_type: str,
    scope_id: str,
    scene_id: str | None,
) -> None:
    session.add(
        LlmCall(
            llm_call_id=call_id,
            provider="fake",
            model="fake",
            node_id=node_id,
            step=step_key if node_id == "extraction" else "chapter_near_final_review",
            project_id="P_RESUME",
            scene_id=scene_id,
            chapter_id="CH_RESUME",
            scope_type=scope_type,
            scope_id=scope_id,
            execution_id=execution_id,
            execution_step_key=step_key,
            request_payload_summary={"_accounting_provider_execution_mode": "online"},
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            latency_ms=10,
            estimated_tokens=12,
            reserved_tokens=12,
            budget_charged_tokens=12,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id=f"attempt_{call_id}",
            llm_call_id=call_id,
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=4,
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            latency_ms=10,
            estimated_tokens=12,
            reserved_tokens=12,
            budget_charged_tokens=12,
            usage_is_estimate=False,
            accounting_status="settled",
            request_dispatched_at="2026-07-14T00:00:00Z",
            settled_at="2026-07-14T00:00:01Z",
        )
    )
    session.commit()


def test_archive_prose_settled_parent_without_product_blocks_resend_and_budget_growth(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-prose-parent-only"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._record_prose_events = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop before prose parent")
    )
    with pytest.raises(RuntimeError, match="stop before prose parent"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 5
    call_id = "llm_archive_prose_parent_only"
    _add_settled_archive_parent(
        session,
        call_id=call_id,
        execution_id=execution_id,
        step_key="archive:prose_event_extract:0",
        node_id="extraction",
        scope_type="scene",
        scope_id="CH_RESUME_SC01",
        scene_id="CH_RESUME_SC01",
    )
    tokens_before = state.scene_tokens_used
    counters = (12, 12, 12)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent = session.get(LlmCall, call_id)
    assert (parent.total_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == counters
    assert session.scalar(
        select(func.count()).select_from(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == call_id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:prose_event_extract:0",
        )
    ) == 1
    session.refresh(state)
    assert state.scene_tokens_used == tokens_before


def test_chapter_evaluation_settled_parent_without_row_blocks_resend_and_budget_growth(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:archive-chapter-parent-only"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop before chapter parent")
    )
    with pytest.raises(RuntimeError, match="stop before chapter parent"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 9
    call_id = "llm_archive_chapter_parent_only"
    _add_settled_archive_parent(
        session,
        call_id=call_id,
        execution_id=execution_id,
        step_key="archive:chapter_near_final:0",
        node_id="chapter_near_final_review",
        scope_type="chapter",
        scope_id=scene.chapter_id,
        scene_id=None,
    )
    tokens_before = state.scene_tokens_used

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene(scene.scene_id, execution_id=execution_id)
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    parent = session.get(LlmCall, call_id)
    assert (parent.total_tokens, parent.reserved_tokens, parent.budget_charged_tokens) == (12, 12, 12)
    assert session.scalar(
        select(func.count()).select_from(LlmCallAttempt).where(
            LlmCallAttempt.llm_call_id == call_id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    ) == 0
    session.refresh(state)
    assert state.scene_tokens_used == tokens_before


def test_archive_vector_external_write_is_reused_after_cursor_crash(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-vector-crash"
    generation_client = _CountingGenerationClient()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    original = first._index_scene_to_vector_store

    def _write_then_crash(scene, content, **kwargs):  # noqa: ANN001, ANN003, ANN202
        result = original(scene, content, **kwargs)
        assert result["outcome"] in {"indexed", "already_present", "non_persistent"}
        raise RuntimeError("crash after vector write")

    first._index_scene_to_vector_store = _write_then_crash
    with pytest.raises(RuntimeError, match="crash after vector write"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint_json["sub_index"] == 6
    assert state.scene_status != "archived"

    resumed = orchestrator()
    resumed._run_archive_chapter_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after vector cursor")
    )
    with pytest.raises(RuntimeError, match="stop after vector cursor"):
        resumed.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    session.refresh(state)
    assert state.run_checkpoint_json["sub_index"] == 7
    vector_product = state.run_checkpoint_json["artifact_refs"]["archive_vector_product"]
    assert vector_product["outcome"] == "non_persistent"
    assert vector_product["write_status"] == "already_present"


@pytest.mark.parametrize("external_change", ["cleared", "rebuilt_by_other_scene"])
def test_memory_vector_product_is_non_persistent_and_fast_path_ignores_external_reset(
    session, external_change
) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:memory-vector-{external_change}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    assert orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    product = state.run_checkpoint_json["artifact_refs"]["archive_vector_product"]
    assert product["backend"] == "memory"
    assert product["outcome"] == "non_persistent"
    from novel_system.services.vector_store import get_vector_store

    store = get_vector_store(backend="memory")
    if external_change == "cleared":
        store.delete_collection(product["collection_name"])
    else:
        store.write_collection(
            product["collection_name"],
            [{"id": "another-scene", "text": "rebuilt elsewhere"}],
        )

    assert orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)["scene_status"] == "archived"


def test_non_chapter_last_writes_fixed_archive_products_and_ordered_manifest(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-fixed-non-last"
    result = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    assert refs["archive_chapter_product"]["outcome"] == "not_applicable"
    assert refs["archive_volume_product"]["outcome"] == "not_applicable"
    assert refs["archive_chapter_evaluation_product"]["outcome"] == "not_applicable"
    assert refs["archive_drift_product"]["outcome"] == "not_applicable"
    assert [entry["sub_index"] for entry in refs["archive_manifest"]] == list(range(4, 12))


def test_archived_fast_path_revalidates_full_manifest_before_return(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-manifest-tamper"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"]["archive_manifest"][0]["product_hash"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_core_checkpoint_contains_full_independently_hashed_snapshots(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:archive-core-snapshots"
    Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    hashes = state.run_checkpoint_json["artifact_hashes"]
    assert {
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
    }.issubset(refs)
    assert {
        "archive_final_scene_snapshot",
        "archive_scene_memory_snapshot",
        "archive_rolling_note_snapshot",
        "archive_attempt_snapshot",
    }.issubset(hashes)
    assert refs["archive_scene_memory_snapshot"]["runtime_eligibility_basis"] == "direct_read"
    assert refs["archive_rolling_note_snapshot"]["revision_no"] == 1
    assert "qc_report_id" in refs["archive_attempt_snapshot"]["details_json"]


@pytest.mark.parametrize(
    "mutation",
    [
        "final_source_bundle_hash",
        "memory_runtime_basis",
        "rolling_revision",
        "attempt_qc_tamper",
        "attempt_qc_delete",
    ],
)
def test_archive_core_snapshot_field_tamper_blocks_archived_fast_path(session, mutation) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-core-field-{mutation}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    refs = state.run_checkpoint_json["artifact_refs"]
    if mutation == "final_source_bundle_hash":
        session.get(FinalScene, refs["final_scene_row_id"]).source_bundle_hash = "tampered"
    elif mutation == "memory_runtime_basis":
        session.get(SceneMemory, refs["scene_memory_row_id"]).runtime_eligibility_basis = "tampered"
    elif mutation == "rolling_revision":
        rolling = session.get(ChapterRollingNote, refs["archive_core"]["chapter_rolling_note_row_id"])
        rolling.revision_no += 1
    else:
        attempt = session.get(AttemptTracker, refs["archive_core"]["archive_attempt_id"])
        details = dict(attempt.details_json or {})
        if mutation == "attempt_qc_delete":
            details.pop("qc_report_id", None)
        else:
            details["qc_report_id"] = "tampered"
        attempt.details_json = details
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archived_worker_fast_path_restores_real_run_job_owner_and_rejects_wrong_job(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    state = session.get(SceneRunState, scene.scene_id)
    job_id = "scene-job-archive-fast-path"
    session.add(
        ChapterRunJob(
            job_id=job_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            status="running",
            job_type="scene_run_full",
            payload_json={"scene_id": scene.scene_id},
        )
    )
    state.active_run_job_id = job_id
    session.commit()
    execution_id = "scene-job-archive-execution"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    assert orchestrator().run_scene(
        scene.scene_id,
        execution_id=execution_id,
        run_job_id=job_id,
    )["scene_status"] == "archived"
    assert orchestrator().run_scene(
        scene.scene_id,
        execution_id=execution_id,
        run_job_id=job_id,
    )["scene_status"] == "archived"

    wrong_job_id = "scene-job-archive-wrong"
    session.add(
        ChapterRunJob(
            job_id=wrong_job_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            status="running",
            job_type="scene_run_full",
            payload_json={"scene_id": scene.scene_id},
        )
    )
    session.commit()
    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene(
            scene.scene_id,
            execution_id=execution_id,
            run_job_id=wrong_job_id,
        )
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_chapter_last_archive_uses_real_chapter_scope_and_aggregate_inputs(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    scene.project_id = None
    session.commit()
    execution_id = "idempotency:archive-chapter-last"

    result = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    assert result["scene_status"] == "archived"
    state = session.get(SceneRunState, scene.scene_id)
    refs = state.run_checkpoint_json["artifact_refs"]
    chapter_product = refs["archive_chapter_product"]
    assert chapter_product["outcome"] == "aggregated"
    assert chapter_product["inputs"] == sorted(
        chapter_product["inputs"], key=lambda item: item["row_id"]
    )
    evaluation_product = refs["archive_chapter_evaluation_product"]
    assert evaluation_product["outcome"] == "evaluated"
    parent = session.get(LlmCall, evaluation_product["evaluator_llm_call_id"])
    assert parent.scope_type == "chapter"
    assert parent.scope_id == scene.chapter_id
    assert parent.chapter_id == scene.chapter_id
    assert parent.scene_id is None
    assert parent.execution_step_key == "archive:chapter_near_final:0"

    Aggregator(session).run_final_aggregate(scene.chapter_id)
    session.commit()
    replay = Orchestrator(
        session,
        scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
        hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
    ).run_scene(scene.scene_id, execution_id=execution_id)
    assert replay["scene_status"] == "archived"


@pytest.mark.parametrize("sub_index", [8, 9, 10, 11])
def test_archive_subcursor_8_to_11_resumes_without_replaying_prefix(session, sub_index) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-subcursor-{sub_index}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    if sub_index == 8:
        first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after sub8")
        )
    elif sub_index == 9:
        first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after sub9")
        )
    elif sub_index == 10:
        original_product = first._archive_product

        def _stop_before_sub11(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if kwargs.get("kind") == "style_drift":
                raise RuntimeError("stop after sub10")
            return original_product(*args, **kwargs)

        first._archive_product = _stop_before_sub11
    else:
        first._archive_manifest = lambda: (_ for _ in ()).throw(RuntimeError("stop after sub11"))

    with pytest.raises(RuntimeError, match=f"stop after sub{sub_index}"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "near_final_ready"
    assert state.run_checkpoint_json["sub_index"] == sub_index
    assert state.scene_status != "archived"
    prefix_hashes = deepcopy(state.run_checkpoint_json["artifact_hashes"])

    result = orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert result["scene_status"] == "archived"
    session.refresh(state)
    for key, value in prefix_hashes.items():
        assert state.run_checkpoint_json["artifact_hashes"][key] == value


@pytest.mark.parametrize(
    ("sub_index", "product_key"),
    [
        (8, "archive_chapter_product"),
        (9, "archive_volume_product"),
        (10, "archive_chapter_evaluation_product"),
        (11, "archive_drift_product"),
    ],
)
def test_archive_subcursor_8_to_11_tamper_blocks_resume(session, sub_index, product_key) -> None:
    _seed_resume_scene(session)
    execution_id = f"idempotency:archive-subcursor-tamper-{sub_index}"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    if sub_index == 8:
        first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    elif sub_index == 9:
        first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop"))
    elif sub_index == 10:
        original_product = first._archive_product
        first._archive_product = lambda *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("stop"))
            if kwargs.get("kind") == "style_drift"
            else original_product(*args, **kwargs)
        )
    else:
        first._archive_manifest = lambda: (_ for _ in ()).throw(RuntimeError("stop"))
    with pytest.raises(RuntimeError, match="stop"):
        first.run_scene("CH_RESUME_SC01", execution_id=execution_id)
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = deepcopy(state.run_checkpoint_json)
    payload["artifact_refs"][product_key]["outcome"] = "tampered"
    state.run_checkpoint_json = payload
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_chapter_last_sub8_crash_does_not_create_second_chapter_memory(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:chapter-last-sub8-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_volume_aggregate = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after real sub8")
    )
    with pytest.raises(RuntimeError, match="stop after real sub8"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 8
    row_id = state.run_checkpoint_json["artifact_refs"]["archive_chapter_product"]["chapter_memory"]["row_id"]
    assert session.scalar(
        select(func.count()).select_from(ChapterMemory).where(
            ChapterMemory.chapter_id == scene.chapter_id,
            ChapterMemory.aggregate_stage == "final",
        )
    ) == 1

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(
        select(func.count()).select_from(ChapterMemory).where(
            ChapterMemory.chapter_id == scene.chapter_id,
            ChapterMemory.aggregate_stage == "final",
        )
    ) == 1
    assert session.get(ChapterMemory, row_id) is not None


def test_chapter_last_sub9_volume_boundary_crash_reuses_same_summary(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    chapter = session.get(ChapterGoal, scene.chapter_id)
    chapter.display_order = 5
    for ordinal in range(1, 5):
        chapter_id = f"CH_RESUME_{ordinal}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id="P_RESUME",
                display_order=ordinal,
                chapter_goal=f"prior {ordinal}",
            )
        )
        session.add(
            ChapterMemory(
                row_id=f"chapter_memory_final_{chapter_id}_v1",
                chapter_id=chapter_id,
                aggregate_stage="final",
                content=f"prior atmosphere {ordinal}",
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            )
        )
    session.commit()
    execution_id = "idempotency:chapter-last-sub9-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    first._run_archive_chapter_evaluation = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stop after real sub9")
    )
    with pytest.raises(RuntimeError, match="stop after real sub9"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 9
    volume_product = state.run_checkpoint_json["artifact_refs"]["archive_volume_product"]
    assert volume_product["outcome"] == "aggregated"
    row_id = volume_product["volume_summary"]["row_id"]
    assert session.scalar(select(func.count()).select_from(VolumeSummary)) == 1

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(select(func.count()).select_from(VolumeSummary)) == 1
    assert session.get(VolumeSummary, row_id).active_flag == 1
    window = [f"CH_RESUME_{ordinal}" for ordinal in range(1, 5)] + [scene.chapter_id]
    Aggregator(session).aggregate_volume_summary("P_RESUME", 1, window)
    session.commit()
    assert session.get(VolumeSummary, row_id).active_flag == 0
    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"


def test_chapter_last_sub10_crash_reuses_evaluation_parent_and_budget(session) -> None:
    _seed_resume_scene(session)
    scene = session.get(SceneCard, "CH_RESUME_SC01")
    scene.is_chapter_last = 1
    session.commit()
    execution_id = "idempotency:chapter-last-sub10-crash"

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=_CountingGenerationClient()),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
        )

    first = orchestrator()
    original_product = first._archive_product
    first._archive_product = lambda *args, **kwargs: (
        (_ for _ in ()).throw(RuntimeError("stop after real sub10"))
        if kwargs.get("kind") == "style_drift"
        else original_product(*args, **kwargs)
    )
    with pytest.raises(RuntimeError, match="stop after real sub10"):
        first.run_scene(scene.scene_id, execution_id=execution_id)
    state = session.get(SceneRunState, scene.scene_id)
    assert state.run_checkpoint_json["sub_index"] == 10
    product = state.run_checkpoint_json["artifact_refs"]["archive_chapter_evaluation_product"]
    parent = session.get(LlmCall, product["evaluator_llm_call_id"])
    counters = (
        parent.estimated_tokens,
        parent.reserved_tokens,
        parent.budget_charged_tokens,
        parent.total_tokens,
    )
    call_count = session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    )
    evaluation_count = session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    )

    assert orchestrator().run_scene(scene.scene_id, execution_id=execution_id)["scene_status"] == "archived"
    assert session.scalar(
        select(func.count()).select_from(LlmCall).where(
            LlmCall.execution_id == execution_id,
            LlmCall.execution_step_key == "archive:chapter_near_final:0",
        )
    ) == call_count
    assert session.scalar(
        select(func.count()).select_from(WriterEvaluation).where(
            WriterEvaluation.object_type == "chapter",
            WriterEvaluation.object_id == scene.chapter_id,
        )
    ) == evaluation_count
    session.refresh(parent)
    assert (
        parent.estimated_tokens,
        parent.reserved_tokens,
        parent.budget_charged_tokens,
        parent.total_tokens,
    ) == counters


def test_missing_soft_qc_checkpoint_row_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-missing")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc_report_id"]
    session.delete(session.get(QcReport, report_id))
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as missing:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-missing")
    assert missing.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_soft_qc_report_content_hash_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-report-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    report_id = state.run_checkpoint_json["artifact_refs"]["soft_qc_report_id"]
    report = session.get(QcReport, report_id)
    report.rewrite_brief_json = [{"instruction": "tampered carry note"}]
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-report-corrupt")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_near_final_checkpoint_source_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    final_row_id = state.run_checkpoint_json["artifact_refs"]["final_scene_row_id"]
    final_scene = session.get(FinalScene, final_row_id)
    final_scene.source_bundle_hash = "sha256:tampered"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_near_final_carry_notes_hash_blocks_without_repeating_provider(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _PassNearFinal(session)

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    first = orchestrator()
    first.archiver = _FailArchiveOnce()
    with pytest.raises(RuntimeError, match="fail after near-final checkpoint"):
        first.run_scene("CH_RESUME_SC01", execution_id="idempotency:near-carry-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    payload = dict(state.run_checkpoint_json)
    refs = dict(payload["artifact_refs"])
    refs["carry_notes"] = [*(refs.get("carry_notes") or []), {"kind": "tampered"}]
    payload["artifact_refs"] = refs
    state.run_checkpoint_json = payload
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:near-carry-corrupt")

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert near_final.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_corrupt_hard_qc_source_binding_blocks_checkpoint_resume(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    late_failure = _FailAfterStyle()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=late_failure,
        )

    with pytest.raises(RuntimeError, match="fail after style checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    hard_report_id = state.run_checkpoint_json["artifact_refs"]["qc_report_id"]
    report = session.get(QcReport, hard_report_id)
    report.source_draft_row_id = "draft_from_another_execution"
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:hard-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert len(generation_client.requests) == provider_calls


def test_corrupt_soft_qc_decision_hash_blocks_checkpoint_resume(session) -> None:
    _seed_resume_scene(session)
    generation_client = _CountingGenerationClient()
    soft_qc = _PassSoftQc(session)
    near_final = _FailNearFinal()

    def orchestrator() -> Orchestrator:
        return Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=HardQcEngine(session, llm_client=_HardPassClient()),
            soft_qc_engine=soft_qc,
            near_final_service=near_final,
        )

    with pytest.raises(RuntimeError, match="fail after soft checkpoint"):
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-corrupt")
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    checkpoint = dict(state.run_checkpoint_json)
    refs = dict(checkpoint["artifact_refs"])
    refs["soft_qc_branch"] = "waive"
    checkpoint["artifact_refs"] = refs
    state.run_checkpoint_json = checkpoint
    session.commit()
    provider_calls = len(generation_client.requests)

    with pytest.raises(DomainError) as corrupt:
        orchestrator().run_scene("CH_RESUME_SC01", execution_id="idempotency:soft-corrupt")
    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert soft_qc.calls == 1
    assert len(generation_client.requests) == provider_calls


def test_provider_owner_lease_tracks_each_request_timeout_and_restores_default(session, monkeypatch) -> None:
    _seed_resume_scene(session)
    events: list[tuple[str, int]] = []

    class _TimeoutClient(_AccountedTestClient):
        def generate(self, request: LLMRequest) -> LLMResponse:
            events.append(("provider", int(request.timeout_seconds or 0)))
            return _response({"scene_text": "timeout lease"}, f"timeout-{request.timeout_seconds}")

    def _budget(**kwargs):  # noqa: ANN003, ANN202
        return {"budget": kwargs["base_budget"], "continuity_warning": {}}

    monkeypatch.setattr("novel_system.services.llm_task_runner.finalize_request_budget", _budget)

    def _task(timeout_seconds: int) -> SimpleNamespace:
        return SimpleNamespace(
            model="fake-model",
            temperature=0.2,
            max_output_tokens=128,
            response_format="json_object",
            provider="fake-provider",
            timeout_seconds=timeout_seconds,
            provider_id="provider-1",
            account_id="account-1",
            reasoning_level="medium",
            api_mode="responses",
            credential_mode=None,
            provider_options={},
        )

    routes = SimpleNamespace(
        node_routing={"short": _task(10), "long": _task(70)},
        task_routing={},
    )
    runner = LLMNodeRunner(session, llm_client=_TimeoutClient(), routing_config=routes)

    def _renew(*, lease_seconds: int) -> None:
        events.append(("lease", lease_seconds))

    token = begin_llm_execution("exec-timeout", lease_renewer=_renew)
    try:
        for node_id in ("short", "long"):
            runner.run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-timeout",
                bundle_hash="sha256:timeout",
                node_id=node_id,
                step=node_id,
                prompt={
                    "system_prompt": "system",
                    "token_budget": {},
                    "template_name": node_id,
                    "template_version": "v1",
                },
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)

    grace = owner_lease_grace_seconds()
    default_ttl = owner_lease_ttl_seconds()
    assert events == [
        ("lease", max(default_ttl, 10 + grace)),
        ("provider", 10),
        ("lease", default_ttl),
        ("lease", max(default_ttl, 70 + grace)),
        ("provider", 70),
        ("lease", default_ttl),
    ]


def test_owner_lease_envelope_never_shrinks_default_and_covers_all_llm_retries(monkeypatch) -> None:
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_ttl_seconds", lambda: 30)
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_grace_seconds", lambda: 5)

    assert _execution_owner_lease_seconds(
        request_timeout_seconds=10,
        client=object(),
    ) == 30

    client = LLMClient(
        provider="openai_compatible",
        base_url="https://provider.invalid/v1",
        api_key="test",
        timeout_seconds=70,
        max_retries=2,
        retry_backoff_seconds=1.0,
    )
    physical_attempts = (2 + 1) * (3 + 1)
    backoff_envelope = int((3 + 1) * 2 * 30 * 1.2)
    assert _execution_owner_lease_seconds(
        request_timeout_seconds=70,
        client=client,
    ) >= physical_attempts * 70 + backoff_envelope + 5


def test_owner_lease_envelope_survives_an_unbounded_request_timeout(monkeypatch) -> None:
    """不限时(0)不能退回默认 TTL:长任务会在调用中途丢租约并被二次执行。"""

    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_ttl_seconds", lambda: 30)
    monkeypatch.setattr("novel_system.services.idempotency.owner_lease_grace_seconds", lambda: 5)

    unbounded = _execution_owner_lease_seconds(request_timeout_seconds=0, client=object())
    assert unbounded == UNBOUNDED_TIMEOUT_LEASE_SECONDS
    assert unbounded > 30


def test_provider_heartbeat_periodically_renews_with_a_detached_callback() -> None:
    class _Lease:
        def __init__(self) -> None:
            self.detached_renewals: list[int] = []

        def renew(self, *, lease_seconds: int) -> None:
            del lease_seconds

        def renew_detached(self, *, lease_seconds: int) -> None:
            self.detached_renewals.append(lease_seconds)

    lease = _Lease()
    token = begin_llm_execution("exec-heartbeat", lease_renewer=lease.renew)
    try:
        with _execution_owner_heartbeat(lease_seconds=3_600, interval_seconds=0.01):
            time.sleep(0.045)
    finally:
        end_llm_execution(token)

    assert len(lease.detached_renewals) >= 2
    assert set(lease.detached_renewals) == {3_600}


def test_dispatch_truth_allows_predispatch_retry_but_blocks_unknown_provider_outcome(session, monkeypatch) -> None:
    _seed_resume_scene(session)

    def _budget(**kwargs):  # noqa: ANN003, ANN202
        return {"budget": kwargs["base_budget"], "continuity_warning": {}}

    monkeypatch.setattr("novel_system.services.llm_task_runner.finalize_request_budget", _budget)
    task = SimpleNamespace(
        model="fake-model",
        temperature=0.2,
        max_output_tokens=128,
        response_format="json_object",
        provider="fake-provider",
        timeout_seconds=10,
        provider_id="provider-1",
        account_id="account-1",
        reasoning_level="medium",
        api_mode="responses",
        credential_mode=None,
        provider_options={},
    )
    routes = SimpleNamespace(node_routing={"dispatch-test": task}, task_routing={})
    prompt = {
        "system_prompt": "system",
        "token_budget": {},
        "template_name": "dispatch-test",
        "template_version": "v1",
    }

    class _MustNotDispatch(_AccountedTestClient):
        def generate(self, request):  # noqa: ANN001, ANN201
            raise AssertionError("provider must not be called")

    def _lost_before_dispatch(*, lease_seconds: int) -> None:
        raise DomainError("RUN_OWNER_LEASE_LOST", "lost before provider", status_code=409)

    token = begin_llm_execution("exec-predispatch", lease_renewer=_lost_before_dispatch)
    try:
        with pytest.raises(LLMNodeExecutionError):
            LLMNodeRunner(session, llm_client=_MustNotDispatch(), routing_config=routes).run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-dispatch",
                bundle_hash="sha256:dispatch",
                node_id="dispatch-test",
                step="dispatch-test",
                prompt=prompt,
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)
    pre_calls = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "exec-predispatch")
    ).scalars().all()
    assert pre_calls == []
    assert SceneRunCheckpointService(session).reconcile_step_output(
        scene_id="CH_RESUME_SC01",
        execution_id="exec-predispatch",
        execution_step_key="dispatch-test",
        output_exists=False,
    ) == "retry"

    class _UnknownProviderOutcome(_AccountedTestClient):
        def generate(self, request):  # noqa: ANN001, ANN201
            raise TimeoutError("provider outcome unknown")

    token = begin_llm_execution("exec-dispatched")
    try:
        with pytest.raises(LLMNodeExecutionError):
            LLMNodeRunner(session, llm_client=_UnknownProviderOutcome(), routing_config=routes).run(
                scene_id="CH_RESUME_SC01",
                chapter_id="CH_RESUME",
                bundle_id="bundle-dispatch",
                bundle_hash="sha256:dispatch",
                node_id="dispatch-test",
                step="dispatch-test",
                prompt=prompt,
                user_prompt="user",
            )
    finally:
        end_llm_execution(token)
    dispatched_call = session.execute(
        select(LlmCall).where(LlmCall.execution_id == "exec-dispatched")
    ).scalar_one()
    assert dispatched_call.request_dispatched_at is not None
    with pytest.raises(DomainError) as unknown:
        SceneRunCheckpointService(session).reconcile_step_output(
            scene_id="CH_RESUME_SC01",
            execution_id="exec-dispatched",
            execution_step_key="dispatch-test",
            output_exists=False,
        )
    assert unknown.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"


def test_real_pipeline_blocks_settled_ledger_before_repeating_neutral_provider(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:settled-narrow-window"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "bundle_ready"
    session.add(
        LlmCall(
            llm_call_id="settled-neutral-window",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            provider="fake",
            model="fake",
            node_id="neutral_draft",
            step="neutral_draft",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            execution_id=execution_id,
            execution_step_key="neutral_draft",
            accounting_status="settled",
            request_dispatched_at="2026-07-13T00:00:00+00:00",
            settled_at="2026-07-13T00:00:01+00:00",
        )
    )
    session.commit()
    generation_client = _CountingGenerationClient()

    with pytest.raises(DomainError) as blocked:
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)
    assert blocked.value.code == "RUN_CHECKPOINT_OUTPUT_MISSING"
    assert generation_client.requests == []


def test_checkpoint_rejects_tampered_frozen_bundle_snapshot(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:tampered-frozen-bundle"
    author_note = "冻结并保留这条作者指令。"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
            author_note=author_note,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    assert state.run_checkpoint == "bundle_ready"
    bundle = session.get(SceneBundle, state.current_bundle_id)
    snapshot = dict(bundle.frozen_snapshot_json)
    inline = dict(snapshot["inline_digests"])
    inline["author_instruction"] = "持久层篡改后的作者指令。"
    snapshot["inline_digests"] = inline
    bundle.frozen_snapshot_json = snapshot
    session.commit()

    with pytest.raises(DomainError) as corrupt:
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
            author_note=author_note,
        )

    assert corrupt.value.code == "RUN_CHECKPOINT_CORRUPT"
    assert corrupt.value.details["bundle_integrity"]["error_code"] == "bundle_hash_mismatch"


def test_real_pipeline_releases_undispatched_reservation_then_calls_neutral_once(session) -> None:
    _seed_resume_scene(session)
    execution_id = "idempotency:reserved-narrow-window"
    with pytest.raises(RuntimeError, match="stop at bundle checkpoint"):
        Orchestrator(session, scene_generation_service=_FailBeforeNeutral()).run_scene(
            "CH_RESUME_SC01",
            execution_id=execution_id,
        )
    state = session.get(SceneRunState, "CH_RESUME_SC01")
    state.scene_tokens_reserved = 20
    session.add(
        LlmCall(
            llm_call_id="reserved-neutral-window",
            scope_type="scene",
            scope_id="CH_RESUME_SC01",
            provider="fake",
            model="fake",
            node_id="neutral_draft",
            step="neutral_draft",
            scene_id="CH_RESUME_SC01",
            chapter_id="CH_RESUME",
            execution_id=execution_id,
            execution_step_key="neutral_draft",
            estimated_tokens=20,
            reserved_tokens=20,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.add(
        LlmCallAttempt(
            attempt_id="attempt-reserved-neutral-window",
            llm_call_id="reserved-neutral-window",
            provider_attempt_no=0,
            dispatch_kind="initial",
            request_max_output_tokens=10,
            estimated_tokens=20,
            reserved_tokens=20,
            accounting_status="reserved",
            request_dispatched_at=None,
        )
    )
    session.commit()
    generation_client = _CountingGenerationClient()

    with pytest.raises(RuntimeError, match="stop after neutral retry"):
        Orchestrator(
            session,
            scene_generation_service=SceneGenerationService(session, llm_client=generation_client),
            hard_qc_engine=_FailBeforeHardQc(),
        ).run_scene("CH_RESUME_SC01", execution_id=execution_id)

    session.refresh(state)
    reserved_call = session.get(LlmCall, "reserved-neutral-window")
    assert reserved_call.accounting_status == "released"
    assert state.scene_tokens_reserved == 0
    assert state.run_checkpoint == "neutral_ready"
    assert len(generation_client.requests) == 1
