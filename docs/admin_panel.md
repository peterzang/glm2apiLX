# 管理面板使用手册

## 访问

启动服务后浏览器打开：
```
http://<host>:8000/admin
```

## 登录

密码优先级（高 → 低）：
1. 环境变量 `ADMIN_PASSWORD`
2. `.env` 文件中的 `ADMIN_PASSWORD`
3. `SERVER_API_KEYS` 第一个值
4. 默认值 `admin`（强烈建议修改）

会话有效期 8 小时，登录后 token 存在 `localStorage`。

## 功能模块

### 1. 仪表盘（Dashboard）

实时展示核心 KPI：
- **累计统计**：总请求数 / 成功数 / 客户端错误 / 服务端错误 / 成功率
- **近 5 分钟**：p50 / p95 / p99 延迟
- **小时图**：最近 48 小时请求量（CSS bar chart）
- **Top 模型**：使用频次最高的模型
- **协议分布**：openai-chat / anthropic-messages / openai-responses

每 5 秒自动刷新。

### 2. 账号管理（Accounts）

显示所有上游账号槽位状态：
- 索引 / 类型（guest / refresh_token）
- device_id（脱敏显示前后各 4 位）
- 累计请求数 / 成功数 / 失败数
- 当前 device_id 已用次数 / 阈值
- 在线状态

支持操作：
- **手动轮换**：点击"轮换"按钮立即换 device_id（reason=manual）

### 3. 请求日志（Logs）

最近 500 条请求记录：
- 时间 / 方法 / 路径 / 协议 / 模型
- 状态码 / 耗时（ms）
- 使用账号索引 / 是否流式 / 错误信息
- request_id（可链路追踪）

支持按状态码、模型、协议筛选。

### 4. 轮换事件（Rotates）

device_id 轮换审计日志：
- 时间 / 账号索引
- 触发原因：
  - `rate_limited` — 被风控被动触发
  - `proactive` — 达到阈值主动触发
  - `manual` — 管理面板手动触发
- 旧 device_id / 新 device_id

### 5. 配置查看（Config）

显示当前生效配置（敏感字段脱敏）：
- 服务监听地址 / 端口 / API 前缀
- GLM 上游配置
- 并发 / 队列 / 重试参数
- device_id 轮换阈值
- 工具黑名单

### 6. 系统监控（System）

- 运行时长（uptime）
- 内存使用（RSS / VMS）
- CPU 时间（user / sys）
- 文件描述符数
- Python 版本 / 平台
- 当前时间

## API 端点

所有 API 都需要 `X-Admin-Token` header（登录返回的 token）。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/api/login` | 登录获取 token |
| POST | `/admin/api/logout` | 注销 |
| GET | `/admin/api/dashboard` | 仪表盘数据 |
| GET | `/admin/api/logs` | 请求日志 |
| GET | `/admin/api/rotates` | 轮换事件 |
| GET | `/admin/api/accounts` | 账号列表 |
| GET | `/admin/api/config` | 当前配置 |
| GET | `/admin/api/system` | 系统信息 |
| POST | `/admin/api/accounts/{idx}/rotate` | 手动轮换指定账号 |

### 示例

```bash
# 登录
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/admin/api/login \
  -H "Content-Type: application/json" \
  -d '{"password":"your-password"}' | jq -r .token)

# 获取仪表盘
curl -s http://127.0.0.1:8000/admin/api/dashboard \
  -H "X-Admin-Token: $TOKEN" | jq

# 手动轮换账号 #0
curl -s -X POST http://127.0.0.1:8000/admin/api/accounts/0/rotate \
  -H "X-Admin-Token: $TOKEN"
```

## 安全建议

1. **修改默认密码**：在 `.env` 中设置强 `ADMIN_PASSWORD`
2. **不要暴露公网**：默认监听 0.0.0.0，建议用反向代理 + IP 白名单
3. **启用 SERVER_API_KEYS**：如果对外提供服务，给 API 客户端也加鉴权
4. **HTTPS**：反向代理层做 TLS 终止
5. **定期检查轮换日志**：异常多的 `rate_limited` 说明被风控了
