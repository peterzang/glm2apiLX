"""v56b 修复：GLM 模型幻觉 'open' 工具的防护。

修复内容:
- P1-1: .env BLOCKED_TOOL_NAMES 加 'open'（与代码硬编码对齐）
- P1-2: system prompt 增强 — 明确列出可用工具 + 禁止工具列表
- P2: 文本检测不做（容易误杀合法 'open' 词汇），靠 system prompt 降低概率

注意：GLM 的 open 工具幻觉是模型层问题，glm2api 只能缓解不能根治。
"""
import os

import pytest

from glm2api.protocol.tool_protocol import build_tool_call_instructions, BLOCKED_NATIVE_TOOL_NAMES
from glm2api.services.translator import convert_messages


# === P1-1: BLOCKED_TOOL_NAMES 配置验证 ===

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
    """代码 BLOCKED_NATIVE_TOOL_NAMES 应包含 'open'。"""
    assert "open" in BLOCKED_NATIVE_TOOL_NAMES
    assert "open_url" in BLOCKED_NATIVE_TOOL_NAMES
    assert "open_link" in BLOCKED_NATIVE_TOOL_NAMES


# === P1-2: system prompt 增强 ===

def test_tool_protocol_prompt_lists_available_tools():
    """build_tool_call_instructions 应明确列出可用工具。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write", "Bash"],
        server_side_tool_names=set(),
    )
    # 应包含可用工具列表
    assert "Read" in prompt
    assert "Write" in prompt
    assert "Bash" in prompt
    # 应有 "Your available tools are" 语句
    assert "Your available tools are" in prompt
    # 应有 "MUST ONLY call tools from this list" 强调
    assert "MUST ONLY" in prompt or "only" in prompt.lower()


def test_tool_protocol_prompt_bans_open_tools():
    """build_tool_call_instructions 应禁止 open/open_url 等工具。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write"],
        server_side_tool_names=set(),
    )
    # 应提到 open 系列工具被禁止
    assert "open" in prompt.lower()
    assert "open_url" in prompt
    # v56b: 应包含 'open'（单独的，不只是 open_url）
    assert "`open`" in prompt or "open," in prompt.lower()


def test_tool_protocol_prompt_suggests_alternatives():
    """build_tool_call_instructions 应建议用可用工具替代。"""
    prompt = build_tool_call_instructions(
        tool_names=["Read", "Write", "Bash"],
        server_side_tool_names=set(),
    )
    # 应有"用 Read 读文件"之类的建议
    assert "Read" in prompt
    assert "closest available tool" in prompt or "use the closest" in prompt.lower()


def test_convert_messages_enhances_tool_prompt():
    """convert_messages 应在 system prompt 注入增强的工具提示。"""
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
    # system prompt 应包含增强的工具提示
    prompt = converted[0]["content"][0]["text"]
    assert "TOOL USE PROTOCOL" in prompt or "TOOL SCHEMAS" in prompt
    assert "Read" in prompt
    # 应禁止 open 工具
    assert "open" in prompt.lower()


# === 验证不破坏正常工具调用 ===

def test_normal_tool_call_still_works():
    """正常工具调用不受影响。"""
    tools = [
        {"type": "function", "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}
    ]
    prompt = build_tool_call_instructions(
        tool_names=["get_weather"],
        server_side_tool_names=set(),
    )
    assert "get_weather" in prompt
    assert "Your available tools are" in prompt


def test_empty_tools_no_crash():
    """空 tools 列表不应崩溃。"""
    prompt = build_tool_call_instructions(
        tool_names=[],
        server_side_tool_names=set(),
    )
    assert prompt  # 应返回非空字符串
    assert "TOOL USE PROTOCOL" in prompt
