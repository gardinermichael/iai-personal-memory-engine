from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _help(*args: str) -> str:
    env = os.environ.copy()
    pythonpath = str(SRC)
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}{os.pathsep}{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    env.setdefault("NO_COLOR", "1")
    completed = subprocess.run(
        [sys.executable, "-m", *args, "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return completed.stdout


def test_iai_mcp_top_level_help_includes_examples_and_major_workflows() -> None:
    out = _help("iai_mcp.cli")

    assert "Examples:" in out
    assert "Major workflows:" in out
    assert "daemon" in out
    assert "capture-hooks" in out
    assert "crypto" in out
    assert "maintenance" in out
    assert "topology" in out


def test_iai_help_explains_iai_vs_iai_mcp() -> None:
    out = _help("iai_mcp.iai_cli")

    assert "Use `iai` for day-to-day memory reads/writes" in out
    assert "use `iai-mcp` for operator workflows" in out
    assert "daemon control" in out
    assert "hook installation" in out
    assert "crypto" in out
    assert "maintenance" in out


def test_major_iai_mcp_command_help_has_descriptions_and_examples() -> None:
    expected = {
        ("iai_mcp.cli", "daemon"): ["background sleep daemon", "scheduler lifecycle"],
        ("iai_mcp.cli", "capture-hooks"): ["Claude Code hooks", "ambient session capture"],
        ("iai_mcp.cli", "crypto"): ["encryption keys", "recovery workflows"],
        ("iai_mcp.cli", "maintenance"): ["one-shot maintenance", "dry-run"],
    }

    for args, phrases in expected.items():
        out = _help(*args)
        assert "Examples:" in out
        for phrase in phrases:
            assert phrase in out


def test_major_iai_command_help_has_descriptions_and_examples() -> None:
    expected = {
        ("iai_mcp.iai_cli", "recall"): ["Recall memories", "offline bank scan"],
        ("iai_mcp.iai_cli", "capture"): ["Write one episodic record", "via the daemon"],
    }

    for args, phrases in expected.items():
        out = _help(*args)
        assert "Examples:" in out
        for phrase in phrases:
            assert phrase in out


def test_help_surfaces_representative_side_effect_labels() -> None:
    outputs = [
        _help("iai_mcp.cli"),
        _help("iai_mcp.cli", "daemon"),
        _help("iai_mcp.cli", "capture-hooks"),
        _help("iai_mcp.cli", "maintenance"),
        _help("iai_mcp.iai_cli", "recall"),
        _help("iai_mcp.iai_cli", "capture"),
    ]
    joined = "\n".join(outputs)

    assert "[read-only]" in joined
    assert "[writes memory]" in joined
    assert "[installs files/services]" in joined
