"""v21 P4 修复验证：Python 代码缩进规范化（安全版本）。

v21 报告 P4：GLM 偶尔输出 1 空格缩进（而非 4 空格），导致 Python IndentationError。
v21 P4 修复：新增 _normalize_python_indentation 函数，只对 .py 文件做缩进修复。

修复策略（保守）：
- 只修复"行首恰好 1 个空格"的缩进为 4 个空格
- 不修改 2/3/4/8 等其他缩进（避免破坏正确的缩进）
- 不修改空行和注释行
- 不修改行内空格
- 只对 .py 文件生效（.txt/.json/.md 等不受影响）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.services.translator import _shell_write_to_python, _normalize_python_indentation


# === _normalize_python_indentation 单元测试 ===

def test_1_space_indent_normalized_to_4_spaces():
    """1 空格缩进应被规范化为 4 空格。"""
    content = "for i in range(10):\n print(i)\n print(i*2)"
    result = _normalize_python_indentation(content)
    assert "    print(i)" in result
    assert "    print(i*2)" in result
    # 不应该有行首 1 空格缩进（检查每行）
    for line in result.split('\n'):
        if line and not line.isspace():
            # 行首不应恰好 1 个空格
            assert not (line.startswith(' ') and not line.startswith('  ')), \
                f"行首仍有 1 空格缩进: {line!r}"


def test_4_space_indent_unchanged():
    """4 空格缩进应保持不变（向后兼容）。"""
    content = "def hello():\n    print('hi')\n    return True"
    result = _normalize_python_indentation(content)
    assert result == content, f"4 空格缩进不应被修改，实际: {result!r}"


def test_2_space_indent_unchanged():
    """2 空格缩进应保持不变（避免破坏正确的 2 空格缩进）。"""
    content = "def hello():\n  print('hi')\n  return True"
    result = _normalize_python_indentation(content)
    assert result == content, f"2 空格缩进不应被修改，实际: {result!r}"


def test_8_space_indent_unchanged():
    """8 空格缩进（嵌套块）应保持不变。"""
    content = "def outer():\n    for i in range(3):\n        print(i)"
    result = _normalize_python_indentation(content)
    assert result == content


def test_empty_lines_preserved():
    """空行应保持不变。"""
    content = "line1\n\nline2\n   \nline3"
    result = _normalize_python_indentation(content)
    # 空行和纯空白行保持原样
    assert "\n\n" in result
    assert "   \n" in result


def test_no_indent_lines_unchanged():
    """无缩进的行（顶层语句）应保持不变。"""
    content = "import os\nimport sys\nprint('hello')"
    result = _normalize_python_indentation(content)
    assert result == content


def test_single_line_content_unchanged():
    """单行内容（无换行符）应保持不变。"""
    content = "print('hello')"
    result = _normalize_python_indentation(content)
    assert result == content


def test_empty_content_unchanged():
    """空内容应保持不变。"""
    assert _normalize_python_indentation("") == ""
    assert _normalize_python_indentation(None) is None


# === 端到端测试：.py 文件缩进修复 ===

def test_py_file_1_space_indent_fixed_e2e(tmp_path):
    """端到端：.py 文件 1 空格缩进应被修复为 4 空格。

    v21 task2 失败场景：GLM 输出 1 空格缩进的 fib.py，IndentationError。
    v21 P4 修复后应自动规范化为 4 空格缩进。
    """
    cmd = "cat > fib.py << 'EOF'\na, b = 0, 1\nfor i in range(20):\n print(a)\n a, b = b, a + b\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    fib_file = tmp_path / "fib.py"
    assert fib_file.exists()
    content = fib_file.read_text()

    # 1 空格缩进应被修复为 4 空格
    assert "    print(a)" in content, f"1 空格应被修复为 4 空格，实际: {content!r}"
    assert "    a, b = b, a + b" in content
    # 检查每行：行首不应恰好 1 个空格
    for line in content.split('\n'):
        if line and not line.isspace():
            assert not (line.startswith(' ') and not line.startswith('  ')), \
                f"行首仍有 1 空格缩进: {line!r}"

    # Python 语法应正确
    import ast
    ast.parse(content)


def test_py_file_4_space_indent_preserved_e2e(tmp_path):
    """端到端：.py 文件 4 空格缩进应保持不变（向后兼容）。"""
    cmd = "cat > hello.py << 'EOF'\ndef hello():\n    print('hi')\n    return True\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    hello_file = tmp_path / "hello.py"
    content = hello_file.read_text()
    # 4 空格缩进保持不变
    assert "    print('hi')" in content
    assert "    return True" in content


def test_txt_file_1_space_indent_not_modified(tmp_path):
    """端到端：.txt 文件 1 空格缩进应保持不变（安全，只改 .py）。

    风险控制：不修改 .txt/.json/.md 等非 Python 文件。
    """
    cmd = "cat > notes.txt << 'EOF'\n line1\n line2\n line3\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    notes_file = tmp_path / "notes.txt"
    content = notes_file.read_text()
    # .txt 文件的 1 空格缩进应保持不变（不规范化）
    assert " line1" in content, f".txt 文件 1 空格应保留，实际: {content!r}"
    assert " line2" in content
    assert " line3" in content


def test_json_file_indent_not_modified(tmp_path):
    """端到端：.json 文件缩进应保持不变。"""
    cmd = 'cat > data.json << \'EOF\'\n[\n {"name": "Alice"}\n]\nEOF'
    result = _shell_write_to_python(cmd)
    assert result is not None

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    json_file = tmp_path / "data.json"
    content = json_file.read_text()
    # JSON 缩进应保持不变
    assert ' {"name": "Alice"}' in content


def test_py_file_with_mixed_indent_levels(tmp_path):
    """端到端：.py 文件混合缩进（1 空格 + 4 空格）应只修复 1 空格部分。"""
    cmd = "cat > mixed.py << 'EOF'\ndef hello():\n    for i in range(3):\n print(i)\n        print('done')\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None

    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    mixed_file = tmp_path / "mixed.py"
    content = mixed_file.read_text()
    # 1 空格缩进 'print(i)' 应被修复为 4 空格
    # 但 '        print(\'done\')' 8 空格缩进应保持不变
    # 注意：修复后 '    print(i)' 与 '        print(\'done\')' 缩进不一致，
    # 但这是 GLM 输出的原始问题，我们只修复 1 空格 → 4 空格
    assert "    print(i)" in content or "        print(i)" in content  # 至少不是 1 空格
    assert "        print('done')" in content  # 8 空格保持不变
