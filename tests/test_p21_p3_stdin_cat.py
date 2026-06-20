"""v21 P3 修复验证：cat > file - (stdin) 检测 + 转换为空文件创建。

v21 报告 task9 失败根因：GLM 输出 cat >sort.py - （从 stdin 读），
codex_sim 执行时 stdin 关闭，cat 等待 stdin 输入导致 30s timeout。

v21 P3 修复：检测 cat > file - 模式，转换为 python3 -c 创建空文件，
避免 shell 挂起。文件内容为空（GLM 本意是从 stdin 读内容，但 stdin 没有数据）。

测试覆盖：
1. cat > file - 检测 + 空文件创建
2. cat >> file - 追加模式
3. cat > file - && other_command 混合
4. 端到端验证不挂起
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import _shell_write_to_python, sanitize_tool_call_payload


def test_stdin_cat_write_detected():
    """cat > file - 应被检测并转为创建空文件。

    v21 task9 失败根因：GLM 输出 cat >sort.py -，shell 等待 stdin 挂起。
    v21 P3 修复：检测到这种模式时创建空文件避免挂起。
    """
    cmd = "cat > sort.py -"
    result = _shell_write_to_python(cmd)
    assert result is not None, "cat > file - 应被检测"
    assert result[0] == "python3"
    assert "'sort.py'" in result[2]


def test_stdin_cat_append_mode():
    """cat >> file - 追加模式也应被检测。"""
    cmd = "cat >> log.txt -"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'log.txt'" in result[2]
    # 应该用 'a' 模式（追加）
    assert "open(_p,'a')" in result[2]


def test_stdin_cat_with_trailing_command():
    """cat > file - && other_command 混合也应被检测。"""
    cmd = "cat > sort.py - && echo done"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'sort.py'" in result[2]


def test_stdin_cat_e2e_creates_empty_file(tmp_path):
    """端到端：cat > file - 应创建空文件，不挂起。

    v21 task9 失败根因：shell 等待 stdin 30s timeout。
    v21 P3 修复后应立即创建空文件（< 1s）。
    """
    cmd = "cat > sort.py -"
    result = _shell_write_to_python(cmd)
    assert result is not None

    # 执行并计时（应该 < 2s，不能挂起 30s）
    start = time.time()
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=5)
    elapsed = time.time() - start

    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"
    assert elapsed < 2.0, f"应该立即完成（< 2s），实际 {elapsed:.1f}s（可能挂起）"

    # 应该创建空文件
    target = tmp_path / "sort.py"
    assert target.exists(), "sort.py 未创建"
    assert target.read_text() == "", f"应该是空文件，实际: {target.read_text()!r}"


def test_stdin_cat_via_sanitize():
    """通过 sanitize_tool_call_payload 验证端到端。"""
    cleaned = sanitize_tool_call_payload(
        "shell",
        {"command": ["sh", "-c", "cat > sort.py -"]},
    )
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"
    assert "'sort.py'" in cmd[2]


def test_normal_heredoc_not_affected_by_stdin_check():
    """正常 heredoc 不应被 stdin 检测影响（向后兼容）。"""
    cmd = "cat > hello.py << 'EOF'\nprint('hello')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'hello.py'" in result[2]
    # 应该有实际内容（不是空文件）
    assert "b64decode" in result[2]


def test_normal_echo_not_affected_by_stdin_check():
    """正常 echo > file 不应被 stdin 检测影响（向后兼容）。"""
    cmd = 'echo "hello" > test.txt'
    result = _shell_write_to_python(cmd)
    assert result is not None
    assert "'test.txt'" in result[2]


def test_stdin_cat_does_not_hang_e2e(tmp_path):
    """端到端：cat > file - 不应挂起（关键测试）。

    v21 task9 失败：codex_sim 执行 cat >sort.py - 时 stdin 关闭，
    cat 等待 stdin 30s timeout。v21 P3 修复后应立即完成。
    """
    cmd = "cat > output.txt -"
    result = _shell_write_to_python(cmd)
    assert result is not None

    # 关键测试：5s timeout，如果挂起会 timeout
    try:
        proc = subprocess.run(
            result, cwd=str(tmp_path),
            capture_output=True, text=True,
            timeout=5,  # 5s timeout，正常应该 < 1s
            stdin=subprocess.DEVNULL,  # 模拟 codex_sim stdin 关闭
        )
        assert proc.returncode == 0
        assert (tmp_path / "output.txt").exists()
    except subprocess.TimeoutExpired:
        pytest.fail("cat > file - 挂起超过 5s（P3 修复无效）")
