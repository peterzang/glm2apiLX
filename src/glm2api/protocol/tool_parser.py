from __future__ import annotations

import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .tool_protocol import BLOCKED_NATIVE_TOOL_NAMES

CODE_FENCE_PATTERN = re.compile(r"```[\s\S]*?```")
TOOL_RESULT_PATTERN = re.compile(
    r"<(?:(?:\|DSML\|)|ml_)?tool_result\b[\s\S]*?</(?:(?:\|DSML\|)|ml_)?tool_result>",
    re.IGNORECASE,
)
START_TAG_PATTERN = re.compile(
    r"<(?P<tag>\|DSML\|tool_calls|tool_calls|ml_tool_calls|ml_tool_call)\b[^>]*>",
    re.IGNORECASE,
)
DSML_TAG_PATTERN = re.compile(r"</?\|DSML\|(?P<name>tool_calls|invoke|parameter|tool_result)\b", re.IGNORECASE)
DSML_OPEN_TAG_PATTERN = re.compile(
    r"<\|dsml\|(?P<name>tool_calls|toolcalls|invoke|parameter|tool_result|toolresult)\b(?P<attrs>[^<>]*?)>",
    re.IGNORECASE,
)
DSML_CLOSE_TAG_PATTERN = re.compile(
    r"</\|dsml\|(?P<name>tool_calls|toolcalls|invoke|parameter|tool_result|toolresult)\s*\|?\s*>",
    re.IGNORECASE,
)
DSML_COMPACT_CLOSE_TAG_PATTERN = re.compile(
    r"(?:</\|dsml|<\|/dsml)(?P<name>toolcalls|invoke|parameter|toolresult)\s*\|\s*>",
    re.IGNORECASE,
)
DSML_DOUBLE_PIPE_CLOSE_TAG_PATTERN = re.compile(
    r"<\|\|dsml\|(?P<name>tool_calls|toolcalls|invoke|parameter|tool_result|toolresult)\s*\|?\s*>",
    re.IGNORECASE,
)
DSML_TOOL_CALLS_CLOSE_PATTERN = re.compile(
    r"(?:</\|dsml\|tool_calls\s*>|</\|dsml\|tool_calls\s*\|\s*>|</\|dsmltool_?calls\s*\|\s*>|<\|/dsmltool_?calls\s*\|\s*>)",
    re.IGNORECASE,
)
DSML_TOOL_CALLS_TRAILING_CLOSE_PATTERN = re.compile(
    r"(?:</\|dsml\|tool_calls\s*>|</\|dsml\|tool_calls\s*\|\s*>|</\|dsmltool_?calls\s*\|\s*>|<\|/dsmltool_?calls\s*\|\s*>|</\|dsml\|tool_calls\s*$|</\|dsmltool_?calls\s*\|?\s*$|<\|/dsmltool_?calls\s*\|?\s*$)",
    re.IGNORECASE,
)
PARAM_NAME_TAG_PATTERN = re.compile(r"<param_name>\s*(.*?)\s*</param_name>", re.IGNORECASE | re.DOTALL)
PARAM_VALUE_TAG_PATTERN = re.compile(r"<param_value>\s*(.*?)\s*</param_value>", re.IGNORECASE | re.DOTALL)
TAG_NAME_HINTS = [
    "<|",
    "</|",
    "<|DSML|",
    "</|DSML|",
    "<|DSML|tool_calls",
    "</|DSML|tool_calls",
    "<|DSML|invoke",
    "</|DSML|invoke",
    "<|DSML|parameter",
    "</|DSML|parameter",
    "<|DSML|tool_result",
    "</|DSML|tool_result",
    "<m",
    "</m",
    "<ml_",
    "</ml_",
    "<ml_tool_calls",
    "</ml_tool_calls",
    "<ml_tool_call",
    "</ml_tool_call",
    "<ml_tool_name",
    "</ml_tool_name",
    "<ml_parameters",
    "</ml_parameters",
    "<ml_tool_result",
    "</ml_tool_result",
    "<tool_calls",
    "</tool_calls",
    "<invoke",
    "</invoke",
    "<parameter",
    "</parameter",
]


def _local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    if ":" in tag:
        tag = tag.split(":", 1)[1]
    return tag.lower()


def _canonical_dsml_name(name: str) -> str:
    normalized = name.lower().replace("_", "")
    if normalized == "toolcalls":
        return "tool_calls"
    if normalized == "toolresult":
        return "tool_result"
    return normalized


def _repair_malformed_dsml(block: str) -> str:
    if "<|" not in block and "]]|>" not in block:
        return block

    repaired = block.replace("]]|>", "]]>")
    if "<![CDATA[" in repaired:
        repaired = re.sub(
            r"(?<!\])\]>(?=</\|dsml\|parameter\b|</\|DSML\|parameter\b|</parameter\b|</\|dsmlparameter\|)",
            "]]>",
            repaired,
            flags=re.IGNORECASE,
        )

    # Fix GLM bug: closing tag written as open tag (e.g. `<|DSML|invoke>` instead of `</|DSML|invoke>`)
    # We detect these by looking for `<|DSML|invoke>` that appears AFTER parameters closed,
    # right before `</|DSML|tool_calls>` or another `<|DSML|invoke name=` opening.
    # Strategy: any `<|DSML|invoke>` (no name attr, no slash) immediately followed by
    # `</|DSML|tool_calls>` OR by `<|DSML|invoke name=` is actually a closing tag.
    repaired = re.sub(
        r"<\|DSML\|invoke\s*>",
        "</|DSML|invoke>",
        repaired,
        flags=re.IGNORECASE,
    )

    def replace_open(match: re.Match[str]) -> str:
        name = _canonical_dsml_name(match.group("name"))
        attrs = match.group("attrs").rstrip("|").rstrip()
        return f"<|DSML|{name}{attrs}>"

    def replace_close(match: re.Match[str]) -> str:
        return f"</|DSML|{_canonical_dsml_name(match.group('name'))}>"

    repaired = DSML_OPEN_TAG_PATTERN.sub(replace_open, repaired)
    repaired = DSML_CLOSE_TAG_PATTERN.sub(replace_close, repaired)
    repaired = DSML_COMPACT_CLOSE_TAG_PATTERN.sub(replace_close, repaired)
    repaired = DSML_DOUBLE_PIPE_CLOSE_TAG_PATTERN.sub(replace_close, repaired)
    repaired = re.sub(
        r"(?:</\|dsml\|tool_calls|</\|dsmltool_?calls|<\|/dsmltool_?calls)\s*\|?\s*$",
        "</|DSML|tool_calls>",
        repaired,
        flags=re.IGNORECASE,
    )
    return repaired


def _normalize_dsml_to_xml(block: str) -> str:
    repaired = _repair_malformed_dsml(block)
    xml_block = DSML_TAG_PATTERN.sub(lambda match: match.group(0).replace("|DSML|", ""), repaired)
    # v19 修复 P0-5：GLM 输出 CDATA 内容含 ] 字符时会产生双 ]]>]]>
    # 第一个 ]]> 是 GLM 误判的 CDATA 关闭符，第二个 ]]> 才是真正的关闭符
    # XML 解析器遇到第一个 ]]> 就关闭 CDATA，剩余内容成为无效字符，整个 DSML 块解析失败
    # 修复：把连续的 ]]>]]> (或更多) 替换为单个 ]]>
    xml_block = re.sub(r"(?:\]\]>){2,}", "]]>", xml_block)
    return xml_block


def _is_allowed_tool_name(tool_name: str, allowed_tool_names: set[str] | None) -> bool:
    if tool_name in BLOCKED_NATIVE_TOOL_NAMES:
        return False
    return allowed_tool_names is None or tool_name in allowed_tool_names


def _balanced_text(value: str) -> str:
    """规范化 DSML 文本节点的无关空白。

    注意：此函数只应用于普通 XML 文本节点（非 CDATA）。
    CDATA 内容是字面量，含 Python 代码缩进、JSON 缩进等结构信息，
    不应该被规范化。_leaf_text 会区分两者。

    v18 修复（P0-1）：旧实现用 re.sub(r"\\s+", " ", value) 把所有空白字符
    （包括换行符 \\n 和 tab \\t）替换成单个空格，导致 GLM 输出的多行 heredoc、
    多行 echo 内容、多行 Python 代码全部被压成单行，破坏语句分隔。

    v19 修复（P0-4）：v18 的修复仍用 line.strip() 把每行首尾空格去掉，
    导致 Python 代码缩进丢失（4 空格变 0 空格），IndentationError。
    现在 _balanced_text 只用于普通文本节点，CDATA 由 _leaf_text 直接返回原样。
    """
    # 只规范化空格和 tab，保留换行符
    value = re.sub(r"[ \t]+", " ", value)
    # 行首尾去空格但保留换行
    value = "\n".join(line.strip() for line in value.split("\n"))
    return value.strip()


def _leaf_text(element: ET.Element) -> str:
    """提取元素的文本内容，CDATA 内容原样返回不规范化。

    v19 修复（P0-4）：CDATA 内容是字面量（含 Python 代码缩进、JSON 缩进等），
    不应该经过 _balanced_text 规范化（会 strip 掉行首缩进）。
    用启发式：如果文本含换行符，说明是多行 CDATA，直接返回原样。
    """
    text = "".join(element.itertext())
    # CDATA 内容（多行）原样返回，保留缩进
    if "\n" in text:
        return text
    # 单行文本节点（XML 文本）可以规范化
    return _balanced_text(text)


def _coerce_leaf_value(text: str) -> object:
    stripped = text.strip()
    if stripped == "":
        return ""
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            if stripped.startswith("[") and not stripped.endswith("]"):
                try:
                    return json.loads(stripped + "]")
                except json.JSONDecodeError:
                    pass
            return stripped
    if stripped in {"true", "false"}:
        return stripped == "true"
    if stripped == "null":
        return None
    if re.fullmatch(r"-?\d+", stripped):
        try:
            return int(stripped)
        except ValueError:
            return stripped
    if re.fullmatch(r"-?\d+\.\d+", stripped):
        try:
            return float(stripped)
        except ValueError:
            return stripped
    return stripped


def _append_value(mapping: dict[str, object], key: str, value: object) -> None:
    if key not in mapping:
        mapping[key] = value
        return
    existing = mapping[key]
    if isinstance(existing, list):
        existing.append(value)
        return
    mapping[key] = [existing, value]


def _xml_value_to_object(element: ET.Element) -> object:
    children = [child for child in list(element) if isinstance(child.tag, str)]
    if not children:
        return _coerce_leaf_value(_leaf_text(element))

    repeated_item_only = all(_local_name(child.tag) == "item" for child in children)
    if repeated_item_only:
        return [_xml_value_to_object(child) for child in children]

    result: dict[str, object] = {}
    for child in children:
        key = child.attrib.get("name", "").strip() or _local_name(child.tag)
        _append_value(result, key, _xml_value_to_object(child))
    return result


def _extract_tool_name(element: ET.Element) -> str:
    if _local_name(element.tag) == "invoke":
        return element.attrib.get("name", "").strip()
    for tag_name in ("ml_tool_name", "tool_name"):
        tool_name_element = element.find(tag_name)
        if tool_name_element is not None:
            return _leaf_text(tool_name_element)
    return ""


def _extract_arguments(element: ET.Element) -> dict[str, object] | None:
    if _local_name(element.tag) == "invoke":
        parameters: dict[str, object] = {}
        parameter_children = [
            child
            for child in list(element)
            if isinstance(child.tag, str) and _local_name(child.tag) == "parameter"
        ]
        for child in parameter_children:
            key = child.attrib.get("name", "").strip()
            if key:
                _append_value(parameters, key, _xml_value_to_object(child))
        return parameters

    for tag_name in ("ml_parameters", "parameters"):
        parameters_element = element.find(tag_name)
        if parameters_element is not None:
            parsed = _xml_value_to_object(parameters_element)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
    return None


def _build_tool_call(name: str, arguments: dict[str, object], index: int) -> dict[str, object]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "index": index,
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _parse_tool_call_element(
    element: ET.Element,
    allowed_tool_names: set[str] | None,
    index: int,
) -> dict[str, object] | None:
    if _local_name(element.tag) not in {"invoke", "tool_call", "ml_tool_call"}:
        return None

    tool_name = _extract_tool_name(element)
    if not tool_name:
        return None
    if not _is_allowed_tool_name(tool_name, allowed_tool_names):
        return None

    arguments = _extract_arguments(element)
    if arguments is None:
        return None

    return _build_tool_call(tool_name, arguments, index)


def _extract_malformed_tool_call_from_root(
    root: ET.Element,
    allowed_tool_names: set[str] | None,
    index: int,
) -> dict[str, object] | None:
    root_name = _local_name(root.tag)
    if root_name not in {"tool_calls", "ml_tool_calls"}:
        return None

    tool_name = _extract_tool_name(root)
    if not tool_name:
        return None
    if not _is_allowed_tool_name(tool_name, allowed_tool_names):
        return None

    for tag_name in ("ml_parameters", "parameters"):
        parameters_element = root.find(tag_name)
        if parameters_element is not None:
            parsed = _xml_value_to_object(parameters_element)
            arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
            return _build_tool_call(tool_name, arguments, index)

    names = [match.group(1).strip() for match in PARAM_NAME_TAG_PATTERN.finditer(ET.tostring(root, encoding="unicode"))]
    values = [match.group(1).strip() for match in PARAM_VALUE_TAG_PATTERN.finditer(ET.tostring(root, encoding="unicode"))]
    if names and values and len(names) == len(values):
        arguments = {
            key: _coerce_leaf_value(value)
            for key, value in zip(names, values, strict=False)
            if key
        }
        return _build_tool_call(tool_name, arguments, index)
    if names and not values:
        return None

    direct_pairs: dict[str, object] = {}
    children = [child for child in list(root) if isinstance(child.tag, str)]
    for child in children:
        key = _local_name(child.tag)
        if key in {"tool_name", "ml_tool_name", "tool_call", "ml_tool_call"}:
            continue
        if key in {"param_name", "param_value"}:
            continue
        direct_pairs[key] = _xml_value_to_object(child)
    if direct_pairs:
        return _build_tool_call(tool_name, direct_pairs, index)
    return None


def _parse_xml_block(
    block: str,
    allowed_tool_names: set[str] | None,
    start_index: int,
) -> tuple[list[dict[str, object]], tuple[int, int] | None]:
    try:
        root = ET.fromstring(_normalize_dsml_to_xml(block))
    except ET.ParseError:
        # P1 修复：DSML 解析失败时尝试更多修复策略
        # 策略 1: 移除 CDATA 内容中的非法 XML 字符
        try:
            cleaned = block
            # 移除 XML 非法字符 (除了 \t \n \r)
            cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cleaned)
            root = ET.fromstring(_normalize_dsml_to_xml(cleaned))
        except ET.ParseError:
            # 策略 2: 提取所有 CDATA 内容，尝试正则解析工具调用
            try:
                fallback_calls = _regex_fallback_parse_tool_calls(block, allowed_tool_names)
                if fallback_calls:
                    return fallback_calls, (start_index, start_index + len(block))
            except Exception:
                pass
            return [], None

    root_name = _local_name(root.tag)
    if root_name in {"tool_calls", "ml_tool_calls"}:
        candidates = [
            child
            for child in list(root)
            if isinstance(child.tag, str) and _local_name(child.tag) in {"invoke", "tool_call", "ml_tool_call"}
        ]
    elif root_name in {"tool_call", "ml_tool_call"}:
        candidates = [root]
    else:
        return [], None

    tool_calls: list[dict[str, object]] = []
    for candidate in candidates:
        parsed = _parse_tool_call_element(candidate, allowed_tool_names, len(tool_calls))
        if parsed is not None:
            tool_calls.append(parsed)

    if not tool_calls:
        malformed = _extract_malformed_tool_call_from_root(root, allowed_tool_names, 0)
        if malformed is not None:
            tool_calls.append(malformed)

    if not tool_calls:
        return [], None
    return tool_calls, (start_index, start_index + len(block))


def _mask_code_fences(text: str) -> str:
    masked = list(text)
    for match in CODE_FENCE_PATTERN.finditer(text):
        for index in range(match.start(), match.end()):
            masked[index] = " "
    return "".join(masked)


def _find_matching_block(
    masked_text: str,
    start_match: re.Match[str],
    *,
    allow_trailing_close: bool = False,
) -> tuple[int, int] | None:
    tag_name = start_match.group("tag").lower()
    if tag_name == "|dsml|tool_calls":
        closing_pattern = DSML_TOOL_CALLS_TRAILING_CLOSE_PATTERN if allow_trailing_close else DSML_TOOL_CALLS_CLOSE_PATTERN
    else:
        closing_pattern = re.compile(rf"</{re.escape(tag_name)}\s*>", re.IGNORECASE)
    closing_match = closing_pattern.search(masked_text, start_match.end())
    if closing_match is None:
        return None
    return start_match.start(), closing_match.end()


def _extract_tool_blocks(
    text: str,
    allowed_tool_names: set[str] | None,
    *,
    allow_trailing_close: bool = False,
) -> tuple[list[tuple[int, int]], list[dict[str, object]]]:
    masked_text = _mask_code_fences(text)
    spans: list[tuple[int, int]] = []
    tool_calls: list[dict[str, object]] = []
    cursor = 0

    while cursor < len(masked_text):
        match = START_TAG_PATTERN.search(masked_text, cursor)
        if match is None:
            break
        span = _find_matching_block(masked_text, match, allow_trailing_close=allow_trailing_close)
        if span is None:
            break

        start, end = span
        block_calls, parsed_span = _parse_xml_block(text[start:end], allowed_tool_names, start)
        if parsed_span is not None and block_calls:
            for offset, tool_call in enumerate(block_calls, start=len(tool_calls)):
                tool_call["index"] = offset
            spans.append(parsed_span)
            tool_calls.extend(block_calls)
            cursor = end
            continue
        if match.group("tag").lower() in {"|dsml|tool_calls", "tool_calls", "ml_tool_calls", "ml_tool_call"}:
            spans.append((start, end))
            cursor = end
            continue

        cursor = match.end()

    return spans, tool_calls


def _remove_spans(text: str, spans: list[tuple[int, int]], *, trim_outer_whitespace: bool = True) -> str:
    if not spans:
        cleaned = TOOL_RESULT_PATTERN.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip() if trim_outer_whitespace else cleaned

    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    cleaned = "".join(parts)
    cleaned = TOOL_RESULT_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() if trim_outer_whitespace else cleaned


def _find_unmatched_fence_start(text: str) -> int | None:
    last_open = None
    cursor = 0
    while True:
        index = text.find("```", cursor)
        if index == -1:
            break
        if last_open is None:
            last_open = index
        else:
            last_open = None
        cursor = index + 3
    return last_open


def _find_incomplete_block_start(text: str, *, allow_trailing_close: bool = False) -> int | None:
    masked_text = _mask_code_fences(text)
    cursor = 0
    while cursor < len(masked_text):
        match = START_TAG_PATTERN.search(masked_text, cursor)
        if match is None:
            break
        span = _find_matching_block(masked_text, match, allow_trailing_close=allow_trailing_close)
        if span is None:
            return match.start()
        cursor = span[1]
    return None


def _find_partial_tag_start(text: str) -> int | None:
    lowered_text = text.lower()
    pipe_tag_start = lowered_text.rfind("<|")
    if pipe_tag_start != -1 and ">" not in lowered_text[pipe_tag_start:]:
        return pipe_tag_start
    for hint in TAG_NAME_HINTS:
        lowered_hint = hint.lower()
        max_overlap = min(len(hint), len(text))
        for size in range(max_overlap, 0, -1):
            if lowered_text.endswith(lowered_hint[:size]):
                return len(text) - size
    return None


def _looks_like_tool_markup_fragment(text: str) -> bool:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped:
        return False
    if lowered.startswith("<|dsml|") or lowered.startswith("</|dsml|") or lowered.startswith("<|/dsml"):
        return True
    if stripped.startswith("<ml_") or stripped.startswith("</ml_"):
        return True
    if stripped.startswith("<tool_") or stripped.startswith("</tool_"):
        return True
    if stripped.startswith("<invoke") or stripped.startswith("</invoke"):
        return True
    if stripped.startswith("<parameter") or stripped.startswith("</parameter"):
        return True
    if stripped.startswith("<m") and any(token in stripped for token in ("ml_", "tool_", "tool_calls", "tool_result")):
        return True
    return False


def _split_stream_text(
    text: str,
    allowed_tool_names: set[str] | None,
    final: bool,
) -> tuple[str, str, list[dict[str, object]]]:
    hold_from_candidates = [
        index
        for index in (_find_unmatched_fence_start(text), _find_incomplete_block_start(text, allow_trailing_close=final))
        if index is not None
    ]

    if not final:
        partial_start = _find_partial_tag_start(text)
        if partial_start is not None:
            hold_from_candidates.append(partial_start)

    if final:
        safe_end = min(hold_from_candidates) if hold_from_candidates else len(text)
    elif hold_from_candidates:
        safe_end = min(hold_from_candidates)
    else:
        safe_end = len(text)

    processable = text[:safe_end]
    remainder = text[safe_end:]
    spans, tool_calls = _extract_tool_blocks(processable, allowed_tool_names, allow_trailing_close=final)
    visible = _remove_spans(processable, spans, trim_outer_whitespace=final)
    return visible, remainder, tool_calls


def parse_tool_calls_from_text(text: str, allowed_tool_names: set[str] | None = None) -> tuple[str, list[dict[str, object]]]:
    if not text:
        return "", []
    spans, tool_calls = _extract_tool_blocks(text, allowed_tool_names, allow_trailing_close=True)
    return _remove_spans(text, spans), tool_calls


@dataclass
class StreamingToolParser:
    pending_text: str = ""
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    allowed_tool_names: set[str] | None = None

    def consume(self, chunk: str) -> str:
        if not chunk:
            return ""
        self.pending_text += chunk
        visible, remainder, parsed_calls = _split_stream_text(
            self.pending_text,
            allowed_tool_names=self.allowed_tool_names,
            final=False,
        )
        self.pending_text = remainder
        self.tool_calls.extend(parsed_calls)
        return visible

    def flush(self) -> tuple[str, list[dict[str, object]]]:
        visible, remainder, parsed_calls = _split_stream_text(
            self.pending_text,
            allowed_tool_names=self.allowed_tool_names,
            final=True,
        )
        self.pending_text = ""
        self.tool_calls.extend(parsed_calls)
        tail = "" if _looks_like_tool_markup_fragment(remainder) else remainder
        return (visible + tail).strip(), self.tool_calls


def _regex_fallback_parse_tool_calls(
    block: str,
    allowed_tool_names: set[str] | None,
) -> list[dict[str, object]]:
    """P1 修复：DSML XML 解析失败时的正则兜底解析。

    当 XML 解析器无法解析 DSML 块时，用正则表达式提取工具调用信息。
    提取模式：
    - invoke name="xxx" → 工具名
    - parameter name="yyy" → 参数名
    - CDATA[...] → 参数值
    """
    import json as _json
    tool_calls: list[dict[str, object]] = []

    # 提取所有 invoke name="xxx" 块
    invoke_pattern = re.compile(
        r'invoke\s+name="([^"]+)"(.*?)(?=invoke\s+name=|</.*?tool_calls|$)',
        re.DOTALL,
    )
    for invoke_match in invoke_pattern.finditer(block):
        tool_name = invoke_match.group(1).strip()
        if allowed_tool_names and tool_name not in allowed_tool_names:
            continue

        invoke_body = invoke_match.group(2)

        # 提取所有 parameter name="xxx" 的 CDATA 值
        params: dict[str, object] = {}
        param_pattern = re.compile(
            r'parameter\s+name="([^"]+)"\s*>\s*<!\[CDATA\[(.*?)\]\]>',
            re.DOTALL,
        )
        for param_match in param_pattern.finditer(invoke_body):
            param_name = param_match.group(1).strip()
            param_value = param_match.group(2).strip()
            # 尝试解析为 JSON
            try:
                params[param_name] = _json.loads(param_value)
            except (json.JSONDecodeError, ValueError):
                params[param_name] = param_value

        tool_calls.append({
            "type": "function",
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "function": {
                "name": tool_name,
                "arguments": _json.dumps(params, ensure_ascii=False),
            },
        })

    return tool_calls
