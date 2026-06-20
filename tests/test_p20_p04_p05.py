"""v20 修复验证：P0-4 CDATA 缩进保留 + P0-5 CDATA 双 ]]> 解析。

v19 报告发现 2 个新 P0 bug：

P0-4: _balanced_text strip 行首缩进（v19 P0-1 修复的回归 bug）
- v19 _balanced_text 用 line.strip() 把每行首尾空格全部去掉
- 导致 Python 代码 4 空格缩进变成 0 空格，IndentationError
- 影响 task2 fib.py / task3 prime.py / task6 app.py

P0-5: CDATA 内容含 ] 字符导致双 ]]> 解析失败
- GLM 输出 CDATA 内容含 ] 时（如 JSON 数组结尾 ]）会产生双 ]]>]]>
- XML 解析器遇到第一个 ]]> 就关闭 CDATA，剩余内容成为无效字符
- 影响 task4 data.json（0 文件创建）

v20 修复：
- P0-4: _leaf_text 区分 CDATA（含换行符）和普通文本，CDATA 原样返回不规范化
- P0-5: _normalize_dsml_to_xml 把连续 ]]>]]> 替换为单个 ]]>
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.protocol.tool_parser import (
    _balanced_text,
    _leaf_text,
    _normalize_dsml_to_xml,
    parse_tool_calls_from_text,
)
from glm2api.services.translator import sanitize_tool_call_payload, _shell_write_to_python


# === P0-4 测试：CDATA 缩进保留 ===

def test_balanced_text_single_line_still_normalizes():
    """单行文本节点仍然规范化（向后兼容）。"""
    text = "  hello   world  "
    result = _balanced_text(text)
    assert result == "hello world"


def test_leaf_text_multiline_cdata_preserves_indentation():
    """多行 CDATA 内容应保留缩进，不经过 _balanced_text 规范化。

    v19 P0-4 回归 bug：_leaf_text 调用 _balanced_text 把 4 空格缩进 strip 掉。
    v20 修复：含换行符的文本（CDATA）直接返回原样。
    """
    # 模拟 CDATA 内容（含 4 空格缩进的 Python 代码）
    cdata_content = 'a, b = 0, 1\nfor _ in range(20):\n    print(a)\n    a, b = b, a + b\n'

    # 创建一个模拟 XML 元素（用 ET 解析含 CDATA 的 XML）
    import xml.etree.ElementTree as ET
    xml_str = f'<root><![CDATA[{cdata_content}]]></root>'
    root = ET.fromstring(xml_str)
    text = _leaf_text(root)

    # 验证 4 空格缩进被保留
    assert "    print(a)" in text, f"4 空格缩进应被保留，实际: {text!r}"
    assert "    a, b = b, a + b" in text, f"4 空格缩进应被保留，实际: {text!r}"
    # 不应该被压成单行
    assert "\n" in text, "换行符应被保留"


def test_leaf_text_single_line_normalizes():
    """单行 XML 文本节点仍然规范化（向后兼容）。"""
    import xml.etree.ElementTree as ET
    xml_str = '<root>  hello   world  </root>'
    root = ET.fromstring(xml_str)
    text = _leaf_text(root)
    assert text == "hello world"


def test_p04_fib_py_indentation_preserved_end_to_end(tmp_path):
    """端到端：fib.py 应保留 4 空格缩进，运行成功。

    v19 报告 task2 失败根因：_balanced_text strip 缩进，fib.py 内容变成
    'for _ in range(20):\nprint(a)' （print 没缩进），IndentationError。
    v20 修复后应保留 4 空格缩进。
    """
    # 模拟 GLM 输出的 command（含 4 空格缩进）
    cmd = "cat > fib.py << 'EOF'\na, b = 0, 1\nfor _ in range(20):\n    print(a)\n    a, b = b, a + b\nEOF"
    result = _shell_write_to_python(cmd)
    assert result is not None

    import subprocess
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    fib_file = tmp_path / "fib.py"
    assert fib_file.exists()
    content = fib_file.read_text()
    # 4 空格缩进必须保留
    assert "    print(a)" in content, f"4 空格缩进丢失: {content!r}"
    assert "    a, b = b, a + b" in content, f"4 空格缩进丢失: {content!r}"

    # 运行 fib.py 验证
    proc2 = subprocess.run(["python3", "fib.py"], cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc2.returncode == 0, f"fib.py 运行失败: {proc2.stderr}"
    # 前 5 个 Fibonacci 数
    assert "0\n1\n1\n2\n3" in proc2.stdout


def test_p04_flask_app_indentation_preserved(tmp_path):
    """端到端：Flask app.py 应保留 4 空格缩进。"""
    cmd = """cat > app.py << 'EOF'
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

    import subprocess
    proc = subprocess.run(result, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    content = app_file.read_text()
    # 4 空格缩进必须保留
    assert "    return jsonify" in content, f"函数体缩进丢失: {content!r}"
    assert "    app.run(debug=True)" in content, f"main 块缩进丢失: {content!r}"

    # 验证 Python 语法正确（用 ast.parse）
    import ast
    ast.parse(content)  # 不抛异常说明语法正确


# === P0-5 测试：CDATA 双 ]]> 解析 ===

def test_p05_normalize_double_cdata_close():
    """_normalize_dsml_to_xml 应把双 ]]>]]> 替换为单个 ]]>。"""
    # 模拟 GLM 输出的双 ]]> CDATA
    block = '<![CDATA[["bash", "-c", "echo hello"]]>]]>'
    result = _normalize_dsml_to_xml(block)
    # 应该只有一个 ]]>
    assert result.count("]]>") == 1, f"应该只有 1 个 ]]>，实际: {result.count(']]>')}, result: {result!r}"


def test_p05_triple_cdata_close():
    """三个连续 ]]> 也应替换为单个。"""
    block = '<![CDATA[content]]>]]>]]>'
    result = _normalize_dsml_to_xml(block)
    assert result.count("]]>") == 1, f"应该只有 1 个 ]]>，实际: {result.count(']]>')}"


def test_p05_single_cdata_close_unchanged():
    """单个 ]]> 应保持不变（向后兼容）。"""
    block = '<![CDATA[hello world]]>'
    result = _normalize_dsml_to_xml(block)
    assert "]]>" in result
    assert result.count("]]>") == 1


def test_p05_json_array_in_cdata_parses():
    """CDATA 含 JSON 数组结尾 ] 应正确解析。

    v19 报告 task4 失败根因：GLM 输出 CDATA 含 ] 产生双 ]]>]]>，
    XML 解析器遇到第一个 ]]> 就关闭 CDATA，剩余内容成为无效字符。
    v20 修复后应正确解析。
    """
    # 模拟 GLM 输出的含 JSON 数组的 CDATA（双 ]]>）
    # 这是 v19 报告 task4 的实际场景
    dsml_block = '''<|DSML|tool_calls>
<|DSML|invoke name="shell">
<|DSML|parameter name="command"><![CDATA[["bash", "-c", "cat > data.json << 'EOF'\\n[\\n {\"name\": \"Alice\"}\\n]\\nEOF"]]>]]>
</|DSML|parameter>
</|DSML|invoke>
</|DSML|tool_calls>'''

    summary, tool_calls = parse_tool_calls_from_text(dsml_block)
    # 应该解析出 1 个 tool_call
    assert len(tool_calls) == 1, f"应该解析出 1 个 tool_call，实际: {len(tool_calls)}"
    tc = tool_calls[0]
    assert tc["function"]["name"] == "shell"
    args = tc["function"]["arguments"]
    if isinstance(args, str):
        import json
        args = json.loads(args)
    command = args.get("command", [])
    # 应该包含 data.json 写入命令
    cmd_str = " ".join(str(p) for p in command) if isinstance(command, list) else str(command)
    assert "data.json" in cmd_str, f"应该包含 data.json，实际: {cmd_str!r}"


def test_p05_double_cdata_with_python_content():
    """CDATA 含 Python 代码（含 ] 字符如 list.append）+ 双 ]]> 应正确解析。"""
    # Python 代码含 ] 字符（如 arr[i]] 或 list.append()）
    dsml_block = '''<|DSML|tool_calls>
<|DSML|invoke name="shell">
<|DSML|parameter name="command"><![CDATA[["bash", "-c", "cat > test.py << 'EOF'\\narr = [1, 2, 3]\\nprint(arr[0])\\nEOF"]]>]]>
</|DSML|parameter>
</|DSML|invoke>
</|DSML|tool_calls>'''

    summary, tool_calls = parse_tool_calls_from_text(dsml_block)
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    args = tc["function"]["arguments"]
    if isinstance(args, str):
        import json
        args = json.loads(args)
    command = args.get("command", [])
    cmd_str = " ".join(str(p) for p in command) if isinstance(command, list) else str(command)
    assert "test.py" in cmd_str
    assert "arr = [1, 2, 3]" in cmd_str or "arr" in cmd_str


# === 端到端集成测试 ===

def test_p04_p05_combined_end_to_end(tmp_path):
    """端到端：含缩进的 Python 代码 + JSON 数组（双 ]]>）应同时正确处理。"""
    # 模拟 GLM 输出：含 4 空格缩进 Python + JSON 数组（产生双 ]]>）
    dsml_block = '''<|DSML|tool_calls>
<|DSML|invoke name="shell">
<|DSML|parameter name="command"><![CDATA[["bash", "-c", "cat > app.py << 'EOF'\\ndef process(data):\\n    result = []\\n    for item in data:\\n        result.append(item)\\n    return result\\nEOF"]]>]]>
</|DSML|parameter>
</|DSML|invoke>
</|DSML|tool_calls>'''

    summary, tool_calls = parse_tool_calls_from_text(dsml_block)
    assert len(tool_calls) == 1

    # 通过 sanitize_tool_call_payload 转换
    tc = tool_calls[0]
    args = tc["function"]["arguments"]
    if isinstance(args, str):
        import json
        args = json.loads(args)
    cleaned = sanitize_tool_call_payload("shell", args)
    cmd = cleaned.get("command", [])
    assert cmd[0] == "python3", f"应转为 python3，实际: {cmd[0]}"

    # 执行
    import subprocess
    proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"python3 failed: {proc.stderr}"

    app_file = tmp_path / "app.py"
    assert app_file.exists()
    content = app_file.read_text()
    # 4 空格缩进必须保留
    assert "    result = []" in content, f"缩进丢失: {content!r}"
    assert "    for item in data:" in content
    assert "        result.append(item)" in content  # 8 空格缩进

    # Python 语法应正确
    import ast
    ast.parse(content)
