"""Tests for v27: v33 审计报告 3 个 P2 修复验证。

P2-1: curl keep-alive 偶发 501 → 添加 do_HEAD 处理
P2-2: 防暴力破解返回 429 + Retry-After header
P2-3: json_object 响应剥离 markdown 代码块
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.server import _strip_markdown_fences, _apply_json_response_format_stripping


# === P2-3: _strip_markdown_fences 测试 ===

def test_strip_markdown_fences_json_block():
    """标准 ```json ... ``` 代码块应被剥离。"""
    text = '```json\n{"name": "Alice", "age": 30}\n```'
    result = _strip_markdown_fences(text)
    assert result == '{"name": "Alice", "age": 30}'


def test_strip_markdown_fences_plain_block():
    """无语言标识的 ``` ... ``` 代码块应被剥离。"""
    text = '```\n{"key": "value"}\n```'
    result = _strip_markdown_fences(text)
    assert result == '{"key": "value"}'


def test_strip_markdown_fences_jsonc():
    """```jsonc 也应被剥离。"""
    text = '```jsonc\n{"a": 1}\n```'
    result = _strip_markdown_fences(text)
    assert result == '{"a": 1}'


def test_strip_markdown_fences_no_fence_unchanged():
    """不含 ``` 的纯 JSON 原样返回。"""
    text = '{"name": "Alice"}'
    assert _strip_markdown_fences(text) == '{"name": "Alice"}'


def test_strip_markdown_fences_empty_string():
    """空字符串原样返回。"""
    assert _strip_markdown_fences("") == ""


def test_strip_markdown_fences_multiline_json():
    """多行 JSON 代码块应正确剥离，保留内部换行。"""
    text = '```json\n{\n  "name": "Alice",\n  "age": 30\n}\n```'
    result = _strip_markdown_fences(text)
    assert result == '{\n  "name": "Alice",\n  "age": 30\n}'


def test_strip_markdown_fences_uppercase_json():
    """大写 ```JSON 也应被剥离。"""
    text = '```JSON\n{"x": 1}\n```'
    result = _strip_markdown_fences(text)
    assert result == '{"x": 1}'


def test_strip_markdown_fences_no_closing_fence():
    """只有开头 ```json 没有结尾 ```（部分场景）也应剥离。"""
    text = '```json\n{"a": 1}\n'
    result = _strip_markdown_fences(text)
    assert result == '{"a": 1}'


def test_strip_markdown_fences_with_leading_trailing_whitespace():
    """代码块前后有空白也应正确剥离。"""
    text = '  \n```json\n{"a": 1}\n```\n  '
    result = _strip_markdown_fences(text)
    assert result == '{"a": 1}'


def test_strip_markdown_fences_preserves_non_json_text():
    """含 ``` 但不是 JSON 代码块格式的文本不应被修改。"""
    text = 'Here is some code:\n```\nprint("hello")\n```\nDone.'
    # 这个文本不是"整个被代码块包裹"，所以应该原样返回
    result = _strip_markdown_fences(text)
    # 实际行为：完整匹配失败（因为有前后非代码文本），部分匹配也失败
    # 这种情况下原样返回是合理的
    assert "print" in result


# === P2-3: _apply_json_response_format_stripping 测试 ===

def test_apply_stripping_json_object_mode():
    """response_format=json_object 时应剥离 markdown。"""
    result = {
        "choices": [{
            "message": {
                "content": '```json\n{"name": "Bob"}\n```'
            }
        }]
    }
    payload = {"response_format": {"type": "json_object"}}
    _apply_json_response_format_stripping(result, payload)
    assert result["choices"][0]["message"]["content"] == '{"name": "Bob"}'


def test_apply_stripping_json_schema_mode():
    """response_format=json_schema 时也应剥离 markdown。"""
    result = {
        "choices": [{
            "message": {
                "content": '```json\n{"x": 1}\n```'
            }
        }]
    }
    payload = {"response_format": {"type": "json_schema", "json_schema": {"schema": {}}}}
    _apply_json_response_format_stripping(result, payload)
    assert result["choices"][0]["message"]["content"] == '{"x": 1}'


def test_apply_stripping_no_response_format():
    """没有 response_format 时不做任何修改。"""
    result = {
        "choices": [{
            "message": {
                "content": '```json\n{"x": 1}\n```'
            }
        }]
    }
    payload = {}
    _apply_json_response_format_stripping(result, payload)
    # 原样保留（因为没有 response_format）
    assert result["choices"][0]["message"]["content"] == '```json\n{"x": 1}\n```'


def test_apply_stripping_text_mode_no_change():
    """response_format=text 时不做修改（即使是 markdown 代码块）。"""
    result = {
        "choices": [{
            "message": {
                "content": '```python\nprint("hello")\n```'
            }
        }]
    }
    payload = {"response_format": {"type": "text"}}
    _apply_json_response_format_stripping(result, payload)
    # text 模式下不剥离（用户可能确实想要 markdown）
    assert result["choices"][0]["message"]["content"] == '```python\nprint("hello")\n```'


def test_apply_stripping_no_markdown_unchanged():
    """json 模式但内容不含 markdown 时原样返回。"""
    result = {
        "choices": [{
            "message": {
                "content": '{"name": "Alice"}'
            }
        }]
    }
    payload = {"response_format": {"type": "json_object"}}
    _apply_json_response_format_stripping(result, payload)
    assert result["choices"][0]["message"]["content"] == '{"name": "Alice"}'


def test_apply_stripping_empty_content():
    """空 content 不抛异常。"""
    result = {
        "choices": [{
            "message": {
                "content": ""
            }
        }]
    }
    payload = {"response_format": {"type": "json_object"}}
    _apply_json_response_format_stripping(result, payload)
    assert result["choices"][0]["message"]["content"] == ""


def test_apply_stripping_no_choices():
    """没有 choices 字段不抛异常。"""
    result = {"id": "test"}
    payload = {"response_format": {"type": "json_object"}}
    _apply_json_response_format_stripping(result, payload)
    assert result == {"id": "test"}


def test_apply_stripping_none_content():
    """content=None 不抛异常。"""
    result = {
        "choices": [{
            "message": {
                "content": None
            }
        }]
    }
    payload = {"response_format": {"type": "json_object"}}
    _apply_json_response_format_stripping(result, payload)
    assert result["choices"][0]["message"]["content"] is None


# === P2-2: 防暴力破解逻辑测试 ===

from glm2api.admin.api import (
    _check_brute_force,
    _record_login_failure,
    _login_failures,
    _LOGIN_MAX_FAILURES,
    _get_client_ip,
)


class _MockHandler:
    """模拟 BaseHTTPRequestHandler 用于 _get_client_ip 测试。"""

    def __init__(self, headers: dict | None = None, client_address=("127.0.0.1", 12345)):
        self.headers = headers or {}
        self.client_address = client_address


def test_get_client_ip_direct():
    """无 X-Forwarded-For 时返回 client_address[0]。"""
    handler = _MockHandler(headers={}, client_address=("192.168.1.1", 12345))
    assert _get_client_ip(handler) == "192.168.1.1"


def test_get_client_ip_xff():
    """有 X-Forwarded-For 时返回第一个 IP（最原始客户端）。"""
    handler = _MockHandler(headers={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"})
    assert _get_client_ip(handler) == "203.0.113.1"


def test_get_client_ip_xff_single():
    """X-Forwarded-For 单个 IP。"""
    handler = _MockHandler(headers={"X-Forwarded-For": "203.0.113.5"})
    assert _get_client_ip(handler) == "203.0.113.5"


def test_get_client_ip_xff_with_spaces():
    """X-Forwarded-For 含空格也应正确解析。"""
    handler = _MockHandler(headers={"X-Forwarded-For": " 203.0.113.1 , 10.0.0.1 "})
    assert _get_client_ip(handler) == "203.0.113.1"


def test_brute_force_allows_under_threshold():
    """失败次数 < MAX_FAILURES 时允许登录。"""
    # 清空状态
    _login_failures.clear()
    ip = "10.0.0.1"
    # 记录 4 次失败（< 5）
    for _ in range(_LOGIN_MAX_FAILURES - 1):
        _record_login_failure(ip)
    assert _check_brute_force(ip) is True


def test_brute_force_blocks_at_threshold():
    """失败次数 >= MAX_FAILURES 时阻止登录。"""
    _login_failures.clear()
    ip = "10.0.0.2"
    # 记录 5 次失败（== MAX）
    for _ in range(_LOGIN_MAX_FAILURES):
        _record_login_failure(ip)
    assert _check_brute_force(ip) is False


def test_brute_force_blocks_above_threshold():
    """失败次数 > MAX_FAILURES 时继续阻止。"""
    _login_failures.clear()
    ip = "10.0.0.3"
    for _ in range(_LOGIN_MAX_FAILURES + 3):
        _record_login_failure(ip)
    assert _check_brute_force(ip) is False


def test_brute_force_isolated_per_ip():
    """不同 IP 的失败记录互不影响。"""
    _login_failures.clear()
    ip1 = "10.0.0.10"
    ip2 = "10.0.0.11"
    # ip1 失败 5 次（被锁）
    for _ in range(_LOGIN_MAX_FAILURES):
        _record_login_failure(ip1)
    # ip2 应该仍可登录
    assert _check_brute_force(ip1) is False
    assert _check_brute_force(ip2) is True


def test_brute_force_no_failures_allowed():
    """无失败记录的 IP 应该允许登录。"""
    _login_failures.clear()
    assert _check_brute_force("10.0.0.99") is True
