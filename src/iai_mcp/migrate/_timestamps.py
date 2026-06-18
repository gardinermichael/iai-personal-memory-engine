"""Transcript timestamp re-derivation migration.

Re-derives per-turn ``created_at`` values from on-disk session transcripts
for records whose timestamps collapsed to a single shared value.
"""
from __future__ import annotations

import fnmatch
import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path

from iai_mcp.events import write_event
from iai_mcp.store import (
    MemoryStore,
)

from iai_mcp.migrate import _progress_read, _progress_write, _progress_clear


log = logging.getLogger(__name__)


def _find_transcript_ts(
    session_id: str,
    source_uuid: str | None,
    literal_surface: str,
    transcript_root: Path,
    *,
    transcript_paths: "list[Path] | None" = None,
    since: "datetime | None" = None,
    before: "datetime | None" = None,
) -> "datetime | None":
    """Return the parsed transcript timestamp for a record, or None if unresolvable.

    Scans all JSONL files under transcript_root matching */<session_id>.jsonl.
    Fast path: match by source_uuid. Fallback: match by content hash of literal_surface
    against the transcript line text field.
    """
    from iai_mcp.capture import _resolve_ts

    # Validate session_id to prevent path traversal.
    if not session_id or "/" in session_id or ".." in session_id:
        return None

    if transcript_paths is None:
        pattern = f"*/{session_id}.jsonl"
        matches = list(transcript_root.glob(pattern))
    else:
        matches = list(transcript_paths)
    if not matches:
        return None

    import hashlib

    surface_hash = hashlib.sha256(literal_surface.encode("utf-8")).hexdigest()

    for transcript_path in matches:
        try:
            with transcript_path.open("r", encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = obj.get("timestamp")
                    if not ts_str:
                        continue
                    parsed_ts = _resolve_ts(ts_str)
                    if parsed_ts is None:
                        continue
                    if since is not None and parsed_ts < since:
                        continue
                    if before is not None and parsed_ts >= before:
                        continue
                    # Fast path: uuid match.
                    if source_uuid and obj.get("uuid") == source_uuid:
                        return parsed_ts
                    # Content-hash fallback: compare against message text fields.
                    text_candidate = (
                        obj.get("text")
                        or obj.get("content")
                        or ""
                    )
                    if isinstance(text_candidate, list):
                        # Content is sometimes a list of blocks.
                        parts = []
                        for block in text_candidate:
                            if isinstance(block, dict):
                                parts.append(block.get("text") or "")
                            elif isinstance(block, str):
                                parts.append(block)
                        text_candidate = "".join(parts)
                    if text_candidate:
                        candidate_hash = hashlib.sha256(
                            text_candidate.encode("utf-8")
                        ).hexdigest()
                        if candidate_hash == surface_hash:
                            return parsed_ts
        except (OSError, UnicodeDecodeError):
            continue

    return None


def _parse_yyyy_mm_dd(value: str | None, *, option: str) -> "datetime | None":
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{option} must be in YYYY-MM-DD format") from exc
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def _path_matches_any(path: Path, patterns: list[str]) -> bool:
    text = path.as_posix()
    name = path.name
    return any(
        fnmatch.fnmatch(text, pat) or fnmatch.fnmatch(name, pat)
        for pat in patterns
    )


def _project_name_from_transcript_path(path: Path, transcript_root: Path) -> str | None:
    try:
        rel = path.relative_to(transcript_root)
    except ValueError:
        return path.parent.name or None
    return rel.parts[0] if len(rel.parts) >= 2 else None


def _filter_transcript_paths(
    paths: list[Path],
    transcript_root: Path,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    exclude_project: list[str] | None = None,
) -> list[Path]:
    include = include or []
    exclude = exclude or []
    exclude_project = exclude_project or []
    out: list[Path] = []
    project_filters = {item.rstrip("/") for item in exclude_project}
    for path in paths:
        if include and not _path_matches_any(path, include):
            continue
        if exclude and _path_matches_any(path, exclude):
            continue
        project_name = _project_name_from_transcript_path(path, transcript_root)
        if project_filters:
            path_text = path.as_posix().rstrip("/")
            parent_text = path.parent.as_posix().rstrip("/")
            if (
                (project_name and project_name in project_filters)
                or path_text in project_filters
                or parent_text in project_filters
            ):
                continue
        out.append(path)
    return out


def migrate_rederive_collapsed_timestamps(
    store: "MemoryStore",
    *,
    dry_run: bool = False,
    transcript_root: "Path | None" = None,
    since: str | None = None,
    before: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    exclude_project: list[str] | None = None,
) -> dict:
    """Re-derive per-turn created_at from on-disk transcripts for records
    whose timestamps collapsed to a single shared value.

    Returns a dict with keys: records_updated, skipped_no_transcript,
    skipped_no_match, dry_run.

    Safe to call multiple times (idempotent). Records whose transcripts are
    absent or unmatched are never modified.  Only created_at is updated —
    literal_surface and provenance_json are never touched.
    """
    from iai_mcp.hippo import HippoDB

    since_dt = _parse_yyyy_mm_dd(since, option="--since")
    before_dt = _parse_yyyy_mm_dd(before, option="--before")
    if since_dt is not None and before_dt is not None and since_dt >= before_dt:
        raise ValueError("--since must be earlier than --before")

    if transcript_root is None:
        transcript_root = Path.home() / ".claude" / "projects"
    else:
        transcript_root = Path(transcript_root)

    db = store.db
    if not isinstance(db, HippoDB):
        return {
            "records_updated": 0,
            "skipped_no_transcript": 0,
            "skipped_no_match": 0,
            "dry_run": dry_run,
        }

    # Load collapsed-group candidates: episodic records sharing created_at
    # with at least 2 other records (group size >= 3).
    with db._conn_lock:
        rows = db._conn.execute(
            "SELECT id, created_at FROM records"
            " WHERE tier = 'episodic'"
            "   AND tombstoned_at IS NULL"
            " GROUP BY created_at"
            " HAVING COUNT(*) >= 3"
        ).fetchall()

    if not rows:
        return {
            "records_updated": 0,
            "skipped_no_transcript": 0,
            "skipped_no_match": 0,
            "dry_run": dry_run,
        }

    # Collect all record IDs in collapsed groups.
    candidate_created_ats = {row[1] for row in rows}
    with db._conn_lock:
        all_candidates = db._conn.execute(
            "SELECT id FROM records"
            " WHERE tier = 'episodic'"
            "   AND tombstoned_at IS NULL"
            "   AND created_at IN ({})".format(
                ",".join("?" * len(candidate_created_ats))
            ),
            list(candidate_created_ats),
        ).fetchall()

    record_ids = [row[0] for row in (all_candidates or [])]

    progress = _progress_read(store)
    done_ids: set[str] = set(progress.get("done_ids", []))

    records_updated = 0
    skipped_no_transcript = 0
    skipped_no_match = 0

    for rec_id_str in record_ids:
        if rec_id_str in done_ids:
            continue

        try:
            from uuid import UUID
            rec = store.get(UUID(rec_id_str))
        except (ValueError, Exception):
            skipped_no_match += 1
            continue

        if rec is None:
            skipped_no_match += 1
            continue

        prov = (rec.provenance or [{}])[0]
        session_id = prov.get("session_id") or ""
        source_uuid = prov.get("source_uuid") or None

        if not session_id:
            skipped_no_transcript += 1
            done_ids.add(rec_id_str)
            continue

        # Check whether any transcript file exists for this session.
        if not session_id or "/" in session_id or ".." in session_id:
            skipped_no_transcript += 1
            done_ids.add(rec_id_str)
            continue

        transcript_matches = _filter_transcript_paths(
            list(transcript_root.glob(f"*/{session_id}.jsonl")),
            transcript_root,
            include=include,
            exclude=exclude,
            exclude_project=exclude_project,
        )
        if not transcript_matches:
            skipped_no_transcript += 1
            done_ids.add(rec_id_str)
            continue

        ts = _find_transcript_ts(
            session_id=session_id,
            source_uuid=source_uuid,
            literal_surface=rec.literal_surface,
            transcript_root=transcript_root,
            transcript_paths=transcript_matches,
            since=since_dt,
            before=before_dt,
        )

        if ts is None:
            skipped_no_match += 1
            done_ids.add(rec_id_str)
            continue

        if not dry_run:
            with db._conn_lock:
                db._conn.execute(
                    "UPDATE records SET created_at = ? WHERE id = ?",
                    (ts, rec_id_str),
                )
            records_updated += 1
        else:
            records_updated += 1

        done_ids.add(rec_id_str)

        if not dry_run:
            _progress_write(
                store,
                {"done_ids": list(done_ids)},
            )

    if not dry_run:
        try:
            write_event(
                store,
                "migration_rederive_timestamps",
                {
                    "records_updated": records_updated,
                    "skipped_no_transcript": skipped_no_transcript,
                    "skipped_no_match": skipped_no_match,
                },
            )
        except (OSError, ValueError, RuntimeError) as exc:
            log.error("migration_rederive_timestamps event write failed: %s", exc)

        _progress_clear(store)

    return {
        "records_updated": records_updated,
        "skipped_no_transcript": skipped_no_transcript,
        "skipped_no_match": skipped_no_match,
        "dry_run": dry_run,
    }
