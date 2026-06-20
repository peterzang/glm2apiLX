"""v20 P2-1 修复验证：SYSTEM REMINDER 注入 4 空格缩进提示。

v20 报告 P2-1：task2 失败根因——GLM 输出 1 空格缩进（而非 4 空格），
导致 Python IndentationError。v20 修复在 SYSTEM REMINDER 中加 4 空格缩进提示。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import convert_messages


def test_system_reminder_includes_4_space_indentation_hint():
    """SYSTEM REMINDER 应包含 4 空格缩进提示（v20 P2-1）。"""
    tools = [{
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "array", "items": {"type": "string"}}},
            },
        },
    }]
    messages = [{"role": "user", "content": "Create hello.py"}]

    result = convert_messages(messages, tools, tool_choice="auto")
    # convert_messages 返回 list[dict]，content 是 list[dict]
    text = result[0]["content"][0]["text"]

    # 应该注入 SYSTEM REMINDER
    assert "[SYSTEM REMINDER:" in text
    # 应该包含 4 空格缩进提示
    assert "4-space indentation" in text, \
        f"SYSTEM REMINDER 应包含 4-space indentation 提示"


def test_system_reminder_still_has_tool_call_instruction():
    """v20 P2-1 修复后，原有的工具调用指令仍应保留。"""
    tools = [{
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute shell command",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    messages = [{"role": "user", "content": "test"}]

    result = convert_messages(messages, tools, tool_choice="auto")
    text = result[0]["content"][0]["text"]

    # 原有指令
    assert "Tools are available" in text
    assert "MUST be a tool call" in text
    # v20 新增
    assert "4-space indentation" in text


def test_system_reminder_not_injected_when_no_tools():
    """无工具时不应注入 SYSTEM REMINDER。"""
    messages = [{"role": "user", "content": "hello"}]
    result = convert_messages(messages, None, tool_choice="auto")
    text = result[0]["content"][0]["text"]
    assert "[SYSTEM REMINDER:" not in text
    assert "4-space indentation" not in text


def test_system_reminder_not_injected_when_tool_choice_none():
    """tool_choice=none 时不应注入 SYSTEM REMINDER。"""
    tools = [{
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute shell command",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    messages = [{"role": "user", "content": "test"}]

    result = convert_messages(messages, tools, tool_choice="none")
    text = result[0]["content"][0]["text"]
    assert "[SYSTEM REMINDER:" not in text
