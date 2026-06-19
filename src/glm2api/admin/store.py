"""Admin state store: in-memory metrics + request log buffer.

This module is the single shared window into runtime state.
Both the HTTP server (which records request outcomes) and the admin API
(which exposes them) import the same global instance.
"""
from __future__ import annotations

import collections
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ApiKeyRecord:
    """An API key for tracking usage (created via admin panel or from env vars)."""
    key: str                          # the actual API key string
    name: str                         # human-readable name (e.g. "production", "test")
    created_at: float                 # epoch seconds
    is_env: bool                      # True if from SERVER_API_KEYS env var (not deletable)
    enabled: bool                     # whether this key is active
    # Per-key usage stats
    total_requests: int = 0
    total_success: int = 0
    total_errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    last_used_ts: float = 0.0


@dataclass(slots=True)
class RequestRecord:
    """One completed (or failed) HTTP request through the gateway."""
    ts: float                          # epoch seconds
    method: str                        # GET / POST
    path: str                          # /v1/chat/completions, /v1/responses, ...
    protocol: str                      # openai-chat | openai-responses | anthropic | openai-legacy | openai-embeddings | openai-moderations | openai-images | other
    model: str                         # requested model ("" if not applicable)
    status: int                        # HTTP status returned to client
    duration_ms: int                   # end-to-end latency in ms
    client_ip: str
    account_index: int                 # -1 if not assigned / unknown
    stream: bool
    error: str                         # "" on success
    request_id: str                    # internal request_id (for cross-correlation)


@dataclass(slots=True)
class AccountSnapshot:
    """Point-in-time view of one guest/refresh account."""
    index: int
    is_guest: bool
    device_id_short: str               # first 8 chars
    request_id_counter: int
    device_request_count: int
    rotate_threshold: int
    cached_token_expires_in: float     # seconds remaining; -1 if no token
    prefetch_in_progress: bool
    has_prefetched_token: bool
    last_used_ts: float                # 0 if never
    rotate_count: int                  # how many device_id rotations since start
    error_count: int                   # failed requests attributed to this account
    success_count: int                 # successful requests


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_MAX_LOG_RECORDS = 500                  # keep last 500 requests in memory
_HOURLY_BUCKETS = 48                    # 48 hour rolling window
_TOP_N_MODELS = 8


class AdminStore:
    """Thread-safe admin state. One global instance per process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._request_log: Deque[RequestRecord] = collections.deque(maxlen=_MAX_LOG_RECORDS)
        # Aggregated counters
        self._total_requests = 0
        self._total_success = 0          # 2xx
        self._total_errors = 0           # 5xx or exceptions
        self._total_client_errors = 0    # 4xx
        # Per-account stats (kept by store, fed by auth manager via hooks)
        self._account_stats: Dict[int, Dict[str, int]] = {}
        # Per-model stats
        self._model_counter: Dict[str, int] = collections.Counter()
        # Per-protocol stats
        self._protocol_counter: Dict[str, int] = collections.Counter()
        # Hourly histogram: list of (hour_start_epoch, total, success, error)
        self._hourly: Deque[Dict[str, Any]] = collections.deque(maxlen=_HOURLY_BUCKETS)
        # Active sessions (admin login)
        self._sessions: Dict[str, float] = {}  # token -> expires_at
        # Process start time
        self._started_at = time.time()
        # Rotate events (for audit log)
        self._rotate_events: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        # Model probe cache: model_id -> { ts, ok, latency_ms, status, error, content_preview, account_index }
        # 用于 admin 面板「模型」页显示每个模型最近一次探针结果
        self._model_probe_cache: Dict[str, Dict[str, Any]] = {}
        # Token 累计（用于仪表盘 KPI）
        self._token_totals = {"prompt": 0, "completion": 0, "total": 0}
        # 每分钟请求计数：{ minute_epoch: count }，保留最近 60 分钟
        self._rpm_buckets: Dict[int, int] = collections.defaultdict(int)
        # 历史 RPM 峰值
        self._peak_rpm = 0
        # 30 分钟滚动窗口 token 累计（用于仪表盘 KPI）
        self._token_30m_buckets: Dict[int, Dict[str, int]] = collections.defaultdict(
            lambda: {"prompt": 0, "completion": 0, "total": 0}
        )
        # 复读检测触发统计（用于管理面板「复读率」展示）
        # _repetition_events: list of {ts, model, path}，最多保留 200 条
        self._repetition_events: Deque[Dict[str, Any]] = collections.deque(maxlen=200)
        # _repetition_counter: {(model, path): count}
        self._repetition_counter: Dict[tuple, int] = collections.Counter()
        # 按模型延迟统计（用于「按模型平均延迟」展示）
        # _model_latencies: {model: list of duration_ms}，每个模型最多保留 100 条
        self._model_latencies: Dict[str, Deque[int]] = collections.defaultdict(
            lambda: collections.deque(maxlen=100)
        )
        # API Key 管理：{key_string: ApiKeyRecord}
        self._api_keys: Dict[str, ApiKeyRecord] = {}

    # -----------------------------------------------------------------
    # Recording
    # -----------------------------------------------------------------

    def record_request(self, rec: RequestRecord) -> None:
        with self._lock:
            self._request_log.append(rec)
            self._total_requests += 1
            if 200 <= rec.status < 300:
                self._total_success += 1
            elif 400 <= rec.status < 500:
                self._total_client_errors += 1
            else:
                self._total_errors += 1
            if rec.model:
                self._model_counter[rec.model] += 1
                # 按模型延迟统计（v3 审核报告建议：帮助用户选模型时参考）
                if 200 <= rec.status < 300:  # 只统计成功请求
                    self._model_latencies[rec.model].append(rec.duration_ms)
            self._protocol_counter[rec.protocol] = self._protocol_counter.get(rec.protocol, 0) + 1
            if rec.account_index >= 0:
                stats = self._account_stats.setdefault(rec.account_index, {
                    "success": 0, "error": 0, "last_used_ts": 0.0,
                })
                if 200 <= rec.status < 300:
                    stats["success"] += 1
                else:
                    stats["error"] += 1
                stats["last_used_ts"] = rec.ts
            self._hourly_append(rec)
            # 更新 RPM bucket
            minute = int(rec.ts // 60) * 60
            self._rpm_buckets[minute] = self._rpm_buckets.get(minute, 0) + 1
            # 清理超过 60 分钟的旧 bucket
            cutoff = minute - 60 * 60
            for k in list(self._rpm_buckets.keys()):
                if k < cutoff:
                    del self._rpm_buckets[k]
            # 更新 peak_rpm
            current_rpm = self._rpm_buckets.get(minute, 0)
            if current_rpm > self._peak_rpm:
                self._peak_rpm = current_rpm

    def record_token_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次成功 chat 请求的 token 使用量（用于仪表盘 KPI）。
        
        在 server.py 的 chat_completion 成功返回后调用。
        """
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        pt = max(0, int(prompt_tokens))
        ct = max(0, int(completion_tokens))
        tt = pt + ct
        now = time.time()
        minute = int(now // 60) * 60
        with self._lock:
            self._token_totals["prompt"] += pt
            self._token_totals["completion"] += ct
            self._token_totals["total"] += tt
            bucket = self._token_30m_buckets[minute]
            bucket["prompt"] += pt
            bucket["completion"] += ct
            bucket["total"] += tt
            # 清理超过 30 分钟的旧 bucket
            cutoff = minute - 30 * 60
            for k in list(self._token_30m_buckets.keys()):
                if k < cutoff:
                    del self._token_30m_buckets[k]

    def record_repetition_event(self, model: str, path: str) -> None:
        """记录一次复读检测触发事件（用于管理面板展示复读率）。
        
        参数：
          model: 触发复读的模型名（如 "glm-5.2"）
          path: 调用路径（"stream" 或 "non_stream"）
        """
        now = time.time()
        with self._lock:
            self._repetition_events.append({
                "ts": now,
                "model": model,
                "path": path,
            })
            self._repetition_counter[(model, path)] += 1

    def get_repetition_stats(self) -> Dict[str, Any]:
        """返回复读检测统计（用于管理面板展示）。"""
        with self._lock:
            # 最近 24 小时复读事件数
            cutoff_24h = time.time() - 24 * 3600
            recent_count = sum(1 for e in self._repetition_events if e["ts"] >= cutoff_24h)
            # 按 model 分组
            by_model: Dict[str, int] = collections.Counter()
            by_path: Dict[str, int] = collections.Counter()
            for (model, path), count in self._repetition_counter.items():
                by_model[model] += count
                by_path[path] += count
            return {
                "total_events": sum(self._repetition_counter.values()),
                "recent_24h_count": recent_count,
                "by_model": dict(by_model),
                "by_path": dict(by_path),
                "recent_events": [
                    {"ts": e["ts"], "model": e["model"], "path": e["path"]}
                    for e in list(self._repetition_events)[-20:]
                ],
            }

    def _hourly_append(self, rec: RequestRecord) -> None:
        hour = int(rec.ts // 3600) * 3600
        if not self._hourly or self._hourly[-1]["hour"] != hour:
            self._hourly.append({"hour": hour, "total": 0, "success": 0, "error": 0})
        bucket = self._hourly[-1]
        bucket["total"] += 1
        if 200 <= rec.status < 300:
            bucket["success"] += 1
        elif rec.status >= 500:
            bucket["error"] += 1

    def record_rotate(self, account_index: int, old_dev_short: str, new_dev_short: str, reason: str) -> None:
        with self._lock:
            self._rotate_events.append({
                "ts": time.time(),
                "account_index": account_index,
                "old_device": old_dev_short,
                "new_device": new_dev_short,
                "reason": reason,
            })

    # -----------------------------------------------------------------
    # Sessions
    # -----------------------------------------------------------------

    def create_session(self, ttl_seconds: int = 8 * 3600) -> str:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with self._lock:
            self._sessions[token] = time.time() + ttl_seconds
        return token

    def validate_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None:
                return False
            if exp < time.time():
                self._sessions.pop(token, None)
                return False
            return True

    def revoke_session(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    # -----------------------------------------------------------------
    # Aggregations (called by admin API)
    # -----------------------------------------------------------------

    def dashboard(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            # Recent-window metrics (last 5 minutes)
            cutoff_5m = now - 300
            recent = [r for r in self._request_log if r.ts >= cutoff_5m]
            recent_success = sum(1 for r in recent if 200 <= r.status < 300)
            recent_total = len(recent)
            recent_success_rate = (recent_success / recent_total * 100) if recent_total else 0.0
            recent_latencies = sorted(r.duration_ms for r in recent) if recent else []
            p50 = recent_latencies[len(recent_latencies) // 2] if recent_latencies else 0
            p95_idx = int(len(recent_latencies) * 0.95)
            p95 = recent_latencies[min(p95_idx, len(recent_latencies) - 1)] if recent_latencies else 0
            p99_idx = int(len(recent_latencies) * 0.99)
            p99 = recent_latencies[min(p99_idx, len(recent_latencies) - 1)] if recent_latencies else 0
            # All-time
            all_total = self._total_requests
            all_success_rate = (self._total_success / all_total * 100) if all_total else 0.0
            # Hourly histogram (oldest first)
            hourly = list(self._hourly)
            # Top models
            top_models = self._model_counter.most_common(_TOP_N_MODELS)
            # Protocols
            protocols = dict(self._protocol_counter)
            # RPM（最近 1 分钟的请求数）
            current_minute = int(now // 60) * 60
            rpm = self._rpm_buckets.get(current_minute, 0)
            # 30 分钟平均 RPM
            cutoff_30m = now - 30 * 60
            rpm_30m_total = sum(c for m, c in self._rpm_buckets.items() if m * 60 >= cutoff_30m)
            avg_rpm = round(rpm_30m_total / 30.0, 1) if rpm_30m_total else 0.0
            # 30 分钟请求总数
            requests_30m = sum(1 for r in self._request_log if r.ts >= cutoff_30m)
            # 30 分钟 token 累计
            token_30m = {"prompt": 0, "completion": 0, "total": 0}
            for m, bucket in self._token_30m_buckets.items():
                if m * 60 >= cutoff_30m:
                    for k in token_30m:
                        token_30m[k] += bucket[k]
            # 活跃账号数（success > 0 或 last_used_ts > 0）
            accounts_active = sum(1 for s in self._account_stats.values() if s.get("last_used_ts", 0) > 0)
            accounts_total = len(self._account_stats)
            # 协议分类（chat / models / images / admin / meta / other）
            # "meta" 协议包含 /v1/models / /v1/models/{id} / /health，
            # 这里把 meta 全算到 models（健康检查占比小，可接受）
            proto_breakdown = {
                "chat": sum(c for p, c in protocols.items() if p in ("openai-chat", "anthropic", "openai-responses", "openai-legacy")),
                "models": protocols.get("meta", 0),
                "images": protocols.get("openai-images", 0),
                "embeddings": protocols.get("openai-embeddings", 0),
                "moderations": protocols.get("openai-moderations", 0),
                "other": protocols.get("other", 0),
            }
            return {
                "now": now,
                "uptime_seconds": now - self._started_at,
                "all_time": {
                    "total": all_total,
                    "success": self._total_success,
                    "client_errors": self._total_client_errors,
                    "server_errors": self._total_errors,
                    "success_rate": round(all_success_rate, 2),
                },
                "recent_5m": {
                    "total": recent_total,
                    "success": recent_success,
                    "success_rate": round(recent_success_rate, 2),
                    "p50_ms": p50,
                    "p95_ms": p95,
                    "p99_ms": p99,
                },
                "hourly": hourly,
                "top_models": [{"model": m, "count": c} for m, c in top_models],
                "protocols": protocols,
                # === 新增字段（参考 Qwen2API_Go 仪表盘）===
                "rpm": rpm,
                "avg_rpm": avg_rpm,
                "peak_rpm": self._peak_rpm,
                "requests_30m": requests_30m,
                "token_totals": dict(self._token_totals),
                "token_30m": token_30m,
                "accounts_active": accounts_active,
                "accounts_total": accounts_total,
                "proto_breakdown": proto_breakdown,
                # 复读检测统计
                "repetition": self.get_repetition_stats_unlocked(),
                # 按模型延迟统计（v3 审核报告建议）
                "model_latencies": self._get_model_latencies_summary(),
            }

    def _get_model_latencies_summary(self) -> Dict[str, Dict[str, float]]:
        """返回每个模型的延迟统计摘要（avg / p50 / p95 / count）。"""
        summary: Dict[str, Dict[str, float]] = {}
        for model, latencies in self._model_latencies.items():
            if not latencies:
                continue
            sorted_lats = sorted(latencies)
            n = len(sorted_lats)
            avg = sum(sorted_lats) / n
            p50 = sorted_lats[n // 2]
            p95_idx = int(n * 0.95)
            p95 = sorted_lats[min(p95_idx, n - 1)]
            summary[model] = {
                "count": n,
                "avg_ms": round(avg, 1),
                "p50_ms": p50,
                "p95_ms": p95,
            }
        return summary

    def get_repetition_stats_unlocked(self) -> Dict[str, Any]:
        """返回复读检测统计（已持锁版本，避免重入）。"""
        cutoff_24h = time.time() - 24 * 3600
        recent_count = sum(1 for e in self._repetition_events if e["ts"] >= cutoff_24h)
        by_model: Dict[str, int] = collections.Counter()
        by_path: Dict[str, int] = collections.Counter()
        for (model, path), count in self._repetition_counter.items():
            by_model[model] += count
            by_path[path] += count
        return {
            "total_events": sum(self._repetition_counter.values()),
            "recent_24h_count": recent_count,
            "by_model": dict(by_model),
            "by_path": dict(by_path),
        }

    def recent_logs(self, limit: int = 100, only_errors: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self._request_log)
        if only_errors:
            records = [r for r in records if r.status >= 400]
        records = records[-limit:][::-1]  # newest first
        return [
            {
                "ts": r.ts,
                "method": r.method,
                "path": r.path,
                "protocol": r.protocol,
                "model": r.model,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "client_ip": r.client_ip,
                "account_index": r.account_index,
                "stream": r.stream,
                "error": r.error,
                "request_id": r.request_id,
            }
            for r in records
        ]

    def rotate_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._rotate_events)[-limit:][::-1]

    def account_extra_stats(self, idx: int) -> Dict[str, Any]:
        with self._lock:
            return dict(self._account_stats.get(idx, {"success": 0, "error": 0, "last_used_ts": 0.0}))

    # -----------------------------------------------------------------
    # Model probe cache（用于 admin「模型」页面展示最近一次探针结果）
    # -----------------------------------------------------------------

    def record_model_probe(self, model: str, result: Dict[str, Any]) -> None:
        """记录单模型最近一次探针结果，覆盖旧记录。"""
        with self._lock:
            self._model_probe_cache[model] = {
                "ts": time.time(),
                **result,
            }

    def get_model_probe_cache(self) -> Dict[str, Dict[str, Any]]:
        """返回所有模型的最近探针结果（model_id -> result）。"""
        with self._lock:
            # 拷贝避免外部修改
            return {k: dict(v) for k, v in self._model_probe_cache.items()}

    def clear_model_probe_cache(self) -> None:
        """清空所有探针缓存。"""
        with self._lock:
            self._model_probe_cache.clear()

    # -----------------------------------------------------------------
    # API Key 管理
    # -----------------------------------------------------------------

    def init_env_api_keys(self, env_keys: List[str]) -> None:
        """从环境变量 SERVER_API_KEYS 初始化 API keys（不可删除）。"""
        with self._lock:
            for key in env_keys:
                key = key.strip()
                if key and key not in self._api_keys:
                    self._api_keys[key] = ApiKeyRecord(
                        key=key,
                        name="环境变量",
                        created_at=self._started_at,
                        is_env=True,
                        enabled=True,
                    )

    def create_api_key(self, name: str) -> Dict[str, Any]:
        """创建一个新的 API key，返回 {key, name}。"""
        with self._lock:
            new_key = f"sk-glm2api-{uuid.uuid4().hex[:32]}"
            self._api_keys[new_key] = ApiKeyRecord(
                key=new_key,
                name=name or "未命名",
                created_at=time.time(),
                is_env=False,
                enabled=True,
            )
            return {"key": new_key, "name": name or "未命名"}

    def delete_api_key(self, key: str) -> bool:
        """删除一个 API key（环境变量的不可删除）。"""
        with self._lock:
            rec = self._api_keys.get(key)
            if rec is None:
                return False
            if rec.is_env:
                return False
            del self._api_keys[key]
            return True

    def toggle_api_key(self, key: str, enabled: bool) -> bool:
        """启用/禁用一个 API key。"""
        with self._lock:
            rec = self._api_keys.get(key)
            if rec is None:
                return False
            rec.enabled = enabled
            return True

    def record_api_key_usage(
        self, key: str, success: bool, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> None:
        """记录某 API key 的一次请求使用量。"""
        with self._lock:
            rec = self._api_keys.get(key)
            if rec is None:
                return
            rec.total_requests += 1
            if success:
                rec.total_success += 1
            else:
                rec.total_errors += 1
            rec.prompt_tokens += max(0, prompt_tokens)
            rec.completion_tokens += max(0, completion_tokens)
            rec.total_tokens += max(0, prompt_tokens + completion_tokens)
            rec.last_used_ts = time.time()

    def get_api_keys(self) -> List[Dict[str, Any]]:
        """返回所有 API key 的信息（含用量统计），key 脱敏显示。"""
        with self._lock:
            result = []
            for rec in self._api_keys.values():
                # 脱敏：只显示前 8 位和后 4 位
                if len(rec.key) > 16:
                    masked_key = rec.key[:8] + "..." + rec.key[-4:]
                else:
                    masked_key = rec.key[:4] + "..."
                result.append({
                    "key": masked_key,
                    "full_key": rec.key if not rec.is_env else None,  # 完整 key 仅非环境变量返回
                    "name": rec.name,
                    "created_at": rec.created_at,
                    "is_env": rec.is_env,
                    "enabled": rec.enabled,
                    "total_requests": rec.total_requests,
                    "total_success": rec.total_success,
                    "total_errors": rec.total_errors,
                    "prompt_tokens": rec.prompt_tokens,
                    "completion_tokens": rec.completion_tokens,
                    "total_tokens": rec.total_tokens,
                    "last_used_ts": rec.last_used_ts,
                })
            # 按 total_requests 降序
            result.sort(key=lambda x: x["total_requests"], reverse=True)
            return result

    def get_api_key_for_auth(self, key: str) -> bool:
        """检查 API key 是否有效（存在且启用）。"""
        with self._lock:
            rec = self._api_keys.get(key)
            if rec is None:
                return False
            return rec.enabled


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

GLOBAL_STORE = AdminStore()


def get_store() -> AdminStore:
    return GLOBAL_STORE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def classify_protocol(path: str, payload: Optional[dict] = None) -> str:
    """Map an HTTP path to a coarse protocol bucket for stats.

    IMPORTANT: chat/completions check must come BEFORE /completions,
    because "/v1/chat/completions" ends with "/completions" too.
    """
    if path.endswith("/v1/chat/completions") or path.endswith("/chat/completions"):
        return "openai-chat"
    if path.endswith("/v1/messages") or path.endswith("/messages"):
        return "anthropic"
    if path.endswith("/v1/responses") or path.endswith("/responses"):
        return "openai-responses"
    if path.endswith("/v1/embeddings") or path.endswith("/embeddings"):
        return "openai-embeddings"
    if path.endswith("/v1/moderations") or path.endswith("/moderations"):
        return "openai-moderations"
    if path.endswith("/v1/completions") or path.endswith("/completions"):
        return "openai-legacy"
    if "/images/" in path:
        return "openai-images"
    if path == "/health" or path.endswith("/models"):
        return "meta"
    return "other"


def humanize_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"
