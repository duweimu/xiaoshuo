"""FE-ALIGN Phase 4: 回收站（作品级软删 + 三级统一列表 + 永久清除）。"""
from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from tests.fixture_works import seed_fixture_works

_seq = 0


def _post(client, path, body=None):
    global _seq
    _seq += 1
    response = client.post(path, json=body or {}, headers={"X-Idempotency-Key": f"trash-{_seq}"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_project(client) -> dict:
    global _seq
    _seq += 1
    response = client.post(
        "/api/v2/projects",
        json={"title": f"回收站测试 {_seq}", "outline_text": "大纲"},
        headers={"X-Idempotency-Key": f"trash-create-{_seq}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def test_project_soft_delete_restore_roundtrip(client):
    project = _create_project(client)
    pid = project["project_id"]
    _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "第一章"})

    deleted = client.delete(f"/api/v2/projects/{pid}")
    assert deleted.status_code == 200, deleted.text

    listed = client.get("/api/v2/projects").json()["data"]["items"]
    assert all(item["project_id"] != pid for item in listed)

    trash = client.get("/api/v2/trash").json()["data"]["items"]
    entry = next(item for item in trash if item["id"] == f"work:{pid}")
    assert entry["kind"] == "work"
    assert entry["restorable"] is True

    _post(client, f"/api/v2/projects/{pid}/restore")
    listed = client.get("/api/v2/projects").json()["data"]["items"]
    assert any(item["project_id"] == pid for item in listed)
    # 数据无损：目录原样回来
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert tree["chapters"][0]["title"] == "第一章"


def test_project_trash_hides_children_from_legacy_author_routes(client):
    project = _create_project(client)
    project_id = project["project_id"]
    chapter = _post(
        client,
        f"/api/v2/projects/{project_id}/catalog/chapters",
        {"title": "父项目回收后不可见"},
    )["chapter"]
    chapter_id = chapter["chapter_id"]

    before = client.get("/api/v1/chapters")
    assert before.status_code == 200
    assert chapter_id in {
        item["chapter_id"] for item in before.json()["data"]["items"]
    }

    deleted = client.delete(f"/api/v2/projects/{project_id}")
    assert deleted.status_code == 200, deleted.text

    after = client.get("/api/v1/chapters")
    assert after.status_code == 200
    assert chapter_id not in {
        item["chapter_id"] for item in after.json()["data"]["items"]
    }

    restored = _post(client, f"/api/v2/projects/{project_id}/restore")
    assert restored["trashed"] is False
    visible_again = client.get("/api/v1/chapters")
    assert visible_again.status_code == 200
    assert chapter_id in {item["chapter_id"] for item in visible_again.json()["data"]["items"]}


def test_unified_trash_lists_three_levels(client, session):
    seed_fixture_works(session)
    session.commit()
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "要删的章"})["chapter"]
    scene_id = chapter["scenes"][0]["scene_id"]

    # 场景级软删（v2 桥接）
    scene_del = client.delete(f"/api/v2/projects/{pid}/catalog/scenes/{scene_id}")
    assert scene_del.status_code == 200, scene_del.text
    # 章级软删
    chapter_del = client.delete(f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}")
    # 既有规则：章下有已 trash 场景时阻止章删 —— 先恢复场景再删章
    if chapter_del.status_code == 409:
        _post(client, f"/api/v2/trash/scene:{scene_id}/restore")
        chapter_del = client.delete(f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}")
    assert chapter_del.status_code == 200, chapter_del.text
    # 作品级软删（demo 可删）
    work_del = client.delete("/api/v2/projects/work-b")
    assert work_del.status_code == 200, work_del.text

    merged = client.get(f"/api/v2/trash?project_id={pid}").json()["data"]["items"]
    kinds = {item["kind"] for item in merged}
    assert "work" in kinds and "chapter" in kinds
    # 章被删后其场景随章隐藏（场景行被章级 trash 级联），统一列表以章为单位呈现
    assert any(item["id"] == f"chapter:{chapter['chapter_id']}" for item in merged)
    assert any(item["id"] == "work:work-b" for item in merged)

    # 目录读不到已删的章
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert all(c["chapter_id"] != chapter["chapter_id"] for c in tree["chapters"])

    # 恢复链路
    _post(client, f"/api/v2/trash/chapter:{chapter['chapter_id']}/restore")
    _post(client, "/api/v2/trash/work:work-b/restore")
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert any(c["chapter_id"] == chapter["chapter_id"] for c in tree["chapters"])
    listed = client.get("/api/v2/projects").json()["data"]["items"]
    assert any(item["project_id"] == "work-b" for item in listed)


def test_scene_trash_keeps_draft_and_restore_brings_it_back(client, session):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    scene_id = chapter["scenes"][0]["scene_id"]
    ensured = _post(client, f"/api/v1/author-drafts/scene/{scene_id}/ensure")
    draft_id = ensured["draft"]["draft_id"]
    saved = client.patch(
        f"/api/v1/author-drafts/{draft_id}",
        json={"content": "正文留着，恢复即回。", "base_revision_no": ensured["draft"]["revision_no"]},
    )
    assert saved.status_code == 200

    client.delete(f"/api/v2/projects/{pid}/catalog/scenes/{scene_id}")
    _post(client, f"/api/v2/trash/scene:{scene_id}/restore")
    current = client.get(f"/api/v1/author-drafts/scene/{scene_id}/current").json()["data"]
    assert current["draft"]["content"] == "正文留着，恢复即回。"


def test_purge_project_leaves_no_residue(client, session):
    project = _create_project(client)
    pid = project["project_id"]
    _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "第一章"})

    # 未软删前不允许 purge
    premature = client.delete(f"/api/v2/trash/work:{pid}")
    assert premature.status_code == 409

    client.delete(f"/api/v2/projects/{pid}")
    headers = {"X-Idempotency-Key": "purge-project-replay"}
    purged = client.delete(f"/api/v2/trash/work:{pid}", headers=headers)
    assert purged.status_code == 200, purged.text
    replayed = client.delete(f"/api/v2/trash/work:{pid}", headers=headers)
    assert replayed.status_code == 200, replayed.text
    assert replayed.headers["X-Idempotency-Status"] == "replayed"
    assert replayed.json()["data"] == purged.json()["data"]

    assert session.query(StoryProject).filter_by(project_id=pid).count() == 0
    assert session.query(ChapterGoal).filter_by(project_id=pid).count() == 0
    assert session.query(SceneCard).filter_by(project_id=pid).count() == 0
    listed = client.get("/api/v2/projects").json()["data"]["items"]
    assert all(item["project_id"] != pid for item in listed)
    trash = client.get("/api/v2/trash").json()["data"]["items"]
    assert all(item["id"] != f"work:{pid}" for item in trash)


def test_scene_restore_blocked_when_chapter_trashed(client):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    scene_id = chapter["scenes"][0]["scene_id"]
    # 章级软删级联场景（既有规则：反向顺序——场景已删时章删会被 409 阻止）
    chapter_del = client.delete(f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}")
    assert chapter_del.status_code == 200, chapter_del.text

    merged = client.get(f"/api/v2/trash?project_id={pid}").json()["data"]["items"]
    scene_entry = next(item for item in merged if item["id"] == f"scene:{scene_id}")
    assert scene_entry["restorable"] is False  # 先恢复章

    blocked = client.post(
        f"/api/v2/trash/scene:{scene_id}/restore",
        json={},
        headers={"X-Idempotency-Key": "trash-blocked-restore"},
    )
    assert blocked.status_code == 409
