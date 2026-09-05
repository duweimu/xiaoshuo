from __future__ import annotations

import pytest

from novel_system.services.llm_task_runner import LLMNodeRunner
from tests.real_llm_fakes import ScenePipelineOnlineFake

from .test_orchestrator_flow import seed_story

pytestmark = pytest.mark.chroma_integration


@pytest.fixture(autouse=True)
def _online_pipeline(monkeypatch) -> None:
    """假生成已退役：验收冒烟的整链场景运行注入在线记账测试替身。"""
    monkeypatch.setattr(
        "novel_system.services.orchestrator.LLMNodeRunner",
        lambda session: LLMNodeRunner(session, llm_client=ScenePipelineOnlineFake()),
    )


def test_l3_acceptance_smoke(client, session) -> None:
    seed_story(client, session=session)
    run_scene = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "acceptance-scene-1"},
    )
    assert run_scene.status_code == 200
    assert run_scene.json()["data"]["current_bundle_id"]
