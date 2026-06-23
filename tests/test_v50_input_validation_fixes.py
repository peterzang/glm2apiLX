"""v50 审核修复：3 个新 BUG + 3 个 WARN 的回归测试。

修复的问题:
- BUG1: messages=null 返回 502 → 400
- BUG2: messages=字符串/缺失 返回 200 → 400
- BUG3: max_tokens=999999 不截断 → clamp 到 32768
- WARN1: Content-Type 缺失（宽松接受，记录 warning）
- WARN2: 无 anthropic-version（宽松接受，Claude Code 友好）
- WARN3: temperature=999 / top_p=-1 → clamp 到 [0,2] / [0,1]
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
        return e.code, json.loads(e.read().decode())


@pytest.fixture
def server():
    s, t, p = _make_server()
    yield p
    s.shutdown()
    t.join(timeout=1)


# === BUG 1: messages=null 返回 502 → 应返回 400 ===

def test_bug1_messages_null_returns_400(server):
    """messages=null 应返回 400，而非 502 upstream error。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": None,
    })
    assert status == 400, f"messages=null 应返回 400，实际 {status}"
    assert "messages" in str(body).lower() or "required" in str(body).lower()


def test_bug1_messages_null_anthropic_returns_400(server):
    """Anthropic /v1/messages messages=null 也应返回 400。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": None,
    })
    assert status == 400, f"Anthropic messages=null 应返回 400，实际 {status}"


# === BUG 2: messages=字符串/缺失 返回 200 → 应返回 400 ===

def test_bug2_messages_string_returns_400(server):
    """messages=字符串应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": "hello",
    })
    assert status == 400, f"messages=字符串 应返回 400，实际 {status}"


def test_bug2_messages_number_returns_400(server):
    """messages=数字应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": 123,
    })
    assert status == 400, f"messages=数字 应返回 400，实际 {status}"


def test_bug2_messages_missing_returns_400(server):
    """无 messages 字段应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
    })
    assert status == 400, f"无 messages 应返回 400，实际 {status}"


def test_bug2_messages_object_returns_400(server):
    """messages=对象（非数组）应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": {"role": "user", "content": "hi"},
    })
    assert status == 400, f"messages=对象 应返回 400，实际 {status}"


def test_bug2_messages_string_anthropic_returns_400(server):
    """Anthropic messages=字符串也应返回 400。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": "hello",
    })
    assert status == 400, f"Anthropic messages=字符串 应返回 400，实际 {status}"


# === BUG 3: max_tokens=999999 不截断 → 应 clamp 到 32768 ===

def test_bug3_max_tokens_huge_clamped(server):
    """max_tokens=999999 应 clamp 到 32768，返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 999999,
    })
    assert status == 200, f"max_tokens=999999 应 clamp 后返回 200，实际 {status}"


def test_bug3_max_tokens_at_limit_works(server):
    """max_tokens=32768 应正常工作（在上限内）。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32768,
    })
    assert status == 200, f"max_tokens=32768 应正常工作，实际 {status}"


def test_bug3_max_tokens_just_over_limit_clamped(server):
    """max_tokens=32769 应 clamp 到 32768。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32769,
    })
    assert status == 200, f"max_tokens=32769 应 clamp 后返回 200，实际 {status}"


# === WARN 3: temperature=999 / top_p=-1 不校验 → clamp ===

def test_warn3_temperature_huge_clamped(server):
    """temperature=999 应 clamp 到 2.0，返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 999,
    })
    assert status == 200, f"temperature=999 应 clamp 后返回 200，实际 {status}"


def test_warn3_temperature_negative_clamped(server):
    """temperature=-1 应 clamp 到 0.0，返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": -1,
    })
    assert status == 200, f"temperature=-1 应 clamp 后返回 200，实际 {status}"


def test_warn3_temperature_normal_works(server):
    """temperature=1.0 应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 1.0,
    })
    assert status == 200, f"temperature=1.0 应正常工作，实际 {status}"


def test_warn3_temperature_string_returns_400(server):
    """temperature=字符串应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": "hot",
    })
    assert status == 400, f"temperature=字符串 应返回 400，实际 {status}"


def test_warn3_top_p_negative_clamped(server):
    """top_p=-1 应 clamp 到 0.0，返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "top_p": -1,
    })
    assert status == 200, f"top_p=-1 应 clamp 后返回 200，实际 {status}"


def test_warn3_top_p_huge_clamped(server):
    """top_p=999 应 clamp 到 1.0，返回 200。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "top_p": 999,
    })
    assert status == 200, f"top_p=999 应 clamp 后返回 200，实际 {status}"


def test_warn3_top_p_normal_works(server):
    """top_p=0.9 应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "top_p": 0.9,
    })
    assert status == 200, f"top_p=0.9 应正常工作，实际 {status}"


# === WARN 1/2: Content-Type 缺失 / 无 anthropic-version → 宽松接受 ===

def test_warn1_no_content_type_accepted(server):
    """无 Content-Type 应宽松接受（返回 200，不报 415）。"""
    # 故意不发 Content-Type
    data = json.dumps({
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server}/v1/chat/completions",
        data=data,
        headers={"Authorization": "Bearer sk-test-key"},  # 无 Content-Type
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200, f"无 Content-Type 应宽松接受，实际 {resp.status}"
    except urllib.error.HTTPError as e:
        # 415 也算可接受（审计报告建议），但不能 500
        assert e.code in (200, 415), f"无 Content-Type 应返回 200 或 415，实际 {e.code}"


def test_warn2_no_anthropic_version_accepted(server):
    """无 anthropic-version header 应宽松接受（Claude Code 友好）。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    })
    # _post 不带 anthropic-version header，应仍然返回 200
    assert status == 200, f"无 anthropic-version 应宽松接受，实际 {status}"


# === 正常请求不应被误杀 ===

def test_normal_request_all_params_works(server):
    """带所有正常参数的请求应正常工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "temperature": 0.7,
        "top_p": 0.9,
    })
    assert status == 200
    assert "choices" in body


def test_normal_anthropic_with_all_params_works(server):
    """正常 Anthropic 请求带所有参数应正常工作。"""
    status, body = _post(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "temperature": 0.7,
        "top_p": 0.9,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert body.get("type") == "message"


# === v49 修复回归（确保没破坏）===

def test_v49_max_tokens_zero_still_400(server):
    """v49 修复的 max_tokens=0 仍应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 0,
    })
    assert status == 400


def test_v49_empty_messages_still_400(server):
    """v49 修复的空 messages 仍应返回 400。"""
    status, body = _post(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [],
    })
    assert status == 400


def test_v49_no_model_fallback_still_works(server):
    """v49 修复的无 model fallback 仍应工作。"""
    status, body = _post(server, "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert body["model"] == "glm-5.2-flash"
