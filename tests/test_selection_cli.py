import json
import os
import subprocess
import sys

from .conftest import ROOT


def test_scripted_example_is_offline_and_replay_does_not_call_again(tmp_path):
    command = [
        sys.executable,
        "-S",
        str(ROOT / "examples/ai_paper_demo.py"),
        "--output-dir",
        str(tmp_path / "example"),
    ]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "no actual AI inference" in first.stdout and "calls this run: 3" in first.stdout
    report = (tmp_path / "example/report.md").read_bytes()
    second = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    assert "calls this run: 0" in second.stdout
    assert (tmp_path / "example/report.md").read_bytes() == report


def test_cli_model_opt_in_is_required_before_state_creation(tmp_path, events):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(events[0]))
    state = tmp_path / "state.sqlite3"
    command = [
        sys.executable,
        "-S",
        "-m",
        "etf_autopilot.cli",
        "--config",
        str(ROOT / "config/paper.json"),
        "--state",
        str(state),
        "paper-decision",
        str(event),
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    blocked = subprocess.run(
        [*command, "--model-id", "unapproved"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 2 and not state.exists()
    assert json.loads(blocked.stdout)["code"] == (
        "model_requires_opt_in_id_region_and_positive_cost_reserve"
    )
    deterministic = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )
    result = json.loads(deterministic.stdout)
    assert result["status"] == "completed" and result["selection"]["source"] == "deterministic"
