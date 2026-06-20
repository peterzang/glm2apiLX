"""Tests for v22: 添加用户账号时必须先验证 token 可用性。

Covers:
- validate_refresh_token: 成功 / 上游错误码 / HTTP 异常 / 网络异常 / 空 token / 游客占位符
- add_user_account: 验证通过才添加 + 复用 cached_token + 持久化到 token 文件
- add_user_account: 验证失败不修改账号列表
"""
from __future__ import annotations

import sys
import json
import io
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from glm2api.config import GUEST_REFRESH_TOKEN_MARKER
from glm2api.services.glm_auth import GLMAccessTokenManager, AccountState, AccessToken


# === 测试夹具 ===

def _make_config(tmp_path: Path, *, guest_mode: bool = True, existing_tokens: list[str] | None = None):
    """构造一个最小可用的 AppConfig 用于测试。"""
    from glm2api.config import AppConfig

    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    token_file = tmp_path / "token.txt"
    if existing_tokens:
        token_file.write_text("\n".join(existing_tokens) + "\n", encoding="utf-8")

    if guest_mode:
        refresh_tokens = [GUEST_REFRESH_TOKEN_MARKER] * 3
    else:
        refresh_tokens = existing_tokens or []

    return AppConfig(
        env_file_path=env_path,
        env_file_created=False,
        token_file_path=token_file,
        host="127.0.0.1",
        port=8000,
        api_prefix="/v1",
        log_level="INFO",
        debug_dump_all=False,
        request_timeout=10,
        glm_base_url="https://chatglm.cn",
        glm_use_guest_refresh_token=guest_mode,
        glm_refresh_token=refresh_tokens[0] if refresh_tokens else "",
        glm_refresh_tokens=refresh_tokens,
        glm_assistant_id="test",
        glm_image_assistant_id="test_img",
        glm_image_model_name="test_img_model",
        glm_user_agent="test-ua",
        glm_delete_conversation=True,
        glm_max_concurrency=3,
        glm_queue_wait_timeout=600,
        glm_busy_max_retries=30,
        glm_busy_retry_interval=2.0,
        glm_guest_max_retries=3,
        device_id_rotate_threshold=8,
        blocked_tool_names=[],
        exposed_models=["glm-5.2"],
        model_aliases={},
        server_api_keys=[],
        cors_allow_origin="",
    )


def _make_response(payload: dict, status: int = 200):
    """构造一个 mock urlopen 返回的 context manager。"""
    body = json.dumps(payload).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = {"Content-Encoding": ""}
    mock_resp.read.return_value = body
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


@pytest.fixture
def manager(tmp_path):
    """构造一个游客模式的 manager（含 3 个游客账号）。"""
    import logging
    cfg = _make_config(tmp_path, guest_mode=True)
    return GLMAccessTokenManager(cfg, logging.getLogger("test"))


# === validate_refresh_token 测试 ===

def test_validate_empty_token_raises(manager):
    with pytest.raises(ValueError, match="为空"):
        manager.validate_refresh_token("")
    with pytest.raises(ValueError, match="为空"):
        manager.validate_refresh_token("   ")


def test_validate_guest_marker_raises(manager):
    with pytest.raises(ValueError, match="游客占位符"):
        manager.validate_refresh_token(GUEST_REFRESH_TOKEN_MARKER)
    with pytest.raises(ValueError, match="游客占位符"):
        manager.validate_refresh_token("guest")


def test_validate_success_returns_access_token(manager):
    """验证通过：返回包含 access_token 的 AccessToken。"""
    payload = {
        "code": 0,
        "result": {
            "access_token": "at_test_12345",
            "refresh_token": "rt_test_67890",
        },
    }
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        result = manager.validate_refresh_token("rt_input_token_xxx")
    assert result.access_token == "at_test_12345"
    assert result.refresh_token == "rt_test_67890"
    assert result.expires_at > 0


def test_validate_upstream_error_code_raises(manager):
    """上游返回错误码：抛 ValueError 含 code 和 msg。"""
    payload = {
        "code": 401,
        "msg": "token 已失效，请重新登录",
        "result": {},
    }
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        with pytest.raises(ValueError, match=r"code=401"):
            manager.validate_refresh_token("rt_invalid_token_xxx")


def test_validate_upstream_missing_access_token_raises(manager):
    """上游返回 code=0 但 access_token 缺失：抛 ValueError。"""
    payload = {"code": 0, "result": {}}
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        with pytest.raises(ValueError, match="不可用"):
            manager.validate_refresh_token("rt_token_xxx")


def test_validate_http_error_raises_with_status(manager):
    """HTTP 4xx/5xx：抛 ValueError 含状态码。"""
    err = urllib.error.HTTPError(
        url="https://chatglm.cn/refreshToken",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"msg":"token expired"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ValueError, match=r"HTTP 401"):
            manager.validate_refresh_token("rt_token_xxx")


def test_validate_network_error_raises(manager):
    """网络错误：抛 ValueError 含 reason。"""
    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(ValueError, match="网络错误"):
            manager.validate_refresh_token("rt_token_xxx")


def test_validate_timeout_raises(manager):
    """超时：抛 ValueError。"""
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(ValueError, match="超时"):
            manager.validate_refresh_token("rt_token_xxx")


def test_validate_does_not_modify_account_list(manager):
    """验证不修改内部状态：账号列表长度不变。"""
    before_count = len(manager._accounts)
    payload = {"code": 0, "result": {"access_token": "at", "refresh_token": "rt"}}
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        manager.validate_refresh_token("rt_input_xxx")
    assert len(manager._accounts) == before_count


# === add_user_account 测试 ===

def test_add_valid_account_succeeds_and_caches_token(manager):
    """添加成功：账号列表 +1，新账号含 cached_token，非游客。"""
    payload = {
        "code": 0,
        "result": {
            "access_token": "at_added_test",
            "refresh_token": "rt_input_refresh_token_for_test_only_long_enough",
        },
    }
    original_count = len(manager._accounts)
    with patch("urllib.request.urlopen", return_value=_make_response(payload)) as mock_open:
        idx = manager.add_user_account("rt_input_refresh_token_for_test_only_long_enough")

    assert idx == original_count  # 新账号 index == 原 list length
    assert len(manager._accounts) == original_count + 1
    new_acc = manager._accounts[idx]
    assert new_acc.is_guest is False
    assert new_acc.refresh_token == "rt_input_refresh_token_for_test_only_long_enough"
    assert new_acc.cached_token is not None
    assert new_acc.cached_token.access_token == "at_added_test"
    # 确实发起了 HTTP 调用
    assert mock_open.called


def test_add_invalid_account_does_not_modify_list(manager):
    """验证失败：账号列表不变。"""
    payload = {"code": 401, "msg": "token 失效", "result": {}}
    original_count = len(manager._accounts)
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        with pytest.raises(ValueError, match="不可用"):
            manager.add_user_account("rt_invalid_token_for_test_only_long_enough")
    assert len(manager._accounts) == original_count


def test_add_account_persists_to_token_file(tmp_path):
    """添加成功后：refresh_token 写入 token 文件，重启后可加载。"""
    import logging
    from glm2api.config import load_refresh_tokens

    cfg = _make_config(tmp_path, guest_mode=True)
    mgr = GLMAccessTokenManager(cfg, logging.getLogger("test"))

    payload = {"code": 0, "result": {
        "access_token": "at",
        "refresh_token": "rt_persist_test_token_xxxxxxxxxxxxx",
    }}
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        mgr.add_user_account("rt_persist_test_token_xxxxxxxxxxxxx")

    # token 文件已创建并含新 token
    assert cfg.token_file_path.exists()
    saved_tokens = load_refresh_tokens(cfg.token_file_path)
    assert "rt_persist_test_token_xxxxxxxxxxxxx" in saved_tokens


def test_add_account_persists_to_existing_token_file(tmp_path):
    """token 文件已存在：以追加模式写入，不破坏已有内容。"""
    import logging
    from glm2api.config import load_refresh_tokens

    existing = ["rt_existing_user_token_xxxxxxxxxxxxxx"]
    cfg = _make_config(tmp_path, guest_mode=False, existing_tokens=existing)
    mgr = GLMAccessTokenManager(cfg, logging.getLogger("test"))

    payload = {"code": 0, "result": {
        "access_token": "at",
        "refresh_token": "rt_new_added_token_yyyyyyyyyyyyy",
    }}
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        mgr.add_user_account("rt_new_added_token_yyyyyyyyyyyyy")

    saved = load_refresh_tokens(cfg.token_file_path)
    assert "rt_existing_user_token_xxxxxxxxxxxxxx" in saved
    assert "rt_new_added_token_yyyyyyyyyyyyy" in saved


def test_add_account_appends_to_config_list(manager):
    """添加成功后：config.glm_refresh_tokens 列表也 +1，保持同步。"""
    payload = {"code": 0, "result": {
        "access_token": "at",
        "refresh_token": "rt_config_sync_token_zzzzzzzzzzzzzzz",
    }}
    before = len(manager.config.glm_refresh_tokens)
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        manager.add_user_account("rt_config_sync_token_zzzzzzzzzzzzzzz")
    assert len(manager.config.glm_refresh_tokens) == before + 1
    assert manager.config.glm_refresh_tokens[-1] == "rt_config_sync_token_zzzzzzzzzzzzzzz"


def test_added_account_first_request_uses_cached_token(manager):
    """添加后：cached_token 已设置，首次请求不需要再调上游。"""
    payload = {"code": 0, "result": {
        "access_token": "at_no_refetch_needed",
        "refresh_token": "rt_no_refetch_token_www",
    }}
    with patch("urllib.request.urlopen", return_value=_make_response(payload)) as mock_open:
        idx = manager.add_user_account("rt_no_refetch_token_www")
        # 立即获取 access_token：应该复用 cached_token，不再发 HTTP
        mock_open.reset_mock()
        token = manager._get_access_token_for_index(idx)
    assert token == "at_no_refetch_needed"
    assert mock_open.call_count == 0  # 没有再次调用 urlopen


def test_get_account_count_increments(manager):
    """get_account_count 反映添加后的新数量。"""
    payload = {"code": 0, "result": {"access_token": "at", "refresh_token": "rt_count_test_long_enough"}}
    before = manager.get_account_count()
    with patch("urllib.request.urlopen", return_value=_make_response(payload)):
        manager.add_user_account("rt_count_test_long_enough")
    assert manager.get_account_count() == before + 1
