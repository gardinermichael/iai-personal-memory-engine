#!/usr/bin/env bash
# Install or verify iai-mcp host integration for Claude Code, Claude Desktop,
# and Codex CLI. Safe to re-run; preserves unrelated host configuration.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WRAPPER_JS="${REPO_ROOT}/mcp-wrapper/dist/index.js"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
IAI_STORE="${HOME}/.iai-mcp"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '   \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[0;33m!\033[0m %s\n' "$*"; }

if [[ ! -f "${WRAPPER_JS}" ]]; then
    warn "MCP wrapper not found at ${WRAPPER_JS}; run scripts/install.sh first if hosts cannot start iai-mcp"
fi

step "Claude Code MCP registration"
if command -v claude >/dev/null 2>&1; then
    claude mcp add iai-mcp -- node "${WRAPPER_JS}" || warn "Claude Code registration failed; continuing with other hosts"
    ok "Claude Code registration attempted"
else
    warn "claude CLI not found; skipping Claude Code registration"
fi

step "Claude Desktop MCP config"
python3 - "${WRAPPER_JS}" <<'PY'
import json
import os
import sys
from pathlib import Path

wrapper = sys.argv[1]
if sys.platform == "darwin":
    config = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
elif os.name == "nt":
    appdata = os.environ.get("APPDATA")
    config = Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
else:
    config = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

if config is None:
    print("WARN: cannot determine Claude Desktop config path")
    raise SystemExit(0)

config.parent.mkdir(parents=True, exist_ok=True)
try:
    data = json.loads(config.read_text()) if config.exists() and config.read_text().strip() else {}
except json.JSONDecodeError as exc:
    print(f"WARN: {config} is not valid JSON ({exc}); leaving it unchanged")
    raise SystemExit(0)

if not isinstance(data, dict):
    print(f"WARN: {config} top-level JSON is not an object; leaving it unchanged")
    raise SystemExit(0)

servers = data.setdefault("mcpServers", {})
if not isinstance(servers, dict):
    print(f"WARN: {config} mcpServers is not an object; leaving it unchanged")
    raise SystemExit(0)

servers["iai-mcp"] = {"command": "node", "args": [wrapper]}
config.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
print(f"OK: ensured iai-mcp in {config}")
PY

step "Codex MCP and hooks config"
python3 - "${WRAPPER_JS}" "${PYTHON_BIN}" "${IAI_STORE}" <<'PY'
from pathlib import Path
import re
import sys

wrapper, python_bin, store = sys.argv[1:4]
config = Path.home() / ".codex" / "config.toml"
config.parent.mkdir(parents=True, exist_ok=True)
text = config.read_text() if config.exists() else ""
changed = False

# Preserve the user's TOML as text. Add the iai-mcp server only if absent.
if not re.search(r"(?m)^\s*\[mcp_servers\.iai-mcp\]\s*$", text):
    block = f'''
[mcp_servers.iai-mcp]
command = "node"
args = ["{wrapper.replace('\\', '\\\\').replace('"', '\\"')}"]

[mcp_servers.iai-mcp.env]
IAI_MCP_PYTHON = "{python_bin.replace('\\', '\\\\').replace('"', '\\"')}"
IAI_MCP_STORE = "{store.replace('\\', '\\\\').replace('"', '\\"')}"
'''
    text = text.rstrip() + "\n" + block if text.strip() else block.lstrip()
    changed = True

features = re.search(r"(?ms)^(\s*\[features\]\s*\n)(.*?)(?=^\s*\[|\Z)", text)
if features:
    body = features.group(2)
    if re.search(r"(?m)^\s*hooks\s*=", body):
        new_body = re.sub(r"(?m)^(\s*hooks\s*=\s*).*$", r"\1true", body, count=1)
    else:
        new_body = body + ("" if body.endswith("\n") else "\n") + "hooks = true\n"
    if new_body != body:
        text = text[:features.start(2)] + new_body + text[features.end(2):]
        changed = True
else:
    text = text.rstrip() + "\n\n[features]\nhooks = true\n" if text.strip() else "[features]\nhooks = true\n"
    changed = True

if changed:
    config.write_text(text)
print(f"OK: ensured iai-mcp MCP server and [features].hooks = true in {config}")
PY
ok "Codex config updated without writing deprecated codex_hooks = true"

step "Install capture hooks for all supported targets"
iai-mcp capture-hooks install --target all
ok "capture hooks install completed"

step "Capture hook status for all supported targets"
iai-mcp capture-hooks status --target all
ok "capture hooks status completed"
