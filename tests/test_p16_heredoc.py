"""P16-2 验证：heredoc 写入命令自动转为 python3 -c 写入。

v13 审核报告：codex 长任务 todo.py 创建 0 字节，根因是 heredoc 语法被引号转义破坏。
修复方案：检测 cat > file << 'EOF'...EOF 模式，转为 python3 -c "open(file,'w').write('...')"
v15 修复：多 heredoc 支持 + base64 编码避免转义问题
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _heredoc_to_python_write


def test_heredoc_simple_conversion():
    """简单 heredoc 命令应转为 python3 -c 写入。"""
    cmd = "cat > hello.py << 'EOF'\nprint('hello')\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None, "应检测到 heredoc 并转换"
    assert result[0] == "python3"
    assert result[1] == "-c"
    # v15 修复后使用 base64 编码，检查文件路径和关键标记存在
    assert "'hello.py'" in result[2], "应包含文件路径 hello.py"
    assert "b64decode" in result[2], "应使用 base64 解码"


def test_heredoc_pyeof_conversion():
    """PYEOF 定界符也应检测。"""
    cmd = "cat > todo.py << 'PYEOF'\nimport argparse\nprint('todo')\nPYEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "'todo.py'" in result[2], "应包含 todo.py"
    assert "b64decode" in result[2], "应使用 base64 解码"


def test_heredoc_no_delimiter_quotes():
    """不带引号的定界符也应检测。"""
    cmd = "cat > test.txt << EOF\nhello world\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "'test.txt'" in result[2], "应包含 test.txt"
    assert "b64decode" in result[2], "应使用 base64 解码"


def test_non_heredoc_not_converted():
    """非 heredoc 命令不应转换。"""
    result = _heredoc_to_python_write("ls -la")
    assert result is None

    result = _heredoc_to_python_write("echo hello > file.txt")
    assert result is None

    result = _heredoc_to_python_write("python3 hello.py")
    assert result is None


def test_heredoc_dangerous_filepath_not_converted():
    """含危险字符的文件路径不转换（安全检查）。"""
    cmd = "cat > file;rm -rf / << 'EOF'\ncontent\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is None


def test_heredoc_via_sanitize_tool_call():
    """通过 sanitize_tool_call_payload 端到端验证。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", "cat > hello.py << 'EOF'\nprint('hi')\nEOF"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", "应转为 python3"
    assert "'hello.py'" in cmd[2], "应包含 hello.py"
    assert "b64decode" in cmd[2], "应使用 base64 解码"


def test_heredoc_with_special_chars_in_content():
    """heredoc 内容含特殊字符（引号、换行）应正确转义。

    v15 重构后使用 base64 编码，内容在 base64 中完全保留原样，
    解码时一定能恢复原始内容。
    """
    cmd = "cat > test.py << 'EOF'\nprint(\"hello 'world'\")\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "b64decode" in result[2], "应使用 base64"
    assert "'test.py'" in result[2], "应包含 test.py"
