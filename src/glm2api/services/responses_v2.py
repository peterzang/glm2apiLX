"""OpenAI Responses API v2 adapter (/v1/responses).

v23 新增：完整适配新版 OpenAI Responses API 规范（2025+）。

与 v1 的区别（v1 在 responses_adapter.py 保留）：
1. text.format 支持 (json_schema / json_object / text)
2. reasoning 配置透传 (effort / summary)
3. metadata 支持
4. background 模式
5. truncation 策略
6. verbosity 控制
7. service_tier / prompt_cache_key / safety_identifier
8. max_tool_calls / top_logprobs
9. usage.input_tokens_details.cached_tokens
10. reasoning items 输出（summary）

v2 端点路径：/v1/responses（与 v1 共用，但通过 Accept header 或 body 版本字段区分）
实际实现：v2 是 v1 的超集，v1 请求会被 v2 正确处理（向后兼容）。

参考规范：
- https://developers.openai.com/api/reference/resources/responses/methods/create
- openai-python/src/openai/types/responses/response.py
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from ..core.openai_compat import (
    gen_function_call_id,
    gen_function_call_item_id,
    gen_message_id,
    gen_response_id,
    system_fingerprint,
)

_logger = logging.getLogger("glm2api.responses_v2")


def _safe_json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# v2 Request: Responses -> OpenAI chat/completions
# ---------------------------------------------------------------------------


def responses_v2_to_openai(payload: dict[str, object]) -> dict[str, object]:
    """将新版 Responses API 请求转为 chat/completions 格式。

    支持 v2 新字段：
    - text.format (json_schema / json_object)
    - reasoning (effort / summary)
    - metadata
    - background
    - truncation
    - verbosity
    - service_tier / prompt_cache_key / safety_identifier
    - max_tool_calls / top_logprobs
    """
    messages: list[dict[str, object]] = []

    # --- instructions -> system ---
    instructions = payload.get("instructions")
    if instructions and isinstance(instructions, str):
        messages.append({"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        # v2: instructions 可以是 input item 数组
        for item in instructions:
            if isinstance(item, dict):
                _append_response_item(messages, item)

    # --- input ---
    input_data = payload.get("input")
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict):
                continue
            _append_response_item(messages, item)

    # --- build base output ---
    result: dict[str, object] = {
        "model": payload.get("model", "glm-4"),
        "messages": messages,
        "stream": payload.get("stream", False),
    }

    # --- max_output_tokens -> max_tokens ---
    if payload.get("max_output_tokens") is not None:
        result["max_tokens"] = payload["max_output_tokens"]

    # --- temperature / top_p ---
    if payload.get("temperature") is not None:
        result["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        result["top_p"] = payload["top_p"]

    # --- v2: text.format -> response_format ---
    text_config = payload.get("text")
    if isinstance(text_config, dict):
        fmt = text_config.get("format")
        if isinstance(fmt, dict):
            fmt_type = fmt.get("type")
            if fmt_type == "json_schema":
                # 结构化输出：json_schema
                response_format: dict[str, object] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": fmt.get("name", "response"),
                        "schema": fmt.get("schema", {}),
                    },
                }
                if fmt.get("strict") is not None:
                    response_format["json_schema"]["strict"] = fmt["strict"]  # type: ignore[index]
                result["response_format"] = response_format
            elif fmt_type == "json_object":
                result["response_format"] = {"type": "json_object"}
            # type == "text" 是默认，不需要设置

        # v2: verbosity 控制（low/medium/high）
        verbosity = text_config.get("verbosity")
        if verbosity and isinstance(verbosity, str):
            # 转为 temperature 启发式：low=0.3, medium=0.7, high=1.0
            verbosity_temp = {"low": 0.3, "medium": 0.7, "high": 1.0}.get(verbosity.lower())
            if verbosity_temp is not None and "temperature" not in result:
                result["temperature"] = verbosity_temp

    # --- tools ---
    resp_tools = payload.get("tools")
    if isinstance(resp_tools, list) and resp_tools:
        openai_tools = []
        for tool in resp_tools:
            if not isinstance(tool, dict):
                continue
            tool_type = str(tool.get("type", ""))
            if tool_type == "function":
                function_tool = {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters", {}),
                    },
                }
                if tool.get("strict") is not None:
                    function_tool["function"]["strict"] = tool["strict"]  # type: ignore[index]
                openai_tools.append(function_tool)
            elif tool_type.startswith("web_search"):
                result["web_search"] = True  # type: ignore[assignment]
            # v2: 其他内置工具（file_search / code_interpreter / computer_use）暂不支持
        if openai_tools:
            result["tools"] = openai_tools

    if payload.get("tool_choice") is not None:
        result["tool_choice"] = payload["tool_choice"]

    # --- v2: parallel_tool_calls ---
    if payload.get("parallel_tool_calls") is not None:
        result["parallel_tool_calls"] = payload["parallel_tool_calls"]

    # --- v2: reasoning 配置 ---
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort:
            result["reasoning_effort"] = effort
        # reasoning.summary 暂不影响 chat/completions，仅记录
        summary = reasoning.get("summary")
        if summary:
            _logger.debug("reasoning.summary requested: %s", summary)

    # --- v2: metadata（透传到 result，不影响 chat/completions） ---
    metadata = payload.get("metadata")
    if metadata:
        result["_v2_metadata"] = metadata  # 仅供 v2 响应回填

    # --- v2: background（暂不支持真正的后台，但记录） ---
    if payload.get("background"):
        _logger.debug("background mode requested (treated as sync)")
        # 真正的后台模式需要 polling + job store，当前同步处理

    # --- v2: truncation ---
    truncation = payload.get("truncation")
    if truncation == "auto":
        # auto 模式：超长输入自动截断（chat/completions 默认行为）
        pass
    # disabled 模式：超长输入报错（也是默认行为）

    # --- v2: service_tier / prompt_cache_key / safety_identifier ---
    # 这些字段不影响 GLM 上游，仅记录
    for field in ("service_tier", "prompt_cache_key", "safety_identifier"):
        val = payload.get(field)
        if val:
            _logger.debug("%s=%s (ignored by GLM upstream)", field, val)

    # --- v2: max_tool_calls / top_logprobs ---
    if payload.get("max_tool_calls") is not None:
        _logger.debug("max_tool_calls=%s (not enforced)", payload["max_tool_calls"])
    if payload.get("top_logprobs") is not None:
        # GLM 不支持 logprobs，记录但不传递
        _logger.debug("top_logprobs=%s (not supported by GLM)", payload["top_logprobs"])

    # --- v2: previous_response_id（暂不支持状态化，需要 conversation store） ---
    if payload.get("previous_response_id"):
        _logger.debug("previous_response_id=%s (stateless mode, ignored)", payload["previous_response_id"])

    # --- v2: store（默认 false，无状态） ---
    # store=true 需要持久化响应，当前不实现

    # --- v2: user（已废弃，但兼容） ---
    if payload.get("user"):
        result["user"] = payload["user"]

    return result


def _append_response_item(messages: list[dict[str, object]], item: dict[str, object]) -> None:
    """将 Responses input item 追加到 chat messages。"""
    item_type = item.get("type")

    if item_type == "message" or (item_type is None and "content" in item):
        _append_response_message(messages, item)
    elif item_type == "function_call_output":
        call_id = str(item.get("call_id", ""))
        tool_name = ""
        for prev_msg in reversed(messages):
            if prev_msg.get("role") == "assistant" and isinstance(prev_msg.get("tool_calls"), list):
                for tc in prev_msg["tool_calls"]:  # type: ignore[union-attr]
                    if isinstance(tc, dict) and tc.get("id") == call_id:
                        fn = tc.get("function", {})
                        if isinstance(fn, dict):
                            tool_name = str(fn.get("name", ""))
                        break
                if tool_name:
                    break
        msg: dict[str, object] = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": str(item.get("output", "")),
        }
        if tool_name:
            msg["name"] = tool_name
        messages.append(msg)
    elif item_type == "function_call":
        try:
            args = item.get("arguments", "{}")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            args = "{}"
        tc_entry = {
            "id": str(item.get("call_id", f"call_{uuid.uuid4().hex[:24]}")),
            "type": "function",
            "function": {
                "name": str(item.get("name", "")),
                "arguments": args,
            },
        }
        if messages and messages[-1].get("role") == "assistant" and isinstance(messages[-1].get("tool_calls"), list):
            messages[-1]["tool_calls"].append(tc_entry)  # type: ignore[union-attr]
        else:
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [tc_entry],
            })
    elif item_type in ("reasoning",):
        # v2: reasoning item（输入侧）— 透传为 system 消息，让 GLM 知道之前的推理
        summary = item.get("summary")
        if summary:
            content = summary.get("content") if isinstance(summary, dict) else str(summary)
            if content:
                messages.append({"role": "system", "content": f"[Previous reasoning summary: {content}]"})


def _append_response_message(messages: list[dict[str, object]], item: dict[str, object]) -> None:
    """将 Responses message item 转为 chat message。"""
    role = str(item.get("role", "user"))
    content = item.get("content")
    if isinstance(content, str):
        messages.append({"role": role, "content": content})
        return
    if isinstance(content, list):
        parts: list[dict[str, object]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "output_text", "text"}:
                parts.append({"type": "text", "text": str(part.get("text", ""))})
            elif part_type in {"input_image", "image_url"}:
                image_url = part.get("image_url") or part.get("url")
                if isinstance(image_url, dict):
                    image_url = image_url.get("url")
                if image_url:
                    img: dict[str, object] = {"type": "image_url", "image_url": {"url": str(image_url)}}
                    detail = part.get("detail")
                    if detail:
                        img["image_url"]["detail"] = detail  # type: ignore[index]
                    parts.append(img)
        if parts:
            messages.append({"role": role, "content": parts})


# ---------------------------------------------------------------------------
# v2 Response: OpenAI chat/completions -> Responses
# ---------------------------------------------------------------------------


def openai_to_responses_v2(
    result: dict[str, object],
    model: str,
    request_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """将 chat/completions 响应转为新版 Responses API 格式。

    v2 新增字段：
    - usage.input_tokens_details.cached_tokens
    - reasoning items (summary)
    - metadata 回填
    - background / service_tier / truncation 回填
    - parallel_tool_calls 回填
    - text.format 回填
    """
    response_id = (
        result.get("id")
        if isinstance(result.get("id"), str) and str(result["id"]).startswith("resp_")
        else gen_response_id()
    )
    created = (
        int(result.get("created", time.time()))
        if isinstance(result.get("created"), (int, float))
        else int(time.time())
    )
    output: list[dict[str, object]] = []
    output_text_parts: list[str] = []
    status = "completed"
    incomplete_details: dict[str, object] | None = None

    choices = result.get("choices", [])
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message", {})
            if isinstance(message, dict):
                # v2: reasoning items (summary) — 如果有 reasoning_content，输出 reasoning summary item
                reasoning = message.get("reasoning_content")
                if reasoning and isinstance(reasoning, str) and reasoning.strip():
                    output.append({
                        "type": "reasoning",
                        "id": gen_message_id(),
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": reasoning.strip(),
                            }
                        ],
                        "status": "completed",
                    })

                # Build output message item
                msg_content: list[dict[str, object]] = []
                text = message.get("content")
                if text:
                    output_text_parts.append(str(text))
                    msg_content.append({
                        "type": "output_text",
                        "text": str(text),
                        "annotations": [],
                    })

                if msg_content:
                    output.append({
                        "type": "message",
                        "id": gen_message_id(),
                        "status": "completed",
                        "role": "assistant",
                        "content": msg_content,
                    })

                # Tool calls -> function_call items
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function", {})
                        try:
                            args_str = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
                        except (TypeError, ValueError):
                            args_str = "{}"
                        output.append({
                            "type": "function_call",
                            "id": gen_function_call_item_id(),
                            "call_id": str(tc.get("id") or gen_function_call_id()),
                            "name": fn.get("name", "") if isinstance(fn, dict) else "",
                            "arguments": str(args_str),
                            "status": "completed",
                        })

            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                status = "incomplete"
                incomplete_details = {"reason": "max_output_tokens"}

    usage = result.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    output_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0

    # v2: GLM 上游异常检测
    if output_tokens > 0 and not output and not output_text_parts:
        _logger.warning(
            "glm upstream anomaly: output_tokens=%d but output array is empty "
            "(model=%s, response_id=%s, input_tokens=%d).",
            output_tokens, model, response_id, input_tokens,
        )

    # v2: 从 request_payload 回填字段
    req = request_payload or {}
    metadata = req.get("metadata") or result.get("_v2_metadata")
    parallel_tool_calls = req.get("parallel_tool_calls", True)
    background = req.get("background", False)
    service_tier = req.get("service_tier")
    truncation = req.get("truncation")
    text_config = req.get("text")
    reasoning_config = req.get("reasoning")
    max_output_tokens = req.get("max_output_tokens")
    max_tool_calls = req.get("max_tool_calls")
    top_logprobs = req.get("top_logprobs")
    temperature = req.get("temperature")
    top_p = req.get("top_p")
    instructions = req.get("instructions")
    previous_response_id = req.get("previous_response_id")
    prompt_cache_key = req.get("prompt_cache_key")
    safety_identifier = req.get("safety_identifier")
    user = req.get("user")
    store = req.get("store", False)
    tool_choice = req.get("tool_choice", "auto")
    tools = req.get("tools", [])

    # v2: 计算推理 token（reasoning_content 的估算）
    reasoning_tokens = 0
    if reasoning and isinstance(reasoning, str):
        # 粗略估算：每 4 字符 1 token
        reasoning_tokens = len(reasoning) // 4

    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "error": None,
        "incomplete_details": incomplete_details,
        "instructions": instructions if isinstance(instructions, str) else None,
        "metadata": metadata if isinstance(metadata, dict) else None,
        "model": model,
        "output": output,
        "output_text": "".join(output_text_parts),
        "parallel_tool_calls": bool(parallel_tool_calls),
        "previous_response_id": previous_response_id if isinstance(previous_response_id, str) else None,
        "store": bool(store),
        "system_fingerprint": system_fingerprint(model),
        "temperature": temperature if isinstance(temperature, (int, float)) else None,
        "top_p": top_p if isinstance(top_p, (int, float)) else None,
        "tool_choice": tool_choice,
        "tools": tools if isinstance(tools, list) else [],
        "max_output_tokens": max_output_tokens if isinstance(max_output_tokens, int) else None,
        "max_tool_calls": max_tool_calls if isinstance(max_tool_calls, int) else None,
        "top_logprobs": top_logprobs if isinstance(top_logprobs, int) else None,
        "background": bool(background),
        "completed_at": created if status == "completed" else None,
        "service_tier": service_tier if isinstance(service_tier, str) else None,
        "truncation": truncation if isinstance(truncation, str) else None,
        "text": text_config if isinstance(text_config, dict) else None,
        "reasoning": reasoning_config if isinstance(reasoning_config, dict) else None,
        "prompt_cache_key": prompt_cache_key if isinstance(prompt_cache_key, str) else None,
        "safety_identifier": safety_identifier if isinstance(safety_identifier, str) else None,
        "user": user if isinstance(user, str) else None,
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {
                "cached_tokens": 0,  # GLM 不支持 prompt cache
            },
            "output_tokens": output_tokens,
            "output_tokens_details": {
                "reasoning_tokens": reasoning_tokens,
            },
            "total_tokens": input_tokens + output_tokens,
        },
    }


# ---------------------------------------------------------------------------
# v2 Streaming: OpenAI SSE -> Responses v2 SSE
# ---------------------------------------------------------------------------


class ResponsesV2StreamAccumulator:
    """新版 Responses API 流式累加器。

    支持 v2 事件类型：
    - response.created
    - response.in_progress
    - response.output_item.added
    - response.content_part.added
    - response.output_text.delta
    - response.output_text.done
    - response.content_part.done
    - response.output_item.done
    - response.function_call_arguments.delta
    - response.function_call_arguments.done
    - response.reasoning_summary_text.delta (v2 新增)
    - response.completed
    - response.failed
    - response.incomplete
    """

    def __init__(self, model: str, request_payload: dict[str, object] | None = None) -> None:
        self.model = model
        self.request_payload = request_payload or {}
        self.response_id = gen_response_id()
        self.created_at = int(time.time())
        self.output: list[dict[str, object]] = []
        self.output_text_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.current_message_id: str | None = None
        self.current_content_index = 0
        self.function_calls: dict[int, dict[str, object]] = {}
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason: str | None = None
        self._started = False
        self._message_started = False
        self._reasoning_started = False

    def _initial_event(self) -> dict[str, object]:
        """构造 response.created 事件的基础数据。"""
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": "in_progress",
            "model": self.model,
            "output": [],
            "parallel_tool_calls": bool(self.request_payload.get("parallel_tool_calls", True)),
            "store": bool(self.request_payload.get("store", False)),
            "previous_response_id": self.request_payload.get("previous_response_id"),
            "temperature": self.request_payload.get("temperature"),
            "top_p": self.request_payload.get("top_p"),
            "tool_choice": self.request_payload.get("tool_choice", "auto"),
            "tools": self.request_payload.get("tools", []),
            "instructions": self.request_payload.get("instructions") if isinstance(self.request_payload.get("instructions"), str) else None,
            "max_output_tokens": self.request_payload.get("max_output_tokens"),
            "reasoning": self.request_payload.get("reasoning"),
            "text": self.request_payload.get("text"),
        }

    def consume_chunk(self, chunk: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
        """消费一个 OpenAI chat chunk，返回 v2 SSE 事件列表。

        返回 [(event_type, event_data), ...]
        """
        events: list[tuple[str, dict[str, object]]] = []

        if not self._started:
            self._started = True
            events.append(("response.created", self._initial_event()))
            events.append(("response.in_progress", self._initial_event()))

        choices = chunk.get("choices", [])
        if not isinstance(choices, list) or not choices:
            # usage only chunk
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self.input_tokens = usage.get("prompt_tokens", 0) or 0
                self.output_tokens = usage.get("completion_tokens", 0) or 0
            return events

        choice = choices[0]
        if not isinstance(choice, dict):
            return events

        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            delta = {}

        # reasoning_content -> reasoning summary delta (v2 新增)
        reasoning_delta = delta.get("reasoning_content")
        if reasoning_delta and isinstance(reasoning_delta, str) and reasoning_delta:
            if not self._reasoning_started:
                self._reasoning_started = True
                reasoning_item = {
                    "type": "reasoning",
                    "id": gen_message_id(),
                    "summary": [],
                    "status": "in_progress",
                }
                self.output.append(reasoning_item)
                events.append(("response.output_item.added", {
                    "output_index": len(self.output) - 1,
                    "item": reasoning_item,
                }))
            self.reasoning_parts.append(reasoning_delta)
            events.append(("response.reasoning_summary_text.delta", {
                "item_id": self.output[-1].get("id") if self.output else None,
                "output_index": len(self.output) - 1,
                "summary_index": 0,
                "delta": reasoning_delta,
            }))

        # text content
        content_delta = delta.get("content")
        if content_delta and isinstance(content_delta, str) and content_delta:
            if not self._message_started:
                self._message_started = True
                self.current_message_id = gen_message_id()
                msg_item = {
                    "type": "message",
                    "id": self.current_message_id,
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                self.output.append(msg_item)
                events.append(("response.output_item.added", {
                    "output_index": len(self.output) - 1,
                    "item": msg_item,
                }))
                # content part added
                content_part = {
                    "type": "output_text",
                    "text": "",
                    "annotations": [],
                }
                msg_item["content"].append(content_part)  # type: ignore[union-attr]
                self.current_content_index = 0
                events.append(("response.content_part.added", {
                    "item_id": self.current_message_id,
                    "output_index": len(self.output) - 1,
                    "content_index": 0,
                    "part": content_part,
                }))

            self.output_text_parts.append(content_delta)
            # 更新 message content
            msg_item = self.output[-1]
            if isinstance(msg_item.get("content"), list) and msg_item["content"]:
                msg_item["content"][0]["text"] += content_delta  # type: ignore[index]
            events.append(("response.output_text.delta", {
                "item_id": self.current_message_id,
                "output_index": len(self.output) - 1,
                "content_index": 0,
                "delta": content_delta,
            }))

        # tool_calls
        tool_calls_delta = delta.get("tool_calls")
        if isinstance(tool_calls_delta, list):
            for tc_delta in tool_calls_delta:
                if not isinstance(tc_delta, dict):
                    continue
                tc_index = tc_delta.get("index", 0)
                if tc_index not in self.function_calls:
                    # 新 function_call item
                    fc_id = gen_function_call_item_id()
                    call_id = tc_delta.get("id") or gen_function_call_id()
                    fc_item = {
                        "type": "function_call",
                        "id": fc_id,
                        "call_id": call_id,
                        "name": "",
                        "arguments": "",
                        "status": "in_progress",
                    }
                    self.function_calls[tc_index] = {
                        "item": fc_item,
                        "output_index": len(self.output),
                    }
                    self.output.append(fc_item)
                    events.append(("response.output_item.added", {
                        "output_index": len(self.output) - 1,
                        "item": fc_item,
                    }))
                fc_info = self.function_calls[tc_index]
                fn = tc_delta.get("function", {})
                if isinstance(fn, dict):
                    if fn.get("name"):
                        fc_info["item"]["name"] = fn["name"]  # type: ignore[index]
                    args_delta = fn.get("arguments", "")
                    if args_delta:
                        fc_info["item"]["arguments"] += args_delta  # type: ignore[index]
                        events.append(("response.function_call_arguments.delta", {
                            "item_id": fc_info["item"]["id"],
                            "output_index": fc_info["output_index"],
                            "delta": args_delta,
                        }))

        # finish_reason
        finish_reason = choice.get("finish_reason")
        if finish_reason:
            self.finish_reason = finish_reason
            # usage
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self.input_tokens = usage.get("prompt_tokens", 0) or 0
                self.output_tokens = usage.get("completion_tokens", 0) or 0

        return events

    def finalize(self) -> list[tuple[str, dict[str, object]]]:
        """流结束时生成 done 事件。"""
        events: list[tuple[str, dict[str, object]]] = []

        # 关闭 reasoning item
        if self._reasoning_started and self.output:
            reasoning_item = self.output[0]
            reasoning_item["status"] = "completed"  # type: ignore[index]
            reasoning_item["summary"] = [{  # type: ignore[index]
                "type": "summary_text",
                "text": "".join(self.reasoning_parts),
            }]
            events.append(("response.reasoning_summary_text.done", {
                "item_id": reasoning_item.get("id"),
                "output_index": 0,
                "summary_index": 0,
                "text": "".join(self.reasoning_parts),
            }))
            events.append(("response.output_item.done", {
                "output_index": 0,
                "item": reasoning_item,
            }))

        # 关闭 message item
        if self._message_started and self.current_message_id:
            # 找到 message item
            for i, item in enumerate(self.output):
                if item.get("type") == "message" and item.get("id") == self.current_message_id:
                    item["status"] = "completed"
                    # content part done
                    if isinstance(item.get("content"), list) and item["content"]:
                        content_part = item["content"][0]
                        events.append(("response.output_text.done", {
                            "item_id": self.current_message_id,
                            "output_index": i,
                            "content_index": 0,
                            "text": content_part.get("text", ""),
                        }))
                        events.append(("response.content_part.done", {
                            "item_id": self.current_message_id,
                            "output_index": i,
                            "content_index": 0,
                            "part": content_part,
                        }))
                    events.append(("response.output_item.done", {
                        "output_index": i,
                        "item": item,
                    }))
                    break

        # 关闭 function_call items
        for tc_index, fc_info in self.function_calls.items():
            fc_item = fc_info["item"]
            fc_item["status"] = "completed"
            events.append(("response.function_call_arguments.done", {
                "item_id": fc_item["id"],
                "output_index": fc_info["output_index"],
                "arguments": fc_item.get("arguments", ""),
            }))
            events.append(("response.output_item.done", {
                "output_index": fc_info["output_index"],
                "item": fc_item,
            }))

        # 最终 status
        status = "completed"
        incomplete_details = None
        if self.finish_reason == "length":
            status = "incomplete"
            incomplete_details = {"reason": "max_output_tokens"}

        # 计算推理 token
        reasoning_tokens = len("".join(self.reasoning_parts)) // 4

        final_response = self._initial_event()
        final_response.update({
            "status": status,
            "output": self.output,
            "output_text": "".join(self.output_text_parts),
            "incomplete_details": incomplete_details,
            "completed_at": int(time.time()) if status == "completed" else None,
            "usage": {
                "input_tokens": self.input_tokens,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": self.output_tokens,
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
                "total_tokens": self.input_tokens + self.output_tokens,
            },
        })

        if status == "incomplete":
            events.append(("response.incomplete", final_response))
        else:
            events.append(("response.completed", final_response))

        return events
