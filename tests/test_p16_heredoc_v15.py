"""v15 修复验证：多 heredoc 文件路径映射 + 端到端写入。

v15 报告 P16-2 task6 bug：cat > requirements.txt + cat > app.py 两个 heredoc 合并到
一个 shell command 字符串时，旧实现只取第一个，导致 app.py 完全丢失。
修复后用 finditer() 收集所有 heredoc，每个生成独立的 open().write() 语句，
合并到一个 python3 -c 脚本中顺序执行。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _heredoc_to_python_write


def test_heredoc_multiple_files():
    """多个 heredoc 在同一命令字符串中应全部转换（v15 task6 bug 修复）。"""
    cmd = """cat > requirements.txt << 'EOF'
flask
EOF
cat > app.py << 'EOF'
from flask import Flask
app = Flask(__name__)
EOF"""
    result = _heredoc_to_python_write(cmd)
    assert result is not None, "应检测到 heredoc 并转换"
    assert result[0] == "python3"
    assert result[1] == "-c"
    # 两个文件路径都应在生成的脚本中
    assert "'requirements.txt'" in result[2], f"缺少 requirements.txt: {result[2]}"
    assert "'app.py'" in result[2], f"缺少 app.py: {result[2]}"


def test_heredoc_three_files():
    """三个连续 heredoc 都应被处理。"""
    cmd = """cat > a.txt << 'EOF'
content A
EOF
cat > b.txt << 'EOF'
content B
EOF
cat > c.txt << 'EOF'
content C
EOF"""
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]
    assert "'b.txt'" in result[2]
    assert "'c.txt'" in result[2]


def test_heredoc_e2e_writes_all_files(tmp_path):
    """端到端：执行生成的 python3 命令，所有文件都应被创建且内容正确。"""
    cmd = """cat > requirements.txt << 'EOF'
flask
EOF
cat > app.py << 'EOF'
from flask import Flask
app = Flask(__name__)

@app.route('/')
def hello():
    return 'Hello'

if __name__ == '__main__':
    app.run(debug=True)
EOF"""
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    # 验证 requirements.txt
    req_file = tmp_path / "requirements.txt"
    assert req_file.exists(), "requirements.txt 未创建"
    assert req_file.read_text() == "flask"

    # 验证 app.py
    app_file = tmp_path / "app.py"
    assert app_file.exists(), "app.py 未创建"
    app_content = app_file.read_text()
    assert "from flask import Flask" in app_content
    assert "@app.route('/')" in app_content
    assert "app.run(debug=True)" in app_content


def test_heredoc_e2e_creates_subdirectory(tmp_path):
    """端到端：子目录路径应自动创建父目录。"""
    cmd = "cat > src/app/main.py << 'EOF'\nprint('main')\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "src" / "app" / "main.py"
    assert target.exists(), "src/app/main.py 未创建"
    assert target.read_text() == "print('main')"


def test_heredoc_e2e_special_chars(tmp_path):
    """端到端：内容含单引号、双引号、反斜杠应正确写入。

    v15 修复后使用 base64 编码，所有特殊字符在 base64 中安全保留，
    解码时一定恢复原始内容。
    """
    # 内容含单引号、双引号、单+双引号组合
    cmd = "cat > test.py << 'EOF'\nprint(\"hello 'world'\")\nmsg = \"it's a 'test'\"\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "test.py"
    assert target.exists()
    content = target.read_text()
    # 验证所有特殊字符都被正确保留
    assert "print(\"hello 'world'\")" in content, f"内容缺失 print 语句: {content!r}"
    assert "msg = \"it's a 'test'\"" in content, f"内容缺失 msg 语句: {content!r}"


def test_heredoc_eof_string_in_content(tmp_path):
    """边界用例：内容本身含 EOF 字样不应误判边界。"""
    cmd = "cat > readme.txt << 'EOF'\nThis file has EOF in it.\nEOF marker here.\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "readme.txt"
    assert target.exists()
    content = target.read_text()
    # 注意：因为正则要求 EOF 独占一行（前后只有空白），所以最后的 EOF 是真正的结束符
    # 内容应该是 "This file has EOF in it.\nEOF marker here."
    assert "This file has EOF in it." in content
    assert "EOF marker here." in content


def test_heredoc_via_sanitize_multi_files():
    """通过 sanitize_tool_call_payload 端到端验证多 heredoc。"""
    cmd_str = "cat > a.txt << 'EOF'\nA\nEOF\ncat > b.txt << 'EOF'\nB\nEOF"
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", cmd_str]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'a.txt'" in cmd[2]
    assert "'b.txt'" in cmd[2]


def test_heredoc_preserves_single_file_behavior():
    """单文件 heredoc 行为应保持兼容（不破坏现有用例）。"""
    cmd = "cat > hello.py << 'EOF'\nprint('hello')\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert result[0] == "python3"
    assert result[1] == "-c"
    assert "'hello.py'" in result[2]
