from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, digest, money, number
from etf_autopilot.evaluation import (
    EvaluationRegistry,
    evaluate_window,
    evaluation_markdown,
    protocol_template,
    validate_protocol,
)
from etf_autopilot.history import import_history

from .conftest import ROOT
from .test_history import rehash


@pytest.fixture
def dataset():
    fixture = ROOT / "fixtures/history_contract"
    return import_history(fixture / "manifest.json", fixture / "monthly.csv")


@pytest.fixture
def windows():
    return {
        "development": {"start": "2022-12", "end": "2023-11"},
        "validation": {"start": "2023-12", "end": "2024-11"},
        "holdout": {"start": "2024-12", "end": "2026-01"},
    }


@pytest.fixture
def protocol(dataset, config, windows):
    return protocol_template(dataset, config, windows)


@pytest.fixture
def registry(tmp_path):
    return EvaluationRegistry(tmp_path / "evaluations.sqlite3")


def test_evaluation_retains_aligned_dates_capital_and_independent_comparators(
    dataset, config, protocol
):
    result = evaluate_window(dataset, config, protocol, "development")
    assert result["origin"] == "synthetic_fixture"
    assert result["ai_effect_tested"] is False
    assert result["model_calls"] == 0
    assert result["automatic_promotion"] is False
    assert result["strategy_minus_without_ai_eur"] == "0.00"
    accounts = result["accounts"]
    dates = [[d["at"] for d in account["decisions"]] for account in accounts.values()]
    assert all(d == dates[0] for d in dates)
    for role, account in accounts.items():
        s = account["summary"]
        assert s["starting_capital_eur"] == "5000.00"
        assert s["reconciliation"] == "matched"
        assert s["valuations"] == 12
        assert s["orders"] > 0
        assert s["operations_eur"] == ("0.00" if role == "passive" else "0.12")
        assert account["decisions"][-1]["status"] == "valuation_only"
        assert account["decisions"][-1]["fills"] == []
        assert money(s["fees_eur"]) == money(str(s["orders"]))
        assert money(s["net_equity_eur"]) == money(s["cash_eur"]) + sum(
            money(p["market_value_eur"]) for p in s["positions"]
        ) - money(s["operations_eur"])
    assert "SYNTHETIC CONTRACT CHECK" in evaluation_markdown(result)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("overlap", "overlapping_or_nonchronological_windows"),
        ("reverse", "overlapping_or_nonchronological_windows"),
        ("warmup", "evaluation_window_missing_warmup"),
        ("one_mark", "evaluation_window_requires_two_valuations"),
        ("beyond", "evaluation_window_outside_dataset"),
        ("changed_config", "evaluation_dataset_or_config_changed"),
        ("changed_dataset", "evaluation_dataset_or_config_changed"),
        ("cost", "evaluation_operating_cost_budget_exceeded"),
    ],
)
def test_protocol_changes_cannot_silently_change_evaluation(
    dataset, config, protocol, case, expected
):
    if case == "overlap":
        protocol["windows"]["validation"]["start"] = "2023-11"
    elif case == "reverse":
        protocol["windows"]["development"] = {"start": "2023-11", "end": "2023-01"}
    elif case == "warmup":
        protocol["windows"]["development"]["start"] = "2022-11"
    elif case == "one_mark":
        protocol["windows"]["development"]["end"] = "2022-12"
    elif case == "beyond":
        protocol["windows"]["holdout"]["end"] = "2030-01"
    elif case == "changed_config":
        config["execution"]["fee_eur"] = "2"
    elif case == "changed_dataset":
        protocol["dataset_id"] = "f" * 64
    elif case == "cost":
        protocol["operating_cost_per_observation_eur"] = "1.01"
    with pytest.raises(SafetyError, match=expected):
        validate_protocol(protocol, dataset, config)


def test_signal_uses_available_close_instead_of_later_execution_observation(
    dataset, config, windows
):
    # The opening index looks strong, but the previous closing signal is weak.
    row = dataset["rows"][10]
    row["signals"]["RESEARCH_EQUITY"] = "50"
    row["executions"]["RESEARCH_EQUITY"] = "200"
    rehash(dataset)
    protocol = protocol_template(dataset, config, windows)
    result = evaluate_window(dataset, config, protocol, "development")
    first = result["accounts"]["strategy"]["decisions"][0]
    assert first["targets"]["RESEARCH_EQUITY"] == "0"
    assert first["signal_at"] < first["available_at"] < first["at"]


def test_later_data_cannot_change_earlier_targets_or_results(dataset, config, protocol, windows):
    original = evaluate_window(dataset, config, protocol, "development")
    later = deepcopy(dataset)
    for row in later["rows"][34:]:
        for asset in row["signals"]:
            row["signals"][asset] = str(number(row["signals"][asset]) * 3)
            row["executions"][asset] = str(number(row["executions"][asset]) * 3)
    rehash(later)
    alternate_protocol = protocol_template(later, config, windows)
    alternate = evaluate_window(later, config, alternate_protocol, "development")
    for role in original["accounts"]:
        assert original["accounts"][role]["summary"] == alternate["accounts"][role]["summary"]
        assert [d["targets"] for d in original["accounts"][role]["decisions"]] == [
            d["targets"] for d in alternate["accounts"][role]["decisions"]
        ]


def test_registration_required_and_repeated_runs_are_cached(dataset, config, protocol, registry):
    with pytest.raises(SafetyError, match="register_protocol_before_evaluation"):
        registry.run(protocol, dataset, config, "development")
    registration = registry.register(protocol, dataset, config)
    assert registration["protocol_id"] == digest(protocol)
    assert registry.register(protocol, dataset, config) == registration
    first = registry.run(protocol, dataset, config, "development")
    assert first["cached"] is False
    reopened = EvaluationRegistry(registry.path)
    second = reopened.run(protocol, dataset, config, "development")
    assert second == {**first, "cached": True}


def test_holdout_is_explicit_and_requires_both_earlier_windows(dataset, config, protocol, registry):
    registry.register(protocol, dataset, config)
    with pytest.raises(SafetyError, match="complete_development_and_validation"):
        registry.run(protocol, dataset, config, "holdout")
    registry.run(protocol, dataset, config, "development")
    with pytest.raises(SafetyError, match="complete_development_and_validation"):
        registry.run(protocol, dataset, config, "holdout")
    registry.run(protocol, dataset, config, "validation")
    result = registry.run(protocol, dataset, config, "holdout")
    assert result["window"] == "holdout"
    assert registry.run(protocol, dataset, config, "holdout") == {**result, "cached": True}


def test_modified_policy_cannot_reuse_an_exposed_holdout(
    dataset, config, protocol, registry, windows
):
    registry.register(protocol, dataset, config)
    for window in ("development", "validation", "holdout"):
        registry.run(protocol, dataset, config, window)
    revised = deepcopy(config)
    revised["execution"]["fee_eur"] = "2.00"
    changed_protocol = protocol_template(dataset, revised, windows)
    registry.register(changed_protocol, dataset, revised)
    for window in ("development", "validation"):
        registry.run(changed_protocol, dataset, revised, window)
    with pytest.raises(SafetyError, match="holdout_already_exposed"):
        registry.run(changed_protocol, dataset, revised, "holdout")


def test_data_revision_cannot_make_the_same_holdout_fresh(
    dataset, config, protocol, registry, windows
):
    registry.register(protocol, dataset, config)
    for window in ("development", "validation", "holdout"):
        registry.run(protocol, dataset, config, window)
    revised = deepcopy(dataset)
    revised["rows"][0]["signals"]["RESEARCH_EQUITY"] = "111"
    rehash(revised)
    changed_protocol = protocol_template(revised, config, windows)
    registry.register(changed_protocol, revised, config)
    for window in ("development", "validation"):
        registry.run(changed_protocol, revised, config, window)
    with pytest.raises(SafetyError, match="holdout_already_exposed"):
        registry.run(changed_protocol, revised, config, "holdout")


def test_risk_stop_keeps_valuing_losses_without_stopping_an_independent_comparator(
    dataset, config, windows
):
    for index, value in ((10, "100"), (11, "50"), (12, "25"), (13, "10")):
        row = dataset["rows"][index]
        row["signals"]["RESEARCH_EQUITY"] = "50"  # Strategy stays in cash for equity.
        row["executions"]["RESEARCH_EQUITY"] = value
    rehash(dataset)
    windows["development"]["end"] = "2023-03"
    protocol = protocol_template(dataset, config, windows)
    result = evaluate_window(dataset, config, protocol, "development")
    passive = result["accounts"]["passive"]
    assert passive["summary"]["halt"] == "drawdown_limit_exceeded"
    assert len(passive["decisions"]) == 4
    assert money(passive["decisions"][-1]["net_equity_eur"]) < money(
        passive["decisions"][1]["net_equity_eur"]
    )
    assert all(not row["fills"] for row in passive["decisions"][1:])
    assert result["accounts"]["strategy"]["summary"]["halt"] is None
    assert result["accounts"]["strategy"]["summary"]["reconciliation"] == "matched"


def test_holdout_reservation_survives_failed_computation(
    dataset, config, protocol, registry, monkeypatch
):
    import etf_autopilot.evaluation as module

    registry.register(protocol, dataset, config)
    registry.run(protocol, dataset, config, "development")
    registry.run(protocol, dataset, config, "validation")
    original = module.evaluate_window
    monkeypatch.setattr(
        module,
        "evaluate_window",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("injected interruption after exposure reservation")
        ),
    )
    with pytest.raises(RuntimeError, match="injected interruption"):
        registry.run(protocol, dataset, config, "holdout")
    with registry._connect() as conn:
        assert conn.execute("SELECT count(*) FROM exposures").fetchone()[0] == 3
        assert (
            conn.execute("SELECT status FROM runs WHERE window='holdout'").fetchone()[0]
            == "reserved"
        )
    monkeypatch.setattr(module, "evaluate_window", original)
    assert registry.run(protocol, dataset, config, "holdout")["window"] == "holdout"


def test_validation_cannot_precede_development(dataset, config, protocol, registry):
    registry.register(protocol, dataset, config)
    with pytest.raises(SafetyError, match="complete_development_before_validation"):
        registry.run(protocol, dataset, config, "validation")


def test_seen_development_data_cannot_be_relabelled_an_untouched_holdout(
    dataset, config, protocol, registry, windows
):
    registry.register(protocol, dataset, config)
    registry.run(protocol, dataset, config, "development")
    # Different protocol with an earlier development/validation period and a
    # purported holdout that overlaps the already examined development results.
    changed_windows = {
        "development": {"start": "2022-12", "end": "2023-01"},
        "validation": {"start": "2023-02", "end": "2023-03"},
        "holdout": {"start": "2023-04", "end": "2023-05"},
    }
    changed = protocol_template(dataset, config, changed_windows)
    registry.register(changed, dataset, config)
    registry.run(changed, dataset, config, "development")
    registry.run(changed, dataset, config, "validation")
    with pytest.raises(SafetyError, match="holdout_already_exposed"):
        registry.run(changed, dataset, config, "holdout")


def test_engine_change_cannot_reuse_a_protocol_or_cached_result(
    dataset, config, protocol, registry, monkeypatch
):
    import etf_autopilot.evaluation as module

    registry.register(protocol, dataset, config)
    registry.run(protocol, dataset, config, "development")
    monkeypatch.setattr(module, "engine_fingerprint", lambda: "f" * 64)
    with pytest.raises(SafetyError, match="evaluation_engine_changed"):
        registry.run(protocol, dataset, config, "development")


def test_fees_that_cross_the_drawdown_stop_are_recorded_immediately(dataset, config, windows):
    config["execution"]["fee_eur"] = "100"
    config["risk"]["max_drawdown"] = "0.01"
    protocol = protocol_template(dataset, config, windows)
    result = evaluate_window(dataset, config, protocol, "development")
    passive = result["accounts"]["passive"]
    assert passive["decisions"][0]["status"] == "halted_after_execution"
    assert passive["summary"]["halt"] == "drawdown_limit_exceeded"
    assert passive["summary"]["orders"] == 2
    assert all(not row["fills"] for row in passive["decisions"][1:])
    assert passive["summary"]["reconciliation"] == "matched"
