from __future__ import annotations

import pytest

from novel_system.api.app import create_app
from novel_system.api.request_types import MAX_API_OBJECT_PROPERTIES


def _headers(key: str) -> dict[str, str]:
    return {"X-Idempotency-Key": key}


def _validation_issues(response) -> list[dict[str, str]]:
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "REQUEST_VALIDATION_FAILED"
    return payload["error"]["details"]["issues"]


def test_chapter_upsert_rejects_server_owned_fields(client) -> None:
    response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": "BOUNDARY_CH",
            "chapter_goal": "Keep lifecycle fields server-owned.",
            "trashed_flag": 0,
        },
        headers=_headers("boundary-ch-extra"),
    )

    assert response.status_code == 422
    assert any(item["type"] == "extra_forbidden" for item in _validation_issues(response))
    assert "Keep lifecycle fields server-owned." not in response.text


def test_scene_upsert_rejects_rollups_and_timestamps(client) -> None:
    chapter = client.post(
        "/api/v1/chapters",
        json={"chapter_id": "BOUNDARY_SC_CH", "chapter_goal": "A goal."},
        headers=_headers("boundary-sc-ch"),
    )
    assert chapter.status_code == 200

    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "BOUNDARY_SC",
            "chapter_id": "BOUNDARY_SC_CH",
            "scene_goal": "A scene goal.",
            "words_current": 999999,
            "updated_at": "spoofed",
        },
        headers=_headers("boundary-sc-extra"),
    )

    assert response.status_code == 422
    forbidden = {
        item["field"].rsplit(".", 1)[-1]
        for item in _validation_issues(response)
    }
    assert forbidden == {"updated_at", "words_current"}
    assert "999999" not in response.text


def test_existing_scene_cannot_be_reparented(client) -> None:
    for suffix in ("A", "B"):
        response = client.post(
            "/api/v1/chapters",
            json={"chapter_id": f"BOUNDARY_{suffix}", "chapter_goal": suffix},
            headers=_headers(f"boundary-parent-{suffix}"),
        )
        assert response.status_code == 200

    created = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "BOUNDARY_MOVE_SC",
            "chapter_id": "BOUNDARY_A",
            "scene_goal": "Stay in chapter A.",
        },
        headers=_headers("boundary-sc-create"),
    )
    assert created.status_code == 200

    moved = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "BOUNDARY_MOVE_SC",
            "chapter_id": "BOUNDARY_B",
            "scene_goal": "Try to move.",
        },
        headers=_headers("boundary-sc-move"),
    )

    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == "SCENE_IDENTITY_IMMUTABLE"


def test_flexible_json_body_rejects_excessive_top_level_properties(client) -> None:
    payload = {
        f"field_{index}": index
        for index in range(MAX_API_OBJECT_PROPERTIES + 1)
    }

    response = client.post("/api/v1/projects", json=payload)

    assert response.status_code == 422
    assert _validation_issues(response)
    assert "field_256" not in response.text


def test_flexible_json_body_rejects_excessive_nesting(client) -> None:
    payload: dict = {"value": "leaf"}
    for _ in range(25):
        payload = {"nested": payload}

    response = client.post("/api/v1/projects", json=payload)

    assert response.status_code == 422
    assert _validation_issues(response)
    assert "leaf" not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/passages/patch-candidates",
        "/api/v1/passage-patch-candidates/UNKNOWN/reject",
        "/api/v1/literary-quality/analyze-text",
        "/api/v1/literary-quality/chapter-set-review",
        "/api/v1/review-items/UNKNOWN/resolve",
        "/api/v1/review-items/UNKNOWN/unresolve",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/steps/book_brief/generate",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/steps/book_brief/fe-candidates",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/steps/book_brief/restore",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/steps/book_brief/approve",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/steps/book_brief/accept-stale",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/assistant",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/scene-triage/suggest",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/scenes/accept-stale",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/scene-triage/UNKNOWN/apply",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/orphaned-scenes/UNKNOWN/resolve",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/resync",
        "/api/v2/projects/UNKNOWN/snowflake-workspace/outline/approve",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/architecture/generate",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/plan/candidates",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/plan/fill",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/plan/review",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/plan/apply",
        "/api/v2/projects/UNKNOWN/catalog/chapters",
        "/api/v2/projects/UNKNOWN/catalog/chapter-order",
        "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN/scenes",
        "/api/v2/projects/UNKNOWN/catalog/import",
        "/api/v2/projects/UNKNOWN/library/entities",
        "/api/v2/projects/UNKNOWN/library/relations",
        "/api/v2/projects/UNKNOWN/library/timeline",
        "/api/v2/projects/UNKNOWN/library/characters",
        "/api/v1/author-drafts/chapter/UNKNOWN/ensure",
        "/api/v1/author-drafts/chapter/UNKNOWN/ensure-blank",
        "/api/v1/chapters/UNKNOWN/deep-review",
        "/api/v1/chapters/UNKNOWN/run/full",
        "/api/v1/scenes/UNKNOWN/deep-review",
        "/api/v1/scenes/UNKNOWN/preflight/create-cards",
        "/api/v1/scenes/UNKNOWN/resume-after-selection",
        "/api/v1/system-config/UNKNOWN/activate",
        "/api/v1/system-config/llm/providers/UNKNOWN/default",
        "/api/v2/projects/UNKNOWN/restore",
        "/api/v2/style-reference/books/UNKNOWN/reclassify",
        "/api/v2/style-reference/books/UNKNOWN/safety-profile/extract",
        "/api/v2/style-reference/profiles/UNKNOWN/preview",
        "/api/v2/style-reference/runs/UNKNOWN/cancel",
        "/api/v2/style-reference/runs/UNKNOWN/synthesize",
        "/api/v2/trash/UNKNOWN/restore",
    ],
)
def test_fixed_command_bodies_reject_unknown_fields_before_domain_lookup(
    client,
    path: str,
) -> None:
    response = client.post(path, json={"unexpected_secret": "must-not-be-echoed"})

    assert response.status_code == 422
    issues = _validation_issues(response)
    assert any(item["type"] == "extra_forbidden" for item in issues)
    assert "must-not-be-echoed" not in response.text


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("patch", "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN"),
        ("patch", "/api/v2/projects/UNKNOWN/catalog/scenes/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/catalog/chapters/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/catalog/scenes/UNKNOWN"),
        ("patch", "/api/v2/projects/UNKNOWN/library/entities/UNKNOWN"),
        ("patch", "/api/v2/projects/UNKNOWN/library/timeline/UNKNOWN"),
        ("patch", "/api/v2/projects/UNKNOWN/library/characters/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/library/relations/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/library/timeline/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/library/characters/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN/library/entities/UNKNOWN"),
        ("delete", "/api/v1/system-config/llm/providers/UNKNOWN"),
        ("delete", "/api/v2/projects/UNKNOWN"),
        ("delete", "/api/v2/style-reference/banned-terms/UNKNOWN"),
        ("delete", "/api/v2/style-reference/bindings/UNKNOWN"),
        ("delete", "/api/v2/style-reference/books/UNKNOWN"),
        ("delete", "/api/v2/trash/UNKNOWN"),
    ],
)
def test_fixed_non_post_bodies_reject_unknown_fields_before_domain_lookup(
    client,
    method: str,
    path: str,
) -> None:
    response = client.request(
        method,
        path,
        json={"unexpected_secret": "must-not-be-echoed"},
    )

    assert response.status_code == 422
    assert any(item["type"] == "extra_forbidden" for item in _validation_issues(response))
    assert "must-not-be-echoed" not in response.text


def test_every_open_json_mutation_schema_advertises_property_ceiling() -> None:
    spec = create_app().openapi()
    offenders: list[str] = []

    def inspect_schema(schema: dict, *, operation: str) -> None:
        if schema.get("type") == "object" and schema.get("additionalProperties") is True:
            if schema.get("maxProperties") != MAX_API_OBJECT_PROPERTIES:
                offenders.append(operation)
        for variant in schema.get("anyOf", []):
            inspect_schema(variant, operation=operation)

    for path, path_item in spec["paths"].items():
        for method in ("post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if operation is None:
                continue
            schema = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            inspect_schema(schema, operation=f"{method.upper()} {path}")

    assert offenders == []


def test_every_mutation_declares_a_request_body_contract() -> None:
    spec = create_app().openapi()
    missing: list[str] = []

    for path, path_item in spec["paths"].items():
        for method in ("post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if operation is not None and "requestBody" not in operation:
                missing.append(f"{method.upper()} {path}")

    assert missing == []
