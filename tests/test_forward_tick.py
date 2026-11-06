import json

import pytest

from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.forward_tick import run_forward_tick
from etf_autopilot.issuer import SS

from .test_capture import fetcher
from .test_forward import at
from .test_issuer import exports as exports
from .test_issuer import growth, xml_change
from .test_observations import captured as captured
from .test_observations import plan as plan


def forbidden(*_):
    raise AssertionError("Replay must not read network, clock or model")


def test_complete_tick_and_replay_verify_evidence_without_side_effects(
    store, config, captured, exports, tmp_path
):
    plan, _, _ = captured
    calls = []
    directory = tmp_path / "tick"
    receipt = run_forward_tick(
        store,
        config,
        plan,
        directory,
        fetch=fetcher(exports, calls),
        clock=lambda: at("2026-01-08T09:00:00Z"),
    )
    assert len(calls) == 3 and receipt["cycle"]["status"] == "decision_committed"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "report.md").stat().st_mode & 0o777 == 0o600
    before = store.read()
    assert run_forward_tick(store, config, plan, directory, fetch=forbidden, clock=forbidden) == {
        **receipt,
        "duplicate": True,
    }
    assert store.read() == before
    assert load_json(directory / "report.json")["model_calls"] == 0
    (directory / "report.md").write_text("changed")
    with pytest.raises(SafetyError, match="forward_tick_files_changed"):
        run_forward_tick(store, config, plan, directory, fetch=forbidden, clock=forbidden)


def test_network_failure_preserved_without_retry(store, config, captured, tmp_path):
    plan, _, _ = captured
    directory = tmp_path / "failed"
    calls = []

    def broken(*_):
        calls.append(1)
        raise SafetyError("source_unavailable")

    with pytest.raises(SafetyError, match="source_unavailable"):
        run_forward_tick(
            store, config, plan, directory, fetch=broken, clock=lambda: at("2026-01-08T09:00:00Z")
        )
    with pytest.raises(SafetyError, match="issuer_capture_incomplete_use_new_directory"):
        run_forward_tick(store, config, plan, directory, fetch=forbidden, clock=forbidden)
    assert len(calls) == 1 and not (directory / "tick.json").exists()
    with pytest.raises(SafetyError, match="state_not_initialized"):
        store.read()


def test_recovery_after_state_commit_reuses_capture_without_charging_again(
    store, config, captured, exports, tmp_path, monkeypatch
):
    from etf_autopilot import forward_tick

    plan, _, _ = captured
    directory = tmp_path / "interrupted"
    original = forward_tick.run_forward_decision

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process stopped after committed decision")

    monkeypatch.setattr(forward_tick, "run_forward_decision", interrupted)
    with pytest.raises(RuntimeError, match="process stopped"):
        run_forward_tick(
            store,
            config,
            plan,
            directory,
            fetch=fetcher(exports, []),
            clock=lambda: at("2026-01-08T09:00:00Z"),
        )
    before = store.read()
    monkeypatch.setattr(forward_tick, "run_forward_decision", original)
    receipt = run_forward_tick(
        store, config, plan, directory, fetch=forbidden, clock=lambda: at("2026-01-08T10:00:00Z")
    )
    assert receipt["cycle"]["duplicate"] and store.read() == before
    assert before["operations"]["months"]["2026-01"]["spend_eur"] == "0.01"


def test_full_fixture_pipeline_calls_only_bounded_injected_choice(
    store, config, captured, exports, tmp_path
):
    plan, _, _ = captured
    config["operations"]["max_model_calls"] = 1

    def declining(root):
        for row in list(growth(root))[1:]:
            stamp = row[0].find(SS + "Data").text
            if stamp and ("Dec 2025" in stamp or "Jan 2026" in stamp):
                row[1].find(SS + "Data").text = "7,000.00"

    xml_change(exports / "equity.xls", declining)
    calls = []

    def provider(*_):
        assert store.read()["decisions"]["forward-decision:2026-01"]["status"] == "reserved"
        calls.append(1)
        return '{"strategy":"passive"}'

    directory = tmp_path / "choice"
    receipt = run_forward_tick(
        store,
        config,
        plan,
        directory,
        fetch=fetcher(exports, []),
        clock=lambda: at("2026-01-08T09:00:00Z"),
        provider=provider,
        provider_name="fixture",
        reserved_cost_eur="0.02",
    )
    assert receipt["cycle"]["selection_status"] == "ready" and calls == [1]
    assert run_forward_tick(
        store,
        config,
        plan,
        directory,
        fetch=forbidden,
        clock=forbidden,
        provider=forbidden,
        provider_name="fixture",
        reserved_cost_eur="0.02",
    )["duplicate"]
    assert calls == [1]


def test_tick_context_change_or_lost_state_cannot_claim_replay(
    store, config, captured, exports, tmp_path
):
    plan, _, _ = captured
    directory = tmp_path / "tick"
    receipt = run_forward_tick(
        store,
        config,
        plan,
        directory,
        fetch=fetcher(exports, []),
        clock=lambda: at("2026-01-08T09:00:00Z"),
    )
    changed = load_json(plan)
    changed["downloads"]["RESEARCH_EQUITY"]["portfolio_id"] = "333"
    plan.write_text(json.dumps(changed))
    with pytest.raises(SafetyError, match="forward_tick_context_conflict"):
        run_forward_tick(store, config, plan, directory, fetch=forbidden, clock=forbidden)
    changed["downloads"]["RESEARCH_EQUITY"]["portfolio_id"] = "111"
    plan.write_text(json.dumps(changed))

    def tamper(state):
        state["events"][receipt["observation_id"]]["hash"] = digest("different context")
        return None, state

    store.update(tamper)
    with pytest.raises(SafetyError, match="forward_tick_state_missing"):
        run_forward_tick(store, config, plan, directory, fetch=forbidden, clock=forbidden)


def test_stale_tick_retains_report_and_cli_replays_it_without_network(
    store, config, captured, exports, tmp_path, capsys
):
    from etf_autopilot.cli import main

    plan, _, _ = captured
    directory = tmp_path / "stale"
    receipt = run_forward_tick(
        store,
        config,
        plan,
        directory,
        fetch=fetcher(exports, []),
        clock=lambda: at("2026-01-12T09:00:00Z"),
    )
    assert receipt["cycle"]["diagnosis"] == "fresh_forward_observation_required"
    assert load_json(directory / "report.json")["accounts"]["strategy"]["orders"] == 0
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    assert (
        main(
            [
                "--config",
                str(config_path),
                "--state",
                str(store.path),
                "forward-tick",
                str(plan),
                "--out-dir",
                str(directory),
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["duplicate"] and result["report"] == str(directory / "report.md")
