# 部署指南

## 方式一：systemd（推荐生产环境）

### 1. 准备目录

```bash
sudo mkdir -p /opt/glm2api /var/log/glm2api
sudo useradd -r -s /usr/sbin/nologin glm2api
sudo chown glm2api:glm2api /opt/glm2api /var/log/glm2api
```

### 2. 克隆代码

```bash
sudo -u glm2api git clone https://github.com/LX-u0/glm2api.git /opt/glm2api
cd /opt/glm2api
```

### 3. 创建虚拟环境

```bash
sudo -u glm2api python3.12 -m venv .venv
sudo -u glm2api .venv/bin/pip install -e .
```

### 4. 配置环境

```bash
sudo -u glm2api cp configs/env.example .env
sudo -u glm2api nano .env  # 至少修改 ADMIN_PASSWORD
```

### 5. 安装 systemd 服务

```bash
sudo cp configs/glm2api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now glm2api
sudo systemctl status glm2api
```

### 6. 查看日志

```bash
sudo journalctl -u glm2api -f
# 或
sudo tail -f /var/log/glm2api/server.log
```

## 方式二：直接运行

```bash
git clone https://github.com/LX-u0/glm2api.git
cd glm2api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

cp configs/env.example .env
# 编辑 .env

./scripts/start.sh
./scripts/status.sh
./scripts/stop.sh
```

## 方式三：Docker

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY . /app/

RUN python -m venv .venv && \
    .venv/bin/pip install --no-cache-dir -e .

EXPOSE 8000

CMD [".venv/bin/python", "main.py"]
```

### 构建与运行

```bash
docker build -t glm2api .
docker run -d \
  --name glm2api \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/token.txt:/app/token.txt:ro \
  --restart unless-stopped \
  glm2api
```

## 反向代理（Nginx）

```nginx
server {
    listen 443 ssl http2;
    server_name glm2api.example.com;

    ssl_certificate     /etc/letsencrypt/live/glm2api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/glm2api.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 必需
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    # 管理面板 IP 白名单（可选）
    location /admin {
        allow 10.0.0.0/8;
        allow 192.168.0.0/16;
        deny all;

        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }
}
```

## 验证部署

```bash
# 1. 健康检查
curl -s http://127.0.0.1:8000/health
# 期望：{"status":"ok"}

# 2. 模型列表
curl -s http://127.0.0.1:8000/v1/models | python -m json.tool

# 3. 真实请求
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-4-flash","messages":[{"role":"user","content":"hi"}]}' | jq

# 4. 管理面板
curl -s http://127.0.0.1:8000/admin/ -o /dev/null -w "%{http_code}\n"
# 期望：200
```

## 常见问题

### Q: 启动报 `Configuration error: 配置文件不是有效的 UTF-8 编码`
A: `.env` 文件包含 BOM 或非 UTF-8 字符，用 `file -i .env` 检查编码，重新保存为 UTF-8。

### Q: 上游返回 429
A: 触发风控，调低 `GLM_MAX_CONCURRENCY`，或调高 `GLM_DEVICE_ID_ROTATE_THRESHOLD`（如改成 5）。

### Q: 上游返回"请等待其他对话生成完毕"
A: 单账号同时占用多个会话，已内置重试机制（`GLM_BUSY_MAX_RETRIES=30`），如频繁出现可调高这个值。

### Q: 管理面板登录失败
A: 检查 `.env` 中 `ADMIN_PASSWORD` 是否被正确读取（管理面板的 Config 页可看到脱敏值）。

### Q: 内存持续增长
A: 请求日志最多保留 500 条，理论上限很低。如仍增长，检查是否有泄漏：
```bash
ps -o pid,rss,cmd -p $(cat /opt/glm2api/.server.pid)
```
