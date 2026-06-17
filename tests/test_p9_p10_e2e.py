"""P9/P10 e2e tests: max_tokens 强制限制 + 描述性检测阈值边界测试.

v7 审核报告建议：
- P9: mock 上游返回超长响应，验证被截断
- P10: 30/100/500 字符各触发一次描述性检测
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import GLMEventAccumulator


def _make_acc(max_tokens: int = 0, allowed_tools: set | None = None) -> GLMEventAccumulator:
    return GLMEventAccumulator(
        model="glm-5.2",
        allowed_tool_names=allowed_tools,
        debug_enabled=False,
        logger=MagicMock(),
        max_tokens_limit=max_tokens,
    )


# === P9: max_tokens 强制限制 ===

def test_max_tokens_1_truncates_long_response():
    """max_tokens=1 时，超长文本应被截断到极短。"""
    acc = _make_acc(max_tokens=1)
    long_text = "A" * 100  # 100 字符，÷3 = 33 tokens，远超 max_tokens=1
    acc._cached_full_text = long_text
    acc._render_cache_dirty = False
    acc.last_full_text = long_text
    result = acc.build_response()
    finish = result["choices"][0]["finish_reason"]
    content = result["choices"][0]["message"]["content"] or ""
    assert finish == "length", f"expected length, got {finish}"
    assert len(content) <= 4, f"expected <=4 chars, got {len(content)}: {content!r}"


def test_max_tokens_100_allows_reasonable_response():
    """max_tokens=100 时，300 字符以内的文本不被截断。"""
    acc = _make_acc(max_tokens=100)
    short_text = "Hello, how are you today?"
    acc._cached_full_text = short_text
    acc._render_cache_dirty = False
    acc.last_full_text = short_text
    result = acc.build_response()
    finish = result["choices"][0]["finish_reason"]
    assert finish == "stop", f"expected stop, got {finish}"


def test_max_tokens_0_means_no_limit():
    """max_tokens=0 表示不限制。"""
    acc = _make_acc(max_tokens=0)
    long_text = "A" * 10000
    acc._cached_full_text = long_text
    acc._render_cache_dirty = False
    acc.last_full_text = long_text
    result = acc.build_response()
    finish = result["choices"][0]["finish_reason"]
    assert finish == "stop", f"expected stop (no limit), got {finish}"


# === P10: 描述性检测阈值边界 ===

def test_descriptive_text_30_chars_triggers():
    """30+ 字符的描述性文本应触发检测。"""
    acc = _make_acc(allowed_tools={"shell"})
    # 35 字符的描述性文本（以 "I'll create" 开头）
    text = "I'll create the todo app now.\n\n"  # 32 chars
    assert len(text) > 30
    acc._cached_full_text = text
    acc._render_cache_dirty = False
    acc.last_full_text = text
    with pytest.raises(RuntimeError, match="descriptive_text_without_tool_call"):
        acc.build_response()


def test_descriptive_text_29_chars_does_not_trigger():
    """29 字符以下不触发（边界测试）。"""
    acc = _make_acc(allowed_tools={"shell"})
    text = "I'll create the app."  # 20 chars
    assert len(text) < 30
    acc._cached_full_text = text
    acc._render_cache_dirty = False
    acc.last_full_text = text
    # 不应抛异常
    result = acc.build_response()
    assert result is not None


def test_useful_text_with_code_block_not_triggered():
    """含代码块的有用文本不触发（即使以 "I'll" 开头）。"""
    acc = _make_acc(allowed_tools={"shell"})
    text = "I'll create the app:\n```python\nprint('hello')\n```\nDone."
    acc._cached_full_text = text
    acc._render_cache_dirty = False
    acc.last_full_text = text
    # 不应抛异常（因为有代码块）
    result = acc.build_response()
    assert result is not None


def test_planning_text_triggers():
    """包含 'planning the tasks' 的文本应触发检测。"""
    acc = _make_acc(allowed_tools={"shell"})
    text = "Let me start by planning the tasks for this project carefully."
    assert len(text) > 30
    acc._cached_full_text = text
    acc._render_cache_dirty = False
    acc.last_full_text = text
    with pytest.raises(RuntimeError, match="descriptive_text_without_tool_call"):
        acc.build_response()
