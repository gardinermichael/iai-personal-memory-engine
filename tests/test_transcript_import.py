import json

from iai_mcp.transcript_import import (
    parse_claude_code_jsonl,
    parse_codex_cli_jsonl,
    parse_generic_jsonl_transcript,
)


def _write_jsonl(path, rows):
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            if isinstance(row, str):
                fh.write(row + "\n")
            else:
                fh.write(json.dumps(row) + "\n")


def test_claude_code_adapter_normalizes_and_counts_skips(tmp_path):
    path = tmp_path / "claude.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "summary", "text": "ignore"},
            {"type": "user", "uuid": "u1", "timestamp": "2026-01-01T00:00:00Z", "session_id": "s1", "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            "{bad json",
            {"type": "assistant", "message": {"role": "assistant", "content": "hi there"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "x"}]}},
        ],
    )

    result = parse_claude_code_jsonl(path)

    assert [r.role for r in result.records] == ["user", "assistant"]
    assert result.records[0].text == "hello"
    assert result.records[0].source_uuid == "u1"
    assert result.records[0].session_id == "s1"
    assert result.records[0].source_host == "claude"
    assert result.records[0].source_transcript_path == str(path)
    assert result.records[0].line_number == 2
    assert result.stats.records == 2
    assert result.stats.skipped_malformed == 1
    assert result.stats.skipped_empty_text == 1
    assert result.stats.skipped_unsupported == 1


def test_codex_cli_adapter_handles_payload_items(tmp_path):
    path = tmp_path / "codex.jsonl"
    _write_jsonl(
        path,
        [
            {"type": "session_meta", "id": "not a turn"},
            {"timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "user_message", "message": "please edit", "id": "m1", "session_id": "cx"}},
            {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item", "payload": {"item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}], "id": "m2"}}},
            {"payload": {"type": "tool_call", "name": "shell"}},
        ],
    )

    result = parse_codex_cli_jsonl(path)

    assert [(r.role, r.text) for r in result.records] == [("user", "please edit"), ("assistant", "done")]
    assert result.records[0].session_id == "cx"
    assert result.records[0].source_uuid == "m1"
    assert result.records[1].source_uuid == "m2"
    assert result.records[1].source_host == "codex"
    assert result.stats.skipped_unsupported == 2


def test_generic_adapter_falls_back_to_role_text_fields(tmp_path):
    path = tmp_path / "generic.jsonl"
    _write_jsonl(path, [{"role": "assistant", "text": "generic text", "conversation_id": "g1"}])

    result = parse_generic_jsonl_transcript(path, source_host="other")

    assert len(result.records) == 1
    assert result.records[0].role == "assistant"
    assert result.records[0].text == "generic text"
    assert result.records[0].session_id == "g1"
    assert result.records[0].source_host == "other"
