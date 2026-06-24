"""v51 审核：STRICT_VALIDATION 开关回归测试。

v51 报告建议加 STRICT_VALIDATION=true 开关供严格合规场景使用。
默认宽松模式（Claude Code 友好），启用后严格校验：
- Content-Type 必须是 application/json，否则 415
- /v1/messages 必须带 anthropic-version header，否则 400
"""
import json
import os
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


def _post_raw(port, path, body, headers=None):
    """发请求，返回 (status, body_dict)。headers 可自定义。"""
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    h = {"Content-Type": "application/json", "Authorization": "Bearer sk-test-key"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=h, method="POST")
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


# === 默认宽松模式（无 STRICT_VALIDATION）===

def test_default_no_content_type_accepted(server):
    """默认宽松模式：无 Content-Type 应接受（200），不报 415。"""
    # 故意只发 Authorization，不发 Content-Type
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
            assert resp.status == 200, f"默认模式无 Content-Type 应接受，实际 {resp.status}"
    except urllib.error.HTTPError as e:
        assert e.code == 200, f"默认模式无 Content-Type 应接受，实际 {e.code}"


def test_default_no_anthropic_version_accepted(server):
    """默认宽松模式：无 anthropic-version 应接受（200）。"""
    status, body = _post_raw(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"默认模式无 anthropic-version 应接受，实际 {status}"


def test_default_with_content_type_works(server):
    """默认模式：带 Content-Type 正常工作。"""
    status, body = _post_raw(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200


def test_default_with_anthropic_version_works(server):
    """默认模式：带 anthropic-version 正常工作。"""
    status, body = _post_raw(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"anthropic-version": "2023-06-01"})
    assert status == 200


# === 严格模式（STRICT_VALIDATION=true）===

def test_strict_no_content_type_rejected(server, monkeypatch):
    """严格模式：无 Content-Type 应返回 415。"""
    monkeypatch.setenv("STRICT_VALIDATION", "true")
    data = json.dumps({
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server}/v1/chat/completions",
        data=data,
        headers={"Authorization": "Bearer sk-test-key", "Content-Type": "text/plain"},  # 错误类型
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            # 如果到了这里说明严格模式没生效
            assert False, f"严格模式应拒绝 text/plain Content-Type，实际 {resp.status}"
    except urllib.error.HTTPError as e:
        assert e.code == 415, f"严格模式错误 Content-Type 应返回 415，实际 {e.code}"


def test_strict_correct_content_type_accepted(server, monkeypatch):
    """严格模式：正确 Content-Type 应接受（200）。"""
    monkeypatch.setenv("STRICT_VALIDATION", "true")
    status, body = _post_raw(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"严格模式正确 Content-Type 应接受，实际 {status}"


def test_strict_no_anthropic_version_rejected(server, monkeypatch):
    """严格模式：/v1/messages 无 anthropic-version 应返回 400。"""
    monkeypatch.setenv("STRICT_VALIDATION", "true")
    # 不带 anthropic-version header
    data = json.dumps({
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{server}/v1/messages",
        data=data,
        headers={"Authorization": "Bearer sk-test-key", "Content-Type": "application/json"},
        # 故意不带 anthropic-version
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert False, f"严格模式应拒绝无 anthropic-version，实际 {resp.status}"
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"严格模式无 anthropic-version 应返回 400，实际 {e.code}"


def test_strict_with_anthropic_version_accepted(server, monkeypatch):
    """严格模式：带 anthropic-version 应接受（200）。"""
    monkeypatch.setenv("STRICT_VALIDATION", "true")
    status, body = _post_raw(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"anthropic-version": "2023-06-01"})
    assert status == 200, f"严格模式带 anthropic-version 应接受，实际 {status}"


def test_strict_chat_completions_no_anthropic_version_needed(server, monkeypatch):
    """严格模式：/v1/chat/completions 不需要 anthropic-version（只 /v1/messages 需要）。"""
    monkeypatch.setenv("STRICT_VALIDATION", "true")
    status, body = _post_raw(server, "/v1/chat/completions", {
        "model": "glm-5.2-flash",
        "messages": [{"role": "user", "content": "hi"}],
    })
    # chat/completions 不需要 anthropic-version，应该 200
    assert status == 200, f"严格模式 chat/completions 不需要 anthropic-version，实际 {status}"


# === 开关切换验证 ===

def test_strict_can_be_disabled(server, monkeypatch):
    """STRICT_VALIDATION=false 应退回宽松模式。"""
    monkeypatch.setenv("STRICT_VALIDATION", "false")
    # 无 anthropic-version 应该被接受
    status, body = _post_raw(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"STRICT_VALIDATION=false 应宽松接受，实际 {status}"


def test_strict_default_is_off(server, monkeypatch):
    """默认（未设环境变量）应该是宽松模式。"""
    monkeypatch.delenv("STRICT_VALIDATION", raising=False)
    status, body = _post_raw(server, "/v1/messages", {
        "model": "glm-5.2-flash",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200, f"默认应宽松接受，实际 {status}"
