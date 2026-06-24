"""pytest 全局配置。

v52: 测试环境关闭 API rate limiting，避免连续测试触发限流。
生产环境默认 60 req/min，测试环境设为 0（不限制）。
"""
import os

# 测试环境关闭 rate limiting
os.environ.setdefault("API_RATE_LIMIT_PER_MINUTE", "0")
# 测试环境关闭 STRICT_VALIDATION（宽松模式，Claude Code 友好）
os.environ.setdefault("STRICT_VALIDATION", "")
# 测试环境用较小的 body size 限制便于测试，但默认仍 10MB
# 不设 MAX_BODY_SIZE_MB，用默认值
