"""v17 修复验证：通用 shell 文件写入命令转 python3 -c 写入。

v16 修复了 heredoc 单行/多行/append 模式，但 GLM 还会用 echo/printf/tee 写文件：
  echo "content" > file
  echo "content" >> file
  printf "fmt" > file
  echo "content" | tee file
  echo "content" | tee -a file

v17 新增 _shell_write_to_python() 统一处理所有写入方式：
  - 优先调用 _heredoc_to_python_write 处理 heredoc（已稳定）
  - 新增 echo/printf/tee 处理逻辑
  - 用 base64 编码 content 避免转义问题
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _shell_write_to_python


# === echo > file 测试 ===

def test_echo_double_quote_write():
    """echo "content" > file 应转为 python3 -c 写入。"""
    cmd = 'echo "print(\'hello\')" > hello.py'
    result = _shell_write_to_python(cmd)
    assert result is not None, "echo > file 应被检测"
    assert result[0] == "python3"
    assert "'hello.py'" in result[2]
    assert "open(_p,'w')" in result[2]


def test_echo_single_quote_write():
    """echo 'content' > file 应转为 python3 -c 写入。"""
    cmd = "echo 'hello world' > notes.txt"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'notes.txt'" in result[2]


def test_echo_append_mode():
    """echo "content" >> file 应使用 open(..., 'a') 模式。"""
    cmd = 'echo "line2" >> log.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "open(_p,'a')" in result[2], "追加模式应使用 'a'"


def test_echo_e2e_creates_file(tmp_path):
    """端到端：echo > file 应正确创建文件。"""
    cmd = 'echo "hello world" > greeting.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "greeting.txt"
    assert target.exists()
    # echo 默认会追加换行符
    assert target.read_text() == "hello world"


def test_echo_append_e2e(tmp_path):
    """端到端：echo >> file 应追加内容。"""
    # 第一次写
    cmd1 = 'echo "line1" > log.txt'
    result1 = _shell_write_to_python(cmd1)
    subprocess.run(result1, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)

    # 第二次追加
    cmd2 = 'echo "line2" >> log.txt'
    result2 = _shell_write_to_python(cmd2)
    subprocess.run(result2, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)

    target = tmp_path / "log.txt"
    content = target.read_text()
    assert "line1" in content
    assert "line2" in content


# === printf > file 测试 ===

def test_printf_write():
    """printf "fmt" > file 应转为 python3 -c 写入。"""
    cmd = 'printf "host: localhost\\n" > config.yaml'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'config.yaml'" in result[2]


def test_printf_e2e_creates_file(tmp_path):
    """端到端：printf > file 应正确创建文件。"""
    cmd = 'printf "hello" > test.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "test.txt"
    assert target.exists()
    # printf 不像 echo 那样自动加换行
    assert target.read_text() == "hello"


# === tee 测试 ===

def test_tee_write():
    """echo "content" | tee file 应转为 python3 -c 写入。"""
    cmd = 'echo "meeting at 3pm" | tee notes.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'notes.txt'" in result[2]
    assert "open(_p,'w')" in result[2]


def test_tee_append_mode():
    """echo "content" | tee -a file 应使用追加模式。"""
    cmd = 'echo "new entry" | tee -a log.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "open(_p,'a')" in result[2], "tee -a 应使用 'a' 模式"


def test_tee_e2e_creates_file(tmp_path):
    """端到端：echo | tee 应正确创建文件。"""
    cmd = 'echo "meeting at 3pm" | tee notes.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "notes.txt"
    assert target.exists()
    assert "meeting at 3pm" in target.read_text()


# === 多写入操作合并测试 ===

def test_multiple_echo_writes_in_one_command():
    """多个 echo > file 在一个命令字符串中应全部转换。"""
    cmd = 'echo "line1" > a.txt && echo "line2" > b.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]
    assert "'b.txt'" in result[2]


def test_mixed_echo_and_append():
    """混合 echo > file 和 echo >> file 应正确区分模式。"""
    cmd = 'echo "header" > log.txt && echo "data" >> log.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "open(_p,'w')" in result[2], "第一次写应该用 'w' 模式"
    assert "open(_p,'a')" in result[2], "追加写应该用 'a' 模式"


# === 兼容性测试（heredoc 仍应工作）===

def test_heredoc_still_works_through_shell_write():
    """通过 _shell_write_to_python 调用 heredoc 仍应正常工作。"""
    cmd = "cat > hello.py << 'EOF'\nprint('hello')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert result[0] == "python3"
    assert "'hello.py'" in result[2]


def test_heredoc_single_line_still_works():
    """单行 heredoc 通过 _shell_write_to_python 仍应正常工作。"""
    cmd = "cat > a.txt << 'EOF' hello EOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]


# === 非写入命令不应转换 ===

def test_non_write_command_not_converted():
    """非写入命令（ls / cd / mkdir 等）不应转换。"""
    assert _shell_write_to_python("ls -la") is None
    assert _shell_write_to_python("cd /tmp") is None
    assert _shell_write_to_python("mkdir -p project") is None
    assert _shell_write_to_python("rm -rf /tmp/test") is None
    assert _shell_write_to_python("python3 hello.py") is None


# === 端到端 sanitize 验证 ===

def test_echo_via_sanitize_tool_call():
    """通过 sanitize_tool_call_payload 验证 echo > file 端到端。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", "echo 'hello world' > greeting.txt"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'greeting.txt'" in cmd[2]


def test_printf_via_sanitize_tool_call():
    """通过 sanitize_tool_call_payload 验证 printf > file 端到端。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", 'printf "host: localhost\\n" > config.yaml']},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3"
    assert "'config.yaml'" in cmd[2]


def test_tee_via_sanitize_tool_call():
    """通过 sanitize_tool_call_payload 验证 tee 端到端。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", 'echo "data" | tee log.txt']},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3"
    assert "'log.txt'" in cmd[2]


# === 内容含特殊字符 ===

def test_echo_with_quotes_in_content(tmp_path):
    """echo 内容含引号应正确处理。"""
    # 用双引号包裹，内部用单引号
    cmd = 'echo "print(\'hello\')" > test.py'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "test.py"
    assert target.exists()
    content = target.read_text()
    assert "print('hello')" in content


def test_echo_with_subdirectory_path(tmp_path):
    """echo > src/app/main.py 应自动创建父目录。"""
    cmd = 'echo "print(\'main\')" > src/app/main.py'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "src" / "app" / "main.py"
    assert target.exists()
    assert "print('main')" in target.read_text()


# === v17 新增：echo 含字面双引号（GLM 没正确转义）===

def test_echo_bare_content_with_literal_double_quotes():
    """echo print("hello") > file 应正确处理（GLM 实际输出格式）。

    GLM 经常生成 echo print("hello") > hello.py，但没有为内容加引号，
    导致 shell 把 print / ( / "hello" / ) 解释为多个参数 + 语法错误。
    我们的转换器应该捕获这种场景并转为 python3 写入。
    """
    cmd = 'echo print("hello") > hello.py'
    result = _shell_write_to_python(cmd)
    assert result is not None, "GLM 实际输出格式应被检测"
    assert result[0] == "python3"
    assert "'hello.py'" in result[2]
    assert "b64decode" in result[2]


def test_echo_bare_content_with_literal_double_quotes_e2e(tmp_path):
    """端到端：echo print("hello") > hello.py 应正确创建文件。"""
    cmd = 'echo print("hello") > hello.py'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "hello.py"
    assert target.exists()
    content = target.read_text()
    # 内容应该是 print("hello")（含字面双引号）
    assert 'print("hello")' in content, f"内容应含字面双引号，实际: {content!r}"


def test_echo_dq_not_duplicated_by_bare():
    """echo "hello" > file 不应被 dq 和 bare 模式重复匹配。

    v17 修复：用 span 重叠检查避免同一字符被多个模式匹配。
    """
    cmd = 'echo "hello" > greeting.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    # 应该只生成一个 open() 语句，不是两个
    open_count = result[2].count("open(_p,")
    assert open_count == 1, f"应只生成 1 个 open()，实际: {open_count}, script: {result[2]}"


def test_mixed_echo_dq_and_bare_in_one_command():
    """一个命令字符串中混合 echo "..." > file 和 echo bare > file 应都处理。"""
    cmd = 'echo "hello" > a.txt && echo print(world) > b.py'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]
    assert "'b.py'" in result[2]
    # 应该有两个 open() 语句
    open_count = result[2].count("open(_p,")
    assert open_count == 2, f"应生成 2 个 open()，实际: {open_count}"


def test_echo_bare_with_parentheses_e2e(tmp_path):
    """端到端：echo print(world) > b.py 应正确创建文件（无字面引号）。"""
    cmd = 'echo print(world) > b.py'
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "b.py"
    assert target.exists()
    content = target.read_text()
    assert "print(world)" in content
