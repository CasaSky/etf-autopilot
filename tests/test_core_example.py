import json
import os
import sqlite3
import subprocess
import sys

from .conftest import ROOT


def test_example_runs_without_installed_packages_from_another_directory(tmp_path):
    output = tmp_path / "example-output"
    command = [
        sys.executable,
        "-S",
        str(ROOT / "examples/paper_demo.py"),
        "--output-dir",
        str(output),
    ]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "Synthetic fixtures" in first.stdout
    assert "already recorded: 0" in first.stdout
    report = (output / "report.md").read_bytes()
    with sqlite3.connect(output / "paper.sqlite3") as conn:
        original = json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "already recorded: 3" in second.stdout
    assert (output / "report.md").read_bytes() == report
    with sqlite3.connect(output / "paper.sqlite3") as conn:
        replayed = json.loads(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
    assert replayed == original


def test_core_only_broker_command_explains_its_optional_dependency(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-S", "-m", "etf_autopilot.cli", "broker-check", "--allow-account-read"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "status": "blocked",
        "code": "broker_dependencies_missing_install_broker_extra",
    }
