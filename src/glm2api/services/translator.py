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
        final_command = cleaned.get("command")
        if isinstance(final_command, list) and len(final_command) >= 3:
            cmd_str = " ".join(str(p) for p in final_command)
            converted = _heredoc_to_python_write(cmd_str)
            if converted:
                cleaned["command"] = converted

    return cleaned


def _heredoc_to_python_write(cmd_str: str) -> list[str] | None:
    """检测 heredoc 写入命令并转为 python3 -c 写入。

    检测模式：cat > file << 'DELIMITER'\ncontent\nDELIMITER
    转为：python3 -c "with open('file','w') as f: f.write('content')"

    返回 None 表示不是 heredoc 命令或不适合转换。
    """
    import re as _re
    # 匹配 cat > file << 'DELIMITER' 或 cat << 'DELIMITER' > file
    # 也匹配 cat > file << DELIMITER（不带引号）
    heredoc_pattern = _re.compile(
        r"cat\s+>\s*(\S+)\s*<<\s*['\"]?(\w+)['\"]?\n(.*?)\n\2",
        _re.DOTALL,
    )
    match = heredoc_pattern.search(cmd_str)
    if not match:
        return None

    filepath = match.group(1)
    content = match.group(3)

    # 安全检查：filepath 不含危险字符
    if any(c in filepath for c in [";", "|", "&", "$", "`", "(", ")"]):
        return None

    # 转义 content 中的单引号（用于 python3 -c '...' 格式）
    escaped_content = content.replace("\\", "\\\\").replace("'", "\\'")

    python_cmd = f"with open('{filepath}','w') as f: f.write('{escaped_content}')"
    return ["python3", "-c", python_cmd]


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
            "Do NOT write text explanations. Call a tool NOW.]"
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

    def _estimate_usage(self, completion_text: str = "") -> dict[str, int]:
        """Compute realistic prompt_tokens / completion_tokens / total_tokens."""
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
        return {
            "prompt_tokens": max(prompt_tokens, 1),
            "completion_tokens": max(completion_tokens, 1),
            "total_tokens": max(prompt_tokens + completion_tokens, 2),
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
        # P9 修复：非流式路径也检查 max_tokens
        finish_reason = "tool_calls" if all_tool_calls else "stop"
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
