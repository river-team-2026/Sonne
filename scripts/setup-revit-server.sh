#!/usr/bin/env bash
set -euo pipefail
# Run as the same non-root user who installed Sonne.
[[ "$EUID" -ne 0 ]] || { echo 'Run as the normal Sonne user, not root.' >&2; exit 1; }
memory_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
[[ "$memory_kib" -ge 11534336 ]] || { echo 'The Revit profile needs approximately 12 GiB of host RAM (8 GiB for Ollama).' >&2; exit 1; }
source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
runtime="${1:?Usage: setup-revit-server.sh RUNTIME HTTPS_URL [CLIENT_ID] [LABEL] [--enable-writes] [--enable-publications]}"
url="${2:?The Windows-facing HTTPS endpoint, ending in /revit/, is required}"
client_id="${3:-windows-01}"
label="${4:-Revit Windows 01}"
flags=()
for flag in "${@:5}"; do
    [[ "$flag" == --enable-writes || "$flag" == --enable-publications ]] || { echo 'Unknown option.' >&2; exit 1; }
    flags+=("$flag")
done
cd -- "$runtime"
[[ -f .env && -f compose.yaml && -f compose.prompt-studio.yaml ]] || { echo 'Not an installed Sonne runtime.' >&2; exit 1; }
umask 077
mkdir -p integrations/revit .state/openclaw/integrations/revit .state/revit-publications/{inbox,archives}
for file in common.mjs hub.mjs mcp-server.mjs check.mjs Dockerfile package.json package-lock.json; do
    if [[ "$source_root" != "$PWD" ]]; then cp -- "$source_root/integrations/revit/$file" "integrations/revit/$file"; fi
    cp -- "$source_root/integrations/revit/$file" ".state/openclaw/integrations/revit/$file"
done
if [[ "$source_root" != "$PWD" ]]; then
    cp -- "$source_root/integrations/revit/"{publication-server,publications,publications-aps,publish-room-version}.mjs integrations/revit/
fi
if [[ "$source_root" != "$PWD" ]]; then cp -- "$source_root/compose.revit.yaml" compose.revit.yaml; fi
provision=(python3 "$source_root/scripts/provision-revit.py" "$PWD" "$url" --client-id "$client_id" --label "$label")
"${provision[@]}" "${flags[@]}"
compose=(docker compose --env-file .env -f compose.yaml -f compose.override.yaml -f compose.prompt-studio.yaml -f compose.revit.yaml)
if [[ ${SONNE_DOCKER_USE_SUDO:-} == 1 ]]; then compose=(sudo "${compose[@]}"); fi
"${compose[@]}" up -d --wait --wait-timeout 90 ollama openclaw
"${compose[@]}" exec -T openclaw npm ci --omit=dev --ignore-scripts --prefix /home/node/.openclaw/integrations/revit
"${compose[@]}" up -d --build --force-recreate --wait --wait-timeout 30 revit-hub
"${compose[@]}" exec -T openclaw node dist/index.js config set --batch-file /home/node/.openclaw/revit-mcp-config.json
if ! "${compose[@]}" exec -T openclaw node dist/index.js config get tools.allow --json > .state/revit-hub/current-tools.json; then
    printf 'null\n' > .state/revit-hub/current-tools.json
fi
python3 - <<'PY'
import json
from pathlib import Path
tools = json.loads(Path('.state/revit-hub/current-tools.json').read_text())
# An absent/empty allowlist already permits native tools. Never narrow it here.
if tools:
    for tool in ['revit__revit_sessions', 'revit__revit_tools', 'revit__revit_read', 'revit__revit_edit', 'revit__revit_call', 'revit__revit_audit', 'revit__revit_bulk_set', 'revit__revit_room_publications']:
        if tool not in tools:
            tools.append(tool)
    Path('.state/openclaw/revit-tools-config.json').write_text(json.dumps([{'path': 'tools.allow', 'value': tools}]))
else:
    Path('.state/openclaw/revit-tools-config.json').unlink(missing_ok=True)
PY
if [[ -f .state/openclaw/revit-tools-config.json ]]; then
    "${compose[@]}" exec -T openclaw node dist/index.js config set --batch-file /home/node/.openclaw/revit-tools-config.json
fi
"${compose[@]}" exec -T openclaw node dist/index.js mcp probe revit --json
"${compose[@]}" exec -T openclaw node dist/index.js mcp reload
echo "Router installed for $url"
