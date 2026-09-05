from __future__ import annotations

from novel_system.api.app import create_app
from novel_system.api.openapi_contract import ERROR_SCHEMA_NAME, SUCCESS_SCHEMA_NAME


_OPERATION_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def test_every_versioned_api_operation_documents_standard_success_and_error_envelopes() -> None:
    spec = create_app().openapi()
    schemas = spec["components"]["schemas"]

    assert schemas[SUCCESS_SCHEMA_NAME]["required"] == ["ok", "data", "error", "request_id"]
    assert schemas[ERROR_SCHEMA_NAME]["required"] == ["ok", "data", "error", "request_id"]

    operation_counts = {"v1": 0, "v2": 0}
    for path, path_item in spec["paths"].items():
        version = next(
            (name for name in operation_counts if path.startswith(f"/api/{name}/")),
            None,
        )
        if version is None:
            continue
        for method, operation in path_item.items():
            if method not in _OPERATION_METHODS:
                continue
            operation_counts[version] += 1
            responses = operation["responses"]
            successful = [response for status, response in responses.items() if status.startswith("2")]
            assert successful, f"{method.upper()} {path} has no success response"
            for response in successful:
                assert response["content"]["application/json"]["schema"] == _ref(SUCCESS_SCHEMA_NAME)
            assert responses["default"]["content"]["application/json"]["schema"] == _ref(ERROR_SCHEMA_NAME)
            for status, response in responses.items():
                if status == "default" or status.startswith("2"):
                    continue
                assert response["content"]["application/json"]["schema"] == _ref(ERROR_SCHEMA_NAME)

    # Guard each API generation independently: a healthy v1 count must never
    # hide an accidentally excluded v2 router (the original blind spot).
    # Floors sit just below the live counts; lower them only alongside a
    # deliberate endpoint retirement (2026-09 subtraction batch 1: v1 → 91, v2 → 96).
    assert operation_counts["v1"] >= 90
    assert operation_counts["v2"] >= 95


def test_health_probes_keep_their_minimal_non_enveloped_contract() -> None:
    spec = create_app().openapi()

    assert spec["paths"]["/live"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] != _ref(
        SUCCESS_SCHEMA_NAME
    )
    assert spec["paths"]["/ready"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] != _ref(
        SUCCESS_SCHEMA_NAME
    )
