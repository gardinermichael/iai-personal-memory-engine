#!/bin/sh
# IAI-MCP Stop hook — ambient WRITE-side capture.
#
# Fires when a Claude Code session ends. Reads the session's JSONL transcript,
# batch-captures user + assistant turns into the iai-mcp episodic tier through
# `iai-mcp capture-transcript --no-spawn`. Never spawns a daemon — if the
# daemon is unreachable the call defers events to
# ~/.iai-mcp/.deferred-captures/ for the daemon to drain on next socket
# activation.
#
# Fail-safe by design: any error exits 0 so session teardown is never blocked.
# Logs go to ~/.iai-mcp/logs/capture-YYYY-MM-DD.log for audit.
#
# Hook payload (stdin JSON from Claude Code and Codex-compatible hook hosts)
# contains shared fields such as:
#   - session_id       (UUID of the session that just ended)
#   - transcript_path  (absolute path to the session JSONL) — when absent, we
#                      fall back only to Claude Code's per-project transcript
#                      dir for the matching session_id.
#   - cwd              (working directory at session end)

set -u  # no -e: we must not abort on errors, fail-safe is paramount
input=$(cat 2>/dev/null || true)

# Best-effort jq; fall back to Python if jq missing.
extract() {
  key=$1
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$input" | jq -r ".${key} // empty" 2>/dev/null
  else
    printf '%s' "$input" | /usr/bin/python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('${key}', '') or '')
except Exception:
    print('')
" 2>/dev/null
  fi
}

session_id=$(extract "session_id")
transcript_path=$(extract "transcript_path")
cwd=$(extract "cwd")

# Claude fallback: locate transcript if the hook payload did not include its
# path. Claude Code stores transcripts under ~/.claude/projects/{cwd-hash}/{uuid}.jsonl
if [ -z "$transcript_path" ] && [ -n "$session_id" ]; then
  projects_dir="$HOME/.claude/projects"
  if [ -d "$projects_dir" ]; then
    # Look for the most recent file whose basename starts with session_id.
    # ls -t (mtime newest first). Avoid `find` per the project's no-grep hook.
    for d in "$projects_dir"/*/; do
      candidate="${d}${session_id}.jsonl"
      if [ -f "$candidate" ]; then
        transcript_path="$candidate"
        break
      fi
    done
  fi
fi

mkdir -p "$HOME/.iai-mcp/logs" 2>/dev/null || true
log="$HOME/.iai-mcp/logs/capture-$(date -u +%Y-%m-%d).log"
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

{
  echo "---"
  echo "$ts session=$session_id cwd=$cwd transcript=$transcript_path"
} >> "$log" 2>/dev/null

# Skip if we couldn't find anything to capture.
if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  echo "$ts skipped: no transcript found" >> "$log" 2>/dev/null
  exit 0
fi

# Locate the `iai-mcp` CLI. Resolution order:
#   1. IAI_MCP_SESSION_CAPTURE_CLI environment variable — highest priority;
#      set in your shell init for non-standard install locations.
#   2. ~/.iai-mcp/.cli-path cache — written on first successful resolution
#      so subsequent session-ends skip the scan entirely.
#   3. `command -v iai-mcp` — PATH lookup; picks up pyenv shims, pipx
#      wrappers, and any other PATH-managed install transparently.
#   4. Baked-in candidate list — checked when PATH has no entry; covers
#      common install locations (pyenv shims, pipx, homebrew, user-site,
#      dev venv).
# Only generic $HOME-relative or system paths belong here; install-specific
# paths belong in the env var or the cache.
cli_cache="$HOME/.iai-mcp/.cli-path"
iai_cli=""
if [ -n "${IAI_MCP_SESSION_CAPTURE_CLI:-}" ] && [ -x "$IAI_MCP_SESSION_CAPTURE_CLI" ]; then
  iai_cli="$IAI_MCP_SESSION_CAPTURE_CLI"
fi
if [ -z "$iai_cli" ] && [ -f "$cli_cache" ]; then
  cached=$(cat "$cli_cache" 2>/dev/null || true)
  [ -x "$cached" ] && iai_cli="$cached"
fi
if [ -z "$iai_cli" ]; then
  resolved=$(command -v iai-mcp 2>/dev/null || true)
  if [ -n "$resolved" ] && [ -x "$resolved" ]; then
    iai_cli="$resolved"
    printf '%s' "$iai_cli" > "$cli_cache" 2>/dev/null || true
  fi
fi
if [ -z "$iai_cli" ]; then
  for candidate in \
    "$HOME/.pyenv/shims/iai-mcp" \
    "$HOME/.local/bin/iai-mcp" \
    "$HOME/.local/pipx/venvs/iai-mcp/bin/iai-mcp" \
    "/opt/homebrew/bin/iai-mcp" \
    "$HOME/IAI-MCP/.venv/bin/iai-mcp" \
    "/usr/local/bin/iai-mcp"
  do
    if [ -x "$candidate" ]; then
      iai_cli="$candidate"
      printf '%s' "$iai_cli" > "$cli_cache" 2>/dev/null || true
      break
    fi
  done
fi

if [ -z "$iai_cli" ]; then
  echo "$ts skipped: iai-mcp CLI not found" >> "$log" 2>/dev/null
  exit 0
fi

# Atomically rename the active-writer marker so the drain can see it on the
# next WAKE/DROWSY pass. Target name uses `.live-${epoch}.jsonl` so it never
# collides with the safety-net output shape `${session_id}-${epoch}.jsonl`
# in the same second. Also clean the per-session offset state — the session
# is ending, no further per-turn writes will reference it.
if [ -n "$session_id" ]; then
  live_file="$HOME/.iai-mcp/.deferred-captures/${session_id}.live.jsonl"
  if [ -f "$live_file" ]; then
    mv "$live_file" "$HOME/.iai-mcp/.deferred-captures/${session_id}.live-$(date +%s).jsonl" 2>/dev/null || true
  fi
  offset_state="$HOME/.iai-mcp/.capture-state/${session_id}.offset"
  [ -f "$offset_state" ] && rm -f "$offset_state" 2>/dev/null
fi

# Run capture with a 30s hard timeout — if it hangs, the session must still
# end cleanly. `timeout` is in coreutils (macOS: brew install coreutils). We
# fall back to a background kill loop if absent.
if command -v timeout >/dev/null 2>&1; then
  result=$(timeout 30 "$iai_cli" capture-transcript --no-spawn \
    --session-id "$session_id" \
    --max-turns 100000 \
    "$transcript_path" 2>&1)
elif command -v gtimeout >/dev/null 2>&1; then
  result=$(gtimeout 30 "$iai_cli" capture-transcript --no-spawn \
    --session-id "$session_id" \
    --max-turns 100000 \
    "$transcript_path" 2>&1)
else
  result=$("$iai_cli" capture-transcript --no-spawn \
    --session-id "$session_id" \
    --max-turns 100000 \
    "$transcript_path" 2>&1)
fi
rc=$?

{
  echo "$ts rc=$rc result=$result"
} >> "$log" 2>/dev/null

exit 0
