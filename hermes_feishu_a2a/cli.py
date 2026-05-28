"""Small operational CLI for the Hermes Feishu A2A plugin."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core import AgentRegistry, FeishuDiscovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-feishu-a2a")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="print loaded registry and current self agent")
    sub.add_parser("discover", help="call Feishu API and print discovered bots")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        load_error = ""
        try:
            registry = AgentRegistry.from_environment()
        except Exception as exc:
            registry = AgentRegistry([])
            load_error = f"Failed to load registry: {exc}"
        result = _doctor_payload(registry)
        if load_error:
            result["status"] = "error"
            result["errors"].insert(0, load_error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ok" else 1

    if args.command == "discover":
        agents = FeishuDiscovery.from_environment().discover()
        payload = {
            "agents": [_agent_to_dict(agent) for agent in agents],
            "cache_hint": os.getenv("HERMES_FEISHU_A2A_REGISTRY_CACHE", "$HERMES_HOME/feishu-a2a/registry.json"),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    return 2


def _doctor_payload(registry: AgentRegistry) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    registry_file = os.getenv("HERMES_FEISHU_A2A_REGISTRY_FILE", "").strip()
    legacy_registry_file = os.getenv("HERMES_FEISHU_BOT_REGISTRY_FILE", "").strip()
    accounts_file = os.getenv("HERMES_FEISHU_ACCOUNTS_FILE", "").strip()
    registry_json_present = bool(os.getenv("HERMES_FEISHU_A2A_REGISTRY_JSON", "").strip())
    legacy_registry_json_present = bool(os.getenv("HERMES_FEISHU_BOT_REGISTRY_JSON", "").strip())
    accounts_json_present = bool(os.getenv("HERMES_FEISHU_ACCOUNTS_JSON", "").strip())

    if registry_file and not Path(registry_file).expanduser().exists():
        errors.append(f"HERMES_FEISHU_A2A_REGISTRY_FILE does not exist: {registry_file}")
    if legacy_registry_file and not Path(legacy_registry_file).expanduser().exists():
        errors.append(f"HERMES_FEISHU_BOT_REGISTRY_FILE does not exist: {legacy_registry_file}")
    if accounts_file and not Path(accounts_file).expanduser().exists():
        errors.append(f"HERMES_FEISHU_ACCOUNTS_FILE does not exist: {accounts_file}")

    if not registry.agents:
        errors.append("No agents loaded. Set HERMES_FEISHU_A2A_REGISTRY_FILE or HERMES_FEISHU_ACCOUNTS_FILE.")
    if registry.agents and not registry.self_agent():
        errors.append("Self agent is not resolved. Set HERMES_FEISHU_SELF_ACCOUNT_ID, HERMES_FEISHU_SELF_AGENT_NAME, or HERMES_FEISHU_SELF_OPEN_ID.")

    if not os.getenv("FEISHU_APP_ID", "").strip():
        warnings.append("FEISHU_APP_ID is not set in the current shell. Hermes may still provide it from profile config.")
    if not os.getenv("FEISHU_APP_SECRET", "").strip():
        warnings.append("FEISHU_APP_SECRET is not set in the current shell. Hermes may still provide it from profile config.")
    if os.getenv("HERMES_FEISHU_PATCH_ADAPTER", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        warnings.append("HERMES_FEISHU_PATCH_ADAPTER is disabled; bot-to-bot inbound/outbound patching will not run.")
    if os.getenv("HERMES_FEISHU_SELF_ACCOUNT_ID", "").strip() == "default":
        warnings.append("HERMES_FEISHU_SELF_ACCOUNT_ID is 'default'. Use a stable unique id when running multiple agents.")

    registry_source = "discovery"
    if registry_file or registry_json_present:
        registry_source = "a2a_registry"
    elif legacy_registry_file or legacy_registry_json_present:
        registry_source = "legacy_registry"
    elif accounts_file or accounts_json_present:
        registry_source = "accounts_discovery"

    return {
        "status": "error" if errors else "ok",
        "errors": errors,
        "warnings": warnings,
        "hermes_home": os.getenv("HERMES_HOME", str(Path.home() / ".hermes")),
        "registry_source": registry_source,
        "env": {
            "HERMES_FEISHU_A2A_REGISTRY_FILE": _redact_path(registry_file),
            "HERMES_FEISHU_BOT_REGISTRY_FILE": _redact_path(legacy_registry_file),
            "HERMES_FEISHU_ACCOUNTS_FILE": _redact_path(accounts_file),
            "HERMES_FEISHU_A2A_REGISTRY_JSON": registry_json_present,
            "HERMES_FEISHU_BOT_REGISTRY_JSON": legacy_registry_json_present,
            "HERMES_FEISHU_ACCOUNTS_JSON": accounts_json_present,
            "HERMES_FEISHU_SELF_ACCOUNT_ID": os.getenv("HERMES_FEISHU_SELF_ACCOUNT_ID", ""),
            "HERMES_FEISHU_SELF_AGENT_NAME": os.getenv("HERMES_FEISHU_SELF_AGENT_NAME", "") or os.getenv("FEISHU_BOT_NAME", ""),
            "HERMES_FEISHU_SELF_OPEN_ID": bool(os.getenv("HERMES_FEISHU_SELF_OPEN_ID", "") or os.getenv("FEISHU_BOT_OPEN_ID", "")),
            "FEISHU_APP_ID": bool(os.getenv("FEISHU_APP_ID", "")),
            "FEISHU_APP_SECRET": bool(os.getenv("FEISHU_APP_SECRET", "")),
            "HERMES_FEISHU_PATCH_ADAPTER": os.getenv("HERMES_FEISHU_PATCH_ADAPTER", "true"),
        },
        "self_agent": _agent_to_dict(registry.self_agent()),
        "agents": [_agent_to_dict(agent) for agent in registry.agents],
    }


def _redact_path(value: str) -> str:
    if not value:
        return ""
    return str(Path(value).expanduser())


def _agent_to_dict(agent):
    if not agent:
        return None
    return {
        "name": agent.name,
        "open_id": agent.open_id,
        "account_id": agent.account_id,
        "description": agent.description,
        "aliases": list(agent.aliases),
        "self": agent.self_agent,
    }


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
