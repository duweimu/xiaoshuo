from __future__ import annotations

import re
import tomllib
from pathlib import Path
from zoneinfo import ZoneInfo

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent


def _requirement_shape(value: str) -> tuple[str, str, str]:
    requirement = Requirement(value)
    return (
        canonicalize_name(requirement.name),
        str(requirement.specifier),
        str(requirement.marker or ""),
    )


def test_uv_lock_matches_pyproject_direct_dependencies() -> None:
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
    novel_package = next(package for package in lock["package"] if package["name"] == "novel-system")

    expected = {_requirement_shape(value) for value in project["project"]["dependencies"]}
    for extra_name, requirements in project["project"]["optional-dependencies"].items():
        expected.update(
            _requirement_shape(f"{value}; extra == '{extra_name}'")
            for value in requirements
        )
    actual = {
        _requirement_shape(
            f"{item['name']}{item.get('specifier', '')}"
            + (f"; {item['marker']}" if item.get("marker") else "")
        )
        for item in novel_package["metadata"]["requires-dist"]
    }
    assert actual == expected, "pyproject.toml changed without regenerating uv.lock"


def test_hashed_default_requirements_export_matches_uv_resolution() -> None:
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
    resolved: dict[str, set[str]] = {}
    for package in lock["package"]:
        if "registry" not in package.get("source", {}):
            continue
        resolved.setdefault(canonicalize_name(package["name"]), set()).add(str(package["version"]))

    exported: dict[str, set[str]] = {}
    text = (BACKEND_ROOT / "requirements.lock").read_text(encoding="utf-8")
    for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==([^ ;\\]+)", text):
        exported.setdefault(canonicalize_name(match.group(1)), set()).add(match.group(2))

    assert exported, "requirements.lock must not be empty"
    for package_name, versions in exported.items():
        assert package_name in resolved
        assert versions <= resolved[package_name], (
            f"{package_name} in requirements.lock does not match uv.lock"
        )

    required_default_names = {
        canonicalize_name(Requirement(value).name)
        for value in project["project"]["dependencies"]
    }
    required_default_names.update(
        canonicalize_name(Requirement(value).name)
        for value in project["project"]["optional-dependencies"]["dev"]
    )
    assert required_default_names <= exported.keys()
    assert "chromadb" not in exported
    assert "--extra dev" in text
    assert "--all-extras" not in text
    assert "--hash=sha256:" in text


def test_chroma_is_an_explicit_optional_dependency() -> None:
    project = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    core_names = {
        canonicalize_name(Requirement(value).name)
        for value in project["project"]["dependencies"]
    }
    chroma_names = {
        canonicalize_name(Requirement(value).name)
        for value in project["project"]["optional-dependencies"]["chroma"]
    }

    assert "chromadb" not in core_names
    assert chroma_names == {"chromadb"}


def test_hashed_chroma_requirements_export_is_committed() -> None:
    text = (BACKEND_ROOT / "requirements-chroma.lock").read_text(encoding="utf-8")
    assert "--extra chroma" in text
    assert "chromadb==1.5.9" in text
    assert "--hash=sha256:" in text


def test_runtime_timezone_and_node_lock_ownership_are_explicit() -> None:
    assert str(ZoneInfo("Asia/Shanghai")) == "Asia/Shanghai"
    assert not (REPOSITORY_ROOT / "package-lock.json").exists()
    assert (REPOSITORY_ROOT / "frontend-react" / "package-lock.json").is_file()
