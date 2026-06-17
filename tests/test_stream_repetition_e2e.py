"""端到端测试：流式路径复读检测（P1-3 验证）。

构造 mock 上游 SSE 返回复读文本，验证 stream_chat_completion 的 buffer + 检测 + error chunk 机制。

测试场景：
  1. 上游返回复读文本（同句重复 10 次）→ 应触发检测，yield error chunk + [DONE]
  2. 上游返回正常长文本 → 不触发，正常输出所有 chunks
  3. 上游返回短文本（< 240 字符）→ 不触发，正常输出
  4. 上游返回 240+ 字符但不复读 → 不触发，正常输出
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.glm_client import GLMWebClient


def _make_sse_event(text_delta: str, status: str = "") -> dict:
    """构造一个 GLM 上游 SSE 事件，模拟 text delta。

    必须包含 logic_id 才能被 GLMEventAccumulator.parts_by_logic_id 接受。
    必须包含 content 数组（含 type=text 的 item）才能被 _render_full_output 提取文本。
    """
    event = {
        "status": status or "processing",
        "conversation_id": "test-conv-001",
        "parts": [
            {
                "logic_id": "logic-1",
                "content": [
                    {"type": "text", "text": text_delta},
                ],
            }
        ],
    }
    return event


def _make_finish_event() -> dict:
    """构造一个 finish 状态的事件。"""
    return {"status": "finish", "parts": []}


class _FakeResponse:
    """模拟 urllib 上游响应，提供 SSE 事件迭代。"""
    def __init__(self, events):
        self._events = list(events)
    def read(self):
        return b""  # 不实际使用
    def close(self):
        pass


def _setup_client() -> GLMWebClient:
    """构造一个绕过 __init__ 的 GLMWebClient 实例。"""
    client = GLMWebClient.__new__(GLMWebClient)
    client.logger = MagicMock()
    client.config = MagicMock()
    client.config.debug_dump_all = False
    client.config.glm_assistant_id = "65940acff94777010aa6b796"
    client.config.glm_image_assistant_id = "65a232c082ff90a2ad2f15e2"
    client.config.glm_image_model_name = "glm-image-1"
    client.config.blocked_tool_names = []
    client.config.glm_delete_conversation = False  # 测试时跳过删除
    # request_queue mock：acquire 立即返回 lease
    client.request_queue = MagicMock()
    fake_lease = MagicMock()
    fake_lease.ticket = 0
    client.request_queue.acquire = MagicMock(return_value=fake_lease)
    # _get_preferred_account_index mock
    client._get_preferred_account_index = MagicMock(return_value=None)
    return client


def _collect_stream_output(generator) -> str:
    """收集 generator yield 的所有字节，拼接成字符串。"""
    chunks = []
    for chunk in generator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8", errors="replace"))
        else:
            chunks.append(str(chunk))
    return "".join(chunks)


def test_stream_repetition_detected_emits_error_chunk():
    """上游返回复读文本 → 应触发检测，yield error chunk + [DONE]，不发 content。"""
    client = _setup_client()
    # 构造复读文本：同句重复 10 次（远超 5 次阈值），每次约 50 字符 = 总 500 字符
    sentence = "I will create the complete CLI todo application now."
    repetition_text = (sentence + " ") * 10  # ~520 字符

    # 模拟上游 SSE：先发 1 个大 chunk（包含完整复读文本），再发 finish
    events = [_make_sse_event(repetition_text), _make_finish_event()]

    with patch.object(client, "_open_chat_stream", return_value=(_FakeResponse(events), "65940acff94777010aa6b796")):
        with patch.object(client, "_iter_sse_events", return_value=iter(events)):
            with patch.object(client, "delete_conversation"):
                generator = client.stream_chat_completion({
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "build todo app"}],
                })
                output = _collect_stream_output(generator)

    # 应包含 error chunk
    assert "repetition_loop_detected" in output, f"未检测到复读 error chunk，output: {output[:500]}"
    # 应包含 [DONE]
    assert "[DONE]" in output, f"未检测到 [DONE]，output: {output[:500]}"
    # 应记录到 admin store
    # （由于 admin store 是真实单例，触发事件会记录在内存里）


def test_stream_normal_text_no_trigger():
    """上游返回正常长文本 → 不触发检测，正常输出所有 content。"""
    client = _setup_client()
    # 构造 240+ 字符的唯一文本（不重复）
    normal_text = (
        "To build a todo CLI app, we need several components. "
        "First, the argument parser should support add, list, complete, and delete subcommands. "
        "Second, the storage layer must persist tasks to a JSON file with proper error handling. "
        "Third, each subcommand needs comprehensive tests covering all branches. "
        "Finally, we should document the usage in README.md."
    )
    assert len(normal_text) > 240

    events = [_make_sse_event(normal_text), _make_finish_event()]

    with patch.object(client, "_open_chat_stream", return_value=(_FakeResponse(events), "65940acff94777010aa6b796")):
        with patch.object(client, "_iter_sse_events", return_value=iter(events)):
            with patch.object(client, "delete_conversation"):
                generator = client.stream_chat_completion({
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "explain how to build a todo app"}],
                })
                output = _collect_stream_output(generator)

    # 不应有 error chunk
    assert "repetition_loop_detected" not in output, f"误判正常文本为复读：{output[:500]}"
    # 应包含正常文本内容
    assert "todo CLI app" in output or "argument parser" in output, f"未输出正常文本：{output[:500]}"


def test_stream_short_text_no_trigger():
    """上游返回短文本（< 240 字符）→ 不触发检测，正常输出。"""
    client = _setup_client()
    short_text = "Hello! How can I help you today?"

    events = [_make_sse_event(short_text), _make_finish_event()]

    with patch.object(client, "_open_chat_stream", return_value=(_FakeResponse(events), "65940acff94777010aa6b796")):
        with patch.object(client, "_iter_sse_events", return_value=iter(events)):
            with patch.object(client, "delete_conversation"):
                generator = client.stream_chat_completion({
                    "model": "glm-5.2-flash",
                    "messages": [{"role": "user", "content": "hi"}],
                })
                output = _collect_stream_output(generator)

    assert "repetition_loop_detected" not in output
    assert "Hello" in output


def test_stream_long_unique_text_no_trigger():
    """上游返回 240+ 字符但不复读 → 不触发，正常输出。"""
    client = _setup_client()
    # 构造 500 字符的唯一文本
    unique_text = (
        "The quick brown fox jumps over the lazy dog. "
        "Pack my box with five dozen liquor jugs. "
        "How vexingly quick daft zebras jump! "
        "Sphinx of black quartz, judge my vow. "
        "The five boxing wizards jump quickly. "
        "Quick zephyrs blow, vexing daft Jim. "
        "Two driven jocks help fax my big quiz. "
        "Crazy Fredrick bought many very exquisite opal jewels."
    )
    assert len(unique_text) > 240

    events = [_make_sse_event(unique_text), _make_finish_event()]

    with patch.object(client, "_open_chat_stream", return_value=(_FakeResponse(events), "65940acff94777010aa6b796")):
        with patch.object(client, "_iter_sse_events", return_value=iter(events)):
            with patch.object(client, "delete_conversation"):
                generator = client.stream_chat_completion({
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "tell me pangrams"}],
                })
                output = _collect_stream_output(generator)

    assert "repetition_loop_detected" not in output
    assert "quick brown fox" in output


def test_stream_repetition_records_to_admin_store():
    """触发复读检测时应记录到 admin store（repetition 统计）。"""
    from glm2api.admin.store import get_store
    store = get_store()
    # 记录触发前的 repetition count
    stats_before = store.get_repetition_stats()
    count_before = stats_before["total_events"]

    client = _setup_client()
    sentence = "I will create the complete CLI todo application now."
    repetition_text = (sentence + " ") * 10
    events = [_make_sse_event(repetition_text), _make_finish_event()]

    with patch.object(client, "_open_chat_stream", return_value=(_FakeResponse(events), "65940acff94777010aa6b796")):
        with patch.object(client, "_iter_sse_events", return_value=iter(events)):
            with patch.object(client, "delete_conversation"):
                generator = client.stream_chat_completion({
                    "model": "glm-5.2",
                    "messages": [{"role": "user", "content": "build todo app"}],
                })
                _ = _collect_stream_output(generator)

    # 验证 repetition count 增加
    stats_after = store.get_repetition_stats()
    count_after = stats_after["total_events"]
    assert count_after > count_before, f"复读事件未记录到 admin store: before={count_before}, after={count_after}"
    # by_path 应包含 stream
    assert "stream" in stats_after["by_path"], f"by_path 未包含 stream: {stats_after['by_path']}"
    # by_model 应包含 glm-5.2
    assert "glm-5.2" in stats_after["by_model"], f"by_model 未包含 glm-5.2: {stats_after['by_model']}"
