"""Import historical Claude Code and Codex CLI transcripts."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TARGET_ROOTS: dict[str, Path] = {
    "claude": Path.home() / ".claude",
    "codex": Path.home() / ".codex",
}

_COUNT_KEYS = ("inserted", "reinforced", "skipped", "errors")


@dataclass(frozen=True)
class CandidateTranscript:
    path: Path
    target: str


def _targets(value: str) -> tuple[str, ...]:
    if value == "all":
        return ("claude", "codex")
    return (value,)


def _iter_jsonl_under(root: Path) -> Iterable[Path]:
    try:
        if root.is_file():
            if root.suffix == ".jsonl":
                yield root
            return
        if root.is_dir():
            yield from root.rglob("*.jsonl")
    except OSError:
        return


def _infer_target(path: Path, selected: tuple[str, ...]) -> str:
    expanded = path.expanduser()
    for target, root in _TARGET_ROOTS.items():
        try:
            expanded.relative_to(root.expanduser())
            return target
        except ValueError:
            continue
    if len(selected) == 1:
        return selected[0]
    return "explicit"


def discover_transcripts(
    *, target: str = "all", paths: list[str] | None = None
) -> list[CandidateTranscript]:
    """Return unique JSONL transcript candidates for selected targets and paths."""

    selected = _targets(target)
    seen: set[Path] = set()
    candidates: list[CandidateTranscript] = []

    def add(path: Path, source_target: str) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser().absolute()
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(CandidateTranscript(path=resolved, target=source_target))

    for selected_target in selected:
        for transcript in _iter_jsonl_under(_TARGET_ROOTS[selected_target].expanduser()):
            add(transcript, selected_target)

    for raw in paths or []:
        explicit = Path(raw).expanduser()
        explicit_target = _infer_target(explicit, selected)
        for transcript in _iter_jsonl_under(explicit):
            add(transcript, explicit_target)

    candidates.sort(key=lambda c: (c.target, str(c.path)))
    return candidates


def _zero_counts() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}


def _normalise_counts(counts: dict) -> dict[str, int]:
    normalised = _zero_counts()
    for key in _COUNT_KEYS:
        try:
            normalised[key] = int(counts.get(key, 0))
        except (TypeError, ValueError):
            normalised[key] = 0
    return normalised


def _session_id_for(candidate: CandidateTranscript) -> str:
    if candidate.target in {"claude", "codex"}:
        return f"{candidate.target}:{candidate.path.stem}"
    return candidate.path.stem


def _print_file_result(
    candidate: CandidateTranscript, counts: dict[str, int] | None = None
) -> None:
    payload = {
        "target": candidate.target,
        "path": str(candidate.path),
    }
    if counts is not None:
        payload.update({key: counts[key] for key in _COUNT_KEYS})
    print(json.dumps(payload, ensure_ascii=False))


def cmd_import_sessions(args: argparse.Namespace) -> int:
    """Scan for or import historical transcript JSONL files."""

    mode = getattr(args, "import_sessions_cmd", None) or "import"
    dry_run = bool(getattr(args, "dry_run", False) or mode == "scan")

    try:
        candidates = discover_transcripts(
            target=getattr(args, "target", "all"),
            paths=list(getattr(args, "path", None) or []),
        )
    except Exception as exc:  # noqa: BLE001 -- command-level failure boundary
        print(
            f"import-sessions: scan failed {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        for candidate in candidates:
            _print_file_result(candidate)
        print(
            json.dumps({"files": len(candidates), "dry_run": True}, ensure_ascii=False)
        )
        return 0

    try:
        from iai_mcp.capture import capture_transcript
        from iai_mcp.store import MemoryStore

        store = MemoryStore()
    except Exception as exc:  # noqa: BLE001 -- store open is a command-level failure
        print(
            "import-sessions: failed to open memory store "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    totals = _zero_counts()
    for candidate in candidates:
        try:
            raw_counts = capture_transcript(
                store,
                candidate.path,
                session_id=_session_id_for(candidate),
                max_turns=getattr(args, "max_turns", 100_000),
            )
            counts = _normalise_counts(raw_counts)
        except Exception as exc:  # noqa: BLE001 -- per-file failures should not fail command
            counts = _zero_counts()
            counts["errors"] = 1
            print(
                f"import-sessions: {candidate.path}: failed {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        for key in _COUNT_KEYS:
            totals[key] += counts[key]
        _print_file_result(candidate, counts)

    summary = {"files": len(candidates), **totals}
    print(json.dumps(summary, ensure_ascii=False))
    return 0
