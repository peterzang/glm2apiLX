"""v19 P0-2 修复验证：cat << EOF > file 格式（GLM 实际输出格式）。

v18 审核报告 P0-2：_shell_write_to_python 不支持 'cat << EOF > file' 格式
（heredoc 操作符在前，重定向在后），只支持 'cat > file << EOF' 格式。

GLM 实际输出的是 'cat << EOF > file\\n...\\nEOF' 格式（heredoc 在前），
v18 修复前返回 None，命令直接传给 sh -c 执行，但因为 _balanced_text bug
把 \\n 压成空格，sh 也无法识别 heredoc。

v19 修复：新增 multi_line_v2 和 single_line_v2 正则，支持 heredoc 在前的格式。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _shell_write_to_python


# === v19 P0-2：cat << EOF > file 多行格式（GLM 实际输出）===

def test_heredoc_first_multi_line():
    """cat << 'EOF' > file 多行格式应被检测（GLM 实际输出格式）。

    v18 报告 P0-2：GLM 实际输出 'cat << EOF > file' 格式（heredoc 在前），
    v18 修复前只支持 'cat > file << EOF' 格式（重定向在前），返回 None。
    """
    cmd = "cat << 'EOF' > hello.py\nprint('hello')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None, "cat << 'EOF' > file 格式应被检测"
    assert result[0] == "python3"
    assert "'hello.py'" in result[2]
    assert "b64decode" in result[2]


def test_heredoc_first_multi_line_e2e(tmp_path):
    """端到端：cat << 'EOF' > file 应正确创建文件。"""
    cmd = "cat << 'EOF' > hello.py\nprint('hello')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "hello.py"
    assert target.exists()
    assert "print('hello')" in target.read_text()


def test_heredoc_first_unquoted_delimiter():
    """cat << EOF > file 不带引号定界符也应被检测。"""
    cmd = "cat << EOF > test.txt\nhello world\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'test.txt'" in result[2]


def test_heredoc_first_append_mode():
    """cat << 'EOF' >> file 追加模式（heredoc 在前）。"""
    cmd = "cat << 'EOF' >> log.txt\nline\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "open(_p,'a')" in result[2], "追加模式应使用 'a'"


def test_heredoc_first_append_e2e(tmp_path):
    """端到端：cat << 'EOF' >> file 应追加内容。"""
    # 第一次写
    cmd1 = "cat << 'EOF' > log.txt\nline1\nEOF"
    subprocess.run(_shell_write_to_python(cmd1), cwd=str(tmp_path),
                   capture_output=True, text=True, timeout=10)
    # 追加
    cmd2 = "cat << 'EOF' >> log.txt\nline2\nEOF"
    subprocess.run(_shell_write_to_python(cmd2), cwd=str(tmp_path),
                   capture_output=True, text=True, timeout=10)

    log_file = tmp_path / "log.txt"
    content = log_file.read_text()
    assert "line1" in content
    assert "line2" in content


def test_heredoc_first_task2_fib_repro(tmp_path):
    """v18 报告 task2 完整复现：cat << 'EOF' > fib.py + python3 fib.py。"""
    cmd = """cat << 'EOF' > fib.py
a, b = 0, 1
for _ in range(20):
    print(a)
    a, b = b, a + b
EOF
python3 fib.py"""
    result = _shell_write_to_python(cmd)
    assert result is not None, "task2 fib.py 场景应被检测"
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    fib_file = tmp_path / "fib.py"
    assert fib_file.exists(), "fib.py 未创建"
    content = fib_file.read_text()
    # 内容应该保留换行符（多行 Python 代码）
    assert "a, b = 0, 1" in content
    assert "for _ in range(20):" in content
    assert "print(a)" in content
    assert "a, b = b, a + b" in content


def test_heredoc_first_single_line():
    """cat << 'EOF' content EOF > file 单行格式（heredoc 在前）。"""
    cmd = "cat << 'EOF' hello EOF > a.txt"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]


def test_heredoc_first_mixed_with_echo():
    """cat << 'EOF' > file + echo > file 混合（heredoc 在前 + echo）。"""
    cmd = """cat << 'EOF' > app.py
print('hello')
EOF
echo "flask" > requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'app.py'" in result[2]
    assert "'requirements.txt'" in result[2]


def test_backward_compat_redirect_first():
    """向后兼容：cat > file << 'EOF' 旧格式仍工作。"""
    cmd = "cat > hello.py << 'EOF'\nprint('hello')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'hello.py'" in result[2]


def test_mixed_both_orders():
    """两种顺序混合：cat << EOF > a + cat > b << EOF。"""
    cmd = """cat << 'EOF' > a.py
print('a')
EOF
cat > b.py << 'EOF'
print('b')
EOF"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.py'" in result[2]
    assert "'b.py'" in result[2]
    # 应该有两个 open() 语句
    open_count = result[2].count("open(_p,")
    assert open_count == 2, f"应生成 2 个 open()，实际: {open_count}"


def test_heredoc_first_via_sanitize():
    """通过 sanitize_tool_call_payload 验证 cat << EOF > file 端到端。"""
    cmd_str = "cat << 'EOF' > hello.py\nprint('hi')\nEOF"
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", cmd_str]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'hello.py'" in cmd[2]


def test_heredoc_first_with_python_code(tmp_path):
    """端到端：cat << 'EOF' > file 写入复杂 Python 代码（task6 类场景）。"""
    cmd = """cat << 'EOF' > app.py
from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/')
def hello():
    return jsonify({'msg': 'Hello'})

if __name__ == '__main__':
    app.run(debug=True)
EOF"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    assert app_file.exists()
    content = app_file.read_text()
    # 内容应该保留换行符，是多行 Python 代码
    assert "from flask import Flask" in content
    assert "@app.route('/')" in content
    assert "app.run(debug=True)" in content
    # 应该有多个换行（不是被压成单行）
    assert content.count('\n') >= 5, f"内容应保留换行符，实际只有 {content.count(chr(10))} 个换行"


def test_heredoc_first_subdirectory_path(tmp_path):
    """cat << 'EOF' > src/app/main.py 子目录路径应自动创建父目录。"""
    cmd = "cat << 'EOF' > src/app/main.py\nprint('main')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "src" / "app" / "main.py"
    assert target.exists()
    assert "print('main')" in target.read_text()


# === v19 多命令数组测试（GLM 实际输出格式）===

def test_multi_command_array_via_sanitize():
    """GLM 生成的多命令数组应正确转换。

    v19 修复：sanitize_tool_call_payload 之前要求 command 数组长度 >= 3，
    导致 GLM 生成的 ["cat > app.py << 'EOF'...", "echo flask > requirements.txt"]
    这种长度为 2 的多命令数组被跳过转换。
    v19 修复后：长度 >= 1 就尝试转换，且正确区分 token 数组 vs 完整命令数组。
    """
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["cat > app.py << 'EOF'\nprint('hello')\nEOF", "echo flask > requirements.txt"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'app.py'" in cmd[2], "应包含 app.py"
    assert "'requirements.txt'" in cmd[2], "应包含 requirements.txt"


def test_multi_command_array_e2e(tmp_path):
    """端到端：多命令数组（heredoc + echo）应同时创建两个文件。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": [
            "cat > app.py << 'EOF'\nfrom flask import Flask\napp = Flask(__name__)\nEOF",
            "echo flask > requirements.txt"
        ]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3"
    proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    req_file = tmp_path / "requirements.txt"
    assert app_file.exists(), "app.py 未创建"
    assert req_file.exists(), "requirements.txt 未创建"
    assert "from flask import Flask" in app_file.read_text()
    assert "flask" in req_file.read_text()


def test_sh_c_array_still_works():
    """向后兼容：["sh", "-c", "cat > file << EOF..."] 仍正确处理。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", "cat > hello.py << 'EOF'\nprint('hi')\nEOF"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'hello.py'" in cmd[2]


def test_single_command_array():
    """单个命令的数组也应正确转换。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["echo 'hello' > test.txt"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'test.txt'" in cmd[2]


def test_three_commands_array():
    """三个命令的数组（heredoc + echo + echo）应全部转换。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": [
            "cat > app.py << 'EOF'\nprint('hello')\nEOF",
            "echo flask > requirements.txt",
            "echo config > config.ini",
        ]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3"
    assert "'app.py'" in cmd[2]
    assert "'requirements.txt'" in cmd[2]
    assert "'config.ini'" in cmd[2]
    # 应该有三个 open() 语句
    open_count = cmd[2].count("open(_p,")
    assert open_count == 3, f"应生成 3 个 open()，实际: {open_count}"
