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
            server_version = "glm2api/0.1.0"
            protocol_version = "HTTP/1.1"

            def do_OPTIONS(self) -> None:
                # Admin panel also handles OPTIONS (CORS preflight)
                if handle_admin_request(self, config, glm_client, logger):
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self._send_common_headers()
                self.end_headers()

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
                    # Record model + stream for admin metrics (best-effort)
                    if isinstance(payload.get("model"), str):
                        self._admin_model = str(payload["model"])
                    if payload.get("stream"):
                        self._admin_stream = True

                    # --- Anthropic Messages API ---
                    if path == f"{config.api_prefix}/messages":
                        logger.info("收到 Anthropic 请求 model=%s stream=%s", payload.get("model"), payload.get("stream"))
                        self._handle_anthropic_messages(payload)
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
                    # v19 修复 P2：空 messages 数组早期校验，避免发到上游导致挂起
                    messages = payload.get("messages")
                    if not isinstance(messages, list) or not payload.get("model"):
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                "you must provide a model and messages parameter",
                                error_type=ERROR_INVALID_REQUEST,
                                param="messages" if not messages else "model",
                                request_id=gen_request_id(),
                            ),
                        )
                        return
                    # v19 P2: messages 是空数组时返回错误（避免上游 GLM 挂起）
                    if len(messages) == 0:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            make_error(
                                "messages array must not be empty",
                                error_type=ERROR_INVALID_REQUEST,
                                param="messages",
                                request_id=gen_request_id(),
                            ),
                        )
                        return

                    # Validate model exists
                    model_id = str(payload.get("model", ""))
                    if model_id not in self._get_effective_exposed_models():
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

                    if payload.get("stream"):
                        self._stream_completion(payload)
                        return

                    logger.info("收到 chat 请求 model=%s", payload.get("model"))
                    result, conversation_id = glm_client.chat_completion(payload)
                    # 记录 token 使用量到 admin store（用于仪表盘 KPI）
                    self._record_token_usage(result)
                    # 动态模型发现：从上游响应提取真实模型名，加入动态注册表
                    # 用户核心诉求：未来 chatglm.cn 升级到 GLM-5.3 时无需改代码，自动显示
                    self._discover_dynamic_model(result)
                    self._write_json(HTTPStatus.OK, result)
                except QueueTimeoutError as exc:
                    logger.warning("GLM 队列等待超时 error=%s", exc)
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        make_error(
                            f"Service temporarily unavailable: {exc}",
                            error_type=ERROR_SERVER,
                            code="queue_timeout",
                            request_id=gen_request_id(),
                        ),
                    )
                except UpstreamAPIError as exc:
                    logger.warning("上游 GLM 返回错误 status=%s error=%s", exc.status_code, exc)
                    status = self._safe_http_status(exc.status_code, fallback=HTTPStatus.BAD_GATEWAY)
                    self._write_json(
                        status,
                        make_error(
                            str(exc),
                            error_type=ERROR_UPSTREAM,
                            code="upstream_error",
                            request_id=gen_request_id(),
                        ),
                    )
                except ValueError as exc:
                    logger.warning("请求参数错误 path=%s error=%s", self.path, exc)
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        make_error(
                            str(exc),
                            error_type=ERROR_INVALID_REQUEST,
                            code="invalid_request",
                            request_id=gen_request_id(),
                        ),
                    )
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端连接提前断开 path=%s error=%s", self.path, exc)
                    self._admin_error = f"client_disconnected: {exc}"
                except Exception as exc:
                    logger.error("处理请求失败 error=%s\n%s", exc, traceback.format_exc())
                    self._admin_error = str(exc)
                    self._safe_write_json(
                        HTTPStatus.BAD_GATEWAY,
                        make_error(
                            f"Upstream error: {exc}",
                            error_type=ERROR_UPSTREAM,
                            code=exc.__class__.__name__.lower(),
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
                    )
                except Exception:
                    pass  # never let metrics break the request

            # ---- Anthropic Messages API ----

            def _handle_anthropic_messages(self, payload: dict[str, object]) -> None:
                model = str(payload.get("model", "glm-4"))
                openai_payload = anthropic_to_openai(payload)

                # v20 P2-2: strict_model_validation 模式下校验模型是否存在
                # 默认宽松模式（容错 fallback 到默认模型），符合 Claude Code 客户端友好设计
                # 设置环境变量 STRICT_MODEL_VALIDATION=true 启用严格模式
                strict_model_validation = os.environ.get("STRICT_MODEL_VALIDATION", "").lower() in ("true", "1", "yes")
                if strict_model_validation and model not in self._get_effective_exposed_models():
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        make_error(
                            f"The model '{model}' does not exist",
                            error_type="invalid_request_error",
                            param="model",
                            code="model_not_found",
                            request_id=gen_request_id(),
                        ),
                    )
                    return

                if payload.get("stream"):
                    self._stream_anthropic(openai_payload, model)
                    return

                result, _ = glm_client.chat_completion(openai_payload)
                response = openai_to_anthropic_response(result, model)
                self._record_token_usage(result)
                self._discover_dynamic_model(result)
                self._write_json(HTTPStatus.OK, response)

            def _stream_anthropic(self, openai_payload: dict[str, object], model: str) -> None:
                openai_payload["stream"] = True
                stream_iter = glm_client.stream_chat_completion(openai_payload)
                accumulator = AnthropicStreamAccumulator(model=model)

                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                try:
                    for chunk in stream_iter:
                        if not chunk:
                            continue
                        if not accumulator.started:
                            start_event = accumulator.start_message()
                            self.wfile.write(start_event.encode("utf-8"))
                            self.wfile.flush()
                        events = accumulator.feed_chunk(chunk)
                        for event in events:
                            self.wfile.write(event.encode("utf-8"))
                            self.wfile.flush()
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Anthropic 流式响应过程中断开 model=%s error=%s", model, exc)
                    return
                except Exception as exc:
                    logger.error("Anthropic 流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())

                # Ensure message_stop is always sent (idempotent via _finished flag)
                if accumulator.started:
                    try:
                        for event in accumulator._finish():
                            self.wfile.write(event.encode("utf-8"))
                            self.wfile.flush()
                    except _CLIENT_DISCONNECTED:
                        pass

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
                stream_iter = glm_client.stream_chat_completion(openai_payload)
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

                chunk_queue: queue.Queue[object] = queue.Queue()
                sentinel = object()

                def read_upstream() -> None:
                    try:
                        for upstream_chunk in stream_iter:
                            chunk_queue.put(upstream_chunk)
                    except BaseException as exc:
                        chunk_queue.put(exc)
                    finally:
                        chunk_queue.put(sentinel)

                threading.Thread(target=read_upstream, daemon=True).start()

                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=RESPONSES_STREAM_HEARTBEAT_SECONDS)
                        except queue.Empty:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
                        events = accumulator.feed_chunk(chunk)  # type: ignore[arg-type]
                        for event in events:
                            self.wfile.write(event.encode("utf-8"))
                            self.wfile.flush()
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Responses 流式响应过程中断开 model=%s error=%s", model, exc)
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
                stream_iter = glm_client.stream_chat_completion(openai_payload)
                accumulator = ResponsesV2StreamAccumulator(model=model, request_payload=request_payload)

                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                chunk_queue: queue.Queue[object] = queue.Queue()
                sentinel = object()

                def read_upstream() -> None:
                    try:
                        for upstream_chunk in stream_iter:
                            chunk_queue.put(upstream_chunk)
                    except BaseException as exc:
                        chunk_queue.put(exc)
                    finally:
                        chunk_queue.put(sentinel)

                threading.Thread(target=read_upstream, daemon=True).start()

                try:
                    while True:
                        try:
                            queued = chunk_queue.get(timeout=RESPONSES_STREAM_HEARTBEAT_SECONDS)
                        except queue.Empty:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            continue

                        if queued is sentinel:
                            break
                        if isinstance(queued, BaseException):
                            raise queued
                        chunk = queued
                        if not chunk:
                            continue
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
                            return

                    # 流结束，发送 finalize 事件
                    for event_type, event_data in accumulator.finalize():
                        sse_event = f"event: {event_type}\ndata: {json.dumps(event_data, ensure_ascii=False)}\n\n"
                        self.wfile.write(sse_event.encode("utf-8"))
                        self.wfile.flush()

                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 Responses v2 流式响应过程中断开 model=%s error=%s", model, exc)
                    return
                except Exception as exc:
                    logger.error("Responses v2 流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())

                logger.info("Responses v2 流式请求完成 model=%s", model)

            # ---- Chat completions (original) ----

            def _stream_completion(self, payload: dict[str, object]) -> None:
                model = str(payload.get("model", "unknown"))
                logger.info("开始流式响应 model=%s", model)
                stream_iter = glm_client.stream_chat_completion(payload)
                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                sent_done = False
                try:
                    for chunk in stream_iter:
                        if chunk:
                            debug_dump(logger, config.debug_dump_all, f"HTTP 出站流式分片 model={model}", chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            if b"data: [DONE]\n\n" in chunk:
                                sent_done = True
                except UpstreamAPIError as exc:
                    logger.warning("流式请求中途收到上游错误 status=%s error=%s", exc.status_code, exc)
                    self._write_sse_error(str(exc), "upstream_error")
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在流式响应过程中断开 model=%s error=%s", model, exc)
                    return
                except Exception as exc:
                    logger.error("流式请求失败 model=%s error=%s\n%s", model, exc, traceback.format_exc())
                    self._write_sse_error(str(exc), exc.__class__.__name__)
                finally:
                    if not sent_done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except _CLIENT_DISCONNECTED:
                            pass
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
                    "type": "model",
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
                    "system_fingerprint": system_fingerprint(),
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
                """Stream a legacy /v1/completions response, translating chat SSE -> completion SSE."""
                self.send_response(HTTPStatus.OK)
                self._send_common_headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                sent_done = False
                try:
                    for chunk in glm_client.stream_chat_completion(chat_payload):
                        if not chunk:
                            continue
                        text = chunk.decode("utf-8", errors="ignore")
                        # Each chunk is `data: {...}\n\n` with chat.completion.chunk shape
                        # Convert to text_completion chunk shape
                        for line in text.split("\n\n"):
                            line = line.strip()
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                self.wfile.write(b"data: [DONE]\n\n")
                                self.wfile.flush()
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
                                "system_fingerprint": system_fingerprint(),
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
                            self.wfile.write(f"data: {json.dumps(legacy_chunk, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))
                            self.wfile.flush()
                except _CLIENT_DISCONNECTED as exc:
                    logger.warning("客户端在 legacy completion 流式响应过程中断开 model=%s error=%s", model_id, exc)
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
                logger.info("legacy completion 流式请求完成 model=%s", model_id)

            # ---- Auth ----

            def _authorize(self) -> bool:
                if not config.server_api_keys:
                    return True
                # Support both Bearer token and x-api-key header (Anthropic style)
                authorization = self.headers.get("Authorization", "")
                if authorization.startswith("Bearer "):
                    token = authorization[7:].strip()
                    if token in config.server_api_keys:
                        self._admin_api_key = token  # 记录用于 per-key 用量统计
                        return True
                x_api_key = self.headers.get("x-api-key", "")
                if x_api_key and x_api_key.strip() in config.server_api_keys:
                    self._admin_api_key = x_api_key.strip()
                    return True
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

            def _send_common_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Authorization, Content-Type, x-api-key, anthropic-version",
                )
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

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
