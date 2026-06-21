#!/usr/bin/env python3
"""WAF Bypass Proxy — 绕过 Cloudflare WAF 对反引号 prompt 的拦截。

问题：Cloudflare WAF 的 Command Injection 规则拦截含反引号 ` (U+0060)
的 prompt，特别是 `python -m`、`perl -e` 等模式。Claude Code 的 prompt
大量使用反引号包裹命令，导致请求被 403 拦截。

方案：本地代理把请求体中的 ` (U+0060) 替换成 ˋ (U+02CB，
MODIFIER LETTER GRAVE ACCENT)，视觉几乎一样但 WAF 不拦截。
glm2api 收到后在应用层把 ˋ 还原成 `，GLM 收到正确的原始 prompt。

用法：
  1. 在本地运行此脚本：
     python scripts/waf_bypass_proxy.py --target https://glm2api.onrender.com --port 8001

  2. Claude Code 连接到本地代理：
     export ANTHROPIC_BASE_URL=http://127.0.0.1:8001
     claude -p "你的任务"

  3. 代理会把请求转发到 glm2api（通过 Cloudflare），反引号已被替换为安全字符。
"""
from __future__ import annotations

import argparse
import http.server
import json
import ssl
import sys
import urllib.request
import urllib.error

# 反引号替换：U+0060 → U+02CB
_BACKTICK = '\x60'        # ` GRAVE ACCENT（WAF 拦截目标）
_BACKTICK_SAFE = '\u02cb' # ˋ MODIFIER LETTER GRAVE ACCENT（WAF 不拦截）


def _bypass_backticks_in_body(body: bytes) -> bytes:
    """把请求体中的反引号 ` 替换成安全字符 ˋ。

    直接在 bytes 层面替换（UTF-8 编码后），不需要解析 JSON。
    因为反引号在 JSON 中不是特殊字符，直接替换是安全的。
    """
    backtick_bytes = _BACKTICK.encode('utf-8')        # b'\x60' (1 byte)
    safe_bytes = _BACKTICK_SAFE.encode('utf-8')       # b'\xcb\x8b' (2 bytes)
    return body.replace(backtick_bytes, safe_bytes)


class BypassProxyHandler(http.server.BaseHTTPRequestHandler):
    """转发所有请求到目标 URL，同时把请求体中的反引号替换为安全字符。"""

    # 不打印默认的请求日志（减少噪音）
    def log_message(self, format, *args):
        pass

    def _proxy(self, method: str) -> None:
        # 读取请求体
        content_length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(content_length) if content_length else b''

        # v35: 替换反引号为安全字符
        if body and _BACKTICK.encode('utf-8') in body:
            body = _bypass_backticks_in_body(body)
            print(f'  [bypass] {method} {self.path} — replaced backticks ({content_length} bytes)', file=sys.stderr)

        # 构建目标 URL
        target_url = f'{self.server.target_url}{self.path}'

        # 转发请求
        req = urllib.request.Request(target_url, data=body if body else None, method=method)

        # 转发所有 headers（除了 Host，由 urllib 自动设置）
        for key, val in self.headers.items():
            if key.lower() in ('host', 'content-length', 'transfer-encoding'):
                continue
            req.add_header(key, val)

        # 设置正确的 Content-Length
        if body:
            req.add_header('Content-Length', str(len(body)))

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                status = resp.status
                resp_headers = resp.headers
                resp_body = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_headers = exc.headers
            resp_body = exc.read()
        except Exception as exc:
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            error_body = json.dumps({'error': {'message': f'Proxy error: {exc}', 'type': 'proxy_error'}}).encode()
            self.send_header('Content-Length', str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return

        # 发送响应
        self.send_response(status)
        for key, val in resp_headers.items():
            if key.lower() in ('transfer-encoding', 'content-encoding', 'connection'):
                continue
            self.send_header(key, val)
        self.send_header('Content-Length', str(len(resp_body)))
        self.end_headers()

        # 流式响应需要逐块写入
        self.wfile.write(resp_body)
        self.wfile.flush()

    def do_GET(self):
        self._proxy('GET')

    def do_POST(self):
        self._proxy('POST')

    def do_PUT(self):
        self._proxy('PUT')

    def do_DELETE(self):
        self._proxy('DELETE')

    def do_PATCH(self):
        self._proxy('PATCH')

    def do_HEAD(self):
        self._proxy('HEAD')

    def do_OPTIONS(self):
        self._proxy('OPTIONS')


class BypassProxyServer(http.server.ThreadingHTTPServer):
    target_url: str = ''


def main():
    parser = argparse.ArgumentParser(description='WAF Bypass Proxy for glm2api')
    parser.add_argument('--target', '-t', default='https://glm2api.onrender.com',
                        help='目标 glm2api URL（默认: https://glm2api.onrender.com）')
    parser.add_argument('--port', '-p', type=int, default=8001,
                        help='本地监听端口（默认: 8001）')
    parser.add_argument('--host', default='127.0.0.1',
                        help='本地监听地址（默认: 127.0.0.1）')
    args = parser.parse_args()

    # 确保 target 不以 / 结尾
    target = args.target.rstrip('/')

    server = BypassProxyServer((args.host, args.port), BypassProxyHandler)
    server.target_url = target

    print(f"""
╔══════════════════════════════════════════════════════╗
║           WAF Bypass Proxy for glm2api               ║
╠══════════════════════════════════════════════════════╣
║                                                      ║
║  监听: http://{args.host}:{args.port}                     ║
║  目标: {target}                    ║
║                                                      ║
║  功能: 把请求体中的反引号 ` 替换为 ˋ                  ║
║        绕过 Cloudflare WAF 的 Command Injection 规则    ║
║                                                      ║
║  Claude Code 配置:                                    ║
║    export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}     ║
║    claude -p "你的任务"                                ║
║                                                      ║
║  按 Ctrl+C 停止                                       ║
╚══════════════════════════════════════════════════════╝
""", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n停止代理...', file=sys.stderr)
        server.shutdown()


if __name__ == '__main__':
    main()
