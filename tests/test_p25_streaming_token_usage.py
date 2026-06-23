"""Tests for v25: 流式请求 token 用量记录修复。

之前的 bug：4 个流式 handler（OpenAI / Anthropic / Responses v1 / Responses v2 / Legacy）
都没调用 _record_token_usage，导致 stream=true 的请求（绝大多数 SDK 默认行为）
token 用量从不被记录。用户看到"API 调用多次但用量才几十"。

修复：新增 _extract_usage_from_sse_chunks + _record_streaming_token_usage
从 SSE chunks 中提取 usage 并记录到 admin store。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.admin.store import AdminStore
from glm2api.server import _extract_usage_from_sse_chunks


@pytest.fixture
def store() -> AdminStore:
    return AdminStore()


def _make_sse_chunk(usage: dict | None = None, content: str = "hello") -> bytes:
    """构造一个 OpenAI SSE chunk bytes。最后一个 chunk 含 usage 字段。"""
    payload: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1234567890,
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


# === _extract_usage_from_sse_chunks 提取逻辑测试 ===

def test_extract_usage_from_final_chunk():
    """最后一个含 usage 字段的 chunk 应被提取。"""
    chunks = [
        _make_sse_chunk(content="hello"),
        _make_sse_chunk(content=" world"),
        _make_sse_chunk(usage={"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23}),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 15
    assert usage["completion_tokens"] == 8
    assert usage["total_tokens"] == 23


def test_extract_usage_skips_done_marker():
    """data: [DONE] 应被跳过（不是合法 JSON）。"""
    chunks = [
        b"data: [DONE]\n\n",
        _make_sse_chunk(usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}),
        b"data: [DONE]\n\n",
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 3


def test_extract_usage_takes_last_when_multiple():
    """多个含 usage 的 chunk 时，取最后一个（OpenAI 在最后一帧发 usage）。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
        _make_sse_chunk(usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50


def test_extract_usage_returns_none_when_no_usage():
    """没有含 usage 的 chunk 时返回 None。"""
    chunks = [
        _make_sse_chunk(content="hello"),
        _make_sse_chunk(content=" world"),
        b"data: [DONE]\n\n",
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is None


def test_extract_usage_empty_chunks():
    """空 chunks 列表返回 None。"""
    assert _extract_usage_from_sse_chunks([]) is None


def test_extract_usage_handles_invalid_json():
    """含非法 JSON 行的 chunk 不应抛异常，应跳过继续扫描。"""
    chunks = [
        b"data: {invalid json}\n\n",
        _make_sse_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        b"not even a data line\n\n",
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5


def test_extract_usage_handles_zero_values():
    """prompt=0 completion=0 的 usage 也应正常提取（调用方决定是否记录）。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0


def test_extract_usage_accepts_str_chunks():
    """chunks 既可以是 bytes 也可以是 str（防御性）。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}).decode("utf-8"),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 4


def test_extract_usage_ignores_non_data_lines():
    """event:xxx / 注释行等非 data: 行应被忽略。"""
    chunks = [
        b"event: ping\n\n",
        b": keep-alive\n\n",
        _make_sse_chunk(usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 3


def test_extract_usage_handles_missing_total_tokens():
    """usage 字典缺 total_tokens 时应默认为 0，不抛错。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 12, "completion_tokens": 8}),
    ]
    usage = _extract_usage_from_sse_chunks(chunks)
    assert usage is not None
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 8
    assert usage["total_tokens"] == 0


# === 模拟 _record_streaming_token_usage 行为（通过 store 直接验证）===
# 这部分测试验证：调用提取 + 记录后，store 中的 token_totals 反映正确

def _extract_and_record(store: AdminStore, chunks: list[bytes], api_key: str = ""):
    """模拟 handler._record_streaming_token_usage 的核心逻辑。"""
    usage = _extract_usage_from_sse_chunks(chunks)
    if usage is None:
        return
    pt, ct = usage["prompt_tokens"], usage["completion_tokens"]
    if pt <= 0 and ct <= 0:
        return
    store.record_token_usage(pt, ct)
    if api_key:
        store.record_api_key_usage(api_key, success=True, prompt_tokens=pt, completion_tokens=ct)


def test_record_streaming_usage_persists_to_token_totals(store):
    """流式 usage 记录后，token_totals 应反映。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}),
    ]
    _extract_and_record(store, chunks)

    d = store.dashboard()
    assert d["token_totals"]["prompt"] == 50
    assert d["token_totals"]["completion"] == 30
    assert d["token_totals"]["total"] == 80


def test_record_streaming_usage_persists_to_30m_window(store):
    """流式 usage 记录后，token_30m 30 分钟窗口应反映。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}),
    ]
    _extract_and_record(store, chunks)

    d = store.dashboard()
    assert d["token_30m"]["prompt"] == 50
    assert d["token_30m"]["completion"] == 30
    assert d["token_30m"]["total"] == 80


def test_record_streaming_usage_accumulates_multiple_calls(store):
    """多次流式请求的 usage 应累加。"""
    for _ in range(3):
        chunks = [
            _make_sse_chunk(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
        ]
        _extract_and_record(store, chunks)

    d = store.dashboard()
    assert d["token_totals"]["prompt"] == 30  # 10 * 3
    assert d["token_totals"]["completion"] == 15  # 5 * 3
    assert d["token_totals"]["total"] == 45  # 15 * 3


def test_record_streaming_usage_with_api_key_records_per_key(store):
    """含 api_key 时，per-key 用量也应记录。"""
    created = store.create_api_key(name="test-streaming")
    raw_key = created["key"]

    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}),
    ]
    _extract_and_record(store, chunks, api_key=raw_key)

    keys = store.get_api_keys()
    test_key = next((k for k in keys if k.get("name") == "test-streaming"), None)
    assert test_key is not None
    assert test_key["total_requests"] == 1
    assert test_key["prompt_tokens"] == 20
    assert test_key["completion_tokens"] == 12
    assert test_key["total_tokens"] == 32


def test_record_streaming_usage_zero_usage_not_recorded(store):
    """prompt=0 completion=0 的 usage 不应记录（避免污染统计）。"""
    chunks = [
        _make_sse_chunk(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ]
    _extract_and_record(store, chunks)

    d = store.dashboard()
    assert d["token_totals"]["prompt"] == 0
    assert d["token_totals"]["completion"] == 0


def test_record_streaming_usage_no_usage_not_recorded(store):
    """没有 usage 的 chunks 不应记录。"""
    chunks = [
        _make_sse_chunk(content="hello"),
        b"data: [DONE]\n\n",
    ]
    _extract_and_record(store, chunks)

    d = store.dashboard()
    assert d["token_totals"]["prompt"] == 0
    assert d["token_totals"]["completion"] == 0
