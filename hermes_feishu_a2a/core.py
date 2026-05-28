"""Core coordination logic for Hermes Feishu multi-agent collaboration."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MENTION_TAG_RE = re.compile(r"<at\s+user_id=[\"'](?P<id>[^\"']+)[\"']>(?P<name>.*?)</at>", re.IGNORECASE)
TASK_ID_RE = re.compile(r"\b(?:task|任务|协作)[-_ ]?(?:id|编号)?[:：# ]+([A-Za-z0-9_.:-]{4,96})", re.IGNORECASE)
NOTIFY_ONLY_MARKER = "🔕仅通知"


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()


def registry_cache_path() -> Path:
    return Path(
        os.getenv(
            "HERMES_FEISHU_A2A_REGISTRY_CACHE",
            hermes_home() / "feishu-a2a" / "registry.json",
        )
    ).expanduser()


def load_json_env(*, json_env: str, file_env: str) -> Any:
    raw = os.getenv(json_env, "").strip()
    path = os.getenv(file_env, "").strip()
    if not raw and path:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    if not raw:
        return None
    return json.loads(raw)


def platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


@dataclass(frozen=True)
class AgentSpec:
    name: str
    open_id: str
    account_id: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    self_agent: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, fallback_id: str = "") -> "AgentSpec":
        aliases = value.get("aliases") or value.get("alias") or ()
        if isinstance(aliases, str):
            aliases = [aliases]
        return cls(
            name=str(value.get("name") or value.get("botName") or value.get("bot_name") or fallback_id).strip(),
            open_id=str(value.get("open_id") or value.get("botOpenId") or value.get("user_id") or "").strip(),
            account_id=str(value.get("account_id") or value.get("accountId") or fallback_id).strip(),
            description=str(value.get("description") or value.get("role") or "").strip(),
            aliases=tuple(str(item).strip() for item in aliases if str(item).strip()),
            self_agent=bool(value.get("self") or value.get("self_agent")),
        )

    @property
    def display_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


class AgentRegistry:
    def __init__(self, agents: Iterable[AgentSpec]):
        self.agents = tuple(agent for agent in agents if agent.name and agent.open_id)
        self._by_name: dict[str, AgentSpec] = {}
        self._by_open_id: dict[str, AgentSpec] = {}
        self._by_account_id: dict[str, AgentSpec] = {}
        for agent in self.agents:
            self._by_open_id[agent.open_id] = agent
            if agent.account_id:
                self._by_account_id[agent.account_id] = agent
            for name in agent.display_names:
                self._by_name[self.normalize_name(name)] = agent

    @classmethod
    def from_environment(cls) -> "AgentRegistry":
        manual = (
            load_json_env(json_env="HERMES_FEISHU_A2A_REGISTRY_JSON", file_env="HERMES_FEISHU_A2A_REGISTRY_FILE")
            or load_json_env(json_env="HERMES_FEISHU_BOT_REGISTRY_JSON", file_env="HERMES_FEISHU_BOT_REGISTRY_FILE")
        )
        if manual:
            return cls(agents_from_json(manual))
        return cls(FeishuDiscovery.from_environment().discover())

    @staticmethod
    def normalize_name(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip().lower()

    def by_open_id(self, open_id: str) -> AgentSpec | None:
        return self._by_open_id.get((open_id or "").strip())

    def by_account_id(self, account_id: str) -> AgentSpec | None:
        return self._by_account_id.get((account_id or "").strip())

    def by_name(self, name: str) -> AgentSpec | None:
        return self._by_name.get(self.normalize_name(name))

    def self_agent(self) -> AgentSpec | None:
        env_open_id = os.getenv("HERMES_FEISHU_SELF_OPEN_ID", "").strip() or os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
        env_account = os.getenv("HERMES_FEISHU_SELF_ACCOUNT_ID", "").strip()
        env_name = os.getenv("HERMES_FEISHU_SELF_AGENT_NAME", "").strip() or os.getenv("FEISHU_BOT_NAME", "").strip()
        if env_open_id and (found := self.by_open_id(env_open_id)):
            return found
        if env_account and (found := self.by_account_id(env_account)):
            return found
        if env_name and (found := self.by_name(env_name)):
            return found
        return next((agent for agent in self.agents if agent.self_agent), None)

    def other_agents(self) -> tuple[AgentSpec, ...]:
        self_agent = self.self_agent()
        if not self_agent:
            return self.agents
        return tuple(agent for agent in self.agents if agent.open_id != self_agent.open_id)

    def render_prompt(self, *, available_open_ids: set[str] | None = None) -> str:
        rows: list[str] = []
        for agent in self.other_agents():
            if available_open_ids is not None and agent.open_id not in available_open_ids:
                continue
            alias_text = f" aliases: {', '.join(agent.aliases)}" if agent.aliases else ""
            desc = f" - {agent.description}" if agent.description else ""
            rows.append(f'- <at user_id="{agent.open_id}">{agent.name}</at>{alias_text}{desc}')
        return "\n".join(rows)

    def replace_text_mentions(self, text: str, *, skip_open_id: str = "") -> str:
        if not text or not self.agents:
            return text
        names = sorted(self._by_name, key=len, reverse=True)
        alternatives = "|".join(re.escape(name) for name in names if name)
        if not alternatives:
            return text
        delimiter = r"[\s,，。.!！?？:：;；)）\]\}】]"
        pattern = re.compile(rf"(?<![\w<])@(?P<name>{alternatives})(?=$|{delimiter})", re.IGNORECASE)

        def replace(match: re.Match[str]) -> str:
            agent = self.by_name(match.group("name"))
            if not agent or (skip_open_id and agent.open_id == skip_open_id):
                return match.group(0)
            return f'<at user_id="{agent.open_id}">{agent.name}</at>'

        return add_at_fallback_labels(pattern.sub(replace, text))


def agents_from_json(data: Any) -> list[AgentSpec]:
    if isinstance(data, dict):
        if "agents" in data:
            data = data["agents"]
        else:
            return [AgentSpec.from_mapping(value, fallback_id=key) for key, value in data.items() if isinstance(value, dict)]
    if not isinstance(data, list):
        raise ValueError("Agent registry must be a list, {'agents': [...]}, or {agent_id: {...}}")
    return [AgentSpec.from_mapping(item) for item in data if isinstance(item, dict)]


class FeishuDiscovery:
    def __init__(self, accounts: dict[str, dict[str, str]], *, domain: str = "feishu", ttl_seconds: int = 86400):
        self.accounts = accounts
        self.domain = (domain or "feishu").lower()
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_environment(cls) -> "FeishuDiscovery":
        accounts_data = load_json_env(json_env="HERMES_FEISHU_ACCOUNTS_JSON", file_env="HERMES_FEISHU_ACCOUNTS_FILE")
        accounts: dict[str, dict[str, str]] = {}
        if isinstance(accounts_data, dict):
            for account_id, account in accounts_data.items():
                if isinstance(account, dict):
                    accounts[str(account_id)] = {
                        "app_id": str(account.get("app_id") or account.get("appId") or ""),
                        "app_secret": str(account.get("app_secret") or account.get("appSecret") or ""),
                        "name": str(account.get("name") or account.get("agent") or account_id),
                        "description": str(account.get("description") or ""),
                    }
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if app_id and app_secret:
            accounts.setdefault(
                os.getenv("HERMES_FEISHU_SELF_ACCOUNT_ID", "default").strip() or "default",
                {
                    "app_id": app_id,
                    "app_secret": app_secret,
                    "name": os.getenv("FEISHU_BOT_NAME", "Hermes Agent").strip() or "Hermes Agent",
                    "description": os.getenv("HERMES_FEISHU_SELF_DESCRIPTION", "").strip(),
                },
            )
        return cls(
            accounts,
            domain=os.getenv("FEISHU_DOMAIN", "feishu"),
            ttl_seconds=env_int("HERMES_FEISHU_REGISTRY_TTL_SECONDS", 86400),
        )

    @property
    def base_url(self) -> str:
        return "https://open.larksuite.com" if self.domain == "lark" else "https://open.feishu.cn"

    def discover(self) -> list[AgentSpec]:
        if not self.accounts:
            return []
        cached = self._read_cache()
        if cached:
            cached_agents = agents_from_json(cached.get("agents", []))
            if set(self.accounts).issubset({agent.account_id for agent in cached_agents if agent.account_id}):
                return cached_agents
        agents: list[AgentSpec] = []
        for account_id, account in self.accounts.items():
            app_id = account.get("app_id", "").strip()
            app_secret = account.get("app_secret", "").strip()
            if not app_id or not app_secret:
                continue
            try:
                token = self.tenant_token(app_id, app_secret)
                bot = self.bot_info(token)
                agents.append(
                    AgentSpec(
                        name=bot.get("name") or account.get("name") or account_id,
                        open_id=bot.get("open_id") or "",
                        account_id=account_id,
                        description=account.get("description", ""),
                        self_agent=account_id == (os.getenv("HERMES_FEISHU_SELF_ACCOUNT_ID", "default") or "default"),
                    )
                )
            except Exception as exc:
                logger.warning("Failed to discover Feishu bot for %s: %s", account_id, exc)
        if agents:
            self._write_cache(agents)
        return agents

    def tenant_token(self, app_id: str, app_secret: str) -> str:
        data = self.request_json(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            method="POST",
            data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return str(data["tenant_access_token"])

    def bot_info(self, token: str) -> dict[str, str]:
        data = self.request_json(f"{self.base_url}/open-apis/bot/v3/info", headers={"Authorization": f"Bearer {token}"})
        bot = data.get("bot") or {}
        return {"open_id": str(bot.get("open_id") or ""), "name": str(bot.get("app_name") or bot.get("bot_name") or "")}

    def chat_bot_open_ids(self, chat_id: str, token: str) -> set[str]:
        open_ids: set[str] = set()
        page_token = ""
        for _ in range(20):
            params = urllib.parse.urlencode(
                {"member_id_type": "open_id", "page_size": "100", **({"page_token": page_token} if page_token else {})}
            )
            data = self.request_json(
                f"{self.base_url}/open-apis/im/v1/chats/{chat_id}/members?{params}",
                headers={"Authorization": f"Bearer {token}"},
            )
            for item in data.get("data", {}).get("items", []):
                if item.get("member_type") == "bot" and item.get("member_id"):
                    open_ids.add(str(item["member_id"]))
            if not data.get("data", {}).get("has_more"):
                break
            page_token = str(data.get("data", {}).get("page_token") or "")
        return open_ids

    def request_json(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Feishu API request failed: {exc}") from exc
        if payload.get("code") != 0:
            raise RuntimeError(f"Feishu API error {payload.get('code')}: {payload.get('msg')}")
        return payload

    def _read_cache(self) -> dict[str, Any] | None:
        try:
            cached = json.loads(registry_cache_path().read_text(encoding="utf-8"))
            discovered_at = datetime.fromisoformat(str(cached.get("discovered_at", "")).replace("Z", "+00:00"))
            if time.time() - discovered_at.timestamp() <= self.ttl_seconds:
                return cached
        except Exception:
            return None
        return None

    def _write_cache(self, agents: list[AgentSpec]) -> None:
        payload = {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "agents": [
                {
                    "name": agent.name,
                    "open_id": agent.open_id,
                    "account_id": agent.account_id,
                    "description": agent.description,
                    "aliases": list(agent.aliases),
                    "self": agent.self_agent,
                }
                for agent in agents
            ],
        }
        path = registry_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to write registry cache %s: %s", path, exc)


class LoopGuard:
    def __init__(self, *, window_seconds: int = 900, max_events: int = 8):
        self.window_seconds = window_seconds
        self.max_events = max_events
        self._events: dict[tuple[str, str], list[float]] = {}

    def allow(self, chat_id: str, task_id: str | None) -> bool:
        if not task_id:
            return True
        now = time.time()
        key = (chat_id or "", task_id)
        events = [ts for ts in self._events.get(key, []) if now - ts <= self.window_seconds]
        if len(events) >= self.max_events:
            self._events[key] = events
            return False
        events.append(now)
        self._events[key] = events
        return True


class FeishuA2ACoordinator:
    def __init__(
        self,
        registry: AgentRegistry,
        *,
        enabled: bool = True,
        require_mention: bool = True,
        transform_mentions: bool = True,
        max_task_events: int = 8,
    ):
        self.registry = registry
        self.enabled = enabled
        self.require_mention = require_mention
        self.transform_mentions = transform_mentions
        self.loop_guard = LoopGuard(max_events=max_task_events)
        self.native_a2a_chats: set[str] = set()

    @classmethod
    def from_environment(cls) -> "FeishuA2ACoordinator":
        return cls(
            AgentRegistry.from_environment(),
            enabled=env_bool("HERMES_FEISHU_A2A_ENABLED", True),
            require_mention=env_bool("HERMES_FEISHU_REQUIRE_AGENT_MENTION", True),
            transform_mentions=env_bool("HERMES_FEISHU_TRANSFORM_MENTIONS", True),
            max_task_events=env_int("HERMES_FEISHU_MAX_TASK_EVENTS", 8),
        )

    def pre_llm_call(self, *, platform: str, chat_id: str = "", session_id: str = "", **_: Any) -> dict[str, str] | None:
        if not self.enabled or platform_name(platform) != "feishu":
            return None
        roster = self.registry.render_prompt()
        if not roster:
            return None
        permission_note = ""
        if chat_id and chat_id not in self.native_a2a_chats:
            permission_note = (
                "\n- If bot-to-bot @ does not trigger the target bot, ask the Feishu app admin to enable "
                "im:message.group_at_msg.include_bot:readonly for every participating app."
            )
        return {
            "context": (
                "[A2A - Feishu group collaboration rules]\n"
                "Available Hermes agents in this Feishu group:\n"
                f"{roster}\n\n"
                "Default behaviour:\n"
                "- Do not proactively mention other agents unless the user asks for collaboration or the task clearly needs it.\n"
                "- Mention at most one agent per message.\n"
                "- Use <at user_id=\"ou_xxx\">AgentName</at> only when requesting work, not when merely naming an agent.\n"
                f"- For one-way notifications, include the marker: {NOTIFY_ONLY_MARKER}.\n"
                "- When another agent assigns you work, finish the work and @ the initiating agent back with the result.\n"
                "- When another agent returns a result to you, synthesize it for the user instead of bouncing it back.\n"
                "- Include a short task_id when handing off work so loops can be detected.\n"
                "- If you write @AgentName in plain text, this plugin will convert known names to Feishu at-tags before sending."
                f"{permission_note}"
            )
        }

    def post_llm_call(self, *, assistant_response: str, platform: str, **_: Any) -> dict[str, str] | None:
        if not self.enabled or not self.transform_mentions or platform_name(platform) != "feishu":
            return None
        transformed = self.transform_outbound(assistant_response)
        if transformed != assistant_response:
            return {"content": transformed, "response_text": transformed}
        return None

    def transform_outbound(self, content: str, *, self_open_id: str = "") -> str:
        if not self.enabled or not self.transform_mentions:
            return content
        return self.registry.replace_text_mentions(content or "", skip_open_id=self_open_id)

    def should_accept_bot_sender(
        self,
        *,
        sender_open_id: str,
        mentioned_ids: Iterable[str],
        chat_id: str = "",
        text: str = "",
    ) -> bool:
        if not self.enabled or not self.registry.by_open_id(sender_open_id):
            return False
        if not self.require_mention:
            return True
        self_agent = self.registry.self_agent()
        ids = set(mentioned_ids)
        mentioned = bool(self_agent and self_agent.open_id in ids)
        mentioned = mentioned or bool(self_agent and f'<at user_id="{self_agent.open_id}">' in (text or ""))
        if mentioned and chat_id:
            self.native_a2a_chats.add(chat_id)
        return mentioned

    def process_inbound_text(self, *, text: str, sender_open_id: str = "", chat_id: str = "") -> dict[str, str] | None:
        if not self.enabled:
            return None
        task_id = extract_task_id(text)
        if not self.loop_guard.allow(chat_id, task_id):
            return {"action": "skip", "reason": "feishu_a2a_loop_guard"}
        sender = self.registry.by_open_id(sender_open_id)
        if not sender:
            return None
        sender_at = f'<at user_id="{sender.open_id}">{sender.name}</at>'
        reply_rule = "这是一条仅通知消息，不需要 @ 回对方。" if NOTIFY_ONLY_MARKER in text else f"如需 @ 回对方请使用：{sender_at}"
        prefix = f"[来自 Hermes 机器人「{sender.name}」- {reply_rule}]\n\n"
        if text.startswith(prefix):
            return None
        return {"action": "rewrite", "text": prefix + text}


def extract_task_id(text: str) -> str | None:
    match = TASK_ID_RE.search(text or "")
    return match.group(1) if match else None


def add_at_fallback_labels(content: str) -> str:
    return re.sub(
        r'<at user_id="([^"]+)">([^<]+)</at>(?!\s*\([^)]+\))',
        lambda match: f'<at user_id="{match.group(1)}">{match.group(2)}</at> ({match.group(2)})',
        content or "",
    )


def extract_mentions_from_content(raw_content: str) -> set[str]:
    ids = {match.group("id") for match in MENTION_TAG_RE.finditer(raw_content or "")}
    try:
        payload = json.loads(raw_content or "{}")
    except json.JSONDecodeError:
        return ids
    if isinstance(payload, dict):
        for key in ("text", "title"):
            if isinstance(payload.get(key), str):
                ids.update(match.group("id") for match in MENTION_TAG_RE.finditer(payload[key]))
    ids.update(extract_mention_ids(payload))
    return ids


def extract_mention_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        if value.get("tag") in {"at", "mention"}:
            for key in ("user_id", "open_id", "id"):
                if value.get(key):
                    ids.add(str(value[key]))
        for item in value.values():
            ids.update(extract_mention_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(extract_mention_ids(item))
    return ids
