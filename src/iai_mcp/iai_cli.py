from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any

__version__ = "1.1.2"


_CYAN = "\x1b[96m"
_DIM = "\x1b[2m"
_RESET = "\x1b[0m"


def _color(text: str, *, color: str = _CYAN) -> str:
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"{color}{text}{_RESET}"


_LOGO_LINES: tuple[str, ...] = (
    "  ██╗ █████╗ ██╗     ██████╗██╗     ██╗",
    "  ██║██╔══██╗██║    ██╔════╝██║     ██║",
    "  ██║███████║██║    ██║     ██║     ██║",
    "  ██║██╔══██║██║    ██║     ██║     ██║",
    "  ██║██║  ██║██║    ╚██████╗███████╗██║",
    "  ╚═╝╚═╝  ╚═╝╚═╝     ╚═════╝╚══════╝╚═╝",
)


def _print_logo() -> None:
    for line in _LOGO_LINES:
        print(_color(line))
    print(_color("  iai-cli · terminal memory for your agent  ", color=_DIM))
    print()


def _format_hits(hits: list[dict], *, max_surface_chars: int = 200) -> str:
    lines: list[str] = []
    for h in hits:
        surface = (h.get("literal_surface") or h.get("surface") or "")[:max_surface_chars]
        score_raw = h.get("score") or h.get("final_score") or 0.0
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0
        lines.append(f"  {score:.3f}  {surface}")
    return "\n".join(lines)


def cmd_recall(args: argparse.Namespace) -> int:
    import json as _json

    from iai_mcp.cli import _send_jsonrpc_request

    cue = args.cue
    limit = max(1, int(args.limit))
    json_mode = bool(getattr(args, "json", False))

    _raw_rt = os.environ.get("IAI_RECALL_READ_TIMEOUT", "")
    try:
        _recall_read_timeout = max(0.5, float(_raw_rt))
    except (ValueError, TypeError):
        _recall_read_timeout = 2.0

    _raw_am = os.environ.get("IAI_RECALL_ASLEEP_MARGIN_SEC", "")
    try:
        _asleep_margin_sec = max(0.0, float(_raw_am))
    except (ValueError, TypeError):
        _asleep_margin_sec = 3.0

    _asleep = False
    try:
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _Path2
        from iai_mcp.lifecycle_state import load_state as _load_lc_state, LifecycleState as _LS

        _lc_env = os.environ.get("IAI_MCP_STORE")
        _lc_root = _Path2(_lc_env) if _lc_env else _Path2.home() / ".iai-mcp"
        _lc_path = _lc_root / "lifecycle_state.json"
        _lc_rec = _load_lc_state(_lc_path)
        _lc_state = _lc_rec.get("current_state")
        if _lc_state in (
            _LS.SLEEP.value,
            _LS.HIBERNATION.value,
        ):
            _since_raw = _lc_rec.get("since_ts", "")
            _since_dt = _dt.fromisoformat(_since_raw)
            _age_sec = (_dt.now(_tz.utc) - _since_dt).total_seconds()
            if _age_sec >= _asleep_margin_sec:
                _asleep = True
    except Exception:  # noqa: BLE001
        _asleep = False

    resp = None
    if not _asleep:
        resp = _send_jsonrpc_request(
            "memory_recall",
            {"cue": cue, "budget_tokens": limit * 300},
            read_timeout=_recall_read_timeout,
        )
    if isinstance(resp, dict) and "result" in resp and isinstance(resp["result"], dict):
        result = resp["result"]
        hits_raw = result.get("hits") or []
        if isinstance(hits_raw, list):
            hits = hits_raw[:limit]
            if json_mode:
                payload = {"hits": hits, "_source": "daemon", "count": len(hits)}
                print(_json.dumps(payload))
                return 0
            if not hits:
                print(_color("(no hits)"))
                return 0
            print(_color(f"via daemon  [N={len(hits)}]"))
            print(_format_hits(hits))
            return 0


    _store_root_direct: "str | None" = None
    _store_reached: bool = False
    try:
        from pathlib import Path as _Path
        from iai_mcp.semantic_recall import recall_semantic_warm as _recall_warm

        _store_env = os.environ.get("IAI_MCP_STORE")
        store_root = _Path(_store_env) if _store_env else _Path.home() / ".iai-mcp"
        _store_root_direct = str(store_root)

        _hippo_db = store_root / "hippo" / "brain.sqlite3"
        if not _hippo_db.exists():
            raise FileNotFoundError(f"store absent: {_hippo_db}")

        _store_reached = True
        degraded_hits = _recall_warm(store_root, cue, n=limit)

        if json_mode:
            _src = (degraded_hits[0].get("_source", "direct-store") if degraded_hits else "direct-store")
            payload = {
                "hits": degraded_hits,
                "_source": _src,
                "count": len(degraded_hits),
            }
            print(_json.dumps(payload))
            return 0
        if not degraded_hits:
            print(_color("(no hits)", color=_DIM))
            return 0
        print(_color("(daemon unreachable — store recall)", color=_DIM), file=sys.stderr)
        for h in degraded_hits:
            surface = (h.get("literal_surface") or "")[:200]
            score_raw = h.get("score") or 0.0
            try:
                score = float(score_raw)
            except (TypeError, ValueError):
                score = 0.0
            print(f"  {score:.3f}  {surface}")
        return 0
    except Exception:  # noqa: BLE001
        if _store_reached:
            pass

    print(_color("(daemon unreachable — bank-fallback)", color=_DIM), file=sys.stderr)
    completed = subprocess.run(  # noqa: S603 -- argv list, no shell
        ["iai-mcp", "bank-recall", "--query", cue, "--limit", str(limit)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        sys.stdout.write(completed.stdout)
        return 0
    sys.stderr.write(completed.stderr or "recall failed\n")
    return 1


def cmd_ask(args: argparse.Namespace) -> int:
    import json as _json

    from iai_mcp.cli import _send_jsonrpc_request

    question = args.question
    limit = max(1, int(args.limit))

    recall_resp = _send_jsonrpc_request(
        "memory_recall",
        {"cue": question, "budget_tokens": limit * 300},
    )
    hits: list[dict] = []
    if isinstance(recall_resp, dict) and isinstance(recall_resp.get("result"), dict):
        raw = recall_resp["result"].get("hits") or []
        if isinstance(raw, list):
            hits = raw[:limit]

    if not hits:
        print(
            "(no memories matched the question — daemon down, or empty store)",
            file=sys.stderr,
        )
        return 1

    memories = [
        {
            "id": str(h.get("id") or h.get("record_id") or "?"),
            "surface": (h.get("literal_surface") or h.get("surface") or "")[:500],
        }
        for h in hits
    ]
    prompt = (
        "Answer the question grounded in the memories. Cite by id. "
        "Reply in 1-3 sentences, plain text.\n\n"
        f"Question: {question}\n"
        f"Memories: {_json.dumps(memories)}"
    )

    from iai_mcp.claude_cli import invoke_claude_sync

    result = invoke_claude_sync(prompt, model="haiku", timeout_sec=60.0)
    if not result.get("ok"):
        reason = result.get("reason", "unknown")
        print(f"ask failed: {reason}", file=sys.stderr)
        return 1

    data = result.get("data") or {}
    answer = ""
    if isinstance(data, dict):
        answer = str(data.get("result") or data.get("text") or "").strip()

    if not answer:
        print("ask failed: empty answer from claude -p", file=sys.stderr)
        return 1

    print(answer)
    ids = ", ".join(m["id"] for m in memories)
    print(_color(f"\nSources: {ids}", color=_DIM))
    return 0


def cmd_status(args: argparse.Namespace) -> int:  # noqa: ARG001 -- argparse contract
    from iai_mcp.claude_cli import verify_credentials_subscription
    from iai_mcp.cli import _send_jsonrpc_request

    daemon_state = "DOWN"
    record_count = "?"
    regime = "?"
    resp = _send_jsonrpc_request("topology", {})
    if isinstance(resp, dict):
        result = resp.get("result")
        if isinstance(result, dict):
            daemon_state = "UP"
            n = result.get("N")
            record_count = str(n) if n is not None else "?"
            regime = str(result.get("regime") or "?")

    creds = verify_credentials_subscription()
    if creds.get("ok"):
        sub_label = creds.get("subscription_type") or creds.get("billing_type") or "active"
    else:
        sub_label = f"missing ({creds.get('reason', 'unknown')})"

    print(_color("iai status"))
    print(f"  daemon         {daemon_state}")
    print(f"  records        {record_count}")
    print(f"  regime         {regime}")
    print(f"  subscription   {sub_label}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from iai_mcp.cli import _send_jsonrpc_request

    text = args.text
    session_id = getattr(args, "session_id", None) or "-"
    use_json = getattr(args, "json", False)

    resp = _send_jsonrpc_request(
        "memory_capture",
        {
            "text": text,
            "session_id": session_id,
            "tier": "episodic",
        },
    )
    if not isinstance(resp, dict):
        store_root_env = os.environ.get("IAI_MCP_STORE")
        if store_root_env:
            try:
                from pathlib import Path as _Path
                from iai_mcp.direct_write import write_turn_direct
                store_root = _Path(store_root_env)
                result = write_turn_direct(
                    store_root=store_root,
                    text=text,
                    session_id=session_id,
                    role="user",
                    deferred_embedding=True,
                )
                rid = result.get("record_id") or "?"
                if use_json:
                    import json as _json
                    print(_json.dumps({"id": rid, "status": result.get("status"), "_source": "direct-store"}))
                else:
                    print(_color(f"captured  id={rid}  [direct-store]"))
                return 0
            except Exception as exc:
                print(f"capture failed (direct write): {exc}", file=sys.stderr)
                return 1
        print(
            "capture failed: daemon unreachable. "
            "Start the daemon with `iai-mcp daemon start`.",
            file=sys.stderr,
        )
        return 1
    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", "unknown") if isinstance(err, dict) else str(err)
        print(f"capture failed: {msg}", file=sys.stderr)
        return 1
    if "result" in resp:
        result = resp["result"]
        rid: Any = "?"
        if isinstance(result, dict):
            rid = result.get("id") or result.get("record_id") or "?"
        if use_json:
            import json as _json
            print(_json.dumps({"id": rid, "status": "inserted", "_source": "daemon"}))
        else:
            print(_color(f"captured  id={rid}"))
        return 0
    print("capture failed: malformed daemon reply", file=sys.stderr)
    return 1


def _resolve_store_root():
    from pathlib import Path

    env = os.environ.get("IAI_MCP_STORE")
    return Path(env) if env else Path.home() / ".iai-mcp"


def cmd_last(args: argparse.Namespace) -> int:
    import json as _json

    from iai_mcp.direct_recency import read_recent_user_turns_direct

    n = max(0, int(args.n))
    session_id = args.session or None
    emit_json = getattr(args, "json", False)

    store_root = _resolve_store_root()

    turns_direct = read_recent_user_turns_direct(store_root, n=n, session_id=session_id)

    from iai_mcp.capture import read_pending_live_events

    pending = read_pending_live_events(session_id=session_id)

    if turns_direct or pending:
        from iai_mcp.capture import _idem_tag as _cap_idem_tag

        store_idem_set: set[str] = set()
        for r in turns_direct:
            for tag in (r.tags or []):
                if tag.startswith("idem:"):
                    store_idem_set.add(tag)

        seen_pending: set[str] = set()
        pending_user = []
        for ev in pending:
            if ev.get("role") != "user":
                continue
            ev_session = ev.get("session_id", "-")
            if session_id and ev_session != session_id:
                continue
            src_uuid = ev.get("source_uuid")
            ts_iso = ev.get("ts_iso", "")
            text = ev.get("text", "")
            idem = _cap_idem_tag(ev_session, "user", ts_iso, text, source_uuid=src_uuid)
            if idem in store_idem_set or idem in seen_pending:
                continue
            seen_pending.add(idem)
            pending_user.append(ev)

        turn_dicts = []
        for r in turns_direct:
            turn_dicts.append({
                "record_id": str(r.id),
                "literal_surface": r.literal_surface,
                "session_id": (r.provenance or [{}])[0].get("session_id"),
                "captured_at": r.created_at.isoformat() if r.created_at else None,
            })
        for ev in pending_user:
            src_uuid = ev.get("source_uuid")
            idem = _cap_idem_tag(
                ev.get("session_id", "-"),
                "user",
                ev.get("ts_iso", ""),
                ev.get("text", ""),
                source_uuid=src_uuid,
            )
            idem_hex = idem[5:] if idem.startswith("idem:") else idem
            rid = f"pending:{src_uuid}" if src_uuid else (f"pending:{idem_hex}" if idem_hex else "pending:unknown")
            turn_dicts.append({
                "record_id": rid,
                "literal_surface": ev.get("text", ""),
                "session_id": ev.get("session_id"),
                "captured_at": ev.get("ts_iso"),
            })

        from datetime import datetime, timezone

        def _ts_key(d: dict):
            raw = d.get("captured_at")
            if not raw:
                return datetime.min.replace(tzinfo=timezone.utc)
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return datetime.min.replace(tzinfo=timezone.utc)

        turn_dicts.sort(key=_ts_key, reverse=True)
        turn_dicts = turn_dicts[:n]

        if emit_json:
            print(_json.dumps({"turns": turn_dicts, "count": len(turn_dicts), "_source": "direct-store"}))
            return 0

        if not turn_dicts:
            print(_color("(no user turns found)"))
            return 0
        for t in turn_dicts:
            captured = (t.get("captured_at") or "?")[:19]
            sid = (t.get("session_id") or "?")[:8]
            surface = (t.get("literal_surface") or "")[:120]
            print(_color(f"[{captured}] {sid}: {surface}"))
        return 0

    from iai_mcp.cli import _send_jsonrpc_request

    resp = _send_jsonrpc_request(
        "episodes_recent",
        {"n": n, "session_id": session_id},
    )
    if isinstance(resp, dict) and "result" in resp and isinstance(resp["result"], dict):
        result = resp["result"]
        sock_turns = result.get("turns") or []
        if emit_json:
            print(_json.dumps({"turns": sock_turns, "count": len(sock_turns)}))
            return 0
        if not sock_turns:
            print(_color("(no user turns found)"))
            return 0
        for t in sock_turns:
            captured = (t.get("captured_at") or "?")[:19]
            sid = (t.get("session_id") or "?")[:8]
            surface = (t.get("literal_surface") or "")[:120]
            print(_color(f"[{captured}] {sid}: {surface}"))
        return 0

    if emit_json:
        print(_json.dumps({"turns": [], "count": 0, "_source": "direct-store"}))
        return 0
    print(_color("(no user turns found)"))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iai",
        description=(
            "Terminal memory for your agent — recall, capture, ask, status. "
            "Use `iai` for day-to-day memory reads/writes; use `iai-mcp` for "
            "operator workflows such as daemon control, hook installation, "
            "crypto, doctor, and maintenance."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  [read-only] iai recall "project decisions"
  [writes memory] iai capture "Remember this design decision"
  Operator/admin equivalent: iai-mcp daemon status
""",
        add_help=True,
    )
    parser.add_argument("--version", action="version", version=f"iai {__version__}")

    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p_recall = sub.add_parser(
        "recall",
        help="Recall memories by natural-language cue",
        description=(
            "[read-only] Recall memories. Uses the daemon when alive; falls back "
            "to the offline bank scan when daemon is down.\n\n"
            "Examples:\n  iai recall \"project decisions\"\n  iai recall \"api keys\" --limit 3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_recall.add_argument("cue", help="Natural-language query")
    p_recall.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum hits to print (default 5)",
    )
    p_recall.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Print result as a JSON payload for programmatic use (MCP wrapper)",
    )
    p_recall.set_defaults(func=cmd_recall)

    p_capture = sub.add_parser(
        "capture",
        help="Capture one episodic memory",
        description=(
            "[writes memory] Write one episodic record to the store via the daemon.\n\n"
            "Examples:\n  iai capture \"Remember the deployment checklist\"\n"
            "  iai capture \"Follow up with Sam\" --session-id SESSION"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_capture.add_argument("text", help="Memory text to store")
    p_capture.add_argument(
        "--session-id",
        default=None,
        help="Session identifier (default '-')",
    )
    p_capture.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit result as JSON on stdout (for programmatic use)",
    )
    p_capture.set_defaults(func=cmd_capture)

    p_ask = sub.add_parser(
        "ask",
        help="Recall + LLM synthesis grounded in memories",
        description="Recall the top-K memories matching the question, then "
        "synthesize an answer via `claude -p` (subscription-billed). "
        "Prints the answer + a Sources: footer with the cited record ids.",
    )
    p_ask.add_argument("question", help="Natural-language question")
    p_ask.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max memories to ground the answer (default 5)",
    )
    p_ask.set_defaults(func=cmd_ask)

    p_status = sub.add_parser(
        "status",
        help="Short health summary (daemon + records + subscription)",
        description="User-tier health summary. For the 17-row operator "
        "checklist run `iai-mcp doctor` instead.",
    )
    p_status.set_defaults(func=cmd_status)

    p_last = sub.add_parser(
        "last",
        help="Show the most-recent user-turn records, time-descending",
        description="Return the N most-recent role:user turns from the store. "
        "Optionally filter to a single session with --session.",
    )
    p_last.add_argument(
        "--n",
        type=int,
        default=5,
        help="Number of turns to return (default 5)",
    )
    p_last.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help="Filter to a specific session UUID",
    )
    p_last.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit turns as a JSON object on stdout (for programmatic use)",
    )
    p_last.set_defaults(func=cmd_last)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd is None:
        _print_logo()
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
