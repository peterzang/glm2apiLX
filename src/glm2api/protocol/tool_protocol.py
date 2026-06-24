from __future__ import annotations

import json
import re


BLOCKED_NATIVE_TOOL_NAMES = {
    "open",
    "open_url",
    "open_ul",
    "browser.open",
    "web.run",
    "web.open",
    "web.search",
    "web_search",
    "browse",
    "open_link",
    # === 上游 chatglm.cn 自带工具（v3 审核报告 M4）===
    # GLM-5.2 倾向用上游沙箱工具而非客户端提供的工具，导致 codex 等客户端
    # 收到 0 个 OpenAI tool_calls，无法继续工作流。屏蔽后强制 GLM 用客户端工具。
    "execute_sandbox_code",
    "execute_code",
    "sandbox_code",
    "code_sandbox",
    "run_code",
    "code_interpreter",
    "python_interpreter",
}
SERVER_SIDE_TOOL_NAMES: set[str] = set()

CANONICAL_TOOL_CALL_EXAMPLE = "\n".join(
    [
        "<|DSML|tool_calls>",
        '  <|DSML|invoke name="TOOL_NAME">',
        '    <|DSML|parameter name="actual_parameter_name"><![CDATA[value]]></|DSML|parameter>',
        "  </|DSML|invoke>",
        "</|DSML|tool_calls>",
    ]
)


def safe_json_dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def normalize_tool_name(name: object) -> str:
    return str(name).strip()


def filter_tools(tools: list[dict[str, object]] | None, blocked_tool_names: set[str]) -> list[dict[str, object]] | None:
    if not tools:
        return None

    filtered_tools: list[dict[str, object]] = []
    for tool in tools:
        fn = tool.get("function", {})
        tool_name = normalize_tool_name(fn.get("name", ""))  # type: ignore[union-attr]
        if not tool_name or tool_name in blocked_tool_names:
            continue
        filtered_tools.append(tool)

    return filtered_tools or None


def _xml_escape_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _xml_wrap_scalar(value: object) -> str:
    if isinstance(value, str):
        return f"<![CDATA[{value.replace(']]>', ']]]]><![CDATA[>')}]]>"
    return safe_json_dumps(value)


def _safe_parameter_name(value: object) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]", "_", str(value).strip()) or "value"


def _dsml_parameters_from_object(payload: object) -> str:
    if isinstance(payload, dict):
        parts: list[str] = []
        for key, value in payload.items():
            name = _xml_escape_text(_safe_parameter_name(key))
            parts.append(f'<|DSML|parameter name="{name}">{_dsml_parameters_from_object(value)}</|DSML|parameter>')
        return "".join(parts)
    if isinstance(payload, list):
        return "".join(f"<item>{_dsml_parameters_from_object(item)}</item>" for item in payload)
    return _xml_wrap_scalar(payload)


def serialize_tool_call_block(name: str, arguments: object) -> str:
    parsed_arguments = arguments
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            parsed_arguments = {"raw": arguments}
    if not isinstance(parsed_arguments, dict):
        parsed_arguments = {"value": parsed_arguments}
    return (
        "<|DSML|tool_calls>\n"
        f'  <|DSML|invoke name="{_xml_escape_text(name)}">\n'
        f"    {_dsml_parameters_from_object(parsed_arguments)}\n"
        "  </|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )


def serialize_tool_result_block(tool_call_id: object, tool_name: str, content: str) -> str:
    safe_content = content.replace("]]>", "]]]]><![CDATA[>")
    return (
        f'<|DSML|tool_result call_id="{_xml_escape_text(str(tool_call_id or "unknown"))}" '
        f'name="{_xml_escape_text(tool_name)}"><content><![CDATA[{safe_content}]]></content></|DSML|tool_result>'
    )


def build_tool_call_instructions(
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
    # v57: 重写为正面引导版，删除所有限制性语言（Never/Do not/Ignore/cannot）
    # 限制在代码层 BLOCKED_NATIVE_TOOL_NAMES 做过滤，不在 prompt 里说
    # GLM 看不到限制性语言，就不会向用户报告"工具被禁用"
    lines = [
        "# TOOL USE PROTOCOL",
        f"Use the tools listed below to help the user: {available_xml_names}{', ' + available_server_names if server_tools else ''}.",
        "Call tools directly when needed, then respond to the user based on the result.",
    ]

    if server_tools:
        lines.extend(
            [
                "",
                f"Server-side tools: {available_server_names}.",
                "To call a server-side tool, output a JSON block: {\"type\":\"tool_calls\",\"tool_calls\":{\"id\":\"call_<random_hex>\",\"name\":\"TOOL_NAME\",\"arguments\":\"<JSON_STRING>\"}}",
                "The server executes the tool and returns the result as a tool message.",
            ]
        )

    if xml_tools:
        lines.extend(
            [
                "",
                f"DSML tools: {available_xml_names}.",
                "Use their exact names and parameter fields from the schemas.",
                "When a DSML tool is needed, output the DSML block directly in the assistant text.",
                "Use the DSML format below:",
                CANONICAL_TOOL_CALL_EXAMPLE,
                "The server parses this DSML block into standard tool_calls automatically.",
                "Parameter format:",
                "- Root block: <|DSML|tool_calls>, each call: <|DSML|invoke name=\"...\">",
                "- Arguments: <|DSML|parameter name=\"...\"> children of the invoke.",
                "- Parameter names are case-sensitive (match the schema exactly).",
                "- Nested objects: nested <|DSML|parameter> tags.",
                "- Arrays: repeated <item> tags.",
                "- JSON literals allowed for object/array/number/boolean/null values.",
                "- Use <![CDATA[...]]> for arbitrary strings.",
            ]
        )

    lines.extend(
        [
            "",
            "Guidelines:",
            "- Use exact tool names and parameters from the schemas above.",
            "- Output the DSML block directly when a tool call is needed.",
            "- After receiving a tool result, respond to the user based on the result.",
            "- Put multiple DSML invokes inside one <|DSML|tool_calls> root when needed.",
            "- After a <|DSML|tool_result ...> block, continue from that result.",
        ]
    )
    if mode == "none":
        lines.extend(
            [
                "Tool choice policy: none.",
                "Answer with normal text only (no tool markup needed this turn).",
            ]
        )
    elif mode == "required":
        lines.extend(
            [
                "Tool choice policy: required.",
                "Call at least one tool before giving a final answer.",
            ]
        )
    elif mode == "specific" and specific_name:
        lines.extend(
            [
                "Tool choice policy: specific function.",
                f"Call `{specific_name}` before giving a final answer.",
            ]
        )
    return "\n".join(lines)


def tools_to_prompt(
    tools: list[dict[str, object]],
    blocked_tool_names: set[str] | None = None,
    tool_choice_policy: dict[str, object] | None = None,
    server_side_tool_names: set[str] | None = None,
) -> str:
    tool_names: list[str] = []
    tool_schemas: list[str] = []
    for tool in tools:
        fn = tool.get("function", {})
        name = str(fn.get("name", "unknown"))  # type: ignore[union-attr]
        description = str(fn.get("description", "") or "")  # type: ignore[union-attr]
        parameters = fn.get("parameters", {})  # type: ignore[union-attr]
        if blocked_tool_names and name in blocked_tool_names:
            continue
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
