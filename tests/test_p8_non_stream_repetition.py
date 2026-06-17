"""P8 验证：非流式路径描述性文本检测的 admin repetition 统计。

v5 报告指出非流式路径的 non_stream_descriptive 未实测验证。
这个测试 mock 非流式请求触发描述性检测，验证 record_repetition_event 调用路径正确。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import GLMEventAccumulator
from glm2api.admin.store import get_store


def test_non_stream_descriptive_records_repetition_event():
    """非流式 build_response 触发描述性检测时应记录到 admin store。

    构造一个 GLMEventAccumulator，喂描述性文本（"I'll create..." > 500 字符），
    调 build_response() 应抛 RuntimeError，同时 record_repetition_event 被调用。
    """
    store = get_store()
    stats_before = store.get_repetition_stats()
    count_before = stats_before["total_events"]

    # 构造 accumulator
    acc = GLMEventAccumulator(
        model="glm-5.2",
        allowed_tool_names={"shell"},
        debug_enabled=False,
        logger=MagicMock(),
    )

    # 构造描述性文本（> 500 字符，以 "I'll" 开头）
    descriptive_text = (
        "I'll create all three files using the shell tool.\n"
        "The tests directory doesn't exist at the expected location.\n"
        "Let me check and recreate the files properly.\n"
        "I'll start by creating the main todo.py file with argparse.\n"
        "Then I'll create the test file with pytest.\n"
        "Finally I'll create the README.md with usage examples.\n"
        "Let me begin the implementation now.\n"
        "I'll create all three files using the shell tool.\n"
        "The tests directory doesn't exist at the expected location.\n"
        "Let me check and recreate the files properly.\n"
        "I'll start by creating the main todo.py file with argparse.\n"
    )
    assert len(descriptive_text) > 500
    assert descriptive_text.lower().startswith("i'll ")

    # 设置 accumulator 状态模拟已收到完整响应
    acc._cached_full_text = descriptive_text
    acc._render_cache_dirty = False
    acc.last_full_text = descriptive_text

    # build_response 应抛 RuntimeError
    with pytest.raises(RuntimeError, match="descriptive_text_without_tool_call"):
        acc.build_response()

    # 验证 admin store 记录了事件
    stats_after = store.get_repetition_stats()
    count_after = stats_after["total_events"]
    assert count_after > count_before, (
        f"非流式描述性文本事件未记录: before={count_before}, after={count_after}"
    )
    # by_path 应包含 non_stream_descriptive
    assert "non_stream_descriptive" in stats_after["by_path"], (
        f"by_path 未包含 non_stream_descriptive: {stats_after['by_path']}"
    )
    # by_model 应包含 glm-5.2
    assert "glm-5.2" in stats_after["by_model"], (
        f"by_model 未包含 glm-5.2: {stats_after['by_model']}"
    )


def test_non_stream_useful_text_does_not_trigger():
    """非流式路径：有用的长文本不应触发描述性检测。"""
    store = get_store()
    stats_before = store.get_repetition_stats()

    acc = GLMEventAccumulator(
        model="glm-5.2",
        allowed_tool_names={"shell"},
        debug_enabled=False,
        logger=MagicMock(),
    )

    # 构造有用的长文本（不以 "I'll" 开头，含代码块）
    useful_text = (
        "Here is the implementation:\n"
        "```python\n"
        "import argparse\n"
        "import json\n"
        "import os\n"
        "\n"
        "def add_todo(title):\n"
        "    print(f'Added: {title}')\n"
        "```\n"
        "This implementation uses argparse for CLI argument parsing "
        "and JSON for storage. The add subcommand takes a title argument "
        "and saves it to a JSON file. The list subcommand reads and displays all todos. "
        "The complete subcommand marks a todo as done by its ID. "
        "The delete subcommand removes a todo by its ID. "
        "Error handling includes file not found, invalid JSON, and missing arguments. "
        "The storage file defaults to todos.json in the current directory."
    )
    assert len(useful_text) > 500
    assert "```" in useful_text  # 含代码块

    acc._cached_full_text = useful_text
    acc._render_cache_dirty = False
    acc.last_full_text = useful_text

    # build_response 不应抛异常
    result = acc.build_response()
    assert result is not None

    # admin store 不应记录事件
    stats_after = store.get_repetition_stats()
    assert stats_after["total_events"] == stats_before["total_events"]
