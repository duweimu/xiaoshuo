from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, OperationLog, SystemConfigSnapshot, SystemSecret, utcnow
from novel_system.db.session import SessionLocal
from novel_system.runtime_defaults import DEFAULT_LLM_TIMEOUT_SECONDS
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import normalize
from novel_system.services.llm_client import (
    DEFAULT_PROVIDER_BASE_URLS,
    LLMClient,
    LLMConfigurationError,
    LLMRequest,
    ProviderRuntimeConfig,
    SUPPORTED_API_MODES,
    SUPPORTED_CREDENTIAL_MODES,
    SUPPORTED_PROVIDERS,
    parse_model_routing_config,
)
from novel_system.services.llm_accounting import (
    LLMCallContext,
    execute_accounted_completion_probe,
)
from novel_system.services.llm_providers import (
    adapter_registry,
    get_provider_preset,
    provider_catalog as adapter_provider_catalog,
    provider_preset_payloads,
)
from novel_system.services.llm_node_registry import (
    active_llm_node_ids,
    default_task_config_payload,
    get_role_slot_spec,
    llm_node_catalog,
    llm_node_statuses,
    reserved_llm_node_ids,
    role_slot_catalog,
    role_slot_node_ids,
)
from novel_system.services.prompt_builder import PromptConfigurationError, parse_prompt_templates


CONFIG_CATEGORIES = ("api", "models", "prompts", "allowlists", "hash_contract")
YAML_CONFIG_FILES = {
    "models": "models.yaml",
    "prompts": "prompts.yaml",
    "allowlists": "allowlists.yaml",
    "hash_contract": "hash_contract.yaml",
}
LLM_API_KEY_SECRET_ID = "llm_api_key"
LLM_PROVIDER_SECRET_PREFIX = "llm_provider"
LLM_NODE_STATUSES = llm_node_statuses()
# 探活调用向记账层申报的输出预算(详见 _probe_completion 内注释)
PROBE_ACCOUNTING_OUTPUT_BUDGET = 1024
# 「测试连接」的超时与长文本生成上限无关，探测必须有限且短。
PROVIDER_PROBE_TIMEOUT_SECONDS = 30.0
# task_routing 里唯一不是节点 id 的合法键:parse_model_routing_config 把它映射到
# style_draft,从不并入 node_routing。剪枝退役节点时必须放过它。
_TASK_ROUTING_ALIASES = frozenset({"stylize"})


def repo_config_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "config"


def _read_with_transient_retry(reader):
    from novel_system.services.config_snapshot_reader import read_with_transient_retry

    return read_with_transient_retry(reader, sleep=time.sleep)


def load_active_config_payload(category: str) -> dict[str, Any] | None:
    from novel_system.services.config_snapshot_reader import load_active_config_payload as read_payload

    return read_payload(category, session_factory=SessionLocal, sleep=time.sleep)


def load_active_config_yaml(category: str) -> str | None:
    from novel_system.services.config_snapshot_reader import load_active_config_yaml as read_yaml

    return read_yaml(category, session_factory=SessionLocal, sleep=time.sleep)


def apply_active_api_config(settings):
    payload = load_active_config_payload("api")
    api_key = load_secret_value(LLM_API_KEY_SECRET_ID)
    if not payload and not api_key:
        return settings

    llm_payload = _coerce_api_payload(payload or {})
    providers = _provider_payloads_from_llm(llm_payload)
    if providers:
        provider_id = str(llm_payload.get("default_provider_id") or next(iter(providers.keys())))
        provider_payload = providers.get(provider_id) or next(iter(providers.values()))
        provider_secret = load_secret_value(llm_provider_api_key_secret_id(provider_id))
        return replace(
            settings,
            llm_provider=provider_payload.get("provider_type", provider_payload.get("provider", settings.llm_provider)),
            llm_base_url=_normalize_provider_base_url(
                provider_payload.get("base_url", settings.llm_base_url),
                provider_payload.get("provider_type", provider_payload.get("provider", settings.llm_provider)),
            ),
            llm_enabled=llm_payload.get("enabled", provider_payload.get("enabled", settings.llm_enabled)),
            llm_timeout_seconds=llm_payload.get("timeout_seconds", settings.llm_timeout_seconds),
            llm_api_key=provider_secret or api_key or settings.llm_api_key,
        )
    return replace(
        settings,
        llm_provider=llm_payload.get("provider", settings.llm_provider),
        llm_base_url=_normalize_provider_base_url(
            llm_payload.get("base_url", settings.llm_base_url),
            llm_payload.get("provider", settings.llm_provider),
        ),
        llm_enabled=llm_payload.get("enabled", settings.llm_enabled),
        llm_timeout_seconds=llm_payload.get("timeout_seconds", settings.llm_timeout_seconds),
        llm_api_key=api_key or settings.llm_api_key,
    )


def load_secret_value(secret_id: str) -> str | None:
    def _read():
        with SessionLocal() as session:
            secret = session.get(SystemSecret, secret_id)
            if secret is None:
                return None
            try:
                return _decrypt_secret(secret.encrypted_value)
            except (DomainError, InvalidToken):
                return None

    return _read_with_transient_retry(_read)


def llm_provider_api_key_secret_id(provider_id: str) -> str:
    return f"{LLM_PROVIDER_SECRET_PREFIX}:{provider_id}:api_key"


def load_llm_provider_runtime_configs() -> dict[str, ProviderRuntimeConfig]:
    payload = load_active_config_payload("api") or {}
    llm_payload = _coerce_api_payload(payload) if payload else {}
    providers = _provider_payloads_from_llm(llm_payload)
    if not providers:
        from novel_system.core_runtime import load_core_runtime

        settings = load_core_runtime()
        provider_id = settings.llm_provider
        providers = {
            provider_id: {
                "provider_id": provider_id,
                "provider_type": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "credential_mode": "api_key" if settings.llm_api_key else "none",
                "enabled": settings.llm_enabled,
                "timeout_seconds": settings.llm_timeout_seconds,
            }
        }

    runtime_configs: dict[str, ProviderRuntimeConfig] = {}
    for provider_id, provider_payload in providers.items():
        credential_mode = str(provider_payload.get("credential_mode") or "api_key")
        if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
            continue
        secret_id = llm_provider_api_key_secret_id(provider_id)
        secret_value = load_secret_value(secret_id)
        legacy_api_key = load_secret_value(LLM_API_KEY_SECRET_ID) if provider_id in {"openai_compatible", "openai"} else None
        runtime_configs[provider_id] = ProviderRuntimeConfig(
            provider_id=provider_id,
            provider_type=str(provider_payload.get("provider_type") or provider_payload.get("provider") or provider_id),
            account_id=_optional_text(provider_payload.get("account_id")),
            base_url=_normalize_provider_base_url(
                provider_payload.get("base_url") or DEFAULT_PROVIDER_BASE_URLS.get(str(provider_payload.get("provider_type")), ""),
                provider_payload.get("provider_type") or provider_payload.get("provider") or provider_id,
            ),
            api_key=(secret_value or legacy_api_key) if credential_mode == "api_key" else None,
            credential_mode=credential_mode if credential_mode in SUPPORTED_CREDENTIAL_MODES else "api_key",
            api_mode=str(provider_payload.get("api_mode") or "chat"),  # type: ignore[arg-type]
            enabled=_bool_value(provider_payload.get("enabled", True)),
            models=tuple(str(item) for item in provider_payload.get("models", []) if isinstance(item, str)),
            provider_options=dict(provider_payload.get("provider_options") or {}),
        )
    return runtime_configs


def build_runtime_llm_client(
    *,
    settings: Any,
    provider_configs: dict[str, ProviderRuntimeConfig] | None = None,
    client_cls: type[LLMClient] | None = None,
) -> tuple[LLMClient | None, bool]:
    """运行时 LLM client 的统一工厂(fail-closed):LLM 未启用返回 (None, False)。

    settings 必传,由调用方 get_settings() 取得:settings 模块要读本模块的
    活动快照,本模块反向 import settings(哪怕函数内延迟)会构成依赖环,
    被 test_service_architecture 的全包环守卫拒绝。
    provider_configs 供调用方先用同一份配置做凭据检查(literary_eval),
    client_cls 保住调用方模块全局名 LLMClient 的测试打桩点。
    """
    if not settings.llm_enabled:
        return None, False
    if provider_configs is None:
        provider_configs = load_llm_provider_runtime_configs()
    factory = client_cls or LLMClient
    client = factory(
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
        provider_configs=provider_configs,
    )
    return client, True


class SystemConfigService:
    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        self.session = session
        self.auto_commit = auto_commit

    def _finish_mutation(self) -> None:
        """Commit direct calls, or only flush when an API wrapper owns commit."""

        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

    def overview(self) -> dict[str, Any]:
        categories = {}
        for category in CONFIG_CATEGORIES:
            categories[category] = self._category_payload(category)
        history = self.session.execute(
            select(SystemConfigSnapshot).order_by(
                SystemConfigSnapshot.created_at.desc(),
                SystemConfigSnapshot.category.asc(),
                SystemConfigSnapshot.version.desc(),
            )
        ).scalars().all()
        return {
            "runtime": {
                "admin_configured": bool(_admin_token()),
                "secret_configured": bool(_config_secret()),
                "supported_categories": list(CONFIG_CATEGORIES),
            },
            "categories": categories,
            "history": [_serialize_snapshot(snapshot) for snapshot in history],
        }

    def create_draft(
        self,
        *,
        category: str,
        yaml_raw: str,
        secrets: dict[str, str] | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        _ensure_category(category)
        parsed, validation = validate_config(category, yaml_raw)
        if not validation["ok"]:
            raise DomainError(
                "CONFIG_VALIDATION_FAILED",
                validation["message"],
                status_code=422,
                details=validation,
            )

        version = self._next_version(category)
        snapshot = SystemConfigSnapshot(
            snapshot_id=f"config_{category}_{uuid.uuid4().hex[:12]}",
            category=category,
            version=version,
            yaml_raw=yaml_raw,
            parsed_json=parsed,
            validation_json=validation,
            status="draft",
            active_flag=0,
            created_by=actor_ref,
        )
        self.session.add(snapshot)
        secret_payload = self._save_secrets(category=category, secrets=secrets or {}, actor_ref=actor_ref)
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "secrets": secret_payload,
        }

    def activate(self, snapshot_id: str, *, actor_ref: str) -> dict[str, Any]:
        snapshot = self.session.get(SystemConfigSnapshot, snapshot_id)
        if snapshot is None:
            raise DomainError("CONFIG_SNAPSHOT_NOT_FOUND", "config snapshot was not found", status_code=404)
        validation = dict(snapshot.validation_json or {})
        if validation.get("ok") is not True:
            raise DomainError(
                "CONFIG_VALIDATION_FAILED",
                validation.get("message") or "config snapshot is not valid",
                status_code=422,
                details=validation,
            )

        previous = _active_snapshot(self.session, snapshot.category)
        if previous is not None and previous.snapshot_id != snapshot.snapshot_id:
            previous.active_flag = 0
            previous.status = "superseded"

        snapshot.active_flag = 1
        snapshot.status = "active"
        snapshot.activated_at = utcnow()
        self.session.add(
            OperationLog(
                event_type="system_config_activated",
                object_type="system_config",
                object_ref=snapshot.snapshot_id,
                payload_json={
                    "actor_ref": actor_ref,
                    "category": snapshot.category,
                    "version": snapshot.version,
                    "previous_snapshot_id": previous.snapshot_id if previous is not None else None,
                    "validation": validation,
                },
            )
        )
        self._finish_mutation()
        return {"snapshot": _serialize_snapshot(snapshot)}

    def export_category(self, category: str) -> dict[str, Any]:
        _ensure_category(category)
        payload = self._category_payload(category)
        return {
            "category": category,
            "source": payload["source"],
            "yaml_raw": payload["yaml_raw"],
        }

    def test_provider(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        provider_payload = _coerce_api_payload(payload)
        provider = str(provider_payload.get("provider_type") or provider_payload.get("provider") or "openai_compatible")
        if provider not in SUPPORTED_PROVIDERS:
            raise DomainError("CONFIG_PROVIDER_UNSUPPORTED", f"unsupported provider {provider}", status_code=422)

        base_url = _normalize_provider_base_url(provider_payload.get("base_url"), provider)
        if not base_url:
            raise DomainError("CONFIG_PROVIDER_INVALID", "provider base_url is required", status_code=422)

        provider_id = _optional_text(provider_payload.get("provider_id"))
        default_credential_mode = "none" if provider_id and not provider_payload.get("api_key") else "api_key"
        credential_mode = str(provider_payload.get("credential_mode") or default_credential_mode)
        if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
            raise DomainError(
                "CONFIG_PROVIDER_INVALID",
                f"unsupported credential_mode {credential_mode}",
                status_code=422,
            )
        api_key = None
        if credential_mode == "api_key":
            provider_secret = load_secret_value(llm_provider_api_key_secret_id(provider_id)) if provider_id else None
            api_key = provider_payload.get("api_key") or provider_secret or load_secret_value(LLM_API_KEY_SECRET_ID)
        timeout_value = provider_payload.get("timeout_seconds")
        timeout_seconds = _probe_timeout_seconds(timeout_value, default_seconds=10.0)
        requested_model = _requested_probe_model(provider_payload)
        should_check_completion = bool(requested_model) and _bool_value(provider_payload.get("check_completion", False))
        trust_env = _httpx_trust_env_for_base_url(base_url)
        adapter = adapter_registry()[provider]
        provider_options = dict(provider_payload.get("provider_options") or {})
        list_request = adapter.list_models_request(base_url=base_url, api_key=api_key, provider_options=provider_options)
        started_at = time.perf_counter()
        if list_request is None:
            checks: dict[str, Any] = {
                "connection": {
                    "ok": None,
                    "status_code": None,
                    "message": "该服务不提供模型列表接口，跳过连接检查",
                }
            }
            if should_check_completion and requested_model:
                completion_result = _probe_completion(
                    session=self.session,
                    provider=provider,
                    base_url=base_url,
                    api_key=api_key,
                    provider_options=provider_options,
                    model=str(requested_model),
                    api_mode=str(provider_payload.get("api_mode") or "chat"),
                    timeout_seconds=timeout_seconds,
                )
                checks["completion"] = completion_result
                return {
                    "ok": completion_result["ok"] is True,
                    "status_code": completion_result.get("status_code"),
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "message": completion_result["message"]
                    if completion_result["ok"] is not True
                    else f"模型 {requested_model} 已通过最小生成探测（该服务不提供模型列表接口）",
                    "available_models": [],
                    "checks": checks,
                }
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "message": "该服务不提供模型列表接口；请填写模型名并勾选生成探测",
                "available_models": [],
                "checks": checks,
            }
        try:
            response = httpx.get(list_request.url, headers=list_request.headers, timeout=timeout_seconds, trust_env=trust_env)
        except httpx.RequestError as exc:
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "message": str(exc),
                "available_models": [],
                "checks": {
                    "connection": {
                        "ok": False,
                        "status_code": None,
                        "message": str(exc),
                    }
                },
            }

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        checks: dict[str, Any] = {
            "connection": {
                "ok": response.is_success,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message": "model list endpoint reached" if response.is_success else _provider_error_summary(response),
            }
        }
        if not response.is_success:
            if should_check_completion and requested_model:
                completion_result = _probe_completion(
                    session=self.session,
                    provider=provider,
                    base_url=base_url,
                    api_key=api_key,
                    provider_options=provider_options,
                    model=str(requested_model),
                    api_mode=str(provider_payload.get("api_mode") or "chat"),
                    timeout_seconds=timeout_seconds,
                )
                checks["completion"] = completion_result
                if completion_result["ok"] is True:
                    return {
                        "ok": True,
                        "status_code": completion_result.get("status_code") or response.status_code,
                        "latency_ms": int((time.perf_counter() - started_at) * 1000),
                        "message": (
                            f"模型 {requested_model} 已通过最小生成探测；"
                            f"/models 不可用：{_provider_error_summary(response)}"
                        ),
                        "available_models": [],
                        "checks": checks,
                    }
            return {
                "ok": False,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "message": _provider_error_summary(response),
                "available_models": [],
                "checks": checks,
            }

        model_ids = _normalize_provider_model_ids(adapter.normalize_listed_model_ids(_extract_model_ids(response)))
        if requested_model:
            model_ok = requested_model in model_ids
            checks["model"] = {
                "ok": model_ok,
                "requested_model": requested_model,
                "available_models": model_ids,
            }
            if not model_ok:
                available_hint = "、".join(model_ids[:5]) if model_ids else "未能从 /models 解析到模型列表"
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "message": f"模型 {requested_model} 未在服务返回的模型列表中出现。可用模型：{available_hint}",
                    "available_models": model_ids,
                    "checks": checks,
                }

        if should_check_completion:
            completion_result = _probe_completion(
                session=self.session,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                provider_options=provider_options,
                model=str(requested_model),
                api_mode=str(provider_payload.get("api_mode") or "chat"),
                timeout_seconds=timeout_seconds,
            )
            checks["completion"] = completion_result
            if completion_result["ok"] is not True:
                return {
                    "ok": False,
                    "status_code": completion_result.get("status_code") or response.status_code,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                    "message": completion_result["message"],
                    "available_models": model_ids,
                    "checks": checks,
                }

        message = (
            f"模型 {requested_model} 可用：连接、模型名、生成均通过"
            if requested_model and checks.get("completion", {}).get("ok") is True
            else (f"模型 {requested_model} 已在服务列表中找到" if requested_model else "provider probe succeeded")
        )
        return {
            "ok": True,
            "status_code": response.status_code,
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "message": message,
            "available_models": model_ids,
            "checks": checks,
        }

    def llm_overview(self) -> dict[str, Any]:
        api_payload = self._category_payload("api")
        models_payload = self._category_payload("models")
        llm_payload = _coerce_api_payload(dict(api_payload.get("parsed") or {}))
        providers = {
            provider_id: self._serialize_provider(provider_id, provider_payload)
            for provider_id, provider_payload in _provider_payloads_from_llm(llm_payload).items()
        }
        try:
            routing = parse_model_routing_config(models_payload.get("parsed") or {})
            node_routes = {
                node_id: _serialize_task_config(node_id, task_config)
                for node_id, task_config in routing.node_routing.items()
            }
        except LLMConfigurationError:
            node_routes = {}
        node_catalog = llm_node_catalog()
        # 存量 models 快照的 node_routing 只前滚不剪枝：节点从注册表退役后，
        # 老安装的快照仍带着它的路由。这类目录外条目没有 spec，不该渲染成
        # 无名路由行或计入就绪统计，单列为 stale_routes 供排查。
        stale_routes = sorted(node_id for node_id in node_routes if node_id not in node_catalog)
        for node_id in stale_routes:
            del node_routes[node_id]
        for node_id, spec in node_catalog.items():
            node_routes.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "status": spec["status"],
                    "configured": False,
                },
            )
            node_routes[node_id].update(
                {
                    "status": spec["status"],
                    "group": spec["group"],
                    "label": spec["label"],
                    "requires_llm": spec["requires_llm"],
                    "template_name": spec["template_name"],
                    "order": spec["order"],
                }
            )
        _annotate_node_route_readiness(node_routes=node_routes, providers=providers)
        missing_active_routes = [
            node_id
            for node_id in active_llm_node_ids()
            if node_id in node_routes and not bool(node_routes[node_id].get("configured"))
        ]
        blocked_routes = [
            {
                "node_id": route.get("node_id"),
                "group": route.get("group"),
                "provider_id": route.get("provider_id"),
                "model": route.get("model"),
                "reason": route.get("readiness_reason"),
            }
            for route in node_routes.values()
            if _route_requires_llm(route)
            and route.get("configured")
            and route.get("ready") is not True
        ]
        return {
            "provider_catalog": _provider_catalog(),
            "default_provider_id": llm_payload.get("default_provider_id") or next(iter(providers.keys()), None),
            # runtime 随 overview 带出,管理面前端无需再拉全量 /system-config(含全部历史快照)
            "runtime": {
                "admin_configured": bool(_admin_token()),
                "secret_configured": bool(_config_secret()),
            },
            "providers": providers,
            "node_catalog": node_catalog,
            "node_routes": node_routes,
            "missing_active_routes": missing_active_routes,
            "blocked_routes": blocked_routes,
            "stale_routes": stale_routes,
            "readiness": _llm_readiness_summary(providers=providers, node_routes=node_routes),
            "role_slots": _role_slot_overview(node_routes),
            "api_snapshot": api_payload.get("active_snapshot"),
            "models_snapshot": models_payload.get("active_snapshot"),
        }

    def llm_call_audit(self, *, limit: int = 1000) -> dict[str, Any]:
        node_catalog = llm_node_catalog()
        active_nodes = set(active_llm_node_ids())
        # 审计 P-15：聚合只需要标量列——不加载 request/response 全量 JSON 载荷
        rows = self.session.execute(
            select(
                LlmCall.llm_call_id,
                LlmCall.node_id,
                LlmCall.step,
                LlmCall.scene_id,
                LlmCall.chapter_id,
                LlmCall.provider,
                LlmCall.provider_id,
                LlmCall.model,
                LlmCall.error_code,
                LlmCall.created_at,
            )
            .order_by(LlmCall.created_at.desc(), LlmCall.llm_call_id.desc())
            .limit(max(1, min(int(limit or 1000), 5000)))
        ).all()
        by_node: dict[str, dict[str, Any]] = {
            node_id: {
                "node_id": node_id,
                "group": node_catalog.get(node_id, {}).get("group"),
                "requires_llm": True,
                "call_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "latest_provider": None,
                "latest_provider_id": None,
                "latest_model": None,
                "latest_error_code": None,
                "offline_deterministic_count": 0,
            }
            for node_id in active_nodes
        }
        offline_required_calls: list[dict[str, Any]] = []
        for row in rows:
            node_id = str(row.node_id or row.step or "")
            if not node_id:
                continue
            entry = by_node.setdefault(
                node_id,
                {
                    "node_id": node_id,
                    "group": node_catalog.get(node_id, {}).get("group"),
                    "requires_llm": node_id in active_nodes,
                    "call_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "latest_provider": None,
                    "latest_provider_id": None,
                    "latest_model": None,
                    "latest_error_code": None,
                    "offline_deterministic_count": 0,
                },
            )
            entry["call_count"] += 1
            if row.error_code:
                entry["failure_count"] += 1
            else:
                entry["success_count"] += 1
            if entry["latest_provider"] is None:
                entry["latest_provider"] = row.provider
                entry["latest_provider_id"] = row.provider_id
                entry["latest_model"] = row.model
                entry["latest_error_code"] = row.error_code
            if row.provider == "offline_deterministic":
                entry["offline_deterministic_count"] += 1
                if node_id in active_nodes:
                    offline_required_calls.append(
                        {
                            "llm_call_id": row.llm_call_id,
                            "node_id": node_id,
                            "step": row.step,
                            "scene_id": row.scene_id,
                            "chapter_id": row.chapter_id,
                            "model": row.model,
                            "created_at": _serialize_datetimeish(row.created_at),
                        }
                    )
        required_matrix = [by_node[node_id] for node_id in active_llm_node_ids()]
        return {
            "limit": limit,
            "required_node_matrix": required_matrix,
            "offline_deterministic_required_calls": offline_required_calls,
            "offline_deterministic_required_count": len(offline_required_calls),
            "nodes_without_calls": [item["node_id"] for item in required_matrix if item["call_count"] == 0],
        }

    def save_llm_provider(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        try:
            provider = _normalize_provider_payload(payload)
        except ValueError as exc:
            raise DomainError("CONFIG_PROVIDER_INVALID", str(exc), status_code=422) from exc
        provider_id = provider["provider_id"]
        api_key = _optional_text(payload.get("api_key"))
        llm_payload = self._current_api_llm_payload()
        providers = _provider_payloads_from_llm(llm_payload)
        providers[provider_id] = {key: value for key, value in provider.items() if key != "api_key"}
        llm_payload["providers"] = providers
        llm_payload["default_provider_id"] = llm_payload.get("default_provider_id") or provider_id
        llm_payload["enabled"] = True if provider["enabled"] else _bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        secret_status = self._secret_status(llm_provider_api_key_secret_id(provider_id))
        if provider["credential_mode"] == "none":
            existing_secret = self.session.get(SystemSecret, llm_provider_api_key_secret_id(provider_id))
            if existing_secret is not None:
                self.session.delete(existing_secret)
            secret_status = _none_secret_status()
        elif api_key:
            secret_status = self._save_secret_value(
                secret_id=llm_provider_api_key_secret_id(provider_id),
                raw_value=api_key,
                actor_ref=actor_ref,
                secret_type="api_key",
                metadata={
                    "provider_id": provider_id,
                    "provider_type": provider["provider_type"],
                    "account_id": provider.get("account_id"),
                },
            )
        provider_view = self._serialize_provider(provider_id, provider)
        provider_view["secret"] = secret_status
        self._finish_mutation()
        return {
            "provider": provider_view,
            "snapshot": _serialize_snapshot(snapshot),
        }

    def set_default_llm_provider(self, *, provider_id: str, actor_ref: str) -> dict[str, Any]:
        provider_id = _required_text(provider_id, "provider_id")
        llm_payload = self._current_api_llm_payload()
        providers = _provider_payloads_from_llm(llm_payload)
        if provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        llm_payload["providers"] = providers
        llm_payload["default_provider_id"] = provider_id
        llm_payload["enabled"] = _bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "default_provider_id": provider_id,
            "provider": self._serialize_provider(provider_id, providers[provider_id]),
            "snapshot": _serialize_snapshot(snapshot),
        }

    def delete_llm_provider(self, *, provider_id: str, actor_ref: str) -> dict[str, Any]:
        provider_id = _required_text(provider_id, "provider_id")
        llm_payload = self._current_api_llm_payload()
        providers = _provider_payloads_from_llm(llm_payload)
        if provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        providers.pop(provider_id)
        llm_payload["providers"] = providers
        if llm_payload.get("default_provider_id") == provider_id:
            next_default = next(iter(providers.keys()), None)
            if next_default is None:
                llm_payload.pop("default_provider_id", None)
            else:
                llm_payload["default_provider_id"] = next_default
        llm_payload["enabled"] = _bool_value(llm_payload.get("enabled", True))
        llm_payload.setdefault("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS)
        snapshot = self._store_config_snapshot(
            category="api",
            parsed={"llm": llm_payload},
            validation={"ok": True, "message": "api config is valid"},
            status="active",
            active=True,
            actor_ref=actor_ref,
        )
        secret = self.session.get(SystemSecret, llm_provider_api_key_secret_id(provider_id))
        if secret is not None:
            self.session.delete(secret)
        # 节点路由不随删而清:仍指向该服务的路由会被就绪检查标为 blocked,
        # 这里带回清单,前端可提示「一键补齐路由」切到默认服务。
        # 退役节点(不在注册表)的残留路由无需重绑,不列入清单——它们由
        # sync-missing / role-routes 写路径剪掉。
        current_models = dict(self._category_payload("models").get("parsed") or {})
        node_catalog = llm_node_catalog()
        orphaned_route_node_ids = sorted(
            node_id
            for node_id, route in dict(current_models.get("node_routing") or {}).items()
            if node_id in node_catalog
            and isinstance(route, dict)
            and str(route.get("provider_id") or "") == provider_id
        )
        self.session.add(
            OperationLog(
                event_type="system_config_llm_provider_deleted",
                object_type="system_config",
                object_ref=snapshot.snapshot_id,
                payload_json={
                    "actor_ref": actor_ref,
                    "provider_id": provider_id,
                    "default_provider_id": llm_payload.get("default_provider_id"),
                    "orphaned_route_node_ids": orphaned_route_node_ids,
                },
            )
        )
        self._finish_mutation()
        return {
            "deleted_provider_id": provider_id,
            "default_provider_id": llm_payload.get("default_provider_id"),
            "orphaned_route_node_ids": orphaned_route_node_ids,
            "snapshot": _serialize_snapshot(snapshot),
        }

    def save_llm_node_routes(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        current_models = dict(self._category_payload("models").get("parsed") or {})
        config_payload = {
            "model_profiles": dict(payload.get("model_profiles") or {}),
            "task_routing": dict(payload.get("task_routing") or {}),
            "node_routing": dict(payload.get("node_routing") or {}),
            "retry_budget": dict(payload.get("retry_budget") or {}),
            "job_runtime": dict(payload.get("job_runtime") or {}),
            # 节点高级编辑表单不提交角色槽元数据；保存时必须沿用当前绑定，
            # 否则一次无关的温度/预算调整会让设置页静默丢失三个角色分工。
            "role_assignments": dict(current_models.get("role_assignments") or {}),
        }
        routing_config = _parse_route_config_or_raise(config_payload)
        # 整表保存是原样写入的高级路径:退役节点的条目照存(overview 仍在
        # stale_routes 列出),但激活校验对它们惰性——老安装带着已删服务的
        # 退役路由不能把整个保存卡成 422。真正的剪枝在 sync-missing / role-routes。
        stale_routes = _retired_route_ids(routing_config.node_routing)
        if _bool_value(payload.get("activate", False)):
            _validate_activating_node_route_bindings(
                node_routing=routing_config.node_routing,
                providers=self.llm_overview()["providers"],
            )
        snapshot = self._store_config_snapshot(
            category="models",
            parsed=config_payload,
            validation={"ok": True, "message": "models config is valid"},
            status="active" if _bool_value(payload.get("activate", False)) else "draft",
            active=_bool_value(payload.get("activate", False)),
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {"snapshot": _serialize_snapshot(snapshot), "stale_routes": stale_routes}

    def sync_missing_llm_node_routes(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        overview = self.llm_overview()
        providers = overview["providers"]
        llm_payload = self._current_api_llm_payload()
        provider_id = _optional_text(payload.get("provider_id")) or _optional_text(llm_payload.get("default_provider_id"))
        if provider_id is None:
            provider_id = next(iter(providers.keys()), None)
        if provider_id is None or provider_id not in providers:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", "configure a provider before syncing node routes", status_code=422)

        provider = providers[provider_id]
        if not _provider_view_ready(provider):
            raise DomainError(
                "CONFIG_ROUTE_PROVIDER_NOT_READY",
                f"provider {provider_id} is not ready; enable it and configure credentials first",
                status_code=422,
            )
        models = _normalize_provider_model_ids(provider.get("models") or [])
        model = _optional_text(payload.get("model")) or (models[0] if models else None)
        if not model:
            raise DomainError(
                "CONFIG_ROUTE_MODEL_MISSING",
                f"provider {provider_id} has no models; probe or fill the model list first",
                status_code=422,
            )
        if models and model not in models:
            raise DomainError(
                "CONFIG_ROUTE_MODEL_MISSING",
                f"model {model} is not listed by provider {provider_id}",
                status_code=422,
            )

        current_models = dict(self._category_payload("models").get("parsed") or {})
        node_routing = dict(current_models.get("node_routing") or {})
        task_routing = dict(current_models.get("task_routing") or {})
        config_payload = {
            "model_profiles": dict(current_models.get("model_profiles") or {}),
            "task_routing": task_routing,
            "node_routing": node_routing,
            "retry_budget": dict(current_models.get("retry_budget") or {}),
            "job_runtime": dict(current_models.get("job_runtime") or {}),
        }
        # 老安装的快照里可能还带着已退役节点的路由(常指向早已删除的服务)。
        # 这里只补齐目录内节点,退役条目不会被重绑,却会被激活校验拦成 422,
        # 让「一键补齐路由」永远失败——先剪掉,并在响应里告知剪了什么。
        pruned_stale_routes = _prune_retired_routes(config_payload)
        synced_node_ids: list[str] = []
        provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
        account_id = _optional_text(provider.get("account_id"))
        api_mode = _provider_route_api_mode(provider_id, provider)
        credential_mode = _optional_text(provider.get("credential_mode"))
        for node_id in active_llm_node_ids():
            route = overview["node_routes"].get(node_id) or {}
            needs_sync = (
                not bool(route.get("configured"))
                or not _optional_text(route.get("provider_id"))
                or not _optional_text(route.get("model"))
                or route.get("ready") is not True
            )
            if not needs_sync:
                continue
            node_routing[node_id] = default_task_config_payload(
                node_id,
                provider_id=provider_id,
                provider=provider_type,
                model=model,
                account_id=account_id,
                api_mode=api_mode,
                credential_mode=credential_mode,
            )
            synced_node_ids.append(node_id)

        routing_config = _parse_route_config_or_raise(config_payload)
        activate = _bool_value(payload.get("activate", False))
        if activate:
            _validate_activating_node_route_bindings(
                node_routing=routing_config.node_routing,
                providers=providers,
            )
        snapshot = self._store_config_snapshot(
            category="models",
            parsed=config_payload,
            validation={"ok": True, "message": "models config is valid"},
            status="active" if activate else "draft",
            active=activate,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "synced_node_ids": synced_node_ids,
            "pruned_stale_routes": pruned_stale_routes,
            "overview": self.llm_overview(),
        }

    def probe_llm_provider(self, *, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        llm_payload = self._current_api_llm_payload()
        provider = self.llm_overview()["providers"].get(provider_id)
        if provider is None:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)
        probe_payload = dict(provider)
        probe_payload["timeout_seconds"] = _probe_timeout_seconds(
            llm_payload.get("timeout_seconds"), default_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS
        )
        probe_payload.update(payload or {})
        return self.test_provider(payload=probe_payload)

    def llm_provider_presets(self) -> dict[str, Any]:
        return {
            "presets": provider_preset_payloads(),
            "provider_catalog": _provider_catalog(),
        }

    def list_llm_provider_models(self, *, provider_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        llm_payload = self._current_api_llm_payload()
        provider = self.llm_overview()["providers"].get(provider_id)
        if provider is None:
            raise DomainError("CONFIG_PROVIDER_NOT_FOUND", f"provider {provider_id} was not found", status_code=404)

        provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
        adapter = adapter_registry().get(provider_type)
        if adapter is None:
            raise DomainError("CONFIG_PROVIDER_UNSUPPORTED", f"unsupported provider {provider_type}", status_code=422)
        base_url = _normalize_provider_base_url(provider.get("base_url"), provider_type)
        credential_mode = str(provider.get("credential_mode") or "api_key")
        api_key = None
        if credential_mode == "api_key":
            api_key = load_secret_value(llm_provider_api_key_secret_id(provider_id)) or load_secret_value(LLM_API_KEY_SECRET_ID)
        timeout_value = payload.get("timeout_seconds") or llm_payload.get("timeout_seconds")
        timeout_seconds = _probe_timeout_seconds(timeout_value, default_seconds=10.0)
        provider_options = dict(provider.get("provider_options") or {})

        configured_models = _normalize_provider_model_ids(provider.get("models") or [])
        preset = get_provider_preset(provider_type)
        preset_models = list(preset.common_models) if preset is not None else []
        fallback_models = list(dict.fromkeys([*configured_models, *preset_models]))

        def _fallback(message: str, *, status_code: int | None = None) -> dict[str, Any]:
            return {
                "provider_id": provider_id,
                "provider_type": provider_type,
                "ok": bool(fallback_models),
                "source": "preset",
                "available_models": fallback_models,
                "status_code": status_code,
                "message": message,
            }

        list_request = adapter.list_models_request(base_url=base_url, api_key=api_key, provider_options=provider_options)
        if list_request is None:
            return _fallback("该服务不提供模型列表接口，已返回预设/已配置模型")

        started_at = time.perf_counter()
        try:
            response = httpx.get(
                list_request.url,
                headers=list_request.headers,
                timeout=timeout_seconds,
                trust_env=_httpx_trust_env_for_base_url(base_url),
            )
        except httpx.RequestError as exc:
            return _fallback(f"模型列表拉取失败：{exc}")
        if not response.is_success:
            return _fallback(f"模型列表拉取失败：{_provider_error_summary(response)}", status_code=response.status_code)

        model_ids = _normalize_provider_model_ids(adapter.normalize_listed_model_ids(_extract_model_ids(response)))
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "ok": True,
            "source": "live",
            "available_models": model_ids,
            "status_code": response.status_code,
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
            "message": f"已从服务实时拉取 {len(model_ids)} 个模型",
        }

    def save_llm_role_routes(self, *, payload: dict[str, Any], actor_ref: str) -> dict[str, Any]:
        assignments = payload.get("assignments")
        if not isinstance(assignments, dict) or not assignments:
            raise DomainError("CONFIG_ROLE_ASSIGNMENTS_REQUIRED", "assignments must be a non-empty mapping", status_code=422)

        overview = self.llm_overview()
        providers = overview["providers"]

        current_models = dict(self._category_payload("models").get("parsed") or {})
        node_routing = dict(current_models.get("node_routing") or {})
        task_routing = dict(current_models.get("task_routing") or {})
        role_assignments = dict(current_models.get("role_assignments") or {})
        # 同 sync-missing:退役节点的残留路由不随分工前滚,剪掉并回报。
        pruned_stale_routes = _prune_retired_routes({"node_routing": node_routing, "task_routing": task_routing})

        applied: dict[str, dict[str, Any]] = {}
        for slot_id, binding in assignments.items():
            slot = get_role_slot_spec(str(slot_id))
            if slot is None:
                raise DomainError("CONFIG_ROLE_SLOT_UNKNOWN", f"unknown role slot {slot_id}", status_code=422)
            if not isinstance(binding, dict):
                raise DomainError("CONFIG_ROLE_ASSIGNMENT_INVALID", f"assignment for {slot_id} must be a mapping", status_code=422)
            provider_id = _optional_text(binding.get("provider_id"))
            if not provider_id or provider_id not in providers:
                raise DomainError(
                    "CONFIG_PROVIDER_NOT_FOUND",
                    f"role slot {slot_id} references unknown provider {provider_id or '(missing)'}",
                    status_code=404 if provider_id else 422,
                )
            provider = providers[provider_id]
            if not _provider_view_ready(provider):
                raise DomainError(
                    "CONFIG_ROUTE_PROVIDER_NOT_READY",
                    f"provider {provider_id} is not ready; enable it and configure credentials first",
                    status_code=422,
                )
            models = _normalize_provider_model_ids(provider.get("models") or [])
            model = _optional_text(binding.get("model")) or (models[0] if models else None)
            if not model:
                raise DomainError(
                    "CONFIG_ROUTE_MODEL_MISSING",
                    f"provider {provider_id} has no models; probe or fill the model list first",
                    status_code=422,
                )
            if models and model not in models:
                raise DomainError(
                    "CONFIG_ROUTE_MODEL_MISSING",
                    f"model {model} is not listed by provider {provider_id}",
                    status_code=422,
                )

            provider_type = str(provider.get("provider_type") or provider.get("provider") or "openai_compatible")
            account_id = _optional_text(provider.get("account_id"))
            api_mode = _provider_route_api_mode(provider_id, provider)
            credential_mode = _optional_text(provider.get("credential_mode"))
            slot_node_ids = role_slot_node_ids(slot.slot_id)
            for node_id in slot_node_ids:
                node_routing[node_id] = default_task_config_payload(
                    node_id,
                    provider_id=provider_id,
                    provider=provider_type,
                    model=model,
                    account_id=account_id,
                    api_mode=api_mode,
                    credential_mode=credential_mode,
                )
            role_assignments[slot.slot_id] = {"provider_id": provider_id, "model": model}
            applied[slot.slot_id] = {
                "provider_id": provider_id,
                "model": model,
                "node_ids": slot_node_ids,
            }

        config_payload = {
            "model_profiles": dict(current_models.get("model_profiles") or {}),
            "task_routing": task_routing,
            "node_routing": node_routing,
            "retry_budget": dict(current_models.get("retry_budget") or {}),
            "job_runtime": dict(current_models.get("job_runtime") or {}),
            "role_assignments": role_assignments,
        }
        routing_config = _parse_route_config_or_raise(config_payload)
        activate = _bool_value(payload.get("activate", True))
        if activate:
            # 只校验本次触达的槽内节点:允许「先把写作主力分出去」的渐进配置,
            # 未触达节点保持原状(与今日的默认 repo 路由同等待遇)。
            touched_node_ids = {node_id for entry in applied.values() for node_id in entry["node_ids"]}
            _validate_activating_node_route_bindings(
                node_routing={
                    node_id: task_config
                    for node_id, task_config in routing_config.node_routing.items()
                    if node_id in touched_node_ids
                },
                providers=providers,
            )
        snapshot = self._store_config_snapshot(
            category="models",
            parsed=config_payload,
            validation={"ok": True, "message": "models config is valid"},
            status="active" if activate else "draft",
            active=activate,
            actor_ref=actor_ref,
        )
        self._finish_mutation()
        return {
            "snapshot": _serialize_snapshot(snapshot),
            "applied": applied,
            "pruned_stale_routes": pruned_stale_routes,
            "overview": self.llm_overview(),
        }

    def _category_payload(self, category: str) -> dict[str, Any]:
        active = _active_snapshot(self.session, category)
        if active is not None:
            payload = {
                "category": category,
                "source": "database_active",
                "yaml_raw": active.yaml_raw,
                "parsed": active.parsed_json,
                "validation": active.validation_json,
                "active_snapshot": _serialize_snapshot(active),
            }
        else:
            yaml_raw, parsed, validation, source = default_config_payload(category)
            payload = {
                "category": category,
                "source": source,
                "yaml_raw": yaml_raw,
                "parsed": parsed,
                "validation": validation,
                "active_snapshot": None,
            }
        if category == "api":
            payload["secrets"] = {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)}
        return payload

    def _current_api_llm_payload(self) -> dict[str, Any]:
        active = _active_snapshot(self.session, "api")
        if active is not None:
            return _coerce_api_payload(dict(active.parsed_json or {}))
        _, parsed, _, _ = default_config_payload("api")
        return _coerce_api_payload(parsed)

    def _store_config_snapshot(
        self,
        *,
        category: str,
        parsed: dict[str, Any],
        validation: dict[str, Any],
        status: str,
        active: bool,
        actor_ref: str,
    ) -> SystemConfigSnapshot:
        if active:
            previous = _active_snapshot(self.session, category)
            if previous is not None:
                previous.active_flag = 0
                previous.status = "superseded"
        yaml_raw = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
        snapshot = SystemConfigSnapshot(
            snapshot_id=f"config_{category}_{uuid.uuid4().hex[:12]}",
            category=category,
            version=self._next_version(category),
            yaml_raw=yaml_raw,
            parsed_json=parsed,
            validation_json=validation,
            status=status,
            active_flag=1 if active else 0,
            activated_at=utcnow() if active else None,
            created_by=actor_ref,
        )
        self.session.add(snapshot)
        return snapshot

    def _next_version(self, category: str) -> int:
        current = self.session.execute(
            select(func.max(SystemConfigSnapshot.version)).where(SystemConfigSnapshot.category == category)
        ).scalar_one_or_none()
        return int(current or 0) + 1

    def _save_secrets(self, *, category: str, secrets: dict[str, str], actor_ref: str) -> dict[str, Any]:
        if category != "api" or LLM_API_KEY_SECRET_ID not in secrets:
            return {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)} if category == "api" else {}
        raw_value = str(secrets.get(LLM_API_KEY_SECRET_ID) or "").strip()
        if not raw_value:
            return {LLM_API_KEY_SECRET_ID: self._secret_status(LLM_API_KEY_SECRET_ID)}
        status = self._save_secret_value(
            secret_id=LLM_API_KEY_SECRET_ID,
            raw_value=raw_value,
            actor_ref=actor_ref,
            secret_type="api_key",
            metadata={"provider_id": "legacy", "provider_type": "openai_compatible"},
        )
        return {LLM_API_KEY_SECRET_ID: status}

    def _save_secret_value(
        self,
        *,
        secret_id: str,
        raw_value: str,
        actor_ref: str,
        secret_type: str,
        metadata: dict[str, Any],
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        secret = self.session.get(SystemSecret, secret_id)
        encrypted = _encrypt_secret(raw_value)
        if secret is None:
            secret = SystemSecret(
                secret_id=secret_id,
                encrypted_value=encrypted,
                value_hint=_mask_secret(raw_value),
                secret_type=secret_type,
                metadata_json=metadata,
                expires_at=expires_at,
                updated_by=actor_ref,
            )
        else:
            secret.encrypted_value = encrypted
            secret.value_hint = _mask_secret(raw_value)
            secret.secret_type = secret_type
            secret.metadata_json = metadata
            secret.expires_at = expires_at
            secret.updated_by = actor_ref
        self.session.add(secret)
        return self._secret_status(secret_id, secret=secret)

    def _secret_status(self, secret_id: str, *, secret: SystemSecret | None = None) -> dict[str, Any]:
        item = secret or self.session.get(SystemSecret, secret_id)
        decryptable = False
        if item is not None and item.encrypted_value:
            # BUG-001: secret 记录"存在"不等于"可解密"。config.secret 轮换后旧密文
            # InvalidToken,运行期 load_secret_value 静默返 None → api_key 为空,但
            # 就绪侧若只看 configured 就会假阳性。这里真正试解密,把三态拆开。
            try:
                decrypted = _decrypt_secret(item.encrypted_value)
                decryptable = bool(decrypted and decrypted.strip())
            except (DomainError, InvalidToken):
                decryptable = False
        return {
            "configured": item is not None,
            "decryptable": decryptable,
            "hint": item.value_hint if item is not None else None,
            "secret_type": item.secret_type if item is not None else None,
            "metadata": item.metadata_json if item is not None else {},
            "expires_at": item.expires_at if item is not None else None,
            "updated_at": item.updated_at if item is not None else None,
        }

    def _serialize_provider(self, provider_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        credential_mode = str(payload.get("credential_mode") or "api_key")
        secret_id = llm_provider_api_key_secret_id(provider_id)
        secret_status = _none_secret_status() if credential_mode == "none" else self._secret_status(secret_id)
        provider_type = payload.get("provider_type") or payload.get("provider")
        return {
            "provider_id": provider_id,
            "provider_type": provider_type,
            "account_id": payload.get("account_id"),
            "base_url": _normalize_provider_base_url(payload.get("base_url"), provider_type),
            "enabled": _bool_value(payload.get("enabled", True)),
            "credential_mode": credential_mode,
            "api_mode": payload.get("api_mode", "chat"),
            "models": _normalize_provider_model_ids(payload.get("models") or []),
            "provider_options": dict(payload.get("provider_options") or {}),
            "secret": secret_status,
        }


def validate_config(category: str, yaml_raw: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        parsed = _parse_yaml_mapping(yaml_raw)
        if category == "api":
            normalized_api = {"llm": _validate_api_config(parsed)}
            return normalized_api, {"ok": True, "message": "api config is valid"}
        if category == "models":
            parse_model_routing_config(parsed)
            return parsed, {"ok": True, "message": "models config is valid"}
        if category == "prompts":
            parse_prompt_templates(parsed)
            return parsed, {"ok": True, "message": "prompts config is valid"}
        if category in {"allowlists", "hash_contract"}:
            return parsed, {"ok": True, "message": f"{category} config is valid"}
    except (yaml.YAMLError, ValueError, LLMConfigurationError, PromptConfigurationError) as exc:
        return {}, {"ok": False, "message": str(exc)}

    raise DomainError("CONFIG_CATEGORY_UNSUPPORTED", f"unsupported config category {category}", status_code=404)


def default_config_payload(category: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    _ensure_category(category)
    if category == "api":
        from novel_system.core_runtime import load_core_runtime

        settings = load_core_runtime()
        parsed = {
            "llm": {
                "provider": settings.llm_provider,
                "base_url": settings.llm_base_url,
                "enabled": settings.llm_enabled,
                "timeout_seconds": settings.llm_timeout_seconds,
            }
        }
        yaml_raw = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
        return yaml_raw, parsed, {"ok": True, "message": "api config is valid"}, "env_default"

    path = repo_config_dir() / YAML_CONFIG_FILES[category]
    yaml_raw = path.read_text(encoding="utf-8")
    parsed, validation = validate_config(category, yaml_raw)
    return yaml_raw, parsed, validation, "repo_default"


def require_admin_token(header_value: str | None, *, client_host: str | None = None) -> None:
    token = _admin_token()
    if token:
        # 常数时间比较，避免逐字符短路的时序侧信道（审计 P-16）
        if header_value is not None and hmac.compare_digest(
            header_value.encode("utf-8"), token.encode("utf-8")
        ):
            return
        raise DomainError("ADMIN_TOKEN_REQUIRED", "valid X-Admin-Token is required", status_code=403)
    if _is_loopback_client(client_host):
        return
    raise DomainError(
        "ADMIN_TOKEN_REQUIRED",
        "valid X-Admin-Token is required; local setup mode only accepts loopback requests",
        status_code=403,
    )


def _is_loopback_client(client_host: str | None) -> bool:
    return str(client_host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def _active_snapshot(session: Session, category: str) -> SystemConfigSnapshot | None:
    return session.execute(
        select(SystemConfigSnapshot)
        .where(SystemConfigSnapshot.category == category, SystemConfigSnapshot.active_flag == 1)
        .order_by(SystemConfigSnapshot.version.desc(), SystemConfigSnapshot.created_at.desc())
    ).scalars().first()


def _serialize_snapshot(snapshot: SystemConfigSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "category": snapshot.category,
        "version": snapshot.version,
        "yaml_raw": snapshot.yaml_raw,
        "parsed": snapshot.parsed_json,
        "validation": snapshot.validation_json,
        "status": snapshot.status,
        "active": bool(snapshot.active_flag),
        "created_by": snapshot.created_by,
        "created_at": snapshot.created_at,
        "activated_at": snapshot.activated_at,
    }


def _serialize_datetimeish(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _ensure_category(category: str) -> None:
    if category not in CONFIG_CATEGORIES:
        raise DomainError("CONFIG_CATEGORY_UNSUPPORTED", f"unsupported config category {category}", status_code=404)


def _parse_yaml_mapping(yaml_raw: str) -> dict[str, Any]:
    payload = yaml.safe_load(yaml_raw) if yaml_raw.strip() else {}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("config YAML must decode to a mapping")
    normalized = normalize(payload)
    if not isinstance(normalized, dict):
        raise ValueError("config YAML must normalize to a mapping")
    return normalized


def _api_timeout_seconds(llm: dict[str, Any]) -> float:
    """解析 llm.timeout_seconds：缺省使用安全上限，显式 0 表示不限时。

    15 分钟不会重引入历史上的 30 秒误杀，同时避免静默上游永久占用 worker。
    负数仍然拒绝：那是笔误，不是“不限”的写法。
    """
    timeout_seconds = _float_value(
        llm.get("timeout_seconds", DEFAULT_LLM_TIMEOUT_SECONDS),
        "llm.timeout_seconds",
    )
    if timeout_seconds < 0:
        raise ValueError("llm.timeout_seconds must not be negative (0 = no ceiling)")
    return timeout_seconds


def _probe_timeout_seconds(
    value: Any,
    *,
    default_seconds: float,
    maximum_seconds: float = PROVIDER_PROBE_TIMEOUT_SECONDS,
) -> float:
    """连通性探测始终有限:0(生成不限时)对"测试连接"没有意义,只会让按钮空转。

    探测问的是"这个地址通不通",不是"这个模型写得慢不慢"。
    """
    if value is None:
        return min(default_seconds, maximum_seconds)
    timeout_seconds = _float_value(value, "timeout_seconds")
    if timeout_seconds <= 0:
        return min(default_seconds, maximum_seconds)
    return min(timeout_seconds, maximum_seconds)


def _validate_api_config(parsed: dict[str, Any]) -> dict[str, Any]:
    llm = _coerce_api_payload(parsed)
    providers = _provider_payloads_from_llm(llm)
    if providers:
        normalized_providers = {
            provider_id: _normalize_provider_payload({"provider_id": provider_id, **provider_payload})
            for provider_id, provider_payload in providers.items()
        }
        timeout_seconds = _api_timeout_seconds(llm)
        return {
            "enabled": _bool_value(llm.get("enabled", True)),
            "timeout_seconds": timeout_seconds,
            "default_provider_id": llm.get("default_provider_id") or next(iter(normalized_providers.keys())),
            "providers": normalized_providers,
        }

    provider = str(llm.get("provider") or "openai_compatible")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider {provider}")

    base_url = _normalize_provider_base_url(llm.get("base_url"), provider)
    if not base_url:
        raise ValueError("llm.base_url is required")

    timeout_seconds = _api_timeout_seconds(llm)

    return {
        "provider": provider,
        "base_url": base_url,
        "enabled": _bool_value(llm.get("enabled", False)),
        "timeout_seconds": timeout_seconds,
    }


def _provider_payloads_from_llm(llm: dict[str, Any]) -> dict[str, dict[str, Any]]:
    providers = llm.get("providers")
    if isinstance(providers, dict):
        return {
            str(provider_id): dict(provider_payload)
            for provider_id, provider_payload in providers.items()
            if isinstance(provider_payload, dict)
        }
    return {}


def _normalize_provider_base_url(value: Any, provider: Any | None = None) -> str:
    base_url = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/responses", "/models"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)].rstrip("/")
            break
    provider_name = str(provider or "").strip()
    adapter = adapter_registry().get(provider_name)
    if adapter is not None and adapter.appends_v1_to_bare_host:
        try:
            parts = urlsplit(base_url)
        except ValueError:
            parts = None
        if parts is not None and parts.scheme and parts.netloc and parts.path in {"", "/"}:
            base_url = f"{base_url}/v1"
    return base_url


def _httpx_trust_env_for_base_url(base_url: str) -> bool:
    try:
        hostname = urlsplit(base_url).hostname
    except ValueError:
        return True
    if not hostname:
        return True
    if hostname.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return True


def _normalize_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    provider_id = _required_text(payload.get("provider_id"), "provider_id")
    provider_type = str(payload.get("provider_type") or payload.get("provider") or "").strip()
    if provider_type not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider {provider_type}")
    credential_mode = str(payload.get("credential_mode") or "api_key")
    if credential_mode not in SUPPORTED_CREDENTIAL_MODES:
        raise ValueError(f"unsupported credential_mode {credential_mode}")
    base_url = _normalize_provider_base_url(payload.get("base_url") or DEFAULT_PROVIDER_BASE_URLS.get(provider_type), provider_type)
    if not base_url:
        raise ValueError("provider base_url is required")
    models = payload.get("models") if isinstance(payload.get("models"), list) else []
    provider_options = payload.get("provider_options") if isinstance(payload.get("provider_options"), dict) else {}
    # api_mode 决定节点路由的端点(chat/responses);写错的值会在之后的
    # role-routes / sync-missing 解析路由时才炸成 LLMConfigurationError,这里就拦住。
    api_mode = str(payload.get("api_mode") or adapter_registry()[provider_type].default_api_mode)
    if api_mode not in SUPPORTED_API_MODES:
        raise ValueError(
            f"provider {provider_id} has unsupported api_mode {api_mode}; "
            f"expected one of {', '.join(sorted(SUPPORTED_API_MODES))}"
        )
    return {
        "provider_id": provider_id,
        "provider_type": provider_type,
        "account_id": _optional_text(payload.get("account_id")),
        "base_url": base_url,
        "enabled": _bool_value(payload.get("enabled", True)),
        "credential_mode": credential_mode,
        "api_mode": api_mode,
        "models": _normalize_provider_model_ids(models),
        "provider_options": dict(provider_options),
    }


def _provider_catalog() -> dict[str, dict[str, Any]]:
    return adapter_provider_catalog()


def _none_secret_status() -> dict[str, Any]:
    return {
        "configured": False,
        "decryptable": False,
        "hint": None,
        "secret_type": "none",
        "metadata": {},
        "expires_at": None,
        "updated_at": None,
    }


def _serialize_task_config(node_id: str, task_config) -> dict[str, Any]:
    spec = llm_node_catalog().get(node_id, {})
    payload = {
        "node_id": node_id,
        "status": LLM_NODE_STATUSES.get(node_id, "active"),
        "configured": True,
        "provider": task_config.provider,
        "provider_id": task_config.provider_id,
        "account_id": task_config.account_id,
        "model": task_config.model,
        "temperature": task_config.temperature,
        "max_output_tokens": task_config.max_output_tokens,
        "response_format": task_config.response_format,
        "reasoning_level": task_config.reasoning_level,
        "api_mode": task_config.api_mode,
        "credential_mode": task_config.credential_mode,
        "provider_options": task_config.provider_options,
    }
    if spec:
        payload.update(
            {
                "status": spec["status"],
                "group": spec["group"],
                "label": spec["label"],
                "requires_llm": spec["requires_llm"],
                "template_name": spec["template_name"],
                "order": spec["order"],
            }
        )
    else:
        payload.setdefault("requires_llm", True)
    return payload


def _provider_view_ready(provider: dict[str, Any]) -> bool:
    if provider.get("enabled") is False:
        return False
    credential_mode = str(provider.get("credential_mode") or "api_key")
    if credential_mode == "none":
        return True
    secret = provider.get("secret") if isinstance(provider.get("secret"), dict) else {}
    # BUG-001: 必须"可解密"才算就绪——仅"存在"会在 config.secret 轮换后假阳性。
    return secret.get("decryptable") is True


def _route_readiness(route: dict[str, Any], providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = str(route.get("status") or "active")
    provider_id = _optional_text(route.get("provider_id"))
    model = _optional_text(route.get("model"))
    configured = bool(route.get("configured") or provider_id or model)
    if status == "reserved" or route.get("requires_llm") is False:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": False,
            "model_missing": False,
            "readiness_reason": "reserved",
        }
    if not configured:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": False,
            "model_missing": False,
            "readiness_reason": "not_configured",
        }

    if not provider_id:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": True,
            "model_missing": False,
            "readiness_reason": "provider_id_missing",
        }
    provider = providers.get(provider_id)
    if provider is None:
        return {
            "ready": False,
            "provider_ready": False,
            "provider_missing": True,
            "model_missing": False,
            "readiness_reason": f"provider_not_found:{provider_id}",
        }

    provider_ready = _provider_view_ready(provider)
    if not model:
        return {
            "ready": False,
            "provider_ready": provider_ready,
            "provider_missing": False,
            "model_missing": True,
            "readiness_reason": "model_missing",
        }
    models = _normalize_provider_model_ids(provider.get("models") or [])
    model_missing = bool(models and model not in models)
    ready = provider_ready and not model_missing
    reason = "ready"
    if not provider_ready:
        # BUG-001: 区分"未配置密钥" vs "密文无法解密(config.secret 轮换)",别压成一态。
        secret = provider.get("secret") if isinstance(provider.get("secret"), dict) else {}
        credential_mode = str(provider.get("credential_mode") or "api_key")
        if provider.get("enabled") is False:
            reason = "provider_disabled"
        elif credential_mode != "none" and secret.get("configured") and not secret.get("decryptable"):
            reason = "secret_decrypt_failed"
        elif credential_mode != "none" and not secret.get("configured"):
            reason = "secret_missing"
        else:
            reason = "provider_not_ready"
    elif model_missing:
        reason = f"model_not_listed:{model}"
    return {
        "ready": ready,
        "provider_ready": provider_ready,
        "provider_missing": False,
        "model_missing": model_missing,
        "readiness_reason": reason,
    }


def _annotate_node_route_readiness(
    *,
    node_routes: dict[str, dict[str, Any]],
    providers: dict[str, dict[str, Any]],
) -> None:
    for route in node_routes.values():
        route.update(_route_readiness(route, providers))


def _route_requires_llm(route: dict[str, Any]) -> bool:
    return str(route.get("status") or "active") == "active" and route.get("requires_llm") is not False


def _llm_readiness_summary(
    *,
    providers: dict[str, dict[str, Any]],
    node_routes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    provider_count = len(providers)
    active_provider_count = sum(1 for provider in providers.values() if _provider_view_ready(provider))
    active_routes = [route for route in node_routes.values() if _route_requires_llm(route)]
    configured_routes = [
        route
        for route in active_routes
        if route.get("configured") or route.get("provider_id") or route.get("model")
    ]
    ready_routes = [route for route in active_routes if route.get("ready") is True]
    blocked_routes = [route for route in active_routes if route.get("ready") is not True]
    return {
        "provider_count": provider_count,
        "active_provider_count": active_provider_count,
        "configured_route_count": len(configured_routes),
        "active_route_count": len(active_routes),
        "ready_route_count": len(ready_routes),
        "blocked_route_count": len(blocked_routes),
        "blocked_routes": [
            {
                "node_id": route.get("node_id"),
                "provider_id": route.get("provider_id"),
                "model": route.get("model"),
                "reason": route.get("readiness_reason"),
            }
            for route in blocked_routes
        ],
        "ready": active_provider_count > 0 and len(ready_routes) > 0 and not blocked_routes,
    }


def _role_slot_overview(node_routes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """槽位目录 + 从 node_routes 反推当前生效绑定(槽内全节点一致才算)。"""
    slots: list[dict[str, Any]] = []
    for entry in role_slot_catalog():
        bindings = {
            (
                _optional_text((node_routes.get(node_id) or {}).get("provider_id")),
                _optional_text((node_routes.get(node_id) or {}).get("model")),
            )
            for node_id in entry["node_ids"]
        }
        if len(bindings) == 1:
            provider_id, model = next(iter(bindings))
            current = (
                {"provider_id": provider_id, "model": model, "mixed": False}
                if provider_id or model
                else None
            )
        else:
            current = {"provider_id": None, "model": None, "mixed": True}
        slots.append({**entry, "current": current})
    return slots


def _retired_route_ids(*routing_tables: dict[str, Any]) -> list[str]:
    """路由表里已退役的键:不在节点注册表(reserved 节点仍在表内),也不是 task 别名。"""
    node_catalog = llm_node_catalog()
    return sorted(
        {
            str(key)
            for table in routing_tables
            for key in table
            if key not in node_catalog and key not in _TASK_ROUTING_ALIASES
        }
    )


def _prune_retired_routes(config_payload: dict[str, Any]) -> list[str]:
    """就地剪掉 node_routing / task_routing 里退役节点的路由,返回剪掉的 id(已排序)。

    parse_model_routing_config 会把 task_routing 并入 node_routing,所以两张表都得剪,
    否则退役条目会在下一次解析时重新冒出来;在解析之前剪,退役条目自身的字段错误也
    不会再阻塞保存。
    """
    pruned: set[str] = set()
    for table_name in ("node_routing", "task_routing"):
        table = config_payload.get(table_name)
        if not isinstance(table, dict):
            continue
        for key in _retired_route_ids(table):
            del table[key]
            pruned.add(key)
    return sorted(pruned)


def _parse_route_config_or_raise(config_payload: dict[str, Any]):
    """路由解析错误(某节点 api_mode / response_format 写错等)是作者可修的配置问题,
    必须以 422 + 点名节点的消息回到设置页,而不是漏成 500 INTERNAL_ERROR。"""
    try:
        return parse_model_routing_config(config_payload)
    except LLMConfigurationError as exc:
        raise DomainError(
            "CONFIG_ROUTE_INVALID",
            f"LLM node route config is invalid: {exc.message}",
            status_code=422,
            details={"llm_error_code": exc.code},
        ) from exc


def _provider_route_api_mode(provider_id: str, provider: dict[str, Any]) -> str:
    """把服务声明的 api_mode 展开到节点路由前先校验:老快照里的非法值会让路由解析
    在下游炸成 500,这里改为 422 并点名该服务。"""
    api_mode = _optional_text(provider.get("api_mode")) or "responses"
    if api_mode not in SUPPORTED_API_MODES:
        raise DomainError(
            "CONFIG_PROVIDER_INVALID",
            f"provider {provider_id} has unsupported api_mode {api_mode}; "
            f"set it to one of {', '.join(sorted(SUPPORTED_API_MODES))} and save the provider again",
            status_code=422,
        )
    return api_mode


def _validate_activating_node_route_bindings(
    *,
    node_routing: dict[str, Any],
    providers: dict[str, dict[str, Any]],
) -> None:
    missing_bindings: list[str] = []
    missing_models: list[str] = []
    not_ready_providers: list[str] = []
    reserved_nodes = reserved_llm_node_ids()
    node_catalog = llm_node_catalog()
    for node_id, task_config in node_routing.items():
        if node_id in reserved_nodes:
            continue
        if node_id not in node_catalog:
            # 退役节点的残留路由是惰性的:不参与激活校验(它常指向已删除的服务),
            # 由写路径剪枝、overview 的 stale_routes 展示。
            continue
        provider_id = task_config.provider_id
        if not provider_id or provider_id not in providers:
            missing_bindings.append(f"{node_id}:{provider_id or 'missing_provider_id'}")
            continue
        if not _provider_view_ready(providers[provider_id]):
            not_ready_providers.append(f"{node_id}:{provider_id}")
        models = _normalize_provider_model_ids(providers[provider_id].get("models") or [])
        if not _optional_text(task_config.model):
            missing_models.append(f"{node_id}:{provider_id}:missing_model")
        elif models and task_config.model not in models:
            missing_models.append(f"{node_id}:{provider_id}:{task_config.model}")

    if missing_bindings:
        raise DomainError(
            "CONFIG_ROUTE_PROVIDER_MISSING",
            "active LLM node routes must reference an existing provider_id: " + ", ".join(missing_bindings),
            status_code=422,
        )
    if not_ready_providers:
        raise DomainError(
            "CONFIG_ROUTE_PROVIDER_NOT_READY",
            "active LLM node routes must reference an enabled provider with configured credentials: "
            + ", ".join(not_ready_providers),
            status_code=422,
        )
    if missing_models:
        raise DomainError(
            "CONFIG_ROUTE_MODEL_MISSING",
            "active LLM node routes must use a model listed by their provider config: " + ", ".join(missing_models),
            status_code=422,
        )


def _required_text(value: Any, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{field} is required")


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_api_payload(payload: dict[str, Any]) -> dict[str, Any]:
    llm = payload.get("llm") if isinstance(payload.get("llm"), dict) else payload
    if not isinstance(llm, dict):
        raise ValueError("api config must include an llm mapping")
    return llm


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _float_value(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a valid number") from exc


def _encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def _fernet() -> Fernet:
    secret = _config_secret()
    if not secret:
        raise DomainError("CONFIG_SECRET_REQUIRED", "NOVEL_SYSTEM_CONFIG_SECRET is required to manage secrets", 403)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-4:]}"


def _admin_token() -> str | None:
    from novel_system.core_runtime import load_core_runtime

    return load_core_runtime().admin_token


def _config_secret() -> str | None:
    from novel_system.core_runtime import load_core_runtime

    return load_core_runtime().config_secret


def _requested_probe_model(payload: dict[str, Any]) -> str | None:
    explicit_model = _optional_text(payload.get("model"))
    if explicit_model:
        return explicit_model
    models = payload.get("models")
    if isinstance(models, list):
        for model in models:
            candidate = _optional_text(model)
            if candidate:
                return candidate
    return None


def _contains_cjk(value: str) -> bool:
    return any(
        "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff"
        for char in value
    )


def _looks_like_provider_model_id(value: str) -> bool:
    text = value.strip()
    return bool(text) and not _contains_cjk(text) and not any(char.isspace() for char in text) and any(char.isalnum() for char in text)


def _normalize_provider_model_id(value: str) -> str:
    text = value.strip()
    if "/" not in text:
        return text
    prefix, suffix = text.split("/", 1)
    suffix = suffix.strip()
    if _contains_cjk(prefix) and _looks_like_provider_model_id(suffix):
        return suffix
    return text


def _normalize_provider_model_ids(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            normalized
            for model in values
            if isinstance(model, str)
            for normalized in [_normalize_provider_model_id(model)]
            if normalized
        )
    )


def _extract_model_ids(response: httpx.Response) -> list[str]:
    try:
        payload = response.json()
    except ValueError:
        return []
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        if not candidates:
            candidates.append(payload)
    elif isinstance(payload, list):
        candidates.extend(payload)

    model_ids: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            normalized = _normalize_provider_model_id(item)
            if normalized:
                model_ids.append(normalized)
        elif isinstance(item, dict):
            for key in ("id", "name", "model"):
                value = _optional_text(item.get(key))
                if value:
                    normalized = _normalize_provider_model_id(value)
                    if normalized:
                        model_ids.append(normalized)
                    break
    return list(dict.fromkeys(model_ids))


def _probe_completion(
    *,
    session: Session,
    provider: str,
    base_url: str,
    api_key: str | None,
    provider_options: dict[str, Any] | None,
    model: str,
    api_mode: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    adapter = adapter_registry().get(provider)
    probe = (
        adapter.completion_probe_request(
            base_url=base_url,
            model=model,
            api_mode=api_mode,
            api_key=api_key,
            provider_options=provider_options,
        )
        if adapter is not None
        else None
    )
    if probe is None:
        return {
            "ok": None,
            "status_code": None,
            "api_mode": api_mode,
            "endpoint": None,
            "message": f"completion check skipped for provider {provider}",
        }
    # 记账申报的输出预算必须 ≥ adapter 探活载荷真正允许的输出(各家 wire 上限 8),
    # 并给两类真实偏差留余量:厂商对话模板使 prompt 计数高于本地估算(实测 ping=11 vs 估 8)、
    # 思考型后端可能不按 max_tokens 截断 reasoning tokens。曾申报 1 → 预留 9 < 实际 19,
    # 探活必然触发 LLM_USAGE_EXCEEDS_RESERVATION 拦截(probe 是 system scope,不入场景预算,
    # 超配无成本)。
    request = LLMRequest(
        model=model,
        messages=[{"role": "user", "content": "ping"}],
        temperature=0.0,
        max_output_tokens=PROBE_ACCOUNTING_OUTPUT_BUDGET,
        response_format="text",
        provider=provider,
        timeout_seconds=timeout_seconds,
        api_mode=probe.api_mode,
        node_id="provider_probe",
        provider_options=provider_options or {},
    )
    accounted = execute_accounted_completion_probe(
        session,
        request,
        LLMCallContext(
            scope_type="system",
            scope_id="provider_probe",
            node_id="provider_probe",
            step="completion_probe",
        ),
        url=probe.url,
        headers=probe.headers,
        payload=probe.payload,
        timeout_seconds=timeout_seconds,
        trust_env=_httpx_trust_env_for_base_url(base_url),
    )
    response = accounted.response
    if response is None:
        return {
            "ok": False,
            "status_code": None,
            "api_mode": probe.api_mode,
            "endpoint": probe.endpoint,
            "message": accounted.error_message or "completion probe failed",
            "error_code": accounted.error_code,
            "llm_call_id": accounted.llm_call_id,
        }
    result = {
        "ok": response.is_success and accounted.error_code is None,
        "status_code": response.status_code,
        "model": model,
        "api_mode": probe.api_mode,
        "endpoint": probe.endpoint,
        "message": (
            "minimal completion succeeded"
            if response.is_success and accounted.error_code is None
            else accounted.error_message or "completion probe accounting failed"
            if response.is_success
            else _completion_error_summary(
                response,
                api_mode=probe.api_mode,
                endpoint=probe.endpoint,
            )
        ),
        "error_code": accounted.error_code,
        "llm_call_id": accounted.llm_call_id,
    }
    if response.status_code == 404 and probe.api_mode == "responses":
        result["next_action"] = "switch_provider_api_mode_to_chat_or_use_responses_compatible_provider"
    return result


def _completion_error_summary(response: httpx.Response, *, api_mode: str, endpoint: str) -> str:
    if response.status_code == 404 and api_mode == "responses" and endpoint == "/responses":
        return (
            "Responses API endpoint returned 404; this provider or relay may only support Chat Completions. "
            "Set api_mode to chat and sync node routes, or use a provider that supports the Responses API."
        )
    return _provider_error_summary(response)


def _provider_error_summary(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] if response.text else f"provider returned status {response.status_code}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return f"provider returned status {response.status_code}"
