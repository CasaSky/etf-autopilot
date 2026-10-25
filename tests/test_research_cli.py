import json
from pathlib import Path

from etf_autopilot.cli import main

from .conftest import ROOT


def test_import_plan_register_and_all_evaluation_windows_are_repeatable(tmp_path, capsys):
    dataset, protocol = tmp_path / "dataset.json", tmp_path / "protocol.json"
    registry, output = tmp_path / "registry.sqlite3", tmp_path / "results"
    common = ["--config", str(ROOT / "config/paper.json")]
    fixture = ROOT / "fixtures/history_contract"
    assert (
        main(
            common
            + [
                "history-import",
                str(fixture / "manifest.json"),
                str(fixture / "monthly.csv"),
                "--out",
                str(dataset),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["origin"] == "synthetic_fixture"
    assert (
        main(
            common
            + [
                "evaluation-plan",
                str(dataset),
                "--development",
                "2022-12:2023-11",
                "--validation",
                "2023-12:2024-11",
                "--holdout",
                "2024-12:2026-01",
                "--out",
                str(protocol),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "planned_not_registered"
    assert (
        main(
            common
            + ["evaluation-register", str(dataset), str(protocol), "--registry", str(registry)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "registered"
    for window in ("development", "validation", "holdout"):
        arguments = common + [
            "evaluate",
            str(dataset),
            str(protocol),
            "--window",
            window,
            "--registry",
            str(registry),
            "--out-dir",
            str(output),
        ]
        assert main(arguments) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["origin"] == "synthetic_fixture"
        assert first["ai_effect_tested"] is False
        assert first["automatic_promotion"] is False
        assert first["cached"] is False
        original = Path(first["result"]).read_bytes()
        assert "SYNTHETIC CONTRACT CHECK" in Path(first["report"]).read_text()
        assert main(arguments) == 0
        cached = json.loads(capsys.readouterr().out)
        assert cached["cached"] is True
        assert Path(cached["result"]).read_bytes() == original


def test_bad_protocol_or_data_returns_a_small_blocked_receipt(tmp_path, capsys):
    data = tmp_path / "invalid.json"
    data.write_text('{"invalid":"input"}')
    assert (
        main(
            [
                "--config",
                str(ROOT / "config/paper.json"),
                "evaluation-plan",
                str(data),
                "--development",
                "2022-12:2023-11",
                "--validation",
                "2023-12:2024-11",
                "--holdout",
                "2024-12:2026-01",
                "--out",
                str(tmp_path / "out.json"),
            ]
        )
        == 2
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "blocked"
    assert receipt["code"] == "unsupported_normalized_dataset"
    assert not (tmp_path / "out.json").exists()
