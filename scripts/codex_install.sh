#!/usr/bin/env bash
# scripts/codex_install.sh — install iai-mcp capture hooks for Codex.
#
# Usage:
#   bash scripts/codex_install.sh
#
# Idempotent. Safe to re-run.

set -euo pipefail

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '   \033[0;32m✓\033[0m %s\n' "$*"; }
warn() { printf '   \033[0;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

step "preflight"
if ! command -v iai-mcp >/dev/null 2>&1; then
    cat >&2 <<'MSG'
iai-mcp was not found on PATH.

Install it first, then re-run this script. From an iai-mcp source checkout, run:

  bash scripts/install.sh

If you installed iai-mcp already, make sure its bin directory is on PATH. For the
standard local install, add this to your shell profile and restart the shell:

  export PATH="${HOME}/.local/bin:${PATH}"
MSG
    exit 1
fi
IAI_MCP="$(command -v iai-mcp)"
ok "iai-mcp found at ${IAI_MCP}"

step "Codex capture hooks"
"${IAI_MCP}" capture-hooks install --target codex
ok "Codex hook install command completed"

step "Codex hooks status"
if "${IAI_MCP}" capture-hooks status --target codex; then
    ok "Codex hooks report active"
else
    warn "Codex hooks status did not report active; review the status output above"
fi

step "Codex feature flag"
CODEX_DIR="${HOME}/.codex"
CODEX_CONFIG="${CODEX_DIR}/config.toml"
mkdir -p "${CODEX_DIR}"

python3 - "${CODEX_CONFIG}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text() if path.exists() else ""
lines = text.splitlines()
had_trailing_newline = text.endswith("\n") or not text

section_re = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
deprecated_re = re.compile(r"^\s*codex_hooks\s*=")
hooks_re = re.compile(r"^\s*hooks\s*=")

# Never leave behind the deprecated key that older Codex installs used.
lines = [line for line in lines if not deprecated_re.match(line)]

features_start = None
features_end = len(lines)
for idx, line in enumerate(lines):
    match = section_re.match(line)
    if not match:
        continue
    if match.group(1).strip() == "features":
        features_start = idx
        features_end = len(lines)
        for end_idx in range(idx + 1, len(lines)):
            if section_re.match(lines[end_idx]):
                features_end = end_idx
                break
        break

if features_start is None:
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(["[features]", "hooks = true"])
else:
    hooks_idx = None
    for idx in range(features_start + 1, features_end):
        if hooks_re.match(lines[idx]):
            hooks_idx = idx
            break
    if hooks_idx is None:
        lines.insert(features_end, "hooks = true")
    else:
        lines[hooks_idx] = "hooks = true"

new_text = "\n".join(lines)
if had_trailing_newline or new_text:
    new_text += "\n"
path.write_text(new_text)
PY
ok "ensured ${CODEX_CONFIG} contains [features] hooks = true"
ok "deprecated codex_hooks key was not written"

step "next steps"
echo "Codex reads hook configuration at startup. If a Codex session is already open,"
echo "fully quit it and start a new Codex session so the capture hooks and"
echo "~/.codex/config.toml feature flag are loaded."
echo
echo "To verify after restart, run:"
echo "  iai-mcp capture-hooks status --target codex"
