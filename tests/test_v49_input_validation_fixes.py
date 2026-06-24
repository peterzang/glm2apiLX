"""v49 紧急审核修复：5 个输入校验 BUG 的回归测试。

修复的 BUG:
1. 错误 key HEAD /v1/models 返回 501 → 修复为 401
2. 不存在模型返回 200 → 修复为 fallback 到 glm-5.2-flash（宽松）/ 404（严格）
3. 空 messages 数组返回 200 → 修复为 400
4. max_tokens=0/-1 返回 200 → 修复为 400
5. 无 model fallback 到 glm-4 → 修复为 glm-5.2-flash
"""
import json
import threading
import time
import urllib.request
import urllib.error
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from glm2api.server import GLM2APIServer


class _FakeGLM:
    """最小化的假 GLM 客户端，用于触发 server 路由但不真正调上游。"""
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
        # 不实际使用，但需要存在
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


def _post(port, path, body, api_key="sk-test-key", method="POST"):
    """发请求，返回 (status, body_dict)。"""
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _head(port, path, api_key="sk-test-key"):
    """发 HEAD 请求，返回 status code。"""
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    conn.request("HEAD", path, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    resp.read()
    conn.close()
    return status


@pytest.fixture
def server():
    s, t, p = _make_server()
    yield p
    s.shutdown()
    t.join(timeout=1)


# === BUG 1: 错误 key HEAD /v1/models 返回 501 → 应返回 401 ===

def test_bug1_invalid_key_head_models_returns_401(server):
    """错误 key 的 HEAD /v1/models 应返回 401，而非 501。"""
    status = _head(server, "/v1/models", api_key="sk-invalid-key")
    assert status == 401, f"错误 key HEAD 应返回 401，实际 {status}"


def test_bug1_valid_key_head_models_returns_200(server):
    """正确 key 的 HEAD /v1/models 应返回 200。"""
    status = _head(server, "/v1/models", api_key="sk-test-key")
    assert status == 200, f"正确 key HEAD 应返回 200，实际 {status}"


# === BUG 2: 不存在模型返回 200 → 应 fallback 或 404 ===

def test_bug2_unknown_model_fallback_to_flash(server):
    """不存在的模型应 fallback 到 glm-5.2-flash（宽松模式默认）。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "gpt-999",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"宽松模式应 fallback 返回 200，实际 {status}"
    assert body["model"] == "glm-5.2-flash", f"应 fallback 到 glm-5.2-flash，实际 {body['model']}"


def test_bug2_unknown_model_strict_mode_returns_404(server, monkeypatch):
    """STRICT_MODEL_VALIDATION=true 时不存在的模型应返回 404。"""
    monkeypatch.setenv("STRICT_MODEL_VALIDATION", "true")
    status, body = _post(server, "/v1/chat/completions", {
        "model": "gpt-999",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 404, f"严格模式应返回 404，实际 {status}"


# === BUG 3: 空 messages 数组返回 200 → 应返回 400 ===

def test_bug3_empty_messages_returns_400(server):
    """空 messages 数组应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [],
    })
    assert status == 400, f"空 messages 应返回 400，实际 {status}"


def test_bug3_empty_messages_anthropic_returns_400(server):
    """Anthropic /v1/messages 空 messages 也应返回 400。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [],
    })
    assert status == 400, f"Anthropic 空 messages 应返回 400，实际 {status}"


# === BUG 4: max_tokens=0/-1 返回 200 → 应返回 400 ===

def test_bug4_max_tokens_zero_returns_400(server):
    """max_tokens=0 应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 0,
    })
    assert status == 400, f"max_tokens=0 应返回 400，实际 {status}"


def test_bug4_max_tokens_negative_returns_400(server):
    """max_tokens=-1 应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": -1,
    })
    assert status == 400, f"max_tokens=-1 应返回 400，实际 {status}"


def test_bug4_max_tokens_zero_anthropic_returns_400(server):
    """Anthropic max_tokens=0 也应返回 400。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 0,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 400, f"Anthropic max_tokens=0 应返回 400，实际 {status}"


# === WARN: 无 model fallback 到 glm-4 → 应改为 glm-5.2-flash ===

def test_warn_no_model_fallback_to_flash(server):
    """无 model 字段应 fallback 到 glm-5.2-flash，而非 glm-4。"""
    status, body = _post(server, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert body["model"] == "glm-5.2-flash", f"应 fallback 到 glm-5.2-flash，实际 {body['model']}"


def test_warn_no_model_anthropic_fallback_to_flash(server):
    """Anthropic 无 model 也应 fallback 到 glm-5.2-flash。"""
    status, body = _post(server, "/v1/messages", {
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    # Anthropic 响应的 model 字段
    assert body.get("model") == "glm-5.2-flash", f"应 fallback 到 glm-5.2-flash，实际 {body.get('model')}"


# === 正常请求不应被误杀 ===

def test_normal_request_still_works(server):
    """正常请求应正常返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    })
    assert status == 200
    assert "choices" in body


def test_normal_anthropic_request_still_works(server):
    """正常 Anthropic 请求应正常返回 200。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert body.get("type") == "message"


def test_normal_max_tokens_positive_works(server):
    """正数 max_tokens 应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    })
    assert status == 200, f"max_tokens=1 应正常工作，实际 {status}"
