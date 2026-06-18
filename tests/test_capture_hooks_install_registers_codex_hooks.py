from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        "iai_mcp.cli._claude_desktop_config_path",
        lambda: None,
        raising=True,
    )
    return home


def _install() -> int:
    from iai_mcp import cli as cli_mod

    return cli_mod.cmd_capture_hooks_install(argparse.Namespace())


def _uninstall() -> int:
    from iai_mcp import cli as cli_mod

    return cli_mod.cmd_capture_hooks_uninstall(argparse.Namespace())


def _codex_hooks(home: Path) -> dict:
    return json.loads((home / ".codex" / "hooks.json").read_text())


def _matching_entries(data: dict, event: str, marker: str) -> list[dict]:
    return [
        entry
        for entry in data.get("hooks", {}).get(event, [])
        if any(
            marker in (hook.get("command") or "") for hook in entry.get("hooks") or []
        )
    ]


def test_install_adds_codex_hooks_json_shape(fake_home):
    assert _install() == 0

    data = _codex_hooks(fake_home)
    assert set(data.get("hooks", {})) >= {"SessionStart", "UserPromptSubmit", "Stop"}

    session_entries = _matching_entries(
        data, "SessionStart", "iai-mcp-session-recall.sh"
    )
    assert len(session_entries) == 1
    assert session_entries[0].get("matcher") == "startup|resume|clear|compact"
    session_hook = session_entries[0]["hooks"][0]
    assert (
        session_hook["command"]
        == f"bash {fake_home}/.codex/hooks/iai-mcp-session-recall.sh"
    )
    assert session_hook["timeout"] == 30
    assert session_hook["statusMessage"] == "Loading iai memory"

    turn_entries = _matching_entries(
        data, "UserPromptSubmit", "iai-mcp-turn-capture.sh"
    )
    assert len(turn_entries) == 1
    assert "matcher" not in turn_entries[0]
    turn_hook = turn_entries[0]["hooks"][0]
    assert (
        turn_hook["command"] == f"bash {fake_home}/.codex/hooks/iai-mcp-turn-capture.sh"
    )
    assert turn_hook["timeout"] == 5
    assert turn_hook["statusMessage"] == "Capturing iai turn"

    stop_entries = _matching_entries(data, "Stop", "iai-mcp-session-capture.sh")
    assert len(stop_entries) == 1
    assert "matcher" not in stop_entries[0]
    stop_hook = stop_entries[0]["hooks"][0]
    assert (
        stop_hook["command"]
        == f"bash {fake_home}/.codex/hooks/iai-mcp-session-capture.sh"
    )
    assert stop_hook["timeout"] == 35
    assert stop_hook["statusMessage"] == "Capturing iai session"


def test_install_codex_hooks_idempotent(fake_home):
    assert _install() == 0
    assert _install() == 0

    data = _codex_hooks(fake_home)
    assert (
        len(_matching_entries(data, "SessionStart", "iai-mcp-session-recall.sh")) == 1
    )
    assert (
        len(_matching_entries(data, "UserPromptSubmit", "iai-mcp-turn-capture.sh")) == 1
    )
    assert len(_matching_entries(data, "Stop", "iai-mcp-session-capture.sh")) == 1


def test_uninstall_removes_only_iai_codex_hooks(fake_home):
    hooks_json = fake_home / ".codex" / "hooks.json"
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /tmp/unrelated.sh",
                                },
                                {
                                    "type": "command",
                                    "command": "bash /tmp/iai-mcp-session-capture.sh",
                                },
                            ]
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "bash /tmp/iai-mcp-turn-capture.sh",
                                }
                            ]
                        }
                    ],
                    "OtherEvent": [
                        {
                            "hooks": [
                                {"type": "command", "command": "bash /tmp/other.sh"}
                            ]
                        }
                    ],
                },
                "unrelatedTopLevel": True,
            },
            indent=2,
        )
    )

    assert _uninstall() == 0

    data = json.loads(hooks_json.read_text())
    assert data["unrelatedTopLevel"] is True
    assert data["hooks"]["OtherEvent"][0]["hooks"][0]["command"] == "bash /tmp/other.sh"
    assert data["hooks"]["Stop"][0]["hooks"] == [
        {"type": "command", "command": "bash /tmp/unrelated.sh"}
    ]
    assert "UserPromptSubmit" not in data["hooks"]
