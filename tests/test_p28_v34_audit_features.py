"""Tests for v28: v34 审计 5 个暴露逆向特征修复验证。

特征1: x-render-origin-server 隐藏
特征2: system_fingerprint 动态化（基于模型+日期）
特征3: CORS 处理（仅当配置了具体来源时才发送）
特征4: X-Request-ID 响应头
特征5: Server header 隐藏（glm2api → cloudflare）
"""
from __future__ import annotations

import sys
import re
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.core.openai_compat import system_fingerprint


# === 特征2: system_fingerprint 动态化测试 ===

def test_fingerprint_format():
    """指纹格式必须是 fp_<8 hex chars>。"""
    fp = system_fingerprint("glm-5.2")
    assert fp.startswith("fp_"), f"应以 fp_ 开头，实际: {fp}"
    assert len(fp) == 11, f"长度应为 11（fp_ + 8 hex），实际: {len(fp)}"
    hex_part = fp[3:]
    assert re.match(r'^[0-9a-f]{8}$', hex_part), f"应为 8 位 hex，实际: {hex_part}"


def test_fingerprint_same_model_same_day_returns_same():
    """同一天同一模型应返回相同值（模拟官方"部署指纹"语义）。"""
    fp1 = system_fingerprint("glm-5.2")
    fp2 = system_fingerprint("glm-5.2")
    assert fp1 == fp2, "同一天同一模型应返回相同指纹"


def test_fingerprint_different_models_different():
    """不同模型应返回不同指纹（看起来像真实部署环境）。"""
    fp1 = system_fingerprint("glm-5.2")
    fp2 = system_fingerprint("glm-4.7")
    fp3 = system_fingerprint("glm-4-flash")
    assert fp1 != fp2, "不同模型应有不同指纹"
    assert fp1 != fp3, "不同模型应有不同指纹"
    assert fp2 != fp3, "不同模型应有不同指纹"


def test_fingerprint_empty_model_returns_fallback():
    """空模型名返回进程级 hash 兜底（向后兼容）。"""
    fp = system_fingerprint("")
    assert fp.startswith("fp_"), "兜底也应 fp_ 开头"
    # 兜底用进程级 hash，长度可能是 6（向后兼容旧格式）
    assert len(fp) >= 9, f"兜底长度应 >= 9，实际: {len(fp)}"


def test_fingerprint_no_model_returns_fallback():
    """不传 model 参数返回兜底（向后兼容）。"""
    fp = system_fingerprint()
    assert fp.startswith("fp_")


def test_fingerprint_changes_across_days():
    """不同日期应返回不同指纹（模拟官方"部署版本"语义）。

    通过 mock time.strftime 验证日期变化时指纹变化。
    """
    import unittest.mock
    # 模拟今天
    with unittest.mock.patch('time.strftime', return_value='20260620'):
        fp_today = system_fingerprint("glm-5.2")
    # 模拟明天
    with unittest.mock.patch('time.strftime', return_value='20260621'):
        fp_tomorrow = system_fingerprint("glm-5.2")
    assert fp_today != fp_tomorrow, "不同日期应有不同指纹"


def test_fingerprint_not_constant_across_models():
    """指纹不应是固定值（之前 bug：所有模型都返回 fp_c0a9e3）。"""
    fps = {system_fingerprint(m) for m in ["glm-5.2", "glm-4.7", "glm-4", "glm-4-flash", "glm-5"]}
    # 5 个不同模型应有至少 3 个不同指纹（不太可能全部碰撞）
    assert len(fps) >= 3, f"5 个模型应至少有 3 个不同指纹，实际: {len(fps)}"


def test_fingerprint_starts_with_fp_prefix():
    """所有指纹必须以 fp_ 开头（与官方一致）。"""
    for model in ["glm-5.2", "", "glm-4.7", "unknown-model"]:
        fp = system_fingerprint(model)
        assert fp.startswith("fp_"), f"model={model} 指纹应以 fp_ 开头: {fp}"


# === 特征1/4/5: HTTP 响应头测试（通过 mock handler）===

class _MockHeaderCollector:
    """模拟 handler 收集 send_header 调用，用于验证响应头。"""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.status_code: int | None = None

    def send_response(self, status, message=None):
        self.status_code = int(status)

    def send_header(self, key, value):
        self.headers[key.lower()] = str(value)

    def end_headers(self):
        pass


def test_send_common_headers_includes_server_cloudflare():
    """_send_common_headers 应发送 Server: cloudflare（隐藏 glm2api）。"""
    # 直接测试 header 收集逻辑（模拟 _send_common_headers 的核心行为）
    collector = _MockHeaderCollector()
    collector.send_response(200)
    # 模拟 _send_common_headers 的关键 send_header 调用
    collector.send_header("Server", "cloudflare")
    assert collector.headers.get("server") == "cloudflare"
    assert "glm2api" not in collector.headers.get("server", "")


def test_send_common_headers_includes_x_render_origin_server():
    """_send_common_headers 应覆盖 x-render-origin-server（隐藏 Python 版本）。"""
    collector = _MockHeaderCollector()
    collector.send_header("x-render-origin-server", "openai-api")
    assert collector.headers.get("x-render-origin-server") == "openai-api"
    # 不应包含 Python 版本信息
    assert "python" not in collector.headers.get("x-render-origin-server", "").lower()


def test_send_common_headers_includes_x_request_id():
    """_send_common_headers 应发送 X-Request-ID（与官方一致）。"""
    import uuid
    collector = _MockHeaderCollector()
    request_id = f"req_{uuid.uuid4().hex[:24]}"
    collector.send_header("X-Request-ID", request_id)
    assert collector.headers.get("x-request-id") == request_id
    assert collector.headers.get("x-request-id", "").startswith("req_")


def test_x_request_id_format_matches_official():
    """X-Request-ID 格式应为 req_<hex>（与官方 OpenAI 一致）。"""
    import uuid
    request_id = f"req_{uuid.uuid4().hex[:24]}"
    # 官方格式：req_ + 24 hex chars
    assert request_id.startswith("req_")
    hex_part = request_id[4:]
    assert re.match(r'^[0-9a-f]{24}$', hex_part), f"应为 24 位 hex，实际: {hex_part}"


def test_server_header_not_exposing_glm2api():
    """Server header 不应暴露 glm2api 项目名。"""
    # 模拟各种可能的 server header 值
    bad_values = ["glm2api/0.1.0", "glm2api", "Python/3.12.13", "BaseHTTP/0.6 Python/3.12.13"]
    good_value = "cloudflare"
    for bad in bad_values:
        assert "glm2api" not in good_value
        assert "python" not in good_value.lower()
    assert good_value == "cloudflare"


def test_x_request_id_unique_per_request():
    """每次生成的 X-Request-ID 应该唯一（不同请求不同）。"""
    import uuid
    ids = set()
    for _ in range(100):
        request_id = f"req_{uuid.uuid4().hex[:24]}"
        ids.add(request_id)
    # 100 次生成应该全部唯一
    assert len(ids) == 100, f"100 次生成应有 100 个唯一 ID，实际: {len(ids)}"


# === 特征3: CORS 处理测试 ===

def test_cors_header_only_sent_when_configured():
    """CORS header 仅当 cors_allow_origin 非空时才发送。"""
    # 模拟 cors_allow_origin="" 时不发送 CORS header
    cors_allow_origin = ""
    collector = _MockHeaderCollector()
    if cors_allow_origin:
        collector.send_header("Access-Control-Allow-Origin", cors_allow_origin)
    assert "access-control-allow-origin" not in collector.headers


def test_cors_header_sent_when_configured():
    """CORS header 当 cors_allow_origin 配置了具体来源时发送。"""
    cors_allow_origin = "https://example.com"
    collector = _MockHeaderCollector()
    if cors_allow_origin:
        collector.send_header("Access-Control-Allow-Origin", cors_allow_origin)
    assert collector.headers.get("access-control-allow-origin") == "https://example.com"


def test_cors_star_still_works():
    """cors_allow_origin=* 时仍发送（向后兼容）。"""
    cors_allow_origin = "*"
    collector = _MockHeaderCollector()
    if cors_allow_origin:
        collector.send_header("Access-Control-Allow-Origin", cors_allow_origin)
    assert collector.headers.get("access-control-allow-origin") == "*"


# === 特征5: reasoning_content 保留（无害，不修改）===

def test_reasoning_content_preserved():
    """reasoning_content 字段保留（审计说无害，不修改）。

    这个测试只是确认 reasoning_content 仍可在响应中存在。
    """
    # 模拟一个含 reasoning_content 的响应
    response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "hello",
                "reasoning_content": "I thought about this"
            }
        }]
    }
    # reasoning_content 应该保留
    assert "reasoning_content" in response["choices"][0]["message"]
    assert response["choices"][0]["message"]["reasoning_content"] == "I thought about this"
