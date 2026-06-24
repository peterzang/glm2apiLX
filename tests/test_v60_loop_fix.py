"""v60 修复：GLM '只说不做'循环 — 只说'Writing file now'但不输出 DSML block。

根因：
GLM 反复说"I will write the file"/"Now writing"但从不输出 DSML tool_call block。
glm2api 的 _is_completion_summary 把这些当总结性文本放行，导致客户端收不到 tool_use，
继续等待，GLM 又生成同样文本——陷入循环。

修复：
1. prompt 加强"必须输出 DSML block"引导
2. _is_completion_summary 检测循环短语，多个循环短语时不判为总结
   → 让描述性检测拦截返回 error → 客户端重试打破循环
"""
from glm2api.services.translator import GLMEventAccumulator
from glm2api.protocol.tool_protocol import build_tool_call_instructions


# === P1: prompt 加强 DSML block 引导 ===

def test_prompt_emphasizes_dsml_block_required():
    """prompt 应强调必须输出 DSML block 才算调用工具。"""
    prompt = build_tool_call_instructions(["Write", "Read"], set())
    assert "MUST output the DSML block" in prompt or "must output" in prompt.lower()


def test_prompt_warns_narration_does_nothing():
    """prompt 应警告'只说不做'没用。"""
    prompt = build_tool_call_instructions(["Write"], set())
    # 应有语句说明"只说 Writing file now 不算调用工具"
    assert "does NOT call" in prompt or "does nothing" in prompt or "does not execute" in prompt


def test_prompt_dsml_is_only_way():
    """prompt 应说明 DSML block 是调用工具的唯一方式。"""
    prompt = build_tool_call_instructions(["Write"], set())
    assert "ONLY way" in prompt or "only way" in prompt.lower()


# === P2: _is_completion_summary 检测循环短语 ===

def test_completion_summary_not_triggered_for_loop_phrases():
    """包含多个循环短语的文本不应被判为总结性文本（应被描述性检测拦截）。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    # 模拟用户的"只说不做"循环文本
    acc._cached_full_text = (
        "I have enough technical reference. Now writing the file directly. "
        "Writing the complete game file now with Write. "
        "I'll stop calling tools and write the file now. "
        "Now writing the file. Creating the file now."
    )
    # 不应判为总结（因为有多个循环短语）
    assert not acc._is_completion_summary(), "循环短语文本不应判为总结性文本"


def test_completion_summary_still_works_for_real_summary():
    """正常的总结性文本仍应被判为总结。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "All files created successfully. The task is done."
    assert acc._is_completion_summary(), "正常总结应判为总结性文本"


def test_completion_summary_short_done_text():
    """短的完成文本应判为总结。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = "Done. File created successfully."
    assert acc._is_completion_summary()


def test_loop_phrases_chinese_detected():
    """中文循环短语也应被检测。"""
    acc = GLMEventAccumulator(model="glm-test", allowed_tool_names={"Write"})
    acc._cached_full_text = (
        "现在直接编写文件。直接写文件。停止调用工具。"
        "现在使用 Write 工具编写文件。现在我将直接编写文件。"
    )
    assert not acc._is_completion_summary(), "中文循环短语不应判为总结"


# === 验证正常请求不受影响 ===

def test_normal_tool_call_prompt_still_works():
    """正常工具调用的 prompt 不受影响。"""
    prompt = build_tool_call_instructions(["get_weather"], set())
    assert "get_weather" in prompt
    assert "Use the tools listed below" in prompt
    assert "<|DSML|tool_calls>" in prompt
