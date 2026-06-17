# Codex CLI 集成指南

> 本文档针对 OpenAI Codex CLI v0.140.0+ 与 glm2api 的集成。

## 必读：wire_api 必须为 responses

Codex CLI v0.140.0 起**强制要求** `wire_api = "responses"`，旧版的 `wire_api = "chat"` 配置直接报错。glm2api 项目同时支持 `/v1/chat/completions` 和 `/v1/responses` 端点，正好对应 codex 新版要求。

**正确配置：**

```toml
# ~/.codex/config.toml
model = "glm-5.2"               # 或 glm-5.2-flash / glm-5.1 等
model_provider = "glm2api"
approval_policy = "never"
sandbox_mode = "workspace-write"

[model_providers.glm2api]
name = "glm2api"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"           # ← 必须是 responses，不能是 chat
env_key = "GLM2API_KEY"
```

**启动 codex：**

```bash
# glm2api 默认不开启鉴权（SERVER_API_KEYS 为空时任意 key 都行）
GLM2API_KEY=sk-test-not-needed codex exec --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox --json '<your prompt>'
```

## 已知限制

### 1. 长 agentic 任务可能失败

GLM 模型（包括 GLM-5.1 / GLM-5.2）在面对长 prompt + 多工具调用场景时，**偶发复读模式**：模型把同一句话重复 20+ 次而不触发 `function_call`，导致 codex agent 无法继续。

**已修复（v0.2+）**：glm2api 在 `chat_completion` 内置复读检测 + 自动重试 1 次。检测规则：
- 文本长度 ≥ 100 字符才检测
- 同一句子重复 ≥ 5 次判定为复读
- 连续 3 句相同判定为复读
- 触发复读后自动重新请求一次（用相同 payload）

**仍可能失败的场景**：模型连续两次都复读。建议用户：
- 把长任务拆成多个小步骤分别发请求
- 简化 prompt，降低工具数量
- 用 `glm-5.2-flash` 替代 `glm-5.1`（轻量版更稳定）

### 2. Codex 元数据 fallback

Codex v0.140.0 不识别 `glm-5.x` 模型名，使用 fallback metadata，导致：
- `usage` 字段可能显示为 0（实际有 token 消耗）
- system prompt 注入可能与模型期望格式不完全一致

**手动配置元数据消除 fallback warning**（推荐）：

```toml
[model_providers.glm2api]
name = "glm2api"
base_url = "http://127.0.0.1:8000/v1"
wire_api = "responses"
env_key = "GLM2API_KEY"
# 手动指定元数据，消除 codex 的 'Model metadata not found' warning
model_context_window = 128000
model_max_output_tokens = 4096
```

**已修复（v0.2+）**：glm2api 在 Responses 流式响应的 `response.completed` 事件里加了 token 估算兜底，即使上游 usage chunk 解析失败，也会从已累积文本长度估算 token 数。

### 3. 流式 usage 字段

Responses 流式响应的 `usage` 字段在历史版本里全是 0（审核报告 P4 问题）。现已修复：
- **优先**用上游 chunk 里的 usage（如果存在且非零）
- **兜底**用文本长度估算（每 ~4 字符 1 token）
- 实测：上游无 usage 时估算输出（input=1/output=3/total=4），上游有 usage 时优先用真实值

### 4. 并发场景日志噪声

并发 + `GLM_DELETE_CONVERSATION=true` 时，会话删除会偶发返回 `conversation 不存在`（因为另一并发请求已删了同一会话）。**已修复（v0.2+）**：对这类响应降级到 DEBUG 级别，不再产生 WARNING 噪声。

## 推荐模型

| 场景 | 推荐模型 | 说明 |
|------|---------|------|
| 短任务（创建文件、运行命令） | `glm-5.2-flash` | 1-2 秒延迟，稳定 |
| 中等任务（6 步文件操作） | `glm-5.1` | glm-5.2 在中等任务偶有跳步倾向，glm-5.1 更稳定 |
| 长 agentic 任务 | `glm-5.2` + 任务拆小 | 直接长 prompt 可能触发复读（已加流式复读检测，触发后自动发 error 让 codex 重试） |
| 思维链推理 | `glm-5.2-think` | 返回 content + reasoning_content |
| 联网搜索 | `glm-5.2-search` | 自动调用搜索 |
| 高并发场景 | `glm-5.2-flash` | 轻量版并发能力强，P3 修复后 8 并发 0 WARNING |

## 流式复读检测（v0.3+ 新增）

为解决 codex CLI 长 agentic 任务复读问题，glm2api 在 `stream_chat_completion` 路径加了 buffer + 复读检测：
- 流式响应先 buffer 240 字符
- 达到阈值后检测复读（连续 3 句相同 / 同句重复 5 次）
- 检测到复读 → 丢弃已 buffer 的 content，发送 OpenAI 兼容 error chunk + [DONE]
- codex 等客户端收到 error 后自动重试
- 不复读 → flush buffer，恢复正常流式

**实测效果**：codex 长任务场景下，glm-5.2 复读文本不再被部分输出后才报错，而是在 240 字符内就被切断，codex 收到 clean error 后立即重试。

**管理面板可观察**：仪表盘 KPI 显示「复读检测触发次数」，按模型/路径分组统计。

**端到端验证**（v3 审核报告 P1-3）：通过 mock 上游 SSE 返回复读文本，5 个端到端测试覆盖：
- 复读文本触发 error chunk ✅
- 正常长文本不误杀 ✅
- 短文本不误杀 ✅
- 240+ 唯一文本不误杀 ✅
- 触发事件记录到 admin store ✅

## 屏蔽上游沙箱工具（v0.4+ 新增，v3 审核报告 M4 修复）

**问题**：GLM-5.2 倾向调用 chatglm.cn 上游自带的 `execute_sandbox_code` 沙箱工具，而不是客户端（如 codex）提供的 shell 工具。这导致 codex 收到 0 个 OpenAI tool_calls，无法继续工作流。

**修复**：把以下上游沙箱工具加入 `BLOCKED_NATIVE_TOOL_NAMES` 黑名单：
- `execute_sandbox_code`
- `execute_code`
- `sandbox_code`
- `code_sandbox`
- `run_code`
- `code_interpreter`
- `python_interpreter`

**Prompt 注入强化**：在 `_legacy_build_tool_call_instructions` 里明确告诉 GLM：
> You do NOT have access to upstream sandbox tools like `execute_sandbox_code`, `execute_code`, `code_interpreter`, `sandbox_code`, or `run_code`. These tools are blocked. Always use the client-provided tools listed below for any code execution or file operation.

**响应解析强化**：`GLMEventAccumulator` 在解析 server-side tool_calls 时，丢弃任何在黑名单里的工具名，不传给客户端。

**效果**：GLM-5.2 必须使用客户端提供的工具（如 codex 的 shell），不再绕道走上游沙箱。

## 已知限制（v3 审核报告 M3/M5）

### M3 GLM-5.2 bash heredoc 引号转义 bug

**现象**：codex 长任务中 GLM-5.2 生成的 bash 命令引号转义错误（如 `#"'!/usr/bin/env python3`、`f"#{todo['"'id']}"`），导致文件创建失败。

**v5 修复（项目侧）**：`translator.py` 新增 `_fix_bash_quote_escaping()` 函数，在 `sanitize_tool_call_payload` 里对 shell 命令做后处理：
- `#"'!` → `#!`（shebang 行转义修复）
- `'"'` → `'`（单引号被双引号包裹修复）
- `\"'` → `'`（转义单引号修复）

实测 6 个测试用例全部通过，包括 v5 报告中的真实 bug 模式。

**仍需注意**：这是 GLM-5.2 模型本身的 shell 命令生成能力问题，项目侧的 sanitize 逻辑只能修复已知的 3 种转义模式。如果 GLM 生成新的转义错误模式，需要扩展 `_fix_bash_quote_escaping` 函数。

### M5 codex 长任务稳定性仍不足

**现象**：codex 长 agentic 任务（多文件构建 + pytest 迭代）仍不可靠。

**v5 改进**：
- 服务器内部重试从 1 次增加到 3 次（`MAX_ATTEMPTS = 3`）
- 每次重试时微调 prompt：插入 `IMPORTANT RETRY HINT: Your previous response did not call any tool...`
- 全部重试失败后兜底返回原始响应（不 500 断连）

**建议**：
- 短任务稳定性 10/10
- 中等任务用 `glm-5.1`
- 长任务拆成多个小 prompt 分别发请求
- 等 GLM-5.2 模型侧改进工具调用稳定性

## 排查工具

### 1. 开启 DEBUG_DUMP_ALL

```bash
# .env 文件
DEBUG_DUMP_ALL=true
```

会在 `log/glm2api_debug.log` 写入所有上游请求/响应原文，是排查兼容性问题的利器。

### 2. 检查管理面板

访问 `http://127.0.0.1:8000/admin`：
- **请求日志**：看每个请求的 status / latency / model
- **轮换事件**：device_id 是否频繁轮换（风控严时轮换多）
- **模型**：探针测试每个模型是否可用

### 3. 手动 curl 测试

```bash
# 测试 Responses 流式
curl -N http://127.0.0.1:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","input":"Count from 1 to 5","stream":true}'

# 测试工具调用
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model":"glm-5.2",
    "messages":[{"role":"user","content":"What is the weather in Beijing?"}],
    "tools":[{"type":"function","function":{
      "name":"get_weather",
      "description":"Get current weather",
      "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}
    }}],
    "max_tokens":200
  }'
```

## 动态模型发现

glm2api v0.3+ 支持**动态模型发现**：每次 chat 请求成功后，自动从上游响应的 `model` 字段提取真实模型名（如 `GLM-5.2`），归一化（`glm-5.2`）后加入"动态发现池"，并自动派生 `-think` / `-search` / `-think-search` 变体。

**好处**：未来 chatglm.cn 升级到 GLM-5.3 时，**不需要改代码**，只要发一次请求，新模型就自动出现在 `/v1/models` 列表里。

**查看动态发现**：管理面板「模型」页 KPI 卡片显示「动态发现模型数」，并在表格中标注哪些是动态发现的。

**手动重置**：调用 `POST /admin/api/upstream_refresh` 会重新发现，但不会清空已发现的模型池（动态池只在进程重启时清空）。
