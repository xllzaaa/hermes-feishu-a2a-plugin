"""Optional Hermes Feishu adapter runtime patch.

Hermes does not yet expose fine-grained inbound/outbound message hooks for this
workflow. This patch wires the coordinator into the current Feishu adapter without
editing Hermes itself:

- outbound ``send``: converts ``@AgentName`` to Feishu ``<at>`` tags;
- inbound bot messages: allows known bot senders through only when they mention
  this Hermes bot, then injects sender-return instructions for text messages.
"""

from __future__ import annotations

import json
import logging
import asyncio
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from .core import FeishuA2ACoordinator, extract_mentions_from_content


logger = logging.getLogger(__name__)

_PATCHED = False
_AT_TAG_PATTERN = re.compile(r'<at\s+user_id="(?P<id>[^"]+)">(?P<name>[^<]+)</at>')
_CHAT_MEMBER_CACHE: dict[tuple[str, str], tuple[float, dict[str, str]]] = {}
_CHAT_MEMBER_CACHE_TTL = 10 * 60
_EDIT_HANDOFF_SENT: set[tuple[str, str]] = set()
_EDIT_HANDOFF_TASKS: dict[tuple[str, str], asyncio.Task] = {}
_EDIT_HANDOFF_DELAY_SECONDS = 2.5


def install_feishu_adapter_patch(coordinator: FeishuA2ACoordinator) -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        from gateway.platforms.feishu import FeishuAdapter  # type: ignore
    except Exception as exc:
        logger.info("Feishu adapter patch not installed because gateway.platforms.feishu is unavailable: %s", exc)
        return False

    original_send = FeishuAdapter.send
    original_edit_message = FeishuAdapter.edit_message
    original_handle_event = FeishuAdapter._handle_message_event_data
    original_allow_group = FeishuAdapter._allow_group_message
    original_build_outbound_payload = FeishuAdapter._build_outbound_payload
    original_feishu_send_with_retry = FeishuAdapter._feishu_send_with_retry

    async def send_with_a2a(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self_open_id = getattr(self, "_bot_open_id", "") or ""
        transformed = coordinator.transform_outbound(content, self_open_id=self_open_id)
        transformed = await _resolve_at_tags_for_sender_app(self, coordinator, chat_id, transformed)
        result = await original_send(
            self,
            chat_id,
            transformed,
            reply_to=reply_to,
            metadata=metadata,
        )
        return result

    async def edit_message_with_a2a(self, chat_id: str, message_id: str, content: str):
        self_open_id = getattr(self, "_bot_open_id", "") or ""
        transformed = coordinator.transform_outbound(content, self_open_id=self_open_id)
        transformed = await _resolve_at_tags_for_sender_app(self, coordinator, chat_id, transformed)
        if transformed != content:
            _debug_print(f"edit rewrite: {content[:120]!r} -> {transformed[:160]!r}")
        result = await original_edit_message(self, chat_id, message_id, transformed)
        _schedule_edit_handoff_once(
            self,
            original_send,
            coordinator,
            chat_id=chat_id,
            message_id=message_id,
            content=transformed,
        )
        return result

    def build_outbound_payload_with_a2a(self, content: str):
        # Feishu only delivers bot-to-bot @ mentions from native text messages.
        # If Hermes formats the response as a post/rich-text message, the <at>
        # text can be visible but will not become a real blue mention nor trigger
        # the target bot. Force A2A handoffs to plain text.
        if '<at user_id="' in (content or ""):
            return "text", json.dumps({"text": content}, ensure_ascii=False)
        return original_build_outbound_payload(self, content)

    async def feishu_send_with_retry_a2a(self, *, chat_id: str, msg_type: str, payload: str, reply_to=None, metadata=None):
        new_msg_type, new_payload = await _rewrite_payload_at_bottom(
            self,
            coordinator,
            chat_id=chat_id,
            msg_type=msg_type,
            payload=payload,
        )
        return await original_feishu_send_with_retry(
            self,
            chat_id=chat_id,
            msg_type=new_msg_type,
            payload=new_payload,
            reply_to=reply_to,
            metadata=metadata,
        )

    def allow_group_with_a2a(self, sender_id: Any, chat_id: str = "") -> bool:
        sender_open_id = getattr(sender_id, "open_id", None) or ""
        if sender_open_id and coordinator.registry.by_open_id(sender_open_id):
            return True
        return original_allow_group(self, sender_id, chat_id)

    async def handle_event_with_a2a(self, data: Any) -> None:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)
        sender_open_id = getattr(sender_id, "open_id", None) or ""
        sender_type = str(getattr(sender, "sender_type", "") or "").lower()

        if sender_type == "bot" and sender_open_id:
            chat_type = getattr(message, "chat_type", "p2p") if message else "p2p"
            chat_id = getattr(message, "chat_id", "") or "" if message else ""
            raw_content = getattr(message, "content", "") or "" if message else ""
            mentioned_ids = set()
            mentioned_names = set()
            for mention in getattr(message, "mentions", None) or []:
                mention_id = getattr(mention, "id", None)
                if getattr(mention_id, "open_id", None):
                    mentioned_ids.add(getattr(mention_id, "open_id"))
                if getattr(mention_id, "user_id", None):
                    mentioned_ids.add(getattr(mention_id, "user_id"))
                for name_attr in ("name", "key", "user_name"):
                    value = getattr(mention, name_attr, None)
                    if isinstance(value, str) and value.strip():
                        mentioned_names.add(value.strip())
            mentioned_ids.update(extract_mentions_from_content(raw_content))
            mentioned_names.update(match.group("name") for match in _AT_TAG_PATTERN.finditer(raw_content))

            if chat_type != "p2p" and not coordinator.should_accept_bot_sender(
                sender_open_id=sender_open_id,
                mentioned_ids=mentioned_ids,
                mentioned_names=mentioned_names,
                chat_id=chat_id,
                text=raw_content,
            ):
                _debug_print(
                    "drop bot message without self mention "
                    f"sender={sender_open_id} chat={chat_id} ids={sorted(mentioned_ids)} names={sorted(mentioned_names)}"
                )
                logger.debug("[hermes-feishu-a2a] Dropping bot message without self mention: %s", sender_open_id)
                return

            if sender_type == "bot":
                _debug_print(
                    "accept bot message "
                    f"sender={sender_open_id} chat={chat_id} ids={sorted(mentioned_ids)} names={sorted(mentioned_names)}"
                )
            _inject_sender_info_into_text_message(coordinator, message, sender_open_id, chat_id)

            old_sender_type = getattr(sender, "sender_type", None)
            try:
                setattr(sender, "sender_type", "user")
                return await original_handle_event(self, data)
            finally:
                try:
                    setattr(sender, "sender_type", old_sender_type)
                except Exception:
                    pass

        return await original_handle_event(self, data)

    FeishuAdapter.send = send_with_a2a
    FeishuAdapter.edit_message = edit_message_with_a2a
    FeishuAdapter._build_outbound_payload = build_outbound_payload_with_a2a
    FeishuAdapter._feishu_send_with_retry = feishu_send_with_retry_a2a
    FeishuAdapter._handle_message_event_data = handle_event_with_a2a
    FeishuAdapter._allow_group_message = allow_group_with_a2a
    _PATCHED = True
    logger.info("Installed Hermes Feishu A2A adapter patch")
    return True


async def _rewrite_payload_at_bottom(
    adapter: Any,
    coordinator: FeishuA2ACoordinator,
    *,
    chat_id: str,
    msg_type: str,
    payload: str,
) -> tuple[str, str]:
    """Last-chance rewrite immediately before Feishu API delivery."""

    try:
        if msg_type == "text":
            body = json.loads(payload or "{}")
            text = str(body.get("text") or "")
            rewritten = coordinator.transform_outbound(text, self_open_id=getattr(adapter, "_bot_open_id", "") or "")
            rewritten = await _resolve_at_tags_for_sender_app(adapter, coordinator, chat_id, rewritten)
            if rewritten != text:
                _debug_print(f"bottom text rewrite: {text[:120]!r} -> {rewritten[:160]!r}")
                return "text", json.dumps({"text": rewritten}, ensure_ascii=False)
            if '<at user_id="' in rewritten:
                _debug_print(f"bottom text contains at tag: {rewritten[:160]!r}")
            return msg_type, payload

        if msg_type == "post":
            text = _extract_post_text(payload)
            if not text:
                return msg_type, payload
            rewritten = coordinator.transform_outbound(text, self_open_id=getattr(adapter, "_bot_open_id", "") or "")
            rewritten = await _resolve_at_tags_for_sender_app(adapter, coordinator, chat_id, rewritten)
            if rewritten != text or '<at user_id="' in rewritten:
                _debug_print(f"bottom post forced to text: {text[:120]!r} -> {rewritten[:160]!r}")
                return "text", json.dumps({"text": rewritten}, ensure_ascii=False)
    except Exception as exc:
        _debug_print(f"bottom rewrite failed: {exc}")
        logger.warning("[hermes-feishu-a2a] bottom rewrite failed: %s", exc)
    return msg_type, payload


def _schedule_edit_handoff_once(
    adapter: Any,
    original_send: Any,
    coordinator: FeishuA2ACoordinator,
    *,
    chat_id: str,
    message_id: str,
    content: str,
) -> None:
    if not chat_id or not message_id or '<at user_id="' not in (content or ""):
        return
    mentions = _AT_TAG_PATTERN.findall(content)
    if not mentions:
        return
    for target_open_id, _name in mentions:
        if (message_id, target_open_id) in _EDIT_HANDOFF_SENT:
            continue
        key = (message_id, target_open_id)
        old_task = _EDIT_HANDOFF_TASKS.get(key)
        if old_task and not old_task.done():
            old_task.cancel()
        _EDIT_HANDOFF_TASKS[key] = asyncio.create_task(
            _send_edit_handoff_after_quiet(
                adapter,
                original_send,
                coordinator,
                chat_id=chat_id,
                message_id=message_id,
                target_open_id=target_open_id,
                content=content,
                source_open_id=getattr(adapter, "_bot_open_id", "") or "",
            )
        )


async def _send_edit_handoff_after_quiet(
    adapter: Any,
    original_send: Any,
    coordinator: FeishuA2ACoordinator,
    *,
    chat_id: str,
    message_id: str,
    target_open_id: str,
    content: str,
    source_open_id: str,
) -> None:
    try:
        await asyncio.sleep(_EDIT_HANDOFF_DELAY_SECONDS)
        key = (message_id, target_open_id)
        if key in _EDIT_HANDOFF_SENT:
            return
        handoff = _extract_handoff_text_for_target(content, target_open_id)
        if not handoff or "▉" in handoff:
            _debug_print(f"edit mention handoff skipped incomplete stream: {handoff[:160]!r}")
            return
        _EDIT_HANDOFF_SENT.add(key)
        handoff = await _resolve_at_tags_for_sender_app(adapter, coordinator, chat_id, handoff)
        _debug_print(f"edit mention handoff create-send after quiet: {handoff[:220]!r}")
        try:
            await original_send(adapter, chat_id, handoff, reply_to=None, metadata=None)
        except Exception as exc:
            logger.warning("[hermes-feishu-a2a] edit handoff send failed: %s", exc)
            return
    except asyncio.CancelledError:
        return


def _extract_handoff_text_for_target(content: str, target_open_id: str) -> str:
    lines = [line.strip() for line in (content or "").splitlines()]
    selected: list[str] = []
    for index, line in enumerate(lines):
        if f'user_id="{target_open_id}"' not in line:
            continue
        selected.append(line)
        for next_line in lines[index + 1 : index + 5]:
            if not next_line or next_line == "---":
                break
            selected.append(next_line)
        break
    if not selected:
        return ""
    text = "\n".join(selected).strip()
    if len(text) > 1200:
        text = text[:1200].rstrip() + "..."
    return text


def _extract_post_text(payload: str) -> str:
    try:
        body = json.loads(payload or "{}")
    except json.JSONDecodeError:
        return ""
    chunks: list[str] = []
    _collect_post_text(body, chunks)
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _collect_post_text(value: Any, chunks: list[str]) -> None:
    if isinstance(value, dict):
        tag = value.get("tag")
        if tag in {"md", "text"} and isinstance(value.get("text"), str):
            chunks.append(value["text"])
        for item in value.values():
            _collect_post_text(item, chunks)
    elif isinstance(value, list):
        for item in value:
            _collect_post_text(item, chunks)


def _debug_print(message: str) -> None:
    if os.getenv("HERMES_FEISHU_A2A_DEBUG", "true").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[hermes-feishu-a2a-gateway-patch] {message}", flush=True)


async def _resolve_at_tags_for_sender_app(
    adapter: Any,
    coordinator: FeishuA2ACoordinator,
    chat_id: str,
    content: str,
) -> str:
    """Rewrite <at> ids to the open_id namespace visible to the sending app.

    Feishu can render an invalid <at> tag as plain "@Name" without creating a
    real mention.  This happens easily when multiple bot apps store each
    target's self-discovered open_id.  Before sending, look up the actual group
    member id from the current app's perspective and replace by display name.
    """

    if not chat_id or '<at user_id="' not in (content or ""):
        return content

    try:
        name_to_open_id = await asyncio.to_thread(_get_chat_bot_member_ids, adapter, coordinator, chat_id)
    except Exception as exc:
        logger.warning("[hermes-feishu-a2a] Failed to resolve chat bot mention ids for %s: %s", chat_id, exc)
        return content

    if not name_to_open_id:
        _debug_print(f"no chat bot member ids resolved for chat={chat_id}")
        return content

    def replace(match):
        old_open_id = match.group("id")
        display_name = match.group("name")
        key = coordinator.registry.normalize_name(display_name)
        new_open_id = name_to_open_id.get(key)
        if not new_open_id or new_open_id == old_open_id:
            return match.group(0)
        logger.info(
            "[hermes-feishu-a2a] Rewrote @%s mention id for current sender app: %s -> %s",
            display_name,
            old_open_id,
            new_open_id,
        )
        _debug_print(f"rewrote @{display_name}: {old_open_id} -> {new_open_id}")
        return f'<at user_id="{new_open_id}">{display_name}</at>'

    return _AT_TAG_PATTERN.sub(replace, content)


def _get_chat_bot_member_ids(adapter: Any, coordinator: FeishuA2ACoordinator, chat_id: str) -> dict[str, str]:
    app_id = str(getattr(adapter, "_app_id", "") or os.getenv("FEISHU_APP_ID", "")).strip()
    app_secret = str(getattr(adapter, "_app_secret", "") or os.getenv("FEISHU_APP_SECRET", "")).strip()
    if not app_id or not app_secret:
        return {}

    cache_key = (app_id, chat_id)
    cached = _CHAT_MEMBER_CACHE.get(cache_key)
    if cached and time.time() - cached[0] <= _CHAT_MEMBER_CACHE_TTL:
        return cached[1]

    domain = str(getattr(getattr(adapter, "_settings", None), "domain_name", "") or os.getenv("FEISHU_DOMAIN", "feishu")).lower()
    base = "https://open.larksuite.com" if domain == "lark" else "https://open.feishu.cn"
    token = _tenant_token(base, app_id, app_secret)

    by_name: dict[str, str] = {}
    total_items = 0
    member_types: set[str] = set()
    sample_keys: set[str] = set()
    page_token = ""
    for _ in range(20):
        params = {"member_id_type": "open_id", "page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        data = _request_json(
            f"{base}/open-apis/im/v1/chats/{urllib.parse.quote(chat_id)}/members?{urllib.parse.urlencode(params)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = data.get("data") or {}
        for item in payload.get("items") or []:
            total_items += 1
            if isinstance(item, dict):
                sample_keys.update(str(key) for key in item.keys())
            member_types.add(str(item.get("member_type") or "").lower())
            if str(item.get("member_type") or "").lower() != "bot":
                continue
            member_id = str(item.get("member_id") or item.get("open_id") or "").strip()
            if not member_id:
                continue
            for name in _member_names(item):
                by_name[coordinator.registry.normalize_name(name)] = member_id
        if not payload.get("has_more"):
            break
        page_token = str(payload.get("page_token") or "")

    # Add configured aliases that point at the resolved canonical bot name.
    for agent in coordinator.registry.agents:
        resolved = by_name.get(coordinator.registry.normalize_name(agent.name))
        if not resolved:
            continue
        for alias in agent.aliases:
            by_name[coordinator.registry.normalize_name(alias)] = resolved

    _debug_print(
        "chat member lookup "
        f"chat={chat_id} total_items={total_items} member_types={sorted(member_types)} "
        f"sample_keys={sorted(sample_keys)[:20]} resolved_bot_names={len(by_name)} names={sorted(by_name)[:12]}"
    )
    _CHAT_MEMBER_CACHE[cache_key] = (time.time(), by_name)
    return by_name


def _member_names(item: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for key in (
        "name",
        "member_name",
        "display_name",
        "tenant_name",
        "en_name",
        "app_name",
        "bot_name",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            names.add(value.strip())
    return names


def _tenant_token(base: str, app_id: str, app_secret: str) -> str:
    data = _request_json(
        f"{base}/open-apis/auth/v3/tenant_access_token/internal",
        method="POST",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    return str(data["tenant_access_token"])


def _request_json(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"Feishu API error {payload.get('code')}: {payload.get('msg')}")
    return payload


def _inject_sender_info_into_text_message(
    coordinator: FeishuA2ACoordinator,
    message: Any,
    sender_open_id: str,
    chat_id: str,
) -> None:
    if not message:
        return
    msg_type = str(getattr(message, "message_type", "") or "").lower()
    raw_content = getattr(message, "content", "") or ""
    try:
        payload = json.loads(raw_content or "{}")
    except json.JSONDecodeError:
        return

    if msg_type == "text" and isinstance(payload, dict):
        text = str(payload.get("text") or "")
        result = coordinator.process_inbound_text(text=text, sender_open_id=sender_open_id, chat_id=chat_id)
        if result and result.get("action") == "rewrite":
            payload["text"] = result["text"]
            setattr(message, "content", json.dumps(payload, ensure_ascii=False))
        return

    if msg_type == "post" and isinstance(payload, dict):
        content = payload.get("content")
        if not isinstance(content, dict):
            return
        title = content.get("title") or ""
        result = coordinator.process_inbound_text(text=str(title), sender_open_id=sender_open_id, chat_id=chat_id)
        if result and result.get("action") == "rewrite":
            content["title"] = result["text"]
            setattr(message, "content", json.dumps(payload, ensure_ascii=False))


def inject_sender_info_into_message(
    coordinator: FeishuA2ACoordinator,
    message: Any,
    sender_open_id: str,
    chat_id: str,
) -> None:
    _inject_sender_info_into_text_message(coordinator, message, sender_open_id, chat_id)
