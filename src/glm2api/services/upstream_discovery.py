"""实时发现 chatglm.cn 上游真实助手列表。

设计：
  1. 拉 chatglm.cn 主页 HTML，解析出当前 main.<hash>.js 的实际 URL
     （chatglm.cn 每次部署 hash 会变，不能写死）
  2. 拉 main.<hash>.js 内容
  3. 正则提取所有 24-hex MongoDB ObjectId（chatglm.cn 用作 assistant_id）
  4. 并发调用 /backend-api/assistant/info?assistant_id=<id> 拉每个助手详情
  5. 返回真实助手列表（含 name / description / chat_mode / scope / avatar / enabled）

缓存：
  - 整个流程耗时约 5-10 秒（拉主页 + JS + 23 次并发 info）
  - 内存缓存 30 分钟，避免每次刷新 admin 页面都重新拉
  - 提供 force_refresh=True 强制刷新

错误容忍：
  - 任一助手 info 拉取失败不影响其他助手
  - main.js 拉取失败时返回上次缓存（如果有）
"""
from __future__ import annotations

import gzip
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from logging import Logger
from typing import Any, Dict, List, Optional, Tuple

from ..config import AppConfig, DEFAULT_GLM_BASE_URL
from .glm_auth import GLMAccessTokenManager


# 主页 URL（chatglm.cn 任意路径都返回同一份 SPA HTML）
_MAIN_PAGE_URL = "https://chatglm.cn/"

# main.js 路径正则（路径形如 /main.aafd9312.js）
_MAIN_JS_RE = re.compile(r'["\']/(main\.[0-9a-f]+\.js)["\']')

# 24-hex MongoDB ObjectId 正则
_OBJECT_ID_RE = re.compile(r'["\']([0-9a-f]{24})["\']')

# 缓存 TTL：30 分钟
_CACHE_TTL_SECONDS = 30 * 60

# 已知的特殊 assistant_id（项目内置）— 用于在结果里标注
KNOWN_ASSISTANT_IDS = {
    "65940acff94777010aa6b796": "default_chat",
    "65a232c082ff90a2ad2f15e2": "default_image",
}


@dataclass(slots=True)
class UpstreamAssistant:
    """从 chatglm.cn 实时拉取的真实助手信息。"""
    assistant_id: str
    name: str
    description: str
    chat_mode: str
    scope: int
    enabled: bool
    avatar: str
    opening_ui: Optional[Dict[str, Any]]
    starter_prompts: List[str]
    is_known: bool           # 是否是项目已知的内置 assistant_id
    known_role: str          # 项目内置角色名（default_chat / default_image / ""）
    fetch_error: str = ""    # 拉取失败时的错误信息（非空表示失败）


class UpstreamDiscovery:
    """实时发现 chatglm.cn 上游真实助手列表。线程安全单例。"""

    def __init__(self, config: AppConfig, logger: Logger, auth: GLMAccessTokenManager) -> None:
        self.config = config
        self.logger = logger
        self.auth = auth
        self._lock = threading.Lock()
        self._cache: Optional[List[UpstreamAssistant]] = None
        self._cache_ts: float = 0.0
        self._cache_main_js_url: str = ""

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    def discover(self, force_refresh: bool = False) -> List[UpstreamAssistant]:
        """发现真实上游助手列表。
        
        force_refresh=True 时强制重新拉取（忽略缓存）。
        返回 UpstreamAssistant 列表（成功的在前，失败的在后）。
        """
        with self._lock:
            now = time.time()
            if (not force_refresh
                    and self._cache is not None
                    and now - self._cache_ts < _CACHE_TTL_SECONDS):
                self.logger.debug("upstream discovery 命中缓存 %d 个助手", len(self._cache))
                return list(self._cache)
            
            # 缓存 miss 或强制刷新
            try:
                assistants, main_js_url = self._discover_uncached()
                self._cache = assistants
                self._cache_ts = now
                self._cache_main_js_url = main_js_url
                self.logger.info(
                    "upstream discovery 完成 共发现 %d 个真实助手 main_js=%s",
                    len(assistants), main_js_url,
                )
                return list(assistants)
            except Exception as exc:
                self.logger.warning("upstream discovery 失败 error=%s", exc)
                # 失败时返回上次缓存（如果有），否则返回空列表
                if self._cache is not None:
                    self.logger.info("upstream discovery 失败，回退到上次缓存 %d 个助手", len(self._cache))
                    return list(self._cache)
                return []

    def get_cache_info(self) -> Dict[str, Any]:
        """返回缓存状态（用于 admin 面板展示）。"""
        with self._lock:
            return {
                "cached": self._cache is not None,
                "cache_ts": self._cache_ts,
                "cache_age_seconds": time.time() - self._cache_ts if self._cache_ts else 0,
                "cache_ttl_seconds": _CACHE_TTL_SECONDS,
                "main_js_url": self._cache_main_js_url,
                "assistant_count": len(self._cache) if self._cache else 0,
            }

    # -----------------------------------------------------------------
    # Internal: 实际拉取逻辑
    # -----------------------------------------------------------------

    def _discover_uncached(self) -> Tuple[List[UpstreamAssistant], str]:
        """完整发现流程：拉主页 → 解析 main.js URL → 拉 main.js → 提取 ID → 并发拉 info。"""
        # Step 1: 拉主页 HTML
        main_html = self._fetch_text(_MAIN_PAGE_URL, headers={
            "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://chatglm.cn/",
        }, timeout=15)
        self.logger.debug("拉到 chatglm.cn 主页 len=%d", len(main_html))

        # Step 2: 解析 main.js URL
        m = _MAIN_JS_RE.search(main_html)
        if not m:
            raise RuntimeError("无法在主页 HTML 中找到 main.<hash>.js 引用")
        main_js_path = m.group(1)
        main_js_url = f"https://chatglm.cn/{main_js_path}"

        # Step 3: 拉 main.js 内容
        main_js = self._fetch_text(main_js_url, headers={
            "Accept": "*/*",
            "Referer": "https://chatglm.cn/",
        }, timeout=20)
        self.logger.debug("拉到 main.js len=%d url=%s", len(main_js), main_js_url)

        # Step 4: 提取所有 24-hex assistant_id（去重保序）
        seen = set()
        assistant_ids: List[str] = []
        for match in _OBJECT_ID_RE.finditer(main_js):
            aid = match.group(1)
            if aid not in seen:
                seen.add(aid)
                assistant_ids.append(aid)
        self.logger.info("从 main.js 提取到 %d 个候选 assistant_id", len(assistant_ids))

        if not assistant_ids:
            raise RuntimeError("main.js 中未找到任何 24-hex assistant_id")

        # Step 5: 并发拉取每个 assistant 的详情
        assistants = self._fetch_assistants_batch(assistant_ids)
        return assistants, main_js_url

    def _fetch_assistants_batch(self, assistant_ids: List[str]) -> List[UpstreamAssistant]:
        """并发拉取所有助手详情。失败的助手也会返回（带 fetch_error 字段）。"""
        results: List[UpstreamAssistant] = []
        # 限并发 10 避免风控
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {
                pool.submit(self._fetch_one_assistant, aid): aid
                for aid in assistant_ids
            }
            for fut in as_completed(futures):
                aid = futures[fut]
                try:
                    assistant = fut.result()
                    results.append(assistant)
                except Exception as exc:
                    # 兜底：构造一个失败的 assistant 对象
                    results.append(UpstreamAssistant(
                        assistant_id=aid,
                        name="",
                        description="",
                        chat_mode="",
                        scope=0,
                        enabled=False,
                        avatar="",
                        opening_ui=None,
                        starter_prompts=[],
                        is_known=aid in KNOWN_ASSISTANT_IDS,
                        known_role=KNOWN_ASSISTANT_IDS.get(aid, ""),
                        fetch_error=f"{type(exc).__name__}: {exc}",
                    ))
        # 排序：成功的在前，失败的在后；同状态下按 name 排序
        results.sort(key=lambda a: (
            0 if a.fetch_error == "" else 1,
            a.name or "~",
        ))
        return results

    def _fetch_one_assistant(self, assistant_id: str) -> UpstreamAssistant:
        """拉单个助手详情。

        账号选择策略：随机选一个账号，避免所有探针请求都打到账号 0 触发风控。
        """
        import random
        account_count = self.auth.get_account_count()
        account_index = random.randint(0, max(0, account_count - 1))
        access_token = self.auth.get_access_token_for_account(account_index)
        device_id = self.auth.get_device_id_for_account(account_index)
        headers = self.auth.get_browser_headers(app_fr="default")
        headers.update({
            "Authorization": f"Bearer {access_token}",
            "X-Device-Id": device_id,
            "Referer": "https://chatglm.cn/main/chat",
            "X-App-Fr": "default",
            "Accept-Encoding": "identity",  # 强制不压缩，避免 gzip/deflate 解压问题
        })
        url = f"{self.config.glm_base_url}/backend-api/assistant/info?assistant_id={assistant_id}"
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            encoding = r.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                import zlib
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    # 兼容 raw deflate（无 zlib header）
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            text = body.decode("utf-8", errors="replace")
            payload = json.loads(text)

        result = payload.get("result") or {}
        return UpstreamAssistant(
            assistant_id=assistant_id,
            name=str(result.get("name", "")),
            description=str(result.get("description", "")),
            chat_mode=str(result.get("chat_mode", "")),
            scope=int(result.get("scope", 0) or 0),
            enabled=bool(result.get("enabled", False)),
            avatar=str(result.get("avatar", "")),
            opening_ui=result.get("opening_ui"),
            starter_prompts=list(result.get("starter_prompts") or []),
            is_known=assistant_id in KNOWN_ASSISTANT_IDS,
            known_role=KNOWN_ASSISTANT_IDS.get(assistant_id, ""),
            fetch_error="",
        )

    def _fetch_text(self, url: str, headers: Dict[str, str], timeout: int) -> str:
        """通用 GET 拉取（自动解压 gzip / deflate）。"""
        full_headers = {
            "User-Agent": self.config.glm_user_agent,
            "Accept-Encoding": "gzip, deflate",
            **headers,
        }
        req = urllib.request.Request(url, headers=full_headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            encoding = r.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                import zlib
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# -------------------------------------------------------------------

_DISCOVERY: Optional[UpstreamDiscovery] = None
_DISCOVERY_LOCK = threading.Lock()


def get_upstream_discovery(config: AppConfig, logger: Logger, auth: GLMAccessTokenManager) -> UpstreamDiscovery:
    """获取 UpstreamDiscovery 单例。第一次调用时初始化。"""
    global _DISCOVERY
    with _DISCOVERY_LOCK:
        if _DISCOVERY is None:
            _DISCOVERY = UpstreamDiscovery(config, logger, auth)
        return _DISCOVERY


def to_dict(assistant: UpstreamAssistant) -> Dict[str, Any]:
    """序列化 UpstreamAssistant 为 JSON 友好的 dict（用于 admin API 响应）。"""
    return {
        "assistant_id": assistant.assistant_id,
        "name": assistant.name,
        "description": assistant.description,
        "chat_mode": assistant.chat_mode,
        "scope": assistant.scope,
        "enabled": assistant.enabled,
        "avatar": assistant.avatar,
        "opening_ui": assistant.opening_ui,
        "starter_prompts": assistant.starter_prompts,
        "is_known": assistant.is_known,
        "known_role": assistant.known_role,
        "fetch_error": assistant.fetch_error,
    }
