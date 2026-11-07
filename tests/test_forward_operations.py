import json

import pytest

from etf_autopilot.domain import SafetyError
from etf_autopilot.forward import run_forward_cycle
from etf_autopilot.forward_operations import forward_status, forward_status_markdown

from .test_forward import at, commit, later
from .test_forward import captured as captured
from .test_forward import daily as daily
from .test_forward import exports as exports
from .test_forward import plan as plan
from .test_forward_selection import bear as bear


def test_stale_status_explains_age_and_preserves_financial_state(store, config, daily):
    run_forward_cycle(store, config, daily, now=at("2026-01-12T09:00:00Z"))
    before = store.read()
    report = forward_status(before, now=at("2026-01-12T10:00:00Z"))
    assert report["status"] == "waiting_for_fresh_data"
    assert report["observation_age_hours"] == "130.00" and report["freshness_limit_hours"] == "36"
    assert report["reserved_model_attempts_this_month"] == 0
    assert report["remaining_estimated_budget_eur_this_month"] == "1.00"
    assert not report["state_changes"] and report["network_calls"] == 0
    assert store.read() == before
    assert "130.00 hours" in forward_status_markdown(before, now=at("2026-01-12T10:00:00Z"))


def test_unknown_attempt_needs_operator_even_after_deadline(store, config, bear):
    run_forward_cycle(
        store,
        config,
        bear,
        now=at("2026-01-08T09:00:00Z"),
        selection={"provider_name": "fixture", "reserved_cost_eur": "0.02"},
    )
    before = store.read()
    report = forward_status(before, now=at("2026-01-19T10:00:00Z"))
    assert report["status"] == "unknown_model_outcome"
    assert report["remaining_model_attempts_this_month"] == 0
    assert report["reserved_spend_eur_this_month"] == "0.03"
    assert store.read() == before


def test_pending_expiry_is_reported_without_changing_decision(store, config, daily):
    commit(store, config, daily)
    before = store.read()
    assert (
        forward_status(before, now=at("2026-01-08T10:00:00Z"))["status"]
        == "waiting_for_later_observation"
    )
    assert (
        forward_status(before, now=at("2026-01-19T10:00:00Z"))["status"]
        == "decision_deadline_passed"
    )
    assert store.read() == before and before["decisions"][before["pending"]]["status"] == "ready"


def test_halt_and_code_changes_take_priority_over_stale_input(store, config, daily, monkeypatch):
    from etf_autopilot import forward_operations

    commit(store, config, daily)
    run_forward_cycle(
        store, config, later(daily), now=at("2026-01-10T09:00:00Z"), fault="unresolved"
    )
    before = store.read()
    assert (
        forward_status(before, now=at("2026-01-19T10:00:00Z"))["status"]
        == "reconciliation_required"
    )
    monkeypatch.setattr(forward_operations, "trial_engine_fingerprint", lambda: "changed")
    assert forward_status(before, now=at("2026-01-19T10:00:00Z"))["status"] == "review_required"
    assert store.read() == before


def test_clock_reversal_and_cli_diagnosis_never_advance_a_trial(
    store, config, daily, monkeypatch, capsys
):
    from etf_autopilot import forward_operations
    from etf_autopilot.cli import main

    commit(store, config, daily)
    before = store.read()
    with pytest.raises(SafetyError, match="forward_status_clock_before_recorded_state"):
        forward_status(before, now=at("2026-01-08T08:00:00Z"))

    class Clock:
        @staticmethod
        def now(tz):
            return at("2026-01-12T09:00:00Z")

    monkeypatch.setattr(forward_operations, "datetime", Clock)
    assert main(["--state", str(store.path), "forward-status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "waiting_for_fresh_data"
    assert store.read() == before
