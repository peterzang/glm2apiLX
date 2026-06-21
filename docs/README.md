# GLM2API 文档

本目录包含 GLM2API 项目的全部文档。

## 目录

| 文档 | 内容 |
|------|------|
| [architecture.md](./architecture.md) | 项目架构与目录结构说明 |
| [api_compatibility.md](./api_compatibility.md) | OpenAI / Anthropic API 兼容性对照表 |
| [admin_panel.md](./admin_panel.md) | 管理面板使用手册 |
| [deployment.md](./deployment.md) | 部署指南（systemd / Docker / 反向代理） |

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/LX-u0/glm2api.git
cd glm2api

# 2. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. 复制配置
cp configs/env.example .env
# 编辑 .env，至少设置 ADMIN_PASSWORD

# 4. 启动
./scripts/start.sh

# 5. 测试
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
```

打开浏览器访问 `http://127.0.0.1:8000/admin`，使用 `.env` 中的 `ADMIN_PASSWORD` 登录。

## Claude Code 终端版接入

glm2api 完整支持 Anthropic Messages API，可作为 Claude Code CLI 的后端。

### 快速接入

```bash
# 1. 在管理面板创建 API Key
#    访问 http://127.0.0.1:8000/admin → API 管理 → 创建 Key

# 2. 配置环境变量
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-glm2api-你的key
export ANTHROPIC_AUTH_TOKEN=sk-glm2api-你的key

# 3. 选择模型
#    短任务/快速回复：
export ANTHROPIC_MODEL=glm-5.2-flash
#    长任务/复杂编程（推荐）：
export ANTHROPIC_MODEL=glm-5.2-think

export ANTHROPIC_SMALL_FAST_MODEL=glm-5.2-flash

# 4. 可选优化
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1
export DISABLE_TELEMETRY=1
export DISABLE_ERROR_REPORTING=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

# 5. 启动 Claude Code
claude --dangerously-skip-permissions -p "你的任务"
```

### 无需手动设置的环境变量

以下环境变量 glm2api 已在网关层自动处理，用户无需设置：

- ~~`CLAUDE_CODE_ATTRIBUTION_HEADER=0`~~ — glm2api 自动剥掉 attribution block
- ~~count_tokens 端点~~ — glm2api 已实现 `POST /v1/messages/count_tokens`

### 长任务不断开

glm2api v34 修复了 Claude Code 长任务断开问题：
- `message_start` 立即发送（不等 GLM 第一个 chunk）
- `ping` 心跳（2 秒间隔，防止 GLM 思考时超时断开）
- 后台线程读上游（主线程可发心跳）

### Render 部署的 WAF 配置

如果部署在 Render 上，Cloudflare WAF 可能拦截含反引号的 prompt。解决方案：

1. **方案 A（开发用）**：Render Dashboard → Settings → Security → 关闭 WAF
2. **方案 B（生产用）**：自定义域名 + Cloudflare Dashboard 加 WAF 例外规则
   - 路径：Security → WAF → Managed Rules → Command Injection
   - 例外：`http.request.uri.path starts with "/v1/"`
3. **方案 C（临时）**：用 `.onrender.com` 直连绕过 Cloudflare
