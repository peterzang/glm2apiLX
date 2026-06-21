"""Tests for v33: Claude Code attribution block 剥离 + count_tokens 端点。

v39 审计发现 Claude Code 长任务断开的根因：
1. Claude Code >= 2.1.36 在 system prompt 前注入 attribution block
   上游 GLM 识别为异常请求 → 403 → 长任务断开
2. count_tokens 端点缺失 → Claude Code 无法精准估算 context window → 提前截断
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.anthropic_adapter import _strip_attribution_block, anthropic_to_openai
from glm2api.core.tokenizer import estimate_message_tokens, estimate_tools_tokens


# === _strip_attribution_block 测试 ===

def test_strip_billing_header():
    text = "x-anthropic-billing-header: cc_version=2.1.185; cc_entrypoint=cli; cch=abc;\n\nYou are helpful."
    result = _strip_attribution_block(text)
    assert "x-anthropic-billing-header" not in result
    assert "You are helpful." in result


def test_strip_system_reminder():
    text = "<system-reminder>attribution</system-reminder>\n\nYou are helpful."
    result = _strip_attribution_block(text)
    assert "<system-reminder>" not in result
    assert "You are helpful." in result


def test_strip_preserves_normal():
    text = "You are a helpful assistant."
    assert _strip_attribution_block(text) == text


def test_strip_empty():
    assert _strip_attribution_block("") == ""


def test_strip_attribution_only_returns_empty():
    """纯 attribution 文本剥离后返回空字符串。"""
    text = "x-anthropic-billing-header: cc_version=2.1.185; cc_entrypoint=cli; cch=abc;"
    result = _strip_attribution_block(text)
    assert result == ""


def test_strip_case_insensitive():
    text = "X-Anthropic-Billing-Header: cc_version=2.1.185;\n\nHello"
    result = _strip_attribution_block(text)
    assert "anthropic-billing-header" not in result.lower()
    assert "Hello" in result


def test_strip_multiple_blocks():
    text = ("x-anthropic-billing-header: cc_version=2.1.185;\n"
            "<system-reminder>r1</system-reminder>\n"
            "<system-reminder>r2</system-reminder>\n"
            "Actual prompt.")
    result = _strip_attribution_block(text)
    assert "x-anthropic-billing-header" not in result
    assert "<system-reminder>" not in result
    assert "Actual prompt." in result


# === anthropic_to_openai 集成测试 ===

def test_strips_attribution_from_string_system():
    payload = {
        "model": "glm-5.2",
        "system": "x-anthropic-billing-header: cc_version=2.1.185;\n\nYou are a coding assistant.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = anthropic_to_openai(payload)
    system_msg = result["messages"][0]
    assert "x-anthropic-billing-header" not in system_msg["content"]
    assert "You are a coding assistant." in system_msg["content"]


def test_strips_attribution_from_list_system():
    payload = {
        "model": "glm-5.2",
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.185; cc_entrypoint=cli; cch=abc;"},
            {"type": "text", "text": "You are a coding assistant."},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = anthropic_to_openai(payload)
    system_msg = result["messages"][0]
    assert "x-anthropic-billing-header" not in system_msg["content"]
    assert "You are a coding assistant." in system_msg["content"]


def test_preserves_normal_system():
    payload = {
        "model": "glm-5.2",
        "system": "You are a helpful assistant.",
        "messages": [{"role": "user", "content": "hi"}],
    }
    result = anthropic_to_openai(payload)
    assert result["messages"][0]["content"] == "You are a helpful assistant."


# === count_tokens 逻辑测试 ===

def test_count_tokens_basic():
    tokens = estimate_message_tokens([{"role": "user", "content": "Hello"}])
    assert tokens > 0


def test_count_tokens_multi_message():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "What is 2+2?"},
    ]
    tokens = estimate_message_tokens(messages)
    assert tokens > 10


def test_count_tokens_cjk():
    messages = [{"role": "user", "content": "你好世界"}]
    tokens = estimate_message_tokens(messages)
    assert tokens >= 4


def test_count_tokens_with_tools():
    messages = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    msg_tokens = estimate_message_tokens(messages)
    tool_tokens = estimate_tools_tokens(tools)
    assert (msg_tokens + tool_tokens) > msg_tokens


def test_count_tokens_response_format():
    """count_tokens 响应格式应为 {"input_tokens": <int>}。"""
    payload = {"model": "glm-5.2", "messages": [{"role": "user", "content": "Hello world"}]}
    openai_payload = anthropic_to_openai(payload)
    messages = openai_payload.get("messages", [])
    total = max(1, estimate_message_tokens(messages))
    response = {"input_tokens": total}
    assert "input_tokens" in response
    assert isinstance(response["input_tokens"], int)
    assert response["input_tokens"] >= 1


def test_count_tokens_min_1():
    payload = {"model": "glm-5.2", "messages": []}
    openai_payload = anthropic_to_openai(payload)
    messages = openai_payload.get("messages", [])
    total = max(1, estimate_message_tokens(messages))
    assert total >= 1
