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
