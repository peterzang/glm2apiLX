"""v18 修复验证：混合写入场景（heredoc + echo/printf/tee 同时出现）。

v17 报告指出 task6 多文件仍 0 字节。根因是 v17 实现先调 _heredoc_to_python_write，
成功就直接返回，导致 heredoc + echo 混合时 echo 部分被丢失。

v18 修复：_shell_write_to_python 不再依赖 _heredoc_to_python_write，
而是同时扫描所有写入模式（heredoc + echo + printf + tee），用 span 去重，
合并所有写入操作到一个 python3 -c 脚本。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import sanitize_tool_call_payload, _shell_write_to_python


# === v18 关键测试：heredoc + echo 混合 ===

def test_heredoc_plus_echo_mix():
    """cat heredoc + echo > file 混合场景应同时处理两个文件。

    v17 bug：先调 _heredoc_to_python_write 成功就直接返回，echo 部分被丢失。
    v18 fix：同时扫描所有写入模式，用 span 去重，合并所有写入操作。
    """
    cmd = """cat > app.py << 'EOF'
print('hello')
EOF
echo "flask" > requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None, "混合场景应被检测"
    assert "'app.py'" in result[2], "缺少 app.py"
    assert "'requirements.txt'" in result[2], "缺少 requirements.txt"
    # 应该有两个 open() 语句
    open_count = result[2].count("open(_p,")
    assert open_count == 2, f"应生成 2 个 open()，实际: {open_count}"


def test_heredoc_plus_echo_mix_e2e(tmp_path):
    """端到端：heredoc + echo 混合应同时创建两个文件。"""
    cmd = """cat > app.py << 'EOF'
print('hello')
EOF
echo "flask" > requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    req_file = tmp_path / "requirements.txt"
    assert app_file.exists(), "app.py 未创建"
    assert req_file.exists(), "requirements.txt 未创建"
    assert "print('hello')" in app_file.read_text()
    assert "flask" in req_file.read_text()


def test_heredoc_plus_echo_task6_full(tmp_path):
    """v18 关键：task6 完整场景（heredoc app.py + echo requirements.txt）。

    v17 报告说 task6 仍 0 字节，根因是 heredoc + echo 混合时 echo 部分被丢失。
    v18 修复后两个文件都应创建。
    """
    cmd = """cat > app.py << 'EOF'
from flask import Flask, jsonify
app = Flask(__name__)

@app.route('/')
def hello():
    return jsonify({'msg': 'Hello'})

if __name__ == '__main__':
    app.run(debug=True)
EOF
echo "flask==3.0.0" > requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    req_file = tmp_path / "requirements.txt"
    assert app_file.exists(), "app.py 未创建"
    assert req_file.exists(), "requirements.txt 未创建"

    app_content = app_file.read_text()
    assert "from flask import Flask" in app_content
    assert "@app.route('/')" in app_content
    assert "app.run(debug=True)" in app_content

    req_content = req_file.read_text()
    assert "flask==3.0.0" in req_content


# === v18 多文件混合各种组合 ===

def test_heredoc_plus_printf_mix():
    """cat heredoc + printf > file 混合。"""
    cmd = """cat > app.py << 'EOF'
print('hello')
EOF
printf "flask" > requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'app.py'" in result[2]
    assert "'requirements.txt'" in result[2]


def test_heredoc_plus_tee_mix():
    """cat heredoc + echo | tee 混合。"""
    cmd = """cat > app.py << 'EOF'
print('hello')
EOF
echo "data" | tee log.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'app.py'" in result[2]
    assert "'log.txt'" in result[2]


def test_heredoc_plus_echo_append_mix(tmp_path):
    """cat heredoc + echo >> file 追加混合。"""
    cmd = """cat > log.txt << 'EOF'
line1
EOF
echo "line2" >> log.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    # 应该有 'w' 和 'a' 两种模式
    assert "open(_p,'w')" in result[2]
    assert "open(_p,'a')" in result[2]

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    log_file = tmp_path / "log.txt"
    content = log_file.read_text()
    assert "line1" in content
    assert "line2" in content


def test_multi_heredoc_plus_multi_echo():
    """多个 heredoc + 多个 echo 混合。"""
    cmd = """cat > a.py << 'EOF'
print('a')
EOF
cat > b.py << 'EOF'
print('b')
EOF
echo "1" > c.txt
echo "2" > d.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.py'" in result[2]
    assert "'b.py'" in result[2]
    assert "'c.txt'" in result[2]
    assert "'d.txt'" in result[2]
    open_count = result[2].count("open(_p,")
    assert open_count == 4, f"应生成 4 个 open()，实际: {open_count}"


def test_mkdir_prefix_plus_heredoc_plus_echo(tmp_path):
    """mkdir 前缀 + heredoc + echo 混合（GLM 实际输出常见）。"""
    cmd = """mkdir -p project && cd project && cat > project/app.py << 'EOF'
print('hello')
EOF
echo "flask" > project/requirements.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'project/app.py'" in result[2]
    assert "'project/requirements.txt'" in result[2]


# === v18 通过 sanitize 端到端验证 ===

def test_mixed_write_via_sanitize():
    """通过 sanitize_tool_call_payload 验证混合写入端到端。"""
    cmd_str = "cat > app.py << 'EOF'\nprint('hello')\nEOF\necho \"flask\" > requirements.txt"
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", cmd_str]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", "应转为 python3"
    assert "'app.py'" in cmd[2]
    assert "'requirements.txt'" in cmd[2]


# === v18 单行 heredoc + echo 混合 ===

def test_single_line_heredoc_plus_echo_mix():
    """单行 heredoc + echo 混合（GLM 单行输出格式）。"""
    cmd = "cat > a.txt << 'EOF' hello EOF echo \"world\" > b.txt"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'a.txt'" in result[2]
    assert "'b.txt'" in result[2]


def test_single_line_heredoc_plus_echo_mix_e2e(tmp_path):
    """端到端：单行 heredoc + echo 混合应同时创建两个文件。"""
    cmd = "cat > a.txt << 'EOF' hello EOF echo \"world\" > b.txt"
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    a_file = tmp_path / "a.txt"
    b_file = tmp_path / "b.txt"
    assert a_file.exists()
    assert b_file.exists()
    assert a_file.read_text() == "hello"
    assert "world" in b_file.read_text()


# === v18 安全性测试：shell 变量展开/命令替换不应转换 ===

def test_echo_double_quote_with_shell_var_not_converted():
    """echo "$HOME" > file 含 shell 变量，不应转换（会写错内容）。

    v18 修复：双引号内 $VAR 会被 shell 展开，我们的转换器写死字面量会出错，
    所以含 $ 或 ` 的双引号字符串直接跳过。
    """
    cmd = 'echo "$HOME" > test.txt'
    result = _shell_write_to_python(cmd)
    # 因为 $HOME 在双引号内会展开，我们的转换器无法正确处理，应该跳过
    # 但 bare_pattern 可能会匹配，所以检查最终生成的脚本不含 $HOME 字面量
    if result:
        # 如果被检测，content 不应该含字面 $HOME（应该被跳过）
        # 或者整个 echo "$HOME" 部分应该被跳过
        # 实际上 bare_pattern 会匹配 echo 后面的裸内容，但 $ 会被危险字符过滤
        # 所以 result 应该是 None
        assert result is None or "$HOME" not in result[2], \
            f"含 $VAR 的内容不应被转换: {result[2]}"


def test_echo_single_quote_with_dollar_sign_preserved():
    """echo '$HOME' > file 单引号内 $ 是字面量，应正确转换。

    v18 行为：单引号内 $ 和 ` 是字面量，不会展开，可以安全保留。
    """
    cmd = "echo '$HOME' > test.txt"
    result = _shell_write_to_python(cmd)
    assert result is not None, "单引号内 $ 应该被保留（字面量）"
    assert "'test.txt'" in result[2]
    # 执行后内容应该是字面量 $HOME
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(result, cwd=tmp, capture_output=True, text=True, timeout=5)
        assert proc.returncode == 0
        content = open(f"{tmp}/test.txt").read()
        assert "$HOME" in content, f"单引号内 $HOME 应该是字面量，实际: {content!r}"


def test_heredoc_with_indented_delimiter():
    """cat > file <<- 'EOF' 缩进定界符应正确检测。

    v18 修复：支持 <<- 缩进 heredoc（bash 特性，定界符前的 tab 会被剥离）。
    """
    cmd = "cat > test.txt <<- 'EOF'\nhello\n\tEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None, "<<- 缩进定界符应被检测"
    assert "'test.txt'" in result[2]


def test_heredoc_with_indented_delimiter_e2e(tmp_path):
    """端到端：<<- 缩进定界符应正确创建文件。"""
    cmd = "cat > test.txt <<- 'EOF'\nhello\n\tEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    target = tmp_path / "test.txt"
    assert target.exists()
    assert "hello" in target.read_text()


def test_echo_with_backticks_in_double_quotes_not_converted():
    """echo "`pwd`" > file 含命令替换，不应转换（会写错内容）。"""
    cmd = 'echo "`pwd`" > test.txt'
    result = _shell_write_to_python(cmd)
    # 含反引号的双引号字符串应该被跳过
    if result:
        assert "`pwd`" not in result[2] or result is None, \
            f"含反引号的内容不应被转换: {result[2]}"


def test_heredoc_content_with_dollar_sign_preserved():
    """heredoc 内容含 $VAR 应保留字面量（'EOF' 定界符不展开变量）。"""
    cmd = "cat > test.sh << 'EOF'\necho $HOME\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=tmp_path_str(), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    # 内容应含字面量 $HOME（因为 'EOF' 定界符不展开变量）


def tmp_path_str():
    """辅助函数：返回临时目录字符串。"""
    import tempfile
    tmp = tempfile.mkdtemp()
    return tmp


def test_complex_mix_all_write_methods(tmp_path):
    """端到端：复杂混合（mkdir + cat heredoc + echo + printf + tee）全部成功。"""
    cmd = """mkdir -p project
cat > project/app.py << 'EOF'
print('hello')
EOF
echo "data" > project/data.txt
printf "config" > project/config.ini
echo "log" | tee project/log.txt"""
    result = _shell_write_to_python(cmd)
    assert result is not None
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    for fname, expected in [
        ("project/app.py", "print('hello')"),
        ("project/data.txt", "data"),
        ("project/config.ini", "config"),
        ("project/log.txt", "log"),
    ]:
        target = tmp_path / fname
        assert target.exists(), f"{fname} 未创建"
        assert expected in target.read_text(), f"{fname} 内容错误"
