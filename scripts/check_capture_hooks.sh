#!/usr/bin/env bash
# Check whether installed MCP hosts appear to be producing recent iai-mcp
# capture-hook logs. This script is intentionally read-only: it does not edit
# host configuration, install packages, or require network access.

set -euo pipefail

RECENT_SECONDS="${IAI_MCP_HOOK_RECENT_SECONDS:-86400}"
LOG_DIR="${IAI_MCP_LOG_DIR:-$HOME/.iai-mcp/logs}"
TODAY_UTC="$(date -u +%Y-%m-%d)"
NOW_EPOCH="$(date -u +%s)"
EXIT_STATUS=0

usage() {
  cat <<USAGE
Usage: $0 [all|claude|codex ...]

Checks installed/requested Claude and Codex CLIs for recent iai-mcp hook log
activity under: $LOG_DIR

Environment:
  IAI_MCP_HOOK_RECENT_SECONDS  Freshness window in seconds (default: 86400)
  IAI_MCP_LOG_DIR              Hook log directory (default: ~/.iai-mcp/logs)
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

REQUESTED=("${@:-all}")
if [[ ${#REQUESTED[@]} -eq 0 || " ${REQUESTED[*]} " == *" all "* ]]; then
  REQUESTED=(claude codex)
fi

start_cli_probe() {
  local host="$1"
  local cmd="$2"
  local tmp
  tmp="$(mktemp)"

  # Version/help probes are cheap and should not require network access. Run in
  # the background so a slow host cannot block checks for the other host.
  (
    if command -v timeout >/dev/null 2>&1; then
      timeout 5 "$cmd" --version >/dev/null 2>"$tmp" || timeout 5 "$cmd" --help >/dev/null 2>>"$tmp" || true
    else
      "$cmd" --version >/dev/null 2>"$tmp" || "$cmd" --help >/dev/null 2>>"$tmp" || true
    fi
  ) &
  PROBES+=("$host:$!:$tmp")
}

file_is_recent() {
  local path="$1"
  [[ -f "$path" ]] || return 1

  local mtime
  if ! mtime="$(stat -c %Y "$path" 2>/dev/null)"; then
    if ! mtime="$(stat -f %m "$path" 2>/dev/null)"; then
      return 1
    fi
  fi

  (( NOW_EPOCH - mtime <= RECENT_SECONDS ))
}

find_recent_log() {
  local prefix="$1"
  local today_path="$LOG_DIR/${prefix}-${TODAY_UTC}.log"

  if file_is_recent "$today_path"; then
    printf '%s\n' "$today_path"
    return 0
  fi

  [[ -d "$LOG_DIR" ]] || return 1
  local path
  while IFS= read -r -d '' path; do
    if file_is_recent "$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done < <(find "$LOG_DIR" -maxdepth 1 -type f -name "${prefix}-????-??-??.log" -print0 2>/dev/null)

  return 1
}

contains_marker() {
  local file="$1"
  local marker="$2"
  [[ -f "$file" ]] && grep -Fq -- "$marker" "$file" 2>/dev/null
}

claude_configured() {
  [[ -x "$HOME/.claude/hooks/iai-mcp-session-capture.sh" || -x "$HOME/.claude/hooks/iai-mcp-turn-capture.sh" ]] && return 0
  contains_marker "$HOME/.claude/settings.json" "iai-mcp-session-capture.sh" && return 0
  contains_marker "$HOME/.claude/settings.json" "iai-mcp-turn-capture.sh" && return 0
  return 1
}

codex_configured() {
  local cfg="$HOME/.codex/config.toml"
  [[ -f "$cfg" ]] || return 1
  contains_marker "$cfg" "iai-mcp" || return 1
  if contains_marker "$cfg" "capture" || contains_marker "$cfg" "hook" || contains_marker "$cfg" "hooks"; then
    return 0
  fi
  return 1
}

host_cli() {
  case "$1" in
    claude) printf 'claude\n' ;;
    codex) printf 'codex\n' ;;
    *) return 1 ;;
  esac
}

host_configured() {
  case "$1" in
    claude) claude_configured ;;
    codex) codex_configured ;;
    *) return 1 ;;
  esac
}

print_log_summary() {
  local host="$1"
  local capture_log=""
  local turn_log=""
  local have_capture=0
  local have_turn=0

  if capture_log="$(find_recent_log capture)"; then
    have_capture=1
  fi
  if turn_log="$(find_recent_log turn-capture)"; then
    have_turn=1
  fi

  echo "[$host] iai-mcp hook logs:"
  if (( have_capture )); then
    echo "  capture:      recent activity observed ($capture_log)"
  else
    echo "  capture:      no recent capture-YYYY-MM-DD.log activity found"
  fi

  if (( have_turn )); then
    echo "  turn-capture: recent activity observed ($turn_log)"
  else
    echo "  turn-capture: no recent turn-capture-YYYY-MM-DD.log activity found"
  fi

  # Codex currently records through Stop/session-capture style logs in this
  # project; Claude Code may produce both per-turn and Stop logs. Treat either
  # log family as activity for each installed/configured host because the log
  # files themselves are shared iai-mcp hook logs rather than host-specific logs.
  (( have_capture || have_turn ))
}

echo "iai-mcp capture hook check"
echo "log directory: $LOG_DIR"
echo "freshness window: ${RECENT_SECONDS}s"
echo

PROBES=()
for host in "${REQUESTED[@]}"; do
  case "$host" in
    claude|codex) ;;
    *) echo "[$host] unknown host — skipped"; continue ;;
  esac

  cli="$(host_cli "$host")"
  if command -v "$cli" >/dev/null 2>&1; then
    echo "[$host] CLI installed: $(command -v "$cli")"
    start_cli_probe "$host" "$cli"
  else
    echo "[$host] CLI not installed — skipped"
    echo
    continue
  fi

  if host_configured "$host"; then
    echo "[$host] iai-mcp hook configuration: appears present"
    if print_log_summary "$host"; then
      echo "[$host] status: OK — recent hook log activity observed"
    else
      echo "[$host] status: FAIL — configured host has no recent hook log activity"
      EXIT_STATUS=1
    fi
  else
    echo "[$host] iai-mcp hook configuration not detected — skipped log-failure enforcement"
    print_log_summary "$host" >/dev/null || true
    echo "[$host] status: SKIPPED — install hooks before expecting activity"
  fi
  echo
done

for probe in "${PROBES[@]}"; do
  IFS=: read -r host pid tmp <<<"$probe"
  if wait "$pid"; then
    echo "[$host] lightweight CLI probe completed"
  else
    echo "[$host] lightweight CLI probe completed with nonzero status (ignored)"
  fi
  rm -f "$tmp"
done

exit "$EXIT_STATUS"
