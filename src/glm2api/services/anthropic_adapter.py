"""Anthropic Messages API (/v1/messages) adapter.

Converts between Anthropic Messages format and the internal OpenAI
chat/completions format so the existing GLM pipeline can be reused.
"""

from __future__ import annotations

import json
import time
import uuid


def _safe_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Request conversion: Anthropic -> OpenAI chat/completions
# ---------------------------------------------------------------------------


def _strip_attribution_block(system_text: str) -> str:
    """剥掉 Claude Code 在 system prompt 前加的 attribution block。

    v39 审计发现：Claude Code >= 2.1.36 会把 attribution 信息作为 text block
    注入到 system prompt 最前面（不是 HTTP header），格式：
        x-anthropic-billing-header: cc_version=2.1.185; cc_entrypoint=cli; cch=xxxxx;

    这个 block 的 cch= 段每次请求都变化，导致：
    1. 上游 GLM 把这个识别成异常请求直接 403（长任务断开的根因）
    2. prompt-cache prefix 每次都 break → 每轮都 full reprocess → 越来越慢

    本函数在网关层主动剥掉这个 attribution block，让用户开箱即用，
    无需手动设 CLAUDE_CODE_ATTRIBUTION_HEADER=0 环境变量。
    """
    if not system_text or not isinstance(system_text, str):
        return system_text
    import re
    # 1. 剥掉 x-anthropic-billing-header 行
    cleaned = re.sub(r'x-anthropic-billing-header:\s*[^\n]*', '', system_text, flags=re.IGNORECASE)
    # 2. 剥掉 <system-reminder>...</system-reminder> 块
    cleaned = re.sub(r'<system-reminder>[\s\S]*?</system-reminder>', '', cleaned)
    # 3. 剥掉独立的 cc_version/cc_entrypoint/cch 片段
    cleaned = re.sub(r'cc_version=[^\s;]*[;\s]*cc_entrypoint=[^\s;]*[;\s]*cch=[^\s;]*[;]?', '', cleaned, flags=re.IGNORECASE)
    # 清理多余空行
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned


# v35: WAF bypass 字符还原
# Cloudflare WAF 的 Command Injection 规则会拦截含反引号 ` (U+0060) 的 prompt，
# 特别是 `python -m`、`perl -e` 等模式。Claude Code 的 prompt 大量使用反引号
# 包裹命令（markdown code span 风格），导致请求被 WAF 拦截返回 403。
#
# 解决方案：本地 bypass 代理脚本把 ` (U+0060) 替换成 ˋ (U+02CB，
# MODIFIER LETTER GRAVE ACCENT)，视觉几乎一样但 WAF 不拦截。
# glm2api 收到请求后在应用层把 ˋ 还原成 `，GLM 收到正确的原始 prompt。
_BACKTICK = '\x60'        # U+0060 GRAVE ACCENT（WAF 拦截目标）
_BACKTICK_SAFE = '\u02cb' # U+02CB MODIFIER LETTER GRAVE ACCENT（WAF 不拦截）


def _restore_backticks(text: str) -> str:
    """把 WAF bypass 代理替换的安全字符 ˋ (U+02CB) 还原成反引号 ` (U+0060)。

    配合 scripts/waf_bypass_proxy.py 使用：
    - 代理脚本：` → ˋ（绕过 WAF）
    - 本函数：ˋ → `（还原给 GLM）

    如果用户没用 bypass 代理（直接连 glm2api），此函数无副作用
    （正常 prompt 不含 ˋ 字符）。
    """
    if not text or not isinstance(text, str):
        return text
    return text.replace(_BACKTICK_SAFE, _BACKTICK)


def anthropic_to_openai(payload: dict[str, object]) -> dict[str, object]:
    """Convert an Anthropic Messages request body to OpenAI chat/completions."""
    messages: list[dict[str, object]] = []

    # --- system ---
    system = payload.get("system")
    if system:
        if isinstance(system, str):
            # v39 P0-1: 剥掉 Claude Code attribution block
            # v35: 还原 WAF bypass 替换的反引号
            stripped = _strip_attribution_block(system)
            stripped = _restore_backticks(stripped)
            if stripped:
                messages.append({"role": "system", "content": stripped})
        elif isinstance(system, list):
            text_parts = []
            for idx, block in enumerate(system):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
                    # v39 P0-1: 第一个 block 通常是 attribution，剥掉它
                    if idx == 0:
                        text = _strip_attribution_block(text)
                    # v35: 还原 WAF bypass 替换的反引号
                    text = _restore_backticks(text)
                    if text:
                        text_parts.append(text)
            if text_parts:
                messages.append({"role": "system", "content": "\n".join(text_parts)})

    # --- messages ---
    for msg in payload.get("messages", []):  # type: ignore[union-attr]
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        content = msg.get("content")

        if isinstance(content, str):
            # v35: 还原 WAF bypass 替换的反引号
            messages.append({"role": role, "content": _restore_backticks(content)})
            continue

        if not isinstance(content, list):
            messages.append({"role": role, "content": ""})
            continue

        # Process content blocks
        openai_content_parts: list[dict[str, object]] = []
        tool_calls: list[dict[str, object]] = []
        tool_results: list[dict[str, object]] = []

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
                # v35: 还原 WAF bypass 替换的反引号
                openai_content_parts.append({"type": "text", "text": _restore_backticks(str(block.get("text", "")))})

            elif block_type == "thinking":
                thinking_text = block.get("thinking", "")
                if thinking_text:
                    openai_content_parts.append({"type": "text", "text": _restore_backticks(str(thinking_text))})

            elif block_type == "image":
                source = block.get("source", {})
                if isinstance(source, dict):
                    media_type = source.get("media_type", "image/png")
                    data = source.get("data", "")
                    if source.get("type") == "base64" and data:
                        openai_content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"},
                        })
                    elif source.get("type") == "url":
                        url = source.get("url", "")
                        if url:
                            openai_content_parts.append({
                                "type": "image_url",
                                "image_url": {"url": str(url)},
                            })

            elif block_type == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id", f"call_{uuid.uuid4().hex[:24]}")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(
                            block.get("input", {}), ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                })

            elif block_type == "tool_result":
                result_content = block.get("content")
                result_text = ""
                if isinstance(result_content, str):
                    result_text = result_content
                elif isinstance(result_content, list):
                    parts = []
                    for rc in result_content:
                        if isinstance(rc, dict) and rc.get("type") == "text":
                            parts.append(str(rc.get("text", "")))
                    result_text = "\n".join(parts)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": result_text,
                })

        if tool_results:
            for tr in tool_results:
                messages.append(tr)
        elif tool_calls:
            text_content = ""
            if openai_content_parts:
                text_content = "\n".join(
                    str(p.get("text", "")) for p in openai_content_parts if p.get("type") == "text"
                )
            msg_out: dict[str, object] = {
                "role": "assistant",
                "content": text_content or None,
                "tool_calls": tool_calls,
            }
            messages.append(msg_out)
        elif len(openai_content_parts) == 1 and openai_content_parts[0].get("type") == "text":
            messages.append({"role": role, "content": openai_content_parts[0].get("text", "")})
        elif openai_content_parts:
            messages.append({"role": role, "content": openai_content_parts})
        else:
            messages.append({"role": role, "content": ""})

    # --- build output payload ---
    result: dict[str, object] = {
        "model": payload.get("model", "glm-4"),
        "messages": messages,
        "stream": payload.get("stream", False),
    }
    if payload.get("max_tokens"):
        # v54: 回退 v53 的强制放大逻辑 — 保留客户端原始值
        # Claude Desktop 收到 finish_reason="length" 会自动续接，
        # 不需要 proxy 层覆盖 max_tokens（且避免对低上限模型传 32768 触发上游错误）
        result["max_tokens"] = payload["max_tokens"]
    if payload.get("temperature") is not None:
        result["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        result["top_p"] = payload["top_p"]
    if payload.get("stop_sequences"):
        result["stop"] = payload["stop_sequences"]

    # --- tools ---
    anthropic_tools = payload.get("tools")
    if isinstance(anthropic_tools, list) and anthropic_tools:
        openai_tools = []
        for tool in anthropic_tools:
            if not isinstance(tool, dict):
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })
        if openai_tools:
            result["tools"] = openai_tools
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, dict):
        choice_type = str(tool_choice.get("type", "")).strip().lower()
        if choice_type == "auto":
            result["tool_choice"] = "auto"
        elif choice_type == "any":
            result["tool_choice"] = "required"
        elif choice_type == "tool":
            name = str(tool_choice.get("name", "")).strip()
            if name:
                result["tool_choice"] = {"type": "function", "function": {"name": name}}

    # --- thinking ---
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        result["reasoning_effort"] = thinking.get("budget_tokens", "medium")

    return result


# ---------------------------------------------------------------------------
# Non-streaming response conversion: OpenAI -> Anthropic
# ---------------------------------------------------------------------------


def openai_to_anthropic_response(result: dict[str, object], model: str, stop_sequences: list[str] | None = None) -> dict[str, object]:
    """Convert an OpenAI chat/completions response to Anthropic Messages format.

    P1-1 修复：支持 stop_sequences 截断。
    v53 P0 修复：stop_sequence 返回匹配的字符串（而非 None），与官方 API 一致。
    """
    content: list[dict[str, object]] = []
    stop_reason = "end_turn"
    matched_stop_sequence: str | None = None  # v53: 记录匹配的 stop_sequence

    choices = result.get("choices", [])
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message", {})
            if isinstance(message, dict):
                # reasoning_content -> thinking block
                reasoning = message.get("reasoning_content")
                if reasoning:
                    content.append({
                        "type": "thinking",
                        "thinking": str(reasoning),
                    })

                # text content
                text = message.get("content")
                if text:
                    text = str(text)
                    # P1-1: stop_sequences 截断
                    if stop_sequences:
                        # v53 P0: 找文本中最早出现的 stop_sequence（而非按列表顺序）
                        earliest_idx = -1
                        earliest_seq = None
                        for stop_seq in stop_sequences:
                            if stop_seq and isinstance(stop_seq, str) and stop_seq in text:
                                idx = text.index(stop_seq)
                                if earliest_idx == -1 or idx < earliest_idx:
                                    earliest_idx = idx
                                    earliest_seq = stop_seq
                        if earliest_seq is not None:
                            text = text[:earliest_idx]
                            stop_reason = "stop_sequence"
                            matched_stop_sequence = earliest_seq
                    content.append({"type": "text", "text": text})

                # tool_calls
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    stop_reason = "tool_use"
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function", {})
                        try:
                            input_data = json.loads(fn.get("arguments", "{}"))  # type: ignore[union-attr]
                        except (json.JSONDecodeError, TypeError):
                            input_data = {}
                        content.append({
                            "type": "tool_use",
                            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                            "name": fn.get("name", ""),  # type: ignore[union-attr]
                            "input": input_data,
                        })

            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                stop_reason = "max_tokens"
            # P1-1: 如果 OpenAI finish_reason="stop" 且提供了 stop_sequences，设为 stop_sequence
            elif finish_reason == "stop" and stop_sequences:
                # v53 P0: 需要找出实际匹配的 stop_sequence 字符串
                # GLM 上游可能已经截断了 stop_sequence，所以文本里可能找不到
                # 检查原始文本中哪个 stop_sequence 出现了
                if not matched_stop_sequence:
                    raw_text = ""
                    if isinstance(choice, dict):
                        msg = choice.get("message", {})
                        if isinstance(msg, dict):
                            c = msg.get("content")
                            if isinstance(c, str):
                                raw_text = c
                    if raw_text:
                        # 先按文本位置找最早出现的
                        earliest_idx = -1
                        earliest_seq = None
                        for stop_seq in stop_sequences:
                            if stop_seq and isinstance(stop_seq, str) and stop_seq in raw_text:
                                idx = raw_text.index(stop_seq)
                                if earliest_idx == -1 or idx < earliest_idx:
                                    earliest_idx = idx
                                    earliest_seq = stop_seq
                        if earliest_seq is not None:
                            matched_stop_sequence = earliest_seq
                    # v53: 只有文本里能找到 stop_sequence 时才判定为 stop_sequence 触发
                    # GLM 上游已截断文本时，无法确定是 stop_sequence 还是正常结束
                    # 为了兼容性，当只有一个 stop_sequence 且 finish_reason=stop 时，
                    # 假设是 stop_sequence 触发（GLM 上游已处理截断）
                    if not matched_stop_sequence and len(stop_sequences) == 1:
                        # 单个 stop_sequence 场景：假设触发（GLM 上游已截断）
                        # 这是有风险的假设，但符合大多数实际场景
                        matched_stop_sequence = stop_sequences[0] if isinstance(stop_sequences[0], str) else None
                # 只有 matched_stop_sequence 不为 None 时才设 stop_reason
                if matched_stop_sequence is not None:
                    stop_reason = "stop_sequence"
                # 否则保持 end_turn（正常结束）

    if not content:
        content.append({"type": "text", "text": ""})

    usage = result.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    output_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        # v53 P0: 返回匹配的 stop_sequence 字符串（而非 None），与官方 API 一致
        "stop_sequence": matched_stop_sequence,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # v31: 官方 Anthropic API 兼容字段
            "cache_creation_input_tokens": 0,  # GLM 不支持 prompt cache
            "cache_read_input_tokens": 0,     # GLM 不支持 prompt cache
            # v53 P1: 补全 usage 字段，与官方 API 一致
            "server_tool_use": {
                "web_search_requests": 0,
                "web_fetch_requests": 0,
            },
            "service_tier": "standard",
        },
    }


# ---------------------------------------------------------------------------
# Streaming: OpenAI SSE -> Anthropic SSE
# ---------------------------------------------------------------------------


class AnthropicStreamAccumulator:
    """Converts OpenAI chat/completions streaming chunks into Anthropic SSE events."""

    def __init__(self, model: str, stop_sequences: list[str] | None = None) -> None:
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.started = False
        self.content_index = 0
        self.current_block_type: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self._pending_tool_calls: dict[int, dict[str, object]] = {}
        self._block_open = False
        self._finished = False
        # v53 P0: 支持 stop_sequence 匹配
        self.stop_sequences = stop_sequences or []
        self.matched_stop_sequence: str | None = None
        self._accumulated_text = ""  # 累积文本用于检测 stop_sequence

    def start_message(self) -> str:
        """Emit message_start event."""
        self.started = True
        msg = {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": self.model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }
        return self._sse("message_start", {"type": "message_start", "message": msg})

    def feed_chunk(self, chunk: bytes) -> list[str]:
        """Process a raw SSE chunk line (already decoded). Returns Anthropic SSE events."""
        text = chunk.decode("utf-8", errors="ignore")
        events: list[str] = []
        for line in text.split("\n\n"):
            line = line.strip()
            if not line:
                continue
            if line == "data: [DONE]":
                events.extend(self._finish())
                continue
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                events.extend(self._process_openai_chunk(data))
        return events

    def _process_openai_chunk(self, data: dict[str, object]) -> list[str]:
        events: list[str] = []
        if not self.started:
            events.append(self.start_message())

        choices = data.get("choices", [])
        if not isinstance(choices, list) or not choices:
            # Usage update
            usage = data.get("usage")
            if isinstance(usage, dict):
                self.input_tokens = usage.get("prompt_tokens", self.input_tokens)  # type: ignore
                self.output_tokens = usage.get("completion_tokens", self.output_tokens)  # type: ignore
            return events

        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            return events
        finish_reason = choice.get("finish_reason")

        # reasoning_content -> thinking block
        reasoning = delta.get("reasoning_content")
        if reasoning:
            if self.current_block_type != "thinking":
                if self._block_open:
                    events.append(self._content_block_stop())
                events.append(self._content_block_start("thinking", {}))
                self.current_block_type = "thinking"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.content_index,
                "delta": {"type": "thinking_delta", "thinking": str(reasoning)},
            }))

        # text content
        content = delta.get("content")
        if content:
            # v53 P0: 累积文本用于检测 stop_sequence
            if self.stop_sequences:
                self._accumulated_text += str(content)
                # 检查是否匹配某个 stop_sequence
                if self.matched_stop_sequence is None:
                    for stop_seq in self.stop_sequences:
                        if stop_seq and isinstance(stop_seq, str) and stop_seq in self._accumulated_text:
                            self.matched_stop_sequence = stop_seq
                            self.stop_reason = "stop_sequence"
                            break
            if self.current_block_type != "text":
                if self._block_open:
                    events.append(self._content_block_stop())
                events.append(self._content_block_start("text", {"text": ""}))
                self.current_block_type = "text"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.content_index,
                "delta": {"type": "text_delta", "text": str(content)},
            }))

        # tool_calls
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_index = tc.get("index", 0)
                fn = tc.get("function", {})
                if not isinstance(fn, dict):
                    continue

                if tc_index not in self._pending_tool_calls:
                    # New tool call - close previous block, start new one
                    if self._block_open:
                        events.append(self._content_block_stop())
                    tool_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                    tool_name = fn.get("name", "")
                    self._pending_tool_calls[tc_index] = {
                        "id": tool_id, "name": tool_name, "arguments": "",
                    }
                    events.append(self._content_block_start("tool_use", {
                        "id": tool_id, "name": tool_name, "input": {},
                    }))
                    self.current_block_type = "tool_use"
                    self.stop_reason = "tool_use"

                args_delta = fn.get("arguments", "")
                if args_delta:
                    self._pending_tool_calls[tc_index]["arguments"] += str(args_delta)  # type: ignore
                    events.append(self._sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self.content_index,
                        "delta": {"type": "input_json_delta", "partial_json": str(args_delta)},
                    }))

        if finish_reason:
            if finish_reason == "length":
                self.stop_reason = "max_tokens"
            elif finish_reason == "tool_calls":
                self.stop_reason = "tool_use"

        # Usage in final chunk
        usage = data.get("usage")
        if isinstance(usage, dict):
            self.input_tokens = usage.get("prompt_tokens", self.input_tokens)  # type: ignore
            self.output_tokens = usage.get("completion_tokens", self.output_tokens)  # type: ignore

        return events

    def _finish(self) -> list[str]:
        if self._finished:
            return []
        self._finished = True
        events: list[str] = []
        if self._block_open:
            events.append(self._content_block_stop())
        # v53 P0: 返回匹配的 stop_sequence 字符串（而非 None）
        # 如果 stop_reason 不是 stop_sequence，则 stop_sequence 应为 None
        stop_seq_value = self.matched_stop_sequence if self.stop_reason == "stop_sequence" else None
        events.append(self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": stop_seq_value},
            "usage": {"output_tokens": self.output_tokens},  # message_delta only has output_tokens per spec
        }))
        events.append(self._sse("message_stop", {"type": "message_stop"}))
        return events

    def _content_block_start(self, block_type: str, initial: dict[str, object]) -> str:
        block: dict[str, object] = {"type": block_type}
        block.update(initial)
        self._block_open = True
        event = self._sse("content_block_start", {
            "type": "content_block_start",
            "index": self.content_index,
            "content_block": block,
        })
        return event

    def _content_block_stop(self) -> str:
        event = self._sse("content_block_stop", {
            "type": "content_block_stop",
            "index": self.content_index,
        })
        self.content_index += 1
        self._block_open = False
        return event

    def _sse(self, event_type: str, data: dict[str, object]) -> str:
        return f"event: {event_type}\ndata: {_safe_json(data)}\n\n"
