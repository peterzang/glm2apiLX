from __future__ import annotations

import hashlib
import gzip
import json
import random
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from logging import Logger

from ..config import AppConfig, GUEST_REFRESH_TOKEN_MARKER, is_guest_token_value
from ..logging_utils import debug_dump


SIGN_SECRET = "8a1317a7468aa3ad86e997d08f3f31cb"
ACCESS_TOKEN_EXPIRES_SECONDS = 3600


def build_sign() -> tuple[str, str, str]:
    now = str(int(time.time() * 1000))
    digits = [int(char) for char in now]
    checksum = (sum(digits) - digits[-2]) % 10
    timestamp = now[:-2] + str(checksum) + now[-1]
    nonce = uuid.uuid4().hex
    sign = hashlib.md5(f"{timestamp}-{nonce}-{SIGN_SECRET}".encode("utf-8")).hexdigest()
    return timestamp, nonce, sign


def build_random_x_forwarded_for() -> str:
    while True:
        first_octet = random.randint(1, 223)
        if first_octet in {10, 127, 169, 172, 192}:
            continue
        octets = [first_octet]
        for _ in range(3):
            octets.append(random.randint(0, 255))
        return ".".join(str(octet) for octet in octets)


@dataclass(slots=True)
class AccessToken:
    access_token: str
    refresh_token: str
    expires_at: float


@dataclass(slots=True)
class AccountState:
    refresh_token: str
    is_guest: bool = False
    cached_token: AccessToken | None = None
    # device_id：每次启动随机生成，被风控时主动轮换
    device_id: str = ""
    request_id_counter: int = 0
    # 主动轮换：每个 device_id 累计请求数（含失败）
    # 超过阈值时主动 rotate，不等失败
    device_request_count: int = 0
    # === device_id 池预 fetch（消除轮换延迟）===
    # rotate 后立即异步 fetch 新 token，下次请求直接命中 0 延迟
    # prefetched_token 必须配合 prefetched_device_id 校验：device_id 又被 rotate 后废弃
    prefetched_token: AccessToken | None = None
    prefetched_device_id: str = ""
    prefetch_in_progress: bool = False
    # v54 C3: 账号熔断状态 — 连续刷新失败 N 次后熔断 T 秒
    # 避免所有 refresh_token 失效时遍历全部账号（100 分钟挂起）
    consecutive_failures: int = 0
    circuit_break_until: float = 0.0  # time.time() 之前的时间戳，过了则恢复


def _load_or_create_device_id(account_index: int) -> str:
    """生成新的随机 device_id（不再持久化）。
    
    设计变更（2026-06-17）：
    之前持久化 device_id 是为了避免"同账号多设备并发"风控。但实测发现
    智谱游客 chat 接口的频控恰恰按 device_id 计数——持久化的 device_id
    用几次就会被风控（"您已多次体验过对话, 请登录后继续使用"），
    全新的随机 device_id 反而稳定可用。
    
    因此这里改为：每次启动生成新 UUID，永不持久化。失败时由
    `rotate_device_id_for_account` 主动轮换。
    """
    return uuid.uuid4().hex


# 游客频控关键字（命中即触发 device_id 轮换）
GUEST_RATE_LIMIT_MARKERS = (
    "多次体验过",
    "请登录后继续使用",
    "请登录后重试",
)


def is_guest_rate_limited(exc: Exception) -> bool:
    """判断异常是否是游客频控（需要轮换 device_id 重试）。"""
    msg = str(exc)
    return any(marker in msg for marker in GUEST_RATE_LIMIT_MARKERS)


class GLMAccessTokenManager:
    def __init__(self, config: AppConfig, logger: Logger) -> None:
        self.config = config
        self.logger = logger
        self._accounts = [
            AccountState(
                refresh_token="" if token == GUEST_REFRESH_TOKEN_MARKER else token,
                is_guest=(token == GUEST_REFRESH_TOKEN_MARKER),
                device_id=_load_or_create_device_id(idx),
            )
            for idx, token in enumerate(config.glm_refresh_tokens)
        ]
        self._current_index = 0
        # RLock：因为 get_device_id_for_account / next_request_id_for_account
        # 会在 _refresh_access_token（已持锁）内被调用，需要可重入
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        logger.info(
            "账号管理器初始化 账号数=%s 游客模式=%s",
            len(self._accounts),
            any(a.is_guest for a in self._accounts),
        )

    def prefetch_initial_tokens(self, count: int = 3) -> None:
        """v47: 启动时预取前 N 个账号的 token，避免首个请求卡在 token 刷新。

        之前：服务启动后第一个请求需要同步获取游客 token（可能 10s+），
        如果多个请求同时到达，都会排队等 token 刷新。

        现在：启动时后台预取前 3 个账号的 token，首个请求直接命中缓存。
        预取失败不阻塞启动（failover 逻辑会在请求时重试）。
        """
        import threading
        count = min(count, len(self._accounts))

        def _prefetch_worker(idx: int) -> None:
            try:
                token = self._get_access_token_for_index(idx)
                self.logger.info("启动预取 token 成功 account=%s", idx)
            except Exception as exc:
                self.logger.warning("启动预取 token 失败 account=%s error=%s", idx, exc)

        threads = []
        for idx in range(count):
            t = threading.Thread(target=_prefetch_worker, args=(idx,), daemon=True)
            t.start()
            threads.append(t)
        self.logger.info("启动预取 token count=%s/%s", count, len(self._accounts))

    def get_browser_headers(self, app_fr: str = "browser_extension") -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*" if app_fr == "default" else "text/event-stream",
            "Accept-Encoding": "gzip, deflate" if app_fr == "default" else "identity",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "App-Name": "chatglm",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Origin": "https://chatglm.cn",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Sec-Ch-Ua": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": self.config.glm_user_agent,
            "X-App-Fr": app_fr,
            "X-App-Platform": "pc",
            "X-App-Version": "0.0.1",
            "X-Device-Brand": "",
            "X-Device-Model": "",
            "X-Lang": "zh",
            "X-Forwarded-For": build_random_x_forwarded_for(),
        }

    def read_json_response(self, response) -> dict[str, object]:
        try:
            raw_body = response.read()
            content_encoding = response.headers.get("Content-Encoding", "").lower()

            if content_encoding == "gzip":
                raw_body = gzip.decompress(raw_body)

            debug_dump(self.logger, self.config.debug_dump_all, "GLM 原始 JSON 响应体", raw_body)
            payload = json.loads(raw_body.decode("utf-8"))
        except gzip.BadGzipFile as exc:
            raise RuntimeError("GLM 响应 gzip 解压失败") from exc
        except UnicodeDecodeError as exc:
            raise RuntimeError("GLM 响应不是合法 UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"GLM 响应不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"GLM 响应格式异常，期望 JSON 对象，实际是: {type(payload).__name__}")
        return payload

    def get_access_token(self) -> str:
        with self._lock:
            return self._get_access_token_for_index(self._current_index)

    def get_account_count(self) -> int:
        return len(self._accounts)

    def validate_refresh_token(self, refresh_token: str) -> AccessToken:
        """验证 refresh_token 是否可用：实际调用 GLM 上游换取 access_token。
        
        流程：
        1. 用临时 device_id + 提供的 refresh_token 调用 /refreshToken
        2. 上游返回 access_token → 验证通过，返回 AccessToken（含 access_token + 可能刷新后的 refresh_token）
        3. 上游返回错误（token 失效 / 过期 / 风控）→ 抛 ValueError 含明确错误信息
        
        不修改内部状态，不持锁，可安全在 admin 接口同步调用。
        """
        if not refresh_token or not refresh_token.strip():
            raise ValueError("refresh_token 为空")
        refresh_token = refresh_token.strip()
        # 拒绝 guest marker / 占位符，避免误把游客模式标记当真实 token 加
        if is_guest_token_value(refresh_token):
            raise ValueError("refresh_token 无效（不能使用游客占位符）")
        device_id = uuid.uuid4().hex
        timestamp, nonce, sign = build_sign()
        request_id = f"{device_id[:8]}-{int(time.time()*1000)}-1"
        request = urllib.request.Request(
            self.config.refresh_url,
            data=b"{}",
            method="POST",
            headers={
                **self.get_browser_headers(),
                "Authorization": f"Bearer {refresh_token}",
                "X-Device-Id": device_id,
                "X-Nonce": nonce,
                "X-Request-Id": request_id,
                "X-Sign": sign,
                "X-Timestamp": timestamp,
            },
        )
        debug_dump(self.logger, self.config.debug_dump_all, "GLM 验证 refresh_token 请求头", dict(request.header_items()))
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                payload = self.read_json_response(response)
        except urllib.error.HTTPError as exc:
            # 4xx/5xx：尝试读 body 看具体错误码
            body = ""
            try:
                raw = exc.read()
                body = raw.decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            msg = f"GLM 上游返回 HTTP {exc.code}"
            if body:
                msg += f": {body}"
            raise ValueError(msg) from exc
        except urllib.error.URLError as exc:
            raise ValueError(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ValueError(f"请求超时：{exc}") from exc

        code = payload.get("code", payload.get("status"))
        result = payload.get("result") or {}
        access_token = result.get("access_token")
        new_refresh_token = result.get("refresh_token", refresh_token)
        # 上游返回错误码（token 失效/过期/风控等）
        if code not in {0, None} or not access_token:
            # 常见错误码翻译，让 admin 看到明确原因
            err_msg = result.get("msg") or payload.get("msg") or str(payload)
            raise ValueError(f"token 不可用（code={code}）：{err_msg}")
        return AccessToken(
            access_token=str(access_token),
            refresh_token=str(new_refresh_token),
            expires_at=time.time() + ACCESS_TOKEN_EXPIRES_SECONDS - random.randint(10, 30),
        )

    def add_user_account(self, refresh_token: str) -> int:
        """添加用户账号（非游客）。
        
        流程：
        1. 调用 validate_refresh_token 实际访问 GLM 上游验证 token 可用性
        2. 验证失败 → 抛 ValueError（含具体原因），账号不会被添加
        3. 验证成功 → append AccountState（已带 cached_token，首次请求 0 延迟）
        4. 持久化 refresh_token 到 token 文件（重启后保留）
        
        返回新账号的 index。
        """
        # 先验证（HTTP 调用，不持锁）
        access_token = self.validate_refresh_token(refresh_token)
        refresh_token = access_token.refresh_token  # 用上游可能刷新后的新 token
        with self._lock:
            idx = len(self._accounts)
            new_account = AccountState(
                refresh_token=refresh_token,
                is_guest=False,
                device_id=_load_or_create_device_id(idx),
                cached_token=access_token,  # 复用验证拿到的 token，首次请求 0 延迟
            )
            self._accounts.append(new_account)
            self.config.glm_refresh_tokens.append(refresh_token)
            self.logger.info(
                "动态添加用户账号 index=%s, 总账号数=%s, access_token 剩余=%.0fs",
                idx, len(self._accounts), access_token.expires_at - time.time(),
            )
        # 锁外持久化（best-effort，失败不影响添加成功）
        try:
            self._persist_new_user_account(refresh_token)
        except Exception as exc:
            self.logger.warning("持久化新账号失败 index=%s error=%s", idx, exc)
        return idx

    def _persist_new_user_account(self, refresh_token: str) -> None:
        """把新添加的用户账号 refresh_token 持久化到 token 文件（追加一行）。
        
        策略：
        - 若 token 文件已存在 → 追加一行
        - 若 .env 中 GLM_REFRESH_TOKEN 已存在但 token 文件不存在 → 
          改为创建 token 文件包含原 .env token + 新 token（保证多账号正确加载）
        - 都不存在 → 创建 token 文件只含新 token
        """
        with self._persist_lock:
            token_file = self.config.token_file_path
            if token_file.exists():
                # 追加模式：直接 append 一行
                with token_file.open("a", encoding="utf-8") as f:
                    f.write(refresh_token + "\n")
                return
            # token 文件不存在：把 .env 中的 GLM_REFRESH_TOKEN（如果有）和新增的合并写入
            existing_tokens: list[str] = []
            env_path = self.config.env_file_path
            if env_path.exists():
                try:
                    for line in env_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("GLM_REFRESH_TOKEN="):
                            val = line[len("GLM_REFRESH_TOKEN="):].strip()
                            if val and not is_guest_token_value(val):
                                existing_tokens.append(val)
                            break
                except Exception:
                    pass
            existing_tokens.append(refresh_token)
            try:
                token_file.parent.mkdir(parents=True, exist_ok=True)
                token_file.write_text("\n".join(existing_tokens) + "\n", encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(f"写入 token 文件失败: {token_file} error={exc}") from exc

    def get_all_accounts_info(self) -> list[dict]:
        """返回所有账号的信息（用于管理面板显示）。"""
        with self._lock:
            result = []
            for idx, acc in enumerate(self._accounts):
                result.append({
                    "index": idx,
                    "is_guest": acc.is_guest,
                    "has_refresh_token": bool(acc.refresh_token),
                    "refresh_token_preview": (acc.refresh_token[:8] + "...") if acc.refresh_token and not acc.is_guest else "",
                    "device_id_short": acc.device_id[:8] if acc.device_id else "",
                    "request_id_counter": acc.request_id_counter,
                    "device_request_count": acc.device_request_count,
                })
            return result

    def get_current_account_index(self) -> int:
        with self._lock:
            return self._current_index

    def is_guest_account(self, account_index: int) -> bool:
        with self._lock:
            return self._accounts[account_index].is_guest

    def get_device_id_for_account(self, account_index: int) -> str:
        """返回该账号当前的 device_id（每次启动随机生成，运行时可通过 rotate 轮换）。"""
        with self._lock:
            if 0 <= account_index < len(self._accounts):
                dev = self._accounts[account_index].device_id
                if dev:
                    return dev
            return uuid.uuid4().hex

    def rotate_device_id_for_account(self, account_index: int, reason: str = "rate_limited") -> str:
        """轮换该账号的 device_id：生成新 UUID + 清空 cached_token。
        
        触发场景：检测到游客频控（"多次体验过对话"）时调用。下一次请求会
        用新 device_id 重新 fetch guest token，从而拿到新的 user_id，
        绕过按 device_id 计数的风控。

        优化（2026-06-17）：rotate 后立即触发后台异步预 fetch 新 token，
        下次请求命中预 fetch 池时 0 延迟；池空退回同步 fetch 兜底。

        参数 reason：用于 admin 审计日志区分触发原因
          - "rate_limited" (默认)：检测到游客频控被动触发
          - "manual"：管理面板手动触发
          - "proactive"：达到阈值主动轮换（注意：此路径在 next_request_id_for_account 中独立处理）
        """
        with self._lock:
            if 0 <= account_index < len(self._accounts):
                acc = self._accounts[account_index]
                old_dev = acc.device_id[:8] if acc.device_id else "None"
                acc.device_id = uuid.uuid4().hex
                acc.cached_token = None
                acc.request_id_counter = 0
                # 废弃旧的预 fetch（device_id 已变）
                acc.prefetched_token = None
                acc.prefetched_device_id = ""
                self.logger.info(
                    "account=%s device_id 已轮换 %s → %s... (reason=%s)",
                    account_index, old_dev, acc.device_id[:8], reason,
                )
                new_dev = acc.device_id
                new_dev_short = acc.device_id[:8]
            else:
                return uuid.uuid4().hex
        # 锁外触发后台 prefetch（避免持锁等待网络）
        self._trigger_prefetch(account_index, new_dev)
        # 通知 admin store 记录轮换事件（best-effort）
        try:
            from ..admin.store import get_store
            get_store().record_rotate(account_index, old_dev, new_dev_short, reason)
        except Exception:
            pass
        return new_dev

    def next_request_id_for_account(self, account_index: int) -> str:
        """生成基于 device_id 的 request_id，保证全局唯一且可追溯。
        
        副作用：累计 device_request_count，超阈值时主动轮换 device_id。
        主动轮换是关键优化：避免"用到失败再换"的反应式策略，
        把风控窗口前的请求都打到新 device_id，降低失败重试率。
        """
        with self._lock:
            if 0 <= account_index < len(self._accounts):
                acc = self._accounts[account_index]
                acc.request_id_counter += 1
                acc.device_request_count += 1
                # 主动轮换：超过阈值时换新 device_id（清 cached_token 强制下次重新 fetch）
                # 默认 8 次，可配置 GLM_DEVICE_ID_ROTATE_THRESHOLD
                threshold = getattr(self.config, "device_id_rotate_threshold", 8)
                rotated_dev: str | None = None
                rotated_old_dev: str = ""
                if threshold > 0 and acc.device_request_count >= threshold:
                    old_dev = acc.device_id[:8]
                    acc.device_id = uuid.uuid4().hex
                    acc.cached_token = None  # 强制重新 fetch guest token
                    acc.request_id_counter = 0
                    acc.device_request_count = 0
                    # 废弃旧的预 fetch（device_id 已变）
                    acc.prefetched_token = None
                    acc.prefetched_device_id = ""
                    self.logger.info(
                        "account=%s 主动轮换 device_id %s → %s... (达到阈值 %d)",
                        account_index, old_dev, acc.device_id[:8], threshold,
                    )
                    rotated_dev = acc.device_id
                    rotated_old_dev = old_dev
                req_id = f"{acc.device_id[:8]}-{int(time.time()*1000)}-{acc.request_id_counter}"
            else:
                return uuid.uuid4().hex
        # 锁外触发后台 prefetch（如果刚轮换）
        if rotated_dev is not None:
            self._trigger_prefetch(account_index, rotated_dev)
            # 通知 admin store 记录主动轮换事件（best-effort）
            try:
                from ..admin.store import get_store
                get_store().record_rotate(account_index, rotated_old_dev, rotated_dev[:8], "proactive")
            except Exception:
                pass
        return req_id

    def _trigger_prefetch(self, account_index: int, device_id: str) -> None:
        """后台异步预 fetch guest token，避免下次请求同步阻塞。
        
        设计要点：
        1. 只对游客账号做预 fetch（登录账号 refresh_token 已存在，无需预取）
        2. 已有预 fetch 在跑时不重复触发
        3. 线程内不持锁发 HTTP；完成后持锁写结果，并校验 device_id 未被再次 rotate
        4. 失败静默（兜底同步 fetch 会再试一次）
        """
        with self._lock:
            if not (0 <= account_index < len(self._accounts)):
                return
            acc = self._accounts[account_index]
            if not acc.is_guest:
                return
            if acc.prefetch_in_progress:
                return
            acc.prefetch_in_progress = True

        def _do_prefetch() -> None:
            token: AccessToken | None = None
            try:
                # 锁外发 HTTP，传入当前 device_id（不再触发 next_request_id 递增）
                token = self._fetch_guest_token_raw(device_id)
            except Exception as exc:
                self.logger.debug(
                    "预 fetch guest token 失败 account=%s dev=%s... error=%s",
                    account_index, device_id[:8], exc,
                )
            finally:
                with self._lock:
                    acc2 = self._accounts[account_index]
                    acc2.prefetch_in_progress = False
                    # 校验：device_id 是否仍然有效（可能已被再次 rotate）
                    if token is not None and acc2.device_id == device_id:
                        acc2.prefetched_token = token
                        acc2.prefetched_device_id = device_id
                        self.logger.info(
                            "预 fetch 完成 account=%s dev=%s... (池内 1 个 token)",
                            account_index, device_id[:8],
                        )
                    # else: device_id 已变，token 废弃

        threading.Thread(
            target=_do_prefetch,
            name=f"glm-prefetch-{account_index}",
            daemon=True,
        ).start()

    def _fetch_guest_token_raw(self, device_id: str) -> AccessToken:
        """底层 guest token fetch，使用指定的 device_id 而非读取账号当前值。
        
        与 _fetch_guest_access_token 的区别：
        - 不调用 next_request_id_for_account（避免触发主动轮换）
        - 不写入 account.refresh_token（仅返回 token 给调用方）
        - 适用于预 fetch 场景；正式请求路径仍走 _fetch_guest_access_token
        """
        timestamp, nonce, sign = build_sign()
        # 预 fetch 用一个独立的 request_id（不递增 account.request_id_counter）
        request_id = f"{device_id[:8]}-pf-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}"
        request = urllib.request.Request(
            self.config.guest_refresh_url,
            data=b"",
            method="POST",
            headers={
                **self.get_browser_headers(app_fr="default"),
                "Content-Length": "0",
                "Referer": "https://chatglm.cn/",
                "X-Device-Id": device_id,
                "X-Nonce": nonce,
                "X-Request-Id": request_id,
                "X-Sign": sign,
                "X-Timestamp": timestamp,
            },
        )
        debug_dump(self.logger, self.config.debug_dump_all, f"GLM 预 fetch 游客 token 请求头 dev={device_id[:8]}", dict(request.header_items()))
        with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
            payload = self.read_json_response(response)
        code = payload.get("code", payload.get("status"))
        result = payload.get("result") or {}
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token")
        if response.status != 200 or code not in {0, None} or not access_token or not refresh_token:
            raise RuntimeError(f"预 fetch GLM 游客 token 失败: {payload}")
        return AccessToken(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            expires_at=time.time() + ACCESS_TOKEN_EXPIRES_SECONDS - random.randint(10, 30),
        )

    def advance_account(self, failed_index: int, reason: str) -> int:
        with self._lock:
            if failed_index != self._current_index:
                return self._current_index
            next_index = (failed_index + 1) % len(self._accounts)
            self._current_index = next_index
            self.logger.warning(
                "账号请求失败，切换 refresh_token 账号 index=%s -> %s reason=%s",
                failed_index,
                next_index,
                reason,
            )
            return next_index

    def reset_account_cycle(self) -> None:
        with self._lock:
            self._current_index = 0

    def invalidate_account(self, account_index: int) -> None:
        with self._lock:
            self._accounts[account_index].cached_token = None

    def get_access_token_for_account(self, account_index: int) -> str:
        # v54 C1: 不再持外层锁 — _get_access_token_for_index 内部自己管理锁
        # （double-checked locking + 锁外 HTTP，避免锁内做网络 I/O 串行化所有账号）
        return self._get_access_token_for_index(account_index)

    def _get_access_token_for_index(self, account_index: int) -> str:
        """v54 C1: double-checked locking + 锁外 HTTP。

        之前整个方法体在 self._lock 内，而 _refresh_access_token 会做 HTTP（最长 60s），
        导致所有账号的 token 获取被串行化（100 并发槽位实际只能服务 1 个请求）。

        现在：
        1. 第一次持锁：检查 cached_token / prefetched_token，命中则立即返回
        2. 锁外：做 HTTP 刷新（可能多个线程同时刷新，结果一致，无害）
        3. 第二次持锁：CAS 写入（如果其他线程已刷新，用其结果）
        """
        # 第一次检查（持锁）
        with self._lock:
            account = self._accounts[account_index]
            if account.cached_token and time.time() < account.cached_token.expires_at - 60:
                self.logger.debug("使用缓存 access_token account=%s 剩余=%.0fs", account_index, account.cached_token.expires_at - time.time())
                return account.cached_token.access_token
            # 命中预 fetch 池：原子交换到 cached_token，0 延迟
            if (
                account.prefetched_token is not None
                and account.prefetched_device_id == account.device_id
                and time.time() < account.prefetched_token.expires_at - 60
            ):
                account.cached_token = account.prefetched_token
                account.prefetched_token = None
                account.prefetched_device_id = ""
                self.logger.info(
                    "命中预 fetch 池 account=%s dev=%s... 剩余=%.0fs",
                    account_index, account.cached_token.refresh_token[:8] if account.cached_token.refresh_token else "?",
                    account.cached_token.expires_at - time.time(),
                )
                return account.cached_token.access_token
            # 都失效，需要刷新 — 释放锁后做 HTTP
            need_refresh = True

        if need_refresh:
            # 锁外做 HTTP（_refresh_access_token 内部调用 next_request_id_for_account /
            # get_device_id_for_account，它们自己加锁，所以锁外调用安全）
            new_token = self._refresh_access_token(account_index)
            # 第二次检查（持锁）：CAS 写入
            with self._lock:
                account = self._accounts[account_index]
                # 如果其他线程已经刷新了有效 token，用它的结果（避免覆盖更新的 token）
                if account.cached_token and time.time() < account.cached_token.expires_at - 60:
                    return account.cached_token.access_token
                account.cached_token = new_token
                return account.cached_token.access_token
        # 理论上不会走到这里（need_refresh 要么 True 要么在第一次检查返回）
        with self._lock:
            account = self._accounts[account_index]
            return account.cached_token.access_token if account.cached_token else ""

    def _refresh_access_token(self, account_index: int) -> AccessToken:
        account = self._accounts[account_index]
        if account.is_guest or not account.refresh_token:
            return self._fetch_guest_access_token(account_index)
        timestamp, nonce, sign = build_sign()
        request = urllib.request.Request(
            self.config.refresh_url,
            data=b"{}",
            method="POST",
            headers={
                **self.get_browser_headers(),
                "Authorization": f"Bearer {account.refresh_token}",
                "X-Device-Id": self.get_device_id_for_account(account_index),
                "X-Nonce": nonce,
                "X-Request-Id": self.next_request_id_for_account(account_index),
                "X-Sign": sign,
                "X-Timestamp": timestamp,
            },
        )
        debug_dump(self.logger, self.config.debug_dump_all, f"GLM 刷新 access_token 请求头 account={account_index}", dict(request.header_items()))
        debug_dump(self.logger, self.config.debug_dump_all, f"GLM 刷新 access_token 请求体 account={account_index}", b"{}")
        with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
            payload = self.read_json_response(response)
        code = payload.get("code", payload.get("status"))
        result = payload.get("result") or {}
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token", account.refresh_token)
        if response.status != 200 or code not in {0, None} or not access_token:
            raise RuntimeError(f"刷新 GLM token 失败: {payload}")
        if refresh_token != account.refresh_token:
            try:
                self._persist_refresh_token(account_index, refresh_token)
            except Exception as exc:
                self.logger.warning("写回 GLM refresh_token 失败 index=%s error=%s", account_index, exc)
            account.refresh_token = refresh_token
            self.config.glm_refresh_tokens[account_index] = refresh_token
            if account_index == 0:
                self.config.glm_refresh_token = refresh_token
            self.logger.info("GLM refresh_token 已自动刷新并写回账号存储 index=%s", account_index)
        return AccessToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + ACCESS_TOKEN_EXPIRES_SECONDS - random.randint(10, 30),
        )

    def _fetch_guest_access_token(self, account_index: int) -> AccessToken:
        """获取游客 access_token。

        v47 修复：使用独立的短超时（10秒）而非 request_timeout（120秒）。
        之前所有 100 个账号都需要刷新时，串行 × 120s = 3.3 小时。
        现在 100 × 10s = 1000s（16 分钟），加上 v39 的快速失败检测，
        大部分情况下账号池能在几秒内恢复。
        """
        account = self._accounts[account_index]
        timestamp, nonce, sign = build_sign()
        request_id = self.next_request_id_for_account(account_index)
        device_id = self.get_device_id_for_account(account_index)
        request = urllib.request.Request(
            self.config.guest_refresh_url,
            data=b"",
            method="POST",
            headers={
                **self.get_browser_headers(app_fr="default"),
                "Content-Length": "0",
                "Referer": "https://chatglm.cn/",
                "X-Device-Id": device_id,
                "X-Nonce": nonce,
                "X-Request-Id": request_id,
                "X-Sign": sign,
                "X-Timestamp": timestamp,
            },
        )
        debug_dump(self.logger, self.config.debug_dump_all, f"GLM 游客 token 请求头 account={account_index}", dict(request.header_items()))
        debug_dump(self.logger, self.config.debug_dump_all, f"GLM 游客 token 请求体 account={account_index}", b"")
        # v47: 游客 token 刷新用 10 秒超时（不是 120 秒）
        # 如果智谱服务器响应慢，快速失败让 failover 逻辑切到下一个账号
        guest_token_timeout = min(10, self.config.request_timeout)
        try:
            with urllib.request.urlopen(request, timeout=guest_token_timeout) as response:
                payload = self.read_json_response(response)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"获取游客 token 网络超时 account={account_index} timeout={guest_token_timeout}s: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"获取游客 token 超时 account={account_index} timeout={guest_token_timeout}s: {exc}") from exc
        code = payload.get("code", payload.get("status"))
        result = payload.get("result") or {}
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token")
        if code not in {0, None} or not access_token or not refresh_token:
            raise RuntimeError(f"获取 GLM 游客 token 失败: {payload}")
        account.refresh_token = str(refresh_token)
        self.logger.info("已获取新的 GLM 游客 refresh_token index=%s", account_index)
        return AccessToken(
            access_token=str(access_token),
            refresh_token=str(refresh_token),
            expires_at=time.time() + ACCESS_TOKEN_EXPIRES_SECONDS - random.randint(10, 30),
        )

    def _persist_refresh_token(self, account_index: int, refresh_token: str) -> None:
        with self._persist_lock:
            if self._accounts[account_index].is_guest:
                return
            if self.config.token_file_path.exists() or len(self.config.glm_refresh_tokens) > 1:
                tokens = list(self.config.glm_refresh_tokens)
                tokens[account_index] = refresh_token
                content = "\n".join(tokens) + "\n"
                try:
                    self.config.token_file_path.write_text(content, encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(f"写入 token 文件失败: {self.config.token_file_path} error={exc}") from exc
                return
            self._persist_env_refresh_token(refresh_token)

    def _persist_env_refresh_token(self, refresh_token: str) -> None:
        env_path = self.config.env_file_path
        if not env_path.exists():
            self.logger.warning(".env 文件不存在，无法自动写回新的 refresh_token")
            return

        try:
            content = env_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f".env 不是有效的 UTF-8 编码: {env_path}") from exc
        except OSError as exc:
            raise RuntimeError(f"读取 .env 失败: {env_path} error={exc}") from exc
        lines = content.splitlines()
        updated = False

        for index, line in enumerate(lines):
            if line.startswith("GLM_REFRESH_TOKEN="):
                lines[index] = f"GLM_REFRESH_TOKEN={refresh_token}"
                updated = True
                break

        if not updated:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"GLM_REFRESH_TOKEN={refresh_token}")

        new_content = "\n".join(lines) + "\n"
        try:
            env_path.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"写入 .env 失败: {env_path} error={exc}") from exc

    def should_switch_account(self, exc: Exception) -> bool:
        """v54 H3: 只在 token 失效或上游服务异常时切换账号。

        之前所有 UpstreamAPIError / HTTPError / URLError / TimeoutError 都切换，
        导致 400/422 用户错误（如 model=invalid）和网络抖动会清空所有账号的 cached_token，
        叠加 token 刷新串行化导致雪崩。
        """
        # UpstreamAPIError — 按 status_code 区分
        if hasattr(exc, "status_code"):
            code = getattr(exc, "status_code", 0) or 0
            # 401/403: token 失效 → 切换账号
            # 429: busy → 切换账号（可能该账号被频控）
            # 5xx: 上游服务异常 → 切换账号
            # 400/422/413/404: 用户错误 → 不切换（切了也没用，新账号同样会拒绝）
            if code in (401, 403, 429) or 500 <= code < 600:
                return True
            return False
        # HTTPError — 同上按 code 区分
        if isinstance(exc, urllib.error.HTTPError):
            code = exc.code or 0
            if code in (401, 403, 429) or 500 <= code < 600:
                return True
            return False
        # URLError / TimeoutError: 网络问题 — 不切换（切到下一账号大概率同样失败，
        # 反而清空 cached_token。调用方的 busy retry 会重试当前账号）
        if isinstance(exc, urllib.error.URLError):
            return False
        if isinstance(exc, TimeoutError):
            return False
        if isinstance(exc, RuntimeError):
            return "token" in str(exc).lower()
        return False
