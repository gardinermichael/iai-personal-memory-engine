"""Adapters for importing chat transcript JSONL files.

The adapters in this module are intentionally tolerant: malformed JSON lines and
unsupported event types are counted and skipped rather than raising and aborting a
whole transcript import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable, Literal

TranscriptRole = Literal["user", "assistant"]


@dataclass(frozen=True)
class NormalizedTranscriptRecord:
    """Shared internal shape for a transcript turn."""

    role: TranscriptRole
    text: str
    timestamp: str | None
    source_uuid: str | None
    session_id: str
    source_host: str
    source_transcript_path: str
    line_number: int


@dataclass
class TranscriptAdapterStats:
    """Counters collected while adapting a transcript."""

    records: int = 0
    skipped_unsupported: int = 0
    skipped_malformed: int = 0
    skipped_empty_text: int = 0
    skipped_bad_role: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        if reason == "malformed_json":
            self.skipped_malformed += 1
        elif reason == "empty_text":
            self.skipped_empty_text += 1
        elif reason == "bad_role":
            self.skipped_bad_role += 1
        else:
            self.skipped_unsupported += 1


@dataclass(frozen=True)
class TranscriptAdapterResult:
    records: list[NormalizedTranscriptRecord]
    stats: TranscriptAdapterStats


def parse_claude_code_jsonl(
    transcript_path: Path | str,
    *,
    session_id: str | None = None,
    max_turns: int = 100_000,
) -> TranscriptAdapterResult:
    """Normalize Claude Code JSONL transcript records.

    Claude Code stores user/assistant turns either at the top level or under a
    ``message`` object, with text usually in ``message.content``.
    """

    return _parse_jsonl(
        transcript_path,
        session_id=session_id,
        source_host="claude",
        max_turns=max_turns,
        normalizer=_normalize_claude_code_obj,
    )


def parse_codex_cli_jsonl(
    transcript_path: Path | str,
    *,
    session_id: str | None = None,
    max_turns: int = 100_000,
) -> TranscriptAdapterResult:
    """Normalize Codex CLI JSONL transcript records.

    Codex session streams have had a few shapes over time. This adapter accepts
    common ``payload``/``item``/top-level message records and skips tool or
    internal events.
    """

    return _parse_jsonl(
        transcript_path,
        session_id=session_id,
        source_host="codex",
        max_turns=max_turns,
        normalizer=_normalize_codex_cli_obj,
    )


def parse_generic_jsonl_transcript(
    transcript_path: Path | str,
    *,
    session_id: str | None = None,
    source_host: str = "generic",
    max_turns: int = 100_000,
) -> TranscriptAdapterResult:
    """Best-effort parser for JSONL chat transcripts with role/text fields."""

    return _parse_jsonl(
        transcript_path,
        session_id=session_id,
        source_host=source_host,
        max_turns=max_turns,
        normalizer=_normalize_generic_obj,
    )


def _parse_jsonl(
    transcript_path: Path | str,
    *,
    session_id: str | None,
    source_host: str,
    max_turns: int,
    normalizer: Any,
) -> TranscriptAdapterResult:
    path = Path(transcript_path).expanduser()
    stats = TranscriptAdapterStats()
    records: list[NormalizedTranscriptRecord] = []
    with path.open(encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if len(records) >= max_turns:
                break
            if not line.strip():
                stats.skip("blank_line")
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                stats.skip("malformed_json")
                continue
            parsed = normalizer(obj)
            if parsed is None:
                stats.skip("unsupported_event")
                continue
            role, text, timestamp, source_uuid, obj_session_id = parsed
            if role not in {"user", "assistant"}:
                stats.skip("bad_role")
                continue
            text = text.strip()
            if not text:
                stats.skip("empty_text")
                continue
            sid = session_id or obj_session_id or _session_id_from_path(path)
            records.append(
                NormalizedTranscriptRecord(
                    role=role,  # type: ignore[arg-type]
                    text=text,
                    timestamp=timestamp,
                    source_uuid=source_uuid,
                    session_id=sid,
                    source_host=source_host,
                    source_transcript_path=str(path),
                    line_number=line_number,
                )
            )
            stats.records += 1
    return TranscriptAdapterResult(records=records, stats=stats)


def _normalize_claude_code_obj(
    obj: dict[str, Any],
) -> tuple[str, str, str | None, str | None, str | None] | None:
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = obj.get("type") or msg.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _content_to_text(msg.get("content", obj.get("content", "")))
    return _tuple_or_none(role, text, obj)


def _normalize_codex_cli_obj(
    obj: dict[str, Any],
) -> tuple[str, str, str | None, str | None, str | None] | None:
    candidate = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    if isinstance(candidate.get("item"), dict):
        candidate = candidate["item"]

    event_type = str(candidate.get("type") or obj.get("type") or "")
    role = candidate.get("role") or obj.get("role")
    if role is None and event_type in {"user_message", "user"}:
        role = "user"
    elif role is None and event_type in {"assistant_message", "assistant"}:
        role = "assistant"

    if role not in {"user", "assistant"}:
        return None

    text = _content_to_text(
        candidate.get("content")
        or candidate.get("message")
        or candidate.get("text")
        or obj.get("message")
        or obj.get("text")
        or ""
    )
    return _tuple_or_none(role, text, {**obj, **candidate})


def _normalize_generic_obj(
    obj: dict[str, Any],
) -> tuple[str, str, str | None, str | None, str | None] | None:
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role") or obj.get("role") or obj.get("type")
    text = _content_to_text(
        msg.get("text") or msg.get("content") or obj.get("text") or obj.get("content") or ""
    )
    return _tuple_or_none(role, text, obj)


def _tuple_or_none(
    role: Any, text: str, obj: dict[str, Any]
) -> tuple[str, str, str | None, str | None, str | None] | None:
    if role is None:
        return None
    return (
        str(role),
        text,
        _first_str(obj, "timestamp", "ts", "created_at", "time"),
        _first_str(obj, "uuid", "id", "source_uuid", "message_id"),
        _first_str(obj, "session_id", "sessionId", "conversation_id"),
    )


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return _content_to_text(content.get("text") or content.get("content") or "")
    if isinstance(content, Iterable):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in {None, "text", "input_text", "output_text"}:
                    text = block.get("text") or block.get("content") or ""
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(p for p in parts if p).strip()
    return str(content) if content is not None else ""


def _first_str(obj: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _session_id_from_path(path: Path) -> str:
    return path.stem or "-"
