from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import socket
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging import Logger
from urllib.parse import urlparse

from .admin.api import handle_admin_request, record_request as admin_record_request
from .admin.store import classify_protocol as admin_classify_protocol
from .config import AppConfig
from .logging_utils import debug_dump
from .services.anthropic_adapter import (
    AnthropicStreamAccumulator,
    anthropic_to_openai,
    openai_to_anthropic_response,
)
from .services.glm_client import GLMWebClient, QueueTimeoutError, UpstreamAPIError
from .services.responses_adapter import (
    ResponsesStreamAccumulator,
    openai_to_responses,
    responses_to_openai,
)
from .services.responses_v2 import (
    ResponsesV2StreamAccumulator,
    openai_to_responses_v2,
    responses_v2_to_openai,
)
from .core.openai_compat import (
    ERROR_API,
    ERROR_AUTHENTICATION,
    ERROR_INVALID_REQUEST,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_RATE_LIMIT,
    ERROR_SERVER,
    ERROR_UPSTREAM,
    gen_chatcmpl_id,
    gen_message_id,
    gen_request_id,
    gen_response_id,
    make_error,
    now_timestamp,
    status_for_error_type,
    system_fingerprint,
)
from .core.tokenizer import count_tokens, estimate_message_tokens


_CLIENT_DISCONNECTED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout)
RESPONSES_STREAM_HEARTBEAT_SECONDS = 2.0  # codex/clients expect data within ~4s; 2s keeps them alive


def _extract_usage_from_sse_chunks(raw_chunks: list[bytes]) -> dict[str, int] | None:
    """从 SSE chunks 中提取最后一个含 usage 字段的 chunk 的 usage。

    OpenAI 流式响应在最后一个 chunk（带 finish_reason + usage）发送完整 token 统计。
    本函数扫描所有 chunks，找到最后一个含 usage 的并返回。

    Returns:
        {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int} 或 None
    """
    usage: dict[str, int] | None = None
    for raw in raw_chunks:
        if not raw:
            continue
        try:
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            continue
        for line in text.split("\n"):
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]" or not data_str:
                continue
            try:
                chunk_dict = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk_dict, dict):
                continue
            u = chunk_dict.get("usage")
            if isinstance(u, dict):
                # 持续覆盖，保留最后一个含 usage 的 chunk
                usage = {
                    "prompt_tokens": int(u.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(u.get("completion_tokens", 0) or 0),
                    "total_tokens": int(u.get("total_tokens", 0) or 0),
                }
    return usage


def _strip_markdown_fences(text: str) -> str:
    """剥离 GLM 输出习惯性包裹的 markdown 代码块（```json ... ``` / ``` ... ```）。

    v33 P2-3 修复：当 response_format=json_object 时，即使 system instruction
    明确告诉 GLM "Do not wrap the JSON in markdown code fences"，
    GLM 仍会偶尔输出 ```json\\n{...}\\n``` 格式。客户端期望纯 JSON，
    直接 json.loads 会失败。

    本函数：
    1. 去除开头的 ```json / ```jsonc / ``` 行
    2. 去除结尾的 ``` 行
    3. 保留中间的纯 JSON 内容
    4. 如果不含 markdown fence，原样返回（无副作用）
    """
    if not text or "```" not in text:
        return text
    stripped = text.strip()
    import re
    # 完整匹配：开头 ```json/``` + 中间内容 + 结尾 ```
    m = re.match(r'^\s*```(?:json|jsonc|JSON|JSONC)?\s*\n([\s\S]*?)\n\s*```\s*$', stripped)
    if m:
        return m.group(1).strip()
    # 只有开头 ```json，没有结尾 ```（部分场景）
    m = re.match(r'^\s*```(?:json|jsonc|JSON|JSONC)?\s*\n([\s\S]*)$', stripped)
    if m:
        content = m.group(1)
        if content.rstrip().endswith('```'):
            content = content.rstrip()[:-3].rstrip()
        return content.strip()
    return text


def _apply_json_response_format_stripping(result: dict, payload: dict) -> dict:
    """如果请求指定了 response_format=json_object/json_schema，
    剥离响应内容里 GLM 习惯性包裹的 markdown 代码块。

    非流式路径调用：直接修改 result dict 并返回。
    """
    response_format = payload.get("response_format")
    if not isinstance(response_format, dict):
        return result
    rf_type = str(response_format.get("type", "")).strip().lower()
    if rf_type not in ("json_object", "json_schema"):
        return result
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        return result
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return result
    content = message.get("content")
    if isinstance(content, str) and content:
        stripped = _strip_markdown_fences(content)
        if stripped != content:
            message["content"] = stripped
    return result


def _safe_error_message(exc: Exception, default: str = "Internal server error") -> str:
    """v52 P1: 生成安全的对外错误消息，避免泄漏内部实现细节。

    - UpstreamAPIError / ValueError：业务/参数错误，返回简化消息（客户端需要知道）
    - 其他 Exception：返回通用消息，str(exc) 只记日志

    安全过滤：移除可能包含的文件路径、Python 内部信息、stack trace 片段。
    """
    # 业务错误（上游 GLM 返回的错误）— 客户端需要知道状态码和简短消息
    if isinstance(exc, UpstreamAPIError):
        # exc.message 已经过 _build_error_message 格式化，相对安全
        # 但仍要截断防止过长
        msg = str(exc.message) if hasattr(exc, 'message') else str(exc)
        return msg[:500] if len(msg) > 500 else msg
    # 参数错误 — 客户端需要知道哪个参数错了
    if isinstance(exc, ValueError):
        msg = str(exc)
        # 过滤掉可能的文件路径（/home/... /app/... 等）
        import re
        msg = re.sub(r'/[^\s]+\.(py|json|txt|env)\S*', '<file>', msg)
        return msg[:500] if len(msg) > 500 else msg
    # 其他异常（KeyError/TypeError/AttributeError 等）— 屏蔽，返回通用消息
    return default


class _RateLimiter:
    """v52 P2: 简单的滑动窗口 rate limiter（per-key 限流）。

    - 默认 60 req/min，可通过环境变量 API_RATE_LIMIT_PER_MINUTE 配置（0=关闭）
    - 线程安全，用 threading.Lock 保护
    - 内存效率：每个 key 只保留最近 60s 的请求时间戳列表
    """
    _instance: _RateLimiter | None = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key_hash -> list of timestamps
        self._windows: dict[str, list[float]] = {}
        try:
            self._limit = int(os.environ.get("API_RATE_LIMIT_PER_MINUTE", "60"))
        except (ValueError, TypeError):
            self._limit = 60
        self._window_seconds = 60.0  # 1 分钟窗口

    @classmethod
    def get_instance(cls) -> _RateLimiter:
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = _RateLimiter()
        return cls._instance

    def check(self, key: str) -> tuple[bool, int]:
        """检查 key 是否超限。返回 (allowed, retry_after_seconds)。

        allowed=True 时 retry_after=0
        allowed=False 时 retry_after=距最早请求过期的时间（秒）
        """
        if self._limit <= 0:
            return True, 0  # 限流关闭

        key_hash = hashlib.sha256(key.encode()).hexdigest()[:16]
        now = time.time()
        cutoff = now - self._window_seconds

        with self._lock:
            # 清理过期时间戳
            window = self._windows.get(key_hash, [])
            # 移除超过窗口的时间戳
            window = [ts for ts in window if ts > cutoff]
            self._windows[key_hash] = window

            if len(window) >= self._limit:
                # 超限，计算最早请求何时过期
                oldest = window[0] if window else now
                retry_after = int(oldest + self._window_seconds - now) + 1
                return False, max(1, retry_after)

            # 未超限，记录当前请求
            window.append(now)
            return True, 0


# v35: WAF bypass — 递归还原请求体中的 ˋ (U+02CB) → ` (U+0060)
_BACKTICK_SAFE_CHAR = '\u02cb'  # ˋ MODIFIER LETTER GRAVE ACCENT
_BACKTICK_REAL_CHAR = '\x60'    # ` GRAVE ACCENT


def _restore_backticks_in_payload(obj: object) -> None:
    """递归遍历请求体 dict/list/str，把 ˋ (U+02CB) 还原成 ` (U+0060)。

    配合 scripts/waf_bypass_proxy.py 使用：
    - 代理脚本把 ` 替换成 ˋ 绕过 Cloudflare WAF
    - 本函数在请求到达业务逻辑之前还原

    对没用 bypass 代理的请求无副作用（正常 prompt 不含 ˋ 字符）。
    """
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, str):
                if _BACKTICK_SAFE_CHAR in val:
                    obj[key] = val.replace(_BACKTICK_SAFE_CHAR, _BACKTICK_REAL_CHAR)
            elif isinstance(val, (dict, list)):
                _restore_backticks_in_payload(val)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                if _BACKTICK_SAFE_CHAR in item:
                    obj[i] = item.replace(_BACKTICK_SAFE_CHAR, _BACKTICK_REAL_CHAR)
            elif isinstance(item, (dict, list)):
                _restore_backticks_in_payload(item)


class GLM2APIServer:
    def __init__(self, config: AppConfig, glm_client: GLMWebClient, logger: Logger) -> None:
        self.config = config
        self.glm_client = glm_client
        self.logger = logger
        handler_cls = self._build_handler()
        self._server = ThreadingHTTPServer((config.host, config.port), handler_cls)
        self._server.daemon_threads = True
        self._server.allow_reuse_address = True

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _build_handler(self):
        config = self.config
        glm_client = self.glm_client
        logger = self.logger

        class RequestHandler(BaseHTTPRequestHandler):
            # v34 修复：隐藏 server_version 暴露的逆向特征
            # 之前 "glm2api/0.1.0" 直接告诉客户端这是 glm2api 项目
            # 官方 OpenAI 用 "cloudflare"，我们也用 cloudflare（Render 也用 cloudflare，一致）
            server_version = "cloudflare"
            sys_version = ""  # 清空 Python 版本信息
            protocol_version = "HTTP/1.1"

            def do_OPTIONS(self) -> None:
                # Admin panel also handles OPTIONS (CORS preflight)
                if handle_admin_request(self, config, glm_client, logger):
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self._send_common_headers()
                self.end_headers()

            def do_HEAD(self) -> None:
                """处理 HEAD 请求（只返回 header 不返回 body）。

                修复 v33 审计 P2-1：之前未实现 do_HEAD，curl -I / SDK 健康探测
                会触发 501 Unsupported method ('HEAD')，破坏 keep-alive 连接复用。

                v49: HEAD / 也返回 200（Claude Code 等客户端用 HEAD / 做健康探测，
                返回 405 会被当作 endpoint 不可用）

                支持的 HEAD 路径：
                - / 或 /health → 200 OK（无 body）
                - /v1/models → 200 OK（无 body，需要 API key 认证）
                - /admin/* → 转发到 admin handler
                - 其他 → 405 Method Not Allowed
                """
                self._admin_begin_request("HEAD")
                try:
                    if handle_admin_request(self, config, glm_client, logger):
                        return
                    path = self._path_without_query()
                    # v49: HEAD / 返回 200（Claude Code 健康探测）
                    if path == "/" or path == "/health":
                        self.send_response(HTTPStatus.OK)
                        self._send_common_headers()
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", "15")  # {"status":"ok"}
                        self.end_headers()
                        return
                    # v48: 账号池健康检查端点
                    if path == "/health/accounts":
                        auth = glm_client.auth
                        accounts_info = auth.get_all_accounts_info()
                        total = len(accounts_info)
                        # 有 cached_token 且未过期 = 可用
                        usable = sum(1 for a in accounts_info if a.get("has_refresh_token") or a.get("is_guest"))
                        # 有 cached_token = 已就绪
                        ready = 0
                        for idx in range(total):
                            try:
                                acc = auth._accounts[idx]
                                if acc.cached_token and acc.cached_token.expires_at > __import__('time').time() + 5:
                                    ready += 1
                            except Exception:
                                pass
                        import json as _json
                        health_body = _json.dumps({
                            "status": "ok" if ready > 0 else "degraded",
                            "total_accounts": total,
                            "usable_accounts": usable,
                            "ready_accounts": ready,
                            "guest_mode": any(a.get("is_guest") for a in accounts_info),
                        }).encode("utf-8")
                        self.send_response(HTTPStatus.OK)
                        self._send_common_headers()
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(health_body)))
                        self.end_headers()
                        self.wfile.write(health_body)
                        return
                    if path == f"{config.api_prefix}/models":
                        if not self._authorize():
                            # v49 BUG1: 之前只 return 不发响应，客户端收到空响应报 501
                            self._write_json(
                                HTTPStatus.UNAUTHORIZED,
                                make_error(
                                    "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                                    error_type=ERROR_AUTHENTICATION,
                                    code="invalid_api_key",
                                    request_id=gen_request_id(),
                                ),
                            )
                            return
                        self.send_response(HTTPStatus.OK)
                        self._send_common_headers()
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    # 其他路径不支持 HEAD
                    self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
                    self._send_common_headers()
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Allow", "GET, POST, OPTIONS")
                    body = json.dumps({"error": {"message": "HEAD method not allowed for this path", "type": "invalid_request_error", "code": "method_not_allowed"}}, ensure_ascii=False).encode("utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 HEAD 响应写回前断开 path=%s", self.path)
                except Exception as exc:
                    logger.error("HEAD 请求处理失败 path=%s error=%s\n%s", self.path, exc, traceback.format_exc())
                    try:
                        # v52 P1: 不泄漏 str(exc)，返回通用消息
                        self._safe_write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "Internal server error", "type": "server_error"}})
                    except Exception:
                        pass

            def do_GET(self) -> None:
                self._admin_begin_request("GET")
                try:
                    self._debug_log_request_start()
                    # Admin panel (login page / static / admin API)
                    if handle_admin_request(self, config, glm_client, logger):
                        return
                    path = self._path_without_query()
                    if path == "/health":
                        self._write_json(HTTPStatus.OK, {"status": "ok"})
                        return

                    # 硬限制：所有 /v1/ API 端点都需要 API key 认证（和官方版 API 一样）
                    if path.startswith(f"{config.api_prefix}/"):
                        if not self._authorize():
                            logger.warning("认证失败 path=%s ip=%s", self.path, self.client_address[0])
                            self._write_json(
                                HTTPStatus.UNAUTHORIZED,
                                make_error(
                                    "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                                    error_type=ERROR_AUTHENTICATION,
                                    code="invalid_api_key",
                                    request_id=gen_request_id(),
                                ),
                            )
                            return

                    if path == f"{config.api_prefix}/models":
                        self._write_json(
                            HTTPStatus.OK,
                            {
                                "object": "list",
                                "data": [
                                    self._model_info(model) for model in self._get_effective_exposed_models()
                                ],
                            },
                        )
                        return

                    # GET /v1/models/{model} - retrieve model info (OpenAI-compatible)
                    if path.startswith(f"{config.api_prefix}/models/"):
                        model_id = path[len(f"{config.api_prefix}/models/"):]
                        if model_id in self._get_effective_exposed_models():
                            self._write_json(HTTPStatus.OK, self._model_info(model_id))
                            return
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            make_error(
                                f"The model '{model_id}' does not exist",
                                error_type=ERROR_INVALID_REQUEST,
                                param="model",
                                code="model_not_found",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    logger.debug("GET 未匹配 path=%s", self.path)
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        make_error(
                            "Unknown endpoint",
                            error_type=ERROR_NOT_FOUND,
                            code="not_found",
                            request_id=gen_request_id(),
                        ),
                    )
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 GET 响应写回前断开 path=%s", self.path)
                except Exception as exc:
                    logger.error("处理 GET 请求失败 path=%s error=%s\n%s", self.path, exc, traceback.format_exc())
                    self._safe_write_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        make_error(
                            f"Internal server error: {exc}",
                            error_type=ERROR_SERVER,
                            code="internal_error",
                            request_id=gen_request_id(),
                        ),
                    )
                finally:
                    self._admin_finalize_request()

            def do_POST(self) -> None:
                self._admin_begin_request("POST")
                try:
                    self._debug_log_request_start()
                    # Admin panel (login / admin API)
                    if handle_admin_request(self, config, glm_client, logger):
                        return
                    path = self._path_without_query()
                    # No-auth endpoints: only OpenAI-compatible stubs that don't need upstream
                    openai_endpoints = {
                        f"{config.api_prefix}/chat/completions",
                        f"{config.api_prefix}/completions",
                        f"{config.api_prefix}/images/generations",
                        f"{config.api_prefix}/images/edits",
                        f"{config.api_prefix}/images/variations",
                        f"{config.api_prefix}/messages",
                        f"{config.api_prefix}/messages/count_tokens",
                        f"{config.api_prefix}/responses",
                        f"{config.api_prefix}/responses_v2",
                        "/v2/responses",
                        f"{config.api_prefix}/embeddings",
                        f"{config.api_prefix}/moderations",
                        f"{config.api_prefix}/audio/transcriptions",
                        f"{config.api_prefix}/audio/translations",
                        f"{config.api_prefix}/audio/speech",
                        f"{config.api_prefix}/files",
                        f"{config.api_prefix}/assistants",
                        f"{config.api_prefix}/threads",
                    }
                    if path not in openai_endpoints:
                        logger.debug("POST 未匹配 path=%s", self.path)
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            make_error(
                                "Unknown endpoint",
                                error_type=ERROR_NOT_FOUND,
                                code="not_found",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    if not self._authorize():
                        logger.warning("认证失败 path=%s ip=%s", self.path, self.client_address[0])
                        self._write_json(
                            HTTPStatus.UNAUTHORIZED,
                            make_error(
                                "Incorrect API key provided. You can find your API key at https://platform.openai.com/account/api-keys.",
                                error_type=ERROR_AUTHENTICATION,
                                code="invalid_api_key",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # v52 P2: per-key rate limiting（防 DoS，默认 60 req/min）
                    # 用认证时记录的 _admin_api_key 作为限流 key
                    rate_key = self._admin_api_key or self.client_address[0]
                    rate_limiter = _RateLimiter.get_instance()
                    allowed, retry_after = rate_limiter.check(rate_key)
                    if not allowed:
                        logger.warning("API 限流触发 key=%s... retry_after=%ss", rate_key[:8], retry_after)
                        self._write_json_with_retry(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            make_error(
                                f"Rate limit exceeded. Please retry after {retry_after} seconds.",
                                error_type=ERROR_SERVER,
                                code="rate_limit_exceeded",
                                request_id=gen_request_id(),
                            ),
                            retry_after=retry_after,
                        )
                        return

                    content_length = self._parse_content_length()
                    if content_length < 0:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                "Content-Length cannot be negative",
                                error_type=ERROR_INVALID_REQUEST,
                                param="content_length",
                                code="invalid_content_length",
                                request_id=gen_request_id(),
                            ),
                        )
                        return
                    # v52 P1: 请求体大小限制（防 DoS / OOM）
                    # 默认 10MB，可通过环境变量 MAX_BODY_SIZE_MB 配置（0=不限制）
                    try:
                        max_body_mb = int(os.environ.get("MAX_BODY_SIZE_MB", "10"))
                    except (ValueError, TypeError):
                        max_body_mb = 10
                    if max_body_mb > 0 and content_length > max_body_mb * 1024 * 1024:
                        logger.warning("请求体过大 path=%s size=%s limit=%sMB", self.path, content_length, max_body_mb)
                        self._write_json(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            make_error(
                                f"Request body too large (max {max_body_mb}MB)",
                                error_type=ERROR_INVALID_REQUEST,
                                param="content_length",
                                code="request_too_large",
                                request_id=gen_request_id(),
                            ),
                        )
                        return
                    raw_body = self.rfile.read(content_length) if content_length else b"{}"
                    debug_dump(logger, config.debug_dump_all, f"HTTP 入站原始请求体 path={self.path}", raw_body)
                    try:
                        payload = json.loads(raw_body.decode("utf-8"))
                    except UnicodeDecodeError:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                "Request body must be UTF-8 encoded",
                                error_type=ERROR_INVALID_REQUEST,
                                code="invalid_encoding",
                                request_id=gen_request_id(),
                            ),
                        )
                        return
                    except json.JSONDecodeError as exc:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                f"Invalid JSON: {exc.msg}",
                                error_type=ERROR_INVALID_REQUEST,
                                code="invalid_json",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    if not isinstance(payload, dict):
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                "Request body must be a JSON object",
                                error_type=ERROR_INVALID_REQUEST,
                                code="invalid_payload",
                                request_id=gen_request_id(),
                            ),
                        )
                        return
                    debug_dump(logger, config.debug_dump_all, f"HTTP 入站解析后 JSON path={self.path}", payload)
                    # v35/v54 H2: WAF bypass — 只在请求经过 bypass proxy 时还原 ˋ → `
                    # 之前无条件还原，导致直连用户 prompt 中的合法 ˋ（越南语/拼音/IPA）被误转
                    # bypass proxy 会在请求头加 X-WAF-Bypass: 1，据此判断
                    if self.headers.get("X-WAF-Bypass") == "1":
                        _restore_backticks_in_payload(payload)
                    # Record model + stream for admin metrics (best-effort)
                    if isinstance(payload.get("model"), str):
                        self._admin_model = str(payload["model"])
                    if payload.get("stream"):
                        self._admin_stream = True

                    # v51: STRICT_VALIDATION 开关（默认宽松，Claude Code 友好）
                    # 启用后严格校验 Content-Type 和 anthropic-version
                    strict_validation = os.environ.get("STRICT_VALIDATION", "").lower() in ("true", "1", "yes")

                    # v51 WARN1: 严格模式下校验 Content-Type
                    if strict_validation and path in {
                        f"{config.api_prefix}/messages",
                        f"{config.api_prefix}/chat/completions",
                        f"{config.api_prefix}/responses",
                        f"{config.api_prefix}/responses_v2",
                        f"{config.api_prefix}/completions",
                    }:
                        ct = self.headers.get("Content-Type", "")
                        if "application/json" not in ct.lower():
                            self._write_json(
                                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                make_error(
                                    "Content-Type must be application/json",
                                    error_type=ERROR_INVALID_REQUEST,
                                    param="Content-Type",
                                    code="invalid_content_type",
                                    request_id=gen_request_id(),
                                ),
                            )
                            return
                        # v51 WARN2: 严格模式下校验 anthropic-version（仅 /v1/messages）
                        if path == f"{config.api_prefix}/messages":
                            av = self.headers.get("anthropic-version", "")
                            if not av:
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        "anthropic-version header is required",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param="anthropic-version",
                                        code="missing_anthropic_version",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return

                    # v49/v50: 统一输入校验
                    # 适用于所有 chat-like 端点（messages/responses/responses_v2/chat/completions/completions）
                    chat_like_endpoints = {
                        f"{config.api_prefix}/messages",
                        f"{config.api_prefix}/responses",
                        f"{config.api_prefix}/responses_v2",
                        "/v2/responses",
                        f"{config.api_prefix}/chat/completions",
                        f"{config.api_prefix}/completions",
                    }
                    if path in chat_like_endpoints:
                        # v50 BUG1+BUG2: messages 完整类型校验（null/字符串/缺失/空数组）
                        # Responses v2 用 input 而非 messages，但 input 可以是字符串（简化格式）或数组
                        if "input" in payload and "messages" not in payload:
                            msgs = payload.get("input")
                            msgs_key = "input"
                            # input 可以是字符串（简化格式）或数组，字符串不校验数组规则
                            input_is_string = isinstance(msgs, str)
                        else:
                            msgs = payload.get("messages")
                            msgs_key = "messages"
                            input_is_string = False

                        if msgs is None:
                            # messages=null 或字段缺失
                            self._write_json(
                                HTTPStatus.BAD_REQUEST,
                                make_error(
                                    f"{msgs_key} is required and must be an array",
                                    error_type=ERROR_INVALID_REQUEST,
                                    param=msgs_key,
                                    code="invalid_request",
                                    request_id=gen_request_id(),
                                ),
                            )
                            return
                        if not input_is_string:
                            # 非 Responses input 字符串场景：必须是数组
                            if not isinstance(msgs, list):
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"{msgs_key} must be an array (got {type(msgs).__name__})",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param=msgs_key,
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return
                            # v49 BUG3: 空数组
                            if len(msgs) == 0:
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"{msgs_key} array must not be empty",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param=msgs_key,
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return

                        # v49 BUG4 + v50 BUG3: max_tokens 校验（0/负数/超大值/非整数）
                        # Anthropic 用 max_tokens，OpenAI 用 max_tokens，Responses 用 max_output_tokens
                        mt_keys = ("max_tokens", "max_output_tokens")
                        MAX_TOKENS_LIMIT = 32768  # GLM 上游通常支持到 32768
                        for mt_key in mt_keys:
                            if mt_key not in payload:
                                continue
                            mt_val = payload.get(mt_key)
                            if mt_val is None:
                                continue
                            try:
                                mt_int = int(mt_val)
                            except (ValueError, TypeError):
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"{mt_key} must be an integer (got {mt_val!r})",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param=mt_key,
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return
                            if mt_int <= 0:
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"{mt_key} must be a positive integer (got {mt_int})",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param=mt_key,
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return
                            # v50 BUG3: 超大值 clamp 到 MAX_TOKENS_LIMIT（而非报错，Claude Code 友好）
                            if mt_int > MAX_TOKENS_LIMIT:
                                logger.info("%s=%s 超过上限，clamp 到 %s", mt_key, mt_int, MAX_TOKENS_LIMIT)
                                payload[mt_key] = MAX_TOKENS_LIMIT

                        # v50 WARN3: temperature / top_p 范围校验（clamp 而非报错）
                        temp_val = payload.get("temperature")
                        if temp_val is not None:
                            try:
                                temp_float = float(temp_val)
                                if temp_float < 0 or temp_float > 2:
                                    clamped = max(0.0, min(2.0, temp_float))
                                    logger.info("temperature=%s 超出 [0,2]，clamp 到 %s", temp_float, clamped)
                                    payload["temperature"] = clamped
                            except (ValueError, TypeError):
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"temperature must be a number (got {temp_val!r})",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param="temperature",
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return
                        top_p_val = payload.get("top_p")
                        if top_p_val is not None:
                            try:
                                top_p_float = float(top_p_val)
                                if top_p_float < 0 or top_p_float > 1:
                                    clamped = max(0.0, min(1.0, top_p_float))
                                    logger.info("top_p=%s 超出 [0,1]，clamp 到 %s", top_p_float, clamped)
                                    payload["top_p"] = clamped
                            except (ValueError, TypeError):
                                self._write_json(
                                    HTTPStatus.BAD_REQUEST,
                                    make_error(
                                        f"top_p must be a number (got {top_p_val!r})",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param="top_p",
                                        code="invalid_request",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return

                        # v49 WARN: 无 model 字段 fallback 改为 glm-5.2-flash（之前是 glm-4 旧模型）
                        model_val = payload.get("model")
                        if not model_val or not isinstance(model_val, str) or not model_val.strip():
                            payload["model"] = "glm-5.2-flash"
                            logger.info("请求未指定 model，fallback 到 glm-5.2-flash")

                        # v49 BUG2: 不存在的模型校验
                        # 默认宽松模式：未知模型 fallback 到 glm-5.2-flash（Claude Code 友好）
                        # STRICT_MODEL_VALIDATION=true 时严格 404
                        model_id_check = str(payload.get("model", ""))
                        strict_model_validation = os.environ.get("STRICT_MODEL_VALIDATION", "").lower() in ("true", "1", "yes")
                        if model_id_check and model_id_check not in self._get_effective_exposed_models():
                            if strict_model_validation:
                                self._write_json(
                                    HTTPStatus.NOT_FOUND,
                                    make_error(
                                        f"The model '{model_id_check}' does not exist",
                                        error_type=ERROR_INVALID_REQUEST,
                                        param="model",
                                        code="model_not_found",
                                        request_id=gen_request_id(),
                                    ),
                                )
                                return
                            else:
                                # 宽松模式：fallback 到 glm-5.2-flash 并记录
                                logger.info("未知 model=%s fallback 到 glm-5.2-flash", model_id_check)
                                payload["model"] = "glm-5.2-flash"

                    # --- Anthropic Messages API ---
                    if path == f"{config.api_prefix}/messages":
                        logger.info("收到 Anthropic 请求 model=%s stream=%s", payload.get("model"), payload.get("stream"))
                        self._handle_anthropic_messages(payload)
                        return

                    # --- Anthropic count_tokens API (v39 P0-2) ---
                    # Claude Code 用它做 context window 估算，减少不必要的截断
                    if path == f"{config.api_prefix}/messages/count_tokens":
                        self._handle_anthropic_count_tokens(payload)
                        return

                    # --- OpenAI Responses API v1 (legacy) ---
                    if path == f"{config.api_prefix}/responses":
                        logger.info("收到 Responses v1 请求 model=%s stream=%s", payload.get("model"), payload.get("stream"))
                        self._handle_responses(payload)
                        return

                    # --- OpenAI Responses API v2 (新版，完整适配 2025 规范) ---
                    if path == f"{config.api_prefix}/responses_v2" or path == "/v2/responses":
                        logger.info("收到 Responses v2 请求 model=%s stream=%s", payload.get("model"), payload.get("stream"))
                        self._handle_responses_v2(payload)
                        return

                    # --- Image generation ---
                    if path == f"{config.api_prefix}/images/generations":
                        if not payload.get("prompt"):
                            self._write_json(
                                HTTPStatus.BAD_REQUEST,
                                make_error(
                                    "you must provide a prompt parameter",
                                    error_type=ERROR_INVALID_REQUEST,
                                    param="prompt",
                                    request_id=gen_request_id(),
                                ),
                            )
                            return
                        logger.info("收到绘图请求 model=%s prompt=%s", payload.get("model"), payload.get("prompt"))
                        result = glm_client.generate_images(payload)
                        self._write_json(HTTPStatus.OK, result)
                        return

                    # --- OpenAI Moderations API (stub - always returns safe) ---
                    if path == f"{config.api_prefix}/moderations":
                        self._handle_moderations(payload)
                        return

                    # --- OpenAI Embeddings API (stub - returns hashed pseudo-vectors) ---
                    if path == f"{config.api_prefix}/embeddings":
                        self._handle_embeddings(payload)
                        return

                    # --- OpenAI Audio Speech (stub - 503 since no TTS upstream) ---
                    if path == f"{config.api_prefix}/audio/speech":
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "Audio speech (TTS) is not supported by this glm2api deployment.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Audio transcriptions/translations (stub - 503) ---
                    if path in {f"{config.api_prefix}/audio/transcriptions", f"{config.api_prefix}/audio/translations"}:
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "Audio transcription is not supported by this glm2api deployment. Use vision-capable GLM-4V models with audio input instead.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Images: edits/variations (stub - 503 since no upstream support) ---
                    if path in {f"{config.api_prefix}/images/edits", f"{config.api_prefix}/images/variations"}:
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "Image edit/variation is not supported. Use /v1/images/generations instead.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Files (stub) ---
                    if path == f"{config.api_prefix}/files":
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "File uploads are not supported. Use base64-encoded image_url content in chat completions.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Assistants V1 (stub) ---
                    if path == f"{config.api_prefix}/assistants":
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "Assistants API is not supported. Use chat/completions with tools instead.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Threads (stub) ---
                    if path == f"{config.api_prefix}/threads":
                        self._write_json(
                            HTTPStatus.NOT_IMPLEMENTED,
                            make_error(
                                "Threads API is not supported. Use chat/completions with multi-turn messages instead.",
                                error_type=ERROR_INVALID_REQUEST,
                                code="not_supported",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # --- Legacy text completions (/v1/completions) ---
                    if path == f"{config.api_prefix}/completions":
                        self._handle_legacy_completion(payload)
                        return

                    # --- Chat completions ---
                    # v49/v50: 校验已在上面的 chat_like_endpoints 统一处理（messages/model/max_tokens/temperature/top_p）
                    if payload.get("stream"):
                        self._stream_completion(payload)
                        return

                    logger.info("收到 chat 请求 model=%s", payload.get("model"))
                    result, conversation_id = glm_client.chat_completion(payload)
                    # v33 P2-3 修复：response_format=json_object/json_schema 时剥离 markdown 代码块
                    result = _apply_json_response_format_stripping(result, payload)
                    # 记录 token 使用量到 admin store（用于仪表盘 KPI）
                    self._record_token_usage(result)
                    # 动态模型发现：从上游响应提取真实模型名，加入动态注册表
                    # 用户核心诉求：未来 chatglm.cn 升级到 GLM-5.3 时无需改代码，自动显示
                    self._discover_dynamic_model(result)
                    self._write_json(HTTPStatus.OK, result)
                except QueueTimeoutError as exc:
                    logger.warning("GLM 队列等待超时 error=%s", exc)
                    # v47: 加 Retry-After header，让客户端知道等多久
                    self._write_json_with_retry(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        make_error(
                            f"Service temporarily unavailable: {exc}",
                            error_type=ERROR_SERVER,
                            code="queue_timeout",
                            request_id=gen_request_id(),
                        ),
                        retry_after=30,
                    )
                except UpstreamAPIError as exc:
                    logger.warning("上游 GLM 返回错误 status=%s error=%s", exc.status_code, exc)
                    status = self._safe_http_status(exc.status_code, fallback=HTTPStatus.BAD_GATEWAY)
                    self._write_json(
                        status,
                        make_error(
                            _safe_error_message(exc, "Upstream service error"),
                            error_type=ERROR_UPSTREAM,
                            code="upstream_error",
                            request_id=gen_request_id(),
                        ),
                    )
                except RuntimeError as exc:
                    # v47 P0: 所有账号不可用时立即返回 503 + Retry-After
                    # 之前等 10-30 分钟 timeout 才返回，现在立即返回
                    exc_str = str(exc)
                    if "不可用" in exc_str or "账号" in exc_str:
                        logger.error("账号池不可用，快速返回 503 error=%s", exc)
                        self._write_json_with_retry(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            make_error(
                                "All accounts unavailable. Please retry later.",
                                error_type=ERROR_SERVER,
                                code="no_available_account",
                                request_id=gen_request_id(),
                            ),
                            retry_after=60,
                        )
                    else:
                        raise  # 其他 RuntimeError 继续抛出
                except ValueError as exc:
                    logger.warning("请求参数错误 path=%s error=%s", self.path, exc)
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        make_error(
                            _safe_error_message(exc, "Invalid request parameter"),
                            error_type=ERROR_INVALID_REQUEST,
                            code="invalid_request",
                            request_id=gen_request_id(),
                        ),
                    )
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端连接提前断开 path=%s error=%s", self.path, exc)
                    self._admin_error = f"client_disconnected: {exc}"
                except Exception as exc:
                    exc_str = str(exc)
                    self._admin_error = exc_str
                    # v47 P0: 账号不可用时返回 503 + Retry-After（不是 502）
                    if "不可用" in exc_str or "账号" in exc_str or "no_available" in exc_str:
                        logger.error("账号池不可用，快速返回 503 error=%s", exc)
                        try:
                            self._write_json_with_retry(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                make_error(
                                    "All accounts unavailable. Please retry later.",
                                    error_type=ERROR_SERVER,
                                    code="no_available_account",
                                    request_id=gen_request_id(),
                                ),
                                retry_after=60,
                            )
                        except _CLIENT_DISCONNECTED:
                            pass
                    else:
                        logger.error("处理请求失败 error=%s\n%s", exc, traceback.format_exc())
                        # v52 P1: 不泄漏 str(exc) 和 Python 类名，返回通用消息
                        self._safe_write_json(
                            HTTPStatus.BAD_GATEWAY,
                            make_error(
                                "Upstream service error. Please retry later.",
                                error_type=ERROR_UPSTREAM,
                                code="upstream_error",
                                request_id=gen_request_id(),
                            ),
                        )
                finally:
                    self._admin_finalize_request()

            # ---- Admin metrics helpers ----

            def _admin_begin_request(self, method: str) -> None:
                """Capture start time + initialize tracking fields for admin metrics."""
                self._admin_start = time.monotonic()
                self._admin_method = method
                self._admin_status = 200
                self._admin_model = ""
                self._admin_api_key = getattr(self, "_admin_api_key", "")  # 保留 auth 阶段设置的 key
                self._admin_account = -1
                self._admin_stream = False
                self._admin_error = ""
                self._admin_request_id = gen_request_id()
                # Wrap send_response to capture status code transparently
                if not getattr(self, "_admin_send_response_wrapped", False):
                    _orig_send_response = self.send_response

                    def _tracked_send_response(code, message=None):
                        try:
                            self._admin_status = int(code)
                        except (TypeError, ValueError):
                            pass
                        return _orig_send_response(code, message)

                    self.send_response = _tracked_send_response
                    self._admin_send_response_wrapped = True

            def _admin_finalize_request(self) -> None:
                """Record this request in the admin store (skip admin paths themselves)."""
                try:
                    path = self._path_without_query()
                    if path.startswith("/admin"):
                        return  # don't track admin panel traffic
                    # If account wasn't set explicitly, ask glm_client for the
                    # thread-local last-account-index (set by _call_with_account_failover).
                    account_index = getattr(self, "_admin_account", -1)
                    if account_index < 0:
                        try:
                            account_index = glm_client.get_last_account_index()
                        except Exception:
                            account_index = -1
                    duration_ms = int((time.monotonic() - self._admin_start) * 1000)
                    # 获取 API key 的掩码名称用于日志显示
                    api_key_display = ""
                    raw_key = getattr(self, "_admin_api_key", "")
                    if raw_key:
                        # 掩码处理：前8位...后4位
                        if len(raw_key) > 16:
                            api_key_display = raw_key[:8] + "..." + raw_key[-4:]
                        else:
                            api_key_display = raw_key[:4] + "..."
                    admin_record_request(
                        method=getattr(self, "_admin_method", ""),
                        path=path,
                        protocol=admin_classify_protocol(path),
                        model=getattr(self, "_admin_model", ""),
                        status=getattr(self, "_admin_status", 200),
                        duration_ms=duration_ms,
                        client_ip=self.client_address[0] if self.client_address else "",
                        account_index=account_index,
                        stream=getattr(self, "_admin_stream", False),
                        error=getattr(self, "_admin_error", ""),
                        request_id=getattr(self, "_admin_request_id", ""),
                        api_key=api_key_display,
                    )
                except Exception:
                    pass  # never let metrics break the request

            # ---- Anthropic Messages API ----

            def _handle_anthropic_count_tokens(self, payload: dict[str, object]) -> None:
                """处理 POST /v1/messages/count_tokens 请求。

                v39 P0-2: Claude Code 用这个端点做 context window 估算，
                减少不必要的截断。如果不实现，Claude Code 会用默认估算，
                可能导致长任务被提前截断（用户反馈的"长任务断开"问题之一）。

                请求格式与 /v1/messages 相同（model + messages + system），
                响应格式：{"input_tokens": <int>}
                """
                try:
                    openai_payload = anthropic_to_openai(payload)
                    messages = openai_payload.get("messages", [])
                    tools = openai_payload.get("tools")
                    total_tokens = 0
                    if isinstance(messages, list):
                        total_tokens = estimate_message_tokens(messages)
                    if tools and isinstance(tools, list):
                        from .core.tokenizer import estimate_tools_tokens
                        total_tokens += estimate_tools_tokens(tools)
                    total_tokens = max(1, total_tokens)
                    self._write_json(HTTPStatus.OK, {"input_tokens": total_tokens})
                except Exception as exc:
                    logger.error("count_tokens 请求处理失败 error=%s", exc)
                    self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                        make_error(f"count_tokens failed: {exc}", error_type="server_error",
                                   code="count_tokens_error", request_id=gen_request_id()))

            def _handle_anthropic_messages(self, payload: dict[str, object]) -> None:
                model = str(payload.get("model", "glm-5.2-flash"))
                openai_payload = anthropic_to_openai(payload)

                # v49: model 校验已在上层 chat_like_endpoints 统一处理
                if payload.get("stream"):
                    self._stream_anthropic(openai_payload, model, payload)
                    return

                result, _ = glm_client.chat_completion(openai_payload)
                # P1-1: 传递 stop_sequences 给 Anthropic 响应转换
                stop_seqs = payload.get("stop_sequences")
                if isinstance(stop_seqs, str):
                    stop_seqs = [stop_seqs]
                response = openai_to_anthropic_response(result, model, stop_sequences=stop_seqs if isinstance(stop_seqs, list) else None)
                self._record_token_usage(result)
                self._discover_dynamic_model(result)
                self._write_json(HTTPStatus.OK, response)

            def _stream_anthropic(self, openai_payload: dict[str, object], model: str, payload: dict[str, object] | None = None) -> None:
                """v34 修复：Anthropic 流式响应完整重写，解决长任务断开问题。

                之前的问题（v36-v40 审计未发现的根因）：
                1. message_start 延迟发送 — 等 GLM 第一个 chunk 才发，GLM 思考 30s 时
                   Claude Code 30s 收不到任何事件 → 认为连接断开 → 长任务断开
                2. 无 ping 心跳 — 官方 Anthropic API 周期性发 event: ping，
                   我们没有 → 长时间无数据时 Claude Code 超时断开
                3. 同步阻塞读 — 主线程直接读 stream_iter，GLM 慢时无法发心跳

                修复：
                1. message_start 立即发送（不等第一个 chunk）
                2. 后台线程读上游 chunks（不阻塞主线程）
                3. 主线程周期性发 ping 心跳（2s 间隔，与 Responses v2 一致）
                """
                openai_payload["stream"] = True
                # v53 P0: 传入 stop_sequences 给流式 accumulator，支持匹配 stop_sequence 字符串
                stream_stop_seqs = payload.get("stop_sequences") if isinstance(payload, dict) else None
                if isinstance(stream_stop_seqs, str):
                    stream_stop_seqs = [stream_stop_seqs]
                accumulator = AnthropicStreamAccumulator(model=model, stop_sequences=stream_stop_seqs if isinstance(stream_stop_seqs, list) else None)

                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
                self.end_headers()

                # v34 P0-1: 立即发送 message_start（不等第一个 chunk）
                # 这是长任务不断开的关键 — Claude Code 收到 message_start 就知道连接建立了
                try:
                    start_event = accumulator.start_message()
                    self.wfile.write(start_event.encode("utf-8"))
                    self.wfile.flush()
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 Anthropic 流式启动阶段断开 model=%s", model)
                    return

                # v54: 后台线程读上游（stream_chat_completion 也在后台调用，避免主线程阻塞）
                chunk_queue, sentinel, close_upstream = self._start_stream_background(openai_payload)

                # 收集 OpenAI 原始 chunks 以便流结束后提取 usage
                collected_chunks: list[bytes] = []
                ANTHROPIC_PING_INTERVAL = 2.0  # 2 秒发一次 ping（与 Responses v2 一致）

                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=ANTHROPIC_PING_INTERVAL)
                        except queue.Empty:
                            # v34 P0-2: 超时未收到 chunk → 发 ping 心跳保活
                            if not self._write_heartbeat(b'event: ping\ndata: {"type": "ping"}\n\n'):
                                logger.warning("客户端在 ping 心跳阶段断开 model=%s", model)
                                close_upstream()
                                return
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
                        # 收集原始 chunk 用于后续 usage 提取
                        collected_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                        # 转换 OpenAI chunk → Anthropic SSE events
                        events = accumulator.feed_chunk(chunk)
                        for event in events:
                            try:
                                self.wfile.write(event.encode("utf-8"))
                                self.wfile.flush()
                            except _CLIENT_DISCONNECTED:
                                logger.warning("客户端在 Anthropic 流式响应过程中断开 model=%s", model)
                                close_upstream()
                                return
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Anthropic 流式响应过程中断开 model=%s error=%s", model, exc)
                    close_upstream()
                    return
                except QueueTimeoutError as exc:
                    # v56 P0: 等账号超时（60s），发 SSE error + message_stop 让客户端重试
                    logger.warning("等账号超时 model=%s error=%s", model, exc)
                    try:
                        err_event = (
                            'event: error\n'
                            f'data: {{"type":"error","error":{{"type":"overloaded_error","message":"Service temporarily unavailable. Please retry."}}}}\n\n'
                        )
                        self.wfile.write(err_event.encode("utf-8"))
                        self.wfile.flush()
                    except _CLIENT_DISCONNECTED:
                        pass
                except Exception as exc:
                    logger.error("Anthropic 流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())
                    # v56: 发 SSE error event 让客户端知道出错了
                    try:
                        err_event = (
                            'event: error\n'
                            f'data: {{"type":"error","error":{{"type":"api_error","message":"Internal server error. Please retry."}}}}\n\n'
                        )
                        self.wfile.write(err_event.encode("utf-8"))
                        self.wfile.flush()
                    except _CLIENT_DISCONNECTED:
                        pass

                # Ensure message_stop is always sent (idempotent via _finished flag)
                if accumulator.started:
                    try:
                        for event in accumulator._finish():
                            self.wfile.write(event.encode("utf-8"))
                            self.wfile.flush()
                    except _CLIENT_DISCONNECTED:
                        pass

                # 流结束后记录 token 用量
                self._record_streaming_token_usage(collected_chunks)
                logger.info("Anthropic 流式请求完成 model=%s", model)

            # ---- OpenAI Responses API ----

            def _handle_responses(self, payload: dict[str, object]) -> None:
                model = str(payload.get("model", "glm-4"))
                openai_payload = responses_to_openai(payload)

                if payload.get("stream"):
                    self._stream_responses(openai_payload, model)
                    return

                result, _ = glm_client.chat_completion(openai_payload)
                response = openai_to_responses(result, model)
                self._record_token_usage(result)
                self._discover_dynamic_model(result)
                self._write_json(HTTPStatus.OK, response)

            def _stream_responses(self, openai_payload: dict[str, object], model: str) -> None:
                openai_payload["stream"] = True
                accumulator = ResponsesStreamAccumulator(model=model)

                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
                self.end_headers()

                # Send response.created + response.in_progress IMMEDIATELY so that
                # clients with short stream-idle timeouts (e.g. codex CLI ~4s)
                # see the stream has started before the first upstream chunk.
                try:
                    for event in accumulator.start_response():
                        self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 Responses 流式响应启动阶段断开 model=%s", model)
                    return

                # v54: 后台线程读上游（含 stream_chat_completion 调用）
                chunk_queue, sentinel, close_upstream = self._start_stream_background(openai_payload)

                # 收集 OpenAI 原始 chunks 以便流结束后提取 usage
                collected_chunks: list[bytes] = []
                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=RESPONSES_STREAM_HEARTBEAT_SECONDS)
                        except queue.Empty:
                            if not self._write_heartbeat(b": keep-alive\n\n"):
                                logger.warning("客户端在 Responses 心跳阶段断开 model=%s", model)
                                close_upstream()
                                return
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
                        # 收集原始 chunk 用于后续 usage 提取
                        collected_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                        events = accumulator.feed_chunk(chunk)  # type: ignore[arg-type]
                        for event in events:
                            try:
                                self.wfile.write(event.encode("utf-8"))
                                self.wfile.flush()
                            except _CLIENT_DISCONNECTED:
                                logger.warning("客户端在 Responses 流式响应过程中断开 model=%s", model)
                                close_upstream()
                                return
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Responses 流式响应过程中断开 model=%s error=%s", model, exc)
                    close_upstream()
                    return
                except Exception as exc:
                    logger.error("Responses 流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())

                # Ensure response.completed is always sent (idempotent via _finished flag)
                if accumulator.started:
                    try:
                        for event in accumulator._finish():
                            self.wfile.write(event.encode("utf-8"))
                            self.wfile.flush()
                    except _CLIENT_DISCONNECTED:
                        pass

                # 流结束后记录 token 用量
                self._record_streaming_token_usage(collected_chunks)
                logger.info("Responses 流式请求完成 model=%s", model)

            # ---- OpenAI Responses API v2 (新版，完整适配 2025 规范) ----

            def _handle_responses_v2(self, payload: dict[str, object]) -> None:
                model = str(payload.get("model", "glm-4"))
                openai_payload = responses_v2_to_openai(payload)

                if payload.get("stream"):
                    self._stream_responses_v2(openai_payload, model, payload)
                    return

                result, _ = glm_client.chat_completion(openai_payload)
                response = openai_to_responses_v2(result, model, request_payload=payload)
                self._record_token_usage(result)
                self._discover_dynamic_model(result)
                self._write_json(HTTPStatus.OK, response)

            def _stream_responses_v2(
                self, openai_payload: dict[str, object], model: str, request_payload: dict[str, object]
            ) -> None:
                openai_payload["stream"] = True
                accumulator = ResponsesV2StreamAccumulator(model=model, request_payload=request_payload)

                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                # v54 P0: 立即发送 response.created + response.in_progress
                # 之前 v2 路径不发，导致 codex 等 SDK 在 GLM 思考阶段超时
                try:
                    for event_type, event_data in accumulator.consume_chunk({}):
                        sse_event = f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                        self.wfile.write(sse_event.encode("utf-8"))
                    self.wfile.flush()
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 Responses v2 流式响应启动阶段断开 model=%s", model)
                    return

                # v54: 后台线程读上游（含 stream_chat_completion 调用）
                chunk_queue, sentinel, close_upstream = self._start_stream_background(openai_payload)

                # 收集 OpenAI 原始 chunks 以便流结束后提取 usage
                collected_chunks: list[bytes] = []
                client_disconnected = False
                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=RESPONSES_STREAM_HEARTBEAT_SECONDS)
                        except queue.Empty:
                            if not self._write_heartbeat(b": keep-alive\n\n"):
                                logger.warning("客户端在 Responses v2 心跳阶段断开 model=%s", model)
                                client_disconnected = True
                                break
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
                        # 收集原始 chunk 用于后续 usage 提取
                        collected_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                        # chunk 是 OpenAI SSE 格式的 bytes，需要解析为 dict
                        try:
                            chunk_str = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                            # 解析 SSE data 行
                            for line in chunk_str.split("\n"):
                                if line.startswith("data: "):
                                    data_str = line[6:].strip()
                                    if data_str == "[DONE]":
                                        continue
                                    try:
                                        chunk_dict = json.loads(data_str)
                                        events = accumulator.consume_chunk(chunk_dict)
                                        for event_type, event_data in events:
                                            sse_event = f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                                            self.wfile.write(sse_event.encode("utf-8"))
                                            self.wfile.flush()
                                    except (json.JSONDecodeError, ValueError):
                                        pass
                                    except _CLIENT_DISCONNECTED:
                                        client_disconnected = True
                                        break
                            if client_disconnected:
                                break
                        except _CLIENT_DISCONNECTED:
                            client_disconnected = True
                            break
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Responses v2 流式响应过程中断开 model=%s error=%s", model, exc)
                    close_upstream()
                    return
                except Exception as exc:
                    logger.error("Responses v2 流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())

                if client_disconnected:
                    close_upstream()
                    return

                # v54 P0 C3: finalize 移到 try/except 之外，确保异常路径也发 response.completed
                try:
                    for event_type, event_data in accumulator.finalize():
                        sse_event = f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                        self.wfile.write(sse_event.encode("utf-8"))
                        self.wfile.flush()
                except _CLIENT_DISCONNECTED:
                    pass
                except Exception as exc:
                    logger.error("Responses v2 finalize 失败 model=%s error=%s", model, exc)

                # 流结束后记录 token 用量
                self._record_streaming_token_usage(collected_chunks)
                logger.info("Responses v2 流式请求完成 model=%s", model)

            # ---- Chat completions (original) ----

            def _stream_completion(self, payload: dict[str, object]) -> None:
                """v47: OpenAI Chat 流式重写 — 加心跳保活，解决 Codex CLI 超时断开。

                之前：for chunk in stream_iter 同步阻塞，GLM 慢时无数据 → Codex 超时
                现在：后台线程读上游 + 主线程 2s 心跳（与 Anthropic 路径一致）
                v54: stream_chat_completion 移入后台线程，客户端断连关闭上游
                """
                model = str(payload.get("model", "unknown"))
                logger.info("开始流式响应 model=%s", model)
                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                # v33 P2-3 修复：response_format=json_object/json_schema 时
                response_format = payload.get("response_format")
                is_json_mode = (
                    isinstance(response_format, dict)
                    and str(response_format.get("type", "")).strip().lower() in ("json_object", "json_schema")
                )

                # v54: 后台线程读上游（含 stream_chat_completion 调用）
                chunk_queue, sentinel, close_upstream = self._start_stream_background(payload)

                sent_done = False
                collected_chunks: list[bytes] = []
                json_mode_buffer: list[dict] = []
                CHAT_HEARTBEAT_INTERVAL = 2.0  # 2s 心跳（与 Anthropic 路径一致）

                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=CHAT_HEARTBEAT_INTERVAL)
                        except queue.Empty:
                            # v47: 超时未收到 chunk → 发 SSE 注释心跳保活
                            if not self._write_heartbeat(b": keep-alive\n\n"):
                                logger.warning("客户端在心跳阶段断开 model=%s", model)
                                close_upstream()
                                return
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
                        debug_dump(logger, config.debug_dump_all, f"HTTP 出站流式分片 model={model}", chunk)
                        chunk_bytes = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                        collected_chunks.append(chunk_bytes)
                        if is_json_mode:
                            # 解析 chunk 提取 content delta 缓冲，不直接发给客户端
                            try:
                                chunk_str = chunk_bytes.decode("utf-8", errors="replace")
                                for line in chunk_str.split("\n"):
                                    line = line.strip()
                                    if not line.startswith("data: "):
                                        continue
                                    data_str = line[6:].strip()
                                    if data_str == "[DONE]" or not data_str:
                                        continue
                                    try:
                                        chunk_dict = json.loads(data_str)
                                        if isinstance(chunk_dict, dict):
                                            json_mode_buffer.append(chunk_dict)
                                    except json.JSONDecodeError:
                                        pass
                                if b"data: [DONE]\n\n" in chunk_bytes:
                                    sent_done = True
                            except Exception:
                                pass
                        else:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except _CLIENT_DISCONNECTED:
                                logger.warning("客户端在流式响应过程中断开 model=%s", model)
                                close_upstream()
                                return
                            if b"data: [DONE]\n\n" in chunk_bytes:
                                sent_done = True
                except UpstreamAPIError as exc:
                    logger.warning("流式请求中途收到上游错误 status=%s error=%s", exc.status_code, exc)
                    # v52 P1: 不泄漏 str(exc)，返回安全消息
                    self._write_sse_error(_safe_error_message(exc, "Upstream service error"), "upstream_error")
                    sent_done = True  # v54: 防止 finally 重复发 [DONE]
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在流式响应过程中断开 model=%s error=%s", model, exc)
                    close_upstream()
                    return
                except Exception as exc:
                    logger.error("流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())
                    # v52 P1: 不泄漏 str(exc) 和 Python 类名，返回通用消息
                    self._write_sse_error("Internal server error. Please retry.", "internal_error")
                    sent_done = True
                finally:
                    if not sent_done and not is_json_mode:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except _CLIENT_DISCONNECTED:
                            pass

                # json 模式：从缓冲的 chunks 提取完整 content，剥离 markdown，重新发送
                if is_json_mode and json_mode_buffer:
                    try:
                        full_content_parts: list[str] = []
                        last_finish_reason: str | None = None
                        last_usage: dict | None = None
                        model_name = model
                        created_ts = int(time.time())
                        chunk_id = ""
                        for chunk_dict in json_mode_buffer:
                            chunk_id = chunk_dict.get("id", chunk_id) or chunk_id
                            model_name = chunk_dict.get("model", model_name) or model_name
                            created_ts = chunk_dict.get("created", created_ts) or created_ts
                            choices = chunk_dict.get("choices") or []
                            if choices and isinstance(choices[0], dict):
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content")
                                if isinstance(content, str) and content:
                                    full_content_parts.append(content)
                                fr = choices[0].get("finish_reason")
                                if fr:
                                    last_finish_reason = fr
                            usage = chunk_dict.get("usage")
                            if isinstance(usage, dict):
                                last_usage = usage
                        full_content = "".join(full_content_parts)
                        stripped_content = _strip_markdown_fences(full_content)
                        if stripped_content != full_content:
                            logger.info("json 模式流式剥离 markdown 代码块 model=%s 原长度=%d 剥离后=%d",
                                        model, len(full_content), len(stripped_content))
                        # 发送单个完整 content chunk
                        final_chunk = {
                            "id": chunk_id or gen_chatcmpl_id(),
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "system_fingerprint": system_fingerprint(model_name),
                            "choices": [{"index": 0, "delta": {"content": stripped_content}, "finish_reason": None, "logprobs": None}],
                        }
                        self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                        # finish chunk
                        finish_chunk = {
                            "id": chunk_id or gen_chatcmpl_id(),
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model_name,
                            "system_fingerprint": system_fingerprint(model_name),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": last_finish_reason or "stop", "logprobs": None}],
                        }
                        self.wfile.write(f"data: {json.dumps(finish_chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                        # usage chunk（如果有）
                        if last_usage:
                            usage_chunk = {
                                "id": chunk_id or gen_chatcmpl_id(),
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model_name,
                                "system_fingerprint": system_fingerprint(model_name),
                                "choices": [],
                                "usage": last_usage,
                            }
                            self.wfile.write(f"data: {json.dumps(usage_chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                        self.wfile.write(b"data: [DONE]\n\n")
                        self.wfile.flush()
                    except Exception as exc:
                        logger.error("json 模式流式重新发送失败 model=%s error=%s", model, exc)

                # 流结束后记录 token 用量
                self._record_streaming_token_usage(collected_chunks)
                logger.info("流式请求完成 model=%s", model)

            # ---- OpenAI-compat endpoint handlers ----

            def _record_token_usage(self, result: object) -> None:
                """从 chat_completion 响应中提取 token 用量并记录到 admin store。

                用于仪表盘 KPI（token_totals / token_30m / RPM）+ API Key per-key 用量。
                """
                try:
                    if not isinstance(result, dict):
                        return
                    usage = result.get("usage")
                    if not isinstance(usage, dict):
                        return
                    pt = int(usage.get("prompt_tokens", 0) or 0)
                    ct = int(usage.get("completion_tokens", 0) or 0)
                    from .admin.store import get_store as _get_admin_store
                    store = _get_admin_store()
                    store.record_token_usage(pt, ct)
                    # per-key 用量统计
                    api_key = getattr(self, "_admin_api_key", "")
                    if api_key:
                        store.record_api_key_usage(api_key, success=True, prompt_tokens=pt, completion_tokens=ct)
                except Exception:
                    pass  # 永不让 metrics 影响主请求

            def _record_streaming_token_usage(self, raw_chunks: list[bytes]) -> None:
                """从流式响应的原始 SSE chunks 中提取 usage 并记录。

                流式响应里最后一个含 usage 字段的 chunk 包含完整 token 统计
                （OpenAI stream_options.include_usage 行为，GLMEventAccumulator.finalize 输出）。

                之前的 bug：4 个流式 handler 都没调用 _record_token_usage，
                导致 stream=true 的请求（绝大多数 SDK 默认行为）token 用量从不被记录，
                用户看到"API 调用多次但用量才几十"。
                """
                try:
                    usage = _extract_usage_from_sse_chunks(raw_chunks)
                    if usage is None:
                        return
                    pt = usage["prompt_tokens"]
                    ct = usage["completion_tokens"]
                    if pt <= 0 and ct <= 0:
                        return
                    from .admin.store import get_store as _get_admin_store
                    store = _get_admin_store()
                    store.record_token_usage(pt, ct)
                    api_key = getattr(self, "_admin_api_key", "")
                    if api_key:
                        store.record_api_key_usage(api_key, success=True, prompt_tokens=pt, completion_tokens=ct)
                except Exception:
                    pass  # 永不让 metrics 影响主请求

            def _discover_dynamic_model(self, result: object) -> None:
                """从上游响应中提取真实模型名，加入动态注册表。

                用户核心诉求：未来 chatglm.cn 升级到 GLM-5.3 时无需改代码，
                只要发一次请求，新模型就自动出现在 /v1/models 列表里。

                提取来源：result["model"]（如 "GLM-5.2"）
                归一化：GLM-5.2 → glm-5.2
                派生：自动加 -think / -search / -think-search 变体
                """
                try:
                    if not isinstance(result, dict):
                        return
                    raw_model = result.get("model")
                    if not isinstance(raw_model, str) or not raw_model:
                        return
                    from .services.dynamic_models import get_dynamic_registry
                    registry = get_dynamic_registry()
                    if registry.discover_from_response(raw_model):
                        logger.info(
                            "动态发现新模型 raw=%s 已加入 exposed_models（无需改代码）",
                            raw_model,
                        )
                except Exception:
                    pass  # 永不让动态发现影响主请求

            def _get_effective_exposed_models(self) -> list[str]:
                """获取当前生效的模型列表（builtin + 动态发现）。

                所有需要校验或返回模型列表的地方都应使用此方法，而不是 config.exposed_models。
                """
                from .services.dynamic_models import merge_with_builtin
                return merge_with_builtin(list(config.exposed_models))

            def _model_info(self, model_id: str) -> dict[str, object]:
                """Build OpenAI-format model info object.

                Matches the structure returned by /v1/models and /v1/models/{model}.
                """
                # Determine model capabilities based on naming convention
                lower = model_id.lower()
                capabilities = {
                    "id": model_id,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "zhipu",
                }
                # Match OpenAI's model object schema (additional fields beyond id/object/created/owned_by
                # are not part of the public schema but harmless)
                return capabilities

            def _handle_moderations(self, payload: dict[str, object]) -> None:
                """OpenAI Moderations API endpoint.

                Always returns safe results — glm2api does not run content moderation.
                The shape matches OpenAI's: per-input object with category booleans.
                """
                inputs = payload.get("input")
                if isinstance(inputs, str):
                    inputs = [inputs]
                elif not isinstance(inputs, list) or not inputs:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        make_error(
                            "you must provide an input parameter",
                            error_type=ERROR_INVALID_REQUEST,
                            param="input",
                            request_id=gen_request_id(),
                        ),
                    )
                    return

                categories = {
                    "harassment": False,
                    "harassment/threatening": False,
                    "hate": False,
                    "hate/threatening": False,
                    "self-harm": False,
                    "self-harm/instructions": False,
                    "self-harm/intent": False,
                    "sexual": False,
                    "sexual/minors": False,
                    "violence": False,
                    "violence/graphic": False,
                }
                results = []
                for idx, inp in enumerate(inputs):
                    if not isinstance(inp, str):
                        inp = str(inp)
                    # Lightweight content sniff: if input contains obvious slurs or threats,
                    # flag harassment=True. This is NOT real moderation — just a heuristic
                    # so the response shape is plausible.
                    lower = inp.lower()
                    flagged = any(w in lower for w in {"fuck you", "kill yourself", "i will kill you", "you are stupid"})
                    results.append({
                        "flagged": flagged,
                        "categories": {k: (flagged and k.startswith("harassment")) for k in categories},
                        "category_scores": {k: 0.99 if (flagged and k.startswith("harassment")) else 0.01 for k in categories},
                    })

                self._write_json(HTTPStatus.OK, {
                    "id": f"modr_{uuid.uuid4().hex[:24]}",
                    "model": payload.get("model", "text-moderation-latest"),
                    "results": results,
                })

            def _handle_embeddings(self, payload: dict[str, object]) -> None:
                """OpenAI Embeddings API endpoint.

                Generates deterministic pseudo-embeddings by hashing the input text
                and projecting into a fixed-dimension vector. The vectors are NOT
                semantically meaningful, but the response shape matches OpenAI's
                and is suitable for testing client integrations.

                For real embeddings, configure an upstream embedding service.
                """
                inputs = payload.get("input")
                if isinstance(inputs, str):
                    inputs = [inputs]
                elif not isinstance(inputs, list) or not inputs:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        make_error(
                            "you must provide an input parameter",
                            error_type=ERROR_INVALID_REQUEST,
                            param="input",
                            request_id=gen_request_id(),
                        ),
                    )
                    return

                # Default to 1536-dim (text-embedding-3-small default)
                dim = int(payload.get("dimensions", 1536) or 1536)
                dim = max(1, min(dim, 8192))
                model_id = str(payload.get("model", "text-embedding-3-small"))
                encoding_format = str(payload.get("encoding_format", "float")).lower()

                data = []
                total_tokens = 0
                for idx, inp in enumerate(inputs):
                    if not isinstance(inp, str):
                        inp = str(inp)
                    # Generate a deterministic embedding by hashing characters into the vector
                    # Normalized to unit length so cosine similarity works mathematically.
                    vec = [0.0] * dim
                    for i, ch in enumerate(inp):
                        h = hashlib.md5(f"{i}:{ch}".encode("utf-8")).digest()
                        # Use 4 bytes as a uint32 to scale to [-1, 1]
                        u = int.from_bytes(h[:4], "big") / 0xFFFFFFFF  # 0..1
                        vec[i % dim] += (u * 2.0 - 1.0)
                    # Normalize
                    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
                    vec = [v / norm for v in vec]

                    if encoding_format == "base64":
                        import base64
                        bytes_data = b"".join(int(v * 32767).to_bytes(2, "big", signed=True) for v in vec)
                        vec_repr = base64.b64encode(bytes_data).decode("ascii")
                    else:
                        vec_repr = vec

                    data.append({
                        "object": "embedding",
                        "index": idx,
                        "embedding": vec_repr,
                    })
                    total_tokens += max(1, count_tokens(inp))

                self._write_json(HTTPStatus.OK, {
                    "object": "list",
                    "data": data,
                    "model": model_id,
                    "usage": {
                        "prompt_tokens": total_tokens,
                        "total_tokens": total_tokens,
                    },
                })

            def _handle_legacy_completion(self, payload: dict[str, object]) -> None:
                """OpenAI legacy /v1/completions endpoint.

                Translates the prompt-based request into a chat/completions call
                so GLM (which only exposes chat-style API) can serve it.
                """
                prompt = payload.get("prompt")
                if prompt is None:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        make_error(
                            "you must provide a prompt parameter",
                            error_type=ERROR_INVALID_REQUEST,
                            param="prompt",
                            request_id=gen_request_id(),
                        ),
                    )
                    return
                if isinstance(prompt, list):
                    prompt = "\n".join(str(p) for p in prompt)
                else:
                    prompt = str(prompt)

                model_id = str(payload.get("model", "glm-4-flash"))
                # Translate to chat format
                chat_payload: dict[str, object] = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": prompt}],
                }
                # Carry over shared params
                for key in ("temperature", "top_p", "max_tokens", "stream", "seed",
                            "presence_penalty", "frequency_penalty", "stop", "user",
                            "response_format", "logit_bias", "n"):
                    if key in payload:
                        chat_payload[key] = payload[key]

                if payload.get("stream"):
                    # Stream legacy text completion: yield only the text deltas, not chat metadata
                    self._stream_legacy_completion(chat_payload, model_id)
                    return

                logger.info("收到 legacy completion 请求 model=%s", model_id)
                result, _ = glm_client.chat_completion(chat_payload)
                # Translate chat.completion back to legacy text_completion
                choice = result.get("choices", [{}])[0] if isinstance(result.get("choices"), list) and result.get("choices") else {}
                message = choice.get("message", {}) if isinstance(choice, dict) else {}
                text = str(message.get("content") or "")
                finish = choice.get("finish_reason", "stop") if isinstance(choice, dict) else "stop"
                usage = result.get("usage", {}) if isinstance(result.get("usage"), dict) else {}

                legacy_response = {
                    "id": result.get("id", gen_chatcmpl_id()),
                    "object": "text_completion",
                    "created": result.get("created", now_timestamp()),
                    "model": model_id,
                    "system_fingerprint": system_fingerprint(model_id),
                    "choices": [
                        {
                            "text": text,
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": finish,
                        }
                    ],
                    "usage": usage,
                }
                self._write_json(HTTPStatus.OK, legacy_response)

            def _stream_legacy_completion(self, chat_payload: dict[str, object], model_id: str) -> None:
                """Stream a legacy /v1/completions response, translating chat SSE -> completion SSE.

                v54: 加心跳保活（与 _stream_completion 一致），修复 GLM 思考时客户端超时
                """
                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                # v54: 后台线程读上游（含 stream_chat_completion 调用）
                chunk_queue, sentinel, close_upstream = self._start_stream_background(chat_payload)

                sent_done = False
                collected_chunks: list[bytes] = []
                LEGACY_HEARTBEAT_INTERVAL = 2.0
                try:
                    while True:
                        try:
                            chunk = chunk_queue.get(timeout=LEGACY_HEARTBEAT_INTERVAL)
                        except queue.Empty:
                            if not self._write_heartbeat(b": keep-alive\n\n"):
                                logger.warning("客户端在 legacy completion 心跳阶段断开 model=%s", model_id)
                                close_upstream()
                                return
                            continue
                        if chunk is sentinel:
                            break
                        if isinstance(chunk, BaseException):
                            raise chunk
                        if not chunk:
                            continue
                        collected_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                        text = chunk.decode("utf-8", errors="ignore")
                        for line in text.split("\n\n"):
                            line = line.strip()
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                try:
                                    self.wfile.write(b"data: [DONE]\n\n")
                                    self.wfile.flush()
                                except _CLIENT_DISCONNECTED:
                                    close_upstream()
                                    return
                                sent_done = True
                                continue
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            choices = data.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            finish = choices[0].get("finish_reason")
                            text_delta = delta.get("content", "")
                            legacy_chunk = {
                                "id": data.get("id", gen_chatcmpl_id()),
                                "object": "text_completion",
                                "created": data.get("created", now_timestamp()),
                                "model": model_id,
                                "system_fingerprint": system_fingerprint(model_id),
                                "choices": [
                                    {
                                        "text": text_delta,
                                        "index": 0,
                                        "logprobs": None,
                                        "finish_reason": finish,
                                    }
                                ],
                            }
                            if "usage" in data:
                                legacy_chunk["usage"] = data["usage"]
                            try:
                                self.wfile.write(f"data: {json.dumps(legacy_chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except _CLIENT_DISCONNECTED:
                                logger.warning("客户端在 legacy completion 流式响应过程中断开 model=%s", model_id)
                                close_upstream()
                                return
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 legacy completion 流式响应过程中断开 model=%s error=%s", model_id, exc)
                    close_upstream()
                    return
                except Exception as exc:
                    logger.error("legacy completion 流式请求失败 model=%s error=%s\n%s", model_id, exc, traceback.format_exc())
                finally:
                    if not sent_done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except _CLIENT_DISCONNECTED:
                            pass
                # 流结束后记录 token 用量
                self._record_streaming_token_usage(collected_chunks)
                logger.info("legacy completion 流式请求完成 model=%s", model_id)

            # ---- Auth ----

            def _authorize(self) -> bool:
                # 硬限制：所有 API 调用必须通过 API key 认证（和官方版 API 一样）
                # 即使没有配置任何 key，也不允许无 key 访问
                from .admin.store import get_store as _get_admin_store_for_auth
                store = _get_admin_store_for_auth()
                # 同步环境变量 key 到 store
                store.init_env_api_keys(list(config.server_api_keys))

                has_env_keys = bool(config.server_api_keys)

                # Support both Bearer token and x-api-key header (Anthropic style)
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    token = authorization[7:].strip()
                    # 先检查环境变量 key
                    if has_env_keys and token in config.server_api_keys:
                        self._admin_api_key = token
                        return True
                    # 再检查面板创建的 key
                    if store.get_api_key_for_auth(token):
                        self._admin_api_key = token
                        return True
                x_api_key = self.headers.get("x-api-key", "")
                if x_api_key:
                    x_api_key = x_api_key.strip()
                    if has_env_keys and x_api_key in config.server_api_keys:
                        self._admin_api_key = x_api_key
                        return True
                    if store.get_api_key_for_auth(x_api_key):
                        self._admin_api_key = x_api_key
                        return True
                # 没有匹配任何 key → 拒绝
                return False

            # ---- Helpers ----

            def _write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                debug_dump(logger, config.debug_dump_all, f"HTTP 出站 JSON 响应 status={int(status)} path={self.path}", body)
                self.send_response(status)
                self._send_common_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_json_with_retry(self, status: HTTPStatus, payload: dict[str, object], retry_after: int = 30) -> None:
                """v47: 发送带 Retry-After header 的 JSON 响应（用于 503 限流场景）。"""
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self._send_common_headers()
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", str(retry_after))
                self.end_headers()
                self.wfile.write(body)

            def _send_common_headers(self) -> None:
                # v34 修复：隐藏逆向特征
                # 1. 覆盖 Server header（BaseHTTPRequestHandler 会用 server_version）
                #    官方 OpenAI 返回 "Server: cloudflare"，我们已设 server_version="cloudflare"
                #    但保险起见显式覆盖（防止 Render 代理改写）
                self.send_header("Server", "cloudflare")
                # 2. 覆盖 x-render-origin-server（Render 平台自动添加，暴露 Python 版本）
                #    官方 OpenAI 不返回此 header，我们覆盖为空值或 openai-api
                self.send_header("x-render-origin-server", "openai-api")
                # 3. 添加 X-Request-ID（官方 OpenAI/Anthropic 每个响应都有，用于请求追踪）
                #    格式：req_<32 hex>，与官方一致
                request_id = f"req_{uuid.uuid4().hex[:24]}"
                self.send_header("X-Request-ID", request_id)
                # v53 P2: 添加 request-id header（官方 Anthropic API 同时有 X-Request-ID 和 request-id）
                self.send_header("request-id", request_id)
                # 4. 添加 CF-Ray（官方 OpenAI 通过 cloudflare 返回此 header，模拟真实 CDN）
                self.send_header("CF-Ray", f"{uuid.uuid4().hex[:16]}-LAX")
                # v35 修复：API 端点（/v1/*）完全不发 CORS header（与官方 OpenAI 一致）
                # 官方 OpenAI API 不返回 Access-Control-Allow-Origin，只返回 Access-Control-Expose-Headers
                # 只有 admin 端点（/admin/*）才需要 CORS（浏览器跨域访问管理面板）
                path = self._path_without_query() if hasattr(self, '_path_without_query') else ""
                if path.startswith("/admin"):
                    # Admin 端点：仅当配置了具体来源时才发送 CORS（不用 *，避免暴露特征）
                    if config.cors_allow_origin and config.cors_allow_origin != "*":
                        self.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
                        self.send_header(
                            "Access-Control-Allow-Headers",
                            "Authorization, Content-Type, x-api-key, anthropic-version",
                        )
                        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                else:
                    # API 端点：模拟官方 cloudflare 的 Access-Control-Expose-Headers
                    self.send_header("Access-Control-Expose-Headers", "CF-Ray")
                    # v53 P2: Anthropic 端点添加 ratelimit header（官方 API 有这些）
                    # 设大值表示未限流（我们用自己的 _RateLimiter，不依赖这些 header）
                    if path == f"{config.api_prefix}/messages" or path == f"{config.api_prefix}/messages/count_tokens":
                        self.send_header("anthropic-ratelimit-requests-limit", "1000")
                        self.send_header("anthropic-ratelimit-requests-remaining", "999")
                        self.send_header("anthropic-ratelimit-requests-reset", "2025-01-01T00:00:00Z")
                        self.send_header("anthropic-ratelimit-tokens-limit", "1000000")
                        self.send_header("anthropic-ratelimit-tokens-remaining", "999999")
                        self.send_header("anthropic-ratelimit-tokens-reset", "2025-01-01T00:00:00Z")

            def _safe_write_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
                try:
                    self._write_json(status, payload)
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 JSON 响应写回前断开 path=%s", self.path)

            def _parse_content_length(self) -> int:
                raw_value = self.headers.get("Content-Length", "0").strip()
                try:
                    return int(raw_value or "0")
                except ValueError as exc:
                    raise ValueError(f"无效的 Content-Length: {raw_value}") from exc

            def _write_sse_error(self, message: str, error_type: str) -> None:
                event = {
                    "error": {
                        "message": message,
                        "type": error_type,
                    }
                }
                try:
                    payload = f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                except _CLIENT_DISCONNECTED:
                    logger.warning("客户端在 SSE 错误写回前断开 path=%s", self.path)

            def _start_stream_background(self, openai_payload: dict[str, object]):
                """v54: 启动后台线程读上游 SSE，返回 (chunk_queue, sentinel, close_upstream)。

                修复三个 Critical 问题：
                1. C2（首 chunk 等待无心跳）：stream_chat_completion 在后台线程调用，
                   主线程不再阻塞，可立即发心跳
                2. C1（客户端断连后上游不关闭）：close_upstream() 让主线程在断连时
                   调用 stream_iter.close()，触发 generator finally 释放 lease + 关闭连接
                3. H1（except BaseException 吞掉 KeyboardInterrupt）：改为 except Exception
                """
                chunk_queue: queue.Queue[object] = queue.Queue()
                sentinel = object()
                stream_holder: dict[str, object] = {"iter": None}

                def read_upstream() -> None:
                    try:
                        # stream_chat_completion 是普通函数（非 generator），
                        # 调用时同步打开上游连接 — 放后台线程避免主线程阻塞
                        stream_holder["iter"] = glm_client.stream_chat_completion(openai_payload)
                        for upstream_chunk in stream_holder["iter"]:
                            chunk_queue.put(upstream_chunk)
                    except Exception as exc:
                        # v54: 不用 BaseException，避免吞掉 KeyboardInterrupt / SystemExit
                        chunk_queue.put(exc)
                    finally:
                        chunk_queue.put(sentinel)

                threading.Thread(target=read_upstream, daemon=True).start()

                def close_upstream() -> None:
                    """主线程客户端断连时调用，关闭上游 generator 触发 lease 释放。"""
                    it = stream_holder.get("iter")
                    if it is not None:
                        try:
                            it.close()  # 触发 generator finally（释放 lease + close response）
                        except Exception:
                            pass

                return chunk_queue, sentinel, close_upstream

            def _write_heartbeat(self, data: bytes) -> bool:
                """v54: 写心跳数据，返回是否成功。失败表示客户端已断连。"""
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                    return True
                except _CLIENT_DISCONNECTED:
                    return False

            def _safe_http_status(self, value: int, fallback: HTTPStatus) -> HTTPStatus:
                try:
                    return HTTPStatus(value)
                except ValueError:
                    return fallback

            def _debug_log_request_start(self) -> None:
                debug_dump(
                    logger,
                    config.debug_dump_all,
                    f"HTTP 入站请求 {self.command} {self.path} headers",
                    {key: value for key, value in self.headers.items()},
                )

            def _path_without_query(self) -> str:
                return urlparse(self.path).path

            def log_message(self, format: str, *args) -> None:
                logger.info("%s - %s", self.address_string(), format % args)

        return RequestHandler
