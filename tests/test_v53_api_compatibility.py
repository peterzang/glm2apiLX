"""v53 企业级审核修复：API 兼容性差异回归测试。

修复的问题:
- P0: stop_sequence 返回 None → 返回匹配的字符串（与官方 API 一致）
- P1: usage 缺少 server_tool_use → 加默认值
- P1: usage 缺少 service_tier → 加默认值 'standard'
- P2: 缺少 request-id 响应头 → 加上
- P2: 缺少 anthropic-ratelimit 响应头 → 加上
"""
import json
import threading
import time
import urllib.request
import urllib.error
from types import SimpleNamespace

import pytest

from glm2api.server import GLM2APIServer
from glm2api.services.anthropic_adapter import (
    openai_to_anthropic_response,
    AnthropicStreamAccumulator,
)


class _FakeGLM:
    def chat_completion(self, payload):
        # 模拟 stop_sequence 触发的响应
        content = "Hello STOP world"
        if payload.get("stop"):
            stops = payload["stop"]
            if isinstance(stops, list):
                for s in stops:
                    if s in content:
                        content = content.split(s)[0]
                        return {
                            "id": "chatcmpl-test",
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": payload.get("model", "glm-5.2-flash"),
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": "Hello STOP world"},
                                "finish_reason": "stop",
                            }],
                            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                        }, "conv_test"
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "glm-5.2-flash"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }, "conv_test"

    def stream_chat_completion(self, payload):
        return iter([])


class _FakeLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _make_server():
    config = SimpleNamespace(
        host="127.0.0.1", port=0, api_prefix="/v1",
        cors_allow_origin="*", server_api_keys=["sk-test-key"],
        debug_dump_all=False, exposed_models=["glm-5.2-flash", "glm-4.6", "glm-4"],
    )
    server = GLM2APIServer(config, _FakeGLM(), _FakeLogger())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    port = server._server.server_address[1]
    return server, thread, port


def _post(port, path, body, api_key="sk-test-key"):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body_dict = json.loads(resp.read().decode())
            return resp.status, body_dict, dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode()), dict(e.headers)
        except Exception:
            return e.code, {}, dict(e.headers)


@pytest.fixture
def server():
    s, t, p = _make_server()
    yield p
    s.shutdown()
    t.join(timeout=1)


# === P0: stop_sequence 返回匹配字符串（非流式）===

def test_stop_sequence_non_stream_returns_matched_string():
    """非流式：stop_sequences 触发时应返回匹配的字符串，而非 None。"""
    # 模拟 OpenAI 响应（包含 stop_sequence）
    openai_result = {
        "choices": [{
            "message": {"role": "assistant", "content": "Hello STOP world"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash", stop_sequences=["STOP"])
    assert result["stop_reason"] == "stop_sequence"
    assert result["stop_sequence"] == "STOP", f"应返回 'STOP'，实际 {result['stop_sequence']!r}"


def test_stop_sequence_non_stream_no_match_returns_none():
    """非流式：未匹配 stop_sequence 时应返回 None（单 stop_sequence 假设触发的场景除外）。

    注意：单个 stop_sequence + finish_reason=stop 时，glm2api 会假设是 stop_sequence 触发
    （因为 GLM 上游已截断文本，无法从文本判断）。这个测试用多个 stop_sequences 验证
    "无法确定"的场景。
    """
    # 多个 stop_sequences，文本里都没有，无法确定 → None
    openai_result = {
        "choices": [{
            "message": {"role": "assistant", "content": "Hello world"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash", stop_sequences=["STOP", "END"])
    # 多个 stop_sequence 且文本里都没有，无法确定 → 保持 end_turn，stop_sequence=None
    assert result["stop_sequence"] is None or result["stop_sequence"] == ""


def test_stop_sequence_non_stream_multiple_matches():
    """非流式：多个 stop_sequences 时应返回第一个匹配的。"""
    openai_result = {
        "choices": [{
            "message": {"role": "assistant", "content": "Hello END world STOP test"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash", stop_sequences=["STOP", "END"])
    # 应返回先出现的 "END"
    assert result["stop_sequence"] == "END", f"应返回 'END'，实际 {result['stop_sequence']!r}"


# === P0: stop_sequence 返回匹配字符串（流式）===

def test_stop_sequence_stream_accumulator_matches():
    """流式：accumulator 应检测 stop_sequence 并返回匹配字符串。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2-flash", stop_sequences=["STOP"])
    # feed_chunk 接收 SSE bytes 格式
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello "},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"STOP"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    for chunk in chunks:
        acc.feed_chunk(chunk)
    # 应检测到 STOP
    assert acc.matched_stop_sequence == "STOP", f"应匹配 STOP，实际 {acc.matched_stop_sequence!r}"
    assert acc.stop_reason == "stop_sequence"


def test_stop_sequence_stream_no_match_returns_none():
    """流式：未匹配 stop_sequence 时 stop_sequence 应返回 None。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2-flash", stop_sequences=["STOP"])
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello world"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    for chunk in chunks:
        acc.feed_chunk(chunk)
    assert acc.matched_stop_sequence is None


def test_stop_sequence_stream_no_stop_sequences_param():
    """流式：未传 stop_sequences 时行为应与之前一致（None）。"""
    acc = AnthropicStreamAccumulator(model="glm-5.2-flash")
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello STOP world"},"finish_reason":null}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
    for chunk in chunks:
        acc.feed_chunk(chunk)
    # 没有传 stop_sequences，不应检测
    assert acc.matched_stop_sequence is None


# === P1: usage 字段完整性 ===

def test_usage_has_server_tool_use():
    """usage 应包含 server_tool_use 字段。"""
    openai_result = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash")
    assert "server_tool_use" in result["usage"]
    assert isinstance(result["usage"]["server_tool_use"], dict)
    assert "web_search_requests" in result["usage"]["server_tool_use"]
    assert "web_fetch_requests" in result["usage"]["server_tool_use"]


def test_usage_has_service_tier():
    """usage 应包含 service_tier 字段。"""
    openai_result = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash")
    assert "service_tier" in result["usage"]
    assert result["usage"]["service_tier"] == "standard"


def test_usage_all_6_fields():
    """usage 应有 6 个字段（与官方 API 一致）。"""
    openai_result = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    result = openai_to_anthropic_response(openai_result, "glm-5.2-flash")
    expected_fields = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "server_tool_use",
        "service_tier",
    }
    assert set(result["usage"].keys()) == expected_fields, f"usage 字段不匹配: {set(result['usage'].keys())}"


# === P2: 响应头 ===

def test_response_has_request_id_header(server):
    """响应应包含 request-id header（与 X-Request-ID 一致）。"""
    status, body, headers = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    # request-id header（小写，HTTP header 不区分大小写）
    headers_lower = {k.lower(): v for k, v in headers.items()}
    assert "request-id" in headers_lower, f"应包含 request-id header，实际 headers: {list(headers_lower.keys())}"
    assert "x-request-id" in headers_lower, "应包含 X-Request-ID header"
    # 两个应该一致
    assert headers_lower["request-id"] == headers_lower["x-request-id"]


def test_response_has_anthropic_ratelimit_headers(server):
    """/v1/messages 响应应包含 anthropic-ratelimit-* header。"""
    status, body, headers = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    headers_lower = {k.lower(): v for k, v in headers.items()}
    assert "anthropic-ratelimit-requests-limit" in headers_lower
    assert "anthropic-ratelimit-requests-remaining" in headers_lower
    assert "anthropic-ratelimit-tokens-limit" in headers_lower
    assert "anthropic-ratelimit-tokens-remaining" in headers_lower


def test_chat_completions_no_anthropic_ratelimit_headers(server):
    """/v1/chat/completions 不应有 anthropic-ratelimit header（只有 Anthropic 端点有）。"""
    status, body, headers = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    headers_lower = {k.lower(): v for k, v in headers.items()}
    # chat/completions 不应有 anthropic-ratelimit header
    assert "anthropic-ratelimit-requests-limit" not in headers_lower


# === E2E: 完整请求验证 ===

def test_e2e_anthropic_response_format_complete(server):
    """E2E: Anthropic 响应格式完整（所有字段 + header）。"""
    status, body, headers = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    # 检查响应字段
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert "content" in body
    assert "model" in body
    assert "stop_reason" in body
    assert "stop_sequence" in body
    assert "usage" in body
    # usage 6 字段
    usage = body["usage"]
    assert "input_tokens" in usage
    assert "output_tokens" in usage
    assert "cache_creation_input_tokens" in usage
    assert "cache_read_input_tokens" in usage
    assert "server_tool_use" in usage
    assert "service_tier" in usage
    # header
    headers_lower = {k.lower(): v for k, v in headers.items()}
    assert "request-id" in headers_lower
    assert "anthropic-ratelimit-requests-limit" in headers_lower


# === 正常请求不应被误杀 ===

def test_normal_request_still_works(server):
    """正常请求应正常工作。"""
    status, body, headers = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert body["type"] == "message"
