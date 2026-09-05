"""Style Reference v1.1 — Phase 1 路由清单(PR-4)+ PR-7 validate / reports。

参见 plans/style-reference-v1-1-fancy-shannon.md §"路由清单"。
prefix: /api/v2/style-reference。
在既有导入、抽取、画像、校验和注入预览端点上，增加候选盲选反馈聚合读接口。
不含公开 inject 写接口(PR-8)。
"""

from __future__ import annotations

# Runtime truth: the public injection contract is `SystemPromptFragments`
# returned by the two `injection-preview` endpoints. There is no public
# `/inject` / `InjectionBundle` HTTP API in the current implementation.

import logging
import uuid
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import BoundedJsonObject, EmptyRequest
from novel_system.api.response import ok
from novel_system.db.models import ReviewItem, utcnow

logger = logging.getLogger(__name__)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.dimensions import Layer
from novel_system.services.style_reference.ingest import (
    MAX_REFERENCE_BOOK_BYTES,
    IngestService,
)
from novel_system.services.style_reference.materialization import MaterializationService
from novel_system.services.style_reference.preview import PreviewService
from novel_system.services.style_reference.profile_synthesizer import ProfileSynthesizer
from novel_system.services.style_reference.profile_fields import generation_safe_summary
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.run_orchestrator import (
    RunOrchestrator,
    start_style_reference_run_worker,
)
from novel_system.services.style_reference.injection import (
    InjectionService,
    default_injection_strategy,
    injection_task_defaults,
)
from novel_system.services.style_reference.metrics_aggregator import MetricsAggregator
from novel_system.services.style_reference.schemas import (
    BindingScope,
    InjectionPreviewRequest,
    InjectionStrategy,
    RunStatus,
    TaskType,
    ValidateRequest,
    ValidationMode,
    ValidationTargetKind,
)
from novel_system.services.style_reference.validation import (
    ValidationOrchestrator,
    start_style_reference_validation_worker,
)
from novel_system.services.system_config import require_admin_token

router = APIRouter(tags=["style_reference"])

PATH_PREFIX = "/api/v2/style-reference"


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class ImportPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file_path: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=512)
    author_label: str | None = Field(default=None, max_length=255)
    cloud_policy: Literal["allow_full_cloud", "segments_only", "local_only"]
    # Wave 7 §5.9 — 导入权属声明 {analysis_rights, send_rights, declared_by}
    rights_declaration: BoundedJsonObject | None = None


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    layers: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(
        default=None, max_length=4
    )
    # True 时立即返回 RUNNING + run_id,抽取在后台线程执行;
    # 调用方轮询 GET /runs/{run_id} 读 coverage_json.progress
    background: bool = False
    # True 时无视 §6.4 输入量门槛(skip 层剔除),强制抽取所请求层
    force: bool = False


class ApplyConfigMixin(BaseModel):
    """apply 时落入 binding.config_json 的注入配置(MIXED 策略消费)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    intensity: int | None = Field(default=None, ge=0, le=100)
    sub_dimensions: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = (
        Field(default=None, max_length=128)
    )
    include_positive: bool | None = None
    include_forbidden: bool | None = None
    include_metric: bool | None = None


class FindingReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Domain validation owns the stable STYLE_REFERENCE_REVIEW_DECISION_INVALID.
    decision: str = Field(min_length=1, max_length=64)
    comment: str | None = Field(default=None, max_length=4_000)


class FindingFeedbackRequest(BaseModel):
    """立项 B — finding 用户反馈(👍/👎)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    vote: str = Field(min_length=1, max_length=64)


class BannedTermCreateRequest(BaseModel):
    """禁用词登记:generation=生成期红线段填充;extraction=抽取期段落过滤。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=512)
    replacement_hint: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="generation", min_length=1, max_length=64)


class ApplyProfileRequest(ApplyConfigMixin):
    scope: str = Field(min_length=1, max_length=64)
    scope_ref_id: str | None = Field(default=None, max_length=255)
    task_type: str = Field(default="scene_generation", min_length=1, max_length=64)
    strategy: str | None = Field(default=None, min_length=1, max_length=64)

    def injection_config(self) -> dict[str, Any]:
        """非空注入配置 → binding.config_json(端到端打通 intensity 滑块)。"""
        config: dict[str, Any] = {}
        if self.intensity is not None:
            config["intensity"] = max(0, min(100, int(self.intensity)))
        if self.sub_dimensions:
            config["sub_dimensions"] = [str(s) for s in self.sub_dimensions]
        for key in ("include_positive", "include_forbidden", "include_metric"):
            value = getattr(self, key)
            if value is not None:
                config[key] = bool(value)
        return config


class ValidateGeneratedRequest(BaseModel):
    """`POST /profiles/{id}/validate` body(profile_id 在 path,不在 body)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    generated_text: str = Field(min_length=1, max_length=2_000_000)
    target_kind: str = Field(default="manual", min_length=1, max_length=64)
    target_ref_id: str | None = Field(default=None, max_length=255)
    # The route translates invalid values to STYLE_REFERENCE_VALIDATE_PARAM_INVALID.
    mode: str = Field(default="async_full", min_length=1, max_length=64)
    task_context: BoundedJsonObject | None = None


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _serialize_book(book) -> dict[str, Any]:
    return {
        "book_id": book.book_id,
        "title": book.title,
        "author_label": book.author_label,
        "source_kind": book.source_kind,
        "source_path": book.source_path,
        "cloud_policy": book.cloud_policy,
        "text_checksum": book.text_checksum,
        "total_chars": book.total_chars,
        "status": book.status,
        "stats_json": book.stats_json or {},
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }


def _serialize_run(run) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "book_id": run.book_id,
        "status": run.status,
        "phase": run.phase,
        "dispatch_state": run.dispatch_state,
        "requested_layers": list(run.requested_layers_json or []),
        "coverage_json": run.coverage_json or {},
        "heartbeat_at": run.heartbeat_at,
        "error_code": run.error_code,
        "error_text": run.error_text,
        "retryable": bool(run.retryable),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _serialize_finding(
    finding, *, evidence: list | None = None, user_vote: str | None = None
) -> dict[str, Any]:
    payload = {
        "finding_id": finding.finding_id,
        "book_id": finding.book_id,
        "run_id": finding.run_id,
        "extraction_id": finding.extraction_id,
        "sub_dimension": finding.sub_dimension,
        "finding_kind": finding.finding_kind,
        "statement": finding.statement,
        "confidence": finding.confidence,
        # 立项 B — 合成基线(NULL=未经反馈调整);前端可据此展示 confidence 漂移。
        "base_confidence": finding.base_confidence,
        "status": finding.status,
        "review_id": finding.review_id,
    }
    # PR-23 — 仅 ?include=evidence 时输出;不带 include 的调用方零回归
    if evidence is not None:
        payload["evidence"] = evidence
    # 立项 B — 当前请求 operator 对该 finding 的票(None=未投);供前端回显投票高亮(跨刷新)
    if user_vote is not None:
        payload["user_vote"] = user_vote
    return payload


def _serialize_profile(profile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "book_id": profile.book_id,
        "run_id": profile.run_id,
        "title": profile.title,
        "status": profile.status,
        "profile_json": profile.profile_json or {},
        "coverage_json": profile.coverage_json or {},
        "version_tag": profile.version_tag,
        "source_finding_ids_json": profile.source_finding_ids_json or [],
    }


def _invalidate_profiles_after_finding_membership_change(
    repo: StyleReferenceRepository,
    finding,
    *,
    previous_status: str,
    next_status: str,
) -> list[str]:
    """finding 进入/退出 rejected 集合时，使同 run 的派生画像失效。

    synthesize 的输入集合是“全部非 rejected finding”。因此 pending 与 approved
    互换不改变画像输入；任一状态与 rejected 互换则会改变输入集合。旧画像中的
    summary/features 无法安全地局部删改，必须停止注入并要求重新合成。
    """
    if (previous_status == "rejected") == (next_status == "rejected"):
        return []

    invalidated: list[str] = []
    for profile in repo.list_profiles(book_id=finding.book_id):
        if profile.run_id != finding.run_id or profile.status == "archived":
            continue
        coverage = dict(profile.coverage_json or {})
        coverage.update(
            {
                "stale": True,
                "stale_reason": "source_finding_membership_changed",
                "stale_finding_id": finding.finding_id,
            }
        )
        profile.coverage_json = coverage
        profile.status = "draft"
        invalidated.append(profile.profile_id)
    repo.session.flush()
    return invalidated


def _serialize_binding(binding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "profile_id": binding.profile_id,
        "scope": binding.scope,
        "scope_ref_id": binding.scope_ref_id,
        "task_type": binding.task_type,
        "strategy": binding.strategy,
        "status": binding.status,
        "config_json": binding.config_json or {},
    }


def _actor(request: Request) -> str:
    return getattr(request.state, "operator_ref", None) or "operator"


def _req_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def _get_llm_client_and_enabled():
    """委托统一工厂 build_runtime_llm_client;保留模块级名字供路由测试打桩。"""
    from novel_system.services.system_config import build_runtime_llm_client
    from novel_system.settings import get_settings

    return build_runtime_llm_client(settings=get_settings())


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------


@router.post(f"{PATH_PREFIX}/books/import-path")
def import_book_path(
    payload: ImportPathRequest,
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    session: Session = Depends(get_session),
):
    # A path is interpreted by the server process, not the browser. Keep this
    # capability behind the existing local/admin boundary in addition to the
    # configured-root check in IngestService.
    require_admin_token(x_admin_token, client_host=_client_host(request))
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        service = IngestService(session, llm_enabled=False)
        result = service.ingest_path(
            file_path=body["file_path"],
            title=body["title"],
            author_label=body.get("author_label"),
            cloud_policy=body["cloud_policy"],
            rights_declaration=body.get("rights_declaration"),
        )
        return {
            "book": _serialize_book(result.book),
            "paragraphs_count": result.paragraphs_count,
            "safety": result.safety_payload,
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/import-path",
        payload=body,
        action=_do,
    )


"""上传体积上限:参考书是纯文本,30 万字 UTF-8 约 1MB;10MB 已极宽裕,
超限直接 413,避免 `file.read()` 把任意大文件整块载入内存。"""
MAX_UPLOAD_BYTES = MAX_REFERENCE_BOOK_BYTES


@router.post(f"{PATH_PREFIX}/books/import-upload")
async def import_book_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(..., min_length=1, max_length=512),
    author_label: str | None = Form(default=None, max_length=255),
    cloud_policy: str = Form(..., min_length=1, max_length=64),
    rights_declaration: str | None = Form(default=None, max_length=20_000),
    session: Session = Depends(get_session),
):
    raw_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise DomainError(
            "STYLE_REFERENCE_UPLOAD_TOO_LARGE",
            f"upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit",
            status_code=413,
        )
    # Wave 7 §5.9 — 权属声明以 JSON 串走 multipart form；解析失败当未声明
    rights_obj: dict[str, Any] | None = None
    if rights_declaration:
        try:
            parsed = json.loads(rights_declaration)
        except (ValueError, TypeError) as exc:
            raise DomainError(
                "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID",
                "rights_declaration must be a JSON object",
                status_code=400,
            ) from exc
        if not isinstance(parsed, dict):
            raise DomainError(
                "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID",
                "rights_declaration must be a JSON object",
                status_code=400,
            )
        rights_obj = parsed
    payload: dict[str, Any] = {
        "file_name": file.filename,
        "title": title,
        "author_label": author_label,
        "cloud_policy": cloud_policy,
        "rights_declaration": rights_obj,
    }

    def _do() -> dict[str, Any]:
        service = IngestService(session, llm_enabled=False)
        result = service.ingest_upload(
            raw_bytes=raw_bytes,
            file_name=payload["file_name"],
            title=payload["title"],
            author_label=payload.get("author_label"),
            cloud_policy=payload["cloud_policy"],
            rights_declaration=payload.get("rights_declaration"),
        )
        return {
            "book": _serialize_book(result.book),
            "paragraphs_count": result.paragraphs_count,
            "safety": result.safety_payload,
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/import-upload",
        payload=payload,
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/books")
def list_books(
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    books = repo.list_books(status=status)
    return ok(
        {"books": [_serialize_book(b) for b in books]},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/books/{{book_id}}")
def get_book(
    book_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    return ok({"book": _serialize_book(book)}, req_id=_req_id(request))


@router.delete(f"{PATH_PREFIX}/books/{{book_id}}")
def delete_book(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        book = repo.get_book(book_id)
        if book is None:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
        # FK 反向 cascade(无 ON DELETE CASCADE):派生数据走 purge_derived_data
        # (与 reclassify 共用),再删 paragraphs → book
        purge_derived_data(session, book_id)
        repo.delete_paragraphs_for_book(book_id)
        repo.delete_book(book_id)
        return {"book_id": book_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}",
        payload={"book_id": book_id},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/reclassify")
def reclassify_book(
    book_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """PR-23 — 重跑段落分类器(复用 ingest 的 classify_paragraphs 管线)。

    更新 paragraph_type / stats_json 并清空派生数据(paragraphs 与 book 保留)。
    分类需要 LLM;不可用时 IngestService.reclassify 抛 LLMRequiredError。
    """

    def _do() -> dict[str, Any]:
        client, enabled = _get_llm_client_and_enabled()
        service = IngestService(session, llm_client=client, llm_enabled=enabled)
        paragraphs_count = service.reclassify(book_id)
        return {
            "book_id": book_id,
            "status": "reclassified",
            "paragraphs_count": paragraphs_count,
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/reclassify",
        payload={"book_id": book_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.post(f"{PATH_PREFIX}/books/{{book_id}}/runs")
def start_run(
    book_id: str,
    payload: StartRunRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    client, enabled = _get_llm_client_and_enabled()
    background = bool(body.get("background"))

    def _do() -> dict[str, Any]:
        # PR-23 — 默认值单点:不带 layers 时全 4 层抽取(语 + 叙 + 景 + 题)
        layers_raw = body.get("layers") or ["language", "narrative", "scene", "theme"]
        try:
            layers = [Layer(layer) for layer in layers_raw]
        except ValueError as exc:
            raise DomainError(
                "STYLE_REFERENCE_LAYER_INVALID",
                f"invalid layer: {exc}",
                status_code=400,
            ) from exc
        orch = RunOrchestrator(session, llm_client=client, llm_enabled=enabled)
        result = orch.start_extract_run(
            book_id,
            layers=layers,
            background=background,
            force=bool(body.get("force")),
            defer_dispatch=background,
        )
        return {
            "run_id": result.run_id,
            "book_id": result.book_id,
            "status": result.status,
            "layers": result.layers,
            "sub_dim_results": [
                {
                    "sub_dimension": r.sub_dimension.value,
                    "findings_count": len(r.findings),
                    "extractions_created": r.extractions_created,
                }
                for r in result.sub_dim_results
            ],
        }

    def _dispatch(result: dict[str, Any]) -> None:
        if not enabled or client is None:
            # A successful replay can happen after an operator disables the
            # provider. Leave the durable run queued for normal recovery.
            logger.warning(
                "style-reference run %s remains queued because LLM is disabled",
                result.get("run_id"),
            )
            return
        start_style_reference_run_worker(
            run_id=str(result["run_id"]),
            book_id=str(result["book_id"]),
            layer_values=[str(layer) for layer in result.get("layers") or []],
            llm_client=client,
        )

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/books/{{book_id}}/runs",
        payload={"book_id": book_id, **body},
        action=_do,
        after_commit=_dispatch if background else None,
    )


@router.get(f"{PATH_PREFIX}/books/{{book_id}}/runs")
def list_book_runs(
    book_id: str,
    request: Request,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    """列出某书的抽取 run(最新在前)。前端维度矩阵据此在合成画像前定位最新 run
    及其 findings(无 list-runs 时只能从 profile.run_id 反推,合成前拿不到)。"""
    repo = StyleReferenceRepository(session)
    runs = repo.list_runs(book_id=book_id, status=status)
    runs = sorted(runs, key=lambda r: (r.created_at or "", r.run_id), reverse=True)
    return ok(
        {"runs": [_serialize_run(r) for r in runs]},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/runs/{{run_id}}")
def get_run(
    run_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    run = repo.get_run(run_id)
    if run is None:
        raise DomainError(
            "STYLE_REFERENCE_RUN_NOT_FOUND",
            f"run {run_id!r} not found",
            status_code=404,
        )
    return ok({"run": _serialize_run(run)}, req_id=_req_id(request))


@router.post(f"{PATH_PREFIX}/runs/{{run_id}}/cancel")
def cancel_run(
    run_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        run = repo.get_run(run_id)
        if run is None:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_FOUND",
                f"run {run_id!r} not found",
                status_code=404,
            )
        # 取消只对 pending/running 有意义。已取消的重复取消是幂等 no-op(不重写 finished_at);
        # done/failed 是终态:改写成 cancelled 会让已合成的 profile 挂在「被取消」的 run 上,
        # 也抹掉 failed 的 error_code/retryable——按场景 run-job 的 RUN_JOB_CANCEL_CONFLICT 契约回 409。
        if run.status == RunStatus.CANCELLED.value:
            return {"run_id": run_id, "status": run.status}
        if run.status not in {RunStatus.PENDING.value, RunStatus.RUNNING.value}:
            raise DomainError(
                "STYLE_REFERENCE_RUN_CANCEL_CONFLICT",
                f"run {run_id!r} already finished with status {run.status!r} and cannot be cancelled",
                status_code=409,
                details={"run_id": run_id, "status": run.status},
            )
        updated = repo.update_run(
            run_id,
            status=RunStatus.CANCELLED.value,
            dispatch_state="cancelled",
            heartbeat_at=utcnow(),
            finished_at=utcnow(),
            retryable=False,
        )
        if updated is None:  # get_run 刚命中同一行；只有并发删除才会走到这里
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_FOUND",
                f"run {run_id!r} disappeared while being cancelled",
                status_code=404,
                details={"run_id": run_id},
            )
        return {"run_id": run_id, "status": updated.status}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/runs/{{run_id}}/cancel",
        payload={"run_id": run_id},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/runs/{{run_id}}/findings")
def list_run_findings(
    run_id: str,
    request: Request,
    sub_dimension: str | None = None,
    finding_kind: str | None = None,
    status: str | None = None,
    include: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    findings = repo.list_findings(
        run_id=run_id,
        sub_dimension=sub_dimension,
        finding_kind=finding_kind,
        status=status,
    )
    # PR-23 — ?include=evidence:总查询数固定 3 条(findings + evidences + quotes)
    evidence_map: dict[str, list[dict[str, Any]]] | None = None
    if include == "evidence":
        evidences = repo.list_evidences_for_findings([f.finding_id for f in findings])
        quotes = {
            q.quote_id: q
            for q in repo.list_quotes_by_ids([e.quote_id for e in evidences])
        }
        evidence_map = {}
        for e in evidences:
            quote = quotes.get(e.quote_id)
            evidence_map.setdefault(e.finding_id, []).append(
                {
                    "evidence_id": e.evidence_id,
                    "anchor_kind": e.anchor_kind,
                    "is_synthetic": e.is_synthetic,
                    "quote_text": quote.quote_text if quote else "",
                    "paragraph_id": quote.paragraph_id if quote else None,
                    "span": [quote.span_start, quote.span_end] if quote else None,
                }
            )
    # 立项 B — 批量取当前 operator 的票,回显投票高亮(跨刷新持久)
    vote_map = repo.operator_votes_for_findings(
        [f.finding_id for f in findings], _actor(request)
    )
    return ok(
        {
            "findings": [
                _serialize_finding(
                    f,
                    evidence=(
                        evidence_map.get(f.finding_id, [])
                        if evidence_map is not None
                        else None
                    ),
                    user_vote=vote_map.get(f.finding_id),
                )
                for f in findings
            ]
        },
        req_id=_req_id(request),
    )


# ---------------------------------------------------------------------------
# Findings review
# ---------------------------------------------------------------------------


@router.post(f"{PATH_PREFIX}/findings/{{finding_id}}/review")
def review_finding(
    finding_id: str,
    payload: FindingReviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        finding = repo.get_finding(finding_id)
        if finding is None:
            raise DomainError(
                "STYLE_REFERENCE_FINDING_NOT_FOUND",
                f"finding {finding_id!r} not found",
                status_code=404,
            )
        decision = body["decision"]
        if decision not in ("approved", "rejected", "pending"):
            raise DomainError(
                "STYLE_REFERENCE_REVIEW_DECISION_INVALID",
                f"decision {decision!r} not allowed",
                status_code=400,
            )
        previous_status = finding.status
        # 创建或 update ReviewItem(prefix `review_style_ref_finding_`)
        review_id = f"review_style_ref_finding_{finding_id[-12:]}"
        existing = session.get(ReviewItem, review_id)
        if existing is None:
            review = ReviewItem(
                review_id=review_id,
                item_type=(
                    "banned_rule_cluster"
                    if finding.finding_kind == "forbidden_pattern"
                    else "style_observation"
                ),
                status=decision,
                candidate_text=finding.statement,
                candidate_payload_json={
                    "source": "style_reference_finding_review",
                    "finding_id": finding_id,
                    "sub_dimension": finding.sub_dimension,
                    "finding_kind": finding.finding_kind,
                    "comment": body.get("comment"),
                },
                active_on_approve=0,
            )
            session.add(review)
        else:
            existing.status = decision
            existing.candidate_payload_json = {
                **(existing.candidate_payload_json or {}),
                "comment": body.get("comment"),
            }
        # 反向更新 finding.review_id + status
        repo.update_finding(finding_id, review_id=review_id, status=decision)
        invalidated_profile_ids = _invalidate_profiles_after_finding_membership_change(
            repo,
            finding,
            previous_status=previous_status,
            next_status=decision,
        )
        session.flush()
        return {
            "finding_id": finding_id,
            "review_id": review_id,
            "decision": decision,
            "invalidated_profile_ids": invalidated_profile_ids,
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/findings/{{finding_id}}/review",
        payload={"finding_id": finding_id, **body},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/findings/{{finding_id}}/user-feedback")
def user_feedback_finding(
    finding_id: str,
    payload: FindingFeedbackRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """立项 B — finding 用户反馈(👍/👎)聚合 → 调档 confidence。一人一票(幂等)。"""
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        from novel_system.services.style_reference.finding_feedback import (
            apply_feedback,
        )

        return apply_feedback(
            session, finding_id, operator_ref=_actor(request), vote=body["vote"]
        )

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/findings/{{finding_id}}/user-feedback",
        # operator_ref 入幂等 payload:幂等记录按 (finding, operator) 分区,
        # 避免不同用户相同 finding+vote 共享幂等键导致误归因重放。
        payload={"finding_id": finding_id, "operator_ref": _actor(request), **body},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Synthesize
# ---------------------------------------------------------------------------


@router.post(f"{PATH_PREFIX}/runs/{{run_id}}/synthesize")
def synthesize_profile(
    run_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        run = repo.get_run(run_id)
        if run is None:
            raise DomainError(
                "STYLE_REFERENCE_RUN_NOT_FOUND",
                f"run {run_id!r} not found",
                status_code=404,
            )
        client, enabled = _get_llm_client_and_enabled()
        synth = ProfileSynthesizer(session, llm_client=client, llm_enabled=enabled)
        profile = synth.synthesize(run.book_id, run_id)
        # FE-ALIGN P5：风格学习完成 → 全局 decision 卡（任一作品的收件箱可见；
        # 「应用到本项目」effect 在 resolve 时以当前作品为 scope 执行绑定）
        try:
            from novel_system.services.review_cards import ReviewCardService

            profile_json = profile.profile_json or {}
            summary = generation_safe_summary(profile_json)
            ReviewCardService(session).create_card(
                {
                    "project_id": None,
                    "kind": "decision",
                    "priority": 1,
                    "title": f"参考画像「{profile.title}」是否应用到本项目",
                    "source": "风格参考",
                    "where": "风格参考 · 刚学完",
                    "detail": (summary[:200] + ("…" if len(summary) > 200 else ""))
                    or "画像已合成，可应用为写作润色基线，可随时关闭。",
                    "dedupe_key": f"style-profile:{profile.profile_id}",
                    "actions": [
                        {
                            "label": "应用到本项目",
                            "intent": "primary",
                            "op": "resolve",
                            "effect": {
                                "type": "bind_style_profile",
                                "profile_id": profile.profile_id,
                            },
                        },
                        {
                            "label": "先去看画像",
                            "intent": "ghost",
                            "op": "nav",
                            "nav_to": "styleref",
                        },
                        {"label": "丢弃", "intent": "quiet", "op": "resolve"},
                    ],
                },
                actor_ref="style_reference",
            )
        except Exception:  # 卡片失败不阻塞画像合成
            logger.exception("style profile decision card creation failed")
        return {"profile": _serialize_profile(profile)}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/runs/{{run_id}}/synthesize",
        payload={"run_id": run_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/profiles")
def list_profiles(
    request: Request,
    book_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profiles = repo.list_profiles(book_id=book_id, status=status)
    return ok(
        {"profiles": [_serialize_profile(p) for p in profiles]},
        req_id=_req_id(request),
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}")
def get_profile(
    profile_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profile = repo.get_profile(profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    return ok({"profile": _serialize_profile(profile)}, req_id=_req_id(request))


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/preview")
def preview_profile(
    profile_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        client, enabled = _get_llm_client_and_enabled()
        svc = PreviewService(session, llm_client=client, llm_enabled=enabled)
        results = svc.generate(profile_id)
        return {
            "profile_id": profile_id,
            "samples": [r.model_dump() for r in results],
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/preview",
        payload={"profile_id": profile_id},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/apply")
def apply_profile(
    profile_id: str,
    payload: ApplyProfileRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        try:
            scope = BindingScope(body["scope"])
            task_type = TaskType(body.get("task_type") or "scene_generation")
            raw_strategy = body.get("strategy")
            strategy = InjectionStrategy(raw_strategy) if raw_strategy else None
        except ValueError as exc:
            raise DomainError(
                "STYLE_REFERENCE_APPLY_PARAM_INVALID",
                str(exc),
                status_code=400,
            ) from exc
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id,
            scope=scope,
            scope_ref_id=body.get("scope_ref_id"),
            task_type=task_type,
            strategy=strategy,
            config_json=payload.injection_config() or None,
        )
        return {
            "profile_id": result.profile_id,
            "binding_id": result.binding_id,
            "review_ids": result.review_ids,
            "item_type_counts": result.item_type_counts,
            "rag_index": result.rag_index,
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/apply",
        payload={"profile_id": profile_id, **body},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/bindings")
def list_bindings(
    profile_id: str,
    request: Request,
    task_type: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    bindings = repo.list_bindings(profile_id=profile_id, task_type=task_type)
    return ok(
        {"bindings": [_serialize_binding(b) for b in bindings]},
        req_id=_req_id(request),
    )


@router.delete(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def delete_binding(
    binding_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        rowcount = repo.delete_binding(binding_id)
        if rowcount == 0:
            raise DomainError(
                "STYLE_REFERENCE_BINDING_NOT_FOUND",
                f"binding {binding_id!r} not found",
                status_code=404,
            )
        return {"binding_id": binding_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/bindings/{{binding_id}}",
        payload={"binding_id": binding_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Banned terms(禁用词:generation=注入红线段填充 / extraction=抽取段落过滤)
# ---------------------------------------------------------------------------


BANNED_TERM_SCOPES = ("generation", "extraction")


def _serialize_banned_term(term) -> dict[str, Any]:
    return {
        "term_id": term.term_id,
        "profile_id": term.profile_id,
        "term": term.term,
        "replacement_hint": term.replacement_hint,
        "source": term.source,
        "scope": term.scope,
        "created_at": term.created_at,
    }


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def list_banned_terms(
    profile_id: str,
    request: Request,
    scope: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    if repo.get_profile(profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    terms = repo.list_banned_terms(profile_id, scope=scope)
    return ok(
        {"terms": [_serialize_banned_term(t) for t in terms]},
        req_id=_req_id(request),
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def create_banned_term(
    profile_id: str,
    payload: BannedTermCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    term_text = payload.term.strip()
    scope = payload.scope.strip()

    def _do() -> dict[str, Any]:
        if not term_text:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                "term must be non-empty",
                status_code=400,
            )
        if scope not in BANNED_TERM_SCOPES:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                f"scope must be one of {BANNED_TERM_SCOPES}",
                status_code=400,
            )
        repo = StyleReferenceRepository(session)
        if repo.get_profile(profile_id) is None:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_NOT_FOUND",
                f"profile {profile_id!r} not found",
                status_code=404,
            )
        # (profile_id, term, scope) 唯一:重复创建返回既有行(幂等友好)
        existing = repo.find_banned_term(profile_id, term_text, scope)
        if existing is not None:
            if payload.replacement_hint is not None:
                existing.replacement_hint = payload.replacement_hint
            return {"term": _serialize_banned_term(existing), "created": False}
        row = repo.create_banned_term(
            term_id=f"sr_term_{uuid.uuid4().hex[:12]}",
            profile_id=profile_id,
            term=term_text,
            replacement_hint=payload.replacement_hint,
            source="user",
            scope=scope,
        )
        return {"term": _serialize_banned_term(row), "created": True}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms",
        payload={
            "profile_id": profile_id,
            "term": term_text,
            "scope": scope,
            "replacement_hint": payload.replacement_hint,
        },
        action=_do,
    )


@router.delete(f"{PATH_PREFIX}/banned-terms/{{term_id}}")
def delete_banned_term(
    term_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        row = repo.get_banned_term(term_id)
        if row is None:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_NOT_FOUND",
                f"banned term {term_id!r} not found",
                status_code=404,
            )
        if row.source == "preset":
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_PROTECTED",
                "preset banned terms cannot be deleted",
                status_code=400,
            )
        repo.delete_banned_term(term_id)
        return {"term_id": term_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/banned-terms/{{term_id}}",
        payload={"term_id": term_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# PR-7 — Validation endpoints
# ---------------------------------------------------------------------------


def _serialize_validation_report(report) -> dict[str, Any]:
    status = report.status
    if not report.verdict and status == "completed":
        # Compatibility for reports created before durable async status was
        # introduced (or by a focused repository test without the new field).
        status = "queued"
    public_status = {
        "queued": "pending",
        "completed": "done",
    }.get(status, status)
    return {
        "report_id": report.report_id,
        "profile_id": report.profile_id,
        "target_kind": report.target_kind,
        "target_ref_id": report.target_ref_id,
        "verdict": report.verdict,
        "status": public_status,
        "error_code": report.error_code,
        "error_text": report.error_text,
        "retryable": bool(report.retryable),
        "started_at": report.started_at,
        "heartbeat_at": report.heartbeat_at,
        "finished_at": report.finished_at,
        "quantitative_json": report.quantitative_json or [],
        "semantic_json": report.semantic_json or [],
        "plagiarism_json": report.plagiarism_json or {},
        "forbidden_hits_json": report.forbidden_hits_json or [],
        "mode_executed": report.mode_executed,
        "created_at": report.created_at,
    }


# async_full 的 pending report(verdict 空)超过该时长视为后台 worker 孤儿
# (进程重启 / 线程池丢失),轮询端点上惰性降级为 fail,避免前端永久轮询。
REPORT_PENDING_TIMEOUT_MINUTES = 10


def _reap_orphan_report(session: Session, report) -> None:
    legacy_pending = not report.verdict and report.status == "completed"
    if report.status == "queued":
        # A queued report has not acquired a worker yet. Startup recovery owns
        # detection of a lost queue because this endpoint cannot distinguish it
        # from valid executor backpressure.
        return
    if report.status != "running" and not legacy_pending:
        return
    from datetime import datetime, timedelta, timezone

    try:
        last_seen = datetime.fromisoformat(
            str(report.heartbeat_at or report.started_at or report.created_at).replace(
                "Z", "+00:00"
            )
        )
    except (TypeError, ValueError):
        return
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=REPORT_PENDING_TIMEOUT_MINUTES
    )
    if last_seen < cutoff:
        report.verdict = "fail"
        report.status = "failed"
        report.error_code = "STYLE_REFERENCE_VALIDATION_INTERRUPTED"
        report.error_text = (
            "async validation was interrupted; submit the text again to retry"
        )
        report.retryable = True
        report.heartbeat_at = utcnow()
        report.finished_at = utcnow()
        session.flush()


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/validate")
def validate_profile_generated(
    profile_id: str,
    payload: ValidateGeneratedRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """PR-7 §7 — sync_only / async_full 双路径 validation。"""
    body = payload.model_dump(mode="json")
    try:
        target_kind = ValidationTargetKind(body.get("target_kind") or "manual")
        mode = ValidationMode(body.get("mode") or "async_full")
    except ValueError as exc:
        raise DomainError(
            "STYLE_REFERENCE_VALIDATE_PARAM_INVALID",
            str(exc),
            status_code=400,
        ) from exc

    req = ValidateRequest(
        generated_text=body["generated_text"],
        target_kind=target_kind,
        target_ref_id=body.get("target_ref_id"),
        mode=mode,
        task_context=body.get("task_context"),
    )
    client, enabled = _get_llm_client_and_enabled()
    background = mode == ValidationMode.ASYNC_FULL

    def _do() -> dict[str, Any]:
        orch = ValidationOrchestrator(session, llm_client=client, llm_enabled=enabled)
        result = orch.validate(profile_id, req, defer_dispatch=background)
        return result.model_dump(mode="json")

    def _dispatch(result: dict[str, Any]) -> None:
        start_style_reference_validation_worker(
            report_id=str(result["report_id"]),
            profile_id=profile_id,
            generated_text=req.generated_text,
            llm_client=client,
            llm_enabled=enabled,
        )

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/validate",
        payload={"profile_id": profile_id, **body},
        action=_do,
        after_commit=_dispatch if background else None,
    )


@router.get(f"{PATH_PREFIX}/reports/{{report_id}}")
def get_validation_report(
    report_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    report = repo.get_validation_report(report_id)
    if report is None:
        raise DomainError(
            "STYLE_REFERENCE_REPORT_NOT_FOUND",
            f"validation report {report_id!r} not found",
            status_code=404,
        )
    _reap_orphan_report(session, report)
    return ok({"report": _serialize_validation_report(report)}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/reports")
def list_validation_reports(
    profile_id: str,
    request: Request,
    verdict: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    reports = repo.list_validation_reports(profile_id=profile_id, verdict=verdict)
    return ok(
        {"reports": [_serialize_validation_report(r) for r in reports]},
        req_id=_req_id(request),
    )


# ---------------------------------------------------------------------------
# PR-9 — Injection preview endpoints
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/bindings/{{binding_id}}/injection-preview")
def get_binding_injection_preview(
    binding_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """PR-9 §5.1 — 读已落盘 binding 渲染 fragments + prefix。"""
    repo = StyleReferenceRepository(session)
    binding = repo.get_binding(binding_id)
    if binding is None:
        raise DomainError(
            "STYLE_REFERENCE_BINDING_NOT_FOUND",
            f"binding {binding_id!r} not found",
            status_code=404,
        )
    profile = repo.get_profile(binding.profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {binding.profile_id!r} not found",
            status_code=404,
        )
    try:
        strategy = InjectionStrategy(binding.strategy)
    except ValueError:
        strategy = InjectionStrategy.A
    fragments = InjectionService(session)._render(
        profile, strategy, binding.config_json or {}
    )
    return ok(
        {
            "fragments": fragments.model_dump(),
            "prefix": fragments.to_system_prompt_prefix(),
        },
        req_id=_req_id(request),
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/injection-preview")
def dryrun_injection_preview(
    profile_id: str,
    payload: InjectionPreviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """PR-9 §5.1 — dryrun:不写盘,直接按入参 strategy/intensity/sub_dimensions 渲染。"""
    # idempotency-exempt: deterministic read-only preview; no DB/file/provider side effect.
    repo = StyleReferenceRepository(session)
    profile = repo.get_profile(profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    config: dict[str, Any] = {
        "intensity": payload.intensity,
        "sub_dimensions": payload.sub_dimensions,
        "include_positive": payload.include_positive,
        "include_forbidden": payload.include_forbidden,
    }
    if payload.include_metric is not None:
        config["include_metric"] = payload.include_metric
    strategy = payload.strategy or default_injection_strategy(payload.task_type)
    fragments = InjectionService(session)._render(profile, strategy, config)
    return ok(
        {
            "fragments": fragments.model_dump(),
            "prefix": fragments.to_system_prompt_prefix(),
        },
        req_id=_req_id(request),
    )


# ---------------------------------------------------------------------------
# Injection 只读辅助:任务默认表 + 叠层预览(前端「注入应用」页数据源)
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/injection/task-defaults")
def get_injection_task_defaults(request: Request):
    """TaskType → 默认策略 + 运行时刷新周期(refresh 真源:llm_node_registry)。"""
    return ok({"tasks": injection_task_defaults()}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/injection/layers")
def get_injection_layers(
    request: Request,
    project_id: str | None = None,
    task_type: str = "scene_generation",
    scene_id: str | None = None,
    character_ids: str | None = None,
    session: Session = Depends(get_session),
):
    """只读叠层预览:resolve_binding_layers 命中层 + 权重/预算分配 + 合并概要。

    character_ids 逗号分隔(onstage 多角色)。无命中层时 layers=[]、merged=null。
    """
    chars = [c.strip() for c in (character_ids or "").split(",") if c.strip()] or None
    data = InjectionService(session).describe_binding_layers(
        project_id,
        task_type,
        character_ids=chars,
        scene_id=scene_id,
    )
    return ok(data, req_id=_req_id(request))


# ---------------------------------------------------------------------------
# PR-10 — Metrics endpoint
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/metrics")
def get_style_reference_metrics(
    request: Request,
    window_hours: int = 168,
    session: Session = Depends(get_session),
):
    """PR-10 §13 — 4 个运营指标 + sample_counts。window_hours=0 = 全部历史。"""
    aggregator = MetricsAggregator(session)
    snapshot = aggregator.compute_all(window_hours=max(0, int(window_hours)))
    return ok({"metrics": snapshot}, req_id=_req_id(request))


@router.get(f"{PATH_PREFIX}/metrics/daily")
def get_style_reference_metrics_daily(
    request: Request,
    window_days: int = 14,
    session: Session = Depends(get_session),
):
    """PR-22 — injection 调用量每日趋势(零填充连续轴,window_days 钳 [1,90])。"""
    aggregator = MetricsAggregator(session)
    result = aggregator.daily_injection_counts(window_days=int(window_days))
    return ok(result, req_id=_req_id(request))
