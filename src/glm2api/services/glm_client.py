from __future__ import annotations

import base64
import codecs
import gzip
import http.client
import json
import mimetypes
import re
import socket
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.generator import _make_boundary # type: ignore
from io import BufferedReader, BytesIO
from logging import Logger
from typing import Callable

from ..config import AppConfig
from ..logging_utils import debug_dump
from .glm_auth import GLMAccessTokenManager, build_sign
from .translator import (
    BLOCKED_NATIVE_TOOL_NAMES,
    GLMEventAccumulator,
    SERVER_SIDE_TOOL_NAMES,
    convert_messages,
    extract_recent_user_url,
    filter_tools,
    resolve_chat_mode,
    resolve_networking,
    resolve_upstream_model,
)


FILE_UPLOAD_URL_SUFFIX = "/backend-api/assistant/file_upload"
FILE_SIZE_LIMIT = 100 * 1024 * 1024
IMAGE_SIZE_TO_ASPECT_RATIO = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "1024x1792": "9:16",
    "1792x1024": "16:9",
}


class UpstreamAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, payload: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class QueueTimeoutError(RuntimeError):
    pass


@dataclass(slots=True)
class QueueLease:
    ticket: int
    release_callback: Callable[[int], None]
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        self.release_callback(self.ticket)


class ConcurrentRequestQueue:
    def __init__(self, logger: Logger, wait_timeout: int, max_concurrency: int) -> None:
        self.logger = logger
        self.wait_timeout = wait_timeout
        self.max_concurrency = max(1, max_concurrency)
        self._condition = threading.Condition()
        self._next_ticket = 0
        self._serving_ticket = 0
        self._released_tickets: set[int] = set()

    def acquire(self, request_name: str) -> QueueLease:
        with self._condition:
            ticket = self._next_ticket
            self._next_ticket += 1
            queue_ahead = max(0, ticket - (self._serving_ticket + self.max_concurrency) + 1)
            start = time.monotonic()

            if queue_ahead > 0:
                self.logger.info("请求进入 GLM 队列 ticket=%s ahead=%s request=%s", ticket, queue_ahead, request_name)

            while ticket >= self._serving_ticket + self.max_concurrency:
                remaining = self.wait_timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise QueueTimeoutError(
                        f"GLM 队列等待超时，前方仍有 {ticket - (self._serving_ticket + self.max_concurrency) + 1} 个请求，请稍后重试。"
                    )
                self._condition.wait(timeout=remaining)

            active_slots = ticket - self._serving_ticket + 1
            self.logger.info(
                "请求获得 GLM 执行槽位 ticket=%s active=%s/%s request=%s",
                ticket,
                active_slots,
                self.max_concurrency,
                request_name,
            )
            return QueueLease(ticket=ticket, release_callback=self._release)

    def _release(self, ticket: int) -> None:
        with self._condition:
            self._released_tickets.add(ticket)
            while self._serving_ticket in self._released_tickets:
                self._released_tickets.remove(self._serving_ticket)
                self._serving_ticket += 1
            self.logger.info("请求离开 GLM 执行槽位 ticket=%s", ticket)
            self._condition.notify_all()


class GLMWebClient:
    def __init__(self, config: AppConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self.auth = GLMAccessTokenManager(config=config, logger=logger)
        self.request_queue = ConcurrentRequestQueue(
            logger=logger,
            wait_timeout=config.glm_queue_wait_timeout,
            max_concurrency=config.glm_max_concurrency,
        )
        # Thread-local: last account index used by this thread (for admin metrics)
        import threading
        self._tl = threading.local()

    def _set_last_account_index(self, idx: int) -> None:
        self._tl.last_account_index = idx

    def get_last_account_index(self) -> int:
        """Return the account index used by the most recent operation on this thread.
        Returns -1 if no operation has run yet."""
        return getattr(self._tl, "last_account_index", -1)

    def _resolve_tools(self, openai_payload: dict[str, object]) -> tuple[list[dict[str, object]] | None, set[str] | None]:
        raw_tools = list(openai_payload.get("tools", [])) if isinstance(openai_payload.get("tools"), list) else None # type: ignore
        blocked_tool_names = {
            name.strip()
            for name in self.config.blocked_tool_names
            if name.strip()
        } | BLOCKED_NATIVE_TOOL_NAMES
        filtered_tools = filter_tools(raw_tools, blocked_tool_names)
        if raw_tools and len(raw_tools) != len(filtered_tools or []):
            blocked_names: list[str] = []
            for tool in raw_tools:
                fn = tool.get("function", {})
                tool_name = str(fn.get("name", "")).strip()
                if tool_name in blocked_tool_names:
                    blocked_names.append(tool_name)
            if blocked_names:
                self.logger.info("已过滤不受支持的工具: %s", ", ".join(blocked_names))
        return filtered_tools, {tool["function"]["name"] for tool in filtered_tools} if filtered_tools else None # type: ignore[index]

    @staticmethod
    def _inject_system_instruction(messages: list[dict[str, object]], instruction: str) -> list[dict[str, object]]:
        """Prepend or augment a system message in the message list.

        If a system message already exists at index 0, append the instruction to it.
        Otherwise, prepend a new system message.

        This is used to implement response_format=json_object by translating it
        into a system instruction (GLM doesn't have a native JSON mode).
        """
        if not messages:
            return [{"role": "system", "content": instruction}]
        new_messages = [dict(m) for m in messages]  # shallow copy each
        first = new_messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            existing = first.get("content")
            if isinstance(existing, str):
                first["content"] = existing + "\n\n" + instruction
            elif isinstance(existing, list):
                # Multi-part system content
                first["content"] = list(existing) + [{"type": "text", "text": instruction}]  # type: ignore[list-item]
            else:
                first["content"] = instruction
        else:
            new_messages.insert(0, {"role": "system", "content": instruction})
        return new_messages

    def chat_completion(self, payload: dict[str, object]) -> tuple[dict[str, object], str | None]:
        # Support n>1 by issuing multiple parallel requests to GLM and merging choices.
        # GLM upstream doesn't support n natively, so we fan out at the gateway level.
        try:
            n_value = int(payload.get("n", 1) or 1)
        except (TypeError, ValueError):
            n_value = 1
        n_value = max(1, min(n_value, 4))  # cap at 4 to limit blast radius
        if n_value > 1:
            return self._chat_completion_n(payload, n_value)

        # 复读检测：第一次失败（触发复读）时自动重试 1 次
        # 审核报告 P1 问题：GLM-5.1 在长 prompt + 工具调用场景下偶发复读模式
        last_exc: Exception | None = None
        for attempt in range(2):  # 最多 2 次尝试（1 次正常 + 1 次重试）
            try:
                result, conv_id = self._chat_completion_single(payload)
                # 复读检测：检查响应是否陷入复读模式
                if attempt == 0 and self._is_repetition_loop(result):
                    self.logger.warning(
                        "检测到 GLM 响应复读循环 model=%s 重试中 (attempt=2/2)",
                        payload.get("model", "unknown"),
                    )
                    # 记录到 admin store 用于面板展示
                    try:
                        from ..admin.store import get_store as _get_admin_store
                        _get_admin_store().record_repetition_event(
                            model=str(payload.get("model", "unknown")),
                            path="non_stream",
                        )
                    except Exception:
                        pass
                    continue  # 重试一次
                return result, conv_id
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    self.logger.warning(
                        "GLM 请求失败 重试中 model=%s error=%s",
                        payload.get("model", "unknown"), exc,
                    )
                    continue
                raise
        if last_exc:
            raise last_exc
        # 兜底（理论上不会到达）
        return self._chat_completion_single(payload)

    def _is_repetition_loop(self, result: dict[str, object]) -> bool:
        """检测响应是否陷入复读模式（同一句话重复 N 次以上）。
        
        判定规则：
          - 提取 choices[0].message.content 文本
          - 按句号/换行切分为句子
          - 如果存在连续 5 句相同（或同一句重复超过 5 次），判定为复读
          - 文本长度 < 100 字符不判定（避免误杀短回复）
        """
        try:
            choices = result.get("choices")
            if not isinstance(choices, list) or not choices:
                return False
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if not isinstance(msg, dict):
                return False
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < 100:
                return False
            # 切句子：按。. ! ? \n 切
            import re
            sentences = [s.strip() for s in re.split(r'[。.!！?？\n]+', content) if len(s.strip()) > 8]
            if len(sentences) < 6:
                return False
            # 检测连续重复
            from collections import Counter
            counter = Counter(sentences)
            most_common_count = counter.most_common(1)[0][1] if counter else 0
            # 同一句子重复 5 次以上判定为复读
            if most_common_count >= 5:
                return True
            # 检测连续 3 句相同
            consecutive = 1
            for i in range(1, len(sentences)):
                if sentences[i] == sentences[i-1]:
                    consecutive += 1
                    if consecutive >= 3:
                        return True
                else:
                    consecutive = 1
            return False
        except Exception:
            return False

    def _chat_completion_single(self, payload: dict[str, object]) -> tuple[dict[str, object], str | None]:
        """单次 chat_completion 调用（无重试逻辑，由 chat_completion 包装重试）。"""
        _, allowed_tool_names = self._resolve_tools(payload)
        lease = self.request_queue.acquire(f"chat:{payload.get('model', 'unknown')}")
        try:
            response, assistant_id = self._open_chat_stream(payload, preferred_account_index=self._get_preferred_account_index(lease.ticket))
        except Exception:
            lease.release()
            raise
        accumulator = GLMEventAccumulator(
            model=str(payload["model"]),
            allowed_tool_names=allowed_tool_names,
            fallback_tool_url=extract_recent_user_url(list(payload.get("messages", []))), # type: ignore[arg-type]
            debug_enabled=self.config.debug_dump_all,
            logger=self.logger,
            prompt_messages=list(payload.get("messages", [])) if isinstance(payload.get("messages"), list) else None,
            tools_schema=list(payload.get("tools", [])) if isinstance(payload.get("tools"), list) else None,
        )
        try:
            for event in self._iter_sse_events(response):
                if not event:
                    continue
                status = event.get("status")
                self._raise_for_event_error(event, stream=False)
                accumulator.consume_event(event)
                if status in {"finish", "intervene"}:
                    return accumulator.build_response(), accumulator.conversation_id
        finally:
            response.close() # type: ignore
            self.delete_conversation(accumulator.conversation_id, assistant_id=assistant_id)
            lease.release()
        return accumulator.build_response(), accumulator.conversation_id

    def _chat_completion_n(self, payload: dict[str, object], n: int) -> tuple[dict[str, object], str | None]:
        """Handle n>1 by issuing multiple sequential calls and merging choices."""
        # Strip n from payload so child calls don't recurse
        child_payload = dict(payload)
        child_payload["n"] = 1
        # Run n sequential calls (parallel would need queue fan-out, sequential is safer)
        responses: list[tuple[dict[str, object], str | None]] = []
        last_error: Exception | None = None
        for _ in range(n):
            try:
                resp, conv = self.chat_completion(child_payload)
                responses.append((resp, conv))
            except Exception as exc:
                last_error = exc
                # Continue to fill as many choices as possible
        if not responses:
            assert last_error is not None
            raise last_error
        # Merge: take the first response as base, append the rest's choices
        base_resp, base_conv = responses[0]
        merged_choices: list[dict[str, object]] = []
        for idx, (resp, _) in enumerate(responses):
            choices = resp.get("choices", [])
            if isinstance(choices, list) and choices:
                ch = dict(choices[0])
                ch["index"] = idx
                merged_choices.append(ch)
        merged = dict(base_resp)
        merged["choices"] = merged_choices
        # Aggregate usage across all calls
        total_prompt = 0
        total_completion = 0
        for resp, _ in responses:
            u = resp.get("usage", {})
            if isinstance(u, dict):
                total_prompt += int(u.get("prompt_tokens", 0) or 0)
                total_completion += int(u.get("completion_tokens", 0) or 0)
        merged["usage"] = {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
        }
        return merged, base_conv

    def generate_images(self, payload: dict[str, object]) -> dict[str, object]:
        lease = self.request_queue.acquire(f"image:{payload.get('model', self.config.glm_image_model_name)}")
        try:
            response, assistant_id = self._open_image_stream(payload, preferred_account_index=self._get_preferred_account_index(lease.ticket))
        except Exception:
            lease.release()
            raise

        accumulator = GLMEventAccumulator(
            model=str(payload.get("model", self.config.glm_image_model_name)),
            debug_enabled=self.config.debug_dump_all,
            logger=self.logger,
        )
        try:
            for event in self._iter_sse_events(response):
                if not event:
                    continue
                status = event.get("status")
                accumulator.consume_event(event)
                if status == "finish":
                    return self._build_images_response(payload, event, accumulator)

            return self._build_images_response(payload, {}, accumulator)
        finally:
            response.close() # type: ignore
            self.delete_conversation(accumulator.conversation_id, assistant_id=assistant_id)
            lease.release()

    def stream_chat_completion(self, payload: dict[str, object]):
        _, allowed_tool_names = self._resolve_tools(payload)
        lease = self.request_queue.acquire(f"stream:{payload.get('model', 'unknown')}")
        try:
            response, assistant_id = self._open_chat_stream(payload, preferred_account_index=self._get_preferred_account_index(lease.ticket))
        except Exception:
            lease.release()
            raise

        accumulator = GLMEventAccumulator(
            model=str(payload["model"]),
            allowed_tool_names=allowed_tool_names,
            fallback_tool_url=extract_recent_user_url(list(payload.get("messages", []))), # type: ignore[arg-type]
            debug_enabled=self.config.debug_dump_all,
            logger=self.logger,
            prompt_messages=list(payload.get("messages", [])) if isinstance(payload.get("messages"), list) else None,
            tools_schema=list(payload.get("tools", [])) if isinstance(payload.get("tools"), list) else None,
        )

        # 流式复读检测：buffer 一定量文本后再决定是否输出
        # 避免 codex 长任务场景下复读文本被部分输出后才报错
        # _rep_buffer：缓存的可见文本 chunk（待 flush）
        # _rep_buffering：是否还在 buffer 阶段（达到阈值前）
        # _rep_triggered：是否已检测到复读（防止重复触发）
        state = {
            "buffering": True,            # 是否还在 buffer 阶段
            "buffered_chunks": [],        # 待 flush 的 OpenAI chunk 字节
            "buffered_text_len": 0,       # 已 buffer 的可见文本长度
            "rep_triggered": False,       # 是否已触发复读
        }
        REP_BUFFER_THRESHOLD = 240  # buffer 240 字符后检测（覆盖大多数复读模式）

        def _build_rep_error_chunk() -> bytes:
            """构造 OpenAI 兼容的 error chunk，让 codex 等客户端收到 error 后自动重试。
            以 [DONE] 结束当前流，错误通过单独 chunk 输出。
            """
            err_payload = {
                "error": {
                    "message": "GLM response detected as repetition loop, please retry",
                    "type": "upstream_error",
                    "code": "repetition_loop_detected",
                }
            }
            return f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8")

        def generate():
            try:
                for event in self._iter_sse_events(response):
                    if not event:
                        continue
                    self._raise_for_event_error(event, stream=True)
                    chunks, status = accumulator.consume_event(event)

                    if state["buffering"] and not state["rep_triggered"]:
                        # buffer 阶段：累积 chunks 但不 yield
                        for chunk in chunks:
                            # 解析 chunk 看是否含 content delta
                            try:
                                chunk_str = chunk.decode("utf-8", errors="replace")
                                # 简单估算：累积所有 chunk 字节，从 accumulator 取真实可见文本长度
                                state["buffered_chunks"].append(chunk)
                            except Exception:
                                state["buffered_chunks"].append(chunk)
                            # 从 accumulator 获取已累积的可见文本长度
                            state["buffered_text_len"] = len(accumulator._completion_text_buffer or "")

                            # 达到阈值时检测复读
                            if state["buffered_text_len"] >= REP_BUFFER_THRESHOLD:
                                # 用累积文本检测复读
                                fake_result = {"choices": [{"message": {"content": accumulator._completion_text_buffer}}]}
                                if self._is_repetition_loop(fake_result):
                                    # 检测到复读：不发任何 content，直接输出 error chunk
                                    self.logger.warning(
                                        "流式路径检测到 GLM 响应复读循环 model=%s 已发 error 让客户端重试",
                                        payload.get("model", "unknown"),
                                    )
                                    from ..admin.store import get_store as _get_admin_store
                                    try:
                                        _get_admin_store().record_repetition_event(
                                            model=str(payload.get("model", "unknown")),
                                            path="stream",
                                        )
                                    except Exception:
                                        pass
                                    state["rep_triggered"] = True
                                    state["buffering"] = False
                                    state["buffered_chunks"] = []  # 丢弃已 buffer 的 content
                                    yield _build_rep_error_chunk()
                                    return
                                else:
                                    # 不复读：flush buffer，恢复正常流式
                                    state["buffering"] = False
                                    for buffered in state["buffered_chunks"]:
                                        yield buffered.encode("utf-8") if isinstance(buffered, str) else buffered
                                    state["buffered_chunks"] = []
                                    break

                        # 如果还在 buffer 阶段且未达阈值，继续 for 循环（不 yield）
                        if state["buffering"]:
                            # 但要检查 finish 状态：如果 finish 了但 buffer 不足阈值，直接 flush
                            if status in {"finish", "intervene"}:
                                state["buffering"] = False
                                for buffered in state["buffered_chunks"]:
                                    yield buffered.encode("utf-8") if isinstance(buffered, str) else buffered
                                state["buffered_chunks"] = []
                                for chunk in accumulator.finalize(
                                    status=status,
                                    last_error=event.get("last_error") if isinstance(event.get("last_error"), dict) else None,
                                ):
                                    yield chunk.encode("utf-8")
                                return
                            continue

                    # 非 buffer 阶段（或已 flush）：正常 yield
                    if not state["rep_triggered"]:
                        for chunk in chunks:
                            yield chunk.encode("utf-8")

                    if status in {"finish", "intervene"}:
                        if not state["rep_triggered"]:
                            for chunk in accumulator.finalize(
                                status=status,
                                last_error=event.get("last_error") if isinstance(event.get("last_error"), dict) else None,
                            ):
                                yield chunk.encode("utf-8")
                        return

                # 流式结束但没收到 finish 事件
                if not state["rep_triggered"]:
                    if state["buffering"]:
                        # 整个流都未达阈值，flush
                        for buffered in state["buffered_chunks"]:
                            yield buffered.encode("utf-8") if isinstance(buffered, str) else buffered
                    for chunk in accumulator.finalize(status="stop"):
                        yield chunk.encode("utf-8")
            finally:
                response.close() # type: ignore
                self.delete_conversation(accumulator.conversation_id, assistant_id=assistant_id)
                lease.release()

        return generate()

    def _raise_for_event_error(self, event: dict[str, object], stream: bool) -> None:
        status = str(event.get("status", "")).strip().lower()
        last_error = event.get("last_error")
        event_error = self._extract_event_error(event)
        if status != "error" and not event_error and not isinstance(last_error, dict):
            return

        error_payload: dict[str, object] = {}
        if isinstance(last_error, dict):
            error_payload.update(last_error)
        if isinstance(event_error, dict):
            error_payload.update(event_error)
        if not error_payload and status != "error":
            return

        error_code = error_payload.get("error_code", error_payload.get("code"))
        error_message = str(
            error_payload.get("err_msg")
            or error_payload.get("message")
            or ("GLM stream request error" if stream else "GLM request error")
        ).strip()
        detail = f"code={error_code} " if error_code is not None else ""
        raise UpstreamAPIError(
            status_code=502,
            message=f"GLM 上游返回错误 | {detail}{error_message}".strip(),
            payload=error_payload or event,
        )

    def _extract_event_error(self, event: dict[str, object]) -> dict[str, object] | None:
        parts = event.get("parts")
        if not isinstance(parts, list):
            return None
        for part in parts:
            if not isinstance(part, dict):
                continue
            error = part.get("error")
            if isinstance(error, dict) and error:
                return error
            part_status = str(part.get("status", "")).strip().lower()
            if part_status == "error":
                return {"message": "GLM part status error"}
        return None

    def delete_conversation(self, conversation_id: str, assistant_id: str | None = None) -> None:
        if not self.config.glm_delete_conversation:
            return
        if not conversation_id:
            self.logger.warning("跳过删除 GLM 会话：未获取到 conversation_id assistant_id=%s", assistant_id or self.config.glm_assistant_id)
            return

        actual_assistant_id = assistant_id or self.config.glm_assistant_id
        body = json.dumps(
            {
                "assistant_id": actual_assistant_id,
                "conversation_id": conversation_id,
            }
        ).encode("utf-8")
        try:
            def send_request(account_index: int, access_token: str):
                timestamp, nonce, sign = build_sign()
                request = urllib.request.Request(
                    self.config.delete_conversation_url,
                    method="POST",
                    data=body,
                    headers={
                        **self.auth.get_browser_headers(),
                        "Authorization": f"Bearer {access_token}",
                        "Referer": "https://chatglm.cn/main/alltoolsdetail",
                        "X-Device-Id": self.auth.get_device_id_for_account(account_index),
                        "X-Nonce": nonce,
                        "X-Request-Id": self.auth.next_request_id_for_account(account_index),
                        "X-Sign": sign,
                        "X-Timestamp": timestamp,
                    },
                )
                return urllib.request.urlopen(request, timeout=self.config.request_timeout)

            with self._call_with_account_failover("delete_conversation", send_request) as response: # type: ignore
                payload = self.auth.read_json_response(response)
            status = payload.get("status", payload.get("code"))
            # 静默"conversation 不存在"类响应（并发场景下另一请求可能已删除同一会话）
            # 这是非致命的（请求本身已成功），不应产生 WARNING 噪声
            payload_msg = str(payload.get("message", "")) + str(payload.get("result", ""))
            is_already_deleted = (
                "conversation" in payload_msg and "不存在" in payload_msg
            ) or status == 404
            if is_already_deleted:
                self.logger.debug(
                    "GLM 会话已被并发删除（静默忽略）conversation_id=%s assistant_id=%s",
                    conversation_id,
                    actual_assistant_id,
                )
                return
            if status not in {0, None}:
                self.logger.warning(
                    "GLM 会话删除返回非成功状态 conversation_id=%s assistant_id=%s payload=%s",
                    conversation_id,
                    actual_assistant_id,
                    payload,
                )
                return
            self.logger.info(
                "已删除 GLM 会话 conversation_id=%s assistant_id=%s",
                conversation_id,
                actual_assistant_id,
            )
        except Exception as exc:
            # 异常时也区分"已不存在"场景，避免日志噪声
            exc_msg = str(exc)
            if "不存在" in exc_msg or "404" in exc_msg:
                self.logger.debug(
                    "GLM 会话删除时已不存在（静默忽略）conversation_id=%s assistant_id=%s",
                    conversation_id,
                    actual_assistant_id,
                )
            else:
                self.logger.warning(
                    "删除 GLM 会话失败 conversation_id=%s assistant_id=%s error=%s",
                    conversation_id,
                    actual_assistant_id,
                    exc,
                )

    def _open_chat_stream(self, openai_payload: dict[str, object], preferred_account_index: int | None = None):
        requested_model = str(openai_payload.get("model", "glm-4"))
        upstream_model, assistant_id = resolve_upstream_model(requested_model, self.config)
        filtered_tools, _ = self._resolve_tools(openai_payload)
        # Translate response_format (json_object / json_schema) into a system instruction
        # GLM doesn't have a native JSON mode, but it can follow instructions reliably.
        response_format = openai_payload.get("response_format")
        messages_for_conversion = list(openai_payload.get("messages", []))  # type: ignore[arg-type]
        if isinstance(response_format, dict):
            rf_type = str(response_format.get("type", "")).strip().lower()
            if rf_type == "json_object":
                # OpenAI's documented behavior: when json_object is requested, the model
                # is told to always respond with valid JSON. We prepend a system message
                # if none exists, or augment the existing system message.
                json_instruction = (
                    "You are a helpful assistant designed to output JSON. "
                    "Always respond with valid, parseable JSON. "
                    "Do not include any text outside the JSON object. "
                    "Do not wrap the JSON in markdown code fences."
                )
                messages_for_conversion = self._inject_system_instruction(
                    messages_for_conversion, json_instruction
                )
            elif rf_type == "json_schema":
                # json_schema mode: provide the schema to the model
                schema = response_format.get("json_schema", {}).get("schema", {}) if isinstance(response_format.get("json_schema"), dict) else {}
                schema_str = json.dumps(schema, ensure_ascii=False, separators=(",", ":")) if schema else "{}"
                json_instruction = (
                    "You are a helpful assistant designed to output JSON matching a specific schema. "
                    "Always respond with valid, parseable JSON that matches this schema exactly:\n"
                    f"{schema_str}\n"
                    "Do not include any text outside the JSON object. "
                    "Do not wrap the JSON in markdown code fences."
                )
                messages_for_conversion = self._inject_system_instruction(
                    messages_for_conversion, json_instruction
                )
        converted_messages = convert_messages(
            messages=messages_for_conversion,
            tools=filtered_tools,
            blocked_tool_names={name.strip() for name in self.config.blocked_tool_names if name.strip()},
            tool_choice=openai_payload.get("tool_choice"),
            server_side_tool_names=SERVER_SIDE_TOOL_NAMES,
        )
        debug_dump(self.logger, self.config.debug_dump_all, "OpenAI 原始 chat 请求 payload", openai_payload)
        debug_dump(self.logger, self.config.debug_dump_all, "转换后的 GLM messages", converted_messages)
        refs = self._upload_referenced_files(list(openai_payload.get("messages", []))) # type: ignore
        if refs:
            converted_messages[0]["content"] = refs + list(converted_messages[0]["content"]) # type: ignore
            debug_dump(self.logger, self.config.debug_dump_all, "附加上传引用后的 GLM messages", converted_messages)

        chat_mode = resolve_chat_mode(
            model=requested_model,
            reasoning_effort=openai_payload.get("reasoning_effort"),
            deep_research=openai_payload.get("deep_research"),
        )
        is_networking = resolve_networking(
            model=requested_model,
            web_search=openai_payload.get("web_search"),
        )

        request_body = json.dumps(
            {
                "assistant_id": assistant_id,
                "conversation_id": "",
                "project_id": "",
                "chat_type": "user_chat",
                "messages": converted_messages,
                "meta_data": {
                    "channel": "",
                    "chat_mode": chat_mode,
                    "draft_id": "",
                    "if_plus_model": True,
                    "input_question_type": "xxxx",
                    "is_networking": is_networking,
                    "is_test": False,
                    "platform": "pc",
                    "quote_log_id": "",
                    "cogview": {"rm_label_watermark": False},
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.logger.info(
            "转发请求 model=%s upstream=%s stream=%s",
            requested_model,
            upstream_model,
            openai_payload.get("stream"),
        )
        debug_dump(self.logger, self.config.debug_dump_all, "转发到 GLM 的 chat 原始请求体", request_body)

        def send_request(account_index: int, access_token: str):
            for attempt in range(self.config.glm_busy_max_retries + 1):
                try:
                    timestamp, nonce, sign = build_sign()
                    request = urllib.request.Request(
                        self.config.chat_stream_url,
                        data=request_body,
                        method="POST",
                        headers={
                            **self.auth.get_browser_headers(),
                            "Authorization": f"Bearer {access_token}",
                            "X-Device-Id": self.auth.get_device_id_for_account(account_index),
                            "X-Nonce": nonce,
                            "X-Request-Id": self.auth.next_request_id_for_account(account_index),
                            "X-Sign": sign,
                            "X-Timestamp": timestamp,
                        },
                    )
                    debug_dump(
                        self.logger,
                        self.config.debug_dump_all,
                        f"转发到 GLM 的 chat 请求头 account={account_index} attempt={attempt + 1}",
                        dict(request.header_items()),
                    )
                    return self._prepare_chat_response(
                        urllib.request.urlopen(request, timeout=self.config.request_timeout)
                    )
                except urllib.error.HTTPError as exc:
                    error_payload = self._read_error_payload(exc)
                    if self._should_retry_busy_error(exc.code, error_payload) and attempt < self.config.glm_busy_max_retries:
                        wait_seconds = self.config.glm_busy_retry_interval
                        self.logger.warning(
                            "GLM 正在处理其他对话，等待重试 attempt=%s/%s wait=%.1fs account=%s",
                            attempt + 1,
                            self.config.glm_busy_max_retries,
                            wait_seconds,
                            account_index,
                        )
                        time.sleep(wait_seconds)
                        continue

                    message = self._build_error_message(exc.code, error_payload)
                    raise UpstreamAPIError(status_code=exc.code, message=message, payload=error_payload) from exc

            raise UpstreamAPIError(status_code=429, message="GLM 长时间忙碌，请稍后重试。")

        response = self._call_with_account_failover(
            f"chat:{requested_model}",
            send_request,
            preferred_account_index=preferred_account_index,
        )
        return response, assistant_id

    def _open_image_stream(self, payload: dict[str, object], preferred_account_index: int | None = None):
        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            raise UpstreamAPIError(status_code=400, message="图片生成请求缺少 prompt")

        size = str(payload.get("size", "1024x1024")).strip().lower()
        aspect_ratio = self._resolve_aspect_ratio(size)
        user_model = str(payload.get("model", self.config.glm_image_model_name)).strip() or self.config.glm_image_model_name
        request_body = json.dumps(
            {
                "assistant_id": self.config.glm_image_assistant_id,
                "conversation_id": "",
                "project_id": "",
                "chat_type": "user_chat",
                "meta_data": {
                    "cogview": {
                        "aspect_ratio": aspect_ratio,
                        "style": self._resolve_image_style(payload),
                        "scene": self._resolve_image_scene(payload),
                        "chat_model": "",
                        "rm_label_watermark": False,
                    },
                    "is_test": False,
                    "input_question_type": "xxxx",
                    "channel": "",
                    "draft_id": "",
                    "chat_mode": "",
                    "is_networking": False,
                    "quote_log_id": "",
                    "platform": "pc",
                },
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        self.logger.info(
            "转发绘图请求 model=%s assistant_id=%s size=%s n=%s",
            user_model,
            self.config.glm_image_assistant_id,
            size,
            payload.get("n", 1),
        )
        debug_dump(self.logger, self.config.debug_dump_all, "OpenAI 原始 image 请求 payload", payload)
        debug_dump(self.logger, self.config.debug_dump_all, "转发到 GLM 的 image 原始请求体", request_body)

        def send_request(account_index: int, access_token: str):
            timestamp, nonce, sign = build_sign()
            request = urllib.request.Request(
                self.config.chat_stream_url,
                data=request_body,
                method="POST",
                headers={
                    **self.auth.get_browser_headers(),
                    "Authorization": f"Bearer {access_token}",
                    "X-Device-Id": self.auth.get_device_id_for_account(account_index),
                    "X-Nonce": nonce,
                    "X-Request-Id": self.auth.next_request_id_for_account(account_index),
                    "X-Sign": sign,
                    "X-Timestamp": timestamp,
                },
            )
            debug_dump(
                self.logger,
                self.config.debug_dump_all,
                f"转发到 GLM 的 image 请求头 account={account_index}",
                dict(request.header_items()),
            )
            try:
                return self._prepare_chat_response(urllib.request.urlopen(request, timeout=self.config.request_timeout))
            except urllib.error.HTTPError as exc:
                error_payload = self._read_error_payload(exc)
                message = self._build_error_message(exc.code, error_payload)
                raise UpstreamAPIError(status_code=exc.code, message=message, payload=error_payload) from exc

        response = self._call_with_account_failover(
            f"image:{user_model}",
            send_request,
            preferred_account_index=preferred_account_index,
        )
        return response, self.config.glm_image_assistant_id

    def _prepare_chat_response(self, response):
        content_type = response.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            payload = self.auth.read_json_response(response)
            debug_dump(self.logger, self.config.debug_dump_all, "GLM 非流式原始 JSON 响应", payload)
            status = payload.get("status")
            message = str(payload.get("message", "")).strip()
            if status not in (0, None) or message:
                raise UpstreamAPIError(
                    status_code=502,
                    message=self._build_error_message(200, payload),
                    payload=payload,
                )

            response_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return BufferedReader(BytesIO(response_body))

        return self._wrap_stream_response(response)

    def _build_images_response(
        self,
        request_payload: dict[str, object],
        final_event: dict[str, object],
        accumulator: GLMEventAccumulator,
    ) -> dict[str, object]:
        requested_count = self._coerce_positive_int(request_payload.get("n"), default=1, maximum=10)
        response_format = str(request_payload.get("response_format", "url")).strip().lower()
        created = int(time.time())

        data: list[dict[str, object]] = []
        ordered_parts = list(accumulator.parts_by_logic_id.values())
        ordered_parts.sort(key=lambda item: str(item.get("logic_id", "")))

        for part in ordered_parts:
            if len(data) >= requested_count:
                break
            if not isinstance(part, dict):
                continue
            part_status = str(part.get("status", ""))
            if part_status != "finish":
                continue
            content_items = part.get("content", [])
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if len(data) >= requested_count:
                    break
                if not isinstance(content, dict) or content.get("type") != "image":
                    continue
                images = content.get("image", [])
                if not isinstance(images, list):
                    continue
                revised_prompt = str(content.get("code", "")).strip() or None
                for image in images:
                    if len(data) >= requested_count:
                        break
                    if not isinstance(image, dict):
                        continue
                    image_url = str(image.get("image_url", "")).strip()
                    if not image_url:
                        continue
                    item: dict[str, object] = {}
                    if response_format == "b64_json":
                        item["b64_json"] = self._download_image_as_base64(image_url)
                    else:
                        item["url"] = image_url
                    if revised_prompt:
                        item["revised_prompt"] = revised_prompt
                    data.append(item)

        if not data:
            raise UpstreamAPIError(
                status_code=502,
                message="GLM 绘图请求已完成，但未返回可用图片结果。",
                payload=final_event,
            )

        self.logger.info("绘图完成 返回图片数=%s", len(data))
        return {
            "created": created,
            "data": data,
        }

    def _resolve_aspect_ratio(self, size: str) -> str:
        normalized = size.strip().lower()
        if normalized in IMAGE_SIZE_TO_ASPECT_RATIO:
            return IMAGE_SIZE_TO_ASPECT_RATIO[normalized]
        if re.fullmatch(r"\d+x\d+", normalized):
            width_str, height_str = normalized.split("x", 1)
            width = max(int(width_str), 1)
            height = max(int(height_str), 1)
            return f"{width}:{height}"
        return "1:1"

    def _resolve_image_style(self, payload: dict[str, object]) -> str:
        style = str(payload.get("style", "none")).strip().lower()
        return style if style else "none"

    def _resolve_image_scene(self, payload: dict[str, object]) -> str:
        scene = str(payload.get("scene", "none")).strip().lower()
        return scene if scene else "none"

    def _coerce_positive_int(self, value: object, default: int, maximum: int) -> int:
        try:
            parsed = int(value) if value is not None else default # type: ignore
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(parsed, maximum))

    def _download_image_as_base64(self, image_url: str) -> str:
        try:
            with urllib.request.urlopen(image_url, timeout=self.config.request_timeout) as response:
                image_bytes = response.read()
            return base64.b64encode(image_bytes).decode("ascii")
        except Exception as exc:
            raise UpstreamAPIError(status_code=502, message=f"下载图片失败: {image_url} error={exc}") from exc

    def _iter_sse_events(self, response):
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")

        def emit_block(block: str):
            lines = [line for line in block.split("\n") if line.startswith("data:")]
            if not lines:
                return None
            payload = "\n".join(line[5:].strip() for line in lines)
            debug_dump(self.logger, self.config.debug_dump_all, "GLM 原始 SSE block", block)
            if payload == "[DONE]":
                return "[DONE]"
            try:
                parsed = json.loads(payload)
                debug_dump(self.logger, self.config.debug_dump_all, "GLM 解析后的 SSE payload", parsed)
                return parsed
            except json.JSONDecodeError:
                self.logger.debug("忽略无法解析的 SSE 片段: %s", payload)
                return None

        while True:
            stop_after_chunk = False
            try:
                raw_chunk = response.read(4096)
            except http.client.IncompleteRead as exc:
                raw_chunk = exc.partial or b""
                stop_after_chunk = True
                self.logger.warning("上游 SSE 连接提前断开(IncompleteRead)，按已接收内容收尾 bytes=%s", len(raw_chunk))
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
                self.logger.warning("上游 SSE 连接被重置(%s)，按已接收内容收尾", type(exc).__name__)
                raw_chunk = b""
                stop_after_chunk = True
            except (socket.timeout, TimeoutError) as exc:
                self.logger.warning("上游 SSE 读取超时(%s)，按已接收内容收尾", type(exc).__name__)
                raw_chunk = b""
                stop_after_chunk = True
            except OSError as exc:
                self.logger.warning("上游 SSE 网络 IO 异常(%s: %s)，按已接收内容收尾", type(exc).__name__, exc)
                raw_chunk = b""
                stop_after_chunk = True
            if not raw_chunk:
                if stop_after_chunk:
                    # 上游断了但还没收到 [DONE]，必须显式收尾，否则客户端会卡死等下一帧
                    break
                break

            pending += decoder.decode(raw_chunk, False).replace("\r\n", "\n")

            while "\n\n" in pending:
                block, pending = pending.split("\n\n", 1)
                event = emit_block(block.strip())
                if event == "[DONE]":
                    return
                if event is not None:
                    yield event

            if stop_after_chunk:
                break

        remaining = decoder.decode(b"", True)
        if remaining:
            pending += remaining

        if pending.strip():
            event = emit_block(pending.strip())
            if event not in (None, "[DONE]"):
                yield event
        # 关键兜底：上游异常断流时也补一个 [DONE]，让 OpenAI/Anthropic/Responses 客户端能正常结束
        # （由调用方在 yield 完后自己发 [DONE]，这里不直接 yield 字符串以避免类型混乱）

    def _upload_referenced_files(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        refs: list[dict[str, object]] = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "image_url":
                    url = item.get("image_url", {}).get("url")
                    if isinstance(url, str) and url:
                        ref = self._upload_file_reference(url, is_image=True)
                        if ref:
                            refs.append(ref)
                elif item_type == "file":
                    url = item.get("file_url", {}).get("url")
                    if isinstance(url, str) and url:
                        ref = self._upload_file_reference(url, is_image=False)
                        if ref:
                            refs.append(ref)
        if refs:
            self.logger.info("上传附件完成 成功数=%s", len(refs))
        return refs

    def _upload_file_reference(self, file_url: str, is_image: bool) -> dict[str, object] | None:
        try:
            filename, mime_type, payload = self._fetch_file_payload(file_url)
            boundary = _make_boundary()
            body = self._build_multipart(boundary, filename, mime_type, payload)
            upload_url = f"{self.config.glm_base_url}{FILE_UPLOAD_URL_SUFFIX}"
            debug_dump(
                self.logger,
                self.config.debug_dump_all,
                f"准备上传附件 url={file_url} filename={filename} mime={mime_type}",
                {"filename": filename, "mime_type": mime_type, "bytes": len(payload)},
            )

            def send_request(account_index: int, access_token: str):
                timestamp, nonce, sign = build_sign()
                request = urllib.request.Request(
                    upload_url,
                    method="POST",
                    data=body,
                    headers={
                        **self.auth.get_browser_headers(),
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Referer": "https://chatglm.cn/",
                        "X-Device-Id": self.auth.get_device_id_for_account(account_index),
                        "X-Nonce": nonce,
                        "X-Request-Id": self.auth.next_request_id_for_account(account_index),
                        "X-Sign": sign,
                        "X-Timestamp": timestamp,
                    },
                )
                debug_dump(
                    self.logger,
                    self.config.debug_dump_all,
                    f"转发到 GLM 的 file_upload 请求头 account={account_index}",
                    dict(request.header_items()),
                )
                debug_dump(
                    self.logger,
                    self.config.debug_dump_all,
                    f"转发到 GLM 的 file_upload 原始请求体 account={account_index}",
                    body,
                )
                return urllib.request.urlopen(request, timeout=self.config.request_timeout)

            with self._call_with_account_failover("file_upload", send_request) as response: # type: ignore
                result = self.auth.read_json_response(response).get("result", {})
            debug_dump(self.logger, self.config.debug_dump_all, "GLM 文件上传响应 result", result)
            source_id = result.get("source_id") # type: ignore
            file_result_url = result.get("file_url", file_url) # type: ignore
            if not source_id:
                return None
            if is_image:
                return {"type": "image_url", "image_url": {"url": file_result_url or source_id}}
            return {"type": "file", "file": [{"source_id": source_id, "file_url": file_result_url}]}
        except Exception as exc:
            self.logger.warning("上传附件失败 url=%s error=%s", file_url, exc)
            return None

    def _fetch_file_payload(self, file_url: str) -> tuple[str, str, bytes]:
        if file_url.startswith("data:"):
            header, encoded = file_url.split(",", 1)
            mime_type = header.split(";")[0][5:] or "application/octet-stream"
            extension = mimetypes.guess_extension(mime_type) or ".bin"
            payload = base64.b64decode(encoded)
            return f"upload-{uuid.uuid4().hex}{extension}", mime_type, payload

        parsed = urllib.parse.urlparse(file_url)
        filename = parsed.path.rsplit("/", 1)[-1] or f"upload-{uuid.uuid4().hex}.bin"
        with urllib.request.urlopen(file_url, timeout=self.config.request_timeout) as response:
            payload = response.read(FILE_SIZE_LIMIT + 1)
            if len(payload) > FILE_SIZE_LIMIT:
                raise ValueError("文件超过 100MB，拒绝上传。")
            mime_type = response.headers.get_content_type()
        mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return filename, mime_type, payload

    def _build_multipart(self, boundary: str, filename: str, mime_type: str, payload: bytes) -> bytes:
        start = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        end = f"\r\n--{boundary}--\r\n".encode("utf-8")
        return start + payload + end

    def _wrap_stream_response(self, response):
        content_encoding = response.headers.get("Content-Encoding", "").lower()
        if content_encoding == "gzip":
            return BufferedReader(gzip.GzipFile(fileobj=response))
        return response

    def _read_error_payload(self, error: urllib.error.HTTPError) -> dict[str, object]:
        try:
            raw_body = error.read()
            content_encoding = error.headers.get("Content-Encoding", "").lower()

            if content_encoding == "gzip":
                raw_body = gzip.decompress(raw_body)

            text = raw_body.decode("utf-8", errors="ignore")
        except Exception as exc:
            return {"message": f"读取上游错误响应失败: {exc}"}
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
        return {"message": text}

    def _should_retry_busy_error(self, status_code: int, payload: dict[str, object]) -> bool:
        if status_code != 429:
            return False
        message = str(payload.get("message", ""))
        inner_status = payload.get("status")
        return inner_status == 10061 or "请等待其他对话生成完毕" in message

    def _build_error_message(self, status_code: int, payload: dict[str, object]) -> str:
        message = str(payload.get("message", "")).strip()
        inner_status = payload.get("status")
        rid = payload.get("rid")
        parts = [f"GLM 请求失败 HTTP {status_code}"]
        if inner_status is not None:
            parts.append(f"status={inner_status}")
        if message:
            parts.append(message)
        if rid:
            parts.append(f"rid={rid}")
        return " | ".join(parts)

    def _get_preferred_account_index(self, ticket: int) -> int | None:
        account_count = self.auth.get_account_count()
        if account_count <= 0:
            return None
        return ticket % account_count

    def _call_with_account_failover(
        self,
        request_name: str,
        operation: Callable[[int, str], object],
        preferred_account_index: int | None = None,
    ):
        account_count = self.auth.get_account_count()
        if account_count <= 0:
            raise RuntimeError("没有可用的 GLM 账号或游客 token 配置")
        start_index = preferred_account_index % account_count if preferred_account_index is not None else self.auth.get_current_account_index()
        last_exc: Exception | None = None

        for offset in range(account_count):
            account_index = (start_index + offset) % account_count
            guest_retry_limit = self.config.glm_guest_max_retries if self.auth.is_guest_account(account_index) else 0
            for attempt in range(guest_retry_limit + 1):
                try:
                    access_token = self.auth.get_access_token_for_account(account_index)
                    # Record which account served this request (for admin metrics)
                    self._set_last_account_index(account_index)
                    return operation(account_index, access_token)
                except Exception as exc:
                    last_exc = exc
                    should_switch = self.auth.should_switch_account(exc)
                    # 游客频控检测：命中即轮换 device_id（核心修复）
                    # 智谱游客 chat 接口按 device_id 计数，持久化 device_id 用几次
                    # 就会被风控；只有换新 device_id + 重新 fetch token 才能恢复
                    from .glm_auth import is_guest_rate_limited as _is_rate_limited
                    is_rate_limited = _is_rate_limited(exc)
                    if is_rate_limited and self.auth.is_guest_account(account_index):
                        new_dev = self.auth.rotate_device_id_for_account(account_index)
                        self.logger.warning(
                            "检测到游客频控，已轮换 device_id account=%s new_dev=%s... 原错误=%s",
                            account_index, new_dev[:8], exc,
                        )
                        # 轮换后必须重试（不管 should_switch）
                        if attempt < guest_retry_limit:
                            backoff = min(2 ** attempt, 4)
                            time.sleep(backoff)
                            continue
                        # 重试用完，尝试切换到下一个账号
                        if account_count > 1:
                            self.auth.advance_account(account_index, f"{request_name}: 频控")
                            break
                        raise
                    if should_switch:
                        self.auth.invalidate_account(account_index)
                    if should_switch and attempt < guest_retry_limit:
                        # 指数退避：1s → 2s → 4s → 8s ...，避免连续 3 次秒级失败被风控
                        backoff = min(2 ** attempt, 16)
                        self.logger.warning(
                            "游客账号请求失败，%.1fs 后重新获取游客 ck 重试 attempt=%s/%s request=%s account=%s error=%s",
                            backoff,
                            attempt + 1,
                            guest_retry_limit,
                            request_name,
                            account_index,
                            exc,
                        )
                        time.sleep(backoff)
                        continue
                    if not should_switch or account_count == 1:
                        raise
                    self.auth.advance_account(account_index, f"{request_name}: {exc}")
                    break

        self.auth.reset_account_cycle()
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"账号轮换失败：{request_name}")
