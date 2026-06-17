"""Tests for repetition loop detection in glm_client.py.

Covers _is_repetition_loop() with various input scenarios:
- Short text (should not trigger)
- Single sentence repeated 5+ times (should trigger)
- 3 consecutive identical sentences (should trigger)
- Mixed non-repeating text (should not trigger)
- Empty / None content (should not trigger)
- Text just under threshold
- Long unique text (should not trigger)
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import 项目内模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.glm_client import GLMWebClient


class _FakeClient(GLMWebClient):
    """跳过 __init__ 的 GLMWebClient 子类，仅用于测试 _is_repetition_loop。"""
    def __init__(self):  # type: ignore[no-untyped-def]
        # 不调用 super().__init__，避免依赖 config/auth
        pass


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


def _make_result(content: str) -> dict:
    """构造一个 chat_completion 风格的 result 对象。"""
    return {"choices": [{"message": {"content": content}}]}


# === 短文本不应触发 ===

def test_short_text_no_trigger(client):
    """短文本（< 100 字符）不应触发复读检测。"""
    result = _make_result("Hello, how are you?")
    assert client._is_repetition_loop(result) is False


def test_empty_content_no_trigger(client):
    """空 content 不应触发。"""
    result = _make_result("")
    assert client._is_repetition_loop(result) is False


def test_none_content_no_trigger(client):
    """None content 不应触发。"""
    result = {"choices": [{"message": {"content": None}}]}
    assert client._is_repetition_loop(result) is False


# === 应触发：单句重复 5 次以上 ===

def test_single_sentence_repeated_5_times_triggers(client):
    """同一句话重复 5 次以上应触发复读检测。"""
    sentence = "I will create the complete CLI todo application with tests and documentation."
    # 5 次重复，每次用句号分隔
    content = (sentence + ". ") * 6  # 6 次重复，确保超过阈值
    assert len(content) >= 100  # 满足长度阈值
    result = _make_result(content)
    assert client._is_repetition_loop(result) is True


def test_consecutive_3_identical_sentences_triggers(client):
    """连续 3 句相同应触发复读检测。"""
    sentence = "Let me create the main file with proper error handling and validation."
    # 构造：unique intro + 3 句连续相同 + 多句 outro 满足句子数 ≥ 6
    content = (
        "First I will plan the implementation carefully. "
        "Then I will design the storage layer for the application. "
        + (sentence + ". ") * 3
        + "Now let me proceed with the actual implementation work. "
        + "Finally I will write comprehensive tests for the codebase."
    )
    assert len(content) >= 100
    result = _make_result(content)
    assert client._is_repetition_loop(result) is True


# === 不应触发：正常长文本 ===

def test_long_unique_text_no_trigger(client):
    """长但内容唯一的文本不应触发。"""
    content = (
        "To build a todo CLI app, we need to handle several components. "
        "First, the argument parser should support add, list, complete, and delete subcommands. "
        "Second, the storage layer must persist tasks to a JSON file. "
        "Third, each subcommand needs proper error handling for edge cases. "
        "Finally, we should write comprehensive tests covering all branches. "
        "The implementation should follow Python best practices and type hints."
    )
    assert len(content) >= 100
    result = _make_result(content)
    assert client._is_repetition_loop(result) is False


def test_mixed_text_with_some_repetition_no_trigger(client):
    """有少量重复但不达阈值的不应触发。"""
    content = (
        "I will create the file. "
        "I will create the file. "  # 重复 2 次（< 5 次阈值）
        "Then I will run the tests. "
        "Then I will fix any failures. "
        "Finally I will document the usage."
    )
    assert len(content) >= 100
    result = _make_result(content)
    assert client._is_repetition_loop(result) is False


# === 边界场景 ===

def test_text_under_100_chars_no_trigger(client):
    """文本刚好 99 字符（< 100 阈值）不应触发，即使重复。"""
    sentence = "Repeat me."
    content = (sentence + " ") * 9  # 90 字符
    assert len(content) < 100
    result = _make_result(content)
    assert client._is_repetition_loop(result) is False


def test_malformed_result_no_trigger(client):
    """结构异常的 result 不应触发（不应抛异常）。"""
    assert client._is_repetition_loop({}) is False
    assert client._is_repetition_loop({"choices": []}) is False
    assert client._is_repetition_loop({"choices": [{}]}) is False
    assert client._is_repetition_loop({"choices": [{"message": {}}]}) is False


def test_tool_calls_only_no_trigger(client):
    """只有 tool_calls 没有 content 的响应不应触发。"""
    result = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{"id": "call_1", "function": {"name": "bash", "arguments": "{}"}}],
            }
        }]
    }
    assert client._is_repetition_loop(result) is False
