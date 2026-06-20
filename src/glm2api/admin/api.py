"""Admin API handlers + static asset serving.

This module exposes a single function `handle_admin_request(handler, config, glm_client)`
which inspects `handler.path` and dispatches to either an admin JSON endpoint
or a static asset. Returns True if the request was handled (caller should not
continue), False if the path is not under /admin (caller continues normal flow).

Authentication:
  - POST /admin/login  (body: {"password": "..."}) -> {"token": "...", "expires_in": 28800}
  - All other /admin/api/* require header: X-Admin-Token: <token>
  - /admin and /admin/static/* are public (login page itself must be reachable)

Password source (in priority order):
  1. env ADMIN_PASSWORD (if non-empty)
  2. first entry of config.server_api_keys (if any)
  3. default "admin" + a warning printed to logs

This is intentionally simple: the panel is meant for local/single-admin use,
not multi-tenant. Token TTL is 8h.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
import traceback
from http import HTTPStatus
from logging import Logger
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AppConfig
from ..core.model_variants import split_model_features
from ..core.model_profiles import get_model_profile
from ..services.glm_client import GLMWebClient, UpstreamAPIError, QueueTimeoutError
from ..services.upstream_discovery import get_upstream_discovery, to_dict as upstream_assistant_to_dict
from ..services.models_registry import (
    get_unified_models,
    get_orphan_assistants,
    to_dict as unified_model_to_dict,
)
from ..services.dynamic_models import merge_with_builtin, get_dynamic_registry
from .store import (
    GLOBAL_STORE,
    RequestRecord,
    classify_protocol,
    get_store,
    humanize_bytes,
)

# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

_STATIC_DIR = Path(__file__).parent / "static"
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def _resolve_static(path: str) -> Optional[Path]:
    """Map a /admin/static/<relpath> URL to a filesystem path under _STATIC_DIR.
    Returns None if the path escapes the static dir or doesn't exist."""
    relpath = path
    if relpath.startswith("/admin/static/"):
        relpath = relpath[len("/admin/static/"):]
    elif relpath == "/admin/static":
        relpath = ""
    # Normalize and prevent directory traversal
    candidate = (_STATIC_DIR / relpath).resolve()
    try:
        candidate.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


# ---------------------------------------------------------------------------
# Admin password & sessions
# ---------------------------------------------------------------------------

def _resolve_admin_password(config: AppConfig, logger: Logger) -> str:
    """Resolve the admin password in priority order:
    1. os.environ['ADMIN_PASSWORD'] (if non-empty) — set by docker/systemd/etc
    2. ADMIN_PASSWORD line in the .env file (re-read each call so hot-edits work)
    3. first entry of config.server_api_keys (if any)
    4. default 'admin' with a warning
    """
    env_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    # Read from .env file (so users don't have to also export it to env)
    try:
        env_path = config.env_file_path
        if env_path and env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "ADMIN_PASSWORD":
                    v = v.strip()
                    if v.startswith(("'", '"')) and v.endswith(("'", '"')) and len(v) >= 2:
                        v = v[1:-1]
                    if v:
                        return v
    except OSError:
        pass
    if config.server_api_keys:
        return config.server_api_keys[0]
    logger.warning("ADMIN_PASSWORD 未设置且 SERVER_API_KEYS 为空，管理面板拒绝登录。请设置 ADMIN_PASSWORD 环境变量。")
    return ""  # P2-4: 不再使用默认密码 admin，返回空字符串拒绝所有登录


def _authorize_admin(handler, config: AppConfig, logger: Logger) -> bool:
    """Check X-Admin-Token header against in-memory sessions."""
    token = handler.headers.get("X-Admin-Token", "").strip()
    if not token:
        # Also accept ?token= for browser convenience (less secure, but fine for local admin)
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(handler.path).query)
        if "token" in qs and qs["token"]:
            token = qs["token"][0].strip()
    return get_store().validate_session(token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_json(handler, status: HTTPStatus, payload: Dict[str, Any], config: AppConfig) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler, file_path: Path, config: AppConfig) -> None:
    try:
        body = file_path.read_bytes()
    except OSError:
        handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
        return
    suffix = file_path.suffix.lower()
    content_type = _MIME_TYPES.get(suffix, "application/octet-stream")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    # Cache static assets for 1h (login page itself is small, no harm)
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler) -> Dict[str, Any]:
    content_length = int(handler.headers.get("Content-Length", "0") or "0")
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Endpoint implementations
# ---------------------------------------------------------------------------

# P1-4: 防暴力破解 — IP 级别登录失败计数
_login_failures: dict[str, list[float]] = {}  # ip -> [timestamps]
_LOGIN_MAX_FAILURES = 5
_LOGIN_LOCKOUT_SECONDS = 900  # 15 分钟


def _check_brute_force(ip: str) -> bool:
    """检查 IP 是否被锁定。返回 True 表示允许登录。"""
    import time as _time
    now = _time.time()
    failures = _login_failures.get(ip, [])
    # 清理过期的失败记录
    failures = [ts for ts in failures if now - ts < _LOGIN_LOCKOUT_SECONDS]
    _login_failures[ip] = failures
    return len(failures) < _LOGIN_MAX_FAILURES


def _record_login_failure(ip: str) -> None:
    """记录一次登录失败。"""
    import time as _time
    if ip not in _login_failures:
        _login_failures[ip] = []
    _login_failures[ip].append(_time.time())


def _handle_login(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    client_ip = handler.client_address[0] if handler.client_address else "unknown"
    # P1-4: 防暴力破解检查
    if not _check_brute_force(client_ip):
        _send_json(handler, HTTPStatus.TOO_MANY_REQUESTS, {
            "error": "too_many_login_attempts",
            "message": f"Too many failed login attempts. Please try again in {_LOGIN_LOCKOUT_SECONDS // 60} minutes.",
        }, config)
        return
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    password = str(payload.get("password", "")).strip()
    expected = _resolve_admin_password(config, logger)
    # Constant-time compare to reduce timing leak
    import hmac
    if not password or not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        _record_login_failure(client_ip)
        remaining = _LOGIN_MAX_FAILURES - len(_login_failures.get(client_ip, []))
        _send_json(handler, HTTPStatus.UNAUTHORIZED, {
            "error": "invalid_password",
            "remaining_attempts": max(0, remaining),
        }, config)
        return
    # 登录成功，清除失败记录
    _login_failures.pop(client_ip, None)
    ttl = 8 * 3600
    token = get_store().create_session(ttl_seconds=ttl)
    _send_json(handler, HTTPStatus.OK, {"token": token, "expires_in": ttl}, config)


def _handle_logout(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    token = handler.headers.get("X-Admin-Token", "").strip()
    if token:
        get_store().revoke_session(token)
    _send_json(handler, HTTPStatus.OK, {"ok": True}, config)


def _handle_dashboard(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    _send_json(handler, HTTPStatus.OK, get_store().dashboard(), config)


def _handle_logs(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(handler.path).query)
    limit = int(qs.get("limit", ["100"])[0])
    only_errors = qs.get("errors", ["0"])[0] in ("1", "true", "yes")
    limit = max(1, min(limit, 500))
    _send_json(handler, HTTPStatus.OK, {"logs": get_store().recent_logs(limit=limit, only_errors=only_errors)}, config)


def _handle_rotates(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    _send_json(handler, HTTPStatus.OK, {"events": get_store().rotate_events(limit=100)}, config)


def _account_snapshot(config: AppConfig, glm_client: GLMWebClient, idx: int) -> Optional[Dict[str, Any]]:
    auth = glm_client.auth
    accounts = getattr(auth, "_accounts", [])
    if idx < 0 or idx >= len(accounts):
        return None
    acc = accounts[idx]
    extra = get_store().account_extra_stats(idx)
    cached_token = acc.cached_token
    return {
        "index": idx,
        "is_guest": acc.is_guest,
        "device_id_short": (acc.device_id or "")[:8],
        "request_id_counter": acc.request_id_counter,
        "device_request_count": acc.device_request_count,
        "rotate_threshold": config.device_id_rotate_threshold,
        "cached_token_expires_in": (
            cached_token.expires_at - time.time() if cached_token else -1
        ),
        "prefetch_in_progress": acc.prefetch_in_progress,
        "has_prefetched_token": acc.prefetched_token is not None,
        "last_used_ts": extra.get("last_used_ts", 0.0),
        "success_count": extra.get("success", 0),
        "error_count": extra.get("error", 0),
    }


def _handle_accounts(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    auth = glm_client.auth
    accounts = getattr(auth, "_accounts", [])
    snapshots = []
    for idx in range(len(accounts)):
        snap = _account_snapshot(config, glm_client, idx)
        if snap is not None:
            snapshots.append(snap)
    # Concurrent queue state
    queue = glm_client.request_queue
    with queue._condition:
        queue_ahead = max(0, queue._next_ticket - queue._serving_ticket - queue.max_concurrency)
        max_concurrency = queue.max_concurrency
    _send_json(handler, HTTPStatus.OK, {
        "accounts": snapshots,
        "max_concurrency": max_concurrency,
        "queue_ahead": queue_ahead,
        "queue_wait_timeout": config.glm_queue_wait_timeout,
    }, config)


def _handle_account_rotate(handler, idx_str: str, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    try:
        idx = int(idx_str)
    except ValueError:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_index"}, config)
        return
    auth = glm_client.auth
    accounts = getattr(auth, "_accounts", [])
    if idx < 0 or idx >= len(accounts):
        _send_json(handler, HTTPStatus.NOT_FOUND, {"error": "account_not_found"}, config)
        return
    try:
        old_dev = auth.get_device_id_for_account(idx)[:8]
        # Pass reason="manual" so rotate_device_id_for_account records it correctly
        # (no need to call get_store().record_rotate separately — the auth function does it)
        auth.rotate_device_id_for_account(idx, reason="manual")
        new_dev = auth.get_device_id_for_account(idx)[:8]
        logger.info("admin triggered rotate account=%s old=%s new=%s", idx, old_dev, new_dev)
        _send_json(handler, HTTPStatus.OK, {"ok": True, "old_device": old_dev, "new_device": new_dev}, config)
    except Exception as exc:
        logger.error("admin rotate failed account=%s error=%s", idx, exc)
        _send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}, config)


def _sanitize_config(config: AppConfig) -> Dict[str, Any]:
    """Return a sanitized view of the config: sensitive values are masked."""
    def mask(v: str) -> str:
        if not v:
            return ""
        if len(v) <= 8:
            return "***"
        return v[:4] + "***" + v[-4:]

    return {
        "host": config.host,
        "port": config.port,
        "api_prefix": config.api_prefix,
        "log_level": config.log_level,
        "debug_dump_all": config.debug_dump_all,
        "request_timeout": config.request_timeout,
        "glm_base_url": config.glm_base_url,
        "glm_use_guest_refresh_token": config.glm_use_guest_refresh_token,
        # Single token is sensitive
        "glm_refresh_token": mask(config.glm_refresh_token),
        # Show only count, not content, of multi-account list
        "glm_refresh_tokens_count": len(config.glm_refresh_tokens),
        "glm_assistant_id": config.glm_assistant_id,
        "glm_image_assistant_id": config.glm_image_assistant_id,
        "glm_image_model_name": config.glm_image_model_name,
        "glm_delete_conversation": config.glm_delete_conversation,
        "glm_max_concurrency": config.glm_max_concurrency,
        "glm_queue_wait_timeout": config.glm_queue_wait_timeout,
        "glm_busy_max_retries": config.glm_busy_max_retries,
        "glm_busy_retry_interval": config.glm_busy_retry_interval,
        "glm_guest_max_retries": config.glm_guest_max_retries,
        "device_id_rotate_threshold": config.device_id_rotate_threshold,
        "blocked_tool_names": list(config.blocked_tool_names),
        "exposed_models": list(config.exposed_models),
        "server_api_keys_count": len(config.server_api_keys),
        "server_api_keys_first": mask(config.server_api_keys[0]) if config.server_api_keys else "",
        "cors_allow_origin": config.cors_allow_origin,
        "token_file_path": str(config.token_file_path),
        "env_file_path": str(config.env_file_path),
    }


def _handle_config(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    _send_json(handler, HTTPStatus.OK, _sanitize_config(config), config)


def _handle_models(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """返回统一模型列表（单一数据源，前后端一致）。

    设计目标（用户原话："不要搞的前端是 20 几个模型后端又是几个模型的要统一获取模型"）：
      - 单一 models 列表：所有地方（/v1/models, /admin/api/models, /admin/api/probe,
        /admin/api/probe_model）看到的模型列表完全一致
      - 每个模型附加真实上游助手元数据（upstream_name / upstream_avatar 等），
        不再分成 upstream + local 两个独立列表
      - "孤儿助手"（真实助手中没有对应本地模型别名的）单独返回到 orphan_assistants，
        前端可折叠展示，提醒用户这些助手目前无法通过 OpenAI API 调用

    返回结构：
      {
        "total": 82,                  # 模型总数（= config.exposed_models 长度）
        "base_count": 22,             # 基础模型数
        "models": [...],              # 统一模型列表（含真实助手元数据 + 探针结果）
        "orphan_assistants": [...],   # 未映射到本地模型的真实助手
        "upstream_cache": {...}       # 上游助手缓存状态
      }
    """
    # 拉取探针缓存
    probe_cache = get_store().get_model_probe_cache()
    # 获取合并后的模型列表（builtin + 动态发现）
    # 这是关键：用户原话"未来升级模型我们不需要再添加代码会自动获取到的"
    # 动态发现的模型来自 chat_completion 响应的 model 字段
    effective_models = merge_with_builtin(list(config.exposed_models))
    # 构造统一模型列表（内部会拉取真实助手元数据）
    models = get_unified_models(
        config, logger, glm_client.auth,
        probe_cache=probe_cache,
        fetch_upstream=True,
        effective_models=effective_models,
    )
    # 获取孤儿助手（未映射的真实助手）
    orphans = get_orphan_assistants(config, logger, glm_client.auth)
    # 上游缓存信息
    discovery = get_upstream_discovery(config, logger, glm_client.auth)
    upstream_cache = discovery.get_cache_info()
    # 动态发现注册表统计
    dynamic_stats = get_dynamic_registry().get_stats()
    # 统计
    base_count = len({m.base for m in models})

    _send_json(handler, HTTPStatus.OK, {
        "total": len(models),
        "base_count": base_count,
        "models": [unified_model_to_dict(m) for m in models],
        "orphan_assistants": [upstream_assistant_to_dict(a) for a in orphans],
        "upstream_cache": upstream_cache,
        "dynamic_discovery": dynamic_stats,
    }, config)


def _handle_upstream_refresh(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """强制刷新真实上游助手列表（绕过缓存）。

    刷新后下次调 /admin/api/models 会自动看到新的助手元数据。
    """
    discovery = get_upstream_discovery(config, logger, glm_client.auth)
    discovery.discover(force_refresh=True)
    cache_info = discovery.get_cache_info()
    # 顺便返回新的统一模型列表（前端刷新后立即更新 UI）
    probe_cache = get_store().get_model_probe_cache()
    effective_models = merge_with_builtin(list(config.exposed_models))
    models = get_unified_models(
        config, logger, glm_client.auth,
        probe_cache=probe_cache,
        fetch_upstream=True,
        effective_models=effective_models,
    )
    orphans = get_orphan_assistants(config, logger, glm_client.auth)
    dynamic_stats = get_dynamic_registry().get_stats()
    _send_json(handler, HTTPStatus.OK, {
        "ok": True,
        "cache": cache_info,
        "models": [unified_model_to_dict(m) for m in models],
        "orphan_assistants": [upstream_assistant_to_dict(a) for a in orphans],
        "dynamic_discovery": dynamic_stats,
    }, config)


def _handle_probe_model(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """对单个模型发起最小请求，返回探针结果。

    请求体：
      { "model": "glm-4-flash", "prompt": "hi" (可选) }

    返回：
      {
        "model": "glm-4-flash",
        "ok": true,
        "latency_ms": 1234,
        "status": 200,
        "content_preview": "你好...",
        "usage": {...},
        "account_index": 3,
        "error": null
      }
    """
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    model = str(payload.get("model", "")).strip()
    prompt = str(payload.get("prompt", "") or "hi").strip() or "hi"
    if not model:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_model"}, config)
        return
    # 校验模型存在（builtin + 动态发现）
    effective_models = merge_with_builtin(list(config.exposed_models))
    if model not in effective_models:
        _send_json(handler, HTTPStatus.NOT_FOUND, {
            "error": "model_not_found",
            "model": model,
            "hint": "该模型不在当前服务暴露的模型列表中",
        }, config)
        return

    # 构造最小探针请求：max_tokens=8 足够确认模型可用且消耗极小
    probe_payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8,
        "stream": False,
    }
    start = time.perf_counter()
    try:
        result, _conv_id = glm_client.chat_completion(probe_payload)
        latency_ms = int((time.perf_counter() - start) * 1000)
        # 提取内容预览
        choices = result.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "")
        probe_result = {
            "model": model,
            "ok": True,
            "latency_ms": latency_ms,
            "status": 200,
            "content_preview": content[:200],
            "usage": result.get("usage"),
            "account_index": glm_client.get_last_account_index(),
            "error": None,
        }
    except UpstreamAPIError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        probe_result = {
            "model": model,
            "ok": False,
            "latency_ms": latency_ms,
            "status": exc.status_code,
            "content_preview": "",
            "usage": None,
            "account_index": glm_client.get_last_account_index(),
            "error": str(exc),
        }
    except QueueTimeoutError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        probe_result = {
            "model": model,
            "ok": False,
            "latency_ms": latency_ms,
            "status": 503,
            "content_preview": "",
            "usage": None,
            "account_index": glm_client.get_last_account_index(),
            "error": f"队列超时: {exc}",
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        probe_result = {
            "model": model,
            "ok": False,
            "latency_ms": latency_ms,
            "status": 500,
            "content_preview": "",
            "usage": None,
            "account_index": glm_client.get_last_account_index(),
            "error": f"{type(exc).__name__}: {exc}",
        }
    # 缓存到 store
    get_store().record_model_probe(model, {
        "ok": probe_result["ok"],
        "latency_ms": probe_result["latency_ms"],
        "status": probe_result["status"],
        "content_preview": probe_result["content_preview"],
        "account_index": probe_result["account_index"],
        "error": probe_result["error"],
    })
    _send_json(handler, HTTPStatus.OK, probe_result, config)


def _handle_probe(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """端点测试：接收完整 OpenAI Chat Completions 请求体，转发到上游，返回完整响应。

    请求体：标准 OpenAI Chat Completions payload（model + messages + ...）
    返回：
      {
        "ok": true,
        "latency_ms": 1234,
        "status": 200,
        "response": { ...完整 OpenAI ChatCompletion 对象... },
        "conversation_id": "...",
        "account_index": 3,
        "error": null
      }
    """
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    model = str(payload.get("model", "")).strip()
    if not model:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_model"}, config)
        return
    # 校验模型存在（builtin + 动态发现）
    effective_models = merge_with_builtin(list(config.exposed_models))
    if model not in effective_models:
        _send_json(handler, HTTPStatus.NOT_FOUND, {
            "error": "model_not_found",
            "model": model,
        }, config)
        return
    if not isinstance(payload.get("messages"), list) or not payload.get("messages"):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_messages"}, config)
        return
    # 强制非流式（admin probe 不支持流式，避免长连接复杂度）
    payload["stream"] = False

    # P2 修复：检测图像模型，走图像生成路径而非 chat completions
    # 图像模型（cogView / glm-image-1）应该返回图片 URL 而非文字
    is_image_model = model in (
        config.glm_image_model_name,
        "cogView-4-250304",
    ) or model.startswith("cogView")

    start = time.perf_counter()
    try:
        if is_image_model:
            # 图像模型：走 image generation 路径
            # 从 messages 提取 prompt
            prompt = ""
            for msg in payload.get("messages", []):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        prompt = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                prompt = str(part.get("text", ""))
                                break
                    break
            image_payload = {
                "model": model,
                "prompt": prompt or "a cat",
                "size": payload.get("size", "1024x1024"),
            }
            result = glm_client.generate_images(image_payload)
            latency_ms = int((time.perf_counter() - start) * 1000)
            _send_json(handler, HTTPStatus.OK, {
                "ok": True,
                "latency_ms": latency_ms,
                "status": 200,
                "response": result,
                "conversation_id": None,
                "account_index": glm_client.get_last_account_index(),
                "error": None,
                "is_image": True,
            }, config)
        else:
            # 普通对话模型：走 chat completions 路径
            result, conv_id = glm_client.chat_completion(payload)
            latency_ms = int((time.perf_counter() - start) * 1000)
            _send_json(handler, HTTPStatus.OK, {
                "ok": True,
                "latency_ms": latency_ms,
                "status": 200,
                "response": result,
                "conversation_id": conv_id,
                "account_index": glm_client.get_last_account_index(),
                "error": None,
                "is_image": False,
            }, config)
    except UpstreamAPIError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        _send_json(handler, HTTPStatus.OK, {
            "ok": False,
            "latency_ms": latency_ms,
            "status": exc.status_code,
            "response": exc.payload,
            "conversation_id": None,
            "account_index": glm_client.get_last_account_index(),
            "error": str(exc),
        }, config)
    except QueueTimeoutError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        _send_json(handler, HTTPStatus.OK, {
            "ok": False,
            "latency_ms": latency_ms,
            "status": 503,
            "response": None,
            "conversation_id": None,
            "account_index": glm_client.get_last_account_index(),
            "error": f"队列超时: {exc}",
        }, config)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        logger.error("admin probe failed model=%s error=%s\n%s", model, exc, traceback.format_exc())
        _send_json(handler, HTTPStatus.OK, {
            "ok": False,
            "latency_ms": latency_ms,
            "status": 500,
            "response": None,
            "conversation_id": None,
            "account_index": glm_client.get_last_account_index(),
            "error": f"{type(exc).__name__}: {exc}",
        }, config)


def _handle_system(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    import resource
    import shutil
    # RSS from getrusage (Linux: ru_maxrss in KB; macOS: bytes)
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        rss_bytes = rusage.ru_maxrss
    else:
        rss_bytes = rusage.ru_maxrss * 1024
    # Disk usage of project dir (where logs live)
    log_dir = Path(config.env_file_path).parent / "log"
    log_size = 0
    log_file_count = 0
    if log_dir.is_dir():
        for p in log_dir.rglob("*"):
            if p.is_file():
                try:
                    log_size += p.stat().st_size
                    log_file_count += 1
                except OSError:
                    pass
    # Also count *.log files in project root
    project_root = Path(config.env_file_path).parent
    for p in project_root.glob("*.log"):
        try:
            log_size += p.stat().st_size
            log_file_count += 1
        except OSError:
            pass
    disk_total, disk_used, disk_free = shutil.disk_usage(str(project_root))
    # Threads
    import threading
    thread_count = threading.active_count()
    # Python version + git commit (if available)
    py_version = sys.version.split()[0]
    payload = {
        "now": time.time(),
        "uptime_seconds": time.time() - get_store()._started_at,
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "python": py_version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "node": platform.node(),
        },
        "memory": {
            "rss_bytes": rss_bytes,
            "rss_human": humanize_bytes(rss_bytes),
        },
        "threads": thread_count,
        "disk": {
            "project_root": str(project_root),
            "log_dir": str(log_dir),
            "log_size_bytes": log_size,
            "log_size_human": humanize_bytes(log_size),
            "log_file_count": log_file_count,
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "free_bytes": disk_free,
            "total_human": humanize_bytes(disk_total),
            "used_human": humanize_bytes(disk_used),
            "free_human": humanize_bytes(disk_free),
        },
    }
    _send_json(handler, HTTPStatus.OK, payload, config)


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

# API Key 管理端点

def _handle_apikeys(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """返回所有 API keys（含用量统计）。"""
    from .store import get_store
    store = get_store()
    store.init_env_api_keys(list(config.server_api_keys))
    keys = store.get_api_keys()
    _send_json(handler, HTTPStatus.OK, {
        "keys": keys,
        "total": len(keys),
        "env_keys_count": sum(1 for k in keys if k.get("is_env")),
        "custom_keys_count": sum(1 for k in keys if not k.get("is_env")),
    }, config)


def _handle_apikey_create(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """创建新 API key。请求体: {"name": "my-key-name"}"""
    from .store import get_store
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    name = str(payload.get("name", "")).strip() or "未命名"
    result = get_store().create_api_key(name)
    logger.info("admin created api key name=%s", name)
    _send_json(handler, HTTPStatus.OK, result, config)


def _handle_apikey_delete(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """删除 API key。请求体: {"key": "sk-glm2api-xxx"}"""
    from .store import get_store
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    key = str(payload.get("key", "")).strip()
    if not key:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_key"}, config)
        return
    ok = get_store().delete_api_key(key)
    if ok:
        logger.info("admin deleted api key")
        _send_json(handler, HTTPStatus.OK, {"ok": True}, config)
    else:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "cannot_delete_env_key_or_not_found"}, config)


def _handle_apikey_toggle(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
    """启用/禁用 API key。请求体: {"key": "xxx", "enabled": true/false}"""
    from .store import get_store
    try:
        payload = _read_json_body(handler)
    except (json.JSONDecodeError, UnicodeDecodeError):
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "invalid_json"}, config)
        return
    key = str(payload.get("key", "")).strip()
    enabled = bool(payload.get("enabled", True))
    if not key:
        _send_json(handler, HTTPStatus.BAD_REQUEST, {"error": "missing_key"}, config)
        return
    ok = get_store().toggle_api_key(key, enabled)
    _send_json(handler, HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {
        "ok": ok, "enabled": enabled,
    }, config)


# Map of /admin/api/<name> -> handler function (POST or GET both accepted
# where appropriate; each handler is method-agnostic)
_API_ROUTES = {
    "login": ("POST", _handle_login),
    "logout": ("POST", _handle_logout),
    "dashboard": ("GET", _handle_dashboard),
    "logs": ("GET", _handle_logs),
    "rotates": ("GET", _handle_rotates),
    "accounts": ("GET", _handle_accounts),
    "models": ("GET", _handle_models),
    "upstream_refresh": ("POST", _handle_upstream_refresh),
    "probe_model": ("POST", _handle_probe_model),
    "probe": ("POST", _handle_probe),
    "config": ("GET", _handle_config),
    "system": ("GET", _handle_system),
    # API Key 管理
    "apikeys": ("GET", _handle_apikeys),
    "apikeys/create": ("POST", _handle_apikey_create),
    "apikeys/delete": ("POST", _handle_apikey_delete),
    "apikeys/toggle": ("POST", _handle_apikey_toggle),
}


def handle_admin_request(handler, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> bool:
    """Returns True if request was handled (caller should not continue).

    Handles:
      - GET  /admin               -> serve index.html (login page / SPA)
      - GET  /admin/              -> serve index.html
      - GET  /admin/static/*      -> serve static asset
      - POST /admin/api/login     -> {token, expires_in}
      - *    /admin/api/<name>    -> JSON endpoints (requires X-Admin-Token)
      - OPTIONS /admin/*          -> CORS preflight
    """
    from urllib.parse import urlparse
    path = urlparse(handler.path).path

    # OPTIONS preflight for any admin path
    if handler.command == "OPTIONS":
        handler.send_response(HTTPStatus.NO_CONTENT)
        handler.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
        handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.end_headers()
        return True

    # Not an admin path
    if not (path == "/admin" or path == "/admin/" or path.startswith("/admin/")):
        return False

    # --- Static + index ---
    if path == "/admin" or path == "/admin/" or path == "/admin/index.html":
        index_file = _STATIC_DIR / "index.html"
        _send_static(handler, index_file, config)
        return True

    if path.startswith("/admin/static/"):
        file_path = _resolve_static(path)
        if file_path is None:
            handler.send_error(HTTPStatus.NOT_FOUND, "Not Found")
            return True
        _send_static(handler, file_path, config)
        return True

    # --- API endpoints ---
    if path.startswith("/admin/api/"):
        name = path[len("/admin/api/"):].rstrip("/")
        # account rotate is a special path: /admin/api/accounts/<idx>/rotate
        if name.startswith("accounts/") and name.endswith("/rotate"):
            idx_str = name[len("accounts/"):-len("/rotate")]
            if not _authorize_admin(handler, config, logger):
                _send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, config)
                return True
            _handle_account_rotate(handler, idx_str, config, glm_client, logger)
            return True

        if name not in _API_ROUTES:
            logger.warning("admin api unknown endpoint name=%r path=%r command=%r", name, path, handler.command)
            _send_json(handler, HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint"}, config)
            return True

        expected_method, fn = _API_ROUTES[name]
        if handler.command != expected_method:
            _send_json(handler, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"}, config)
            return True

        # login is the only endpoint that doesn't require auth
        if name != "login":
            if not _authorize_admin(handler, config, logger):
                _send_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, config)
                return True

        try:
            fn(handler, config, glm_client, logger)
        except Exception as exc:
            logger.error("admin endpoint failed path=%s error=%s\n%s", path, exc, traceback.format_exc())
            _send_json(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}, config)
        return True

    # Anything else under /admin that didn't match: serve index (SPA fallback)
    index_file = _STATIC_DIR / "index.html"
    _send_static(handler, index_file, config)
    return True


# ---------------------------------------------------------------------------
# Recording hook (called by main server on every completed request)
# ---------------------------------------------------------------------------

def record_request(
    *,
    method: str,
    path: str,
    protocol: str,
    model: str,
    status: int,
    duration_ms: int,
    client_ip: str,
    account_index: int,
    stream: bool,
    error: str,
    request_id: str,
    api_key: str = "",
) -> None:
    """Called by main server after every request to record it in the admin store."""
    rec = RequestRecord(
        ts=time.time(),
        method=method,
        path=path,
        protocol=protocol,
        model=model,
        status=status,
        duration_ms=duration_ms,
        client_ip=client_ip,
        account_index=account_index,
        stream=stream,
        error=error,
        request_id=request_id,
        api_key=api_key,
    )
    get_store().record_request(rec)
