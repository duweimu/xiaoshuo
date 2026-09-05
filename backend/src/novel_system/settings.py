from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from novel_system.core_runtime import load_core_runtime
from novel_system.database_runtime import DEFAULT_DATABASE_PATH, load_database_runtime
from novel_system.llm_accounting_runtime import load_llm_accounting_runtime
from novel_system.runtime_defaults import DEFAULT_LLM_TIMEOUT_SECONDS


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_VECTOR_STORE_DIR = BACKEND_ROOT / ".vector_store"


@dataclass(slots=True)
class Settings:
    database_url: str
    vector_backend: str
    vector_store_dir: Path
    sqlite_foreign_keys_enabled: bool = True
    chroma_collection_prefix: str = "novel_system"
    idempotency_ttl_seconds: int = 90
    verify_lease_ttl_seconds: int = 180
    reindex_lease_ttl_seconds: int = 180
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    # Per-call LLM wall-clock ceiling. The 15-minute default allows slow local
    # generation while preventing a live-but-silent upstream from holding a
    # worker indefinitely. ``0`` remains an explicit operator opt-out.
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_enabled: bool = False
    # §8 opt-in: layer an independent LLM "editor" critic on top of the rule-based pass.
    llm_auto_critique_enabled: bool = False
    # §2 opt-in: extract narrative events from finished prose (not just the spec).
    llm_event_extraction_enabled: bool = False
    # Singleton-local hard fences, all opt-in: ``0`` disables a fence outright.
    # A single-author desktop install has no third party to fence off, and a
    # finite default only ever fires at the author mid-draft, so every fence
    # ships off and is re-armed by setting its env var to a positive value.
    llm_daily_token_limit: int = 0
    llm_monthly_token_limit: int = 0
    llm_project_daily_token_limit: int = 0
    llm_daily_request_limit: int = 0
    llm_max_concurrent_requests: int = 0
    # Startup reconciliation only touches unowned, non-scene reservations
    # older than this conservative TTL.  It must comfortably exceed normal
    # provider retries so a live legacy request is not mistaken for a crash.
    llm_reservation_recovery_ttl_seconds: int = 3_600
    llm_daily_cost_limit_usd: float = 0.0
    llm_input_cost_per_million_usd: float = 0.0
    llm_output_cost_per_million_usd: float = 0.0
    # Per-scene lifecycle budget multiplier: the scene end-to-end token ceiling is
    # ``N × single-shot baseline`` plus finite business/provider attempt caps. This
    # was the one hard fence that still shipped armed. A single-author desktop
    # install has no third party to fence off and a finite per-scene ceiling only
    # ever fires at the author mid-draft, so it now ships DISARMED like the rest of
    # the fence family: ``0`` = no scene ceiling (finite sentinel budgets, the CAS
    # gate is a no-op). Accounting is unchanged — the ledger and 成本看板 still
    # record every token. Set a positive value to re-arm ``N × baseline`` (and the
    # attempt caps fall back to their config/model defaults).
    scene_token_budget_multiplier: int = 0
    # Snowflake workspace prompt input budget override, in estimated tokens.
    # ``0`` = use each template's declared ``input_token_budget``. Set a positive
    # value to tighten it for a small-context local model (e.g. ollama), where the
    # per-template defaults would overflow the window. Over-budget payloads are
    # shed by relevance (never the step contract or the focused members) and the
    # shedding is reported, never silent.
    snowflake_input_token_budget: int = 0
    admin_token: str | None = None
    config_secret: str | None = None
    auto_create_tables: bool = False
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        # FE-ALIGN: React 前端 dev(5174) / preview(5175)
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    )
    cors_allow_credentials: bool = True
    expose_error_detail: bool = False
    # The desktop service is local-only by default. Remote access is an explicit
    # deployment mode and must be protected by a shared access token.
    local_only: bool = True
    remote_access_token: str | None = None
    # The application reads request bodies in memory.  Keep one global ceiling
    # above the 10 MiB reference-book limit so JSON and multipart parsing can
    # never allocate an attacker-controlled, unbounded buffer.
    max_request_body_bytes: int = 16 * 1024 * 1024
    # Server-side path imports are disabled unless one or more roots are listed.
    # Browser uploads remain available and are the preferred import path.
    style_reference_import_roots: tuple[Path, ...] = ()
    # ``review`` prevents unattended archive for high-risk heuristic matches;
    # ``audit`` records the same findings without blocking publication.
    content_safety_mode: str = "review"
    # Test/acceptance fixture import is a write-capable maintenance boundary.
    # It is absent from OpenAPI and disabled unless an operator opts in.
    fixture_import_enabled: bool = False


def _get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _get_strict_bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off)")


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _get_quota_int_env(name: str, default: int) -> int:
    """Parse an optional hard-fence bound, where ``0`` disables the fence."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    message = f"{name} must be a non-negative integer (0 disables the limit)"
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(message) from exc
    if value < 0:
        raise ValueError(message)
    return value


def _get_list_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    items = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return items or default


def _resolve_runtime_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BACKEND_ROOT / path


def _get_path_list_env(
    name: str,
    default: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    raw_value = os.environ.get(name, "")
    if not raw_value.strip():
        return default
    return tuple(
        _resolve_runtime_path(item.strip())
        for item in raw_value.split(os.pathsep)
        if item.strip()
    )


def get_settings(*, include_runtime_config: bool = True) -> Settings:
    database_runtime = load_database_runtime()
    database_url = database_runtime.database_url
    sqlite_foreign_keys_enabled = database_runtime.sqlite_foreign_keys_enabled
    vector_backend = os.environ.get("NOVEL_SYSTEM_VECTOR_BACKEND", "memory")
    vector_store_dir = _resolve_runtime_path(
        os.environ.get("NOVEL_SYSTEM_CHROMA_DIR", DEFAULT_VECTOR_STORE_DIR)
    )
    chroma_collection_prefix = os.environ.get("NOVEL_SYSTEM_CHROMA_COLLECTION_PREFIX", "novel_system")
    core_runtime = load_core_runtime()
    llm_provider = core_runtime.llm_provider
    llm_base_url = core_runtime.llm_base_url
    llm_api_key = core_runtime.llm_api_key
    llm_timeout_seconds = core_runtime.llm_timeout_seconds
    llm_enabled = core_runtime.llm_enabled
    llm_auto_critique_enabled = _get_bool_env("NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED", False)
    llm_event_extraction_enabled = _get_bool_env("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", False)
    accounting_runtime = load_llm_accounting_runtime()
    llm_daily_token_limit = accounting_runtime.daily_token_limit
    llm_monthly_token_limit = accounting_runtime.monthly_token_limit
    llm_project_daily_token_limit = accounting_runtime.project_daily_token_limit
    llm_daily_request_limit = accounting_runtime.daily_request_limit
    llm_max_concurrent_requests = accounting_runtime.max_concurrent_requests
    llm_reservation_recovery_ttl_seconds = (
        accounting_runtime.reservation_recovery_ttl_seconds
    )
    llm_daily_cost_limit_usd = accounting_runtime.daily_cost_limit_usd
    llm_input_cost_per_million_usd = accounting_runtime.input_cost_per_million_usd
    llm_output_cost_per_million_usd = accounting_runtime.output_cost_per_million_usd
    scene_token_budget_multiplier = _get_quota_int_env(
        "NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", 0
    )
    snowflake_input_token_budget = _get_quota_int_env(
        "NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET", 0
    )
    admin_token = core_runtime.admin_token
    config_secret = core_runtime.config_secret
    auto_create_tables = _get_bool_env("NOVEL_SYSTEM_AUTO_CREATE_TABLES", False)
    cors_origins = _get_list_env(
        "NOVEL_SYSTEM_CORS_ORIGINS",
        (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            # FE-ALIGN: React 前端 dev(5174) / preview(5175)
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5175",
            "http://localhost:5175",
            "http://127.0.0.1:8081",
            "http://localhost:8081",
        ),
    )
    cors_allow_credentials = _get_bool_env("NOVEL_SYSTEM_CORS_ALLOW_CREDENTIALS", True)
    expose_error_detail = _get_bool_env("NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL", False)
    local_only = _get_strict_bool_env("NOVEL_SYSTEM_LOCAL_ONLY", True)
    remote_access_token = os.environ.get("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN") or None
    max_request_body_bytes = _get_positive_int_env(
        "NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES",
        16 * 1024 * 1024,
    )
    style_reference_import_roots = _get_path_list_env(
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS"
    )
    content_safety_mode = os.environ.get("NOVEL_SYSTEM_CONTENT_SAFETY_MODE", "review").strip().lower()
    if content_safety_mode not in {"review", "audit"}:
        raise ValueError("NOVEL_SYSTEM_CONTENT_SAFETY_MODE must be review or audit")
    settings = Settings(
        database_url=database_url,
        vector_backend=vector_backend,
        vector_store_dir=vector_store_dir,
        sqlite_foreign_keys_enabled=sqlite_foreign_keys_enabled,
        chroma_collection_prefix=chroma_collection_prefix,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_enabled=llm_enabled,
        llm_auto_critique_enabled=llm_auto_critique_enabled,
        llm_event_extraction_enabled=llm_event_extraction_enabled,
        llm_daily_token_limit=llm_daily_token_limit,
        llm_monthly_token_limit=llm_monthly_token_limit,
        llm_project_daily_token_limit=llm_project_daily_token_limit,
        llm_daily_request_limit=llm_daily_request_limit,
        llm_max_concurrent_requests=llm_max_concurrent_requests,
        llm_reservation_recovery_ttl_seconds=llm_reservation_recovery_ttl_seconds,
        llm_daily_cost_limit_usd=llm_daily_cost_limit_usd,
        llm_input_cost_per_million_usd=llm_input_cost_per_million_usd,
        llm_output_cost_per_million_usd=llm_output_cost_per_million_usd,
        scene_token_budget_multiplier=scene_token_budget_multiplier,
        snowflake_input_token_budget=snowflake_input_token_budget,
        admin_token=admin_token,
        config_secret=config_secret,
        auto_create_tables=auto_create_tables,
        cors_origins=cors_origins,
        cors_allow_credentials=cors_allow_credentials,
        expose_error_detail=expose_error_detail,
        local_only=local_only,
        remote_access_token=remote_access_token,
        max_request_body_bytes=max_request_body_bytes,
        style_reference_import_roots=style_reference_import_roots,
        content_safety_mode=content_safety_mode,
        fixture_import_enabled=_get_strict_bool_env("NOVEL_SYSTEM_ENABLE_FIXTURE_IMPORT", False),
    )
    if not include_runtime_config:
        return settings

    from novel_system.services.system_config import apply_active_api_config

    return apply_active_api_config(settings)
