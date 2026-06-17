"""统一模型注册中心。

设计目标：
  - 单一数据源：所有地方（/v1/models, /admin/api/models, /admin/api/probe, /admin/api/probe_model）
    都通过本模块获取模型列表，避免"前端 20 几个模型后端又是几个模型"的不一致
  - 统一格式：每个模型都包含 id / base / features / is_variant / is_image_model / profile /
    last_probe / upstream_assistant（关联的真实助手元数据，可能为 None）
  - 动态元数据：真实助手信息通过 upstream_discovery 实时拉取，附加到本地模型上

模型来源：
  - 本地兼容模型：config.exposed_models（BUILTIN_EXPOSED_MODELS 展开变体后的列表）
    这是项目对外暴露的 OpenAI Chat Completions 协议层 model name
  - 真实上游助手：upstream_discovery.discover() 实时从 chatglm.cn 拉取
    通过 assistant_id 关联到本地模型（glm_assistant_id / glm_image_assistant_id）

model → upstream_assistant 映射规则：
  - 图像模型（glm-image-1）→ glm_image_assistant_id 对应的助手
  - 其他所有模型 → glm_assistant_id（默认对话助手）对应的助手
"""
from __future__ import annotations

from dataclasses import dataclass
from logging import Logger
from typing import Any, Dict, List, Optional

from ..config import AppConfig
from ..core.model_profiles import get_model_profile
from ..core.model_variants import split_model_features
from .glm_auth import GLMAccessTokenManager
from .upstream_discovery import UpstreamAssistant, get_upstream_discovery


@dataclass(slots=True)
class UnifiedModel:
    """统一模型对象，融合本地兼容模型 + 真实助手元数据。"""
    # === 本地兼容模型字段（来自 config.exposed_models）===
    id: str                          # model name（如 "glm-5.2-think"）
    base: str                        # 基础模型名（如 "glm-5.2"）
    features: List[str]              # 特性后缀（["think"] / ["search"] / ["think","search"] / []）
    is_variant: bool                 # 是否是变体（features 非空）
    is_image_model: bool             # 是否是图像模型
    profile: Dict[str, Any]          # 模型能力元信息（native_function_calling / preferred_format）
    last_probe: Optional[Dict[str, Any]]  # 最近一次探针结果（来自 admin store，可能为 None）

    # === 关联的真实上游助手元数据（可能为 None，表示无对应助手）===
    upstream_assistant_id: str = ""        # 关联的 chatglm.cn assistant_id
    upstream_name: str = ""                # 真实助手名（如 "ChatGLM"）
    upstream_description: str = ""         # 真实助手描述
    upstream_avatar: str = ""              # 真实助手头像 URL
    upstream_scope: int = 0                # 真实助手 scope
    upstream_enabled: bool = False         # 真实助手是否启用


def get_unified_models(
    config: AppConfig,
    logger: Logger,
    auth: GLMAccessTokenManager,
    probe_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    fetch_upstream: bool = True,
) -> List[UnifiedModel]:
    """获取统一模型列表。

    参数：
      config: AppConfig
      logger: Logger
      auth: GLMAccessTokenManager（用于 upstream_discovery）
      probe_cache: 探针结果缓存（来自 admin store）。如果为 None，last_probe 字段为 None
      fetch_upstream: 是否拉取真实上游助手元数据。False 时只返回本地模型（upstream_* 字段为空）

    返回：
      UnifiedModel 列表，按 config.exposed_models 顺序
    """
    # Step 1: 拉取真实上游助手（可选）
    assistant_map: Dict[str, UpstreamAssistant] = {}
    if fetch_upstream:
        try:
            discovery = get_upstream_discovery(config, logger, auth)
            assistants = discovery.discover(force_refresh=False)
            assistant_map = {a.assistant_id: a for a in assistants if not a.fetch_error}
        except Exception as exc:
            logger.warning("get_unified_models 拉取上游助手失败 error=%s", exc)

    # Step 2: 确定每个本地模型对应的真实助手 ID
    # 图像模型 → glm_image_assistant_id；其他 → glm_assistant_id
    image_model_name = config.glm_image_model_name
    chat_assistant_id = config.glm_assistant_id
    image_assistant_id = config.glm_image_assistant_id

    # Step 3: 构造统一模型列表
    if probe_cache is None:
        probe_cache = {}

    models: List[UnifiedModel] = []
    for model_id in config.exposed_models:
        base, features = split_model_features(model_id)
        profile = get_model_profile(base)
        is_image = model_id == image_model_name
        # 确定关联的 assistant_id
        related_aid = image_assistant_id if is_image else chat_assistant_id
        # 查找真实助手元数据
        upstream = assistant_map.get(related_aid)
        models.append(UnifiedModel(
            id=model_id,
            base=base,
            features=sorted(features),
            is_variant=bool(features),
            is_image_model=is_image,
            profile={
                "native_function_calling": profile.native_function_calling,
                "preferred_format": profile.preferred_format,
            },
            last_probe=probe_cache.get(model_id),
            upstream_assistant_id=related_aid,
            upstream_name=upstream.name if upstream else "",
            upstream_description=upstream.description if upstream else "",
            upstream_avatar=upstream.avatar if upstream else "",
            upstream_scope=upstream.scope if upstream else 0,
            upstream_enabled=upstream.enabled if upstream else False,
        ))
    return models


def get_orphan_assistants(
    config: AppConfig,
    logger: Logger,
    auth: GLMAccessTokenManager,
) -> List[UpstreamAssistant]:
    """获取"孤儿助手"：真实上游助手中没有对应本地兼容模型的助手。

    这些助手目前无法通过 OpenAI API 调用，需要项目添加对应的 model name 别名。
    用于 admin 面板展示"未映射助手"提示。
    """
    try:
        discovery = get_upstream_discovery(config, logger, auth)
        assistants = discovery.discover(force_refresh=False)
    except Exception as exc:
        logger.warning("get_orphan_assistants 拉取失败 error=%s", exc)
        return []

    # 项目已映射的 assistant_id 集合
    mapped_ids = {config.glm_assistant_id, config.glm_image_assistant_id}
    # 返回未映射且拉取成功的助手
    return [
        a for a in assistants
        if not a.fetch_error and a.assistant_id not in mapped_ids
    ]


def to_dict(model: UnifiedModel) -> Dict[str, Any]:
    """序列化 UnifiedModel 为 JSON 友好的 dict。"""
    return {
        "id": model.id,
        "base": model.base,
        "features": model.features,
        "is_variant": model.is_variant,
        "is_image_model": model.is_image_model,
        "profile": model.profile,
        "last_probe": model.last_probe,
        "upstream_assistant_id": model.upstream_assistant_id,
        "upstream_name": model.upstream_name,
        "upstream_description": model.upstream_description,
        "upstream_avatar": model.upstream_avatar,
        "upstream_scope": model.upstream_scope,
        "upstream_enabled": model.upstream_enabled,
    }
