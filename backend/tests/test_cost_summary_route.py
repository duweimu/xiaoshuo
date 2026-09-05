"""Wave 6（§6.3）：GET /api/v2/projects/{id}/cost-summary —— 成本页数据源。

project / chapter / scene 三级下钻；空项目不 500。
"""
from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    LlmCall,
    SceneCard,
    SceneRunState,
    StoryProject,
)


def _seed(session):
    session.add(StoryProject(project_id="RP", title="Route cost summary", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="RCH1",
            project_id="RP",
            planned_scene_count=1,
            chapter_goal="Summarize one archived scene",
        )
    )
    session.add(SceneCard(scene_id="RS1", chapter_id="RCH1", project_id="RP", scene_seq=1, scene_goal="g"))
    session.add(SceneRunState(scene_id="RS1", scene_token_budget=1000, scene_tokens_used=300))
    session.add(
        LlmCall(
            llm_call_id="rc1", provider="openai_compatible", model="gpt-5",
            scope_type="scene", scope_id="RS1",
            node_id="style_draft", step="style_draft", scene_id="RS1", chapter_id="RCH1",
            project_id="RP", prompt_tokens=200, completion_tokens=100, total_tokens=300,
        )
    )
    session.add(
        FinalScene(row_id="rfs1", scene_id="RS1", chapter_id="RCH1", content="正文",
                   status="archived", source_bundle_id="b", source_bundle_hash="h")
    )
    session.commit()


def test_project_cost_summary(client, session):
    _seed(session)
    r = client.get("/api/v2/projects/RP/cost-summary")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["level"] == "project"
    summary = data["summary"]
    assert summary["total_cost"] > 0
    assert summary["archived_scene_count"] == 1


def test_scene_drilldown(client, session):
    _seed(session)
    r = client.get("/api/v2/projects/RP/cost-summary", params={"scene_id": "RS1"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["level"] == "scene"
    assert data["summary"]["scene_id"] == "RS1"
    assert data["summary"]["budget"]["budget"] == 1000
    assert "phase_breakdown" in data["summary"]


def test_chapter_drilldown(client, session):
    _seed(session)
    r = client.get("/api/v2/projects/RP/cost-summary", params={"chapter_id": "RCH1"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["level"] == "chapter"
    assert data["summary"]["chapter_id"] == "RCH1"
    assert data["summary"]["archived_scene_count"] == 1


def test_empty_project_does_not_500(client):
    r = client.get("/api/v2/projects/NOPE/cost-summary")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["summary"]["total_cost"] == 0


def test_cost_dashboard_route(client, session):
    _seed(session)
    r = client.get("/api/v2/projects/RP/cost-dashboard", params={"days": 7})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["project_id"] == "RP"
    assert data["summary"]["total_cost"] > 0
    assert data["trend"]["days"] == 7
    assert len(data["trend"]["series"]) == 7
    assert isinstance(data["by_model"], list)
    assert "top" in data["by_node"]
    assert isinstance(data["by_chapter"], list)
    assert isinstance(data["top_calls"], list)
    # 额度快照与 cost-summary 同源
    assert "daily_tokens" in data["quota"]


def test_cost_dashboard_empty_project_does_not_500(client):
    r = client.get("/api/v2/projects/NOPE/cost-dashboard")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["summary"]["call_count"] == 0
    assert data["by_model"] == []
    assert data["top_calls"] == []
