# 该清单必须与 api/app.py 实际 include_router 挂载的模块一一对应；
# 防漂移守卫：tests/test_routes_all_manifest.py。
__all__ = [
    "author_drafts",
    "canon_continuity",
    "catalog",
    "chapter_manuscripts",
    "chapter_plan",
    "chapters",
    "cost",
    "library",
    "literary_quality",
    "project_overview",
    "projects",
    "reference_safety",
    "review",
    "scenes",
    "snowflake",
    "snowflake_workspace",
    "style_reference",
    "system_config",
    "trash",
    "writer_deep_review",
]
