"""Tests for v23: 仪表盘 KPI 拆分 API/Models/Other 三类请求。

Covers:
- classify_category: 协议 → 大类映射
- record_request: 三类计数器分别累加
- dashboard: 返回 api_total / models_total / other_total 字段
- dashboard: 5 分钟窗口按类别拆分
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.admin.store import AdminStore, RequestRecord, classify_category


def _make_rec(
    *,
    protocol: str = "openai-chat",
    status: int = 200,
    path: str = "/v1/chat/completions",
    ts: float | None = None,
) -> RequestRecord:
    return RequestRecord(
        ts=ts if ts is not None else time.time(),
        method="POST",
        path=path,
        protocol=protocol,
        model="glm-5.2",
        status=status,
        duration_ms=100,
        client_ip="127.0.0.1",
        account_index=0,
        stream=False,
        error="",
        request_id="req_test",
    )


# === classify_category 测试 ===

def test_classify_api_protocols():
    """业务协议都归到 api 类。"""
    for proto in ("openai-chat", "anthropic", "openai-responses",
                  "openai-legacy", "openai-images",
                  "openai-embeddings", "openai-moderations"):
        assert classify_category(proto) == "api", f"{proto} 应归 api"


def test_classify_meta_is_models():
    """/v1/models + /health 的 meta 协议归到 models 类。"""
    assert classify_category("meta") == "models"


def test_classify_other():
    """未识别协议归到 other。"""
    assert classify_category("other") == "other"
    assert classify_category("unknown-protocol") == "other"


# === record_request 三类计数器测试 ===

@pytest.fixture
def store() -> AdminStore:
    return AdminStore()


def test_record_api_request_increments_api_counter(store):
    """业务请求只增加 api 类计数器，不影响 models/other。"""
    store.record_request(_make_rec(protocol="openai-chat", status=200))
    store.record_request(_make_rec(protocol="openai-chat", status=400))
    store.record_request(_make_rec(protocol="anthropic", status=500))
    d = store.dashboard()
    assert d["all_time"]["api_total"] == 3
    assert d["all_time"]["api_success"] == 1
    assert d["all_time"]["api_client_errors"] == 1
    assert d["all_time"]["api_server_errors"] == 1
    assert d["all_time"]["models_total"] == 0
    assert d["all_time"]["other_total"] == 0


def test_record_models_request_increments_models_counter(store):
    """/v1/models 元信息请求只增加 models 类计数器。"""
    store.record_request(_make_rec(protocol="meta", path="/v1/models", status=200))
    store.record_request(_make_rec(protocol="meta", path="/health", status=200))
    d = store.dashboard()
    assert d["all_time"]["models_total"] == 2
    assert d["all_time"]["models_success"] == 2
    assert d["all_time"]["api_total"] == 0
    assert d["all_time"]["other_total"] == 0


def test_record_other_request_increments_other_counter(store):
    store.record_request(_make_rec(protocol="other", path="/admin/api/foo", status=200))
    d = store.dashboard()
    assert d["all_time"]["other_total"] == 1
    assert d["all_time"]["api_total"] == 0
    assert d["all_time"]["models_total"] == 0


def test_total_equals_sum_of_categories(store):
    """三类计数器之和必须等于 total（防漏计/重计）。"""
    store.record_request(_make_rec(protocol="openai-chat"))
    store.record_request(_make_rec(protocol="meta", path="/v1/models"))
    store.record_request(_make_rec(protocol="other", path="/admin"))
    store.record_request(_make_rec(protocol="openai-responses"))
    store.record_request(_make_rec(protocol="anthropic"))
    d = store.dashboard()
    all_t = d["all_time"]
    assert all_t["api_total"] + all_t["models_total"] + all_t["other_total"] == all_t["total"]


# === dashboard 5 分钟窗口按类别拆分 ===

def test_recent_5m_split_by_category(store):
    """5 分钟窗口的请求按类别拆分到 api_total/models_total/other_total。"""
    now = time.time()
    store.record_request(_make_rec(protocol="openai-chat", ts=now))
    store.record_request(_make_rec(protocol="openai-chat", ts=now))
    store.record_request(_make_rec(protocol="meta", path="/v1/models", ts=now))
    store.record_request(_make_rec(protocol="other", ts=now))
    store.record_request(_make_rec(protocol="other", ts=now))
    store.record_request(_make_rec(protocol="other", ts=now))
    d = store.dashboard()
    r5 = d["recent_5m"]
    assert r5["api_total"] == 2
    assert r5["models_total"] == 1
    assert r5["other_total"] == 3
    assert r5["total"] == 6  # 三类之和


def test_recent_5m_excludes_old_requests(store):
    """超过 5 分钟的请求不计入 recent_5m 拆分。"""
    now = time.time()
    old = now - 400  # 6 分钟前
    store.record_request(_make_rec(protocol="openai-chat", ts=old))
    store.record_request(_make_rec(protocol="openai-chat", ts=now))
    d = store.dashboard()
    r5 = d["recent_5m"]
    assert r5["api_total"] == 1  # 只算最新的
    # 但 all_time 仍累计所有
    assert d["all_time"]["api_total"] == 2


def test_category_counters_independent_per_status(store):
    """各类别成功/4xx/5xx 计数独立，互不影响。"""
    store.record_request(_make_rec(protocol="openai-chat", status=200))
    store.record_request(_make_rec(protocol="openai-chat", status=200))
    store.record_request(_make_rec(protocol="openai-chat", status=400))
    store.record_request(_make_rec(protocol="meta", path="/v1/models", status=500))
    store.record_request(_make_rec(protocol="other", status=200))
    d = store.dashboard()
    a = d["all_time"]
    assert a["api_total"] == 3
    assert a["api_success"] == 2
    assert a["api_client_errors"] == 1
    assert a["api_server_errors"] == 0
    assert a["models_total"] == 1
    assert a["models_success"] == 0
    # _total_errors 是全局 5xx 计数，应包含 models 的 1 次
    assert a["server_errors"] == 1


def test_backward_compatible_total_field(store):
    """原 total/success/client_errors/server_errors 字段保持不变（向后兼容）。"""
    store.record_request(_make_rec(protocol="openai-chat", status=200))
    store.record_request(_make_rec(protocol="openai-chat", status=400))
    store.record_request(_make_rec(protocol="meta", path="/v1/models", status=500))
    d = store.dashboard()
    a = d["all_time"]
    # 旧字段
    assert a["total"] == 3
    assert a["success"] == 1
    assert a["client_errors"] == 1
    assert a["server_errors"] == 1
    # 新字段也存在
    assert "api_total" in a
    assert "models_total" in a
    assert "other_total" in a
