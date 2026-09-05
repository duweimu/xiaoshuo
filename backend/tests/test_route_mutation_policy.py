from __future__ import annotations

import ast
from pathlib import Path


ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "novel_system" / "api" / "routes"
MUTATION_METHODS = {"post", "put", "patch", "delete"}
# Route handlers must go through the two shared wrappers in api/mutations.py:
# - idempotent_response: X-Idempotency-Key required (400 IDEMPOTENCY_KEY_REQUIRED when missing)
# - optional_idempotent_response: keyed calls replay durably, unkeyed legacy calls execute once
# Raw execute_with_idempotency / execute_with_optional_idempotency calls and
# per-file copies (_with_idem, _mutation_response aliases) were migrated away
# and must not reappear in route files.
IDEMPOTENCY_BOUNDARIES = {
    "idempotent_response",
    "optional_idempotent_response",
}
READ_ONLY_POST_EXEMPTIONS = {
    ("style_reference.py", "dryrun_injection_preview"),
}


def _route_methods(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    methods: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in MUTATION_METHODS | {"get"}:
            methods.add(target.attr)
    return methods


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def test_every_http_mutation_has_an_idempotency_boundary_or_a_reviewed_read_only_exemption() -> None:
    uncovered: list[str] = []
    observed_exemptions: set[tuple[str, str]] = set()

    for path in sorted(ROUTES_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods = _route_methods(node)
            if not methods.intersection(MUTATION_METHODS):
                continue
            route_key = (path.name, node.name)
            if route_key in READ_ONLY_POST_EXEMPTIONS:
                segment = ast.get_source_segment(source, node) or ""
                assert "idempotency-exempt: deterministic read-only preview" in segment
                observed_exemptions.add(route_key)
                continue
            if not _called_names(node).intersection(IDEMPOTENCY_BOUNDARIES):
                uncovered.append(f"{path.name}:{node.lineno}:{node.name}")

    assert observed_exemptions == READ_ONLY_POST_EXEMPTIONS
    assert uncovered == []


def test_route_handlers_never_own_transactions_directly() -> None:
    offenders: list[str] = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not _route_methods(node):
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "commit":
                    offenders.append(f"{path.name}:{node.lineno}:{node.name}")
                    break

    assert offenders == []
