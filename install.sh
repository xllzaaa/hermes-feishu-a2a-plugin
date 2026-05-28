#!/usr/bin/env bash
set -euo pipefail

PLUGIN_NAME="${PLUGIN_NAME:-hermes_feishu_a2a}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME_BASE="${HERMES_HOME_BASE:-$HOME/.hermes/profiles}"
INSTALL_SKILLS="${INSTALL_SKILLS:-true}"

usage() {
  cat <<'TEXT'
Install Hermes Feishu A2A into one or more Hermes homes.

Usage:
  ./install.sh
  HERMES_HOME="$HOME/.hermes/profiles/product" ./install.sh
  PROFILES="product developer tester" ./install.sh
  HERMES_HOME_BASE="$HOME/.hermes/profiles" PROFILES="product developer" ./install.sh

Notes:
  - This installs the plugin, gateway hook, and bundled A2A skills.
  - It does not disable, rename, or remove other hooks.
  - Set INSTALL_SKILLS=false if you do not want skill symlinks.
  - Set HERMES_FEISHU_A2A_REGISTRY_FILE and self-agent env vars before running Hermes.
TEXT
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

install_one() {
  local hermes_home="$1"
  local target_dir="$hermes_home/plugins/$PLUGIN_NAME"
  local hook_dir="$hermes_home/hooks/hermes_feishu_a2a_gateway_patch"
  local skills_dir="$hermes_home/skills"

  mkdir -p "$hermes_home/plugins" "$hermes_home/hooks" "$hook_dir"

  if [[ "$SOURCE_DIR" != "$target_dir" ]]; then
    if [[ -L "$target_dir" || -f "$target_dir" ]]; then
      rm -f "$target_dir"
    elif [[ -d "$target_dir" ]]; then
      echo "Refusing to replace existing directory: $target_dir" >&2
      echo "Remove it first or run from that directory." >&2
      exit 1
    fi
    ln -s "$SOURCE_DIR" "$target_dir"
  fi

  cat > "$hook_dir/HOOK.yaml" <<'YAML'
name: hermes-feishu-a2a-gateway-patch
description: Install Hermes Feishu A2A adapter patch in the gateway process.
events:
  - gateway:startup
YAML

  cat > "$hook_dir/handler.py" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path


def _plugin_dir() -> Path:
    hermes_home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    return hermes_home / "plugins" / "hermes_feishu_a2a"


def handle(event_type, context):
    plugin_dir = _plugin_dir()
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

    from hermes_feishu_a2a.adapter_patch import install_feishu_adapter_patch
    from hermes_feishu_a2a.core import FeishuA2ACoordinator

    coordinator = FeishuA2ACoordinator.from_environment()
    ok = install_feishu_adapter_patch(coordinator)
    print(
        f"[hermes-feishu-a2a-gateway-patch] installed={ok} agents={len(coordinator.registry.agents)} self={getattr(coordinator.registry.self_agent(), 'name', None)}",
        flush=True,
    )
PY

  echo "Installed $PLUGIN_NAME -> $target_dir"
  echo "Installed gateway hook -> $hook_dir"

  if [[ "$INSTALL_SKILLS" == "true" || "$INSTALL_SKILLS" == "1" || "$INSTALL_SKILLS" == "yes" ]]; then
    mkdir -p "$skills_dir"
    for skill_path in "$SOURCE_DIR"/skills/*; do
      [[ -d "$skill_path" ]] || continue
      local skill_name
      skill_name="$(basename "$skill_path")"
      local skill_target="$skills_dir/$skill_name"
      if [[ -L "$skill_target" || -f "$skill_target" ]]; then
        rm -f "$skill_target"
      elif [[ -d "$skill_target" ]]; then
        echo "Keeping existing skill directory: $skill_target" >&2
        continue
      fi
      ln -s "$skill_path" "$skill_target"
      echo "Installed skill -> $skill_target"
    done
  fi
}

if [[ -n "${PROFILES:-}" ]]; then
  for profile in $PROFILES; do
    install_one "$HERMES_HOME_BASE/$profile"
  done
else
  install_one "${HERMES_HOME:-$HOME/.hermes}"
fi

cat <<'TEXT'

Next:
  1. Configure env vars or your Hermes profile config.
  2. Run: ./hermes-feishu-a2a doctor --profile <profile>
  3. Restart the matching Hermes gateway.
TEXT
