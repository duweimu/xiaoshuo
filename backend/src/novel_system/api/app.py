from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import ipaddress
import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect as sqlalchemy_inspect, text
from sqlalchemy.exc import OperationalError

from novel_system.api.response import error
from novel_system.api.openapi_contract import install_api_openapi_contract
from novel_system.api.request_limits import RequestBodyLimitMiddleware
from novel_system.api.routes import (
    author_drafts,
    catalog,
    canon_continuity,
    chapter_manuscripts,
    chapter_plan,
    chapters,
    cost,
    library,
    literary_quality,
    project_overview,
    projects,
    reference_safety,
    review,
    scenes,
    snowflake,
    snowflake_workspace,
    style_reference,
    system_config,
    trash,
    writer_deep_review,
)
from novel_system.db import models  # noqa: F401
from novel_system.db.base import Base
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from novel_system.db.session import engine
from novel_system.services.database_errors import is_database_busy_error
from novel_system.services.errors import DomainError
from novel_system.settings import get_settings


logger = logging.getLogger(__name__)
SUPPORTED_DATABASE_REVISION = CURRENT_SCHEMA_REVISION


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Discovery is synchronous and quick; actual generation remains in the
    # existing background workers.  Every dispatched worker still has to win
    # its durable CAS, so concurrent ASGI worker startups cannot execute the
    # same job twice.
    from novel_system.services.background_recovery import run_startup_recovery

    run_startup_recovery()
    try:
        yield
    finally:
        from novel_system.services.style_reference.run_orchestrator import (
            shutdown_style_reference_run_executor,
        )
        from novel_system.services.style_reference.validation.runner import (
            shutdown_style_reference_validation_executor,
        )

        shutdown_style_reference_run_executor(wait=False)
        shutdown_style_reference_validation_executor(wait=False)


def _is_loopback_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    # Starlette's in-process TestClient uses this sentinel as the peer name.
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


REMOTE_ACCESS_OPERATOR_REF = "remote-access-token"


def _operator_ref_from_request(request: Request, *, trust_client_header: bool) -> str:
    if not trust_client_header:
        # The remote access token is shared authentication, not an identity
        # provider. Never let its holder forge an arbitrary audit principal.
        return REMOTE_ACCESS_OPERATOR_REF
    actor_ref = (request.headers.get("X-Operator-Ref") or "").strip()
    return actor_ref or "operator"


def create_app() -> FastAPI:
    app_settings = get_settings(include_runtime_config=False)
    if not app_settings.local_only and not app_settings.remote_access_token:
        raise RuntimeError(
            "NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN is required when NOVEL_SYSTEM_LOCAL_ONLY=false"
        )
    if app_settings.auto_create_tables:
        Base.metadata.create_all(bind=engine())
    app = FastAPI(title="Novel System P2", lifespan=_lifespan)
    allow_origins = list(app_settings.cors_origins)
    allow_credentials = app_settings.cors_allow_credentials and "*" not in allow_origins
    # Register the body limiter before CORS. Starlette inserts new middleware
    # at the front, so CORS can still decorate a 413 response and the request-id
    # middleware declared below remains the outermost boundary.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=app_settings.max_request_body_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        started_at = time.perf_counter()
        request.state.request_id = f"req_{uuid.uuid4().hex[:12]}"
        request.state.operator_ref = _operator_ref_from_request(
            request,
            trust_client_header=app_settings.local_only,
        )
        peer_host = request.client.host if request.client is not None else None

        def finalize(response):
            response.headers["X-Request-Id"] = request.state.request_id
            logger.info(
                "api_access method=%s path=%s status=%s duration_ms=%.1f request_id=%s peer=%s",
                request.method,
                request.url.path,
                response.status_code,
                (time.perf_counter() - started_at) * 1000,
                request.state.request_id,
                peer_host or "unknown",
            )
            return response

        health_path = request.url.path in {"/live", "/ready"}
        cors_preflight = bool(
            request.method == "OPTIONS"
            and request.headers.get("Origin")
            and request.headers.get("Access-Control-Request-Method")
        )
        forwarded_request = bool(
            request.headers.get("Forwarded")
            or request.headers.get("X-Forwarded-For")
            or request.headers.get("X-Real-IP")
        )
        # Browser preflight never carries application credentials.  Let the
        # CORS middleware validate its Origin/requested headers; the subsequent
        # real request is still authenticated here without exception.
        if not health_path and not cors_preflight:
            if app_settings.local_only and (
                forwarded_request or not _is_loopback_host(peer_host)
            ):
                response = error(
                    "REMOTE_ACCESS_DISABLED",
                    "this service accepts loopback requests only",
                    status_code=403,
                    details={
                        "local_only": True,
                        "forwarded_request_rejected": forwarded_request,
                    },
                    req_id=request.state.request_id,
                )
                return finalize(response)
            if not app_settings.local_only:
                supplied = request.headers.get("X-Novel-Access-Token")
                expected = app_settings.remote_access_token or ""
                if supplied is None or not hmac.compare_digest(
                    supplied.encode("utf-8"), expected.encode("utf-8")
                ):
                    response = error(
                        "REMOTE_ACCESS_TOKEN_REQUIRED",
                        "valid X-Novel-Access-Token is required",
                        status_code=401,
                        details={"local_only": False},
                        req_id=request.state.request_id,
                    )
                    return finalize(response)
        response = await call_next(request)
        return finalize(response)

    @app.get("/live", tags=["health"])
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/ready", tags=["health"])
    def ready() -> dict[str, str]:
        try:
            with engine().connect() as connection:
                revisions = tuple(
                    str(value)
                    for value in connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                    if value
                )
                inspector = sqlalchemy_inspect(connection)
                available_tables = set(inspector.get_table_names())
                required_columns = {
                    table_name: tuple(column.name for column in table.columns)
                    for table_name, table in Base.metadata.tables.items()
                }
                missing_required_columns = {
                    table_name: sorted(
                        set(expected_columns)
                        - {
                            str(column["name"])
                            for column in inspector.get_columns(table_name)
                        }
                    )
                    for table_name, expected_columns in required_columns.items()
                    if table_name in available_tables
                }
                missing_required_columns = {
                    table_name: columns
                    for table_name, columns in missing_required_columns.items()
                    if columns
                }
        except Exception as exc:
            logger.exception("Readiness database probe failed")
            raise DomainError(
                "SERVICE_NOT_READY",
                "database readiness probe failed",
                status_code=503,
                details={
                    "retryable": True,
                    "reason": "database_probe_failed",
                    "expected_revision": SUPPORTED_DATABASE_REVISION,
                },
            ) from exc
        if revisions != (SUPPORTED_DATABASE_REVISION,):
            current_revision = revisions[0] if len(revisions) == 1 else None
            logger.error(
                "Readiness schema revision mismatch expected=%s actual=%s",
                SUPPORTED_DATABASE_REVISION,
                revisions,
            )
            raise DomainError(
                "SERVICE_NOT_READY",
                "database schema revision is not ready",
                status_code=503,
                details={
                    "retryable": False,
                    "reason": "schema_revision_mismatch",
                    "expected_revision": SUPPORTED_DATABASE_REVISION,
                    "current_revision": current_revision,
                },
            )
        missing_tables = sorted(set(Base.metadata.tables) - available_tables)
        if missing_tables:
            logger.error(
                "Readiness schema table check failed revision=%s missing_tables=%s",
                SUPPORTED_DATABASE_REVISION,
                missing_tables,
            )
            raise DomainError(
                "SERVICE_NOT_READY",
                "database schema is incomplete",
                status_code=503,
                details={
                    "retryable": False,
                    "reason": "schema_tables_missing",
                    "expected_revision": SUPPORTED_DATABASE_REVISION,
                    "missing_table_count": len(missing_tables),
                },
            )
        if missing_required_columns:
            missing_column_count = sum(
                len(columns) for columns in missing_required_columns.values()
            )
            logger.error(
                "Readiness schema column check failed revision=%s tables=%s columns=%s",
                SUPPORTED_DATABASE_REVISION,
                len(missing_required_columns),
                missing_column_count,
            )
            raise DomainError(
                "SERVICE_NOT_READY",
                "database schema is incomplete",
                status_code=503,
                details={
                    "retryable": False,
                    "reason": "schema_columns_missing",
                    "expected_revision": SUPPORTED_DATABASE_REVISION,
                    "missing_table_count": len(missing_required_columns),
                    "missing_column_count": missing_column_count,
                },
            )
        return {"status": "ready"}

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return error(
            exc.code,
            exc.message,
            status_code=exc.status_code,
            details=exc.details,
            req_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        # Never echo Pydantic's ``input`` field: it can contain an entire
        # manuscript or secret.  Field paths and stable error types are enough
        # for clients to correct the request while preserving the API envelope.
        issues = []
        for item in exc.errors()[:32]:
            issue_type = str(item.get("type") or "validation_error")
            public_message = {
                "extra_forbidden": "unexpected field",
                "field_required": "required field is missing",
                "int_type": "value must be an integer",
                "list_type": "value must be a list",
                "string_type": "value must be a string",
                "string_too_long": "string exceeds the allowed length",
                "string_too_short": "string is shorter than the allowed length",
                "too_long": "collection exceeds the allowed length",
                "greater_than_equal": "value is below the allowed minimum",
                "less_than_equal": "value exceeds the allowed maximum",
            }.get(issue_type, "invalid value")
            issues.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "type": issue_type,
                    "message": public_message,
                }
            )
        return error(
            "REQUEST_VALIDATION_FAILED",
            "request validation failed",
            status_code=422,
            details={
                "issues": issues,
                "issue_count": len(exc.errors()),
                "truncated": len(exc.errors()) > len(issues),
            },
            req_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(OperationalError)
    async def operational_error_handler(request: Request, exc: OperationalError):
        if is_database_busy_error(exc):
            return error(
                "DATABASE_BUSY",
                "database is busy; retry after the current long-running operation finishes",
                status_code=503,
                details={"retryable": True},
                req_id=getattr(request.state, "request_id", None),
            )
        return error(
            "DATABASE_OPERATION_FAILED",
            "database operation failed",
            status_code=500,
            details={"retryable": False},
            req_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        req_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled API error request_id=%s", req_id)
        return error(
            "INTERNAL_ERROR",
            str(exc) if app_settings.expose_error_detail else "internal server error",
            status_code=500,
            details={"retryable": False},
            req_id=req_id,
        )

    app.include_router(catalog.router)
    app.include_router(canon_continuity.router)
    app.include_router(chapter_plan.router)
    app.include_router(trash.router)
    app.include_router(chapters.router)
    app.include_router(projects.router)
    app.include_router(project_overview.router)
    app.include_router(cost.router)
    app.include_router(author_drafts.router)
    app.include_router(chapter_manuscripts.router)
    app.include_router(scenes.router)
    app.include_router(snowflake.router)
    app.include_router(snowflake_workspace.router)
    app.include_router(writer_deep_review.router)
    app.include_router(review.router)
    app.include_router(library.router)
    app.include_router(reference_safety.router)
    app.include_router(system_config.router)
    app.include_router(literary_quality.router)
    app.include_router(style_reference.router)
    install_api_openapi_contract(app)
    return app
