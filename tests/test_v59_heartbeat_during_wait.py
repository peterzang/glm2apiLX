"""v59 验证：等账号期间 SSE 心跳回归测试。

v59 报告质疑等账号期间是否有 SSE 心跳。本测试验证 v54 的 _start_stream_background
确实在等账号期间每 2s 发 ping 心跳。

核心逻辑：
- _start_stream_background 在后台线程调 stream_chat_completion（含等账号）
- 主线程 chunk_queue.get(timeout=2s) 超时 → 发 ping
- 等账号期间持续有心跳
"""
import json
import os
import queue
import threading
import time

import pytest

from glm2api.server import GLM2APIServer


class _SlowAccountGLM:
    """模拟等账号 6s 的 GLM 客户端。"""
    def __init__(self):
        self.account_wait = 6.0  # 等账号 6s

    def chat_completion(self, payload):
        time.sleep(self.account_wait)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model", "glm-5.2-flash"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }, "conv_test"

    def stream_chat_completion(self, payload):
        # 模拟等账号 6s（request_queue.acquire 在 stream_chat_completion 内部）
        time.sleep(self.account_wait)
        # 然后返回 chunks
        yield b'data: {"choices":[{"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'


class _FakeLogger:
    def debug(self, *a, **k): pass
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _make_server():
    from types import SimpleNamespace
    config = SimpleNamespace(
        host="127.0.0.1", port=0, api_prefix="/v1",
        cors_allow_origin="*", server_api_keys=["sk-test-key"],
        debug_dump_all=False, exposed_models=["glm-5.2-flash", "glm-4.6", "glm-4"],
    )
    server = GLM2APIServer(config, _SlowAccountGLM(), _FakeLogger())
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
    t.join(timeout=5)


def test_heartbeat_during_account_wait(server):
    """等账号期间应发 ping 心跳（v54 _start_stream_background 验证）。

    _SlowAccountGLM 会等 6s 才返回 chunks。
    这 6s 期间应每 2s 发一个 ping（约 2-3 个 ping）。
    """
    import urllib.request

    port = server
    data = json.dumps({
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-test-key",
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=data,
        headers=headers,
        method="POST",
    )

    start = time.monotonic()
    ping_count = 0
    message_start_time = None
    first_content_time = None

    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.status == 200
        for line in resp:
            line = line.decode().strip()
            if not line:
                continue
            elapsed = time.monotonic() - start

            if "message_start" in line:
                message_start_time = elapsed
            elif "ping" in line and "event:" in line:
                ping_count += 1
            elif "content_block_delta" in line and first_content_time is None:
                first_content_time = elapsed

    # message_start 应立即发送（< 1s）
    assert message_start_time is not None, "应有 message_start"
    assert message_start_time < 1.0, f"message_start 应立即发送，实际 {message_start_time:.1f}s"

    # 等账号 6s 期间应有 ping 心跳（至少 2 个）
    assert ping_count >= 2, f"等账号期间应至少 2 个 ping，实际 {ping_count}"

    # 第一个 content 应在等账号后（> 5s）
    if first_content_time:
        assert first_content_time > 5.0, f"等账号 6s 后才有 content，实际 {first_content_time:.1f}s"


def test_message_start_immediate(server):
    """message_start 应在请求到达后立即发送（不等账号）。"""
    import urllib.request

    port = server
    data = json.dumps({
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-test-key",
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=data,
        headers=headers,
        method="POST",
    )

    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        # 读第一行（应是 message_start）
        first_line = resp.readline().decode().strip()
        elapsed = time.monotonic() - start

    assert "message_start" in first_line, f"第一行应是 message_start，实际 {first_line[:50]}"
    assert elapsed < 1.0, f"message_start 应在 1s 内发送，实际 {elapsed:.1f}s"
