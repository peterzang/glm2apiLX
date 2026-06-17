# 项目架构

## 目录结构

```
glm2api/
├── .github/                    # GitHub CI 配置
│   └── workflows/
│       └── ci.yml              # pytest + import 验证
│
├── configs/                    # 配置示例目录
│   ├── env.example             # 环境变量模板（复制为 .env 使用）
│   └── glm2api.service         # systemd 服务单元文件
│
├── docs/                       # 项目文档
│   ├── README.md
│   ├── architecture.md
│   ├── api_compatibility.md
│   ├── admin_panel.md
│   └── deployment.md
│
├── scripts/                    # 运维脚本
│   ├── start.sh                # 后台启动服务
│   ├── stop.sh                 # 停止服务
│   └── status.sh               # 查看运行状态
│
├── src/                        # 源码（src-layout）
│   └── glm2api/
│       ├── __init__.py
│       ├── __main__.py         # 命令行入口
│       ├── main.py             # 启动脚本（打印 banner）
│       ├── app.py              # 应用编排层
│       ├── server.py           # HTTP 服务器 + 路由
│       ├── config.py           # 配置加载与校验
│       ├── logging_utils.py    # 日志工具
│       │
│       ├── core/               # 核心层：协议无关的纯逻辑
│       │   ├── model_profiles.py   # 模型元信息（context window、能力）
│       │   ├── model_variants.py   # 模型变体展开（think / search 后缀）
│       │   ├── openai_compat.py    # OpenAI 标准响应构造（ID/usage/fingerprint）
│       │   └── tokenizer.py        # token 计数（近似估算）
│       │
│       ├── services/           # 服务层：上游对接 + 协议适配
│       │   ├── glm_auth.py         # 游客 token / device_id 管理
│       │   ├── glm_client.py       # 上游 HTTP 客户端 + 失败转移
│       │   ├── translator.py       # OpenAI ↔ GLM 双向转换
│       │   ├── anthropic_adapter.py# Anthropic Messages API 适配
│       │   └── responses_adapter.py# OpenAI Responses API 适配
│       │
│       ├── protocol/           # 协议层：工具调用解析
│       │   ├── tool_parser.py      # 流式 / 非流式 XML 工具调用解析
│       │   └── tool_protocol.py    # 工具协议定义 + 注入提示词
│       │
│       └── admin/              # 管理面板
│           ├── api.py              # /admin/api/* JSON 端点
│           ├── store.py            # 内存状态存储（请求日志 / 指标）
│           └── static/             # 单页应用前端
│               ├── index.html
│               ├── app.js
│               ├── style.css
│               └── favicon.svg
│
├── tests/                      # 测试
│   ├── test_model_variants.py
│   ├── test_protocol_adapters.py
│   ├── test_tool_parser.py
│   └── test_translator.py
│
├── .gitignore
├── .python-version
├── LICENSE
├── README.md
├── main.py                     # 项目根入口（引用 src/glm2api）
├── pyproject.toml
└── uv.lock
```

## 分层架构

```
┌─────────────────────────────────────────────┐
│  HTTP Layer (server.py)                     │
│  - BaseHTTPRequestHandler 路由分发           │
│  - /v1/chat/completions                     │
│  - /v1/messages (Anthropic)                 │
│  - /v1/responses                            │
│  - /admin/* (管理面板)                      │
└────────────┬────────────────────────────────┘
             │
┌────────────▼────────────────────────────────┐
│  Adapter Layer (services/)                  │
│  - OpenAI → GLM (translator.py)             │
│  - Anthropic → GLM (anthropic_adapter.py)   │
│  - Responses → GLM (responses_adapter.py)   │
└────────────┬────────────────────────────────┘
             │
┌────────────▼────────────────────────────────┐
│  Client Layer (glm_client.py)               │
│  - 上游 HTTP 请求 + SSE 解析                │
│  - 多账号失败转移                           │
│  - 请求排队（GLM_MAX_CONCURRENCY）          │
└────────────┬────────────────────────────────┘
             │
┌────────────▼────────────────────────────────┐
│  Auth Layer (glm_auth.py)                   │
│  - 游客 token 池                            │
│  - device_id 池 + 主动轮换                  │
│  - refresh_token 续期                       │
└────────────┬────────────────────────────────┘
             │
┌────────────▼────────────────────────────────┐
│  Core Layer (core/)                         │
│  - 模型元信息 / 变体展开                    │
│  - OpenAI 标准响应构造                      │
│  - token 计数                               │
└─────────────────────────────────────────────┘
```

## 关键设计决策

### 1. src-layout
采用 PEP 517/518 推荐的 `src/` 布局，避免测试时误导入工作目录而非安装包。

### 2. 按"职责"而非"协议"分包
- `core/` 是协议无关的纯逻辑层（无 IO）
- `services/` 是 IO 层（HTTP + 上游对接）
- `protocol/` 单独抽出工具调用解析，因为它在 services 和 core 之间被复用
- `admin/` 自包含（前后端代码 + 静态资源同包，方便部署）

### 3. 单进程多账号
所有账号共享一个 `GLMWebClient`，通过 thread-local 跟踪当前使用的账号索引，避免锁竞争。

### 4. device_id 主动轮换
每个 device_id 用 N 次后预换（默认 8），把风控窗口前的请求都打到新 device_id，而不是被动等风控才换。

### 5. 管理面板零依赖
前端用原生 HTML + JS + CSS，不依赖 React/Vue/jQuery，可在内网离线环境直接打开。
