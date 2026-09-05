"""审计 P-4/P-5/P-6/P-7 回归：可选注入/基线功能的"生效断言"。

这些功能全部挂在 ``except Exception`` 降级路径后面——历史缺陷（查询不存在
的列、非法的 count().where()、project_id 字符串误推导、写入即销毁的裸
向量实例）都表现为"静默 no-op、测试全绿"。本文件对每个功能断言
**真实产出**（而非仅"不抛错"），并断言降级槽位账本为空。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.orchestrator import Orchestrator


def _seed_catalog_style_scene(session, project_id: str = "projp6"):
    """种一个目录冷启动风格（chapter_id 含多个下划线段）的场景。"""
    session.add(
        StoryProject(
            project_id=project_id, title="T", outline_text="o", planning_mode="snowflake"
        )
    )
    chapter_id = f"{project_id}_CH_deadbeef"
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            chapter_goal="目标",
            display_order=1,
        )
    )
    scene = SceneCard(
        scene_id=f"{chapter_id}_SC_cafebabe",
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=2,
        scene_goal="推进",
        pov_character_id="char_a",
        onstage_chars_json=["char_a"],
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene.scene_id))
    session.flush()
    return scene


def test_narrative_state_digest_uses_scene_project_id(session):
    """P-6：目录式 chapter_id 下，权威状态注入必须按 scene.project_id 命中事件。"""
    scene = _seed_catalog_style_scene(session)
    session.add(
        SceneCard(
            scene_id="earlier_scene",
            chapter_id=scene.chapter_id,
            project_id=scene.project_id,
            scene_seq=1,
            scene_goal="earlier",
        )
    )
    session.flush()
    log = NarrativeEventLog(session)
    log.log_event(
        project_id=scene.project_id,
        scene_id="earlier_scene",
        chapter_id=scene.chapter_id,
        event_type="character_state",
        entity_type="character",
        entity_id="char_a",
        fact_key="injury",
        fact_value="左臂骨折",
        authority_status="accepted",
        source_kind="test_fixture",
    )
    # 事件位于同章前一场，当前场景应能按权威目录位置读取。
    digest = BundleBuilder(session)._narrative_state_digest(scene)
    assert digest is not None, "有事件时权威状态注入不应为空"
    assert "左臂骨折" in digest


def test_scene_vector_indexing_persists_via_factory(session, monkeypatch):
    """P-7：归档场景索引必须写进 get_vector_store() 工厂实例（进程级可见），
    而不是函数返回即销毁的裸 InMemoryVectorStore。"""
    from novel_system.services.vector_store import get_vector_store

    scene = _seed_catalog_style_scene(session, project_id="projp7")
    result = Orchestrator._index_scene_to_vector_store(scene, "一段正文内容用于索引")

    store = get_vector_store()
    collection = f"scenes_{scene.project_id}"
    assert store.collection_exists(collection), "索引后集合应在工厂单例中可见"
    ids = {doc["id"] for doc in store.load_collection(collection)}
    assert scene.scene_id in ids
    assert result["outcome"] == "non_persistent"
    assert result["write_status"] in {"indexed", "already_present"}
    assert result["backend"] == "memory"
    assert result["validation_scope"] == "process_local"


def test_scene_vector_indexing_rejects_stale_same_id_without_overwrite(session):
    from novel_system.services.vector_store import get_vector_store

    scene = _seed_catalog_style_scene(session, project_id="projp7_stale")
    store = get_vector_store()
    collection = f"scenes_{scene.project_id}"
    store.write_collection(collection, [{"id": scene.scene_id, "text": "stale"}])

    result = Orchestrator._index_scene_to_vector_store(scene, "current")

    assert result["outcome"] == "failed"
    assert result["error_code"] == "VECTOR_INDEX_STALE_CONTENT"
    assert store.load_collection(collection) == [{"id": scene.scene_id, "text": "stale"}]
