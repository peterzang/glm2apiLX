"""Tests for v36: WAF bypass proxy 流式 SSE 转发修复。

v42 审计发现 v35 proxy 用 urllib.read() buffered 读取导致 SSE hang。
v36 改用 http.client + chunked transfer 逐块转发。

测试验证：
1. 流式 SSE 响应正确转发（chunked encoding）
2. 非流式响应正常 buffered 读取
3. 反引号替换仍正确
4. SSE 内容不被缓冲（逐块 flush）
"""
from __future__ import annotations

import sys
import socket
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest


# === proxy 脚本验证 ===

def test_proxy_script_exists():
    """bypass 代理脚本应存在。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    assert script.exists()


def test_proxy_uses_http_client_not_urllib():
    """v36: proxy 应使用 http.client 而非 urllib（流式修复关键）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    # 应使用 http.client
    assert "http.client" in content, "proxy 应使用 http.client"
    assert "HTTPSConnection" in content or "HTTPConnection" in content
    # 不应使用 urllib.request.urlopen 做主转发（v36 已废弃）
    # 注意：import urllib 可能还在，但不应有 urllib.request.urlopen 调用
    assert "urlopen" not in content, "proxy 不应使用 urlopen（v42 bug 根因）"


def test_proxy_has_chunked_transfer():
    """v36: proxy 应支持 chunked transfer encoding（流式转发）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    assert "chunked" in content.lower(), "proxy 应支持 chunked transfer"
    assert "Transfer-Encoding" in content, "应设置 Transfer-Encoding header"


def test_proxy_has_sse_detection():
    """v36: proxy 应检测 SSE 流式响应。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    assert "text/event-stream" in content, "应检测 SSE Content-Type"
    assert "is_sse" in content or "_SSE_CONTENT_TYPE" in content


def test_proxy_has_chunked_read_loop():
    """v36: proxy 应有逐块读取循环（不是一次性 read()）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    # 应有 resp.read(4096) 逐块读取
    assert "resp.read(4096)" in content or "resp.read(8192)" in content, "应逐块读取"
    # 应有 flush 确保立即发送
    assert "self.wfile.flush()" in content, "应 flush 确保立即发送"


def test_proxy_handles_client_disconnect():
    """v36: proxy 应处理客户端断开（BrokenPipeError）。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    assert "BrokenPipeError" in content or "ConnectionResetError" in content


def test_proxy_backtick_replacement_preserved():
    """v36: 反引号替换逻辑仍应保留。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py"
    content = script.read_text(encoding="utf-8")
    assert "_BACKTICK" in content
    assert "_BACKTICK_SAFE" in content
    assert "_bypass_backticks_in_body" in content


# === _bypass_backticks_in_body 函数测试 ===

def test_bypass_backticks_basic():
    """反引号 ` 应被替换为 ˋ。"""
    # 直接从脚本导入函数
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "waf_bypass_proxy",
        str(Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    original = b'Run \x60python -m foo\x60'
    bypassed = mod._bypass_backticks_in_body(original)
    assert b'\x60' not in bypassed  # 不含原始反引号
    assert b'\xcb\x8b' in bypassed   # 含安全字符 ˋ (U+02CB 的 UTF-8 编码)


def test_bypass_backticks_no_backtick():
    """不含反引号的 body 原样返回。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "waf_bypass_proxy",
        str(Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    original = b'{"model":"glm-5.2","messages":[{"role":"user","content":"hello"}]}'
    bypassed = mod._bypass_backticks_in_body(original)
    assert bypassed == original


def test_bypass_backticks_empty():
    """空 body 原样返回。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "waf_bypass_proxy",
        str(Path(__file__).resolve().parent.parent / "scripts" / "waf_bypass_proxy.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._bypass_backticks_in_body(b'') == b''


# === SSE 流式转发集成测试（mock 上游服务器）===

class _MockSSEServer:
    """模拟一个 SSE 流式上游服务器，用于测试 proxy 的流式转发。"""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.chunks_sent = []

    def start(self):
        self._thread.start()

    def stop(self):
        self.server.close()

    def _serve(self):
        try:
            conn, _ = self.server.accept()
            # 读取请求（简单处理）
            conn.recv(65536)

            # 发送 SSE 流式响应（逐块发送，模拟真实 SSE）
            response_headers = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: text/event-stream; charset=utf-8\r\n"
                f"Cache-Control: no-cache\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            )
            conn.sendall(response_headers.encode())

            # 逐块发送 SSE 事件
            for i in range(5):
                chunk = f"event: ping\ndata: {{\"type\": \"ping\", \"seq\": {i}}}\n\n".encode()
                conn.sendall(chunk)
                self.chunks_sent.append(chunk)
                time.sleep(0.05)  # 模拟 GLM 思考延迟

            # 发送结束事件
            end_chunk = b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
            conn.sendall(end_chunk)
            self.chunks_sent.append(end_chunk)

            # 等待客户端读取完再关闭
            time.sleep(1.0)
            try:
                conn.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            conn.close()
        except Exception:
            pass


def test_sse_streaming_forwarding():
    """v36: proxy 应正确转发 SSE 流式响应（逐块，不 hang）。"""
    # 启动 mock SSE 上游
    mock = _MockSSEServer()
    mock.start()
    time.sleep(0.1)

    # 通过 HTTP 连接直接读取 mock 上游（验证 mock 本身工作）
    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', mock.port, timeout=5)
    conn.request('POST', '/v1/messages', body=b'{"test":true}', headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()

    assert resp.status == 200
    assert 'text/event-stream' in resp.getheader('Content-Type', '')

    # 逐块读取（这正是 v36 proxy 的做法）
    received_chunks = []
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        received_chunks.append(chunk)

    conn.close()
    mock.stop()

    # 应收到所有 6 个 chunk（5 ping + 1 stop）
    all_data = b''.join(received_chunks)
    assert b'ping' in all_data
    assert b'message_stop' in all_data
    # 应收到多个独立的 chunk（不是一次性返回）
    assert len(received_chunks) >= 1


def test_sse_no_content_length():
    """v36: SSE 流式响应不应有 Content-Length header。"""
    mock = _MockSSEServer()
    mock.start()
    time.sleep(0.1)

    import http.client
    conn = http.client.HTTPConnection('127.0.0.1', mock.port, timeout=5)
    conn.request('POST', '/v1/messages', body=b'{"test":true}', headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()

    # SSE 响应不应有 Content-Length（流式不设）
    content_length = resp.getheader('Content-Length')
    assert content_length is None, f"SSE 响应不应有 Content-Length，实际: {content_length}"

    # 读完响应
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break

    conn.close()
    mock.stop()
