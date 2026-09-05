from __future__ import annotations


def test_fixture_import_endpoint_is_gone(client) -> None:
    response = client.post(
        "/api/v1/review-items/import-demo",
        json={"review_id": "must-not-be-created", "item_type": "style_observation", "candidate_text": "fixture"},
    )

    assert response.status_code in (404, 405)
    assert "/api/v1/review-items/import-demo" not in client.get("/openapi.json").json()["paths"]
