"""Hermes plugin entry point for Feishu A2A collaboration."""

from __future__ import annotations

import logging
import os
from typing import Any

from .adapter_patch import install_feishu_adapter_patch
from .core import FeishuA2ACoordinator

logger = logging.getLogger(__name__)
_coordinator: FeishuA2ACoordinator | None = None


def get_coordinator() -> FeishuA2ACoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = FeishuA2ACoordinator.from_environment()
    return _coordinator


def _pre_llm_call(**kwargs: Any):
    return get_coordinator().pre_llm_call(**kwargs)


def _post_llm_call(**kwargs: Any):
    return get_coordinator().post_llm_call(**kwargs)


def register(ctx: Any) -> None:
    coordinator = get_coordinator()
    if not coordinator.enabled:
        logger.info("Hermes Feishu A2A disabled by HERMES_FEISHU_A2A_ENABLED")
        return

    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", _pre_llm_call)
        ctx.register_hook("post_llm_call", _post_llm_call)
    elif hasattr(ctx, "on"):
        ctx.on("pre_llm_call", _pre_llm_call)
        ctx.on("post_llm_call", _post_llm_call)
    else:
        logger.warning("Hermes plugin context has no register_hook/on method; lifecycle hooks not registered")

    if os.getenv("HERMES_FEISHU_PATCH_ADAPTER", "true").strip().lower() in {"1", "true", "yes", "on"}:
        install_feishu_adapter_patch(coordinator)

    logger.info(
        "Hermes Feishu A2A loaded: %d agent(s), self=%s",
        len(coordinator.registry.agents),
        getattr(coordinator.registry.self_agent(), "name", None),
    )


__all__ = ["register", "get_coordinator", "FeishuA2ACoordinator"]
