"""v20 P3 修复验证：GLM 空 output 异常检测 + 自动重试。

v20 报告 P3：task9 失败——GLM 返回 output_tokens=286 但 output 数组为空。
v20 修复：在 glm_client.chat_completion 重试循环中检测空 output 异常并自动重试。

测试覆盖：
1. _is_empty_output_anomaly 检测函数
2. chat_completion 重试循环集成（mock _chat_completion_single）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.glm_client import GLMWebClient


def _make_empty_output_response(model: str = "glm-4-flash", completion_tokens: int = 286) -> dict:
    """构造一个 GLM 空 output 异常响应（output_tokens > 0 但 content 空）。"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1780000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",  # 空内容
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 321,
            "completion_tokens": completion_tokens,  # > 0
            "total_tokens": 321 + completion_tokens,
        },
    }


def _make_normal_response(model: str = "glm-4-flash") -> dict:
    """构造一个正常响应（有 content）。"""
    return {
        "id": "chatcmpl-normal",
        "object": "chat.completion",
        "created": 1780000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello, world!",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _make_tool_call_response(model: str = "glm-4-flash") -> dict:
    """构造一个有 tool_calls 的响应（即使 content 空也不算异常）。"""
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "created": 1780000000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_test",
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "arguments": '{"command": ["echo", "hi"]}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 50,
            "total_tokens": 60,
        },
    }


# === _is_empty_output_anomaly 单元测试 ===

def test_is_empty_output_anomaly_detects_empty_content_with_tokens():
    """output_tokens > 0 且 content 空时应检测为异常。"""
    client = GLMWebClient.__new__(GLMWebClient)
    result = _make_empty_output_response(completion_tokens=286)
    assert client._is_empty_output_anomaly(result) is True


def test_is_empty_output_anomaly_not_triggered_by_normal_response():
    """正常响应（有 content）不应检测为异常。"""
    client = GLMWebClient.__new__(GLMWebClient)
    result = _make_normal_response()
    assert client._is_empty_output_anomaly(result) is False


def test_is_empty_output_anomaly_not_triggered_by_tool_calls():
    """有 tool_calls（即使 content 空）不应检测为异常。"""
    client = GLMWebClient.__new__(GLMWebClient)
    result = _make_tool_call_response()
    assert client._is_empty_output_anomaly(result) is False


def test_is_empty_output_anomaly_not_triggered_by_zero_tokens():
    """output_tokens=0 且 content 空不应检测为异常（不是 GLM 异常）。"""
    client = GLMWebClient.__new__(GLMWebClient)
    result = _make_empty_output_response(completion_tokens=0)
    assert client._is_empty_output_anomaly(result) is False


def test_is_empty_output_anomaly_handles_malformed_response():
    """异常结构的响应不应抛异常（应返回 False）。"""
    client = GLMWebClient.__new__(GLMWebClient)
    # 各种异常结构
    assert client._is_empty_output_anomaly({}) is False
    assert client._is_empty_output_anomaly({"choices": []}) is False
    assert client._is_empty_output_anomaly({"choices": [{}]}) is False
    assert client._is_empty_output_anomaly({"choices": [{"message": {}}]}) is False
    assert client._is_empty_output_anomaly({"choices": [{"message": {"content": ""}}]}) is False  # 无 usage


# === chat_completion 重试循环集成测试 ===

def test_chat_completion_retries_on_empty_output_anomaly(monkeypatch):
    """chat_completion 应在空 output 异常时自动重试。

    v20 P3：mock _chat_completion_single 第一次返回空 output 异常，
    第二次返回正常响应，验证 chat_completion 重试成功。
    """
    client = GLMWebClient.__new__(GLMWebClient)
    client.logger = __import__("logging").getLogger("test")

    # mock _is_repetition_loop 返回 False（不触发复读检测）
    monkeypatch.setattr(client, "_is_repetition_loop", lambda result: False)
    # mock _inject_retry_hint 返回原 payload
    monkeypatch.setattr(client, "_inject_retry_hint", lambda payload: payload)

    call_count = {"n": 0}

    def mock_single(payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 第一次返回空 output 异常
            return _make_empty_output_response(), "conv_test"
        # 第二次返回正常响应
        return _make_normal_response(), "conv_test"

    monkeypatch.setattr(client, "_chat_completion_single", mock_single)

    # 调用 chat_completion（n=1，不走 _chat_completion_n 分支）
    payload = {"model": "glm-4-flash", "messages": [{"role": "user", "content": "hi"}], "n": 1}
    result, conv_id = client.chat_completion(payload)

    # 应该重试一次后成功
    assert call_count["n"] == 2, f"应该调用 2 次（第一次异常 + 重试），实际: {call_count['n']}"
    assert result["choices"][0]["message"]["content"] == "Hello, world!"


def test_chat_completion_returns_after_max_retries_on_persistent_empty_output(monkeypatch):
    """持续空 output 异常时，重试 MAX_ATTEMPTS 次后返回最后一次响应。"""
    client = GLMWebClient.__new__(GLMWebClient)
    client.logger = __import__("logging").getLogger("test")

    monkeypatch.setattr(client, "_is_repetition_loop", lambda result: False)
    monkeypatch.setattr(client, "_inject_retry_hint", lambda payload: payload)

    call_count = {"n": 0}

    def mock_single(payload):
        call_count["n"] += 1
        # 始终返回空 output 异常
        return _make_empty_output_response(), "conv_test"

    monkeypatch.setattr(client, "_chat_completion_single", mock_single)

    payload = {"model": "glm-4-flash", "messages": [{"role": "user", "content": "hi"}], "n": 1}
    result, conv_id = client.chat_completion(payload)

    # 应该重试 MAX_ATTEMPTS=3 次
    assert call_count["n"] == 3, f"应该调用 3 次（MAX_ATTEMPTS），实际: {call_count['n']}"
    # 最后返回空 output 响应（重试用尽后返回最后一次）
    assert result["choices"][0]["message"]["content"] == ""
    assert result["usage"]["completion_tokens"] == 286
