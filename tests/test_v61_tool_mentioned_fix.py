"""v61 修复：GLM '只说不做'循环 — 工具名提及但无 DSML block 检测。

v60 的循环短语检测没生效（每个 response 只有 1 个短语，阈值 ≥2 不触发）。
v61 换策略：检测"文本提及工具名 + 动作动词但无 DSML block"——这是"只说不做"的铁证。
"""
from glm2api.services.translator import GLMEventAccumulator
from glm2api.protocol.tool_protocol import build_tool_call_instructions


# === 用户实际遇到的循环文本 ===

def test_tool_mentioned_but_no_dsml_english_1():
    """用户输出：I have all the research I need. Writing the game file now."""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write", "Read"})
    acc._cached_full_text = "I have all the research I need. Writing the game file now."
    assert acc._is_tool_mentioned_but_not_called(), "应检测到'只说不做'"


def test_tool_mentioned_but_no_dsml_english_2():
    """用户输出：I have enough research. Writing the game file now."""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "I have enough research. Writing the game file now."
    assert acc._is_tool_mentioned_but_not_called()


def test_tool_mentioned_but_no_dsml_english_3():
    """用户输出：I'll now write the complete game file with Write."""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "I'll now write the complete game file with Write."
    assert acc._is_tool_mentioned_but_not_called()


def test_tool_mentioned_but_no_dsml_chinese_1():
    """用户输出：现在创建完整的游戏文件。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "现在创建完整的游戏文件。"
    assert acc._is_tool_mentioned_but_not_called()


def test_tool_mentioned_but_no_dsml_chinese_2():
    """用户输出：现在编写完整的游戏文件。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "现在编写完整的游戏文件。"
    assert acc._is_tool_mentioned_but_not_called()


def test_tool_mentioned_but_no_dsml_chinese_3():
    """用户输出：现在写入完整的游戏文件。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "现在写入完整的游戏文件。"
    assert acc._is_tool_mentioned_but_not_called()


def test_tool_mentioned_but_no_dsml_repeated():
    """用户输出：多行重复（I have all the research I need. Writing the game file now. 重复多遍）"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = (
        "I have all the research I need. Writing the game file now. "
        "I have all the research I need. Writing the game file now. "
        "I have all the research I need. Writing the game file now."
    )
    assert acc._is_tool_mentioned_but_not_called()


# === 不应误杀的场景 ===

def test_not_triggered_when_dsml_present():
    """有 DSML block 时不应触发（GLM 在尝试调用工具）。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "I will write the file now. <|DSML|tool_calls><|DSML|invoke name=\"Write\">..."
    assert not acc._is_tool_mentioned_but_not_called()


def test_not_triggered_when_code_block_present():
    """有代码块时不应触发（GLM 在展示代码）。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "Here is the code:\n```html\n<html>...</html>\n```\nWriting the file now."
    assert not acc._is_tool_mentioned_but_not_called()


def test_not_triggered_when_no_tool_mentioned():
    """没有提及工具名时不应触发。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "I will create a beautiful game for you now. It will be amazing."
    assert not acc._is_tool_mentioned_but_not_called()


def test_not_triggered_when_text_too_short():
    """文本太短（<8 字符）时不应触发。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "Hi."
    assert not acc._is_tool_mentioned_but_not_called()


def test_not_triggered_when_no_action_verb():
    """没有动作动词时不应触发。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "The Write tool is used for writing files to disk. It takes a path and content."
    assert not acc._is_tool_mentioned_but_not_called()


def test_not_triggered_when_no_tools_provided():
    """没有提供工具时不应触发。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names=None)
    acc._cached_full_text = "Writing the file now."
    assert not acc._is_tool_mentioned_but_not_called()


# === prompt BAD/GOOD 示例验证 ===

def test_prompt_has_bad_good_example():
    """prompt 应有 BAD/GOOD 示例。"""
    prompt = build_tool_call_instructions(["Write"], set())
    assert "BAD" in prompt
    assert "GOOD" in prompt
    assert "does NOTHING" in prompt or "does nothing" in prompt.lower()


def test_prompt_good_example_has_dsml():
    """prompt 的 GOOD 示例应包含 DSML block。"""
    prompt = build_tool_call_instructions(["Write"], set())
    assert "<|DSML|tool_calls>" in prompt
    assert "<|DSML|invoke" in prompt
