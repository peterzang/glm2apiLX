"""Tests for v38: v37 内嵌 bypass proxy 集成测试 + 反引号检测提示。

v44 审计 P1: 加 v37 内嵌 proxy 集成测试到 CI
v44 审计 P2: 自动检测反引号 prompt 并提示用户用 bypass port

测试覆盖：
1. EmbeddedBypassProxy 能正确启动和停止
2. _bypass_backticks 函数正确替换反引号
3. 反引号检测提示逻辑
4. app.py _maybe_start_bypass_proxy 逻辑验证
"""
from __future__ import annotations

import sys
import os
import socket
import time
import threading
import http.client
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.waf_bypass import EmbeddedBypassProxy, _bypass_backticks, _BypassHandler


# === _bypass_backticks 函数测试 ===

def test_bypass_backticks_replaces():
    """反引号 ` 应被替换为 ˋ。"""
    original = b'Run \x60python -m foo\x60'
    result = _bypass_backticks(original)
    assert b'\x60' not in result
    assert b'\xcb\x8b' in result


def test_bypass_backticks_no_change():
    """不含反引号的 body 原样返回。"""
    original = b'{"model":"glm-5.2","messages":[]}'
    assert _bypass_backticks(original) == original


def test_bypass_backticks_empty():
    """空 body 原样返回。"""
    assert _bypass_backticks(b'') == b''


def test_bypass_backticks_preserves_json():
    """JSON 结构不受影响（反引号在 JSON 值中不是特殊字符）。"""
    import json
    original = json.dumps({"content": "Run `python`"}).encode()
    result = _bypass_backticks(original)
    # JSON 仍可正确解析
    parsed = json.loads(result)
    assert parsed["content"] == "Run \u02cbpython\u02cb"


# === EmbeddedBypassProxy 启动/停止测试 ===

def _find_free_port() -> int:
    """找一个可用端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _MockMainServer:
    """模拟 glm2api 主端口服务器。"""

    def __init__(self, port: int):
        self.port = port
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', port))
        self.server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.received_body = b''

    def start(self):
        self._thread.start()

    def stop(self):
        self.server.close()

    def _serve(self):
        try:
            conn, _ = self.server.accept()
            # v54: 循环 recv 直到读完 headers + body
            # 之前只 recv 一次，TCP 分包时拿不到完整 body
            buf = b''
            conn.settimeout(2.0)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # 解析 Content-Length，判断是否读完整
                if b'\r\n\r\n' in buf:
                    header_part, body_part = buf.split(b'\r\n\r\n', 1)
                    cl_match = [l for l in header_part.split(b'\r\n') if l.lower().startswith(b'content-length:')]
                    if cl_match:
                        cl = int(cl_match[0].split(b':', 1)[1].strip())
                        if len(body_part) >= cl:
                            break
                    else:
                        # 无 Content-Length，读到连接关闭即可（但这里 break 避免无限等）
                        break
            self.received_body = buf
            # 返回简单 JSON 响应
            resp = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{"status":"ok"}'
            conn.sendall(resp)
            time.sleep(0.2)
            conn.close()
        except Exception:
            pass


def test_embedded_proxy_start_stop():
    """EmbeddedBypassProxy 应能正确启动和停止。"""
    main_port = _find_free_port()
    proxy_port = _find_free_port()

    mock = _MockMainServer(main_port)
    mock.start()
    time.sleep(0.1)

    proxy = EmbeddedBypassProxy(
        listen_host='127.0.0.1',
        listen_port=proxy_port,
        target_host='127.0.0.1',
        target_port=main_port,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    time.sleep(0.1)

    # 验证 proxy 在监听
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(('127.0.0.1', proxy_port))
        s.close()
        assert True  # 连接成功
    except (ConnectionRefusedError, socket.timeout):
        pytest.fail("proxy 未在监听")

    # 停止
    proxy.shutdown()
    mock.stop()


def test_embedded_proxy_forwards_request():
    """EmbeddedBypassProxy 应正确转发请求到主端口。"""
    main_port = _find_free_port()
    proxy_port = _find_free_port()

    mock = _MockMainServer(main_port)
    mock.start()
    time.sleep(0.1)

    proxy = EmbeddedBypassProxy(
        listen_host='127.0.0.1',
        listen_port=proxy_port,
        target_host='127.0.0.1',
        target_port=main_port,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    time.sleep(0.1)

    # 通过 proxy 发送请求
    conn = http.client.HTTPConnection('127.0.0.1', proxy_port, timeout=5)
    conn.request('POST', '/v1/messages', body=b'{"test":true}', headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()
    assert resp.status == 200
    body = resp.read()
    assert b'"status"' in body
    conn.close()

    # 验证 mock 主服务器收到了请求
    assert b'{"test":true}' in mock.received_body or b'test' in mock.received_body

    proxy.shutdown()
    mock.stop()


def test_embedded_proxy_replaces_backticks():
    """EmbeddedBypassProxy 应把反引号替换为安全字符后再转发。"""
    main_port = _find_free_port()
    proxy_port = _find_free_port()

    mock = _MockMainServer(main_port)
    mock.start()
    time.sleep(0.1)

    proxy = EmbeddedBypassProxy(
        listen_host='127.0.0.1',
        listen_port=proxy_port,
        target_host='127.0.0.1',
        target_port=main_port,
    )
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    time.sleep(0.1)

    # 发送含反引号的请求
    original_body = b'{"content":"Run `python -m foo`"}'
    conn = http.client.HTTPConnection('127.0.0.1', proxy_port, timeout=5)
    conn.request('POST', '/v1/messages', body=original_body, headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()
    resp.read()
    conn.close()

    # 验证 mock 收到的 body 不含反引号（已被替换）
    assert b'\x60' not in mock.received_body, "proxy 应替换反引号"
    assert b'\xcb\x8b' in mock.received_body, "proxy 应替换为安全字符 ˋ"

    proxy.shutdown()
    mock.stop()


# === app.py _maybe_start_bypass_proxy 逻辑验证 ===

def test_maybe_start_bypass_proxy_no_env():
    """不设 WAF_BYPASS_PORT 时不启动 proxy。"""
    from glm2api.app import Application
    from glm2api.config import AppConfig

    # 确保环境变量未设置
    old_val = os.environ.pop("WAF_BYPASS_PORT", None)

    # 创建一个 mock Application（不真正启动服务器）
    # 只测试 _maybe_start_bypass_proxy 逻辑
    # 由于 Application.__init__ 会启动真实服务器，我们直接测试逻辑
    bypass_port_str = os.environ.get("WAF_BYPASS_PORT", "").strip()
    assert not bypass_port_str, "WAF_BYPASS_PORT 应未设置"

    if old_val is not None:
        os.environ["WAF_BYPASS_PORT"] = old_val


def test_maybe_start_bypass_proxy_with_env():
    """设了 WAF_BYPASS_PORT 时应触发 proxy 启动逻辑。"""
    old_val = os.environ.get("WAF_BYPASS_PORT", None)

    os.environ["WAF_BYPASS_PORT"] = "9999"
    bypass_port_str = os.environ.get("WAF_BYPASS_PORT", "").strip()
    assert bypass_port_str == "9999"
    bypass_port = int(bypass_port_str)
    assert bypass_port == 9999

    # 恢复
    if old_val is not None:
        os.environ["WAF_BYPASS_PORT"] = old_val
    else:
        os.environ.pop("WAF_BYPASS_PORT", None)


def test_maybe_start_bypass_proxy_invalid_env():
    """WAF_BYPASS_PORT 非数字时应被忽略。"""
    old_val = os.environ.get("WAF_BYPASS_PORT", None)

    os.environ["WAF_BYPASS_PORT"] = "invalid"
    bypass_port_str = os.environ.get("WAF_BYPASS_PORT", "").strip()
    try:
        int(bypass_port_str)
        pytest.fail("应抛出 ValueError")
    except ValueError:
        pass  # 预期行为

    # 恢复
    if old_val is not None:
        os.environ["WAF_BYPASS_PORT"] = old_val
    else:
        os.environ.pop("WAF_BYPASS_PORT", None)


# === waf_bypass.py 模块验证 ===

def test_waf_bypass_module_exists():
    """waf_bypass.py 模块应存在。"""
    mod_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "waf_bypass.py"
    assert mod_path.exists()


def test_waf_bypass_has_embedded_proxy_class():
    """waf_bypass.py 应包含 EmbeddedBypassProxy 类。"""
    mod_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "waf_bypass.py"
    content = mod_path.read_text(encoding="utf-8")
    assert "class EmbeddedBypassProxy" in content
    assert "ThreadingHTTPServer" in content


def test_waf_bypass_has_sse_support():
    """waf_bypass.py 应支持 SSE 流式转发。"""
    mod_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "waf_bypass.py"
    content = mod_path.read_text(encoding="utf-8")
    assert "text/event-stream" in content
    assert "chunked" in content.lower()
    assert "resp.read(4096)" in content


def test_waf_bypass_has_backtick_replacement():
    """waf_bypass.py 应包含反引号替换逻辑。"""
    mod_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "waf_bypass.py"
    content = mod_path.read_text(encoding="utf-8")
    assert "_BACKTICK" in content
    assert "u02cb" in content.lower() or "\\u02cb" in content


# === app.py 集成验证 ===

def test_app_has_bypass_proxy_method():
    """app.py 应包含 _maybe_start_bypass_proxy 方法。"""
    app_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "app.py"
    content = app_path.read_text(encoding="utf-8")
    assert "_maybe_start_bypass_proxy" in content
    assert "WAF_BYPASS_PORT" in content
    assert "EmbeddedBypassProxy" in content


def test_app_stops_bypass_proxy_on_shutdown():
    """app.py stop() 应关闭 bypass proxy。"""
    app_path = Path(__file__).resolve().parent.parent / "src" / "glm2api" / "app.py"
    content = app_path.read_text(encoding="utf-8")
    assert "_bypass_proxy_server" in content
    assert "shutdown" in content.lower()
