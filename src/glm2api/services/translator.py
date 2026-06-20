from __future__ import annotations

import json
import logging
import re
import sys
import time
from bisect import insort
from dataclasses import dataclass, field
from logging import Logger

from ..config import AppConfig
from ..logging_utils import debug_dump
from ..core.model_variants import model_requests_search, model_requests_thinking, split_model_features
from ..core.openai_compat import (
    gen_chatcmpl_id,
    system_fingerprint,
)
from ..protocol.tool_parser import StreamingToolParser, parse_tool_calls_from_text
from ..protocol.tool_protocol import (
    BLOCKED_NATIVE_TOOL_NAMES,
    CANONICAL_TOOL_CALL_EXAMPLE,
    SERVER_SIDE_TOOL_NAMES,
    build_tool_call_instructions as _protocol_build_tool_call_instructions,
    filter_tools,
    normalize_tool_name,
    safe_json_dumps,
    serialize_tool_call_block as _protocol_serialize_tool_call_block,
    serialize_tool_result_block as _protocol_serialize_tool_result_block,
    tools_to_prompt as _protocol_tools_to_prompt,
)
from ..core.tokenizer import (
    count_tokens,
    estimate_completion_tokens,
    estimate_message_tokens,
    estimate_tools_tokens,
)


ASSISTANT_ID_PATTERN = re.compile(r"^[a-z0-9]{24,}$")
URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+")
POWERSHELL_CMDLET_PATTERN = re.compile(r"^[A-Z][A-Za-z]+-[A-Z][A-Za-z]+$")
POWERSHELL_ALIASES = {"cat", "cd", "copy", "del", "dir", "echo", "erase", "ls", "md", "move", "pwd", "rd", "ren", "rm", "sc", "type"}



def extract_text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text_parts.append(str(item.get("text", "")))
        elif item_type == "image_url":
            url = item.get("image_url", {}).get("url", "")
            text_parts.append(f"[image:{url}]")
        elif item_type == "file":
            url = item.get("file_url", {}).get("url", "")
            text_parts.append(f"[file:{url}]")
    return "\n".join(part for part in text_parts if part)


def extract_first_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)}+")


def extract_recent_user_url(messages: list[dict[str, object]]) -> str | None:
    for message in reversed(messages):
        if str(message.get("role", "")).strip() != "user":
            continue
        text = extract_text_content(message.get("content"))
        url = extract_first_url(text)
        if url:
            return url
    return None


def sanitize_tool_call_payload(
    tool_name: str,
    arguments: object,
    fallback_url: str | None = None,
) -> dict[str, object] | None:
    parsed_arguments = arguments
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None

    if parsed_arguments is None:
        parsed_arguments = {}
    if not isinstance(parsed_arguments, dict):
        return None

    cleaned = {str(key): value for key, value in parsed_arguments.items()}
    if cleaned == {"param_name": "url"} and fallback_url:
        cleaned = {"url": fallback_url}
    elif cleaned == {"param_name": "url"}:
        cleaned = {}
    if "param_name" in cleaned and "param_value" not in cleaned and len(cleaned) == 1:
        cleaned = {}

    if tool_name == "shell":
        command = cleaned.get("command")
        if isinstance(command, str):
            stripped_command = command.strip()
            if stripped_command.startswith("["):
                try:
                    parsed_command = json.loads(stripped_command)
                except json.JSONDecodeError:
                    parsed_command = None
                if isinstance(parsed_command, list):
                    cleaned["command"] = [str(part) for part in parsed_command]
            elif stripped_command.startswith('"'):
                try:
                    parsed_command = json.loads(f"[{stripped_command}]")
                except json.JSONDecodeError:
                    parsed_command = None
                if isinstance(parsed_command, list):
                    cleaned["command"] = [str(part) for part in parsed_command]
            else:
                # P16 修复：Linux 环境下不用 powershell.exe，改用 sh -c
                if sys.platform != "win32":
                    cleaned["command"] = ["sh", "-c", stripped_command]
                else:
                    cleaned["command"] = ["powershell.exe", "-Command", stripped_command]
        elif isinstance(command, list) and command:
            command_parts = [str(part) for part in command]
            command_name = command_parts[0].strip()
            lower_name = command_name.lower()
            is_shell_host = lower_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe"}
            is_powershell_command = bool(POWERSHELL_CMDLET_PATTERN.fullmatch(command_name)) or lower_name in POWERSHELL_ALIASES
            # P16 修复：Linux 环境下把 powershell.exe 命令转成 sh -c
            if sys.platform != "win32" and is_shell_host and lower_name in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
                # 把 ["powershell.exe", "-Command", "cat << ... > file"] 转成 ["sh", "-c", "cat << ... > file"]
                cmd_idx = None
                for i, p in enumerate(command_parts):
                    if p.strip().lower() in ("-command", "-c"):
                        cmd_idx = i + 1
                        break
                if cmd_idx is not None and cmd_idx < len(command_parts):
                    cleaned["command"] = ["sh", "-c", command_parts[cmd_idx]]
                else:
                    cleaned["command"] = ["sh", "-c", " ".join(command_parts[1:])]
            elif is_powershell_command and not is_shell_host:
                if sys.platform != "win32":
                    cleaned["command"] = ["sh", "-c", " ".join(command_parts)]
                else:
                    cleaned["command"] = ["powershell.exe", "-Command", " ".join(command_parts)]

        # === M3 修复：bash heredoc 引号转义修复 ===
        final_command = cleaned.get("command")
        if isinstance(final_command, list):
            cleaned["command"] = [
                _fix_bash_quote_escaping(part) if isinstance(part, str) else part
                for part in final_command
            ]
        elif isinstance(final_command, str):
            cleaned["command"] = _fix_bash_quote_escaping(final_command)

        # === P16-2 修复：heredoc 写入失败 → 自动转为 python3 -c 写入 ===
        # v13 审核报告：codex 长任务 todo.py 创建 0 字节，根因是 heredoc 语法被引号转义破坏
        # 修复方案：检测 cat > file << 'EOF'...EOF 模式，转为 python3 -c "open(file,'w').write('...')"
        # v19 修复：command 数组长度 >= 1 时就尝试转换（之前要求 >= 3，
        # 导致 GLM 生成的 ["cat > app.py << 'EOF'...", "echo flask > requirements.txt"]
        # 这种长度为 2 的多命令数组被跳过）
        final_command = cleaned.get("command")
        if isinstance(final_command, list) and len(final_command) >= 1:
            # 拼接 command 数组：每个元素可能本身就是完整命令（如 "cat > file << EOF..."）
            # 也可能是命令 token（如 ["sh", "-c", "cat > file << EOF..."]）
            # 用换行符拼接多命令元素，用空格拼接 token
            # 启发式：如果元素含空格或 > << 等操作符，说明是完整命令，用换行拼接
            parts = [str(p) for p in final_command]
            # 检测是否是 ["sh", "-c", "..."] / ["bash", "-c", "..."] 这种 token 数组
            if len(parts) >= 3 and parts[0] in ("sh", "bash", "zsh", "/bin/sh", "/bin/bash") and parts[1] == "-c":
                # token 数组：["sh", "-c", "actual command"]
                cmd_str = parts[2] if len(parts) == 3 else " ".join(parts[2:])
            else:
                # 完整命令数组：["cat > file << EOF...", "echo > file2..."]
                # 用换行符拼接（每个元素是独立命令）
                cmd_str = "\n".join(parts)
            # v17: 先尝试通用 shell 写入转换（支持 cat heredoc / echo > / printf > / tee），
            # 失败时降级到原 _heredoc_to_python_write（保持向后兼容）
            converted = _shell_write_to_python(cmd_str)
            if converted:
                cleaned["command"] = converted
            else:
                converted = _heredoc_to_python_write(cmd_str)
                if converted:
                    cleaned["command"] = converted

    return cleaned


def _shell_write_to_python(cmd_str: str) -> list[str] | None:
    """通用 shell 文件写入命令转 python3 -c 写入。

    支持格式（v17 新增 + v18 修复混合场景）：
      1. cat > file << 'EOF' ... EOF     （heredoc，多行/单行）
      2. echo "content" > file            （echo 重定向，双引号）
      3. echo 'content' > file            （echo 重定向，单引号）
      4. echo content > file              （echo 裸内容，无引号或含字面引号）
      5. echo "content" >> file           （echo 追加）
      6. printf "fmt" > file              （printf 重定向）
      7. echo "content" | tee file        （tee）
      8. echo "content" | tee -a file     （tee 追加）

    v18 关键修复：支持**混合写入场景**。v17 实现先调 _heredoc_to_python_write，
    成功就直接返回，导致 heredoc + echo 混合时 echo 部分被丢失。
    v18 改为：先收集 heredoc 块的 span，再扫描 echo/printf/tee，
    跳过与 heredoc 重叠的位置，最后合并所有写入操作到一个 python3 -c 脚本。

    设计原则：
      - 同时支持 heredoc + echo/printf/tee 混合场景
      - 用 base64 编码 content 避免转义问题
      - 自动创建父目录
      - 用 (start, end) 位置去重，避免同一字符被多个模式匹配

    返回 None 表示不是写入命令或不适合转换。
    """
    import re as _re
    import base64 as _b64

    # 安全字符集：字母数字 _ - . / +
    filepath_safe = r"[A-Za-z0-9_./\-+]+"

    # 收集所有 (filepath, mode, content, span_start, span_end) 五元组
    raw_matches: list[tuple[str, str, str, int, int]] = []

    # === 第 1 步：扫描所有 heredoc 块（多行 + 单行 + <<- 缩进 + 两种顺序）===
    # 复用 _heredoc_to_python_write 的正则逻辑，但直接在这里收集 matches
    # v18 修复 P0-2：支持两种 heredoc 顺序：
    #   1. cat > file << EOF\n...\nEOF  （重定向在前，常见格式）
    #   2. cat << EOF > file\n...\nEOF  （heredoc 在前，GLM 实际输出格式）
    # 多行格式 v1（重定向在前）
    multi_line_v1 = _re.compile(
        r"cat\s+(>>?)\s*(" + filepath_safe + r")\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\s*\3\s*(?:\n|$)",
        _re.DOTALL,
    )
    # 多行格式 v2（heredoc 在前，GLM 实际输出）
    multi_line_v2 = _re.compile(
        r"cat\s*<<-?\s*['\"]?(\w+)['\"]?\s*(>>?)\s*(" + filepath_safe + r")\n(.*?)\n\s*\1\s*(?:\n|$)",
        _re.DOTALL,
    )
    # 单行格式 v1（重定向在前）
    # 注意：用 [ \t]+ 而非 \s+，避免匹配多行场景的换行符
    # content 用 [^\n]*? 确保不跨行
    single_line_v1 = _re.compile(
        r"cat\s+(>>?)\s*(" + filepath_safe + r")\s*<<\s*['\"]?(?P<delim1>\w+)['\"]?[ \t]+(?P<content1>[^\n]*?)(?<=\S)[ \t]+(?P=delim1)(?!\w)",
    )
    # 单行格式 v2（heredoc 在前，GLM 实际输出）
    single_line_v2 = _re.compile(
        r"cat\s*<<\s*['\"]?(?P<delim2>\w+)['\"]?[ \t]+(?P<content2>[^\n]*?)(?<=\S)[ \t]+(?P=delim2)[ \t]*(>>?)\s*(" + filepath_safe + r")(?!\w)",
    )

    # 收集所有匹配，用 span 去重（同一位置可能被 v1/v2 两个 pattern 同时匹配）
    heredoc_spans: list[tuple[int, int]] = []

    def _is_span_seen(s, e):
        return any(s < se and e > ss for ss, se in heredoc_spans)

    for m in multi_line_v1.finditer(cmd_str):
        if _is_span_seen(m.start(), m.end()):
            continue
        mode = m.group(1)
        filepath = m.group(2)
        content = m.group(4)
        # 多行 heredoc 内容默认追加末尾换行（与 shell 行为一致）
        if not content.endswith('\n'):
            content = content + '\n'
        raw_matches.append((filepath, mode, content, m.start(), m.end()))
        heredoc_spans.append((m.start(), m.end()))

    for m in multi_line_v2.finditer(cmd_str):
        if _is_span_seen(m.start(), m.end()):
            continue
        mode = m.group(2)
        filepath = m.group(3)
        content = m.group(4)
        if not content.endswith('\n'):
            content = content + '\n'
        raw_matches.append((filepath, mode, content, m.start(), m.end()))
        heredoc_spans.append((m.start(), m.end()))

    for m in single_line_v1.finditer(cmd_str):
        if _is_span_seen(m.start(), m.end()):
            continue
        mode = m.group(1)
        filepath = m.group(2)
        content = m.group("content1")
        if "cat >" in content or "cat >>" in content or "cat <<" in content:
            continue
        raw_matches.append((filepath, mode, content, m.start(), m.end()))
        heredoc_spans.append((m.start(), m.end()))

    for m in single_line_v2.finditer(cmd_str):
        if _is_span_seen(m.start(), m.end()):
            continue
        content = m.group("content2")
        if "cat >" in content or "cat >>" in content or "cat <<" in content:
            continue
        mode = m.group(3)
        filepath = m.group(4)
        raw_matches.append((filepath, mode, content, m.start(), m.end()))
        heredoc_spans.append((m.start(), m.end()))

    # === 第 2 步：定义 echo/printf/tee 模式 ===
    # 双引号：echo "..." > file 或 printf "..." > file
    dq_pattern = _re.compile(
        r"(?:echo|printf)\s+\"((?:[^\"\\]|\\.)*)\"\s*(>>?)\s*(" + filepath_safe + r")"
    )
    # 单引号
    sq_pattern = _re.compile(
        r"(?:echo|printf)\s+'((?:[^'\\]|\\.)*)'\s*(>>?)\s*(" + filepath_safe + r")"
    )
    # 裸内容（可能含字面双引号）
    bare_pattern = _re.compile(
        r"echo\s+([^\s\"'][^<>|&;]*?)\s*(>>?)\s*(" + filepath_safe + r")"
    )
    # tee
    tee_dq_pattern = _re.compile(
        r"echo\s+\"((?:[^\"\\]|\\.)*)\"\s*\|\s*tee\s+(-a)?\s*(" + filepath_safe + r")"
    )
    tee_sq_pattern = _re.compile(
        r"echo\s+'((?:[^'\\]|\\.)*)'\s*\|\s*tee\s+(-a)?\s*(" + filepath_safe + r")"
    )

    def _unescape_dq(s: str) -> str:
        return (s.replace('\\"', '"')
                 .replace('\\\\', '\\')
                 .replace('\\$', '$')
                 .replace('\\`', '`')
                 .replace('\\\n', ''))

    def _unescape_sq(s: str) -> str:
        return s.replace("\\'", "'").replace('\\\\', '\\')

    def _is_heredoc_overlap(start, end):
        """检查位置是否与已收集的 heredoc 块重叠。"""
        for _, _, _, s, e in raw_matches:
            if start < e and end > s:
                return True
        return False

    # === 第 3 步：扫描 echo/printf/tee（跳过 heredoc 重叠位置）===
    # v18 安全：双引号字符串内含 $ ` 会导致 shell 变量展开/命令替换，
    # 我们的转换器写死字面量会导致内容错误，所以含这些字符的直接跳过
    def _has_shell_expansion(s: str) -> bool:
        """检测字符串是否含 shell 变量展开或命令替换（无法安全转义）。"""
        return "$" in s or "`" in s

    for m in dq_pattern.finditer(cmd_str):
        if _is_heredoc_overlap(m.start(), m.end()):
            continue
        content = _unescape_dq(m.group(1))
        if _has_shell_expansion(content):
            continue  # 含 $VAR 或 `cmd`，跳过（shell 会展开，我们写死会错）
        mode = m.group(2)
        filepath = m.group(3)
        raw_matches.append((filepath, mode, content, m.start(), m.end()))

    for m in sq_pattern.finditer(cmd_str):
        if _is_heredoc_overlap(m.start(), m.end()):
            continue
        content = _unescape_sq(m.group(1))
        # 单引号内 $ 和 ` 是字面量，不会展开，所以可以保留
        mode = m.group(2)
        filepath = m.group(3)
        raw_matches.append((filepath, mode, content, m.start(), m.end()))

    for m in tee_dq_pattern.finditer(cmd_str):
        if _is_heredoc_overlap(m.start(), m.end()):
            continue
        content = _unescape_dq(m.group(1))
        if _has_shell_expansion(content):
            continue
        mode = ">>" if m.group(2) == "-a" else ">"
        filepath = m.group(3)
        raw_matches.append((filepath, mode, content, m.start(), m.end()))

    for m in tee_sq_pattern.finditer(cmd_str):
        if _is_heredoc_overlap(m.start(), m.end()):
            continue
        content = _unescape_sq(m.group(1))
        mode = ">>" if m.group(2) == "-a" else ">"
        filepath = m.group(3)
        raw_matches.append((filepath, mode, content, m.start(), m.end()))

    # 裸内容（最后，跳过所有已有 match 重叠位置）
    for m in bare_pattern.finditer(cmd_str):
        if _is_heredoc_overlap(m.start(), m.end()):
            continue
        # 检查与 echo/printf/tee 已有 match 重叠
        overlap = False
        for _, _, _, s, e in raw_matches:
            if m.start() < e and m.end() > s:
                overlap = True
                break
        if overlap:
            continue
        content = m.group(1).strip()
        mode = m.group(2)
        filepath = m.group(3)
        first_token = content.split()[0] if content else ""
        if first_token in ("cat", "printf", "tee", "echo", "ls", "cd", "mkdir", "rm", "cp", "mv"):
            continue
        if any(c in content for c in [";", "&", "$", "`", "<"]):
            continue
        raw_matches.append((filepath, mode, content, m.start(), m.end()))

    if not raw_matches:
        # v21 P3: 检测 cat > file - （从 stdin 读，会导致 shell 挂起）
        # GLM 偶尔输出 cat >sort.py - 这种命令，codex_sim 执行时 stdin 关闭，
        # cat 会等待 stdin 输入导致 30s timeout。检测到这种模式时创建空文件避免挂起。
        stdin_cat_pattern = _re.compile(
            r"cat\s+(>>?)\s*(" + filepath_safe + r")\s+(?:-\s*$|-\s*(?:;|&&|\||$))"
        )
        stdin_matches = []
        for m in stdin_cat_pattern.finditer(cmd_str):
            mode = m.group(1)
            filepath = m.group(2)
            stdin_matches.append((filepath, mode, "", m.start(), m.end()))
        if not stdin_matches:
            return None
        raw_matches = stdin_matches

    # 按 span_start 排序，保持命令原始顺序
    raw_matches.sort(key=lambda x: x[3])

    # 为每个写入生成 python 语句
    statements: list[str] = []
    for filepath, mode, content, _, _ in raw_matches:
        # v21 P4: Python 代码缩进规范化（安全版本）
        # 只对 .py 文件做缩进修复，且只修复"1 空格缩进"为"4 空格缩进"
        # 风险控制：不修改 .json/.txt/.md 等非 Python 文件
        if filepath.endswith('.py') and content:
            content = _normalize_python_indentation(content)
        encoded = _b64.b64encode(content.encode("utf-8")).decode("ascii")
        encoded_repr = repr(encoded)
        filepath_repr = repr(filepath)
        open_mode = "'a'" if mode == ">>" else "'w'"
        stmt = (
            f"import os,base64 as _b; "
            f"_p={filepath_repr}; "
            f"_d=os.path.dirname(_p); "
            f"(_d and not os.path.isdir(_d) and os.makedirs(_d, exist_ok=True)); "
            f"open(_p,{open_mode}).write(_b.b64decode({encoded_repr}).decode('utf-8'))"
        )
        statements.append(stmt)

    full_script = "; ".join(statements)
    return ["python3", "-c", full_script]


def _normalize_python_indentation(content: str) -> str:
    """规范化 Python 代码缩进（安全版本，仅修复 1 空格缩进为 4 空格）。

    v21 报告 P4：GLM 偶尔输出 1 空格缩进（而非 4 空格），导致 Python IndentationError。
    例如：'for i in range(10):\\n print(i)' （print 只有 1 空格缩进）

    修复策略（保守）：
    - 只修复"行首恰好 1 个空格"的缩进为 4 个空格
    - 不修改 2/3/4/8 等其他缩进（避免破坏正确的缩进）
    - 不修改空行和注释行
    - 不修改行内空格

    风险：可能误修改 1 空格缩进的合法代码（如 continuation lines），但 Python
    continuation 通常用 4+ 空格，1 空格几乎总是错误的。
    """
    if not content or '\n' not in content:
        return content
    lines = content.split('\n')
    normalized = []
    for line in lines:
        # 空行或纯空白行保持原样
        if not line or line.isspace():
            normalized.append(line)
            continue
        # 检测行首恰好 1 个空格（后跟非空白字符）
        if line.startswith(' ') and not line.startswith('  '):
            # 1 空格缩进 → 4 空格缩进
            normalized.append('    ' + line[1:])
        else:
            normalized.append(line)
    return '\n'.join(normalized)


def _heredoc_to_python_write(cmd_str: str) -> list[str] | None:
    """检测 heredoc 写入命令并转为 python3 -c 写入。

    检测模式：
      多行格式: cat > file << 'DELIMITER'\\ncontent\\nDELIMITER
      单行格式: cat > file << 'DELIMITER' content DELIMITER  (GLM 实际输出)

    转为：python3 -c "import os,base64; open('file','w').write(b64decode('...'))"

    v15 修复：用 finditer() 收集所有 heredoc，每个生成独立的 open().write() 语句
    v16 修复：支持单行 heredoc 格式（GLM 把 \\n 压成空格的实际输出）
              + 支持 cat >> file 追加模式
              + 自动跳过非 heredoc 段（mkdir/echo/ls 等命令）

    返回 None 表示不是 heredoc 命令或不适合转换。
    """
    import re as _re
    import base64 as _b64

    # 安全检查：filepath 允许的字符集（字母数字 _ - . / +）
    # 不允许 ; | & $ ` ( ) > < 空格 等
    filepath_safe = r"[A-Za-z0-9_./\-+]+"

    # 收集所有 heredoc 块（filepath, mode, content）
    # 模式 1：多行格式（v15 支持）
    #   cat > file << 'DELIMITER'\ncontent\nDELIMITER
    #   cat >> file << 'DELIMITER'\ncontent\nDELIMITER
    multi_line_pattern = _re.compile(
        r"cat\s+(>>?)\s*(" + filepath_safe + r")\s*<<\s*['\"]?(\w+)['\"]?\n(.*?)\n\s*\3\s*(?:\n|$)",
        _re.DOTALL,
    )

    # 模式 2：单行格式（v16 新增，GLM 实际输出格式）
    #   cat > file << 'DELIMITER' content DELIMITER
    #   cat >> file << DELIMITER content DELIMITER
    # 用命名分组避免反向引用混淆；content 用非贪婪 .*? 匹配到第一个定界符
    single_line_pattern = _re.compile(
        r"cat\s+(>>?)\s*(" + filepath_safe + r")\s*<<\s*['\"]?(?P<delim>\w+)['\"]?\s+(?P<content>.*?)(?<=\S)\s+(?P=delim)(?!\w)",
    )

    matches = []  # list of (filepath, mode, content)

    # 先尝试多行格式
    for m in multi_line_pattern.finditer(cmd_str):
        mode = m.group(1)  # > or >>
        filepath = m.group(2)
        content = m.group(4)
        # 多行 heredoc 内容默认追加末尾换行（与 shell 行为一致）
        # shell 行为：cat > file << EOF\nline1\nEOF → 写入 "line1\n"
        # 我们的实现：write(content + '\n') 模拟 shell 行为
        if not content.endswith('\n'):
            content = content + '\n'
        matches.append((filepath, mode, content))

    # 如果多行没匹配到，尝试单行格式
    if not matches:
        for m in single_line_pattern.finditer(cmd_str):
            mode = m.group(1)  # > or >>
            filepath = m.group(2)
            content = m.group("content")
            # 单行格式的 content 不应该跨多个 cat 命令（避免贪婪匹配错误）
            # 如果 content 中含 "cat > " 字样，说明匹配跨越了边界，跳过
            if "cat >" in content or "cat >>" in content:
                continue
            matches.append((filepath, mode, content))

    if not matches:
        return None

    # 为每个 heredoc 生成 open().write() 语句
    statements: list[str] = []
    for filepath, mode, content in matches:
        # 用 base64 编码避免任何转义问题
        encoded = _b64.b64encode(content.encode("utf-8")).decode("ascii")
        encoded_repr = repr(encoded)
        filepath_repr = repr(filepath)
        # 自动创建父目录（codex 常写 src/app/main.py 这种带子目录的路径）
        if mode == ">>":
            # 追加模式
            stmt = (
                f"import os,base64 as _b; "
                f"_p={filepath_repr}; "
                f"_d=os.path.dirname(_p); "
                f"(_d and not os.path.isdir(_d) and os.makedirs(_d, exist_ok=True)); "
                f"open(_p,'a').write(_b.b64decode({encoded_repr}).decode('utf-8'))"
            )
        else:
            # 覆盖模式（默认）
            stmt = (
                f"import os,base64 as _b; "
                f"_p={filepath_repr}; "
                f"_d=os.path.dirname(_p); "
                f"(_d and not os.path.isdir(_d) and os.makedirs(_d, exist_ok=True)); "
                f"open(_p,'w').write(_b.b64decode({encoded_repr}).decode('utf-8'))"
            )
        statements.append(stmt)

    # 合并为一个 python3 -c 脚本，语句之间用分号分隔
    full_script = "; ".join(statements)
    return ["python3", "-c", full_script]


def _fix_bash_quote_escaping(text: str) -> str:
    """修复 GLM-5.2 生成的 bash 命令中的引号转义错误。

    已知 bug 模式（v3-v12 审核报告 M3/P16）：
      1. #"'!    →  #!       （shebang 行的诡异转义）
      2. '"'      →  '         （单引号被双引号包裹）
      3. \\"'     →  '         （转义单引号）
      4. \\"\\\\" →  "         （双重转义双引号，v12 新增）
      5. \\\\n    →  \\n       （过度转义换行符，v12 新增）
    """
    if not text or not isinstance(text, str):
        return text
    # 模式 1：#"'!  →  #!
    text = text.replace('#"\'!', '#!')
    # 模式 2：'"'  →  '
    text = text.replace('\'"\'', '\'')
    # 模式 3：\\"'  →  '
    text = text.replace('\\"\'', '\'')
    # 模式 4：\\"\\\\"  →  "（双重转义双引号）
    text = text.replace('\\" \\"', '"')
    # 模式 5：\\\\n  →  \\n（过度转义换行，但不影响实际执行因为是 JSON 字符串内）
    return text


def sanitize_tool_calls(
    tool_calls: list[dict[str, object]],
    fallback_url: str | None = None,
) -> list[dict[str, object]]:
    sanitized: list[dict[str, object]] = []
    for index, tool_call in enumerate(tool_calls):
        function = tool_call.get("function", {})
        if not isinstance(function, dict):
            continue
        tool_name = str(function.get("name", "")).strip()
        if not tool_name:
            continue
        original_arguments = function.get("arguments", "{}")
        original_value: object = original_arguments
        if isinstance(original_arguments, str):
            try:
                original_value = json.loads(original_arguments)
            except json.JSONDecodeError:
                original_value = original_arguments
        cleaned_arguments = sanitize_tool_call_payload(
            tool_name=tool_name,
            arguments=original_arguments,
            fallback_url=fallback_url,
        )
        if cleaned_arguments is None:
            continue
        repaired = not isinstance(original_value, dict) or (
            # 只有当参数的 key set 发生变化（如 {"param_name":"url"} → {"url": fallback_url}）
            # 才算"实质性修复"，对应的 tool error 结果应该丢弃（因为参数已经不一样了）。
            # 如果只是 value 被规范化（如 ["ls"] → ["powershell.exe","-Command","ls"]），
            # keys 没变，tool_call_id 仍然有效，对应的 tool 结果必须保留。
            # 否则 Codex shell 工具的合法 tool 结果会被错误丢弃，导致模型死循环。
            set(cleaned_arguments.keys()) != set(original_value.keys())
        )
        sanitized.append(
            {
                "id": str(tool_call.get("id", "")) or f"call_repaired_{index}",
                "type": "function",
                "index": index,
                "_repaired": repaired,
                "function": {
                    "name": tool_name,
                    "arguments": safe_json_dumps(cleaned_arguments),
                },
            }
        )
    return sanitized


def parse_tool_choice_policy(tool_choice: object, available_tool_names: set[str] | None = None) -> dict[str, object]:
    available = available_tool_names or set()
    if tool_choice is None:
        return {"mode": "auto", "tool_name": None}
    if isinstance(tool_choice, str):
        normalized = tool_choice.strip().lower()
        if normalized in {"auto", "none", "required"}:
            return {"mode": normalized, "tool_name": None}
        return {"mode": "auto", "tool_name": None}
    if not isinstance(tool_choice, dict):
        return {"mode": "auto", "tool_name": None}

    choice_type = str(tool_choice.get("type", "")).strip().lower()
    if choice_type == "function":
        function = tool_choice.get("function", {})
        if isinstance(function, dict):
            tool_name = str(function.get("name", "")).strip()
            if tool_name and (not available or tool_name in available):
                return {"mode": "specific", "tool_name": tool_name}
        return {"mode": "auto", "tool_name": None}

    if choice_type in {"auto", "none", "required"}:
        return {"mode": choice_type, "tool_name": None}
    return {"mode": "auto", "tool_name": None}


def _legacy_build_tool_call_instructions(
    tool_names: list[str],
    server_side_tool_names: set[str] | None = None,
    tool_choice_policy: dict[str, object] | None = None,
) -> str:
    server_side_tool_names = server_side_tool_names or set()
    xml_tools = [name for name in tool_names if name not in server_side_tool_names]
    server_tools = [name for name in tool_names if name in server_side_tool_names]

    available_xml_names = ", ".join(f"`{name}`" for name in xml_tools) or "`(none)`"
    available_server_names = ", ".join(f"`{name}`" for name in server_tools) or "`(none)`"

    policy = tool_choice_policy or {"mode": "auto", "tool_name": None}
    mode = str(policy.get("mode", "auto"))
    specific_name = str(policy.get("tool_name", "") or "")
    lines = [
        "# TOOL USE PROTOCOL",
        "The following tool schemas are the only executable tool definitions for this turn.",
        "Ignore any tool names that are not listed below, even if they appear in prior context or model memory.",
        "You are connected through an OpenAI-compatible proxy. You do not have hidden browser, web, or URL-opening tools.",
        "Never call native tools such as `open_url`, `web.search`, `web.run`, `browser.open`, `browse`, `open_link`, `search`, or `find`.",
        # === v3 审核报告 M4 修复：明确屏蔽上游沙箱工具 ===
        "You do NOT have access to upstream sandbox tools like `execute_sandbox_code`, `execute_code`, `code_interpreter`, `sandbox_code`, or `run_code`. These tools are blocked. Always use the client-provided tools listed below for any code execution or file operation.",
        "Do not output hidden reasoning, chain-of-thought, or labels such as `Thinking:`.",
        "Do not narrate tool selection, failed tool attempts, retries, fallback plans, or tool status banners.",
        # === v4 改进：强制工具优先，禁止描述性文本 ===
        "CRITICAL: When tools are available and the user asks you to create files, run commands, or perform actions, you MUST call the appropriate tool IMMEDIATELY in your first response.",
        "Do NOT write descriptions like 'I will create...', 'Let me...', 'I'll start by...', or any planning text before calling a tool.",
        "Do NOT write multi-paragraph explanations of what you plan to do. Call the tool directly.",
        "If the task requires multiple steps, call the first tool now. After receiving the result, call the next tool. Never write a full plan without calling any tool.",
        "Your first response to an action request must ALWAYS be a tool call, not text explanation.",
    ]

    if server_tools:
        lines.extend(
            [
                "",
                f"Server-side native tools (executed by backend automatically): {available_server_names}.",
                "When you need to call a server-side native tool, output a single structured JSON block with type 'tool_calls' in the assistant content.",
                'Format: {"type":"tool_calls","tool_calls":{"id":"call_<random_hex>","name":"TOOL_NAME","arguments":"<JSON_STRING>"}}',
                "The arguments field must be a JSON string (not a raw object). The server will intercept this block, execute the tool, and inject the result back into the stream as a tool message.",
                "Do not wrap server-side tool calls in XML. Do not mix prose and the tool_calls JSON block in the same response.",
            ]
        )

    if xml_tools:
        lines.extend(
            [
                "",
                f"XML-based tools (parsed by this server): {available_xml_names}.",
                "Only these XML-based tools are available. Use their exact names and exact parameter fields from the schemas.",
                "If an XML-based tool is needed, output executable XML only. Do not add prose, apologies, analysis, or progress text in the same assistant answer.",
                "Use the private ml-prefixed canonical format below exactly.",
                CANONICAL_TOOL_CALL_EXAMPLE,
                "The server will parse this XML intermediate language back into standard OpenAI tool_calls.",
                "Parameter rules:",
                "- The root executable block must be <ml_tool_calls> and each call must be a <ml_tool_call> child.",
                "- Each <ml_tool_call> must contain exactly one <ml_tool_name> and one <ml_parameters> block.",
                "- Use the real parameter names as XML tags inside <ml_parameters>; never use a literal <param_name> placeholder tag.",
                "- Encode arguments as nested XML tags inside <ml_parameters>.",
                "- Use repeated <item> tags to represent arrays.",
            ]
        )

    lines.extend(
        [
            "",
            "Rules:",
            "- Do not invent tool names outside the declared list.",
            "- If a URL, browsing, or search action is needed, use only an explicitly listed client tool. If none is listed, explain that no such tool is available. Never use bare tool names `search` or `find` unless they are explicitly listed above.",
            "- If you decide to call a tool, call the selected tool directly; do not say you will try, switch, retry, or use a correct tool.",
            "- Never output tool-call display text such as `⚙ tool_name [...]`; output only the executable XML block.",
            "- After receiving a tool result, answer the user directly from the result and do not repeat the earlier tool-call decision process.",
            "- For XML-based tools, do not emit OpenAI JSON tool_calls arrays, function_call objects, or any non-XML tool syntax.",
            "- For XML-based tools, do not use <tool_calls>, <tool_call>, <tool_name>, <parameters>, <function_call>, <tool_use>, <invoke>, or any legacy wrapper.",
            "- Do not place raw JSON directly inside <ml_parameters>.",
            "- Do not mix normal explanation text with executable tool XML.",
            "- Prefer <![CDATA[...]]> for arbitrary strings.",
            "- Put multiple XML calls inside one <ml_tool_calls> root when you truly need multiple calls in one turn.",
            "- After a <ml_tool_result ...> block, continue from that result and call another tool only when necessary.",
        ]
    )
    if mode == "none":
        lines.extend(
            [
                "Tool choice policy: none.",
                "Do not emit any executable tool markup. Answer with normal text only.",
            ]
        )
    elif mode == "required":
        lines.extend(
            [
                "Tool choice policy: required.",
                "You must call at least one tool before giving a final answer.",
            ]
        )
    elif mode == "specific" and specific_name:
        lines.extend(
            [
                "Tool choice policy: specific function.",
                f"You must call exactly `{specific_name}` before giving a final answer.",
                f"Do not call any tool other than `{specific_name}`.",
            ]
        )
    return "\n".join(lines)


def _legacy_tools_to_prompt(
    tools: list[dict[str, object]],
    blocked_tool_names: set[str] | None = None,
    tool_choice_policy: dict[str, object] | None = None,
    server_side_tool_names: set[str] | None = None,
) -> str:
    tool_names: list[str] = []
    tool_schemas: list[str] = []
    for tool in tools:
        fn = tool.get("function", {})
        name = str(fn.get("name", "unknown")) # type: ignore
        description = str(fn.get("description", "") or "") # type: ignore
        parameters = fn.get("parameters", {}) # type: ignore
        tool_names.append(name)
        tool_schemas.append(
            "\n".join(
                [
                    f"Tool: {name}",
                    f"Description: {description}",
                    f"Parameters: {safe_json_dumps(parameters) if isinstance(parameters, dict) else '{}'}",
                ]
            )
        )

    parts = [
        "# TOOL SCHEMAS",
        "Treat the following schema list as the authoritative tool contract for this request.",
        "",
        "\n\n".join(tool_schemas),
        "",
        build_tool_call_instructions(
            tool_names,
            server_side_tool_names=server_side_tool_names,
            tool_choice_policy=tool_choice_policy,
        ),
    ]
    return "\n".join(part for part in parts if part is not None).strip()


build_tool_call_instructions = _protocol_build_tool_call_instructions
serialize_tool_call_block = _protocol_serialize_tool_call_block
serialize_tool_result_block = _protocol_serialize_tool_result_block
tools_to_prompt = _protocol_tools_to_prompt


def convert_messages(
    messages: list[dict[str, object]],
    tools: list[dict[str, object]] | None,
    blocked_tool_names: set[str] | None = None,
    tool_choice: object | None = None,
    server_side_tool_names: set[str] | None = None,
) -> list[dict[str, object]]:
    tools = filter_tools(tools, blocked_tool_names or set())
    available_tool_names = {
        str(tool.get("function", {}).get("name", "")).strip()
        for tool in (tools or [])
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }
    available_tool_names.discard("")
    server_side_tool_names = server_side_tool_names or SERVER_SIDE_TOOL_NAMES
    tool_choice_policy = parse_tool_choice_policy(tool_choice, available_tool_names)
    processed: list[dict[str, str]] = []
    latest_user_url: str | None = extract_recent_user_url(messages)
    valid_tool_call_ids: set[str] = set()
    repaired_tool_call_ids: set[str] = set()
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content")
        if role == "user":
            current_text = extract_text_content(content)
            current_url = extract_first_url(current_text)
            if current_url:
                latest_user_url = current_url
        if role == "assistant" and message.get("tool_calls"):
            tool_blocks: list[str] = []
            raw_tool_calls = message.get("tool_calls", []) # pyright: ignore[reportGeneralTypeIssues]
            sanitized_tool_calls = sanitize_tool_calls(
                raw_tool_calls if isinstance(raw_tool_calls, list) else [],
                fallback_url=latest_user_url,
            )
            for tool_call in sanitized_tool_calls:
                function = tool_call.get("function", {})
                tool_name = str(function.get("name", "unknown"))
                if available_tool_names and tool_name not in available_tool_names:
                    continue
                tool_blocks.append(
                    serialize_tool_call_block(
                        name=tool_name,
                        arguments=function.get("arguments", "{}"),
                    )
                )
                tool_call_id = str(tool_call.get("id", "")).strip()
                if tool_call_id and not tool_call_id.startswith("call_repaired_"):
                    valid_tool_call_ids.add(tool_call_id)
                    if bool(tool_call.get("_repaired")):
                        repaired_tool_call_ids.add(tool_call_id)
            assistant_text = extract_text_content(content).strip() if content else ""
            block = "\n".join(tool_blocks)
            if not assistant_text and not block:
                continue
            content = f"{assistant_text}\n{block}".strip() if assistant_text and block else (assistant_text or block)
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id", "")).strip()
            # 第一个 check：客户端传的 tool_call_id 必须对应一个我们认可过的 assistant tool_call。
            #   - 如果 tool_call_id 为空，放行（兼容某些客户端不传 id 的情况）
            #   - 如果有 id 但不在 valid_tool_call_ids 里，说明对应的 assistant tool_call 被丢了，跳过 tool 结果
            if tool_call_id and valid_tool_call_ids and tool_call_id not in valid_tool_call_ids:
                continue
            # 第二个 check：如果对应的 assistant tool_call 被实质性修复（参数 keys 变了），
            # 说明客户端用错误参数调了工具得到 error，这个 error 已经不相关了，跳过。
            # 注意：sanitize_tool_calls 现在只在 keys 变化时才标记 _repaired，
            # 所以"参数规范化"（如 PowerShell 别名 ls → powershell.exe -Command ls）不会被误标记，
            # Codex shell 工具的合法 tool 结果不会被错误丢弃。
            if tool_call_id and tool_call_id in repaired_tool_call_ids:
                continue
            role = "user"
            tool_name = str(message.get("name", "")).strip() or "unknown_tool"
            tool_result_text = extract_text_content(content)
            # 用普通 user 消息格式传递 tool 结果，而不是 DSML tool_result block。
            # 之前用 <|DSML|tool_result> 格式时，GLM 会误以为这是它要继续输出的对话格式，
            # 导致模型在第二轮反复调用同一个工具，陷入死循环。
            # 改成普通 user 消息 + 明确指令后，模型能正确理解"工具已执行完，现在该总结"。
            content = (
                f"[TOOL RESULT from `{tool_name}` (call_id={tool_call_id or 'unknown'})]\n"
                f"{tool_result_text}\n"
                f"[END OF TOOL RESULT. The tool has been executed by the client. "
                f"Do NOT call `{tool_name}` again with the same arguments. "
                f"Summarize the result for the user in plain text now.]"
            )
        elif role == "assistant" and not content:
            continue

        text = extract_text_content(content) if content else ""
        if text:
            processed.append({"role": role, "content": text})

    transcript_parts: list[str] = []

    if tools and tool_choice_policy.get("mode") != "none":
        transcript_parts.append(
            tools_to_prompt(
                tools,
                blocked_tool_names=blocked_tool_names,
                tool_choice_policy=tool_choice_policy,
                server_side_tool_names=server_side_tool_names,
            )
        )
        transcript_parts.append("# CONVERSATION")

    for item in processed:
        title = (
            item["role"]
            .replace("system", "System")
            .replace("assistant", "Assistant")
            .replace("user", "User")
            .replace("developer", "Developer")
        )
        transcript_parts.append(f"{title}: {item['content']}".strip())

    prompt = "\n\n".join(part for part in transcript_parts if part).strip()

    # P11 修复：在 prompt 末尾（"Assistant: " 之前）加一条最强力的工具调用提醒
    # 根因：工具指令在 prompt 开头，但 codex system prompt 很长（几千字符），
    # GLM 读到末尾时已经"忘记"了开头的工具指令，生成描述性文本而非调工具。
    # 解决：在末尾再放一条极短极强力的提醒，确保 GLM 最后看到的是"调工具"。
    if tools and tool_choice_policy.get("mode") != "none":
        prompt += (
            "\n\n[SYSTEM REMINDER: Tools are available. "
            "Your next response MUST be a tool call. "
            "Do NOT write text explanations. Call a tool NOW. "
            # v20 P2-1: 4 空格缩进提示，缓解 GLM 输出 1 空格缩进导致 Python IndentationError
            # v20 报告 task2 失败根因：GLM 输出 1 空格缩进，for 循环体与 for 同级
            # v22 P5: 重复 4 空格要求（双重提示，进一步缓解 GLM 不听话）
            "When writing Python code, ALWAYS use 4-space indentation. "
            "This is mandatory — 1-space or 2-space indentation will cause SyntaxError.]"
        )
        # P11-2: DEBUG 级别日志（已确认生效，降低生产环境日志噪声）
        logging.getLogger("glm2api.translator").debug(
            "P11 SYSTEM REMINDER 注入 tools=%d mode=%s prompt_len=%d",
            len(tools) if tools else 0,
            tool_choice_policy.get("mode"),
            len(prompt),
        )

    return [{"role": "user", "content": [{"type": "text", "text": prompt + "\n\nAssistant: "}]}]


def resolve_upstream_model(requested_model: str, config: AppConfig) -> tuple[str, str]:
    base_model, _ = split_model_features(requested_model)
    upstream_model = config.model_aliases.get(base_model, base_model)
    assistant_id = upstream_model if ASSISTANT_ID_PATTERN.fullmatch(upstream_model) else config.glm_assistant_id
    return upstream_model, assistant_id


def resolve_chat_mode(model: str, reasoning_effort: object, deep_research: object) -> str:
    lower_model = (model or "").lower()
    if deep_research or "deepresearch" in lower_model or "deep-research" in lower_model:
        return "deep_research"
    if reasoning_effort or model_requests_thinking(model) or "think" in lower_model or "zero" in lower_model:
        return "zero"
    return ""


def resolve_networking(model: str, web_search: object) -> bool:
    return bool(web_search) or model_requests_search(model)


def _estimate_token_count(text: str) -> int:
    """P12-2/P15-2 修复：使用项目已有的 count_tokens 精确估算。
    
    之前用简单的 ÷2/÷4 估算，现在用 core/tokenizer.py 的 count_tokens，
    它已经实现了 CJK 字符分类 + ASCII 词计数 + 数字 + 标点的精确估算。
    """
    if not text:
        return 0
    try:
        from ..core.tokenizer import count_tokens
        return count_tokens(text)
    except Exception:
        # 兜底：简单估算
        cjk_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' or '\uac00' <= c <= '\ud7af')
        total = len(text)
        if total == 0:
            return 0
        cjk_ratio = cjk_count / total
        if cjk_ratio > 0.3:
            return total // 2
        else:
            return total // 4


@dataclass
class GLMEventAccumulator:
    model: str
    allowed_tool_names: set[str] | None = None
    fallback_tool_url: str | None = None
    debug_enabled: bool = False
    logger: Logger | None = None
    conversation_id: str = ""
    created: int = field(default_factory=lambda: int(time.time()))
    parts_by_logic_id: dict[str, dict[str, object]] = field(default_factory=dict)
    ordered_logic_ids: list[str] = field(default_factory=list)
    last_full_text: str = ""
    last_full_reasoning: str = ""
    _part_text_sent: dict[str, int] = field(default_factory=dict)
    _part_reasoning_sent: dict[str, int] = field(default_factory=dict)
    _known_logic_ids_for_text: list[str] = field(default_factory=list)
    _known_logic_ids_for_reasoning: list[str] = field(default_factory=list)
    tool_parser: StreamingToolParser = field(default_factory=StreamingToolParser)
    emitted_role: bool = False
    _finish_reason_sent: bool = False  # P0: 防止重复发送 finish_reason
    _render_cache_dirty: bool = True
    _cached_full_text: str = ""
    _cached_full_reasoning: str = ""
    _cached_part_texts: dict[str, str] = field(default_factory=dict)
    _cached_part_reasonings: dict[str, str] = field(default_factory=dict)
    _server_side_tool_calls: list[dict[str, object]] = field(default_factory=list)
    _server_side_tool_call_ids: set[str] = field(default_factory=set)
    _deferred_visible_text: str = ""
    # OpenAI-compat fields
    prompt_messages: list[dict[str, object]] | None = None
    tools_schema: list[dict[str, object]] | None = None
    response_id: str = field(default_factory=gen_chatcmpl_id)
    _completion_text_buffer: str = ""
    # P9 修复：max_tokens 强制限制
    max_tokens_limit: int = 0  # 0 = 不限制
    _force_finished: bool = False  # 达到 max_tokens 后强制 finish

    def __post_init__(self) -> None:
        self.tool_parser.allowed_tool_names = self.allowed_tool_names

    def _estimate_usage(self, completion_text: str = "") -> dict[str, object]:
        """Compute realistic prompt_tokens / completion_tokens / total_tokens.

        v31 修复：添加 prompt_tokens_details 和 completion_tokens_details 子字段，
        与官方 OpenAI API 格式完全一致。
        """
        prompt_tokens = 0
        if self.prompt_messages is not None:
            try:
                prompt_tokens = estimate_message_tokens(self.prompt_messages)
            except Exception:
                prompt_tokens = 0
        if self.tools_schema:
            try:
                prompt_tokens += estimate_tools_tokens(self.tools_schema)
            except Exception:
                pass
        if not completion_text:
            completion_text = self._cached_full_text or self._completion_text_buffer
        completion_tokens = estimate_completion_tokens(completion_text) if completion_text else 0
        # Min 1 completion token if there's any output at all (matches OpenAI behavior)
        if completion_tokens == 0 and (self._cached_full_text or self._completion_text_buffer or self._server_side_tool_calls):
            completion_tokens = 1
        # v31: 计算推理 token（reasoning_content 的估算）
        reasoning_tokens = 0
        if self._cached_full_reasoning:
            reasoning_tokens = max(1, len(self._cached_full_reasoning) // 4)
        return {
            "prompt_tokens": max(prompt_tokens, 1),
            "completion_tokens": max(completion_tokens, 1),
            "total_tokens": max(prompt_tokens + completion_tokens, 2),
            # v31: 官方 API 兼容子字段
            "prompt_tokens_details": {
                "cached_tokens": 0,  # GLM 不支持 prompt cache
            },
            "completion_tokens_details": {
                "reasoning_tokens": reasoning_tokens,
            },
        }

    def consume_event(self, payload: dict[str, object]) -> tuple[list[str], str | None]:
        debug_dump(self.logger or logging.getLogger("glm2api.null"), self.debug_enabled, "GLM SSE 解析事件", payload)
        if not self.conversation_id and payload.get("conversation_id"):
            self.conversation_id = str(payload["conversation_id"])

        for part in payload.get("parts", []) if isinstance(payload.get("parts"), list) else []: # pyright: ignore[reportGeneralTypeIssues]
            if isinstance(part, dict) and part.get("logic_id"):
                logic_id = str(part["logic_id"])
                if logic_id not in self.parts_by_logic_id:
                    insort(self.ordered_logic_ids, logic_id)
                self.parts_by_logic_id[logic_id] = part
                self._render_cache_dirty = True
            # Extract server-side native tool_calls from content items
            if isinstance(part, dict) and isinstance(part.get("content"), list):
                for content in part["content"]:
                    if isinstance(content, dict) and content.get("type") == "tool_calls":
                        tool_calls_data = content.get("tool_calls")
                        if isinstance(tool_calls_data, dict):
                            tool_name = str(tool_calls_data.get("name", "")).strip()
                            tool_id = str(tool_calls_data.get("id", "")).strip()
                            arguments = tool_calls_data.get("arguments", "{}")
                            # 屏蔽上游沙箱工具（v3 审核报告 M4）
                            # GLM-5.2 倾向调用 execute_sandbox_code 等上游自带工具，
                            # 但这些工具客户端无法执行，导致 codex 看到 0 个 tool_calls
                            if tool_name in BLOCKED_NATIVE_TOOL_NAMES:
                                continue
                            if self.allowed_tool_names is not None and tool_name not in self.allowed_tool_names:
                                continue
                            if tool_name and tool_id and tool_id not in self._server_side_tool_call_ids:
                                self._server_side_tool_call_ids.add(tool_id)
                                self._server_side_tool_calls.append(
                                    {
                                        "id": tool_id,
                                        "type": "function",
                                        "index": len(self._server_side_tool_calls),
                                        "function": {
                                            "name": tool_name,
                                            "arguments": str(arguments) if isinstance(arguments, str) else safe_json_dumps(arguments),
                                        },
                                    }
                                )

        text_delta, reasoning_delta = self._compute_deltas()
        self.last_full_text = self._cached_full_text
        self.last_full_reasoning = self._cached_full_reasoning

        chunks: list[str] = []
        if reasoning_delta:
            chunks.append(
                self._chunk_json(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": reasoning_delta},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            )

        visible_text_delta = self.tool_parser.consume(text_delta)
        if visible_text_delta:
            self._completion_text_buffer += visible_text_delta
            # P9 修复：检查 max_tokens 限制
            if self.max_tokens_limit > 0 and not self._force_finished:
                # 近似 token 计数：每 4 字符 ≈ 1 token
                # P9/P12-2 修复：按内容语言动态估算 token 数
                approx_tokens = _estimate_token_count(self._completion_text_buffer)
                if approx_tokens >= self.max_tokens_limit:
                    self._force_finished = True
                    if self.logger:
                        self.logger.info(
                            "max_tokens 限制触发 approx_tokens=%d limit=%d model=%s，强制 finish",
                            approx_tokens, self.max_tokens_limit, self.model,
                        )
                    # 发送 finish chunk 并返回
                    chunks.append(
                        self._chunk_json(
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {},
                                        "finish_reason": "length",
                                        "logprobs": None,
                                    }
                                ]
                            }
                        )
                    )
                    self._finish_reason_sent = True  # P0: 标记已发送 finish_reason
                    return chunks, "finish"
            if self.allowed_tool_names is not None:
                self._deferred_visible_text += visible_text_delta
            else:
                delta_payload: dict[str, object] = {"content": visible_text_delta}
                if not self.emitted_role:
                    delta_payload = {"role": "assistant", "content": visible_text_delta}
                    self.emitted_role = True
                chunks.append(
                    self._chunk_json(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta_payload,
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                )
        debug_dump(self.logger or logging.getLogger("glm2api.null"), self.debug_enabled, "GLM SSE 生成增量块", chunks)
        return chunks, str(payload.get("status")) if payload.get("status") is not None else None

    def finalize(self, status: str | None, last_error: dict[str, object] | None = None) -> list[str]:
        tail_text, xml_tool_calls = self.tool_parser.flush()
        xml_tool_calls = sanitize_tool_calls(xml_tool_calls, fallback_url=self.fallback_tool_url)
        if not xml_tool_calls:
            xml_tool_calls = self._extract_reasoning_tool_calls()

        # Merge server-side and XML tool calls, re-indexing
        all_tool_calls: list[dict[str, object]] = list(self._server_side_tool_calls)
        for tc in xml_tool_calls:
            tc_copy = dict(tc)
            tc_copy["index"] = len(all_tool_calls)
            all_tool_calls.append(tc_copy)

        if self.logger:
            self.logger.info(
                "响应收尾 status=%s text_len=%s reasoning_len=%s tool_calls=%s server_tools=%s",
                status,
                len(self._cached_full_text),
                len(self._cached_full_reasoning),
                len(xml_tool_calls),
                len(self._server_side_tool_calls),
            )

        # === v4/v6/v10 改进：当客户端提供了 tools 但 GLM 没调任何工具时，检测是否是描述性文本 ===
        # P10 修复：阈值从 500 降到 30 字符
        # P17 修复：多轮对话后期 GLM 可能生成正常的总结性文本（如 "All files created"），
        # 这不应该被误判为描述性文本。增加规则：如果文本包含 "created"/"done"/"completed"/
        # "successfully"/"file" 等完成类关键词，不触发检测。
        DESCRIPTIVE_MIN_LEN = 30
        if (
            not all_tool_calls
            and self.allowed_tool_names is not None
            and len(self.allowed_tool_names) > 0
            and len(self._cached_full_text) > DESCRIPTIVE_MIN_LEN
            and not self._is_useful_response()
            and not self._is_completion_summary()  # P17: 不误判总结性文本
        ):
            if self.logger:
                self.logger.warning(
                    "检测到 GLM 生成描述性文本但未调用工具 text_len=%d model=%s，返回 error 让客户端重试",
                    len(self._cached_full_text),
                    self.model,
                )
            # 记录到复读/描述性文本统计
            try:
                from ..admin.store import get_store as _get_admin_store
                _get_admin_store().record_repetition_event(
                    model=str(self.model),
                    path="stream_descriptive",
                )
            except Exception:
                pass
            # 返回 error chunk（不发任何 content）
            error_chunk = self._chunk_json({
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
                "error": {
                    "message": "Model generated descriptive text without calling available tools. Please retry with a more specific prompt.",
                    "type": "upstream_error",
                    "code": "no_tool_call_descriptive_text",
                },
            })
            return [error_chunk, "data: [DONE]\n\n"]

        chunks: list[str] = []
        final_text = self._deferred_visible_text + tail_text
        self._deferred_visible_text = ""
        if final_text:
            self._completion_text_buffer += final_text
        if not final_text and not all_tool_calls and self.allowed_tool_names is not None:
            _, attempted_tool_calls = parse_tool_calls_from_text(
                self._cached_full_text.strip(),
                allowed_tool_names=None,
            )
            unavailable_names = sorted(
                {
                    str(tool_call.get("function", {}).get("name", "")).strip()
                    for tool_call in attempted_tool_calls
                    if isinstance(tool_call.get("function"), dict)
                    and str(tool_call.get("function", {}).get("name", "")).strip()
                    not in self.allowed_tool_names
                }
            )
            if unavailable_names:
                allowed_names = ", ".join(sorted(self.allowed_tool_names)) or "(none)"
                final_text = (
                    "模型尝试调用未声明工具 "
                    + ", ".join(f"`{name}`" for name in unavailable_names)
                    + f"，已阻止。本轮只允许这些工具：{allowed_names}。"
                )
        if final_text and not all_tool_calls:
            delta_payload: dict[str, object] = {"content": final_text}
            if not self.emitted_role:
                delta_payload = {"role": "assistant", "content": final_text}
                self.emitted_role = True
            chunks.append(
                self._chunk_json(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta_payload,
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            )

        if status == "intervene" and last_error and last_error.get("intervene_text"):
            chunks.append(
                self._chunk_json(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "\n\n" + str(last_error["intervene_text"])},
                                "finish_reason": None,
                            }
                        ]
                    }
                )
            )

        if all_tool_calls:
            if not self.emitted_role:
                chunks.append(
                    self._chunk_json(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant"},
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                )
                self.emitted_role = True
            for tool_call in all_tool_calls:
                chunks.append(
                    self._chunk_json(
                        {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": tool_call["index"],
                                                "id": tool_call["id"],
                                                "type": "function",
                                                "function": tool_call["function"],
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                )

        finish_reason = "tool_calls" if all_tool_calls else "stop"
        # P0 修复：如果 consume_event 已经发送了 finish_reason（如 "length"），
        # finalize 不再重复发送 finish_reason chunk
        if not self._finish_reason_sent:
            # Per OpenAI spec: chunk with finish_reason (no usage here)
            chunks.append(
                self._chunk_json(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason,
                                "logprobs": None,
                            }
                        ],
                    }
                )
            )
        # Final usage chunk (matches OpenAI stream_options.include_usage behavior)
        # choices is empty array here per spec
        chunks.append(
            self._chunk_json(
                {
                    "choices": [],
                    "usage": self._estimate_usage(),
                }
            )
        )
        chunks.append("data: [DONE]\n\n")
        debug_dump(self.logger or logging.getLogger("glm2api.null"), self.debug_enabled, "GLM SSE finalize 输出", chunks)
        return chunks

    def build_response(self) -> dict[str, object]:
        full_text, full_reasoning = self._render_full_output()
        if not full_text and self.last_full_text:
            full_text = self.last_full_text
        if not full_reasoning and self.last_full_reasoning:
            full_reasoning = self.last_full_reasoning
        clean_content, xml_tool_calls = parse_tool_calls_from_text(
            full_text.strip(),
            allowed_tool_names=self.allowed_tool_names,
        )
        xml_tool_calls = sanitize_tool_calls(xml_tool_calls, fallback_url=self.fallback_tool_url)
        if not xml_tool_calls:
            xml_tool_calls = self._extract_reasoning_tool_calls(full_reasoning)

        # Merge server-side and XML tool calls, re-indexing
        all_tool_calls: list[dict[str, object]] = list(self._server_side_tool_calls)
        for tc in xml_tool_calls:
            tc_copy = dict(tc)
            tc_copy["index"] = len(all_tool_calls)
            all_tool_calls.append(tc_copy)

        # === v4 改进：非流式路径也检测描述性文本 ===
        # 当客户端提供了 tools 但 GLM 没调任何工具且文本是描述性的，抛异常让 chat_completion 重试
        if (
            not all_tool_calls
            and self.allowed_tool_names is not None
            and len(self.allowed_tool_names) > 0
            and len(full_text) > 30  # P10: 从 500 降到 30
            and not self._is_useful_response()
        ):
            if self.logger:
                self.logger.warning(
                    "非流式路径检测到描述性文本但未调用工具 text_len=%d model=%s",
                    len(full_text),
                    self.model,
                )
            # 记录统计
            try:
                from ..admin.store import get_store as _get_admin_store
                _get_admin_store().record_repetition_event(
                    model=str(self.model),
                    path="non_stream_descriptive",
                )
            except Exception:
                pass
            # 抛异常让 chat_completion 的重试逻辑捕获
            raise RuntimeError("descriptive_text_without_tool_call")

        final_content = clean_content.strip()

        # P2-7: json_object / json_schema 模式下剥离 markdown 代码块
        # GLM 倾向用 ```json ... ``` 包裹 JSON 输出，导致客户端 json.loads() 失败
        if self.tools_schema is None or not all_tool_calls:
            # 检测是否是 JSON 模式（通过 prompt_messages 中的 system 指令判断）
            is_json_mode = False
            if self.prompt_messages:
                for msg in self.prompt_messages:
                    if isinstance(msg, dict) and msg.get("role") == "system":
                        content = msg.get("content", "")
                        if isinstance(content, str) and ("output JSON" in content or "json_schema" in content.lower()):
                            is_json_mode = True
                            break
            if is_json_mode and final_content:
                final_content = _strip_markdown_code_block(final_content)
        # P9 修复：非流式路径也检查 max_tokens
        finish_reason = "tool_calls" if all_tool_calls else "stop"

        # P1 修复：stop 序列支持（官方 API stop 参数）
        # 当模型输出包含 stop 序列时，在 stop 序列处截断并设置 finish_reason="stop"
        stop_sequences = getattr(self, "_stop_sequences", None)
        if stop_sequences and final_content and not all_tool_calls:
            for stop_seq in stop_sequences:
                if stop_seq and isinstance(stop_seq, str) and stop_seq in final_content:
                    idx = final_content.index(stop_seq)
                    final_content = final_content[:idx]
                    finish_reason = "stop"
                    if self.logger:
                        self.logger.info("stop 序列触发 stop=%r 截断到 %d 字符", stop_seq, idx)
                    break
        if (
            not all_tool_calls
            and self.max_tokens_limit > 0
            and final_content
        ):
            # P9/P12-2/P15 修复：按内容语言动态估算 token 数 + 动态截断
            approx_tokens = _estimate_token_count(final_content)
            if approx_tokens > self.max_tokens_limit:
                # P15 修复：截断时也按 CJK 比例动态调整 multiplier
                cjk_count = sum(1 for c in final_content if '\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u30ff' or '\uac00' <= c <= '\ud7af')
                cjk_ratio = cjk_count / len(final_content) if final_content else 0
                multiplier = 2 if cjk_ratio > 0.3 else 4
                final_content = final_content[: self.max_tokens_limit * multiplier]
                finish_reason = "length"
                # P15: 同时修正 usage，让 completion_tokens 反映截断后的实际值
                if self.logger:
                    self.logger.info(
                        "非流式 max_tokens 截断 approx_tokens=%d limit=%d multiplier=%d cjk_ratio=%.2f model=%s",
                        approx_tokens, self.max_tokens_limit, multiplier, cjk_ratio, self.model,
                    )

        message: dict[str, object] = {
            "role": "assistant",
            "content": None if all_tool_calls or not final_content else final_content,
            "reasoning_content": full_reasoning or None,
        }
        if all_tool_calls:
            message["tool_calls"] = [
                {"id": item["id"], "type": "function", "function": item["function"]}
                for item in all_tool_calls
            ]
        response = {
            "id": self.response_id or self.conversation_id or gen_chatcmpl_id(),
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": system_fingerprint(),
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                    "logprobs": None,
                }
            ],
            "usage": self._estimate_usage(completion_text=final_content),
        }
        if self.logger:
            self.logger.info(
                "非流式响应构建完成 model=%s text_len=%s reasoning_len=%s tool_calls=%s",
                self.model,
                len(final_content),
                len(full_reasoning),
                len(all_tool_calls),
            )
        debug_dump(self.logger or logging.getLogger("glm2api.null"), self.debug_enabled, "GLM 非流式最终响应", response)
        return response

    def _is_completion_summary(self) -> bool:
        """P17 修复：检测是否是多轮对话后期的正常总结性文本。

        codex 长任务后期 GLM 可能生成 "All files created successfully" 这类总结，
        不是描述性文本，不应该被检测拦截。
        """
        text = (self._cached_full_text or "").strip().lower()
        if not text or len(text) < 10:
            return False
        # 完成类关键词
        completion_keywords = [
            "created", "done", "completed", "successfully", "file",
            "built", "installed", "passed", "failed", "error",
            "all ", "now ", "finished", "ready",
            "创建", "完成", "成功", "失败", "文件", "已",
        ]
        keyword_count = sum(1 for kw in completion_keywords if kw in text)
        # 如果包含 2+ 个完成类关键词，判定为总结性文本
        if keyword_count >= 2:
            return True
        # 如果文本短且包含 "created"/"done"/"completed"，也判定为总结
        if len(text) < 200 and any(kw in text for kw in ("created", "done", "completed", "successfully")):
            return True
        return False

    def _is_useful_response(self) -> bool:
        """检测 GLM 生成的文本是否是"有用的响应"。

        v6 改进：增加更多描述性短语检测，降低误判率。
        P10 修复：覆盖短文本（"I'll create..." 只有 30+ 字符也检测）。
        """
        text = (self._cached_full_text or "").strip()
        if not text:
            return True  # 空文本不算描述性
        lower = text[:300].lower()
        # 规则 1：开头是描述性短语
        descriptive_starts = (
            "i will ", "i'll ", "let me ", "i'm going to ",
            "first, ", "sure, ", "certainly, ", "of course, ",
            # v6 新增：覆盖更多 GLM 描述性开头
            "i'll create", "i will create", "let me start",
            "i'm going to create", "i plan to", "here's what",
            "i'll build", "i will build", "let me build",
            "i'll implement", "i will implement",
        )
        if lower.startswith(descriptive_starts):
            # 检查是否后续真的调了工具（如果文本里含 XML tool call 标签，说明模型有尝试）
            if "<ml_tool" in text or "tool_calls" in text:
                return True  # 有工具调用尝试
            # 检查是否是代码（有代码块）
            if "```" in text[:500]:
                return True  # 含代码块
            return False  # 纯描述性
        # 规则 2：前 300 字符里多次出现描述性短语
        desc_count = sum(lower.count(phrase) for phrase in ("i will ", "i'll ", "let me "))
        if desc_count >= 3:
            return False
        # 规则 3（v6 新增）：文本包含 "planning" / "tasks" / "steps" 且没调工具
        if any(word in lower for word in ("planning the tasks", "start by planning", "here are the steps", "let me outline")):
            if "<ml_tool" not in text and "```" not in text[:500]:
                return False
        return True

    def _extract_reasoning_tool_calls(self, reasoning_text: str | None = None) -> list[dict[str, object]]:
        source = (reasoning_text if reasoning_text is not None else self.last_full_reasoning) or self._cached_full_reasoning
        if not source:
            return []
        _, tool_calls = parse_tool_calls_from_text(
            source.strip(),
            allowed_tool_names=self.allowed_tool_names,
        )
        return sanitize_tool_calls(tool_calls, fallback_url=self.fallback_tool_url)

    def _compute_deltas(self) -> tuple[str, str]:
        self._render_full_output()
        text_delta_parts: list[str] = []
        reasoning_delta_parts: list[str] = []

        for logic_id in self.ordered_logic_ids:
            rendered_text = self._cached_part_texts.get(logic_id, "")
            rendered_reasoning = self._cached_part_reasonings.get(logic_id, "")

            if rendered_text:
                prev_len = self._part_text_sent.get(logic_id, 0)
                is_new = logic_id not in self._known_logic_ids_for_text
                if is_new:
                    self._known_logic_ids_for_text.append(logic_id)
                    if text_delta_parts or self._part_text_sent:
                        text_delta_parts.append("\n\n")
                    text_delta_parts.append(rendered_text)
                elif len(rendered_text) > prev_len:
                    text_delta_parts.append(rendered_text[prev_len:])
                self._part_text_sent[logic_id] = len(rendered_text)

            if rendered_reasoning:
                prev_len = self._part_reasoning_sent.get(logic_id, 0)
                is_new = logic_id not in self._known_logic_ids_for_reasoning
                if is_new:
                    self._known_logic_ids_for_reasoning.append(logic_id)
                    if reasoning_delta_parts or self._part_reasoning_sent:
                        reasoning_delta_parts.append("\n\n")
                    reasoning_delta_parts.append(rendered_reasoning)
                elif len(rendered_reasoning) > prev_len:
                    reasoning_delta_parts.append(rendered_reasoning[prev_len:])
                self._part_reasoning_sent[logic_id] = len(rendered_reasoning)

        return "".join(text_delta_parts), "".join(reasoning_delta_parts)

    def _render_full_output(self) -> tuple[str, str]:
        if not self._render_cache_dirty:
            return self._cached_full_text, self._cached_full_reasoning

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        self._cached_part_texts.clear()
        self._cached_part_reasonings.clear()
        for logic_id in self.ordered_logic_ids:
            part = self.parts_by_logic_id.get(logic_id)
            if not isinstance(part, dict):
                continue
            content_items = part.get("content", [])
            if not isinstance(content_items, list):
                continue

            part_text: list[str] = []
            part_reasoning: list[str] = []
            for content in content_items:
                if not isinstance(content, dict):
                    continue
                item_type = content.get("type")
                if item_type == "text":
                    part_text.append(str(content.get("text", "")))
                elif item_type == "think":
                    part_reasoning.append(str(content.get("think", "")))
                elif item_type == "code":
                    part_text.append(f"```python\n{content.get('code', '')}\n```")
                elif item_type == "execution_output":
                    part_text.append(str(content.get("content", "")))
                elif item_type == "image":
                    images = content.get("image", [])
                    if isinstance(images, list):
                        for image in images:
                            if isinstance(image, dict) and image.get("image_url"):
                                part_text.append(f"![image]({image['image_url']})")

            rendered_text = "\n".join(filter(None, part_text)).strip()
            rendered_reasoning = "\n".join(filter(None, part_reasoning)).strip()
            if rendered_text:
                text_parts.append(rendered_text)
                self._cached_part_texts[logic_id] = rendered_text
            if rendered_reasoning:
                reasoning_parts.append(rendered_reasoning)
                self._cached_part_reasonings[logic_id] = rendered_reasoning

        self._cached_full_text = "\n\n".join(text_parts)
        self._cached_full_reasoning = "\n\n".join(reasoning_parts)
        self._render_cache_dirty = False
        return self._cached_full_text, self._cached_full_reasoning

    def _chunk_json(self, patch: dict[str, object]) -> str:
        payload = {
            "id": self.response_id or self.conversation_id or gen_chatcmpl_id(),
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": system_fingerprint(),
        }
        payload.update(patch)
        return "data: " + safe_json_dumps(payload) + "\n\n"


def _strip_markdown_code_block(text: str) -> str:
    """P2-7: 剥离 markdown 代码块标记，返回纯内容。

    处理以下格式：
    ```json\n{"key": "value"}\n```  →  {"key": "value"}
    ```\n{"key": "value"}\n```      →  {"key": "value"}
    ```python\nprint("hi")\n```     →  print("hi")
    """
    if not text:
        return text
    text = text.strip()
    # 检测是否以 ``` 开头
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 2:
            # 去掉第一行（```json 或 ```）
            # 去掉最后一行（```）
            # 但最后一行可能不是 ```，所以只去掉首行 + 尾部 ```
            first_line = lines[0].strip()
            # 首行是 ``` 或 ```json 等
            if first_line.startswith("```"):
                lines = lines[1:]
            # 去掉尾部的 ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
    return text
