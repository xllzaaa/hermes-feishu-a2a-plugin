"""Small operational CLI for the Hermes Feishu A2A plugin."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Iterator

from .core import AgentRegistry, FeishuDiscovery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes-feishu-a2a")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="print loaded registry and current self agent")
    doctor.add_argument("--profile", help="Hermes profile name under ~/.hermes/profiles")
    doctor.add_argument("--profiles", nargs="+", help="check multiple Hermes profile names")
    doctor.add_argument(
        "--profiles-base",
        default=str(Path.home() / ".hermes" / "profiles"),
        help="base directory for --profile/--profiles",
    )
    doctor.add_argument(
        "--no-feishu-check",
        action="store_true",
        help="skip Feishu bot/v3/info verification even when credentials are available",
    )
    sub.add_parser("discover", help="call Feishu API and print discovered bots")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        if args.profile or args.profiles:
            names = list(args.profiles or ([args.profile] if args.profile else []))
            result = _profiles_doctor_payload(
                names,
                profiles_base=Path(args.profiles_base).expanduser(),
                feishu_check=not args.no_feishu_check,
            )
        else:
            result = _single_doctor_payload(feishu_check=not args.no_feishu_check)
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


def _single_doctor_payload(*, feishu_check: bool = True) -> dict:
    load_error = ""
    try:
        registry = AgentRegistry.from_environment()
    except Exception as exc:
        registry = AgentRegistry([])
        load_error = f"Failed to load registry: {exc}"
    result = _doctor_payload(registry, profile="", profile_home=None, feishu_check=feishu_check)
    if load_error:
        result["status"] = "error"
        result["errors"].insert(0, load_error)
    return result


def _profiles_doctor_payload(
    profiles: list[str],
    *,
    profiles_base: Path,
    feishu_check: bool = True,
) -> dict:
    results: list[dict] = []
    for profile in profiles:
        profile_home = profiles_base / profile
        profile_env = _profile_env(profile_home)
        profile_env.setdefault("HERMES_HOME", str(profile_home))
        with _temporary_env(profile_env):
            load_error = ""
            try:
                registry = AgentRegistry.from_environment()
            except Exception as exc:
                registry = AgentRegistry([])
                load_error = f"Failed to load registry: {exc}"
            payload = _doctor_payload(registry, profile=profile, profile_home=profile_home, feishu_check=feishu_check)
            if load_error:
                payload["status"] = "error"
                payload["errors"].insert(0, load_error)
            results.append(payload)
    status = "ok" if all(item["status"] == "ok" for item in results) else "error"
    return {
        "status": status,
        "profiles_base": str(profiles_base),
        "profiles": results,
    }


def _doctor_payload(
    registry: AgentRegistry,
    *,
    profile: str = "",
    profile_home: Path | None = None,
    feishu_check: bool = True,
) -> dict:
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

    self_agent = registry.self_agent()
    feishu_identity = _feishu_identity_check(self_agent, feishu_check=feishu_check)
    errors.extend(feishu_identity.pop("errors"))
    warnings.extend(feishu_identity.pop("warnings"))

    registry_source = "discovery"
    if registry_file or registry_json_present:
        registry_source = "a2a_registry"
    elif legacy_registry_file or legacy_registry_json_present:
        registry_source = "legacy_registry"
    elif accounts_file or accounts_json_present:
        registry_source = "accounts_discovery"

    return {
        "status": "error" if errors else "ok",
        "profile": profile,
        "errors": errors,
        "warnings": warnings,
        "hermes_home": str(profile_home or os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))),
        "registry_source": registry_source,
        "feishu_identity": feishu_identity,
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
        "self_agent": _agent_to_dict(self_agent),
        "agents": [_agent_to_dict(agent) for agent in registry.agents],
    }


def _feishu_identity_check(agent, *, feishu_check: bool) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    result = {
        "checked": False,
        "app_id_present": bool(app_id),
        "app_secret_present": bool(app_secret),
        "bot_name": "",
        "bot_open_id": "",
        "registry_open_id": getattr(agent, "open_id", "") if agent else "",
        "match": None,
        "errors": errors,
        "warnings": warnings,
    }
    if not feishu_check:
        warnings.append("Feishu API verification skipped by --no-feishu-check.")
        return result
    if not (app_id and app_secret):
        warnings.append("Cannot verify Feishu bot identity because FEISHU_APP_ID or FEISHU_APP_SECRET is missing.")
        return result
    try:
        discovery = FeishuDiscovery.from_environment()
        token = discovery.tenant_token(app_id, app_secret)
        bot = discovery.bot_info(token)
    except Exception as exc:
        errors.append(f"Failed to verify Feishu bot identity: {exc}")
        return result
    result.update(
        {
            "checked": True,
            "bot_name": bot.get("name", ""),
            "bot_open_id": bot.get("open_id", ""),
            "match": bool(agent and agent.open_id == bot.get("open_id", "")),
        }
    )
    if agent and agent.open_id != bot.get("open_id", ""):
        errors.append(
            "Registry open_id does not match Feishu bot/v3/info. "
            f"registry={agent.open_id}, feishu={bot.get('open_id', '')}"
        )
    if not agent:
        warnings.append("Self agent not resolved, so Feishu identity could not be compared to registry.")
    return result


def _profile_env(profile_home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = profile_home / ".env"
    if env_path.exists():
        env.update(_read_env_file(env_path))
    return env


def _read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


@contextlib.contextmanager
def _temporary_env(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
