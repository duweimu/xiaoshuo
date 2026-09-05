"""Wave 6（结果闭环治理 §5.8/§10）：token/费用聚合——场景/章节/项目三级。

完成门：任意场景可解释总成本、各阶段占比、是否超预算、评审是否独立。跨 provider
token 不相加（分词器不同）、汇总以费用为准；三口径（估算/实际/计费）；额外成本
（失败重试/重复 QC/低分散补候选）可归因。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services import cost_aggregation as ca


def _scene(session, scene_id, chapter_id="CH1", project_id="proj1", seq=None):
    if project_id and session.get(StoryProject, project_id) is None:
        session.add(
            StoryProject(
                project_id=project_id,
                title=project_id,
                outline_text="test outline",
            )
        )
        session.flush()
    if session.get(ChapterGoal, chapter_id) is None:
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                chapter_goal=f"goal {chapter_id}",
            )
        )
        session.flush()
    if seq is None:
        seq = int(
            session.scalar(
                select(func.coalesce(func.max(SceneCard.scene_seq), 0)).where(
                    SceneCard.chapter_id == chapter_id,
                    SceneCard.trashed_flag == 0,
                )
            )
            or 0
        ) + 1
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=seq,
            scene_goal="g",
        )
    )
    session.flush()


def _runstate(session, scene_id, *, budget=None, used=0, criticality=None, policy=None):
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_token_budget=budget,
            scene_tokens_used=used,
            criticality_level=criticality,
            run_policy=policy,
        )
    )
    session.flush()


def _call(
    session,
    scene_id,
    *,
    node_id,
    tokens=150,
    provider="openai_compatible",
    model="gpt-5",
    chapter_id="CH1",
    project_id="proj1",
    error_code=None,
    created_at=None,
):
    idx = _call.counter = getattr(_call, "counter", 0) + 1
    session.add(
        LlmCall(
            llm_call_id=f"llm_{idx:04d}",
            scope_type="scene" if scene_id else "project" if project_id else "system",
            scope_id=scene_id or project_id or node_id,
            provider=provider,
            model=model,
            node_id=node_id,
            step=node_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            prompt_tokens=int(tokens * 2 // 3),
            completion_tokens=int(tokens // 3),
            total_tokens=tokens,
            error_code=error_code,
            created_at=created_at or f"2026-07-12T00:00:{idx:02d}Z",
        )
    )
    session.flush()


def _accounted_retry_call(session, scene_id: str) -> str:
    """Insert one logical parent with two physical attempts and exact aggregates."""
    call_id = f"accounted_retry_{scene_id}"
    session.add(
        LlmCall(
            llm_call_id=call_id,
            scope_type="scene",
            scope_id=scene_id,
            provider="openai_compatible",
            model="gpt-5",
            node_id="style_draft",
            step="style_draft",
            scene_id=scene_id,
            chapter_id="CH1",
            project_id="proj1",
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
            estimated_tokens=120,
            reserved_tokens=150,
            budget_charged_tokens=100,
            usage_is_estimate=True,
            accounting_status="settled",
            request_dispatched_at="2026-07-12T00:00:01Z",
        )
    )
    session.add_all(
        [
            LlmCallAttempt(
                attempt_id=f"{call_id}:0",
                llm_call_id=call_id,
                provider_attempt_no=0,
                dispatch_kind="initial",
                prompt_tokens=30,
                completion_tokens=10,
                total_tokens=40,
                estimated_tokens=40,
                reserved_tokens=50,
                budget_charged_tokens=40,
                usage_is_estimate=True,
                accounting_status="failed",
                request_dispatched_at="2026-07-12T00:00:01Z",
                error_code="LLM_TIMEOUT",
            ),
            LlmCallAttempt(
                attempt_id=f"{call_id}:1",
                llm_call_id=call_id,
                provider_attempt_no=1,
                dispatch_kind="missing_text_degrade",
                prompt_tokens=50,
                completion_tokens=10,
                total_tokens=60,
                estimated_tokens=80,
                reserved_tokens=100,
                budget_charged_tokens=60,
                usage_is_estimate=False,
                accounting_status="settled",
                request_dispatched_at="2026-07-12T00:00:02Z",
            ),
        ]
    )
    session.flush()
    return call_id


def _archived(session, scene_id, chapter_id="CH1"):
    session.add(
        FinalScene(
            row_id=f"fs_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            content="正文",
            status="archived",
            source_bundle_id="b",
            source_bundle_hash="h",
        )
    )
    session.flush()


# ---- classify_phase ----------------------------------------------------------

def test_classify_phase_maps_known_nodes():
    assert ca.classify_phase("style_draft", "style_draft") == "candidate_generation"
    assert ca.classify_phase("neutral_draft", "neutral_draft") == "candidate_generation"
    assert ca.classify_phase("hard_qc", "hard_qc") == "quality_check"
    assert ca.classify_phase("near_final_acceptance_review", "x") == "quality_check"
    assert ca.classify_phase("style_patch", "style_patch") == "revision"
    assert ca.classify_phase("scene_auto_rewrite", "x") == "revision"
    assert ca.classify_phase("writer_deep_review", "x") == "review"
    assert ca.classify_phase("style_profile_extract", "x") == "other"
    # 构思/案头生成类也是候选生成——真实项目的成本大头不落「其他」
    assert ca.classify_phase("snowflake_step_generate", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_step_candidates", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_workspace_assistant", "x") == "candidate_generation"
    assert ca.classify_phase("author_proposal_generate", "x") == "candidate_generation"
    assert ca.classify_phase("project_outline_plan", "x") == "candidate_generation"
    assert ca.classify_phase("scene_generation", "x") == "candidate_generation"
    assert ca.classify_phase("snowflake_scene_triage_suggest", "x") == "review"
    # 风格参考验证/评审仍先被 QC/review 关键词抓走，不受生成词干扰
    assert ca.classify_phase("style_ref_validate_semantic", "x") == "quality_check"
    assert ca.classify_phase("chapter_near_final_review", "x") == "quality_check"


# ---- scene_cost --------------------------------------------------------------

def test_scene_cost_phase_shares_sum_to_one(session):
    _scene(session, "S1")
    _runstate(session, "S1")
    _call(session, "S1", node_id="style_draft", tokens=300)
    _call(session, "S1", node_id="hard_qc", tokens=100)
    _call(session, "S1", node_id="writer_deep_review", tokens=100)
    result = ca.scene_cost(session, "S1")
    shares = sum(p["share"] for p in result["phase_breakdown"].values())
    assert abs(shares - 1.0) < 1e-6
    assert result["phase_breakdown"]["candidate_generation"]["call_count"] == 1
    assert result["total_cost"] > 0
    assert result["call_count"] == 3


def test_scene_cost_cross_provider_tokens_not_summed(session):
    _scene(session, "S2")
    _runstate(session, "S2")
    _call(session, "S2", node_id="style_draft", provider="openai_compatible", model="gpt-5", tokens=200)
    _call(session, "S2", node_id="hard_qc", provider="anthropic", model="claude", tokens=100)
    result = ca.scene_cost(session, "S2")
    assert result["cross_provider"] is True
    assert set(result["tokens_by_provider"]) == {"openai_compatible", "anthropic"}
    assert result["tokens_by_provider"]["openai_compatible"] == 200
    assert result["tokens_by_provider"]["anthropic"] == 100


def test_scene_cost_budget_over_and_under(session):
    _scene(session, "S3")
    _runstate(session, "S3", budget=1000, used=1200, policy="strict")
    _call(session, "S3", node_id="style_draft", tokens=150)
    over = ca.scene_cost(session, "S3")
    assert over["budget"]["over_budget"] is True
    assert over["budget"]["usage_ratio"] > 1.0
    assert over["budget"]["run_policy"] == "strict"

    _scene(session, "S3b")
    _runstate(session, "S3b", budget=1000, used=200)
    _call(session, "S3b", node_id="style_draft", tokens=150)
    under = ca.scene_cost(session, "S3b")
    assert under["budget"]["over_budget"] is False


def test_scene_cost_three_calibers(session):
    _scene(session, "S4")
    _runstate(session, "S4", budget=1000, used=150)
    _call(session, "S4", node_id="style_draft", tokens=150)
    result = ca.scene_cost(session, "S4")
    cal = result["calibers"]
    assert set(cal) == {"estimate", "provider_actual", "budget_charged"}
    assert cal["estimate"]["tokens"] == 150
    assert cal["provider_actual"]["tokens"] == 0
    assert cal["budget_charged"]["tokens"] == 0


def test_scene_cost_uses_parent_once_and_attempts_only_for_calibers_and_observability(session):
    _scene(session, "S4-accounted")
    _runstate(session, "S4-accounted", budget=1_000, used=100)
    _accounted_retry_call(session, "S4-accounted")

    result = ca.scene_cost(session, "S4-accounted")

    assert result["call_count"] == 1
    assert result["is_estimate"] is True
    assert result["total_tokens"] == 100
    assert result["phase_breakdown"]["candidate_generation"]["tokens"] == 100
    assert result["calibers"] == {
        "estimate": {"tokens": 120, "source": "llm_calls.estimated_tokens"},
        "provider_actual": {
            "tokens": 60,
            "source": "llm_call_attempts.total_tokens_with_provider_usage",
        },
        "budget_charged": {
            "tokens": 100,
            "source": "llm_calls.budget_charged_tokens",
        },
    }
    assert result["attempt_observability"] == {
        "attempt_row_count": 2,
        "physical_attempt_count": 2,
        "pre_dispatch_attempt_count": 0,
        "usage_estimate_count": 1,
        "exception_count": 1,
        "retry_attempt_count": 1,
        "transport_retry_attempt_count": 0,
        "response_parse_retry_attempt_count": 0,
        "degrade_attempt_count": 1,
        "legacy_parent_without_attempt_count": 0,
        "legacy_unreconstructable_tokens": 0,
    }
    assert result["extra_cost"]["failed_call_cost"] > 0


@pytest.mark.parametrize(
    ("dispatch_kind", "transport_count", "parse_count", "degrade_count"),
    [
        ("transport_retry", 1, 0, 0),
        ("response_parse_retry", 0, 1, 0),
        ("api_mode_degrade", 0, 0, 1),
        ("structured_output_degrade", 0, 0, 1),
    ],
)
def test_retry_and_degrade_subtypes_are_durably_distinguishable(
    session,
    dispatch_kind: str,
    transport_count: int,
    parse_count: int,
    degrade_count: int,
):
    scene_id = f"S4-{dispatch_kind}"
    _scene(session, scene_id)
    _runstate(session, scene_id, budget=1_000, used=100)
    call_id = _accounted_retry_call(session, scene_id)
    retry = session.query(LlmCallAttempt).filter_by(
        llm_call_id=call_id,
        provider_attempt_no=1,
    ).one()
    retry.dispatch_kind = dispatch_kind
    session.flush()

    observed = ca.scene_cost(session, scene_id)["attempt_observability"]

    assert observed["retry_attempt_count"] == 1
    assert observed["transport_retry_attempt_count"] == transport_count
    assert observed["response_parse_retry_attempt_count"] == parse_count
    assert observed["degrade_attempt_count"] == degrade_count


def test_undispatched_attempt_row_is_not_reported_as_a_physical_provider_attempt(session):
    _scene(session, "S4-undispatched")
    _runstate(session, "S4-undispatched", budget=1_000, used=100)
    call_id = _accounted_retry_call(session, "S4-undispatched")
    rejected = session.query(LlmCallAttempt).filter_by(
        llm_call_id=call_id,
        provider_attempt_no=0,
    ).one()
    rejected.request_dispatched_at = None
    session.flush()

    result = ca.scene_cost(session, "S4-undispatched")

    assert result["attempt_observability"]["attempt_row_count"] == 2
    assert result["attempt_observability"]["physical_attempt_count"] == 1
    assert result["attempt_observability"]["pre_dispatch_attempt_count"] == 1
    assert result["calibers"]["provider_actual"]["tokens"] == 60


def test_scene_cost_extra_cost_attribution(session):
    _scene(session, "S5")
    _runstate(session, "S5", criticality="standard")  # 标准场景初始 N=2
    # 3 个候选 → 超出初始 2 → 1 个补候选归 low_dispersion_topup
    _call(session, "S5", node_id="style_draft", tokens=100, created_at="2026-07-12T00:00:01Z")
    _call(session, "S5", node_id="style_draft", tokens=100, created_at="2026-07-12T00:00:02Z")
    _call(session, "S5", node_id="style_draft", tokens=100, created_at="2026-07-12T00:00:03Z")
    # 2 个 QC → 第 2 个归 repeat_qc
    _call(session, "S5", node_id="hard_qc", tokens=50, created_at="2026-07-12T00:00:04Z")
    _call(session, "S5", node_id="hard_qc", tokens=50, created_at="2026-07-12T00:00:05Z")
    # 1 个失败调用 → failed_call
    _call(session, "S5", node_id="style_patch", tokens=40, error_code="LLM_TIMEOUT", created_at="2026-07-12T00:00:06Z")
    extra = ca.scene_cost(session, "S5")["extra_cost"]
    assert extra["failed_call_cost"] > 0
    assert extra["repeat_qc_cost"] > 0
    assert extra["low_dispersion_topup_cost"] > 0
    assert extra["total"] > 0
    assert 0 <= extra["retry_cost_ratio"] <= 1


def test_scene_cost_empty_no_calls(session):
    _scene(session, "S6")
    _runstate(session, "S6")
    result = ca.scene_cost(session, "S6")
    assert result["total_cost"] == 0
    assert result["call_count"] == 0


# ---- chapter / project rollup ------------------------------------------------

def test_chapter_cost_archived_metrics(session):
    _scene(session, "C1S1", chapter_id="CHX")
    _scene(session, "C1S2", chapter_id="CHX")
    _call(session, "C1S1", node_id="style_draft", tokens=200, chapter_id="CHX")
    _call(session, "C1S2", node_id="style_draft", tokens=100, chapter_id="CHX")
    _archived(session, "C1S1", chapter_id="CHX")
    _archived(session, "C1S2", chapter_id="CHX")
    result = ca.chapter_cost(session, "CHX")
    assert result["archived_scene_count"] == 2
    assert result["total_tokens"] == 300
    assert result["calibers"]["estimate"]["tokens"] == 300
    assert result["calibers"]["provider_actual"]["tokens"] == 0
    assert result["calibers"]["budget_charged"]["tokens"] == 0
    assert result["tokens_per_archived_scene"] == 150


def test_project_cost_rollup(session):
    _scene(session, "P1S1", chapter_id="PCH1", project_id="P1")
    _scene(session, "P1S2", chapter_id="PCH2", project_id="P1")
    _call(session, "P1S1", node_id="style_draft", tokens=200, chapter_id="PCH1", project_id="P1")
    _call(session, "P1S2", node_id="hard_qc", tokens=100, chapter_id="PCH2", project_id="P1")
    _archived(session, "P1S1", chapter_id="PCH1")
    result = ca.project_cost(session, "P1")
    assert result["total_cost"] > 0
    assert result["calibers"]["estimate"]["tokens"] == 300
    assert result["calibers"]["provider_actual"]["tokens"] == 0
    assert result["calibers"]["budget_charged"]["tokens"] == 0
    assert result["chapter_count"] == 2
    assert result["archived_scene_count"] == 1


# ---------------------------------------------------------------------------
# project_cost_dashboard：趋势 / 模型 / 节点 / 章节构成 + Top 调用
# ---------------------------------------------------------------------------

def _today_iso(offset_days=0, seq=0):
    from datetime import UTC, datetime, timedelta

    at = datetime.now(UTC) - timedelta(days=offset_days)
    return at.strftime("%Y-%m-%d") + f"T08:00:{seq:02d}Z"


def _dash_seed(session):
    _scene(session, "D1S1", chapter_id="DCH1", project_id="DP")
    _scene(session, "D1S2", chapter_id="DCH2", project_id="DP", seq=2)
    # 今天：草稿 300 tok；昨天：QC 100 tok（另一 provider/model）；8 天前：草稿 200 tok
    _call(session, "D1S1", node_id="style_draft", tokens=300, chapter_id="DCH1",
          project_id="DP", created_at=_today_iso(0, 1))
    _call(session, "D1S2", node_id="hard_qc", tokens=100, chapter_id="DCH2",
          project_id="DP", provider="anthropic", model="claude-x",
          created_at=_today_iso(1, 2))
    _call(session, "D1S1", node_id="style_draft", tokens=200, chapter_id="DCH1",
          project_id="DP", created_at=_today_iso(8, 3))
    session.flush()


def test_dashboard_trend_dense_window_and_bucketing(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", days=7)
    trend = dash["trend"]
    assert trend["days"] == 7
    assert len(trend["series"]) == 7
    # 稠密补零：窗口内无调用的天也在
    assert all("date" in item for item in trend["series"])
    # 8 天前的调用不进 7 天窗口
    assert trend["window_tokens"] == 400
    assert trend["window_call_count"] == 2
    today_bucket = trend["series"][-1]
    assert today_bucket["tokens"] == 300
    assert today_bucket["call_count"] == 1
    assert today_bucket["cost"] > 0
    yesterday_bucket = trend["series"][-2]
    assert yesterday_bucket["tokens"] == 100


def test_dashboard_summary_covers_all_calls_regardless_of_window(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", days=7)
    assert dash["summary"]["total_tokens"] == 600
    assert dash["summary"]["call_count"] == 3
    # summary 与 project_cost 同口径
    assert dash["summary"]["total_cost"] == ca.project_cost(session, "DP")["total_cost"]


def test_dashboard_by_model_sorted_by_cost(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP")
    by_model = dash["by_model"]
    assert len(by_model) == 2
    assert by_model[0]["cost"] >= by_model[1]["cost"]
    assert {(m["provider"], m["model"]) for m in by_model} == {
        ("openai_compatible", "gpt-5"),
        ("anthropic", "claude-x"),
    }
    gpt = next(m for m in by_model if m["model"] == "gpt-5")
    assert gpt["tokens"] == 500
    assert gpt["call_count"] == 2
    assert gpt["is_estimate"] is True


def test_dashboard_by_node_top_and_remainder(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", node_limit=1)
    by_node = dash["by_node"]
    assert len(by_node["top"]) == 1
    assert by_node["top"][0]["node_id"] == "style_draft"
    assert by_node["top"][0]["phase"] == "candidate_generation"
    assert by_node["top"][0]["tokens"] == 500
    assert by_node["remainder"]["node_count"] == 1
    assert by_node["remainder"]["tokens"] == 100


def test_dashboard_by_chapter_rollup(session):
    _dash_seed(session)
    # 未关联章节的项目级调用排最后
    _call(session, None, node_id="outline_expand", tokens=50, chapter_id=None, project_id="DP")
    session.flush()
    dash = ca.project_cost_dashboard(session, "DP")
    by_chapter = dash["by_chapter"]
    assert by_chapter[0]["chapter_id"] == "DCH1"
    assert by_chapter[0]["tokens"] == 500
    assert by_chapter[0]["scene_count"] == 1
    assert by_chapter[-1]["chapter_id"] is None
    assert by_chapter[-1]["tokens"] == 50


def test_dashboard_top_calls_ordered_and_limited(session):
    _dash_seed(session)
    dash = ca.project_cost_dashboard(session, "DP", call_limit=2)
    top = dash["top_calls"]
    assert len(top) == 2
    assert top[0]["cost"] >= top[1]["cost"]
    assert top[0]["total_tokens"] == 300
    for row in top:
        assert row["phase"] in {"candidate_generation", "quality_check"}
        assert row["accounting_status"]
        assert row["currency"]


def test_dashboard_days_clamped_and_bad_input_safe(session):
    _dash_seed(session)
    assert ca.project_cost_dashboard(session, "DP", days=0)["trend"]["days"] == 1
    assert ca.project_cost_dashboard(session, "DP", days=9999)["trend"]["days"] == ca.DASHBOARD_MAX_DAYS
    assert ca.project_cost_dashboard(session, "DP", days="oops")["trend"]["days"] == ca.DASHBOARD_DEFAULT_DAYS


def test_dashboard_empty_project_returns_empty_shapes(session):
    dash = ca.project_cost_dashboard(session, "NOPE")
    assert dash["summary"]["call_count"] == 0
    assert dash["by_model"] == []
    assert dash["by_node"] == {"top": [], "remainder": None}
    assert dash["by_chapter"] == []
    assert dash["top_calls"] == []
    assert len(dash["trend"]["series"]) == ca.DASHBOARD_DEFAULT_DAYS
    assert dash["trend"]["window_cost"] == 0
