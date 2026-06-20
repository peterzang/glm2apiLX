"""v23 Responses API v2 测试：完整适配新版 OpenAI Responses API 规范。

测试覆盖：
1. responses_v2_to_openai 请求转换（含 v2 新字段）
2. openai_to_responses_v2 响应转换（含 v2 新字段回填）
3. text.format (json_schema / json_object / text)
4. reasoning 配置透传
5. metadata / background / truncation / verbosity
6. reasoning items (summary) 输出
7. usage.input_tokens_details.cached_tokens
8. 向后兼容（v1 请求仍工作）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.responses_v2 import (
    ResponsesV2StreamAccumulator,
    openai_to_responses_v2,
    responses_v2_to_openai,
)


# === 请求转换测试 ===

def test_v2_basic_request_conversion():
    """v2 基本请求应正确转换。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Hello",
        "max_output_tokens": 100,
    }
    result = responses_v2_to_openai(payload)
    assert result["model"] == "glm-4-flash"
    assert result["messages"] == [{"role": "user", "content": "Hello"}]
    assert result["max_tokens"] == 100
    assert result["stream"] is False


def test_v2_instructions_as_system():
    """v2 instructions 应转为 system 消息。"""
    payload = {
        "model": "glm-4-flash",
        "instructions": "You are a helpful assistant.",
        "input": "Hello",
    }
    result = responses_v2_to_openai(payload)
    assert result["messages"][0] == {"role": "system", "content": "You are a helpful assistant."}
    assert result["messages"][1] == {"role": "user", "content": "Hello"}


def test_v2_input_as_message_array():
    """v2 input 数组应正确转换。"""
    payload = {
        "model": "glm-4-flash",
        "input": [
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "message", "role": "assistant", "content": "Hi there"},
        ],
    }
    result = responses_v2_to_openai(payload)
    assert len(result["messages"]) == 2
    assert result["messages"][0]["role"] == "user"
    assert result["messages"][1]["role"] == "assistant"


def test_v2_text_format_json_schema():
    """v2 text.format json_schema 应转为 response_format。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Generate a person",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "person",
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
                "strict": True,
            }
        },
    }
    result = responses_v2_to_openai(payload)
    assert "response_format" in result
    assert result["response_format"]["type"] == "json_schema"
    assert result["response_format"]["json_schema"]["name"] == "person"
    assert result["response_format"]["json_schema"]["strict"] is True


def test_v2_text_format_json_object():
    """v2 text.format json_object 应转为 response_format。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Generate JSON",
        "text": {"format": {"type": "json_object"}},
    }
    result = responses_v2_to_openai(payload)
    assert result["response_format"] == {"type": "json_object"}


def test_v2_text_format_text_default():
    """v2 text.format text 应不设置 response_format（默认）。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Hello",
        "text": {"format": {"type": "text"}},
    }
    result = responses_v2_to_openai(payload)
    assert "response_format" not in result


def test_v2_verbosity_sets_temperature():
    """v2 verbosity 应启发式设置 temperature。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Hello",
        "text": {"verbosity": "low"},
    }
    result = responses_v2_to_openai(payload)
    assert result["temperature"] == 0.3


def test_v2_reasoning_effort_passthrough():
    """v2 reasoning.effort 应透传到 reasoning_effort。"""
    payload = {
        "model": "glm-5.2-think",
        "input": "Think about this",
        "reasoning": {"effort": "high", "summary": "auto"},
    }
    result = responses_v2_to_openai(payload)
    assert result["reasoning_effort"] == "high"


def test_v2_tools_conversion():
    """v2 tools 应正确转换。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Use tool",
        "tools": [
            {"type": "function", "name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {}}},
        ],
    }
    result = responses_v2_to_openai(payload)
    assert "tools" in result
    assert result["tools"][0]["function"]["name"] == "get_weather"


def test_v2_parallel_tool_calls():
    """v2 parallel_tool_calls 应透传。"""
    payload = {
        "model": "glm-4-flash",
        "input": "Hello",
        "parallel_tool_calls": False,
    }
    result = responses_v2_to_openai(payload)
    assert result["parallel_tool_calls"] is False


def test_v2_function_call_output_input():
    """v2 function_call_output 输入项应正确转换。"""
    payload = {
        "model": "glm-4-flash",
        "input": [
            {"type": "message", "role": "user", "content": "Get weather"},
            {"type": "function_call", "call_id": "call_123", "name": "get_weather", "arguments": '{"city":"NYC"}'},
            {"type": "function_call_output", "call_id": "call_123", "output": "Sunny, 72F"},
        ],
    }
    result = responses_v2_to_openai(payload)
    messages = result["messages"]
    # user message
    assert messages[0]["role"] == "user"
    # assistant with tool_calls
    assert messages[1]["role"] == "assistant"
    assert messages[1]["tool_calls"][0]["id"] == "call_123"
    # tool result
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_123"
    assert messages[2]["content"] == "Sunny, 72F"


def test_v2_reasoning_input_item():
    """v2 reasoning 输入项应转为 system 消息。"""
    payload = {
        "model": "glm-5.2-think",
        "input": [
            {"type": "reasoning", "summary": {"content": "Previous reasoning about weather"}},
            {"type": "message", "role": "user", "content": "Continue"},
        ],
    }
    result = responses_v2_to_openai(payload)
    messages = result["messages"]
    # reasoning item 应转为 system 消息
    assert any(m["role"] == "system" and "Previous reasoning" in m["content"] for m in messages)


# === 响应转换测试 ===

def test_v2_basic_response():
    """v2 基本响应应正确转换。"""
    openai_result = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1780000000,
        "model": "glm-4-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    result = openai_to_responses_v2(openai_result, "glm-4-flash")
    assert result["object"] == "response"
    assert result["status"] == "completed"
    assert result["output_text"] == "Hello!"
    assert result["output"][0]["type"] == "message"
    assert result["output"][0]["content"][0]["type"] == "output_text"
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 5
    assert result["usage"]["total_tokens"] == 15


def test_v2_response_with_input_tokens_details():
    """v2 响应应包含 input_tokens_details.cached_tokens。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-4-flash",
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    result = openai_to_responses_v2(openai_result, "glm-4-flash")
    assert "input_tokens_details" in result["usage"]
    assert result["usage"]["input_tokens_details"]["cached_tokens"] == 0
    assert "output_tokens_details" in result["usage"]
    assert result["usage"]["output_tokens_details"]["reasoning_tokens"] == 0


def test_v2_response_with_reasoning_summary():
    """v2 响应应输出 reasoning summary item。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-5.2-think",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "The answer is 391.",
                "reasoning_content": "17 * 23 = 17 * 20 + 17 * 3 = 340 + 51 = 391",
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 50},
    }
    result = openai_to_responses_v2(openai_result, "glm-5.2-think")
    # 第一个 output item 应该是 reasoning
    assert result["output"][0]["type"] == "reasoning"
    assert result["output"][0]["summary"][0]["type"] == "summary_text"
    assert "17 * 23" in result["output"][0]["summary"][0]["text"]
    # 第二个 output item 应该是 message
    assert result["output"][1]["type"] == "message"
    assert result["output"][1]["content"][0]["text"] == "The answer is 391."
    # usage 应含 reasoning_tokens
    assert result["usage"]["output_tokens_details"]["reasoning_tokens"] > 0


def test_v2_response_with_function_call():
    """v2 响应应输出 function_call item。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-4-flash",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }
    result = openai_to_responses_v2(openai_result, "glm-4-flash")
    fc_items = [o for o in result["output"] if o["type"] == "function_call"]
    assert len(fc_items) == 1
    assert fc_items[0]["call_id"] == "call_abc"
    assert fc_items[0]["name"] == "get_weather"
    assert fc_items[0]["arguments"] == '{"city":"NYC"}'


def test_v2_response_metadata_passthrough():
    """v2 响应应回填 metadata。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-4-flash",
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    request_payload = {"metadata": {"user_id": "u123", "session": "s456"}}
    result = openai_to_responses_v2(openai_result, "glm-4-flash", request_payload=request_payload)
    assert result["metadata"] == {"user_id": "u123", "session": "s456"}


def test_v2_response_all_fields_passthrough():
    """v2 响应应回填所有 v2 字段。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-4-flash",
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    request_payload = {
        "model": "glm-4-flash",
        "input": "hi",
        "max_output_tokens": 100,
        "temperature": 0.7,
        "top_p": 0.9,
        "parallel_tool_calls": False,
        "background": True,
        "service_tier": "default",
        "truncation": "auto",
        "text": {"format": {"type": "text"}},
        "reasoning": {"effort": "medium"},
        "prompt_cache_key": "key123",
        "safety_identifier": "user456",
        "store": True,
        "tool_choice": "auto",
        "tools": [],
        "instructions": "You are helpful",
    }
    result = openai_to_responses_v2(openai_result, "glm-4-flash", request_payload=request_payload)
    assert result["max_output_tokens"] == 100
    assert result["temperature"] == 0.7
    assert result["top_p"] == 0.9
    assert result["parallel_tool_calls"] is False
    assert result["background"] is True
    assert result["service_tier"] == "default"
    assert result["truncation"] == "auto"
    assert result["text"] == {"format": {"type": "text"}}
    assert result["reasoning"] == {"effort": "medium"}
    assert result["prompt_cache_key"] == "key123"
    assert result["safety_identifier"] == "user456"
    assert result["store"] is True
    assert result["instructions"] == "You are helpful"
    assert result["tool_choice"] == "auto"


def test_v2_response_incomplete():
    """v2 响应 finish_reason=length 应为 incomplete。"""
    openai_result = {
        "id": "chatcmpl-test",
        "model": "glm-4-flash",
        "choices": [{"message": {"content": "partial..."}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 100},
    }
    result = openai_to_responses_v2(openai_result, "glm-4-flash")
    assert result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": "max_output_tokens"}


def test_v2_backward_compat_v1_request():
    """v1 请求格式应被 v2 正确处理（向后兼容）。"""
    v1_payload = {
        "model": "glm-4-flash",
        "input": "Hello",
        "max_output_tokens": 50,
    }
    result = responses_v2_to_openai(v1_payload)
    assert result["model"] == "glm-4-flash"
    assert result["messages"] == [{"role": "user", "content": "Hello"}]
    assert result["max_tokens"] == 50


# === 流式累加器测试 ===

def test_v2_stream_accumulator_basic():
    """v2 流式累加器应正确处理基本流。"""
    acc = ResponsesV2StreamAccumulator(model="glm-4-flash")

    # 第一个 chunk（role）
    events = acc.consume_chunk({
        "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}],
    })
    # 应该有 response.created 和 response.in_progress
    event_types = [e[0] for e in events]
    assert "response.created" in event_types
    assert "response.in_progress" in event_types

    # 文本 delta
    events = acc.consume_chunk({
        "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}],
    })
    event_types = [e[0] for e in events]
    assert "response.output_item.added" in event_types
    assert "response.content_part.added" in event_types
    assert "response.output_text.delta" in event_types

    # finish
    events = acc.consume_chunk({
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    })

    # finalize
    events = acc.finalize()
    event_types = [e[0] for e in events]
    assert "response.output_text.done" in event_types
    assert "response.content_part.done" in event_types
    assert "response.output_item.done" in event_types
    assert "response.completed" in event_types


def test_v2_stream_accumulator_with_reasoning():
    """v2 流式累加器应处理 reasoning_content。"""
    acc = ResponsesV2StreamAccumulator(model="glm-5.2-think")

    # reasoning delta
    events = acc.consume_chunk({
        "choices": [{"delta": {"reasoning_content": "Thinking..."}, "finish_reason": None}],
    })
    event_types = [e[0] for e in events]
    assert "response.output_item.added" in event_types
    assert "response.reasoning_summary_text.delta" in event_types

    # text delta
    events = acc.consume_chunk({
        "choices": [{"delta": {"content": "Answer"}, "finish_reason": None}],
    })
    assert any(e[0] == "response.output_text.delta" for e in events)

    # finalize
    events = acc.finalize()
    event_types = [e[0] for e in events]
    assert "response.reasoning_summary_text.done" in event_types
    assert "response.completed" in event_types


def test_v2_stream_accumulator_with_function_call():
    """v2 流式累加器应处理 function_call。"""
    acc = ResponsesV2StreamAccumulator(model="glm-4-flash")

    # function call delta
    events = acc.consume_chunk({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_abc",
            "type": "function",
            "function": {"name": "get_weather", "arguments": "{\"city\":"},
        }]}, "finish_reason": None}],
    })
    event_types = [e[0] for e in events]
    assert "response.output_item.added" in event_types
    assert "response.function_call_arguments.delta" in event_types

    # arguments 继续
    events = acc.consume_chunk({
        "choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "function": {"arguments": "\"NYC\"}"},
        }]}, "finish_reason": None}],
    })
    assert any(e[0] == "response.function_call_arguments.delta" for e in events)

    # finalize
    events = acc.finalize()
    event_types = [e[0] for e in events]
    assert "response.function_call_arguments.done" in event_types
    assert "response.output_item.done" in event_types
    assert "response.completed" in event_types
