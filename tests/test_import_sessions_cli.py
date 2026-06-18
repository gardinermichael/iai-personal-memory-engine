from __future__ import annotations

import json
from pathlib import Path

from iai_mcp.cli import main
from iai_mcp.cli._import_sessions import discover_transcripts


def test_discover_transcripts_accepts_explicit_file_and_directory(tmp_path: Path):
    direct = tmp_path / "one.jsonl"
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    nested = nested_dir / "two.jsonl"
    ignored = nested_dir / "notes.txt"
    direct.write_text("{}\n")
    nested.write_text("{}\n")
    ignored.write_text("nope")

    found = discover_transcripts(target="codex", paths=[str(direct), str(nested_dir)])

    assert [(item.target, item.path) for item in found] == [
        ("codex", direct.resolve()),
        ("codex", nested.resolve()),
    ]


def test_import_sessions_scan_prints_candidates_without_importing(
    tmp_path: Path, monkeypatch, capsys
):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({"type": "user", "content": "hello there"}) + "\n")

    def fail_if_imported():  # pragma: no cover - should never be called
        raise AssertionError("dry-run scan must not open MemoryStore")

    monkeypatch.setattr(
        "iai_mcp.cli._import_sessions.MemoryStore", fail_if_imported, raising=False
    )

    assert (
        main(["import-sessions", "scan", "--target", "claude", "--path", str(transcript)])
        == 0
    )

    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[0]) == {
        "target": "claude",
        "path": str(transcript.resolve()),
    }
    assert json.loads(lines[1]) == {"files": 1, "dry_run": True}
