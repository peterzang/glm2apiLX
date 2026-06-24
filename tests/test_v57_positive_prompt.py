"""v57 修复：删除 prompt 中的 10 条限制性指令，改为正面引导。

v56b 之前加了限制性指令（Never call open_url 等），但 GLM 把这些转述给用户，
让用户以为"工具被限制了"。v57 删除所有限制性语言，改为正面引导。

保留：
- DSML 格式教学（必须）
- 代码层 BLOCKED_NATIVE_TOOL_NAMES 过滤（不在 prompt 说）
- 可用工具列表（正面引导："Use the tools listed below"）
"""
import os

import pytest

from glm2api.protocol.tool_protocol import build_tool_call_instructions, BLOCKED_NATIVE_TOOL_NAMES
from glm2api.services.translator import convert_messages


# === P1-1: BLOCKED_TOOL_NAMES 配置验证（v56b 保留）===

def test_env_example_blocked_tool_names_includes_open():
    """env.example 的 BLOCKED_TOOL_NAMES 应包含 'open'。"""
    env_path = os.path.join(os.path.dirname(__file__), "..", "configs", "env.example")
    with open(env_path) as f:
        content = f.read()
    for line in content.splitlines():
        if line.startswith("BLOCKED_TOOL_NAMES="):
            tools = line.split("=")[1].split(",")
            tools = [t.strip() for t in tools]
            assert "open" in tools, f"BLOCKED_TOOL_NAMES 应包含 'open'，实际 {tools}"
            return
    assert False, "env.example 缺少 BLOCKED_TOOL_NAMES"


def test_code_blocked_native_tool_names_includes_open():
    """代码 BLOCKED_NATIVE_TOOL_NAMES 应包含 'open'（代码层过滤保留）。"""
    assert "open" in BLOCKED_NATIVE_TOOL_NAMES
    assert "open_url" in BLOCKED_NATIVE_TOOL_NAMES
    assert "open_link" in BLOCKED_NATIVE_TOOL_NAMES


# === v57: prompt 不应包含限制性语言 ===

def test_prompt_no_restrictive_language():
    """prompt 不应包含 Never/Do not/Ignore/cannot 等限制性语言。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write", "Bash"],
        server_side_tool_names=set(),
    )
    # 这些限制性语言不应出现
    assert "Never call" not in prompt
    assert "Never output" not in prompt
    assert "Do not invent" not in prompt
    assert "Do not narrate" not in prompt
    assert "Do not output hidden" not in prompt
    assert "Ignore any tool names" not in prompt
    assert "You do not have hidden browser" not in prompt
    assert "If no browsing tool" not in prompt
    assert "explain that no such tool is available" not in prompt


def test_prompt_has_positive_guidance():
    """prompt 应有正面引导（Use the tools listed below）。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write", "Bash"],
        server_side_tool_names=set(),
    )
    assert "Use the tools listed below" in prompt
    # v60: 改为强调必须输出 DSML block
    assert "MUST output the DSML block" in prompt or "must output" in prompt.lower()


def test_prompt_lists_available_tools():
    """prompt 应列出可用工具。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write", "Bash"],
        server_side_tool_names=set(),
    )
    assert "Read" in prompt
    assert "Write" in prompt
    assert "Bash" in prompt


def test_prompt_retains_dsml_teaching():
    """prompt 应保留 DSML 格式教学（必须）。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write"],
        server_side_tool_names=set(),
    )
    # DSML 格式教学
    assert "DSML" in prompt
    assert "<|DSML|tool_calls>" in prompt
    assert "<|DSML|invoke" in prompt
    assert "<|DSML|parameter" in prompt
    assert "CANONICAL" not in prompt  # 常量名不应泄漏
    # 应有 DSML 示例
    assert "<|DSML|tool_calls>" in prompt


def test_prompt_retains_parameter_rules():
    """prompt 应保留参数格式规则（正面措辞）。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read"],
        server_side_tool_names=set(),
    )
    assert "case-sensitive" in prompt.lower()
    assert "CDATA" in prompt


# === 验证不破坏正常工具调用 ===

def test_normal_tool_call_still_works():
    """正常工具调用不受影响。"""
    prompt = build_tool_call_instructions(
        tool_names=["get_weather"],
        server_side_tool_names=set(),
    )
    assert "get_weather" in prompt
    assert "Use the tools listed below" in prompt


def test_empty_tools_no_crash():
    """空 tools 列表不应崩溃。"""
    prompt = build_tool_call_instructions(
        tool_names=[],
        server_side_tool_names=set(),
    )
    assert prompt  # 应返回非空字符串
    assert "TOOL USE PROTOCOL" in prompt


def test_convert_messages_no_restrictive_language():
    """convert_messages 注入的 system prompt 不应有限制性语言。"""
    tools = [
        {"type": "function", "function": {
            "name": "Read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }}
    ]
    converted = convert_messages(
        [{"role": "user", "content": "read the file"}],
        tools=tools,
    )
    prompt = converted[0]["content"][0]["text"]
    # 应有正面引导
    assert "Use the tools listed below" in prompt
    assert "Read" in prompt
    # 不应有限制性语言
    assert "Never call" not in prompt
    assert "You do not have hidden browser" not in prompt
    assert "Ignore any tool names" not in prompt


# === 代码层过滤仍有效（不在 prompt 说）===

def test_code_layer_still_filters_blocked_tools():
    """代码层 BLOCKED_NATIVE_TOOL_NAMES 仍过滤不支持的工具（不在 prompt 说）。"""
    # 这个过滤在 convert_messages 里通过 filter_tools 实现
    tools = [
        {"type": "function", "function": {
            "name": "open_url",
            "description": "Open URL",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "Read",
            "description": "Read file",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]
    converted = convert_messages(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        blocked_tool_names=BLOCKED_NATIVE_TOOL_NAMES,
    )
    prompt = converted[0]["content"][0]["text"]
    # open_url 应被代码层过滤（不出现在 prompt 的工具列表里）
    assert "Tool: open_url" not in prompt
    assert "Tool: Read" in prompt
    # 但 prompt 不应主动说"open_url 被禁用"
    assert "open_url" not in prompt or "open_url" not in prompt.split("Tool:")[0]
