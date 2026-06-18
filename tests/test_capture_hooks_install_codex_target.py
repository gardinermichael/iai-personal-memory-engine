from __future__ import annotations

import argparse
import json
import platform
import stat
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="POSIX exec bit + shell hook layout",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
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


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _commands(path: Path) -> list[str]:
    data = _load(path)
    out: list[str] = []
    for entries in data.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks") or []:
                cmd = hook.get("command")
                if cmd:
                    out.append(cmd)
    return out


def _matching_entries(path: Path, event: str, marker: str) -> list[dict]:
    data = _load(path)
    return [
        entry
        for entry in data.get("hooks", {}).get(event, [])
        if any(marker in (hook.get("command") or "") for hook in (entry.get("hooks") or []))
    ]


def test_parser_accepts_codex_target(home):
    from iai_mcp.cli import main

    rc = main(["capture-hooks", "install", "--target", "codex"])

    assert rc == 0
    assert (home / ".codex" / "hooks.json").exists()


def test_codex_install_writes_executable_scripts_and_hooks_json(home):
    from iai_mcp import cli as cli_mod

    rc = cli_mod.cmd_capture_hooks_install(argparse.Namespace(target="codex"))

    assert rc == 0
    hooks_dir = home / ".codex" / "hooks"
    for name in (
        "iai-mcp-session-capture.sh",
        "iai-mcp-turn-capture.sh",
        "iai-mcp-session-recall.sh",
    ):
        hook = hooks_dir / name
        assert hook.exists(), hook
        assert hook.stat().st_mode & stat.S_IXUSR, oct(hook.stat().st_mode)
    assert (home / ".codex" / "hooks.json").exists()


def test_codex_commands_reference_codex_hooks_not_claude_hooks(home):
    from iai_mcp import cli as cli_mod

    cli_mod.cmd_capture_hooks_install(argparse.Namespace(target="codex"))

    commands = _commands(home / ".codex" / "hooks.json")
    assert commands
    assert all(".codex/hooks" in command for command in commands), commands
    assert all(".claude/hooks" not in command for command in commands), commands


def test_codex_target_does_not_touch_claude_files(home):
    from iai_mcp import cli as cli_mod

    rc = cli_mod.cmd_capture_hooks_install(argparse.Namespace(target="codex"))

    assert rc == 0
    assert not (home / ".claude" / "settings.json").exists()
    assert not (home / ".claude" / "hooks").exists()
    assert not (home / ".claude.json").exists()


def test_repeated_codex_install_is_idempotent_one_entry_per_event(home):
    from iai_mcp import cli as cli_mod

    args = argparse.Namespace(target="codex")
    assert cli_mod.cmd_capture_hooks_install(args) == 0
    assert cli_mod.cmd_capture_hooks_install(args) == 0

    hooks_json = home / ".codex" / "hooks.json"
    assert len(_matching_entries(hooks_json, "Stop", "iai-mcp-session-capture.sh")) == 1
    assert len(_matching_entries(hooks_json, "UserPromptSubmit", "iai-mcp-turn-capture.sh")) == 1
    assert len(_matching_entries(hooks_json, "SessionStart", "iai-mcp-session-recall.sh")) == 1


def test_codex_uninstall_removes_only_iai_entries_and_preserves_unrelated_hooks(home):
    from iai_mcp import cli as cli_mod

    args = argparse.Namespace(target="codex")
    cli_mod.cmd_capture_hooks_install(args)
    hooks_json = home / ".codex" / "hooks.json"
    data = _load(hooks_json)
    data["hooks"].setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": "bash /tmp/unrelated.sh", "timeout": 1}]}
    )
    hooks_json.write_text(json.dumps(data, indent=2))

    assert cli_mod.cmd_capture_hooks_uninstall(args) == 0

    data = _load(hooks_json)
    stop_commands = [
        hook.get("command")
        for entry in data.get("hooks", {}).get("Stop", [])
        for hook in (entry.get("hooks") or [])
    ]
    assert stop_commands == ["bash /tmp/unrelated.sh"]
    assert not any(
        "iai-mcp" in (hook.get("command") or "")
        for entries in data.get("hooks", {}).values()
        for entry in entries
        for hook in (entry.get("hooks") or [])
    )


def test_codex_status_after_install_returns_zero_and_prints_codex_wired(home, capsys):
    from iai_mcp import cli as cli_mod

    cli_mod.cmd_capture_hooks_install(argparse.Namespace(target="codex"))
    capsys.readouterr()

    rc = cli_mod.cmd_capture_hooks_status(argparse.Namespace(target="codex"))
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "Codex" in out
    assert "WIRED" in out


def test_target_all_handles_claude_and_codex_paths(home):
    from iai_mcp import cli as cli_mod

    rc = cli_mod.cmd_capture_hooks_install(argparse.Namespace(target="all"))

    assert rc == 0
    assert (home / ".claude" / "settings.json").exists()
    assert (home / ".claude" / "hooks" / "iai-mcp-session-capture.sh").exists()
    assert (home / ".codex" / "hooks.json").exists()
    assert (home / ".codex" / "hooks" / "iai-mcp-session-capture.sh").exists()
    assert any(".claude/hooks" in command for command in _commands(home / ".claude" / "settings.json"))
    assert any(".codex/hooks" in command for command in _commands(home / ".codex" / "hooks.json"))
