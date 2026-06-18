"""v20 P1-5 端到端单元测试：模拟 GLM SSE 输出 → 完整解析链路 → 验证写入文件。

v19 报告指出：单元测试直接调 _shell_write_to_python，输入是测试代码写死的
4 空格缩进字符串，但真实场景中 cmd_str 来自 _balanced_text 处理后的 CDATA 内容。
v19 P0-4 回归 bug（_balanced_text strip 缩进）在单元测试中没暴露，只在端到端测试中才显现。

v20 修复：新增端到端单元测试，模拟完整链路：
  mock GLM SSE 输出（含 DSML CDATA + 4 空格缩进 heredoc）
  → parse_tool_calls_from_text 解析
  → sanitize_tool_call_payload 处理 command 数组
  → _shell_write_to_python 转换
  → 执行 python3 -c 命令
  → 验证写入文件内容保留 4 空格缩进

这种测试能捕获 _balanced_text 回归 bug（P0-4）和 CDATA 解析 bug（P0-5）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.protocol.tool_parser import parse_tool_calls_from_text
from glm2api.services.translator import sanitize_tool_call_payload


def _build_dsml_block(command_array_json: str) -> str:
    """构造一个完整的 DSML tool_calls 块（含 CDATA）。"""
    return f"""<|DSML|tool_calls>
<|DSML|invoke name="shell">
<|DSML|parameter name="command"><![CDATA[{command_array_json}]]>
</|DSML|parameter>
</|DSML|invoke>
</|DSML|tool_calls>"""


def _run_full_pipeline(dsml_block: str, workdir):
    """完整链路：DSML → parse → sanitize → shell_write → 执行 → 返回文件列表。"""
    summary, tool_calls = parse_tool_calls_from_text(dsml_block)
    if not tool_calls:
        return [], "no tool_calls parsed"

    files_created = []
    errors = []
    for tc in tool_calls:
        args = tc["function"]["arguments"]
        if isinstance(args, str):
            args = json.loads(args)
        cleaned = sanitize_tool_call_payload("shell", args)
        cmd = cleaned.get("command", [])
        if not isinstance(cmd, list) or not cmd:
            errors.append(f"empty command after sanitize: {cleaned}")
            continue
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            errors.append(f"exec failed: {proc.stderr[:200]}")
            continue

    for f in sorted(Path(workdir).iterdir()):
        if f.is_file():
            files_created.append((f.name, f.read_text()))
    return files_created, errors


def test_e2e_python_code_with_4_space_indent(tmp_path):
    """端到端：Python 代码含 4 空格缩进应完整保留。

    v19 P0-4 回归 bug：_balanced_text strip 缩进，导致 4 空格变 0 空格。
    v20 修复后应保留 4 空格缩进。
    """
    # 模拟 GLM 输出：含 4 空格缩进的 Python heredoc
    command = [
        "sh", "-c",
        "cat > fib.py << 'EOF'\na, b = 0, 1\nfor _ in range(20):\n    print(a)\n    a, b = b, a + b\nEOF"
    ]
    dsml = _build_dsml_block(json.dumps(command))

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    # 应该创建 fib.py
    fib_files = [f for f in files if f[0] == "fib.py"]
    assert len(fib_files) == 1, f"应该创建 fib.py，实际: {[f[0] for f in files]}"
    content = fib_files[0][1]

    # 4 空格缩进必须保留（v19 P0-4 回归 bug 测试）
    assert "    print(a)" in content, f"4 空格缩进丢失（P0-4 回归）: {content!r}"
    assert "    a, b = b, a + b" in content, f"4 空格缩进丢失（P0-4 回归）: {content!r}"

    # Python 语法应正确
    import ast
    ast.parse(content)

    # 运行 fib.py 应成功
    proc = subprocess.run(["python3", "fib.py"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"fib.py 运行失败: {proc.stderr}"
    assert "0\n1\n1\n2\n3" in proc.stdout


def test_e2e_flask_app_with_indentation(tmp_path):
    """端到端：Flask app.py 含 4 空格缩进应完整保留。"""
    command = [
        "sh", "-c",
        "cat > app.py << 'EOF'\nfrom flask import Flask\napp = Flask(__name__)\n\n@app.route('/')\ndef hello():\n    return 'Hello'\n\nif __name__ == '__main__':\n    app.run(debug=True)\nEOF"
    ]
    dsml = _build_dsml_block(json.dumps(command))

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    app_files = [f for f in files if f[0] == "app.py"]
    assert len(app_files) == 1
    content = app_files[0][1]

    # 4 空格缩进必须保留
    assert "    return 'Hello'" in content, f"函数体缩进丢失: {content!r}"
    assert "    app.run(debug=True)" in content, f"main 块缩进丢失: {content!r}"

    # Python 语法应正确
    import ast
    ast.parse(content)


def test_e2e_multi_command_array_with_indentation(tmp_path):
    """端到端：多命令数组（heredoc + echo）+ 缩进应完整保留。

    v19 P0-3 场景：GLM 生成 ['cat > app.py << EOF...', 'echo flask > requirements.txt']
    """
    command = [
        "cat > app.py << 'EOF'\nfrom flask import Flask\n\n@app.route('/')\ndef index():\n    return 'Hello'\nEOF",
        "echo flask > requirements.txt"
    ]
    dsml = _build_dsml_block(json.dumps(command))

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    # 应该创建两个文件
    file_names = [f[0] for f in files]
    assert "app.py" in file_names, f"app.py 未创建: {file_names}"
    assert "requirements.txt" in file_names, f"requirements.txt 未创建: {file_names}"

    app_content = next(f[1] for f in files if f[0] == "app.py")
    # 4 空格缩进必须保留
    assert "    return 'Hello'" in app_content, f"缩进丢失: {app_content!r}"

    req_content = next(f[1] for f in files if f[0] == "requirements.txt")
    assert "flask" in req_content


def test_e2e_cdata_with_json_array_double_close(tmp_path):
    """端到端：CDATA 含 JSON 数组 + 双 ]]> 应正确解析。

    v19 P0-5 场景：GLM 输出 CDATA 含 ] 产生双 ]]>]]>
    """
    # 构造含双 ]]> 的 DSML 块（模拟 GLM bug）
    command = [
        "sh", "-c",
        "cat > data.json << 'EOF'\n[{\"name\": \"Alice\", \"age\": 30}]\nEOF"
    ]
    command_json = json.dumps(command)
    # 模拟 GLM 双 ]]> bug：在 CDATA 结尾加额外的 ]]>
    dsml = f"""<|DSML|tool_calls>
<|DSML|invoke name="shell">
<|DSML|parameter name="command"><![CDATA[{command_json}]]>]]>
</|DSML|parameter>
</|DSML|invoke>
</|DSML|tool_calls>"""

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    data_files = [f for f in files if f[0] == "data.json"]
    assert len(data_files) == 1, f"data.json 未创建: {[f[0] for f in files]}"
    content = data_files[0][1]
    # JSON 应正确解析
    parsed = json.loads(content)
    assert parsed[0]["name"] == "Alice"
    assert parsed[0]["age"] == 30


def test_e2e_heredoc_first_format_with_indentation(tmp_path):
    """端到端：cat << EOF > file 格式 + 4 空格缩进应完整保留。

    v19 P0-2 场景：GLM 实际输出 'cat << EOF > file'（heredoc 在前）
    """
    command = [
        "sh", "-c",
        "cat << 'EOF' > fib.py\na, b = 0, 1\nfor _ in range(20):\n    print(a)\n    a, b = b, a + b\nEOF"
    ]
    dsml = _build_dsml_block(json.dumps(command))

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    fib_files = [f for f in files if f[0] == "fib.py"]
    assert len(fib_files) == 1
    content = fib_files[0][1]

    # 4 空格缩进必须保留
    assert "    print(a)" in content, f"4 空格缩进丢失: {content!r}"
    assert "    a, b = b, a + b" in content

    # 运行成功
    proc = subprocess.run(["python3", "fib.py"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0
    assert "0\n1\n1\n2\n3" in proc.stdout


def test_e2e_python_code_with_8_space_indent(tmp_path):
    """端到端：8 空格缩进（嵌套块）应完整保留。"""
    command = [
        "sh", "-c",
        "cat > nested.py << 'EOF'\ndef outer():\n    for i in range(3):\n        for j in range(3):\n            print(i, j)\nouter()\nEOF"
    ]
    dsml = _build_dsml_block(json.dumps(command))

    files, errors = _run_full_pipeline(dsml, tmp_path)
    assert not errors, f"pipeline errors: {errors}"

    nested_files = [f for f in files if f[0] == "nested.py"]
    assert len(nested_files) == 1
    content = nested_files[0][1]

    # 8 空格缩进必须保留
    assert "        for j in range(3):" in content, f"8 空格缩进丢失: {content!r}"
    assert "            print(i, j)" in content, f"12 空格缩进丢失: {content!r}"

    # Python 语法应正确
    import ast
    ast.parse(content)

    # 运行成功（调用 outer() 后应输出 9 行）
    proc = subprocess.run(["python3", "nested.py"], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, f"运行失败: {proc.stderr}"
    assert "0 0" in proc.stdout
    assert "2 2" in proc.stdout
    # 应该有 9 行输出（3x3）
    assert len(proc.stdout.strip().split('\n')) == 9
