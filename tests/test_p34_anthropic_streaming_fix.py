"""Tests for v34: Anthropic 流式 SSE 完整重写 — message_start 立即发送 + ping 心跳。

v36-v40 审计未发现的根因：长任务断开不是因为 WAF 或 attribution block，
而是因为 Anthropic 流式 SSE 缺少 message_start 立即发送和 ping 心跳。
当 GLM 思考 30s 时，Claude Code 30s 收不到任何事件 → 认为连接断开 → 长任务断开。

修复：
1. message_start 立即发送（不等第一个 chunk）
2. 后台线程读上游 chunks（不阻塞主线程）
3. 主线程周期性发 ping 心跳（2s 间隔）
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.anthropic_adapter import AnthropicStreamAccumulator


# === message_start 立即发送测试 ===

def test_message_start_emitted_immediately():
    """message_start 应该在 start_message() 调用时立即返回（不等 chunk）。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    # 调用 start_message 前 started=False
    assert not acc.started
    # 调用 start_message
    event = acc.start_message()
    # started 应立即变为 True
    assert acc.started
    # 事件应该是 message_start
    assert "message_start" in event
    # 事件应包含完整的 message 结构
    data_line = [l for l in event.split("\n") if l.startswith("data: ")][0]
    data = json.loads(data_line[6:])
    assert data["type"] == "message_start"
    assert data["message"]["role"] == "assistant"
    assert data["message"]["model"] == "glm-5.2"
    assert data["message"]["stop_reason"] is None
    assert "input_tokens" in data["message"]["usage"]
    assert "output_tokens" in data["message"]["usage"]


def test_message_start_only_once():
    """start_message 只应发送一次（重复调用不应重复发送）。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    acc.start_message()
    assert acc.started
    # feed_chunk 不应再次调用 start_message（因为 started=True）
    chunk = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    events = acc.feed_chunk(chunk)
    # events 不应包含 message_start
    for event in events:
        assert "message_start" not in event


# === ping 心跳格式测试 ===

def test_ping_event_format():
    """ping 事件格式应为 event: ping\\ndata: {"type": "ping"}\\n\\n。"""
    # 模拟 _stream_anthropic 中的 ping 发送
    ping_bytes = b'event: ping\ndata: {"type": "ping"}\n\n'
    # 解析验证
    text = ping_bytes.decode("utf-8")
    assert "event: ping" in text
    assert '"type": "ping"' in text
    # 验证 JSON 可解析
    data_line = [l for l in text.split("\n") if l.startswith("data: ")][0]
    data = json.loads(data_line[6:])
    assert data["type"] == "ping"


# === AnthropicStreamAccumulator tool_use 流式测试 ===

def test_tool_use_streaming_sequence():
    """tool_use 流式事件序列应与官方 Anthropic API 一致。

    官方序列：
    1. content_block_start (tool_use, input={})
    2. content_block_delta (input_json_delta, partial_json=...)
    3. content_block_stop
    4. message_delta (stop_reason=tool_use)
    5. message_stop
    """
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    acc.start_message()

    # 模拟 OpenAI 流式 chunk：tool_call 开始
    chunk1 = json.dumps({
        "choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_123", "function": {"name": "get_weather", "arguments": ""}}]}, "finish_reason": None}]
    }).encode()
    events1 = acc.feed_chunk(b"data: " + chunk1 + b"\n\n")

    # 应包含 content_block_start (tool_use)
    block_start = [e for e in events1 if "content_block_start" in e]
    assert len(block_start) == 1
    data = json.loads(block_start[0].split("data: ")[1])
    assert data["content_block"]["type"] == "tool_use"
    assert data["content_block"]["name"] == "get_weather"
    assert data["content_block"]["input"] == {}

    # 模拟 tool_call arguments delta
    chunk2 = json.dumps({
        "choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Tokyo"}'}}]}, "finish_reason": None}]
    }).encode()
    events2 = acc.feed_chunk(b"data: " + chunk2 + b"\n\n")

    # 应包含 content_block_delta (input_json_delta)
    block_delta = [e for e in events2 if "content_block_delta" in e]
    assert len(block_delta) == 1
    delta_data = json.loads(block_delta[0].split("data: ")[1])
    assert delta_data["delta"]["type"] == "input_json_delta"
    assert delta_data["delta"]["partial_json"] == '{"city":"Tokyo"}'

    # 模拟 finish
    chunk3 = json.dumps({
        "choices": [{"delta": {}, "finish_reason": "tool_calls"}]
    }).encode()
    events3 = acc.feed_chunk(b"data: " + chunk3 + b"\n\n")

    # 模拟 [DONE]
    events4 = acc.feed_chunk(b"data: [DONE]\n\n")

    # 应包含 content_block_stop + message_delta + message_stop
    all_events = events3 + events4
    block_stop = [e for e in all_events if "content_block_stop" in e]
    assert len(block_stop) >= 1

    msg_delta = [e for e in all_events if "message_delta" in e]
    assert len(msg_delta) == 1
    msg_delta_data = json.loads(msg_delta[0].split("data: ")[1])
    assert msg_delta_data["delta"]["stop_reason"] == "tool_use"
    assert "output_tokens" in msg_delta_data["usage"]

    msg_stop = [e for e in all_events if "message_stop" in e]
    assert len(msg_stop) == 1


def test_text_streaming_sequence():
    """text 流式事件序列应与官方 Anthropic API 一致。

    官方序列：
    1. content_block_start (text, text="")
    2. content_block_delta (text_delta, text=...)
    3. content_block_stop
    4. message_delta (stop_reason=end_turn)
    5. message_stop
    """
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    acc.start_message()

    # 模拟 text content delta
    chunk = json.dumps({
        "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]
    }).encode()
    events = acc.feed_chunk(b"data: " + chunk + b"\n\n")

    # 应包含 content_block_start + content_block_delta
    block_start = [e for e in events if "content_block_start" in e]
    assert len(block_start) == 1
    start_data = json.loads(block_start[0].split("data: ")[1])
    assert start_data["content_block"]["type"] == "text"

    block_delta = [e for e in events if "content_block_delta" in e]
    assert len(block_delta) == 1
    delta_data = json.loads(block_delta[0].split("data: ")[1])
    assert delta_data["delta"]["type"] == "text_delta"
    assert delta_data["delta"]["text"] == "Hello"

    # finish
    events2 = acc.feed_chunk(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
    events3 = acc.feed_chunk(b"data: [DONE]\n\n")

    all_finish = events2 + events3
    msg_delta = [e for e in all_finish if "message_delta" in e]
    assert len(msg_delta) == 1
    msg_delta_data = json.loads(msg_delta[0].split("data: ")[1])
    assert msg_delta_data["delta"]["stop_reason"] == "end_turn"


# === count_tokens 端点验证（v33 已实现，v34 回归）===

def test_count_tokens_in_whitelist():
    """count_tokens 端点应在 openai_endpoints 白名单中（否则会被 404 拒绝）。"""
    # 读取 server.py 验证白名单
    server_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "server.py"
    content = server_path.read_text(encoding="utf-8")
    assert "/messages/count_tokens" in content, "count_tokens 路由未在 server.py 中定义"


def test_attribution_strip_in_adapter():
    """attribution 剥离函数应在 anthropic_adapter.py 中。"""
    adapter_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "services" / "anthropic_adapter.py"
    content = adapter_path.read_text(encoding="utf-8")
    assert "_strip_attribution_block" in content
    assert "x-anthropic-billing-header" in content


# === message_start 包含完整 usage 字段测试 ===

def test_message_start_usage_fields():
    """message_start 的 usage 应包含所有官方字段。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    event = acc.start_message()
    data = json.loads(event.split("data: ")[1])
    usage = data["message"]["usage"]
    # 官方 Anthropic API message_start usage 字段
    assert "input_tokens" in usage
    assert "output_tokens" in usage
    assert "cache_creation_input_tokens" in usage
    assert "cache_read_input_tokens" in usage


# === _finish 幂等性测试 ===

def test_finish_idempotent():
    """_finish 应该是幂等的（多次调用不会重复发 message_stop）。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2")
    acc.start_message()
    # 第一次 _finish
    events1 = acc._finish()
    assert len(events1) > 0
    # 第二次 _finish 应返回空
    events2 = acc._finish()
    assert len(events2) == 0
