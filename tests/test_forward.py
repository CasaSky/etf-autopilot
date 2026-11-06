from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta

import pytest

from etf_autopilot.domain import SafetyError, digest, number
from etf_autopilot.forward import forward_summary, reconcile_forward, run_forward_cycle
from etf_autopilot.observations import observations_for_capture

from .conftest import ROOT
from .test_capture import plan as plan
from .test_issuer import exports as exports
from .test_observations import captured as captured


@pytest.fixture
def daily(captured):
    plan, directory, _ = captured
    return observations_for_capture(plan, directory)


def at(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def append(data, through, received, *, multiplier="1.01"):
    result = deepcopy(data)
    day = date.fromisoformat(result["rows"][-1]["date"]) + timedelta(days=1)
    while day <= date.fromisoformat(through):
        if day.weekday() < 5:
            previous = result["rows"][-1]["values"]
            result["rows"].append(
                {
                    "date": day.isoformat(),
                    "values": {
                        key: str(number(value) * number(multiplier))
                        for key, value in previous.items()
                    },
                }
            )
        day += timedelta(days=1)
    result["captured_at"] = received
    result["manifest"]["retrieved_at"] = received
    result["capture_id"] = digest({"previous": data["capture_id"], "received": received})
    result["observation_id"] = digest({k: v for k, v in result.items() if k != "observation_id"})
    return result


def commit(store, config, daily):
    result = run_forward_cycle(store, config, daily, now=at("2026-01-08T09:00:00Z"))
    assert result["status"] == "decision_committed" and result["orders"] == 0
    return result


def later(daily):
    return append(daily, "2026-01-09", "2026-01-10T08:00:00Z")


def test_decision_requires_later_unseen_date_and_cost_is_charged_once(store, config, daily):
    decision = commit(store, config, daily)
    state = store.read()
    frozen = deepcopy(state["decisions"][decision["decision_id"]]["plan"])
    assert state["operations"]["months"]["2026-01"]["spend_eur"] == "0.01"
    same_day = append(daily, "2026-01-08", "2026-01-09T08:00:00Z")
    waiting = run_forward_cycle(store, config, same_day, now=at("2026-01-09T09:00:00Z"))
    assert waiting["status"] == "waiting_for_future_observation" and waiting["orders"] == 0
    future = later(same_day)
    result = run_forward_cycle(store, config, future, now=at("2026-01-10T09:00:00Z"))
    assert result["status"] == "executed" and result["orders"] == 6
    assert result["modeled_value_date"] == "2026-01-09"
    assert store.read()["decisions"][decision["decision_id"]]["plan"] == frozen
    report = forward_summary(store.read())
    assert report["comparable"] and report["units"] == "research_index_units_not_broker_shares"
    assert report["accounts"]["strategy"]["operations_eur"] == "0.01"
    assert report["accounts"]["passive"]["operations_eur"] == "0.00"
    for account in store.read()["accounts"].values():
        fills = [row for row in account["ledger"] if row["type"] == "fill"]
        assert all(row["modeled_value_date"] == "2026-01-09" for row in fills)
        assert all(row["at"] == "2026-01-10T09:00:00+00:00" for row in fills)
        assert account["marks"][-1]["value_date"] == "2026-01-09"
    before = store.read()
    assert run_forward_cycle(store, config, future, now=at("2026-01-10T10:00:00Z")) == {
        **result,
        "duplicate": True,
    }
    assert store.read() == before


def test_concurrent_duplicate_commits_one_month_and_one_operating_charge(store, config, daily):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: run_forward_cycle(store, config, daily, now=at("2026-01-08T09:00:00Z")),
                range(2),
            )
        )
    assert sorted(row["duplicate"] for row in results) == [False, True]
    assert len(store.read()["decisions"]) == 1
    assert store.read()["operations"]["months"]["2026-01"]["spend_eur"] == "0.01"


def test_stale_initial_capture_is_valued_without_decision_or_orders(store, config, daily):
    result = run_forward_cycle(store, config, daily, now=at("2026-01-12T09:00:00Z"))
    assert result["diagnosis"] == "fresh_forward_observation_required" and result["orders"] == 0
    state = store.read()
    assert state["decisions"] == {} and state["pending"] is None
    assert forward_summary(state)["accounts"]["strategy"]["net_equity_eur"] == "5000.00"


def test_future_capture_and_known_history_revision_cannot_change_account(store, config, daily):
    commit(store, config, daily)
    before = store.read()
    future = later(daily)
    with pytest.raises(SafetyError, match="forward_future_capture"):
        run_forward_cycle(store, config, future, now=at("2026-01-09T09:00:00Z"))
    revised = deepcopy(future)
    revised["rows"][10]["values"]["RESEARCH_EQUITY"] = "99"
    revised["observation_id"] = digest({k: v for k, v in revised.items() if k != "observation_id"})
    with pytest.raises(SafetyError, match="forward_observed_history_revised"):
        run_forward_cycle(store, config, revised, now=at("2026-01-10T09:00:00Z"))
    assert store.read() == before


def test_config_cannot_change_after_committing_a_decision(store, config, daily):
    commit(store, config, daily)
    before = store.read()
    config["risk"]["max_drawdown"] = "0.9"
    with pytest.raises(SafetyError, match="forward_trial_config_changed"):
        run_forward_cycle(store, config, later(daily), now=at("2026-01-10T09:00:00Z"))
    assert store.read() == before


def test_freshness_applies_to_selected_execution_date_not_only_latest_row(store, config, daily):
    commit(store, config, daily)
    batch = append(daily, "2026-01-12", "2026-01-13T08:00:00Z")
    result = run_forward_cycle(store, config, batch, now=at("2026-01-13T09:00:00Z"))
    assert result["status"] == "executed"
    assert result["modeled_value_date"] == "2026-01-12"


def test_expired_decision_never_fills_an_old_opportunity(store, config, daily):
    commit(store, config, daily)
    batch = append(daily, "2026-01-16", "2026-01-19T08:00:00Z")
    result = run_forward_cycle(store, config, batch, now=at("2026-01-19T09:00:00Z"))
    assert result["status"] == "decision_expired" and result["orders"] == 0
    assert store.read()["pending"] is None
    assert store.read()["slots"]["2026-01"] == "forward-decision:2026-01"
    assert not forward_summary(store.read())["comparable"]


@pytest.mark.parametrize("fault", ["unknown", "partial", "expired_quote"])
def test_reconciliation_recovers_only_confirmed_outcomes_never_retries_remainder(
    store, config, daily, fault
):
    commit(store, config, daily)
    future = later(daily)
    result = run_forward_cycle(store, config, future, now=at("2026-01-10T09:00:00Z"), fault=fault)
    assert result["status"] == "halted"
    before = len(store.read()["accounts"]["strategy"]["broker"]["operations"])
    assert not forward_summary(store.read())["comparable"]
    assert reconcile_forward(store) == {"status": "matched", "remainder_retry": False}
    assert run_forward_cycle(store, config, future, now=at("2026-01-10T10:00:00Z"))["duplicate"]
    next_data = append(future, "2026-01-12", "2026-01-13T08:00:00Z")
    assert (
        run_forward_cycle(store, config, next_data, now=at("2026-01-13T09:00:00Z"))["orders"] == 0
    )
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == before
    assert not forward_summary(store.read())["comparable"]


def test_decision_targets_remain_frozen_across_a_month_boundary(store, config, daily):
    january = append(daily, "2026-01-29", "2026-01-30T08:00:00Z")
    result = run_forward_cycle(store, config, january, now=at("2026-01-30T09:00:00Z"))
    assert result["status"] == "decision_committed"
    frozen = deepcopy(store.read()["decisions"][result["decision_id"]]["plan"])
    february = append(january, "2026-02-02", "2026-02-03T08:00:00Z", multiplier="0.7")
    executed = run_forward_cycle(store, config, february, now=at("2026-02-03T09:00:00Z"))
    assert executed["status"] == "executed"
    assert store.read()["decisions"][result["decision_id"]]["plan"] == frozen
    assert (
        number(
            store.read()["accounts"]["strategy"]["portfolio"]["positions"]["RESEARCH_EQUITY"][
                "quantity"
            ]
        )
        > 0
    )
    assert set(store.read()["slots"]) == {"2026-01"}


def test_cli_uses_separate_forward_state_and_reports_without_a_config_file(
    daily, tmp_path, monkeypatch, capsys
):
    import json

    from etf_autopilot import forward
    from etf_autopilot.cli import main
    from etf_autopilot.history import write_immutable_json

    class Clock:
        @staticmethod
        def now(tz):
            return at("2026-01-12T09:00:00Z")

    monkeypatch.setattr(forward, "datetime", Clock)
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "daily.json"
    write_immutable_json(path, daily)
    assert main(["--config", str(ROOT / "config/paper.json"), "forward-cycle", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["diagnosis"] == "fresh_forward_observation_required"
    assert (tmp_path / "var/forward-paper.sqlite3").exists()
    assert not (tmp_path / "var/paper.sqlite3").exists()
    assert main(["forward-report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "forward_research_paper"
