"""构思/雪花工作台两处回归守卫（fix group: snowflake-generate-source）。

(1) React 工作台 ``ws-snow.jsx`` 的 ``structuredGenerate`` 对每个 AI 结构化生成按钮
    都 POST ``steps/{step_key}/generate``，body 带 ``source``（fe_scaffold_ai /
    fe_candidate_adopt / fe_scene_focus_ai / fe_char_focus_ai）。请求模型是
    ``extra="forbid"`` 的 StrictRequestModel，没有该字段就整体 422 —— 每一次点击都
    被挡在校验层，连「LLM 未启用」的诚实 409 都到不了。这里锁定：精确的 React body
    绝不能 422；LLM 关闭时是 409 SNOWFLAKE_LLM_REQUIRED；LLM 可用时 200 且触发入口
    随 step-run 的 health_json 落库并经历史列表可见；字段保持有界（非法值仍 422）。

(2) ``materialize`` 与 ``resync`` 对 ``SceneCard.beats_json`` 曾用两套配方（后者会
    多拼一个 hook）。规划表里 beats_json 为空（真实 LLM 输出常常不带这一列）时，刚
    物化完的每一场都会被报成「待同步」。这里锁定：物化 + 目录确认后
    ``resync_status.pending_count == 0``，dry-run resync 没有任何 diff。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import SceneCard, SnowflakeScenePlan
from tests.real_llm_fakes import install_skeleton_snowflake


ALL_STEPS = [
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "character_sheets",
    "short_synopsis",
    "character_synopses",
    "long_synopsis",
    "character_bibles",
    "scene_list",
    "scene_details",
]

# ws-snow.jsx structuredGenerate 发出的四种 body（require_llm 恒为 true）。
REACT_STRUCTURED_GENERATE_BODIES = {
    "book_brief": {"require_llm": True, "source": "fe_scaffold_ai"},
    "one_sentence_summary": {
        "require_llm": True,
        "source": "fe_candidate_adopt",
        "direction_text": "一封旧信把她拉回雨城，冷案牵出家族秘密。",
    },
    "scene_details": {
        "require_llm": True,
        "source": "fe_scene_focus_ai",
        "focus_scene_refs": ["row-1"],
    },
    "character_sheets": {
        "require_llm": True,
        "source": "fe_char_focus_ai",
        "focus_character_refs": ["CHAR01"],
    },
}


def _create_project(client, *, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": (
                "An old letter pulls the heroine back to Rain City.\n"
                "The cold case turns out to be tied to her family.\n"
                "She must decide whether the truth is worth the cost."
            ),
        },
        headers={"X-Idempotency-Key": f"create-fix-src-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _generate(client, project_id: str, step_key: str, body: dict, *, key: str):
    return client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json=body,
        headers={"X-Idempotency-Key": f"generate-fix-src-{project_id}-{step_key}-{key}"},
    )


def _approve(client, project_id: str, step_key: str, *, key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-fix-src-{project_id}-{step_key}-{key}"},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# (1) generate 接受 React 的 source 字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step_key", sorted(REACT_STRUCTURED_GENERATE_BODIES))
def test_generate_accepts_react_source_field_and_fails_closed_without_llm(client, step_key: str) -> None:
    """LLM 关闭：精确的 React body 必须落到诚实的 409，而不是校验层 422。"""
    pid = _create_project(client, key=f"react-body-{step_key}")
    response = _generate(client, pid, step_key, REACT_STRUCTURED_GENERATE_BODIES[step_key], key="no-llm")
    assert response.status_code != 422, response.text
    error = response.json()["error"]
    assert error["code"] != "REQUEST_VALIDATION_FAILED", response.text
    # 文档化的 fail-closed 契约：LLM 未启用 → 409 + 作者可执行的下一步。
    assert response.status_code == 409, response.text
    assert error["code"] == "SNOWFLAKE_LLM_REQUIRED"
    assert error["details"]["next_action"] == "configure_llm_then_retry"


def test_generate_records_react_trigger_source_on_step_run(client, monkeypatch) -> None:
    """LLM 可用：200，触发入口随 health_json 落库、确认后仍保留、历史列表可见。"""
    from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService

    # 规划器骨架直通替身在 settings.llm_enabled=False 时生效；require_llm 闸门看的
    # 是服务上的 llm_enabled() 探针，单独放行它即可得到一条不依赖真实模型的 200 路径。
    install_skeleton_snowflake(monkeypatch)
    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "llm_enabled", lambda self: True)

    pid = _create_project(client, key="react-trigger")
    response = _generate(client, pid, "book_brief", {"require_llm": True, "source": "fe_scaffold_ai"}, key="ok")
    assert response.status_code == 200, response.text
    step = response.json()["data"]["step"]
    assert step["last_generation_source"] == "llm"
    assert step["health"]["trigger_source"] == "fe_scaffold_ai"
    assert step["artifact"]["diagnosis_json"]["trigger_source"] == "fe_scaffold_ai"

    # 确认会重建 health_json：触发入口是这一版草稿的出处事实，不能在确认时被抹掉。
    _approve(client, pid, "book_brief", key="ok")
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    approved = next(s for s in ws["steps"] if s["step_key"] == "book_brief")
    assert approved["status"] == "approved"
    assert approved["health"]["trigger_source"] == "fe_scaffold_ai"

    history = client.get(f"/api/v2/projects/{pid}/snowflake-workspace/steps/book_brief/history").json()["data"]
    assert history["items"][0]["trigger_source"] == "fe_scaffold_ai"

    # 没带 source 的调用（脚本 / 旧客户端）不会凭空长出一个触发入口。
    response = _generate(client, pid, "book_brief", {}, key="plain")
    assert response.status_code == 200, response.text
    assert "trigger_source" not in response.json()["data"]["step"]["health"]


@pytest.mark.parametrize(
    "bad_source",
    ["Bad Source!", "fe-scaffold-ai", "x" * 65, 7],
    ids=["punctuation", "hyphen", "too_long", "not_a_string"],
)
def test_generate_source_field_stays_bounded(client, bad_source) -> None:
    """接受 source 不等于重新打开 envelope：非小写下划线短标识 / 超长 / 非字符串仍 422。"""
    pid = _create_project(client, key="bounded")
    response = _generate(client, pid, "book_brief", {"require_llm": True, "source": bad_source}, key="bad")
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_FAILED"
    assert any(issue["field"] == "body.source" for issue in error["details"]["issues"]), error


# ---------------------------------------------------------------------------
# (2) 物化与 resync 共用一套 beats_json 配方
# ---------------------------------------------------------------------------


def test_fresh_materialization_reports_no_pending_resync_when_plans_lack_beats(client, session, monkeypatch) -> None:
    install_skeleton_snowflake(monkeypatch)
    pid = _create_project(client, key="beats-recipe")
    for step_key in ALL_STEPS:
        response = _generate(client, pid, step_key, {}, key="beats")
        assert response.status_code == 200, response.text
        _approve(client, pid, step_key, key="beats")

    # 规划器骨架会给每场填占位节拍，正好掩盖两套配方的分歧；真实 LLM 输出常常不带
    # beats_json（清洗器不会补），规划行就停在默认的空列表——这才是线上物化后
    # 「N 场待同步」的起点。hook 非空是分歧显形的前提，一并断言。
    session.expire_all()
    plans = session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == pid)).scalars().all()
    assert plans
    assert any((plan.hook or "").strip() for plan in plans), "骨架场景应带 hook"
    for plan in plans:
        plan.beats_json = []
    session.commit()

    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "fix-src-materialize"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "fix-src-approve-outline"},
    )
    assert r.status_code == 200, r.text

    # 横幅数据源：刚物化完不能喊「N 场待同步」。
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert ws["resync_status"]["pending_count"] == 0, ws["resync_status"]
    assert ws["resync_status"]["pending_scenes"] == []

    # dry-run 全量 resync 与横幅口径一致：没有任何 diff。
    r = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/resync", json={"dry_run": True})
    assert r.status_code == 200, r.text
    results = r.json()["data"]["results"]
    assert results
    assert all(item["diff"] == {} and item["reason"] == "already_current" for item in results), results

    # 场卡节拍确实来自兜底配方（不是空的），且与规划行推导出的口径逐场一致。
    session.expire_all()
    for plan in plans:
        card = session.get(SceneCard, plan.scene_id)
        assert card is not None
        assert card.beats_json, plan.scene_id
