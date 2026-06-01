#!/usr/bin/env bash
# Start external mempalace-rpg-mcp for this game package.
# Keeps RPG narrative memory external; this package only supplies config.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)"
cd "$PROJECT_ROOT"

candidates=()
if [ -n "${MEMPALACE_RPG_MCP:-}" ]; then
  candidates+=("$MEMPALACE_RPG_MCP")
fi
candidates+=(
  "mempalace-rpg-mcp"
  "$PROJECT_ROOT/../mempalace-rpg/.venv/bin/mempalace-rpg-mcp"
  "$PROJECT_ROOT/../mempalace/.venv/bin/mempalace-rpg-mcp"
  "/home/tgy23/PI_workingspace/mempalace/.venv/bin/mempalace-rpg-mcp"
)

cmd=""
for candidate in "${candidates[@]}"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    cmd="$(command -v "$candidate")"
    break
  fi
  if [ -x "$candidate" ]; then
    cmd="$candidate"
    break
  fi
done

if [ -z "$cmd" ]; then
  cat >&2 <<'MSG'
未找到 mempalace-rpg-mcp。
请先安装/激活外部 RPG 记忆内核，或设置：
  export MEMPALACE_RPG_MCP=/absolute/path/mempalace-rpg-mcp
MSG
  exit 127
fi

args=(
  --db "${MEMPALACE_RPG_DB:-state/rpg-memory.sqlite3}"
  --memo-setting "${MEMPALACE_RPG_MEMO_SETTING:-memo_setting.json}"
)
# Palace/raw transcript archive is enabled by default for this long-campaign package.
# Set MEMPALACE_RPG_PALACE=0/off/none to run SQLite-only.
palace="${MEMPALACE_RPG_PALACE:-state/rpg-palace}"
case "${palace,,}" in
  ""|0|false|off|none|disabled) ;;
  *) args+=(--palace "$palace") ;;
esac

exec "$cmd" "${args[@]}"
