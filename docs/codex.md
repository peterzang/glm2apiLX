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

**已修复（v0.2+）**：glm2api 在 Responses 流式响应的 `response.completed` 事件里加了 token 估算兜底，即使上游 usage chunk 解析失败，也会从已累积文本长度估算 token 数。

### 3. 流式 usage 字段

Responses 流式响应的 `usage` 字段在历史版本里全是 0（审核报告 P4 问题）。现已修复：
- 优先用上游 chunk 里的 usage（如果存在）
- 兜底用文本长度估算（每 ~4 字符 1 token）

### 4. 并发场景日志噪声

并发 + `GLM_DELETE_CONVERSATION=true` 时，会话删除会偶发返回 `conversation 不存在`（因为另一并发请求已删了同一会话）。**已修复（v0.2+）**：对这类响应降级到 DEBUG 级别，不再产生 WARNING 噪声。

## 推荐模型

| 场景 | 推荐模型 | 说明 |
|------|---------|------|
| 短任务（创建文件、运行命令） | `glm-5.2-flash` | 1-2 秒延迟，稳定 |
| 中等任务（6 步文件操作） | `glm-5.2` | 3-4 秒延迟，工具调用正常 |
| 长 agentic 任务 | `glm-5.2` + 任务拆小 | 直接长 prompt 可能触发复读 |
| 思维链推理 | `glm-5.2-think` | 返回 content + reasoning_content |
| 联网搜索 | `glm-5.2-search` | 自动调用搜索 |

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
