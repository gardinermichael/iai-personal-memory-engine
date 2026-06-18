"""Hermetic regression tests for the turn-capture ceiling.

Verifies that a transcript with more than 200 turns is captured in full
and that re-running capture on the same transcript inserts no duplicates.
"""
from __future__ import annotations

import json
import platform
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="POSIX paths + UNIX socket semantics",
)

SESSION_ID = "sess-test"
_N_TURNS = 250  # deliberately above the old 200-turn cap


@pytest.fixture
def iai_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("IAI_MCP_CRYPTO_PASSPHRASE", "test-capture-ceiling-passphrase")
    monkeypatch.setenv("IAI_MCP_STORE", str(tmp_path / ".iai-mcp"))
    monkeypatch.setenv("IAI_MCP_PATSEP_DRY_RUN", "false")
    import keyring.core
    keyring.core._keyring_backend = None
    yield tmp_path
    keyring.core._keyring_backend = None


def _open_store():
    from iai_mcp.store import MemoryStore
    return MemoryStore()


def _make_transcript(path: Path, n_turns: int = _N_TURNS) -> Path:
    """Write a JSONL transcript with n_turns alternating user/assistant turns.

    Each turn gets a distinct UUID so the idem key uses source_uuid.
    """
    transcript_path = path / "transcript.jsonl"
    lines = []
    base_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(1, n_turns + 1):
        role = "user" if i % 2 == 1 else "assistant"
        ts = base_ts.replace(second=i % 60, minute=i // 60 % 60, hour=i // 3600 % 24)
        turn = {
            "type": role,
            "uuid": str(uuid.uuid4()),
            "timestamp": ts.isoformat(),
            "sessionId": SESSION_ID,
            "message": {
                "role": role,
                "content": f"Turn {i} — {role} text for ceiling test",
            },
        }
        lines.append(json.dumps(turn))
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript_path


def _count_episodic_records(store) -> int:
    """Return the number of active episodic records in the store."""
    with store.db._conn_lock:
        row = store.db._conn.execute(
            "SELECT COUNT(*) FROM records"
            " WHERE tombstoned_at IS NULL"
            " AND tier = 'episodic'"
        ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Task 0 tests — these MUST FAIL before the ceiling is raised (RED gate)
# ---------------------------------------------------------------------------

def test_capture_transcript_beyond_200(iai_home, tmp_path):
    """capture_transcript must store all 250 turns, not just the first 200."""
    from iai_mcp.capture import capture_transcript

    transcript = _make_transcript(tmp_path)
    store = _open_store()

    counts = capture_transcript(store, transcript, session_id=SESSION_ID)

    total_captured = counts["inserted"] + counts["reinforced"]
    assert total_captured == _N_TURNS, (
        f"Expected {_N_TURNS} turns captured; got {total_captured}. "
        f"counts={counts!r}. Turns 201+ are being silently dropped — "
        f"this violates the lossless verbatim-recall invariant."
    )

    # Spot-check verbatim: turn 250 (the last one) must be in the store.
    last_turn_text = f"Turn {_N_TURNS} — assistant text for ceiling test"
    db_count = _count_episodic_records(store)
    assert db_count >= _N_TURNS, (
        f"Store holds only {db_count} episodic records; expected at least {_N_TURNS}."
    )

    # Confirm literal_surface is verbatim for a turn past the old cap.
    all_records = store.all_records()
    late_records = [
        r for r in all_records
        if r.literal_surface and last_turn_text in r.literal_surface
    ]
    assert len(late_records) >= 1, (
        f"Turn {_N_TURNS} literal_surface not found in store. "
        f"literal_surface must be verbatim transcript text, never paraphrased."
    )


def test_deferred_capture_beyond_200(iai_home, tmp_path):
    """write_deferred_captures must write all 250 turns to the deferred file."""
    from iai_mcp.capture import write_deferred_captures

    transcript = _make_transcript(tmp_path)
    out_path = write_deferred_captures(
        session_id=SESSION_ID,
        transcript_path=transcript,
        cwd="/tmp/test",
    )

    assert out_path.exists(), f"Deferred capture file not created at {out_path}"
    lines = out_path.read_text(encoding="utf-8").splitlines()

    # First line is the header; the rest are turn events.
    events = [json.loads(ln) for ln in lines[1:] if ln.strip()]
    assert len(events) == _N_TURNS, (
        f"Expected {_N_TURNS} deferred events; got {len(events)}. "
        f"write_deferred_captures is truncating at the old 200-turn cap."
    )


def test_capture_idempotent_after_cap_raise(iai_home, tmp_path):
    """Re-running capture on the same transcript adds zero new records (SHA256 dedup)."""
    from iai_mcp.capture import capture_transcript

    transcript = _make_transcript(tmp_path)
    store = _open_store()

    # First pass — capture all turns.
    counts_first = capture_transcript(store, transcript, session_id=SESSION_ID)
    total_first = counts_first["inserted"] + counts_first["reinforced"]
    assert total_first == _N_TURNS, (
        f"First pass: expected {_N_TURNS} turns; got {total_first}. "
        f"counts={counts_first!r}"
    )

    count_after_first = _count_episodic_records(store)

    # Second pass — must add zero new records.
    counts_second = capture_transcript(store, transcript, session_id=SESSION_ID)
    count_after_second = _count_episodic_records(store)

    assert count_after_second == count_after_first, (
        f"Second capture pass inserted {count_after_second - count_after_first} "
        f"extra records; expected 0. "
        f"The SHA256 idem dedup must prevent duplicates on re-capture. "
        f"counts_second={counts_second!r}"
    )
    assert counts_second.get("reinforced", 0) == _N_TURNS, (
        f"Second pass must reinforce all {_N_TURNS} turns (not re-insert). "
        f"counts_second={counts_second!r}"
    )


def test_capture_transcript_no_uuid_uses_stable_line_provenance(iai_home, tmp_path):
    """No-uuid transcript lines must be idempotent via path/session/role/timestamp/line."""
    from iai_mcp.capture import capture_transcript

    transcript = tmp_path / "no_uuid.jsonl"
    rows = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "message": {
                "role": "user",
                "content": "No uuid user turn long enough for import idem",
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:01+00:00",
            "message": {
                "role": "assistant",
                "content": "No uuid assistant turn long enough for import idem",
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    store = _open_store()

    first = capture_transcript(store, transcript, session_id="no-uuid-session")
    after_first = _count_episodic_records(store)
    second = capture_transcript(store, transcript, session_id="no-uuid-session")
    after_second = _count_episodic_records(store)

    assert first["inserted"] == 2, first
    assert second["reinforced"] == 2, second
    assert after_second == after_first
    state_dir = tmp_path / ".iai-mcp" / ".import-state"
    assert state_dir.exists()
    state_files = list(state_dir.glob("*.json"))
    assert state_files, "capture_transcript must persist per-import state under store root"
    state = json.loads(state_files[0].read_text(encoding="utf-8"))
    assert len(state["turns"]) == 2
    assert all(key.startswith("jsonl-fallback:") for key in state["turns"])
