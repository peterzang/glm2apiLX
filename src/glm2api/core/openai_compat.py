"""OpenAI API compatibility helpers.

Provides:
- OpenAI-format IDs (chatcmpl-, resp_, msg_, fc_, call_)
- System fingerprint (model + version hash)
- OpenAI-format error response envelopes
- Standard response parameter defaults
"""

from __future__ import annotations

import hashlib
import os
import platform
import time
import uuid
from typing import Any


# ---------------------------------------------------------------------------
# ID generators — match OpenAI's format
# ---------------------------------------------------------------------------


def gen_chatcmpl_id() -> str:
    """OpenAI chat completion ID: chatcmpl-<base62-ish 29 chars>."""
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def gen_response_id() -> str:
    """OpenAI Responses API ID: resp_<base62 24 chars>."""
    return f"resp_{uuid.uuid4().hex[:24]}"


def gen_message_id() -> str:
    """OpenAI message ID: msg_<base62 24 chars>."""
    return f"msg_{uuid.uuid4().hex[:24]}"


def gen_function_call_id() -> str:
    """OpenAI function call ID: call_<base62 24 chars>."""
    return f"call_{uuid.uuid4().hex[:24]}"


def gen_function_call_item_id() -> str:
    """OpenAI function call item ID: fc_<base62 24 chars>."""
    return f"fc_{uuid.uuid4().hex[:24]}"


def gen_request_id() -> str:
    """OpenAI request ID: req_<base62 24 chars>."""
    return f"req_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------------------
# System fingerprint
# ---------------------------------------------------------------------------


# v34 修复：system_fingerprint 动态化
# 官方 OpenAI 的 system_fingerprint 每次请求不同（含模型版本+部署信息）。
# 之前固定返回 fp_xxxxxx，客户端可以通过这个特征识别逆向 API。
# 修复：基于模型名 + 日期生成，同一天同一模型返回相同值（符合官方"部署指纹"语义），
# 但不同模型/不同日期会变化，看起来像真实的部署环境。
_FINGERPRINT_SALT = f"{platform.python_version()}-{platform.system()}-{os.getpid()}"
_FINGERPRINT_HASH = hashlib.md5(_FINGERPRINT_SALT.encode("utf-8")).hexdigest()[:6]


def system_fingerprint(model: str = "") -> str:
    """Return a system fingerprint in OpenAI's format: fp_<8 hex chars>.

    v34 修复：基于模型名 + 日期生成动态指纹。
    - 同一天同一模型返回相同值（模拟官方"部署指纹"语义）
    - 不同模型/不同日期会变化（看起来像真实部署环境）
    - 格式 fp_<8 hex>，与官方一致

    Args:
        model: 模型名（如 "glm-5.2"）。空字符串时用进程级 hash 兜底。
    """
    import time as _time
    if model:
        # 基于模型名 + 当天日期生成（每天变化，符合官方"部署版本"语义）
        date_str = _time.strftime("%Y%m%d")
        base = hashlib.md5(f"{model}:{date_str}".encode("utf-8")).hexdigest()[:8]
        return f"fp_{base}"
    # 兜底：进程级 hash（向后兼容）
    return f"fp_{_FINGERPRINT_HASH}"


# ---------------------------------------------------------------------------
# Error envelopes — match OpenAI's exact format
# ---------------------------------------------------------------------------


def make_error(
    message: str,
    *,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-format error envelope.

    OpenAI's error format:
        {
            "error": {
                "message": "...",
                "type": "invalid_request_error",
                "param": "model",          # nullable
                "code": "model_not_found",  # nullable
                "request_id": "req_..."     # present in most errors
            }
        }
    """
    err: dict[str, Any] = {
        "message": message,
        "type": error_type,
        "param": param,
        "code": code,
    }
    if request_id:
        err["request_id"] = request_id
    return {"error": err}


# Standard error types & codes
ERROR_INVALID_REQUEST = "invalid_request_error"
ERROR_AUTHENTICATION = "authentication_error"
ERROR_PERMISSION = "permission_denied"
ERROR_NOT_FOUND = "not_found_error"
ERROR_RATE_LIMIT = "rate_limit_exceeded"
ERROR_SERVER = "server_error"
ERROR_API = "api_error"
ERROR_UPSTREAM = "upstream_error"

# Common error codes
CODE_MODEL_NOT_FOUND = "model_not_found"
CODE_INVALID_API_KEY = "invalid_api_key"
CODE_INSUFFICIENT_QUOTA = "insufficient_quota"
CODE_RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
CODE_CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
CODE_INVALID_REQUEST = "invalid_request"
CODE_INTERNAL_ERROR = "internal_error"
CODE_BAD_GATEWAY = "bad_gateway"
CODE_SERVICE_UNAVAILABLE = "service_unavailable"


# ---------------------------------------------------------------------------
# HTTP status mapping (matches OpenAI's documented status codes)
# ---------------------------------------------------------------------------


HTTP_STATUS_FOR_ERROR_TYPE = {
    ERROR_INVALID_REQUEST: 400,
    ERROR_AUTHENTICATION: 401,
    ERROR_PERMISSION: 403,
    ERROR_NOT_FOUND: 404,
    ERROR_RATE_LIMIT: 429,
    ERROR_SERVER: 500,
    ERROR_API: 500,
    ERROR_UPSTREAM: 502,
}


def status_for_error_type(error_type: str, default: int = 400) -> int:
    return HTTP_STATUS_FOR_ERROR_TYPE.get(error_type, default)


# ---------------------------------------------------------------------------
# Common parameter defaults for chat completions
# ---------------------------------------------------------------------------


DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_TOKENS = None  # OpenAI returns None when not set
DEFAULT_N = 1
DEFAULT_PRESENCE_PENALTY = 0.0
DEFAULT_FREQUENCY_PENALTY = 0.0
DEFAULT_STREAM = False
DEFAULT_SEED = None


def now_timestamp() -> int:
    """Unix timestamp (seconds) for `created` field."""
    return int(time.time())
