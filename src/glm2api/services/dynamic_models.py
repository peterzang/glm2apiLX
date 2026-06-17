"""动态模型发现注册表（运行时累积）。

设计目标（用户原话）：
  "不管未来升级模型我们不需要再添加代码会自动获取到的，比如今天没有明天就有了
   然后我们显示模型哪里就可以看见，也就是实时动态的更新懂了吗省去添加代码了，
   也就是我们已经有了模型不会提示，等那天突然更新新模型了那我们也会显示新模型，
   就是动态获取的"

实现机制：
  1. 每次 chat_completion 成功后，从上游响应的 model 字段提取真实模型名
     （如 "GLM-5.2" / "GLM-4-Flash"）
  2. 归一化（"GLM-5.2" → "glm-5.2"，"GLM-4-Flash" → "glm-4-flash"）
  3. 检查是否已在 BUILTIN_EXPOSED_MODELS 里（已有则忽略）
  4. 不在则加入"动态发现池"，并自动派生 -think / -search / -think-search 变体
  5. AppConfig.exposed_models 通过 dynamic_registry.get_dynamic_exposed_models()
     在运行时合并 builtin + dynamic

线程安全：
  - 用 threading.Lock 保护，避免并发请求同时修改
  - 动态池是 set，去重自然

持久化：
  - 暂不持久化（进程重启后清空）
  - 但每次启动后第一次请求就会重新发现，所以"动态性"是保留的
  - 未来如需持久化可写 .dynamic_models.json
"""
from __future__ import annotations

import re
import threading
from typing import Set

from ..core.model_variants import expand_model_variants


# 已知的模型名前缀（用于归一化后过滤明显不是模型名的字段）
_KNOWN_PREFIXES = ("glm-", "cogview-")

# 归一化规则：把 GLM 上游返回的模型名转为项目命名规范
# GLM-5.2 → glm-5.2，GLM-4-Flash → glm-4-flash，GLM-4V → glm-4v
_NORMALIZE_RE = re.compile(r'^GLM[-_]?(\d[\w.\-]*)$', re.IGNORECASE)


def normalize_upstream_model_name(raw: str) -> str:
    """把上游返回的模型名归一化为项目命名规范。

    例：
      "GLM-5.2" → "glm-5.2"
      "GLM-4-Flash" → "glm-4-flash"
      "GLM-4V" → "glm-4v"
      "glm-5.1-think" → "glm-5.1-think"（已规范的直接返回）
      "CogView-4" → "cogview-4"
      "清言" → "" （不符合模型名格式，返回空）
    """
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    # 已经是小写规范的，直接返回
    if s.lower().startswith(_KNOWN_PREFIXES):
        return s.lower()
    # GLM-XXX 格式 → glm-xxx
    m = _NORMALIZE_RE.match(s)
    if m:
        return f"glm-{m.group(1).lower()}"
    # CogView-XXX 格式
    if s.lower().startswith("cogview"):
        return s.lower().replace("_", "-")
    # 不符合模型名格式的返回空字符串
    return ""


class DynamicModelRegistry:
    """动态模型发现注册表。线程安全单例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._discovered: Set[str] = set()  # 已发现的新模型（基础模型名）
        self._discovery_count = 0  # 累计发现次数（用于日志统计）

    def discover_from_response(self, raw_model_name: str) -> bool:
        """从上游响应中提取并注册新模型。

        参数：
          raw_model_name: 上游响应的 model 字段值（如 "GLM-5.2"）

        返回：
          True 表示发现了新模型并加入池中；False 表示已存在或不符合模型名格式
        """
        normalized = normalize_upstream_model_name(raw_model_name)
        if not normalized:
            return False
        # 已知前缀才接受（避免把"清言"等用户名误识别为模型）
        if not normalized.startswith(_KNOWN_PREFIXES):
            return False
        # 切掉变体后缀（-think/-search）得到基础模型名
        # 例：glm-5.1-think → glm-5.1
        base = normalized
        for suffix in ("-think-search", "-think", "-search"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        with self._lock:
            if base in self._discovered:
                return False
            self._discovered.add(base)
            self._discovery_count += 1
        return True

    def get_discovered_base_models(self) -> list[str]:
        """返回已发现的基础模型列表（不含变体）。"""
        with self._lock:
            return sorted(self._discovered)

    def get_discovered_with_variants(self) -> list[str]:
        """返回已发现的模型列表（含 -think / -search / -think-search 变体）。"""
        with self._lock:
            if not self._discovered:
                return []
            return expand_model_variants(sorted(self._discovered))

    def get_stats(self) -> dict[str, object]:
        """返回统计信息（用于 admin 面板展示）。"""
        with self._lock:
            return {
                "discovered_count": len(self._discovered),
                "discovered_models": sorted(self._discovered),
                "total_discovery_events": self._discovery_count,
            }

    def clear(self) -> None:
        """清空动态发现池（admin 面板"重置"按钮可用）。"""
        with self._lock:
            self._discovered.clear()
            self._discovery_count = 0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_REGISTRY = DynamicModelRegistry()


def get_dynamic_registry() -> DynamicModelRegistry:
    """获取 DynamicModelRegistry 单例。"""
    return _REGISTRY


def merge_with_builtin(builtin_models: list[str]) -> list[str]:
    """把动态发现的模型合并到 builtin 模型列表。

    参数：
      builtin_models: config.exposed_models（BUILTIN_EXPOSED_MODELS 展开后的列表）

    返回：
      合并后的列表，去重保序（builtin 在前，新发现的按字母序追加在后）
    """
    discovered = _REGISTRY.get_discovered_with_variants()
    if not discovered:
        return list(builtin_models)
    seen = set(builtin_models)
    merged = list(builtin_models)
    for m in discovered:
        if m not in seen:
            merged.append(m)
            seen.add(m)
    return merged
