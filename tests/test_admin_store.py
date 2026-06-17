"""Tests for admin/store.py statistics logic (v3 audit report suggestion).

Covers:
- record_request: total counter, model counter, success/error split, RPM bucket
- record_token_usage: token totals, 30m bucket
- record_repetition_event / get_repetition_stats
- _get_model_latencies_summary
- session management
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.admin.store import AdminStore, RequestRecord


def _make_rec(
    *,
    status: int = 200,
    model: str = "glm-5.2",
    duration_ms: int = 1000,
    account_index: int = 0,
    protocol: str = "openai-chat",
    path: str = "/v1/chat/completions",
) -> RequestRecord:
    return RequestRecord(
        ts=time.time(),
        method="POST",
        path=path,
        protocol=protocol,
        model=model,
        status=status,
        duration_ms=duration_ms,
        client_ip="127.0.0.1",
        account_index=account_index,
        stream=False,
        error="",
        request_id="req_test",
    )


@pytest.fixture
def store() -> AdminStore:
    return AdminStore()


# === record_request 基础计数 ===

def test_record_request_increments_total(store):
    store.record_request(_make_rec())
    store.record_request(_make_rec())
    assert store.dashboard()["all_time"]["total"] == 2


def test_record_request_success_split(store):
    store.record_request(_make_rec(status=200))
    store.record_request(_make_rec(status=200))
    store.record_request(_make_rec(status=404))
    store.record_request(_make_rec(status=500))
    all_time = store.dashboard()["all_time"]
    assert all_time["success"] == 2
    assert all_time["client_errors"] == 1
    assert all_time["server_errors"] == 1


def test_record_request_model_counter(store):
    store.record_request(_make_rec(model="glm-5.2"))
    store.record_request(_make_rec(model="glm-5.2"))
    store.record_request(_make_rec(model="glm-5.1"))
    top = store.dashboard()["top_models"]
    models = {m["model"]: m["count"] for m in top}
    assert models["glm-5.2"] == 2
    assert models["glm-5.1"] == 1


def test_record_request_updates_rpm(store):
    store.record_request(_make_rec())
    store.record_request(_make_rec())
    d = store.dashboard()
    assert d["rpm"] >= 2  # 同一分钟内 2 个请求
    assert d["peak_rpm"] >= 2


# === record_token_usage ===

def test_record_token_usage_accumulates(store):
    store.record_token_usage(10, 20)
    store.record_token_usage(5, 15)
    totals = store.dashboard()["token_totals"]
    assert totals["prompt"] == 15
    assert totals["completion"] == 35
    assert totals["total"] == 50


def test_record_token_usage_ignores_zero(store):
    store.record_token_usage(0, 0)
    totals = store.dashboard()["token_totals"]
    assert totals["prompt"] == 0
    assert totals["completion"] == 0


def test_record_token_usage_negative_clamped(store):
    store.record_token_usage(-5, -10)
    totals = store.dashboard()["token_totals"]
    assert totals["prompt"] == 0
    assert totals["completion"] == 0


# === record_repetition_event / get_repetition_stats ===

def test_repetition_event_recorded(store):
    store.record_repetition_event("glm-5.2", "stream")
    store.record_repetition_event("glm-5.2", "stream")
    store.record_repetition_event("glm-5.1", "non_stream")
    stats = store.get_repetition_stats()
    assert stats["total_events"] == 3
    assert stats["by_model"]["glm-5.2"] == 2
    assert stats["by_model"]["glm-5.1"] == 1
    assert stats["by_path"]["stream"] == 2
    assert stats["by_path"]["non_stream"] == 1


def test_repetition_stats_recent_24h(store):
    store.record_repetition_event("glm-5.2", "stream")
    stats = store.get_repetition_stats()
    assert stats["recent_24h_count"] == 1


def test_repetition_stats_empty_when_no_events(store):
    stats = store.get_repetition_stats()
    assert stats["total_events"] == 0
    assert stats["recent_24h_count"] == 0
    assert stats["by_model"] == {}
    assert stats["by_path"] == {}


# === 按模型延迟统计 ===

def test_model_latencies_summary(store):
    # 记录同一模型的多次请求延迟
    for ms in [1000, 2000, 3000, 4000, 5000]:
        store.record_request(_make_rec(model="glm-5.2", duration_ms=ms, status=200))
    # 失败请求不统计延迟
    store.record_request(_make_rec(model="glm-5.2", duration_ms=9999, status=500))
    summary = store.dashboard()["model_latencies"]
    assert "glm-5.2" in summary
    lat = summary["glm-5.2"]
    assert lat["count"] == 5  # 5 次成功（不含失败的 9999）
    assert lat["avg_ms"] == 3000.0
    assert lat["p50_ms"] == 3000
    assert lat["p95_ms"] == 5000


def test_model_latencies_multiple_models(store):
    store.record_request(_make_rec(model="glm-5.2-flash", duration_ms=500, status=200))
    store.record_request(_make_rec(model="glm-5.2", duration_ms=3000, status=200))
    summary = store.dashboard()["model_latencies"]
    assert "glm-5.2-flash" in summary
    assert "glm-5.2" in summary
    assert summary["glm-5.2-flash"]["avg_ms"] < summary["glm-5.2"]["avg_ms"]


def test_model_latencies_empty(store):
    summary = store.dashboard()["model_latencies"]
    assert summary == {}


# === session 管理 ===

def test_session_create_and_validate(store):
    token = store.create_session(ttl_seconds=3600)
    assert store.validate_session(token) is True


def test_session_revoke(store):
    token = store.create_session(ttl_seconds=3600)
    store.revoke_session(token)
    assert store.validate_session(token) is False


def test_session_invalid_token(store):
    assert store.validate_session("invalid-token") is False
    assert store.validate_session(None) is False
    assert store.validate_session("") is False
