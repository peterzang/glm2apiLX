"""v16 修复验证：单行 heredoc 格式（GLM 实际输出）+ append 模式 + 末尾换行。

v16 报告指出：v15 修复了多行 heredoc，但 GLM 实际生成的命令是**单行格式**
（\n 被压成空格）：cat > file << 'EOF' content EOF

v15 的正则要求 \n 分隔，所以 GLM 实际输出全部检测失败，导致 task6/task7 0 字节。

v16 修复：
1. 新增 single_line_pattern 支持单行 heredoc 格式
2. 支持 cat >> file 追加模式
3. 多行 heredoc 内容默认追加末尾换行（与 shell 行为一致）
4. filepath 安全字符集白名单（[A-Za-z0-9_./-+]+）
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _heredoc_to_python_write


# === 单行格式测试（v16 新增）===

def test_heredoc_single_line_single_file():
    """单行 heredoc 单文件应正确转换（GLM 实际输出格式）。

    GLM 把 cat > file << 'EOF'\\ncontent\\nEOF 压成单行：
    cat > file << 'EOF' content EOF
    """
    cmd = "cat > hello.py << 'EOF' print('hello') EOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None, "单行 heredoc 应被检测"
    assert result[0] == "python3"
    assert "'hello.py'" in result[2]
    assert "b64decode" in result[2]


def test_heredoc_single_line_multi_file():
    """单行多文件 heredoc 应正确转换（v16 task6 真实场景）。

    GLM 输出：mkdir -p p && cat > p/a.txt << 'EOF' hello EOF cat > p/b.txt << 'EOF' world EOF
    期望：两个文件都创建
    """
    cmd = "mkdir -p p && cat > p/a.txt << 'EOF' hello EOF cat > p/b.txt << 'EOF' world EOF echo done"
    result = _heredoc_to_python_write(cmd)
    assert result is not None, "单行多文件 heredoc 应被检测"
    assert "'p/a.txt'" in result[2], f"缺少 p/a.txt: {result[2]}"
    assert "'p/b.txt'" in result[2], f"缺少 p/b.txt: {result[2]}"


def test_heredoc_single_line_e2e(tmp_path):
    """端到端：执行单行多文件 heredoc，两个文件都应被创建。"""
    cmd = "mkdir -p p && cat > p/a.txt << 'EOF' hello EOF cat > p/b.txt << 'EOF' world EOF echo done"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    a_file = tmp_path / "p" / "a.txt"
    b_file = tmp_path / "p" / "b.txt"
    assert a_file.exists(), "p/a.txt 未创建"
    assert b_file.exists(), "p/b.txt 未创建"
    assert a_file.read_text() == "hello"
    assert b_file.read_text() == "world"


def test_heredoc_single_line_task6_repro(tmp_path):
    """端到端：v16 报告 task6 完整 GLM 输出（单行多文件）。

    这是 v16 报告指出的真实 bug 场景。
    """
    cmd = ("mkdir -p flask_project && "
           "cat > flask_project/requirements.txt << 'EOF' Flask==2.3.3 EOF "
           "cat > flask_project/app.py << 'EOF' "
           "from flask import Flask app = Flask(__name__) "
           "@app.route('/') def home(): return 'Hello, Flask!' "
           "if __name__ == '__main__': app.run(debug=True) EOF "
           "echo Done")
    result = _heredoc_to_python_write(cmd)
    assert result is not None, "task6 单行多文件应被检测"

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    req_file = tmp_path / "flask_project" / "requirements.txt"
    app_file = tmp_path / "flask_project" / "app.py"
    assert req_file.exists(), "requirements.txt 未创建"
    assert app_file.exists(), "app.py 未创建"
    assert req_file.read_text() == "Flask==2.3.3"
    app_content = app_file.read_text()
    assert "from flask import Flask" in app_content
    assert "app.run(debug=True)" in app_content


def test_heredoc_single_line_via_sanitize():
    """通过 sanitize_tool_call_payload 验证单行 heredoc 端到端。"""
    cmd_str = "cat > a.txt << 'EOF' hello EOF cat > b.txt << 'EOF' world EOF"
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", cmd_str]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", "应转为 python3"
    assert "'a.txt'" in cmd[2]
    assert "'b.txt'" in cmd[2]


# === append 模式测试（v16 新增）===

def test_heredoc_append_mode_multi_line():
    """cat >> file 追加模式应使用 open(..., 'a') 而非 open(..., 'w')。"""
    cmd = "cat > log.txt << 'EOF'\nline1\nEOF\ncat >> log.txt << 'EOF'\nline2\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    # 第一次写用 'w' 模式，第二次用 'a' 模式
    assert "open(_p,'w')" in result[2], "第一次写应该用 'w' 模式"
    assert "open(_p,'a')" in result[2], "追加写应该用 'a' 模式"


def test_heredoc_append_mode_e2e(tmp_path):
    """端到端：append 模式应保留前次内容。"""
    cmd = "cat > log.txt << 'EOF'\nline1\nEOF\ncat >> log.txt << 'EOF'\nline2\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    log_file = tmp_path / "log.txt"
    assert log_file.exists()
    # shell 行为：line1\n + line2\n = "line1\nline2\n"
    assert log_file.read_text() == "line1\nline2\n"


# === 末尾换行行为测试（v16 修复）===

def test_heredoc_multiline_adds_trailing_newline(tmp_path):
    """多行 heredoc 内容默认追加末尾换行（与 shell 行为一致）。

    shell 行为：cat > file << EOF\\nline1\\nEOF → 写入 "line1\\n"
    v16 修复前：写入 "line1"（少一个换行，导致 append 模式内容粘连）
    v16 修复后：写入 "line1\\n"（与 shell 一致）
    """
    cmd = "cat > a.txt << 'EOF'\nhello\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "a.txt"
    assert target.exists()
    # 应该有末尾换行
    assert target.read_text() == "hello\n", f"应有末尾换行，实际: {target.read_text()!r}"


def test_heredoc_single_line_no_trailing_newline(tmp_path):
    """单行 heredoc 内容不追加末尾换行（因为原本就没换行）。

    单行格式：cat > file << 'EOF' content EOF
    content 是 "content"（无换行），保持原样
    """
    cmd = "cat > a.txt << 'EOF' hello EOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "a.txt"
    assert target.exists()
    # 不应该有末尾换行（单行格式保持原样）
    assert target.read_text() == "hello", f"不应有末尾换行，实际: {target.read_text()!r}"


# === filepath 安全白名单测试 ===

def test_heredoc_filepath_with_subdir_safe():
    """filepath 含子目录路径应允许（codex 常见）。"""
    cmd = "cat > src/app/main.py << 'EOF'\nprint('main')\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "'src/app/main.py'" in result[2]


def test_heredoc_filepath_with_dangerous_chars_blocked():
    """filepath 含危险字符（; | & $ 等）应被拒绝。"""
    # 用单行格式测试，因为多行格式的正则用 \S+ 也能匹配到分号
    cmd = "cat > file;rm -rf / << 'EOF' content EOF"
    result = _heredoc_to_python_write(cmd)
    # 危险字符应被安全白名单拒绝
    assert result is None, f"危险 filepath 不应被转换，实际: {result}"


def test_heredoc_filepath_with_plus_sign_safe():
    """filepath 含 + 应允许（如 C++ 文件名 main.c++）。"""
    cmd = "cat > main.c++ << 'EOF'\nint main() {}\nEOF"
    result = _heredoc_to_python_write(cmd)
    assert result is not None
    assert "'main.c++'" in result[2]
