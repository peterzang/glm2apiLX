"""v37: 内嵌 WAF bypass proxy — 集成到 glm2api 主进程。

用户不需要手动启动外部 proxy 脚本。设置环境变量 WAF_BYPASS_PORT 后，
glm2api 主进程自动启动一个额外的 HTTP 服务器，该服务器：
1. 接收 Claude Code 的请求
2. 把请求体中的反引号 ` 替换为安全字符 ˋ（绕过 Cloudflare WAF）
3. 内部转发到 glm2api 主端口（127.0.0.1:主端口，不经过 Cloudflare）
4. 支持 SSE 流式响应（chunked transfer encoding）

用法：
  export WAF_BYPASS_PORT=8001
  python main.py
  # Claude Code 连到 bypass 端口：
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8001
"""
from __future__ import annotations

import http.client
import http.server
from logging import Logger
from urllib.parse import urlparse


# 反引号替换：U+0060 → U+02CB
_BACKTICK = '\x60'
_BACKTICK_SAFE = '\u02cb'

_SSE_CONTENT_TYPE = 'text/event-stream'
_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
})


def _bypass_backticks(body: bytes) -> bytes:
    """把请求体中的反引号替换为安全字符。"""
    return body.replace(_BACKTICK.encode('utf-8'), _BACKTICK_SAFE.encode('utf-8'))


class _BypassHandler(http.server.BaseHTTPRequestHandler):
    """转发请求到目标端口，同时替换反引号。"""

    # v54: 必须设 HTTP/1.1，否则 chunked transfer encoding 不合规，
    # 且 keep-alive 失效导致 Claude Code 每个请求重建 TCP 连接。
    protocol_version = 'HTTP/1.1'
    # 与主服务对齐，伪装成 cloudflare 而非暴露 Python 版本
    server_version = 'cloudflare'
    sys_version = ''

    def log_message(self, format, *args):
        pass

    def _proxy(self, method: str) -> None:
        content_length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(content_length) if content_length else b''

        if body and _BACKTICK.encode('utf-8') in body:
            body = _bypass_backticks(body)
            print(f'  [bypass] {method} {self.path} — replaced backticks ({len(body)} bytes)')

        target_host = self.server.target_host
        target_port = self.server.target_port

        forward_headers = {}
        for key, val in self.headers.items():
            if key.lower() in _HOP_BY_HOP or key.lower() == 'host':
                continue
            forward_headers[key] = val
        if body:
            forward_headers['Content-Length'] = str(len(body))
        forward_headers['Host'] = f'{target_host}:{target_port}'
        # v54 H2: 标记请求经过 bypass proxy，server 只在有此标记时还原 ˋ → `
        # 避免直连用户 prompt 中的合法 ˋ（越南语/拼音/IPA）被误转
        forward_headers['X-WAF-Bypass'] = '1'

        try:
            conn = http.client.HTTPConnection(target_host, target_port, timeout=300)
            conn.request(method, self.path, body=body if body else None, headers=forward_headers)
            resp = conn.getresponse()
        except Exception as exc:
            import json
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            err = json.dumps({'error': {'message': f'Proxy error: {exc}', 'type': 'proxy_error'}}).encode()
            self.send_header('Content-Length', str(len(err)))
            self.end_headers()
            self.wfile.write(err)
            return

        resp_ct = resp.getheader('Content-Type', '')
        is_sse = _SSE_CONTENT_TYPE in resp_ct

        if is_sse:
            # 流式 SSE：chunked transfer 逐块转发
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                if key.lower() in _HOP_BY_HOP or key.lower() == 'content-length':
                    continue
                self.send_header(key, val)
            # v54: 显式 Connection: close — HTTP/1.1 chunked 结束后关闭连接，
            # 避免客户端误等（部分 SDK 对 chunked + keep-alive 处理不一致）
            self.send_header('Connection', 'close')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f'{len(chunk):x}\r\n'.encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b'\r\n')
                    self.wfile.flush()
                self.wfile.write(b'0\r\n\r\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()
        else:
            # 非流式：buffered 读取
            resp_body = resp.read()
            self.send_response(resp.status)
            for key, val in resp.getheaders():
                if key.lower() in _HOP_BY_HOP or key.lower() == 'content-length':
                    continue
                self.send_header(key, val)
            # v54: 显式 Connection: close，确保响应结束后连接关闭
            self.send_header('Connection', 'close')
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            self.wfile.flush()
            conn.close()

    def do_GET(self): self._proxy('GET')
    def do_POST(self): self._proxy('POST')
    def do_PUT(self): self._proxy('PUT')
    def do_DELETE(self): self._proxy('DELETE')
    def do_PATCH(self): self._proxy('PATCH')
    def do_HEAD(self): self._proxy('HEAD')
    def do_OPTIONS(self): self._proxy('OPTIONS')


class EmbeddedBypassProxy(http.server.ThreadingHTTPServer):
    """内嵌 WAF bypass proxy 服务器。"""

    target_host: str = '127.0.0.1'
    target_port: int = 8000
    daemon_threads = True

    def __init__(self, listen_host: str, listen_port: int,
                 target_host: str, target_port: int, logger: Logger | None = None):
        super().__init__((listen_host, listen_port), _BypassHandler)
        self.target_host = target_host
        self.target_port = target_port
        self._logger = logger
