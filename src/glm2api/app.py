from __future__ import annotations

import errno
import os
import signal
import threading
import traceback

from .config import AppConfig, ConfigError, load_config
from .logging_utils import get_logger, setup_logging
from .server import GLM2APIServer
from .services.glm_client import GLMWebClient


class StartupError(RuntimeError):
    pass


class Application:
    def __init__(self, config: AppConfig) -> None:
        setup_logging(config.log_level)
        self.config = config
        self.logger = get_logger("glm2api.app")
        self.logger.info(
            "初始化应用 并发=%s 账号数=%s 暴露模型=%s",
            config.glm_max_concurrency,
            len(config.glm_refresh_tokens),
            len(config.exposed_models),
        )
        self.client = GLMWebClient(config=config, logger=get_logger("glm2api.glm"))
        try:
            self.server = GLM2APIServer(
                config=config,
                glm_client=self.client,
                logger=get_logger("glm2api.http"),
            )
        except OSError as exc:
            raise self._wrap_server_error(exc) from exc
        except Exception as exc:
            raise StartupError(f"初始化 HTTP 服务失败: {exc}") from exc
        self._stopping = False
        self._bypass_proxy_thread: threading.Thread | None = None
        self._bypass_proxy_server = None
        self._install_signal_handlers()

    def _maybe_start_bypass_proxy(self) -> None:
        """v37: 内嵌 WAF bypass proxy（用户不需要手动启动外部 proxy）。

        环境变量 WAF_BYPASS_PORT 设置后，glm2api 主进程额外监听一个端口，
        该端口接收的请求会先替换反引号（绕过 WAF），然后内部转发到主端口。

        用法：
          export WAF_BYPASS_PORT=8001
          # glm2api 启动后，Claude Code 连到 bypass 端口：
          export ANTHROPIC_BASE_URL=http://127.0.0.1:8001
        """
        bypass_port_str = os.environ.get("WAF_BYPASS_PORT", "").strip()
        if not bypass_port_str:
            return
        try:
            bypass_port = int(bypass_port_str)
        except ValueError:
            self.logger.warning("WAF_BYPASS_PORT 无效: %s", bypass_port_str)
            return
        from .waf_bypass import EmbeddedBypassProxy
        try:
            self._bypass_proxy_server = EmbeddedBypassProxy(
                listen_host=self.config.host,
                listen_port=bypass_port,
                target_host="127.0.0.1",
                target_port=self.config.port,
                logger=get_logger("glm2api.waf_bypass"),
            )
            self._bypass_proxy_thread = threading.Thread(
                target=self._bypass_proxy_server.serve_forever,
                daemon=True, name="waf-bypass-proxy")
            self._bypass_proxy_thread.start()
            self.logger.info(
                "WAF bypass proxy 已启动: http://%s:%s → http://127.0.0.1:%s",
                self.config.host, bypass_port, self.config.port)
        except Exception as exc:
            self.logger.warning("WAF bypass proxy 启动失败 port=%s error=%s", bypass_port, exc)

    def run(self) -> None:
        if self.config.env_file_created:
            self.logger.info("未检测到配置文件，已自动从默认示例复制: %s", self.config.env_file_path)
        self.logger.info(
            "启动服务 host=%s port=%s prefix=%s accounts=%s debug_dump_all=%s models=%s",
            self.config.host,
            self.config.port,
            self.config.api_prefix,
            len(self.config.glm_refresh_tokens),
            self.config.debug_dump_all,
            ",".join(self.config.exposed_models),
        )
        # v37: 启动内嵌 WAF bypass proxy（如果配置了 WAF_BYPASS_PORT）
        self._maybe_start_bypass_proxy()
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            self.logger.info("收到 Ctrl+C，正在优雅关闭服务...")
        except OSError as exc:
            self.logger.error("HTTP 服务运行时异常 error=%s", exc)
            raise StartupError(f"HTTP 服务运行失败: {exc}") from exc
        except Exception as exc:
            self.logger.error("服务异常退出 error=%s\n%s", exc, traceback.format_exc())
            raise
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        self.logger.info("停止 HTTP 服务并释放监听端口...")
        # 停止 bypass proxy
        if self._bypass_proxy_server:
            try:
                self._bypass_proxy_server.shutdown()
            except Exception as exc:
                self.logger.warning("关闭 bypass proxy 时出现异常 error=%s", exc)
        try:
            self.server.shutdown()
        except Exception as exc:
            self.logger.warning("关闭 HTTP 服务时出现异常 error=%s", exc)
        self.logger.info("glm2api 已退出")

    def _install_signal_handlers(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, self._handle_signal)
            except (ValueError, AttributeError):
                continue

    def _handle_signal(self, signum: int, frame) -> None:
        signal_name = signal.Signals(signum).name
        self.logger.info("收到退出信号 %s，准备关闭服务...", signal_name)
        raise KeyboardInterrupt

    def _wrap_server_error(self, exc: OSError) -> StartupError:
        if exc.errno in {errno.EADDRINUSE, 10048}:
            return StartupError(f"端口已被占用: {self.config.host}:{self.config.port}")
        if exc.errno in {errno.EACCES, 10013}:
            return StartupError(f"没有权限监听地址: {self.config.host}:{self.config.port}")
        if exc.errno in {errno.EADDRNOTAVAIL, 10049}:
            return StartupError(f"监听地址不可用: {self.config.host}")
        return StartupError(f"启动 HTTP 服务失败: {exc}")


def create_application() -> Application:
    try:
        config = load_config()
    except ConfigError:
        raise
    except Exception as exc:
        raise StartupError(f"读取配置失败: {exc}") from exc
    return Application(config)
