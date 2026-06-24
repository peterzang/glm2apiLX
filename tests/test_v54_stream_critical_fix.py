"""v54 CRITICAL 修复：流式全 502 回归测试。

v53 修复 stop_sequence 时在 _stream_anthropic 引入了未定义的 payload 变量，
导致所有流式请求 502。这个测试确保 _stream_anthropic 正确接收 payload 参数。
"""
import json
import threading
import time
import urllib.request
import urllib.error
from types import SimpleNamespace

import pytest

from glm2api.server import GLM2APIServer


class _FakeGLM:
    def chat_completion(self, payload):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "glm-5.2-flash"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }, "conv_test"

    def stream_chat_completion(self, payload):
        # 模拟流式 chunks
        yield b'data: {"choices":[{"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'


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


@pytest.fixture
def server():
    s, t, p = _make_server()
    yield p
    s.shutdown()
    t.join(timeout=1)


def _post_stream(port, path, body, api_key="sk-test-key"):
    """发流式请求，返回 (status, events_list)。"""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            events = []
            for line in resp:
                line = line.decode().strip()
                if line:
                    events.append(line)
            return resp.status, events
    except urllib.error.HTTPError as e:
        return e.code, []


# === CRITICAL: 流式请求不应返回 502 ===

def test_stream_anthropic_not_502(server):
    """Anthropic 流式请求应返回 200 + SSE 事件，而非 502。"""
    status, events = _post_stream(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"流式请求应返回 200，实际 {status}（v54 BUG: payload 未定义导致 502）"
    assert len(events) > 0, "应有 SSE 事件"
    # 应包含 message_start 和 message_stop
    event_types = [e for e in events if e.startswith("event:")]
    assert any("message_start" in e for e in event_types), "应有 message_start 事件"
    assert any("message_stop" in e for e in event_types), "应有 message_stop 事件"


def test_stream_anthropic_with_stop_sequences_not_502(server):
    """带 stop_sequences 的 Anthropic 流式请求应返回 200，而非 502。"""
    status, events = _post_stream(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": "说 hello"}],
        "stop_sequences": ["STOP"],
    })
    assert status == 200, f"带 stop_sequences 的流式请求应返回 200，实际 {status}"
    assert len(events) > 0


def test_stream_openai_chat_not_502(server):
    """OpenAI Chat 流式请求应返回 200 + SSE 事件。"""
    status, events = _post_stream(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 30,
    })
    assert status == 200, f"OpenAI 流式请求应返回 200，实际 {status}"
    assert len(events) > 0
    # 应包含 [DONE]
    assert any("[DONE]" in e for e in events), "应有 [DONE] 结束标记"


def test_stream_anthropic_complete_sse_sequence(server):
    """Anthropic 流式应有完整 SSE 事件序列。"""
    status, events = _post_stream(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    # 检查关键事件
    all_text = "\n".join(events)
    assert "message_start" in all_text, "应有 message_start"
    assert "content_block_start" in all_text, "应有 content_block_start"
    assert "content_block_delta" in all_text, "应有 content_block_delta"
    assert "message_stop" in all_text, "应有 message_stop"
