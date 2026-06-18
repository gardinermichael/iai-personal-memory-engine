from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    store = tmp_path / "store"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("IAI_MCP_STORE", str(store))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("IAI_MCP_CRYPTO_PASSPHRASE", "test-import-sessions-passphrase")
    import keyring.core

    keyring.core._keyring_backend = None

    class _FakeEmbedder:
        def embed(self, _text: str) -> list[float]:
            return [0.0] * 384

    monkeypatch.setattr(
        "iai_mcp.embed.embedder_for_store",
        lambda _store: _FakeEmbedder(),
    )
    yield home, store
    keyring.core._keyring_backend = None


def _write_jsonl(path: Path, rows: list[object | str]) -> Path:
    path.write_text(
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _store_count() -> int:
    from iai_mcp.store import MemoryStore

    return len(MemoryStore().all_records())


def _capture(path: Path, **kwargs):
    from iai_mcp.capture import capture_transcript
    from iai_mcp.store import MemoryStore

    return capture_transcript(MemoryStore(), path, session_id="sess-import-test", **kwargs)


def test_imports_claude_style_user_and_assistant_turns(isolated_home, tmp_path):
    transcript = _write_jsonl(
        tmp_path / "claude.jsonl",
        [
            {
                "type": "user",
                "uuid": "claude-user-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Claude user turn fixture text."},
            },
            {
                "type": "assistant",
                "uuid": "claude-assistant-1",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Claude assistant turn fixture text."}],
                },
            },
        ],
    )

    counts = _capture(transcript)

    assert counts["inserted"] == 2
    assert counts["errors"] == 0
    assert _store_count() == 2


def test_imports_codex_style_user_and_assistant_turns(isolated_home, tmp_path):
    transcript = _write_jsonl(
        tmp_path / "codex.jsonl",
        [
            {"role": "user", "content": "Codex user turn fixture text.", "uuid": "codex-user-1"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Codex assistant turn fixture text."}],
                "uuid": "codex-assistant-1",
            },
        ],
    )

    counts = _capture(transcript)

    assert counts["inserted"] == 2
    assert counts["errors"] == 0
    assert _store_count() == 2


def test_malformed_jsonl_lines_are_reported_without_real_home_reads(isolated_home, tmp_path):
    home, _store = isolated_home
    transcript = _write_jsonl(
        tmp_path / "malformed.jsonl",
        [
            "{not-json",
            {"type": "user", "message": {"role": "user", "content": "Valid turn after malformed input."}},
        ],
    )

    counts = _capture(transcript)

    assert counts["errors"] == 1
    assert counts["inserted"] == 1
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()


def test_non_message_events_are_skipped(isolated_home, tmp_path):
    transcript = _write_jsonl(
        tmp_path / "events.jsonl",
        [
            {"type": "system", "message": {"role": "system", "content": "skip this event"}},
            {"event": "tool_call", "payload": {"content": "skip this too"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Assistant message survives skip."}},
        ],
    )

    counts = _capture(transcript)

    assert counts["inserted"] == 1
    assert counts["errors"] == 0
    assert _store_count() == 1


def test_duplicate_imports_are_idempotent(isolated_home, tmp_path):
    transcript = _write_jsonl(
        tmp_path / "dupes.jsonl",
        [
            {"type": "user", "uuid": "dupe-user-1", "message": {"role": "user", "content": "Duplicate import user fixture text."}},
            {"type": "assistant", "uuid": "dupe-assistant-1", "message": {"role": "assistant", "content": "Duplicate import assistant fixture text."}},
        ],
    )

    first = _capture(transcript)
    count_after_first = _store_count()
    second = _capture(transcript)

    assert first["inserted"] == 2
    assert count_after_first == 2
    assert second["inserted"] == 0
    assert second["reinforced"] == 2
    assert _store_count() == count_after_first


def test_dry_run_counts_without_writing(isolated_home, tmp_path):
    transcript = _write_jsonl(
        tmp_path / "dry-run.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "Dry run user fixture text."}},
            {"type": "assistant", "message": {"role": "assistant", "content": "Dry run assistant fixture text."}},
        ],
    )

    counts = _capture(transcript, dry_run=True)

    assert counts["inserted"] == 0
    assert counts["reinforced"] == 0
    assert counts["skipped"] == 2
    assert _store_count() == 0


def test_max_turns_cap_behavior_and_cap_warning(isolated_home, tmp_path):
    _home, store = isolated_home
    transcript = _write_jsonl(
        tmp_path / "capped.jsonl",
        [
            {"type": "user", "uuid": "cap-1", "message": {"role": "user", "content": "First capped fixture text."}},
            {"type": "assistant", "uuid": "cap-2", "message": {"role": "assistant", "content": "Second capped fixture text."}},
            {"type": "user", "uuid": "cap-3", "message": {"role": "user", "content": "Third capped fixture text."}},
        ],
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "iai_mcp.cli",
            "capture-transcript",
            "--dry-run",
            "--session-id",
            "cap",
            "--max-turns",
            "2",
            str(transcript),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["inserted"] == 0
    assert payload["skipped"] == 2
    assert payload["capped"] is True
    assert payload["cap"] == 2
    assert "warning: max-turns cap reached (2)" in proc.stderr
    assert store.exists()


def test_explicit_iai_mcp_store_uses_temporary_store_path(tmp_path):
    home = tmp_path / "home"
    explicit_store = tmp_path / "explicit-store"
    home.mkdir()
    transcript = _write_jsonl(
        tmp_path / "explicit-store.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "Explicit temporary store fixture text."}}],
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "IAI_MCP_STORE": str(explicit_store),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            "IAI_MCP_CRYPTO_PASSPHRASE": "test-explicit-store-passphrase",
            "PYTHONPATH": str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "iai_mcp.cli",
            "capture-transcript",
            "--dry-run",
            "--session-id",
            "explicit",
            str(transcript),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["inserted"] == 0
    assert payload["skipped"] == 1
    assert explicit_store.exists()
    assert not (home / ".iai-mcp").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()
