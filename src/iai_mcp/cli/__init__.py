from __future__ import annotations

import argparse
import importlib.resources as _res
import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


LOCK_PATH: Path = Path.home() / ".iai-mcp" / ".lock"
SOCKET_PATH: Path = Path.home() / ".iai-mcp" / ".daemon.sock"
STATE_PATH: Path = Path.home() / ".iai-mcp" / ".daemon-state.json"

LAUNCHD_TARGET: Path = Path.home() / "Library" / "LaunchAgents" / "com.iai-mcp.daemon.plist"
SYSTEMD_TARGET: Path = Path.home() / ".config" / "systemd" / "user" / "iai-mcp-daemon.service"

DAEMON_LABEL: str = "com.iai-mcp.daemon"
SERVICE_NAME: str = "iai-mcp-daemon.service"

CONSENT_BANNER: str = """\
==============================================================================
iai Sleep Daemon -- First Install Consent
==============================================================================

The sleep daemon runs in the background between Claude Code sessions to
perform neural consolidation (REM cycles, schema induction, drift detection).

Resource cost:
  - RAM: ~400 MB (bge-small-en-v1.5 embedding model kept warm to avoid cold-start)
  - CPU: brief bursts during REM cycles inside your learned quiet window
  - Disk: ~50MB/week in event logs + schema candidates

Claude subscription impact:
  - Max 1 `claude -p` call per night ("lucid moment" main insight)
  - Hard cap: 1% of daily subscription quota, 7% weekly buffer
  - ZERO API costs (no paid-API key -- uses your subscription only)

Opt out anytime:
  iai-mcp daemon uninstall

Continue? [y/N]: """


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _ensure_crypto_key_present():
    if os.environ.get("IAI_MCP_CRYPTO_PASSPHRASE"):
        return None
    from iai_mcp.crypto import KEY_BYTES, CryptoKey
    ck = CryptoKey(user_id="default")
    path = ck._key_file_path()
    if path.exists():
        return None
    import secrets as _secrets
    fresh = _secrets.token_bytes(KEY_BYTES)
    ck._try_file_set(fresh)
    print(f"crypto: created {path} (mode 0o600, {KEY_BYTES} bytes)")
    return path


def _try_short_timeout_connect(timeout_ms: int = 250) -> bool:
    import socket as _socket

    sock_path = os.environ.get("IAI_DAEMON_SOCKET_PATH") or str(SOCKET_PATH)
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout_ms / 1000.0)
    try:
        s.connect(sock_path)
        return True
    except (FileNotFoundError, ConnectionRefusedError, OSError, _socket.timeout):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass




def _send_jsonrpc_request(
    method: str,
    params: dict,
    *,
    connect_timeout: float = 5.0,
    read_timeout: float = 30.0,
) -> dict | None:
    import asyncio
    from iai_mcp.cli._capture import _is_custom_store as _isc
    if not os.environ.get("IAI_DAEMON_SOCKET_PATH") and _isc():
        return None

    sock_path = os.environ.get("IAI_DAEMON_SOCKET_PATH") or str(SOCKET_PATH)

    async def _runner() -> dict | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(sock_path),
                timeout=connect_timeout,
            )
        except (FileNotFoundError, ConnectionRefusedError, OSError, asyncio.TimeoutError):
            return None
        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=read_timeout)
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            logger.debug("jsonrpc request failed: %s", exc)
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    try:
        return asyncio.run(_runner())
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("jsonrpc asyncio.run failed: %s", exc)
        return None




def _send_socket_request(req: dict, *, timeout: float = 30.0) -> dict | None:
    import asyncio

    async def _runner() -> dict | None:
        _sock = os.environ.get("IAI_DAEMON_SOCKET_PATH") or str(SOCKET_PATH)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(_sock),
                timeout=5.0,
            )
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        except OSError:
            return None
        try:
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                return None
            return json.loads(line.decode("utf-8"))
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    return asyncio.run(_runner())


def compute_session_start_tokens_p90(store: "MemoryStore") -> dict[str, int | None]:
    import statistics

    from iai_mcp.events import query_events

    events = query_events(store, kind="session_started", limit=100)
    samples = [
        int(e["data"]["total_cached_tokens"])
        for e in events
        if isinstance(e.get("data"), dict) and "total_cached_tokens" in e["data"]
    ]
    if not samples:
        return {"p90": None, "n_samples": 0}
    if len(samples) == 1:
        return {"p90": samples[0], "n_samples": 1}
    q = statistics.quantiles(samples, n=10, method="inclusive")
    p90 = int(round(q[8]))
    return {"p90": p90, "n_samples": len(samples)}






def _claude_desktop_config_path() -> Path | None:
    import platform as _plat
    home = Path.home()
    sysname = _plat.system()
    if sysname == "Darwin":
        p = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif sysname == "Windows":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        p = Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        p = Path(xdg) / "Claude" / "claude_desktop_config.json"
    return p if p.parent.exists() else None






def _maintenance_compact_metrics(
    hippo_dir: Path,
    store: object | None = None,
) -> dict:
    db_path = hippo_dir / "brain.sqlite3"
    size_bytes = 0
    try:
        if db_path.exists():
            size_bytes = db_path.stat().st_size
    except OSError:
        pass
    size_mb = round(size_bytes / (1024 * 1024), 1)
    records_count = 0
    record_id_set: set[str] = set()
    if store is not None:
        try:
            tbl = store.db.open_table("records")
            records_count = int(tbl.count_rows())
            df = tbl.search().select(["id"]).to_pandas()
            record_id_set = {str(x) for x in df["id"].tolist()}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("compact metrics read failed: %s", exc)
    return {
        "db_size_mb": size_mb,
        "records_count": records_count,
        "record_id_set": record_id_set,
    }


from ._analytics import (
    cmd_health,
    cmd_topology,
    cmd_trajectory,
    cmd_audit,
    cmd_bank_recall,
    cmd_build_native,
    cmd_migrate,
)

from ._maintenance import (
    cmd_schema_cleanup,
    cmd_maintenance_compact_hippo,
    cmd_maintenance_compact_records,
    cmd_maintenance_symmetrize_self_loops,
    cmd_lifecycle_force_unlock,
    cmd_lifecycle_status,
    cmd_maintenance_sleep_cycle,
    cmd_drain_permanent_failed,
    _maintenance_compact_preflight_daemon_alive,
    _maintenance_compact_dry_run,
    _maintenance_compact_apply,
    _format_relative,
    _print_drain_result,
)

from ._crypto import (
    cmd_crypto_status,
    cmd_crypto_rotate,
    cmd_crypto_recover_prior_key,
    cmd_crypto_redact_undecryptable,
    cmd_crypto_migrate_to_file,
    cmd_crypto_init,
)

from ._capture import (
    _truncate_for_claude_code_hook,
    _is_custom_store,
    cmd_session_start,
    get_other_sessions_live_size,
    read_live_fingerprint,
    write_live_fingerprint,
    get_max_created_at,
    _utc_iso,
    read_watermark,
    write_watermark,
    cmd_session_refresh_if_stale,
    cmd_capture_transcript,
    cmd_capture_turn_deferred,
    _capture_hook_paths,
    _turn_hook_paths,
    _resolve_wrapper_path,
    _build_iai_mcp_server_entry,
    _patch_claude_desktop_config,
    _patch_claude_code_config,
    _CAPTURE_HOOK_MARKER,
    _TURN_HOOK_MARKER,
    _SESSION_RECALL_HOOK_MARKER,
    _session_recall_hook_paths,
    _load_settings,
    cmd_capture_hooks_install,
    cmd_capture_hooks_uninstall,
    cmd_capture_hooks_status,
)

from ._daemon import (
    cmd_daemon_install,
    cmd_daemon_uninstall,
    cmd_daemon_start,
    cmd_daemon_stop,
    cmd_daemon_status,
    cmd_daemon_logs,
    cmd_daemon_force_rem,
    cmd_daemon_pause,
    cmd_daemon_resume,
    cmd_daemon_stats,
    cmd_daemon_configure,
    _launchd_template,
    _render_launchd_plist,
    _render_systemd_unit,
    _prompt_consent,
    _record_consent_receipt,
    _remove_state_files,
    _compute_p90_from_events,
    _render_daemon_stats,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iai-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("health", help="show LLM health status")
    h.set_defaults(func=cmd_health)

    bn = sub.add_parser(
        "build-native",
        help=(
            "compile the Rust native extension (iai_mcp_native) in-place. "
            "Run after Python upgrade or on fresh clone. Requires cargo."
        ),
    )
    bn.set_defaults(func=cmd_build_native)

    m = sub.add_parser(
        "migrate",
        help=(
            "migrate records: 1->2 (schema) or 2->3 (encryption); "
            "OR --resume / --rollback a partial reembed migration"
        ),
    )
    m.add_argument("--from", dest="from_", type=int, default=1)
    m.add_argument("--to", type=int, default=2)
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--verbose", "-v", action="store_true")
    m.add_argument(
        "--resume",
        action="store_true",
        help="Resume a partial reembed migration from migration_progress.json checkpoint.",
    )
    m.add_argument(
        "--rollback",
        action="store_true",
        help=(
            "Roll back a partial reembed migration: drop records_v_new and "
            "(if needed) restore records from records_old_<ts>."
        ),
    )
    m.add_argument(
        "--rederive-timestamps",
        action="store_true",
        help=(
            "Re-derive collapsed created_at timestamps from on-disk transcripts. "
            "One-time operation; idempotent. Records with no recoverable transcript "
            "are left unchanged."
        ),
    )
    m.set_defaults(func=cmd_migrate)

    c = sub.add_parser(
        "crypto",
        help="encryption key management",
        description="""\
Manage encryption keys and encrypted record recovery workflows.

The crypto commands create and inspect the local file-backed key, rotate all
encrypted records to a new key, recover records with a prior raw key file,
migrate legacy macOS Keychain material to the file backend, and redact data
that cannot be decrypted.
""",
        epilog="""\
Examples:
  iai-mcp crypto status
  iai-mcp crypto init --user-id default
  iai-mcp crypto rotate
  iai-mcp crypto recover-with-prior-key --prior-key-file ./old.crypto.key --dry-run
  iai-mcp crypto recover-with-prior-key --prior-key-file ./old.crypto.key
  iai-mcp crypto migrate-to-file --keep-keychain
  iai-mcp crypto redact-undecryptable

Side effects:
  init creates a fresh .crypto.key file and refuses to overwrite an existing key.
  rotate re-encrypts all records under a new key.
  recover-with-prior-key stages records, decrypts with current/prior keys, then
    atomically swaps recovered data back under the current key; --dry-run only
    reports affected rows.
  migrate-to-file may write .crypto.key and can delete the legacy Keychain entry
    when --delete-keychain is used.
  redact-undecryptable is destructive: undecryptable literal text is replaced by
    a redaction marker while embeddings, edges, and metadata are preserved.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    crypto_sub = c.add_subparsers(dest="crypto_cmd", required=True)

    cs = crypto_sub.add_parser(
        "status",
        help=(
            "show file-backend key status: backend, path, "
            "mode, uid, length validation, passphrase-fallback flag"
        ),
    )
    cs.add_argument("--user-id", dest="user_id", default="default")
    cs.set_defaults(func=cmd_crypto_status)

    cr = crypto_sub.add_parser(
        "rotate", help="rotate encryption key + re-encrypt all records"
    )
    cr.add_argument("--user-id", dest="user_id", default="default")
    cr.set_defaults(func=cmd_crypto_rotate)

    mtf = crypto_sub.add_parser(
        "migrate-to-file",
        help=(
            "one-time: read existing key from macOS Keychain "
            "and write to .crypto.key file (interactive Terminal only)"
        ),
    )
    mtf.add_argument("--user-id", dest="user_id", default="default")
    mtf_group = mtf.add_mutually_exclusive_group()
    mtf_group.add_argument(
        "--keep-keychain",
        dest="keep_keychain",
        action="store_true",
        default=True,
        help="leave the existing macOS Keychain entry in place (default)",
    )
    mtf_group.add_argument(
        "--delete-keychain",
        dest="keep_keychain",
        action="store_false",
        help="delete the macOS Keychain entry after successful migration",
    )
    mtf.set_defaults(func=cmd_crypto_migrate_to_file)

    ci = crypto_sub.add_parser(
        "init",
        help=(
            "generate a fresh .crypto.key file "
            "(fresh installs only — refuses if file exists)"
        ),
    )
    ci.add_argument("--user-id", dest="user_id", default="default")
    ci.set_defaults(func=cmd_crypto_init)

    rwpk = crypto_sub.add_parser(
        "recover-with-prior-key",
        help=(
            "stage all records, decrypt literal/provenance/gain with current "
            "then prior key, re-encrypt under current key; atomic Lance swap"
        ),
    )
    rwpk.add_argument(
        "--prior-key-file",
        type=Path,
        required=True,
        help="path to exactly 32 raw AES key bytes (same format as .crypto.key)",
    )
    rwpk.add_argument("--user-id", dest="user_id", default="default")
    rwpk.add_argument(
        "--dry-run",
        action="store_true",
        help="report rows that need the prior key without mutating tables",
    )
    rwpk.set_defaults(func=cmd_crypto_recover_prior_key)

    cred = crypto_sub.add_parser(
        "redact-undecryptable",
        help=(
            "replace literal_surface that fails AES-GCM decrypt with a redacted "
            "marker (preserves embeddings, edges, metadata)"
        ),
    )
    cred.add_argument("--user-id", dest="user_id", default="default")
    cred.set_defaults(func=cmd_crypto_redact_undecryptable)

    t = sub.add_parser(
        "trajectory",
        help="aggregate M1..M6 trajectory events",
    )
    t.add_argument(
        "--since",
        type=int,
        default=None,
        help="weeks back to include (default: all history)",
    )
    t.set_defaults(func=cmd_trajectory)

    topo = sub.add_parser(
        "topology",
        help="live small-world topology snapshot: C, L, sigma, communities, rich-club ratio, N, regime",
    )
    topo.set_defaults(func=cmd_topology)

    cap = sub.add_parser(
        "capture-transcript",
        help=(
            "batch-capture a Claude Code JSONL transcript into episodic tier. "
            "Used by the Stop hook for ambient WRITE-side observation capture."
        ),
    )
    cap.add_argument("transcript_path", help="path to the Claude Code JSONL transcript file")
    cap.add_argument("--session-id", default="-", help="session id for provenance")
    cap.add_argument("--max-turns", type=int, default=200,
                     help="cap on turns to scan (default 200; older turns skipped)")
    cap.add_argument(
        "--no-spawn",
        action="store_true",
        default=False,
        help=(
            "Hook-only mode: try connect with 250ms timeout. On miss, write "
            "transcript to ~/.iai-mcp/.deferred-captures/ and exit 0 within 2s. "
            "NEVER spawn daemon. Used by ~/.claude/hooks/iai-mcp-session-capture.sh "
            "to eliminate spawn vector."
        ),
    )
    cap.set_defaults(func=cmd_capture_transcript)

    ctd = sub.add_parser(
        "capture-turn-deferred",
        help=(
            "append a single JSONL event per new transcript turn to "
            "{session_id}.live.jsonl. UserPromptSubmit-hook backend."
        ),
    )
    ctd.add_argument("--session-id", required=True)
    ctd.add_argument("--transcript-path", required=True)
    ctd.add_argument(
        "--max-turns-per-call",
        type=int,
        default=200,
        help="max new turns to process per invocation (default 200)",
    )
    ctd.set_defaults(func=cmd_capture_turn_deferred)

    ssp = sub.add_parser(
        "session-start",
        help=(
            "print the session-start recall payload as markdown on stdout. "
            "Hook target for ~/.claude/hooks/iai-mcp-session-recall.sh."
        ),
    )
    ssp.add_argument("--session-id", default="-", help="session id for provenance")
    ssp.set_defaults(func=cmd_session_start)

    sris = sub.add_parser(
        "session-refresh-if-stale",
        help=(
            "UserPromptSubmit hook gate: compare MAX(created_at) against the "
            "per-session watermark sidecar; call session_refresh_if_stale RPC "
            "only when new memory exists; emit additionalContext JSON on trigger."
        ),
    )
    sris.add_argument("--session-id", default="-", help="session id for watermark sidecar")
    sris.set_defaults(func=cmd_session_refresh_if_stale)

    ch = sub.add_parser(
        "capture-hooks",
        help="install/uninstall/status the Claude Code Stop hook for ambient session capture",
        description="""\
Install, remove, or inspect assistant capture hooks.

Supported targets are Claude Code settings under ~/.claude and Claude Desktop's
MCP server configuration when that config directory is present. The installer
copies the iai-mcp wrapper scripts into ~/.claude/hooks/ and registers the Stop,
UserPromptSubmit, and session recall hooks needed for ambient transcript capture
and recall refresh.
""",
        epilog="""\
Examples:
  iai-mcp capture-hooks status
  iai-mcp capture-hooks install
  iai-mcp capture-hooks uninstall

Side effects:
  install creates/updates hook scripts in ~/.claude/hooks/, patches Claude Code
    settings.json hook entries, and may add/update the iai-mcp server entry in
    Claude Desktop config when available.
  uninstall removes only the iai-mcp-managed hook entries/scripts and leaves
    unrelated user hooks or assistant configuration intact.
  status reports detected files and active registrations without mutating state.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ch_sub = ch.add_subparsers(dest="capture_hooks_cmd", required=True)
    ch_sub.add_parser("install",
                      help="copy Stop hook to ~/.claude/hooks/ and register in settings.json"
                      ).set_defaults(func=cmd_capture_hooks_install)
    ch_sub.add_parser("uninstall",
                      help="remove the Stop hook and its settings.json entry"
                      ).set_defaults(func=cmd_capture_hooks_uninstall)
    ch_sub.add_parser("status",
                      help="show whether the Stop hook is installed and active"
                      ).set_defaults(func=cmd_capture_hooks_status)

    a = sub.add_parser(
        "audit",
        help="identity + shield audit log",
    )
    a.add_argument(
        "--since",
        type=int,
        default=None,
        help="weeks back to include (default: all history)",
    )
    a.add_argument(
        "--severity",
        choices=["info", "warning", "critical"],
        default=None,
        help="filter by severity",
    )
    audit_sub = a.add_subparsers(dest="audit_sub")
    for name, helptext in (
        ("shield", "shield-only audit (match counts redacted)"),
        ("drift", "detect M4 drift anomaly and surface it"),
        ("identity", "s5_* identity events only"),
    ):
        sp = audit_sub.add_parser(name, help=helptext)
        sp.add_argument("--since", type=int, default=None)
        sp.add_argument(
            "--severity",
            choices=["info", "warning", "critical"],
            default=None,
        )
    a.set_defaults(func=cmd_audit)

    d = sub.add_parser(
        "daemon",
        help="sleep daemon: install/uninstall/start/stop/status/logs/...",
        description=f"""\
Manage the iai-mcp sleep daemon as a user service.

On macOS, install/uninstall manages the launchd agent at:
  {LAUNCHD_TARGET}

On Linux, install/uninstall manages the systemd user unit at:
  {SYSTEMD_TARGET}

The daemon listens on the Unix socket:
  {SOCKET_PATH}

Use status for a socket round-trip health check, logs for launchd/journal output,
and configure/pause/resume/force-rem for scheduler control.
""",
        epilog="""\
Examples:
  iai-mcp daemon install --yes
  iai-mcp daemon start
  iai-mcp daemon status
  iai-mcp daemon logs -n 100 --follow
  iai-mcp daemon pause 3600
  iai-mcp daemon resume
  iai-mcp daemon stop
  iai-mcp daemon uninstall --yes

Side effects:
  install writes a launchd plist or systemd user unit and records consent.
  start loads/kickstarts the user service; stop sends SIGTERM via launchctl or
    systemctl and should let the daemon exit cleanly.
  uninstall removes service files and daemon state files.
  logs reads ~/Library/Logs output on macOS or journalctl --user output on Linux.
  force-rem triggers one out-of-schedule REM cycle through the daemon socket.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    daemon_sub = d.add_subparsers(dest="daemon_cmd", required=True)

    di = daemon_sub.add_parser(
        "install",
        help=(
            "install launchd plist (macOS) / systemd user unit (Linux); "
            "first-run consent banner unless --yes"
        ),
    )
    di.add_argument(
        "--dry-run",
        action="store_true",
        help="print plist/unit contents without writing or invoking launchctl/systemctl",
    )
    di.add_argument(
        "--yes", "-y",
        action="store_true",
        help="skip the consent banner (records --yes audit-trail still)",
    )
    di.set_defaults(func=cmd_daemon_install)

    du = daemon_sub.add_parser(
        "uninstall",
        help="C4 clean uninstall: remove plist/unit + 3 state files",
    )
    du.add_argument("--yes", "-y", action="store_true")
    du.set_defaults(func=cmd_daemon_uninstall)

    daemon_sub.add_parser(
        "start", help="launchctl kickstart / systemctl --user start",
    ).set_defaults(func=cmd_daemon_start)

    daemon_sub.add_parser(
        "stop", help="launchctl kill SIGTERM / systemctl --user stop",
    ).set_defaults(func=cmd_daemon_stop)

    daemon_sub.add_parser(
        "status",
        help=(
            "socket round-trip: print daemon FSM state, uptime, version "
            "(warns on version skew vs installed package)"
        ),
    ).set_defaults(func=cmd_daemon_status)

    dlogs = daemon_sub.add_parser(
        "logs",
        help="tail daemon log file (macOS Library/Logs) or journalctl (Linux)",
    )
    dlogs.add_argument("-f", "--follow", action="store_true")
    dlogs.add_argument("-n", "--lines", type=int, default=50)
    dlogs.set_defaults(func=cmd_daemon_logs)

    daemon_sub.add_parser(
        "force-rem",
        help="cooperative force: trigger one REM cycle out-of-schedule",
    ).set_defaults(func=cmd_daemon_force_rem)

    dpause = daemon_sub.add_parser(
        "pause", help="pause daemon scheduler for N seconds",
    )
    dpause.add_argument("seconds", type=int)
    dpause.set_defaults(func=cmd_daemon_pause)

    daemon_sub.add_parser(
        "resume", help="resume daemon scheduler after a pause",
    ).set_defaults(func=cmd_daemon_resume)

    daemon_sub.add_parser(
        "stats",
        help=(
            "VAL-02 longitudinal metrics: session_start_tokens_p90 over the "
            "most recent 100 session_started events (persisted in the events table)"
        ),
    ).set_defaults(func=cmd_daemon_stats)

    dconf = daemon_sub.add_parser(
        "configure",
        help=(
            "per-setting override: set-budget / set-cycle-count / "
            "set-quiet-window / disable-claude / enable-claude"
        ),
    )
    dconf.add_argument(
        "key",
        choices=[
            "set-budget",
            "set-cycle-count",
            "set-quiet-window",
            "disable-claude",
            "enable-claude",
        ],
    )
    dconf.add_argument("value", nargs="?", default=None)
    dconf.set_defaults(func=cmd_daemon_configure)

    sc = sub.add_parser(
        "schema-cleanup",
        help=(
            "soft-delete duplicate schema records. Default "
            "mode is --dry-run; --apply snapshots the memory store dir and "
            "performs the cleanup. Idempotent (re-running is a no-op)."
        ),
    )
    sc_mode = sc.add_mutually_exclusive_group()
    sc_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="(default) print the cleanup diff without mutating the store",
    )
    sc_mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="snapshot the store dir + soft-delete duplicates",
    )
    sc.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=(
            "IAI root directory (defaults to ~/.iai-mcp; Hippo data "
            "lives at <store-path>/hippo)"
        ),
    )
    sc.set_defaults(func=cmd_schema_cleanup)

    mtn = sub.add_parser(
        "maintenance",
        help=(
            "one-shot maintenance ops. Currently: compact-hippo "
            "(PRAGMA wal_checkpoint + VACUUM + hnswlib rebuild)."
        ),
        description="""\
Run one-shot maintenance operations for the local memory store.

Use compact-hippo when storage has grown or after heavy write activity; it
checkpoints WAL data, vacuums SQLite storage, and rebuilds hnswlib indexes.
Use sleep-cycle to run the normal consolidation pipeline on demand instead of
waiting for the daemon scheduler. Use schema-cleanup to remove duplicate schema
records, and symmetrize-self-loops to backfill legacy Hebbian self-loop edges.
""",
        epilog="""\
Examples:
  iai-mcp maintenance compact-hippo --dry-run
  iai-mcp daemon stop
  iai-mcp maintenance compact-hippo --apply --yes
  iai-mcp maintenance sleep-cycle --store-path ~/.iai-mcp
  iai-mcp maintenance sleep-cycle --reset-quarantine
  iai-mcp schema-cleanup --dry-run
  iai-mcp schema-cleanup --apply

Side effects:
  compact-hippo/compact-records require the daemon to be stopped; --apply mutates
    storage by checkpointing, vacuuming, and rebuilding indexes.
  sleep-cycle runs schema mining, knob tuning, dream decay, Hippo optimization,
    and record compaction once; --reset-quarantine clears quarantine state.
  schema-cleanup lives as a top-level command; --apply snapshots the store and
    soft-deletes duplicate schema records.
  symmetrize-self-loops --apply writes missing self-loop edges.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mtn_sub = mtn.add_subparsers(dest="maintenance_cmd", required=True)
    mtn_compact = mtn_sub.add_parser(
        "compact-hippo",
        help=(
            "compact Hippo storage: wal_checkpoint + VACUUM + hnswlib rebuild. "
            "DAEMON MUST BE STOPPED. Default --dry-run; --apply requires "
            "--yes for non-tty."
        ),
    )
    mtn_compact_mode = mtn_compact.add_mutually_exclusive_group()
    mtn_compact_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="(default) print metrics-only JSON; do NOT call optimize",
    )
    mtn_compact_mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="run wal_checkpoint + VACUUM + hnswlib rebuild on Hippo storage",
    )
    mtn_compact.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="(use with --apply) skip the interactive 'y/N' prompt",
    )
    mtn_compact.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=(
            "IAI root directory (defaults to ~/.iai-mcp; Hippo data "
            "lives at <store-path>/hippo). Mirrors `schema-cleanup` flag."
        ),
    )
    mtn_compact.set_defaults(func=cmd_maintenance_compact_hippo)
    mtn_compact_legacy = mtn_sub.add_parser(
        "compact-records",
        help="Deprecated alias for compact-hippo (kept for one release).",
    )
    mtn_compact_legacy_mode = mtn_compact_legacy.add_mutually_exclusive_group()
    mtn_compact_legacy_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="(default) print metrics-only JSON; do NOT call optimize",
    )
    mtn_compact_legacy_mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="run wal_checkpoint + VACUUM + hnswlib rebuild on Hippo storage",
    )
    mtn_compact_legacy.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="(use with --apply) skip the interactive 'y/N' prompt",
    )
    mtn_compact_legacy.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=(
            "IAI root directory (defaults to ~/.iai-mcp; Hippo data "
            "lives at <store-path>/hippo). Mirrors `schema-cleanup` flag."
        ),
    )
    mtn_compact_legacy.set_defaults(func=cmd_maintenance_compact_records)

    mtn_symmetrize = mtn_sub.add_parser(
        "symmetrize-self-loops",
        help=(
            "backfill missing hebbian self-loops on existing records. "
            "DAEMON MUST BE STOPPED. Default --dry-run; --apply requires "
            "--yes for non-tty."
        ),
    )
    mtn_symmetrize_mode = mtn_symmetrize.add_mutually_exclusive_group()
    mtn_symmetrize_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="(default) print counts JSON; do NOT write self-loops",
    )
    mtn_symmetrize_mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="write missing self-loops at delta=0.1 (hebbian edge_type)",
    )
    mtn_symmetrize.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="(use with --apply) skip the interactive 'y/N' prompt",
    )
    mtn_symmetrize.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=(
            "IAI root directory (defaults to ~/.iai-mcp; Hippo data "
            "lives at <store-path>/hippo). Mirrors compact-hippo flag."
        ),
    )
    mtn_symmetrize.set_defaults(func=cmd_maintenance_symmetrize_self_loops)

    mtn_sleep = mtn_sub.add_parser(
        "sleep-cycle",
        help=(
            "run the 5-step sleep pipeline once: "
            "schema_mine, knob_tune, dream_decay, optimize_hippo, "
            "compact_records. 3-strike auto-quarantine; use --force "
            "to override, --reset-quarantine to clear."
        ),
    )
    mtn_sleep.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="run even if quarantined (operator override)",
    )
    mtn_sleep.add_argument(
        "--reset-quarantine",
        dest="reset_quarantine",
        action="store_true",
        default=False,
        help="clear quarantine state before running",
    )
    mtn_sleep.add_argument(
        "--store-path",
        dest="store_path",
        default=None,
        help=(
            "IAI root directory (defaults to ~/.iai-mcp; Hippo data "
            "lives at <store-path>/hippo)"
        ),
    )
    mtn_sleep.set_defaults(func=cmd_maintenance_sleep_cycle)

    doc = sub.add_parser(
        "doctor",
        help=(
            "Diagnose daemon health (incl. (g) duplicate-binder detection). "
            "With --apply, attempt safe repairs "
            "(unlink stale socket, kill duplicate binders, cleanup orphans, "
            "respawn daemon). With --apply --yes, skip confirmations. "
            "Exit 0=all green, 1=any FAIL, 2=--apply tried but FAIL persists."
        ),
        description="""\
Check iai-mcp environment, store, and daemon health.

doctor validates the daemon socket and process state, detects duplicate socket
binders, checks service/log/state expectations, and reports store/environment
conditions that can prevent capture or consolidation from working correctly.
""",
        epilog="""\
Examples:
  iai-mcp doctor
  iai-mcp doctor --headless
  iai-mcp doctor --apply
  iai-mcp doctor --apply --yes

Side effects:
  Without --apply, doctor is read-only.
  --apply may mutate state by unlinking a stale socket, killing duplicate daemon
    binders, cleaning orphaned files, or respawning the daemon.
  --yes skips repair confirmations; use it only when the selected --apply repairs
    are acceptable in non-interactive runs.
  --headless changes diagnosis severity for display/HID checks but does not
    mutate state by itself.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    doc.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="attempt safe repairs after diagnosis; prompts before each destructive action",
    )
    doc.add_argument(
        "--yes", "-y",
        action="store_true",
        default=False,
        help="(use with --apply) skip confirmation prompts; equivalent to typing 'y' to all",
    )
    doc.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help=(
            "force headless mode (downgrade `(n) HID idle source` and "
            "`(b) socket file fresh` from FAIL to WARN). Auto-detected on "
            "Linux when DISPLAY/WAYLAND_DISPLAY are unset; on macOS use this "
            "flag explicitly."
        ),
    )
    def _cmd_doctor_lazy(args: argparse.Namespace) -> int:
        from iai_mcp.doctor import cmd_doctor
        return cmd_doctor(args)
    doc.set_defaults(func=_cmd_doctor_lazy)

    lc = sub.add_parser(
        "lifecycle",
        help=(
            "inspect lifecycle state machine "
            "(WAKE/DROWSY/SLEEP/HIBERNATION). Currently: status."
        ),
    )
    lc_sub = lc.add_subparsers(dest="lifecycle_cmd", required=True)
    lc_status = lc_sub.add_parser(
        "status",
        help=(
            "print current lifecycle state, since-ts, last activity, "
            "wrapper event seq, sleep-cycle progress, quarantine, and "
            "shadow_run flag"
        ),
    )
    lc_status.set_defaults(func=cmd_lifecycle_status)

    lc_unlock = lc_sub.add_parser(
        "force-unlock",
        help=(
            "clear a stale ~/.iai-mcp/.locked lockfile and "
            "print the prior PID / hostname / started_at"
        ),
    )
    lc_unlock.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive [y/N] prompt",
    )
    lc_unlock.set_defaults(func=cmd_lifecycle_force_unlock)

    br = sub.add_parser(
        "bank-recall",
        help=(
            "substring recall over bank/processed + bank/recent without "
            "booting the daemon. Used by the wrapper as a socket-dead "
            "fallback path."
        ),
    )
    br.add_argument("--query", required=True, help="cue substring to match")
    br.add_argument(
        "--limit", type=int, default=20, help="max hits (default 20)"
    )
    br.add_argument(
        "--processed-only", action="store_true", default=False
    )
    br.add_argument(
        "--recent-only", action="store_true", default=False
    )
    br.add_argument(
        "--json",
        action="store_true",
        default=True,
        help="emit JSON to stdout (current default; --no-json is reserved)",
    )
    br.set_defaults(func=cmd_bank_recall)

    dpf = sub.add_parser(
        "drain-permanent-failed",
        help=(
            "recover terminal .permanent-failed-*.jsonl files from "
            ".deferred-captures/. Routes through daemon socket when daemon "
            "is running; direct-open fallback when daemon is down. "
            "--dry-run lists files without mutating anything."
        ),
    )
    dpf.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="list terminal files + event counts without inserting or renaming",
    )
    dpf.set_defaults(func=cmd_drain_permanent_failed)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
