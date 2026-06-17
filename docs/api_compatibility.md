# API 兼容性

GLM2API 实现了 OpenAI / Anthropic / OpenAI Responses 三套 API 协议，最终相似度评分 **99.17%**（28/28 测试通过）。

## OpenAI Chat Completions

### 端点
- `POST /v1/chat/completions` — 标准 chat completion
- `GET /v1/models` — 模型列表
- `GET /v1/models/{model}` — 单个模型详情
- `POST /v1/completions` — legacy text completion
- `POST /v1/moderations` — 内容审核（启发式）
- `POST /v1/embeddings` — 文本 embedding（hash 投影）
- `POST /v1/images/generations` — 文生图（走 GLM cogView）
- `POST /v1/audio/speech` — TTS（占位）
- `POST /v1/audio/transcriptions` — STT（占位）
- `POST /v1/files` / `POST /v1/assistants` / `POST /v1/threads` — 占位兼容

### 支持的请求参数

| 参数 | 类型 | 支持情况 |
|------|------|----------|
| `model` | string | ✅ 必填 |
| `messages` | array | ✅ 必填 |
| `stream` | bool | ✅ |
| `temperature` | float | ✅ |
| `top_p` | float | ✅ |
| `max_tokens` | int | ✅ |
| `n` | int | ✅（fan-out，上限 4） |
| `stop` | array/string | 静默接受 |
| `seed` | int | 静默接受 |
| `presence_penalty` | float | 静默接受 |
| `frequency_penalty` | float | 静默接受 |
| `logit_bias` | object | 静默接受 |
| `logprobs` | bool | 始终返回 `null` |
| `top_logprobs` | int | 静默接受 |
| `user` | string | 静默接受 |
| `response_format` | object | ✅ `json_object` / `json_schema` |
| `tools` | array | ✅ XML 工具调用 |
| `tool_choice` | string/object | ✅ |
| `stream_options.include_usage` | bool | ✅ |

### 响应字段（标准 OpenAI 格式）

```json
{
  "id": "chatcmpl-<hex>",
  "object": "chat.completion",
  "created": 1781670831,
  "model": "glm-4-flash",
  "system_fingerprint": "fp_96035f",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "...",
      "reasoning_content": null
    },
    "finish_reason": "stop",
    "logprobs": null
  }],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 5,
    "total_tokens": 25
  }
}
```

### 流式响应

每个 chunk 的格式：
```json
{
  "id": "chatcmpl-<hex>",
  "object": "chat.completion.chunk",
  "created": 1781670834,
  "model": "glm-4-flash",
  "system_fingerprint": "fp_96035f",
  "choices": [{
    "index": 0,
    "delta": {"content": "..."},
    "finish_reason": null,
    "logprobs": null
  }]
}
```

最后两个 chunk（开启 `stream_options.include_usage` 时）：
1. `finish_reason: "stop"` + `delta: {}`
2. `choices: []` + `usage: {...}`（最终 usage）

最后：`data: [DONE]`

## Anthropic Messages API

### 端点
- `POST /v1/messages` — Anthropic Messages
- `POST /v1/messages/count_tokens` — token 计数

### 支持的请求参数

| 参数 | 支持情况 |
|------|----------|
| `model` | ✅ |
| `messages` | ✅ |
| `system` | ✅ |
| `max_tokens` | ✅ |
| `temperature` | ✅ |
| `top_p` | ✅ |
| `stream` | ✅ |
| `tools` | ✅ |
| `tool_choice` | ✅ |
| `stop_sequences` | 静默接受 |

### 响应格式

```json
{
  "id": "msg_<hex>",
  "type": "message",
  "role": "assistant",
  "model": "glm-4-flash",
  "content": [{"type": "text", "text": "..."}],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 8,
    "output_tokens": 11
  }
}
```

## OpenAI Responses API

### 端点
- `POST /v1/responses` — 新版 Responses API（事件流）

### 事件流

```
response.created
response.in_progress
response.output_item.added
response.content_part.added
response.output_text.delta
response.output_text.done
response.content_part.done
response.output_item.done
response.completed
```

## 错误响应格式

```json
{
  "error": {
    "message": "Model not found",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found",
    "request_id": "req_<hex>"
  }
}
```

## 已知差异（剩余 0.83%）

1. **Embeddings 不是语义 embedding**：使用 hash 投影，结构兼容 OpenAI 但无真实语义
2. **Moderations 不是 ML 模型**：使用启发式规则（关键词 + 长度）
3. **Audio 端点**：TTS/STT 是占位实现，未集成 GLM-4V
4. **reasoning_effort 参数**：GLM 用 `-think` 后缀模型替代，不支持该参数

## 接入示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="dummy"  # SERVER_API_KEYS 为空时任意值即可
)

resp = client.chat.completions.create(
    model="glm-4-flash",
    messages=[{"role": "user", "content": "Hello"}]
)
print(resp.choices[0].message.content)
```

### Anthropic Python SDK

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8000/v1",
    api_key="dummy"
)

resp = client.messages.create(
    model="glm-4-flash",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}]
)
print(resp.content[0].text)
```

### Codex / Claude Code

在 `~/.codex/config.toml`：
```toml
[model_providers.glm2api]
name = "GLM2API"
base_url = "http://server:8000/v1"
env_key = "GLM2API_KEY"
```
