# Dockerfile for glm2api - 用于 Render Docker 部署（可选）
# Render 也支持直接 Python 部署（见 render.yaml），Dockerfile 是备选方案

FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 复制项目文件
COPY pyproject.toml main.py ./
COPY src/ ./src/
COPY configs/ ./configs/
COPY docs/ ./docs/

# 安装依赖
RUN pip install --no-cache-dir -e .

# 暴露端口（Render 会注入 PORT 环境变量）
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8000}/health')" || exit 1

# 启动命令
# Render 会注入 PORT 环境变量，HOST 必须是 0.0.0.0
CMD ["python", "main.py"]
