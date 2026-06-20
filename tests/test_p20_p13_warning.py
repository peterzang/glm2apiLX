"""v20 P1-3 验证：output_tokens > 0 但 output 空时记录 WARNING。

v19 报告 task9 失败：GLM 返回 output_tokens=230 但 output 数组为空，
glm2api 没记录任何警告，客户端收到空响应难以排查。

v20 修复：在 responses_adapter.py openai_to_responses 中检测这种异常并记录 WARNING。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.responses_adapter import openai_to_responses


def test_p13_warning_when_output_tokens_but_empty_output(caplog):
    """output_tokens > 0 但 output 数组为空时应记录 WARNING。

    v19 报告 task9 场景：GLM 返回 output_tokens=230 但 output 为空。
    v20 修复后应记录 WARNING 日志含 model/response_id/output_tokens。
    """
    # 模拟 GLM 返回：有 usage.completion_tokens 但 choices[0].message.content 为空
    openai_response = {
        "id": "chatcmpl-test123",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "glm-4-flash",
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
            "prompt_tokens": 10,
            "completion_tokens": 230,  # output_tokens > 0
            "total_tokens": 240,
        },
    }

    with caplog.at_level(logging.WARNING, logger="glm2api.responses_adapter"):
        result = openai_to_responses(openai_response, "glm-4-flash")

    # 验证 WARNING 被记录
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) >= 1, "应该记录 1 条 WARNING"

    warning_msg = warning_records[0].getMessage()
    assert "output_tokens=230" in warning_msg, f"WARNING 应含 output_tokens=230: {warning_msg}"
    assert "glm-4-flash" in warning_msg, f"WARNING 应含 model: {warning_msg}"
    assert "empty" in warning_msg.lower(), f"WARNING 应说明 output 为空: {warning_msg}"


def test_p13_no_warning_when_normal_response(caplog):
    """正常响应（有 content）不应记录 WARNING。"""
    openai_response = {
        "id": "chatcmpl-test456",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "glm-4-flash",
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

    with caplog.at_level(logging.WARNING, logger="glm2api.responses_adapter"):
        result = openai_to_responses(openai_response, "glm-4-flash")

    # 不应有 WARNING
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 0, f"正常响应不应记录 WARNING，实际: {[r.getMessage() for r in warning_records]}"


def test_p13_no_warning_when_zero_output_tokens(caplog):
    """output_tokens=0 且 output 空时不应记录 WARNING（不是异常）。"""
    openai_response = {
        "id": "chatcmpl-test789",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "glm-4-flash",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 0,  # 0 tokens
            "total_tokens": 10,
        },
    }

    with caplog.at_level(logging.WARNING, logger="glm2api.responses_adapter"):
        result = openai_to_responses(openai_response, "glm-4-flash")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 0, "output_tokens=0 时不应记录 WARNING"


def test_p13_no_warning_when_tool_calls_present(caplog):
    """有 tool_calls 时不应记录 WARNING（即使 content 为空）。"""
    openai_response = {
        "id": "chatcmpl-test-tool",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "glm-4-flash",
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

    with caplog.at_level(logging.WARNING, logger="glm2api.responses_adapter"):
        result = openai_to_responses(openai_response, "glm-4-flash")

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 0, "有 tool_calls 时不应记录 WARNING"
