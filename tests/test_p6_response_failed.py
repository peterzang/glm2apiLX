"""P6 验证：ResponsesStreamAccumulator 收到 Chat 风格 error chunk 时应转为 response.failed。

v4 审核报告 P6 bug：translator.py finalize() 返回 Chat Completions 风格 error chunk，
但 codex 走 wire_api=responses 期望 Responses API 风格的 response.failed 事件。

测试场景：
1. 模拟描述性文本检测触发的 error chunk → 应返回 response.failed 事件
2. 模拟复读检测触发的 error chunk → 应返回 response.failed 事件
3. 正常 chunk 不含 error → 不应返回 response.failed
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.responses_adapter import ResponsesStreamAccumulator


def _feed_chunk(accumulator: ResponsesStreamAccumulator, data: dict) -> list[str]:
    """构造一个 OpenAI Chat Completions 风格的 SSE chunk 并喂给 accumulator。"""
    chunk_bytes = f"data: {json.dumps(data)}\n\n".encode("utf-8")
    return accumulator.feed_chunk(chunk_bytes)


def test_error_chunk_converts_to_response_failed():
    """Chat 风格 error chunk 应转为 response.failed 事件。"""
    acc = ResponsesStreamAccumulator(model="glm-5.2")

    # 先喂一个正常 chunk 让 response.created 发出
    events = _feed_chunk(acc, {
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })

    # 喂一个描述性文本检测触发的 error chunk
    error_chunk = {
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "error": {
            "message": "Model generated descriptive text without calling available tools.",
            "type": "upstream_error",
            "code": "no_tool_call_descriptive_text",
        },
    }
    events = _feed_chunk(acc, error_chunk)

    # 合并所有事件
    all_events = "\n".join(events)

    # 应包含 response.failed
    assert "response.failed" in all_events, f"未找到 response.failed，events: {all_events[:500]}"

    # 应包含 [DONE]
    assert "[DONE]" in all_events, f"未找到 [DONE]"

    # 不应包含 response.completed（error 应替代 completed）
    assert "response.completed" not in all_events, f"不应出现 response.completed"

    # 验证 response.failed 事件的 JSON 结构
    for event_str in events:
        if "response.failed" in event_str:
            # 从 event_str 中提取 data: 行的 JSON
            for line in event_str.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line.replace("data: ", ""))
                    assert data["type"] == "response.failed"
                    assert data["response"]["status"] == "failed"
                    assert data["response"]["error"]["code"] == "no_tool_call_descriptive_text"
                    assert "descriptive text" in data["response"]["error"]["message"]
                    break
            break
    else:
        pytest.fail("未找到 response.failed 事件")


def test_repetition_error_chunk_converts_to_response_failed():
    """复读检测的 error chunk 也应转为 response.failed。"""
    acc = ResponsesStreamAccumulator(model="glm-5.2")

    # 先发 response.created
    _feed_chunk(acc, {
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })

    # 喂复读检测的 error chunk
    error_chunk = {
        "error": {
            "message": "GLM response detected as repetition loop, please retry",
            "type": "upstream_error",
            "code": "repetition_loop_detected",
        },
    }
    events = _feed_chunk(acc, error_chunk)
    all_events = "\n".join(events)

    assert "response.failed" in all_events
    assert "repetition_loop_detected" in all_events
    assert "response.completed" not in all_events


def test_normal_chunk_does_not_trigger_response_failed():
    """正常 chunk 不应触发 response.failed。"""
    acc = ResponsesStreamAccumulator(model="glm-5.2")

    events = _feed_chunk(acc, {
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}, "finish_reason": None}],
    })

    all_events = "\n".join(events)
    assert "response.failed" not in all_events
    assert "response.created" in all_events  # 正常启动


def test_error_chunk_after_finish_does_not_double_complete():
    """error chunk 到达后 _finished=True，后续 _finish() 不应再发 response.completed。"""
    acc = ResponsesStreamAccumulator(model="glm-5.2")

    # 先发 created
    _feed_chunk(acc, {
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })

    # 喂 error chunk
    _feed_chunk(acc, {
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "error": {"message": "test error", "code": "test_error"},
    })

    # 调 _finish() 模拟 server.py 的收尾逻辑
    finish_events = acc._finish()
    finish_str = "\n".join(finish_events)

    # _finish() 应返回空列表（因为 _finished=True）
    assert len(finish_events) == 0, f"_finish() 应返回空但返回了: {finish_str[:200]}"
