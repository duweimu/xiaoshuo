"""FE-ALIGN Phase 5: 待办卡 effect 注册表（D4：后端事务执行）。

handler 签名 (session, project_id, payload) -> result dict。
resolve 端点在同一事务里执行 effect 并把卡置 resolved；未知 type 返回结构化错误。

首批 effect：
- insert_scene       复用 CatalogService.create_scene（P3）
- rename_chapter     复用 CatalogService.update_chapter
- bind_style_profile 复用 style_reference MaterializationService.apply_profile
- create_entity / add_timeline_event  P6 资料库接通时注册
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from novel_system.services.errors import DomainError

EffectHandler = Callable[[Session, str, dict[str, Any]], dict[str, Any]]

_REGISTRY: dict[str, EffectHandler] = {}


def register_effect(effect_type: str, handler: EffectHandler) -> None:
    _REGISTRY[effect_type] = handler


def run_effect(
    session: Session, project_id: str | None, effect: dict[str, Any]
) -> dict[str, Any]:
    effect_type = str((effect or {}).get("type") or "").strip()
    handler = _REGISTRY.get(effect_type)
    if handler is None:
        raise DomainError(
            "REVIEW_EFFECT_UNKNOWN",
            f"unknown review effect type: {effect_type!r}",
            status_code=400,
            details={"effect_type": effect_type, "registered": sorted(_REGISTRY)},
        )
    if not project_id:
        raise DomainError(
            "REVIEW_EFFECT_PROJECT_REQUIRED",
            "effect execution requires a project context",
            status_code=400,
        )
    return handler(session, project_id, dict(effect))


# ---------------------------------------------------------------------------
# 首批 handler
# ---------------------------------------------------------------------------


def _insert_scene(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.catalog import CatalogService

    chapter_id = str(payload.get("chapter_id") or "").strip()
    if not chapter_id:
        raise DomainError(
            "REVIEW_EFFECT_INVALID", "insert_scene requires chapter_id", status_code=400
        )
    scene = dict(payload.get("scene") or {})
    body = {
        "title": scene.get("title"),
        "kind": scene.get("kind"),
        "state": scene.get("state") or "todo",
        "brief": dict(scene.get("brief") or {}),
    }
    if payload.get("at") is not None:
        body["at"] = payload["at"]
    return CatalogService(session).create_scene(project_id, chapter_id, body)


def _rename_chapter(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.catalog import CatalogService

    chapter_id = str(payload.get("chapter_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not chapter_id or not title:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            "rename_chapter requires chapter_id and title",
            status_code=400,
        )
    return CatalogService(session).update_chapter(
        project_id, chapter_id, {"title": title}
    )


def _bind_style_profile(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.style_reference.materialization import (
        MaterializationService,
    )
    from novel_system.services.style_reference.schemas import (
        BindingScope,
        InjectionStrategy,
        TaskType,
    )

    profile_id = str(payload.get("profile_id") or "").strip()
    if not profile_id:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            "bind_style_profile requires profile_id",
            status_code=400,
        )
    scope_value = str(payload.get("scope") or "project")
    raw_scope_ref = str(payload.get("scope_ref_id") or "").strip()
    # 立项 A — scene/character 级绑定必须显式带目标 id;缺失则拒绝(否则静默回退
    # project_id 会落成「场景级绑定却指向项目」的脏数据)。项目级缺省回退 project_id。
    if scope_value in ("scene", "character") and not raw_scope_ref:
        raise DomainError(
            "REVIEW_EFFECT_INVALID",
            f"bind_style_profile scope={scope_value} requires scope_ref_id",
            status_code=400,
        )
    raw_strategy = payload.get("strategy")
    strategy = InjectionStrategy(str(raw_strategy)) if raw_strategy else None
    result = MaterializationService(session).apply_profile(
        profile_id,
        scope=BindingScope(scope_value),
        scope_ref_id=raw_scope_ref or project_id,
        task_type=TaskType(str(payload.get("task_type") or "scene_generation")),
        strategy=strategy,
        config_json=_style_injection_config(payload) or None,
    )
    return {
        "profile_id": result.profile_id,
        "binding_id": result.binding_id,
        "review_ids": result.review_ids,
        "rag_index": result.rag_index,
    }


def _style_injection_config(payload: dict[str, Any]) -> dict[str, Any]:
    """从决策卡 effect 载荷抽取注入配置(intensity / 维度 / include 开关)→
    binding.config_json。与 routes.style_reference.ApplyProfileRequest.injection_config
    同构,使「apply 决策卡批准」与「直接 /apply」落同样的 config。"""
    config: dict[str, Any] = {}
    intensity = payload.get("intensity")
    if intensity is not None:
        try:
            config["intensity"] = max(0, min(100, int(intensity)))
        except (TypeError, ValueError):
            pass
    sub_dimensions = payload.get("sub_dimensions")
    if isinstance(sub_dimensions, list) and sub_dimensions:
        config["sub_dimensions"] = [str(s) for s in sub_dimensions]
    for key in ("include_positive", "include_forbidden", "include_metric"):
        value = payload.get(key)
        if value is not None:
            config[key] = bool(value)
    return config


def _create_entity(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.library import LibraryService

    return LibraryService(session).create_entity(
        project_id,
        {
            "name": payload.get("name"),
            "kind": payload.get("kind") or "concept",
            "summary": payload.get("summary") or "",
            "aliases": payload.get("aliases") or [],
            "details": payload.get("details") or {},
            "tags": payload.get("tags") or [],
        },
    )


def _add_timeline_event(
    session: Session, project_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    from novel_system.services.library import LibraryService

    return LibraryService(session).create_timeline_event(
        project_id,
        {
            "label": payload.get("label"),
            "time_label": payload.get("time_label") or "",
            "chapter_ref": payload.get("chapter_ref") or "",
            "entity_refs": payload.get("entity_refs") or [],
            "note": payload.get("note") or "",
        },
    )


register_effect("insert_scene", _insert_scene)
register_effect("rename_chapter", _rename_chapter)
register_effect("bind_style_profile", _bind_style_profile)
register_effect("create_entity", _create_entity)
register_effect("add_timeline_event", _add_timeline_event)
