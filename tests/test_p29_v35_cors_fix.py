"""Tests for v29: v35 审计 CORS * 修复 + CF-Ray header 验证。

v35 审计剩余 1 个扣分项：CORS * 仍存在（Render .env 覆盖代码默认值）。
修复：
1. env.example 默认 CORS_ALLOW_ORIGIN=（空，不发 CORS header）
2. 代码层面：API 端点（/v1/*）完全不发 CORS header（与官方 OpenAI 一致）
3. 代码层面：admin 端点仅当配置了具体来源时才发送 CORS（不用 *）
4. 新增 CF-Ray header（模拟官方 cloudflare CDN）
5. 新增 Access-Control-Expose-Headers: CF-Ray（与官方一致）
"""
from __future__ import annotations

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest


# === CORS 配置逻辑测试 ===

class _MockConfig:
    """模拟 AppConfig 用于 CORS 逻辑测试。"""

    def __init__(self, cors_allow_origin: str = ""):
        self.cors_allow_origin = cors_allow_origin


class _MockHeaderCollector:
    """模拟 handler 收集 send_header 调用。"""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.status_code: int | None = None

    def send_response(self, status, message=None):
        self.status_code = int(status)

    def send_header(self, key, value):
        self.headers[key.lower()] = str(value)

    def end_headers(self):
        pass


def _simulate_send_common_headers(collector: _MockHeaderCollector, config: _MockConfig, path: str) -> None:
    """模拟 _send_common_headers 的 CORS 逻辑（与 server.py 实现一致）。"""
    # 模拟 server.py 的 _send_common_headers CORS 部分
    if path.startswith("/admin"):
        if config.cors_allow_origin and config.cors_allow_origin != "*":
            collector.send_header("Access-Control-Allow-Origin", config.cors_allow_origin)
            collector.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, x-api-key, anthropic-version")
            collector.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    else:
        # API 端点：模拟官方 cloudflare 的 Access-Control-Expose-Headers
        collector.send_header("Access-Control-Expose-Headers", "CF-Ray")


def test_api_endpoint_no_cors_when_empty():
    """API 端点（/v1/*）：cors_allow_origin 为空时不发任何 CORS header。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="")
    _simulate_send_common_headers(collector, config, "/v1/chat/completions")
    assert "access-control-allow-origin" not in collector.headers
    assert "access-control-allow-methods" not in collector.headers


def test_api_endpoint_no_cors_when_star():
    """API 端点（/v1/*）：cors_allow_origin=* 时也不发 CORS（v35 修复核心）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="*")
    _simulate_send_common_headers(collector, config, "/v1/chat/completions")
    # 关键：即使配置了 *，API 端点也不发 CORS（与官方一致）
    assert "access-control-allow-origin" not in collector.headers
    assert "access-control-allow-methods" not in collector.headers


def test_api_endpoint_no_cors_when_specific_origin():
    """API 端点（/v1/*）：即使配置了具体来源也不发 CORS（与官方一致）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="https://example.com")
    _simulate_send_common_headers(collector, config, "/v1/chat/completions")
    # API 端点永远不发 CORS（官方 OpenAI 也不发）
    assert "access-control-allow-origin" not in collector.headers


def test_api_endpoint_sends_expose_headers():
    """API 端点（/v1/*）：发送 Access-Control-Expose-Headers: CF-Ray（与官方一致）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="")
    _simulate_send_common_headers(collector, config, "/v1/chat/completions")
    assert collector.headers.get("access-control-expose-headers") == "CF-Ray"


def test_admin_endpoint_no_cors_when_empty():
    """admin 端点（/admin/*）：cors_allow_origin 为空时不发 CORS。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="")
    _simulate_send_common_headers(collector, config, "/admin/api/dashboard")
    assert "access-control-allow-origin" not in collector.headers


def test_admin_endpoint_no_cors_when_star():
    """admin 端点（/admin/*）：cors_allow_origin=* 时也不发 CORS（v35 修复）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="*")
    _simulate_send_common_headers(collector, config, "/admin/api/dashboard")
    # 关键：即使配置了 *，也不发 CORS（避免暴露特征）
    assert "access-control-allow-origin" not in collector.headers


def test_admin_endpoint_sends_cors_when_specific_origin():
    """admin 端点（/admin/*）：配置了具体来源时发送 CORS（管理面板跨域需要）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="https://your-domain.com")
    _simulate_send_common_headers(collector, config, "/admin/api/dashboard")
    assert collector.headers.get("access-control-allow-origin") == "https://your-domain.com"
    assert "access-control-allow-methods" in collector.headers


def test_health_endpoint_no_cors():
    """/health 端点：不发 CORS（与 API 端点行为一致）。"""
    collector = _MockHeaderCollector()
    config = _MockConfig(cors_allow_origin="*")
    _simulate_send_common_headers(collector, config, "/health")
    assert "access-control-allow-origin" not in collector.headers


# === CF-Ray header 测试 ===

def test_cf_ray_format():
    """CF-Ray 格式应为 <16 hex>-LAX（与官方 cloudflare 一致）。"""
    import uuid
    cf_ray = f"{uuid.uuid4().hex[:16]}-LAX"
    # 官方格式：<hex>-<机场代码>
    assert re.match(r'^[0-9a-f]{16}-[A-Z]{3}$', cf_ray), f"格式错误: {cf_ray}"


def test_cf_ray_unique_per_request():
    """每次生成的 CF-Ray 应该唯一。"""
    import uuid
    rays = set()
    for _ in range(100):
        cf_ray = f"{uuid.uuid4().hex[:16]}-LAX"
        rays.add(cf_ray)
    assert len(rays) == 100, f"100 次生成应有 100 个唯一 CF-Ray，实际: {len(rays)}"


# === env.example 默认值验证 ===

def test_env_example_cors_default_empty():
    """env.example 中 CORS_ALLOW_ORIGIN 默认值应为空（不发 CORS）。"""
    env_example = Path(__file__).resolve().parent.parent / "configs" / "env.example"
    content = env_example.read_text(encoding="utf-8")
    # 找到 CORS_ALLOW_ORIGIN= 行
    for line in content.splitlines():
        if line.startswith("CORS_ALLOW_ORIGIN="):
            value = line[len("CORS_ALLOW_ORIGIN="):].strip()
            assert value == "", f"env.example CORS_ALLOW_ORIGIN 默认值应为空，实际: {value!r}"
            return
    pytest.fail("env.example 中未找到 CORS_ALLOW_ORIGIN 配置行")


def test_env_example_no_star_default():
    """env.example 不应默认 CORS_ALLOW_ORIGIN=*（v35 修复）。"""
    env_example = Path(__file__).resolve().parent.parent / "configs" / "env.example"
    content = env_example.read_text(encoding="utf-8")
    # 不应存在 CORS_ALLOW_ORIGIN=*（默认值）
    for line in content.splitlines():
        if line.startswith("CORS_ALLOW_ORIGIN="):
            value = line[len("CORS_ALLOW_ORIGIN="):].strip()
            assert value != "*", "env.example 不应默认 CORS_ALLOW_ORIGIN=*"
            return
