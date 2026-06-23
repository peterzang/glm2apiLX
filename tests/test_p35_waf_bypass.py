"""Tests for v35: WAF Bypass — 反引号替换 + 还原。

Cloudflare WAF 拦截含反引号 ` (U+0060) 的 prompt。
方案：本地代理把 ` 替换成 ˋ (U+02CB)，glm2api 收到后还原。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.anthropic_adapter import _restore_backticks, anthropic_to_openai
from glm2api.server import _restore_backticks_in_payload


# === _restore_backticks 测试 ===

BACKTICK = '\x60'
SAFE = '\u02cb'


def test_restore_backticks_basic():
    """ˋ 应被还原成 `。"""
    text = f"Run {SAFE}python -m foo{SAFE}"
    result = _restore_backticks(text)
    assert result == "Run `python -m foo`"
    assert SAFE not in result
    assert BACKTICK in result


def test_restore_backticks_no_safe_char():
    """不含 ˋ 的文本原样返回。"""
    text = "Hello world without backticks"
    assert _restore_backticks(text) == text


def test_restore_backticks_empty():
    """空字符串原样返回。"""
    assert _restore_backticks("") == ""


def test_restore_backticks_already_real():
    """已含真实反引号的文本不受影响。"""
    text = "Already has `real` backticks"
    result = _restore_backticks(text)
    assert result == text
    assert result.count(BACKTICK) == 2


def test_restore_backticks_mixed():
    """混合 ˋ 和 ` 的文本，只还原 ˋ。"""
    text = f"Mixed {SAFE}safe{SAFE} and {BACKTICK}real{BACKTICK}"
    result = _restore_backticks(text)
    assert result == "Mixed `safe` and `real`"
    assert SAFE not in result
    assert result.count(BACKTICK) == 4


# === _restore_backticks_in_payload 测试（递归）===

def test_restore_payload_string_system():
    """payload 中 system 字符串的 ˋ 被还原。"""
    payload = {
        "model": "glm-5.2",
        "system": f"Run {SAFE}python{SAFE} here",
        "messages": [{"role": "user", "content": "hi"}],
    }
    _restore_backticks_in_payload(payload)
    assert SAFE not in payload["system"]
    assert BACKTICK in payload["system"]


def test_restore_payload_list_system():
    """payload 中 system block list 的 ˋ 被还原。"""
    payload = {
        "model": "glm-5.2",
        "system": [
            {"type": "text", "text": f"attribution {SAFE}block{SAFE}"},
            {"type": "text", "text": f"Run {SAFE}python{SAFE}"},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    _restore_backticks_in_payload(payload)
    for block in payload["system"]:
        assert SAFE not in block["text"]
        assert BACKTICK in block["text"]


def test_restore_payload_messages():
    """payload 中 messages 的 ˋ 被还原。"""
    payload = {
        "model": "glm-5.2",
        "messages": [
            {"role": "user", "content": f"Run {SAFE}perl -e foo{SAFE}"},
            {"role": "assistant", "content": "ok"},
        ],
    }
    _restore_backticks_in_payload(payload)
    assert SAFE not in payload["messages"][0]["content"]
    assert BACKTICK in payload["messages"][0]["content"]


def test_restore_payload_nested_content_blocks():
    """payload 中 content block list 的 ˋ 被还原。"""
    payload = {
        "model": "glm-5.2",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": f"Run {SAFE}python{SAFE}"},
                {"type": "text", "text": f"Also {SAFE}perl{SAFE}"},
            ]},
        ],
    }
    _restore_backticks_in_payload(payload)
    for block in payload["messages"][0]["content"]:
        assert SAFE not in block["text"]
        assert BACKTICK in block["text"]


def test_restore_payload_no_safe_char_unchanged():
    """不含 ˋ 的 payload 不受影响。"""
    payload = {
        "model": "glm-5.2",
        "system": "Normal system prompt",
        "messages": [{"role": "user", "content": "Normal message"}],
    }
    original = json_copy(payload)
    _restore_backticks_in_payload(payload)
    assert payload == original


def test_restore_payload_tools_unchanged():
    """tools 中的 JSON schema 不受影响（不含 ˋ）。"""
    payload = {
        "model": "glm-5.2",
        "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    original = json_copy(payload)
    _restore_backticks_in_payload(payload)
    assert payload == original


def json_copy(obj):
    """深拷贝（用 JSON 序列化/反序列化）。"""
    import json
    return json.loads(json.dumps(obj))


# === anthropic_to_openai 集成测试 ===

def test_anthropic_to_openai_restores_backticks():
    """anthropic_to_openai 应正确还原 ˋ → `。"""
    payload = {
        "model": "glm-5.2",
        "system": f"Run {SAFE}python -m foo{SAFE}",
        "messages": [{"role": "user", "content": f"Test {SAFE}perl -e bar{SAFE}"}],
    }
    result = anthropic_to_openai(payload)
    system_content = result["messages"][0]["content"]
    user_content = result["messages"][1]["content"]
    assert SAFE not in system_content
    assert BACKTICK in system_content
    assert SAFE not in user_content
    assert BACKTICK in user_content


# === bypass 代理脚本验证 ===

def test_bypass_proxy_script_exists():
    """bypass 代理脚本应存在。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    assert script.exists(), "scripts/waf_bypass_proxy.py 不存在"


def test_bypass_proxy_has_replace_logic():
    """bypass 代理脚本应包含反引号替换逻辑。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    assert "_BACKTICK" in content
    assert "_BACKTICK_SAFE" in content
    assert "u02cb" in content.lower() or "\\u02cb" in content
    assert "replace" in content.lower()
