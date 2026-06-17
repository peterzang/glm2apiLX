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
