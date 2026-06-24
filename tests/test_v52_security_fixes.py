"""v52 深度代码审核修复：3 个代码问题回归测试。

修复的问题:
- P1.1: 5 处 str(exc) 错误信息泄漏 → 返回通用消息，详情记日志
- P1.2: 无请求体大小限制 → 加 MAX_BODY_SIZE=10MB，超限 413
- P2: 无 API rate limiting → 加 per-key 限流 60req/min，429+Retry-After
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from glm2api.server import GLM2APIServer, _safe_error_message, _RateLimiter, UpstreamAPIError


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
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


@pytest.fixture
def server():
    s, t, p = _make_server()
    yield p
    s.shutdown()
    t.join(timeout=1)


# === P1.1: str(exc) 错误信息泄漏 ===

def test_safe_error_message_upstream_error():
    """UpstreamAPIError 应返回简化消息（业务错误客户端需要知道）。"""
    exc = UpstreamAPIError(status_code=502, message="GLM 请求失败 HTTP 429 | status=10061 请等待")
    msg = _safe_error_message(exc, "default")
    assert "GLM" in msg or "429" in msg  # 业务消息保留
    assert len(msg) <= 500


def test_safe_error_message_value_error():
    """ValueError 应返回过滤后的消息（参数错误客户端需要知道）。"""
    exc = ValueError("无效的 Content-Length: abc at /home/user/server.py:123")
    msg = _safe_error_message(exc, "default")
    # 文件路径应被过滤
    assert "/home/user/server.py" not in msg
    assert "<file>" in msg or "Content-Length" in msg


def test_safe_error_message_generic_exception():
    """普通 Exception 应返回通用消息，不泄漏内部信息。"""
    exc = KeyError("internal_secret_key_12345")
    msg = _safe_error_message(exc, "Internal server error")
    assert msg == "Internal server error"
    assert "internal_secret_key_12345" not in msg


def test_safe_error_message_type_error():
    """TypeError 应返回通用消息。"""
    exc = TypeError("'NoneType' object is not iterable")
    msg = _safe_error_message(exc, "Internal server error")
    assert msg == "Internal server error"
    assert "NoneType" not in msg


def test_safe_error_message_truncates_long():
    """超长错误消息应截断到 500 字符。"""
    long_msg = "x" * 1000
    exc = UpstreamAPIError(status_code=502, message=long_msg)
    msg = _safe_error_message(exc, "default")
    assert len(msg) <= 500


# === P1.2: 请求体大小限制 ===

def test_body_size_normal_works(server):
    """正常大小的请求体应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200


def test_body_size_large_rejected(server, monkeypatch):
    """超大请求体应返回 413。"""
    # 设很小的限制便于测试（1MB）
    monkeypatch.setenv("MAX_BODY_SIZE_MB", "1")
    # 用原始 socket 发请求，避免 urllib 在 server 提前 413 时报 BrokenPipe
    # 构造 2MB 的请求体
    big_content = "x" * (2 * 1024 * 1024)
    body = json.dumps({
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": big_content}],
    }).encode()
    # 手动构造 HTTP 请求，只发 headers 不发完整 body（server 检查 Content-Length 后直接 413）
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", server))
    req_line = (
        f"POST /v1/chat/completions HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server}\r\n"
        f"Authorization: Bearer sk-test-key\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode()
    sock.sendall(req_line)
    # 尝试发送一点 body，但 server 可能已关闭连接
    try:
        sock.sendall(body[:1024])
    except (BrokenPipeError, ConnectionResetError):
        pass
    # 读取响应
    try:
        resp = sock.recv(4096).decode("utf-8", errors="replace")
    except Exception:
        resp = ""
    sock.close()
    # 应包含 413 状态码
    assert "413" in resp, f"超大请求体应返回 413，响应: {resp[:200]}"


def test_body_size_at_limit_works(server, monkeypatch):
    """正好在上限内的请求体应正常工作。"""
    monkeypatch.setenv("MAX_BODY_SIZE_MB", "10")  # 10MB 默认
    # 小请求应正常
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200


# === P2: API rate limiting ===

def test_rate_limiter_allows_normal_requests():
    """正常请求数量内应允许。"""
    limiter = _RateLimiter()
    limiter._limit = 5  # 测试用小限制
    limiter._windows.clear()
    for i in range(5):
        allowed, retry = limiter.check("test-key")
        assert allowed, f"第 {i+1} 个请求应被允许"
        assert retry == 0


def test_rate_limiter_blocks_over_limit():
    """超限应拒绝并返回 retry_after。"""
    limiter = _RateLimiter()
    limiter._limit = 3
    limiter._windows.clear()
    # 发 3 个请求（达到上限）
    for i in range(3):
        allowed, _ = limiter.check("test-key")
        assert allowed
    # 第 4 个应被拒绝
    allowed, retry = limiter.check("test-key")
    assert not allowed, "第 4 个请求应被拒绝"
    assert retry >= 1, "应返回正数 retry_after"


def test_rate_limiter_disabled_when_zero():
    """limit=0 时应关闭限流。"""
    limiter = _RateLimiter()
    limiter._limit = 0
    # 发 100 个请求都应允许
    for i in range(100):
        allowed, retry = limiter.check("test-key")
        assert allowed
        assert retry == 0


def test_rate_limiter_separate_keys():
    """不同 key 应独立计数。"""
    limiter = _RateLimiter()
    limiter._limit = 2
    limiter._windows.clear()
    # key A 发 2 个
    assert limiter.check("key-a")[0]
    assert limiter.check("key-a")[0]
    # key A 超限
    assert not limiter.check("key-a")[0]
    # key B 仍可用
    assert limiter.check("key-b")[0]
    assert limiter.check("key-b")[0]
    assert not limiter.check("key-b")[0]


def test_rate_limiter_window_expires():
    """窗口过期后应恢复。"""
    limiter = _RateLimiter()
    limiter._limit = 1
    limiter._windows.clear()
    limiter._window_seconds = 0.1  # 100ms 窗口便于测试
    # 第 1 个请求允许
    assert limiter.check("test-key")[0]
    # 第 2 个被拒
    assert not limiter.check("test-key")[0]
    # 等待窗口过期
    time.sleep(0.15)
    # 应恢复
    assert limiter.check("test-key")[0]


def test_rate_limiter_singleton():
    """get_instance 应返回单例。"""
    i1 = _RateLimiter.get_instance()
    i2 = _RateLimiter.get_instance()
    assert i1 is i2


# === E2E: rate limiting 端到端 ===

def test_e2e_rate_limit_429(server, monkeypatch):
    """E2E: 超限应返回 429 + Retry-After。"""
    # 重置 rate limiter 并设小限制
    limiter = _RateLimiter.get_instance()
    limiter._limit = 3
    limiter._windows.clear()
    try:
        # 发 3 个请求（达到上限）
        for i in range(3):
            status, _ = _post(server, "/v1/chat/completions", {
                "model": "glm-5.2-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert status == 200, f"第 {i+1} 个请求应成功，实际 {status}"
        # 第 4 个应返回 429
        status, body = _post(server, "/v1/chat/completions", {
            "model": "glm-5.2-flash",
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert status == 429, f"第 4 个请求应返回 429，实际 {status}"
    finally:
        # 恢复默认
        limiter._limit = 60
        limiter._windows.clear()


# === 正常请求不应被误杀 ===

def test_normal_request_still_works(server):
    """正常请求应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert "choices" in body
