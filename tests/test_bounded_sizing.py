from copy import deepcopy
from decimal import Decimal

import pytest

from etf_autopilot.domain import SafetyError, money, number, validate_config
from etf_autopilot.engine import run_event
from etf_autopilot.paper import new_account
from etf_autopilot.report import markdown, summary
from etf_autopilot.strategy import plan, plan_with_limits

DAY = "2026-08-23"


def weights(config):
    return {asset: metadata["weight"] for asset, metadata in config["assets"].items()}


def test_bounded_rebalance_preserves_caps_reconciliation_and_replay(store, config, events):
    config["starting_cash"] = "5100.00"
    config["execution"]["sizing_policy"] = "bounded"
    original = deepcopy(config)
    result = run_event(store, config, events[0])
    assert result["status"] == "completed"
    assert set(result["execution_constraints"]) == {"strategy", "passive", "without_ai"}
    for account in store.read()["accounts"].values():
        fills = [row for row in account["ledger"] if row["type"] == "fill"]
        assert sum(number(row["notional"]) for row in fills) <= Decimal("4500")
        assert number(account["portfolio"]["cash"]) >= Decimal("510")
    report = summary(store.read())
    assert report["constrained_events"] == 1
    assert all(row["reconciliation"] == "matched" for row in report["accounts"].values())
    assert "Constrained events: 1" in markdown(store.read())
    before = store.read()
    assert run_event(store, config, events[0]) == {**result, "duplicate": True}
    assert store.read() == before
    assert config == original


def test_original_policy_still_rejects_same_oversized_rebalance(store, config, events):
    config["starting_cash"] = "5100.00"
    result = run_event(store, config, events[0])
    assert result["diagnosis"] == "daily_volume_limit_exceeded"
    assert all(not a["broker"]["operations"] for a in store.read()["accounts"].values())


@pytest.mark.parametrize("cap,expected_orders", [("2000", 2), ("100", 0)])
def test_small_cap_leaves_cash_and_skips_uneconomic_orders(
    store, config, events, cap, expected_orders
):
    config["execution"]["sizing_policy"] = "bounded"
    config["risk"]["max_daily_buy_eur"] = cap
    result = run_event(store, config, events[0])
    assert result["status"] == "completed"
    for account in store.read()["accounts"].values():
        fills = [row for row in account["ledger"] if row["type"] == "fill"]
        assert len(fills) == expected_orders
        assert sum(number(row["notional"]) for row in fills) <= number(cap)
        assert money(account["portfolio"]["fees"]) == expected_orders
        if not fills:
            assert account["broker"]["active_quote"] is None
            assert account["broker"]["operations"] == {}
    if expected_orders == 0:
        assert all(
            note["status"] == "skipped_below_minimum_order"
            for notes in result["execution_constraints"].values()
            for note in notes
        )
    extra = deepcopy(events[0])
    extra.update(id="cannot-use-new-id-to-retry-remainder", at="2026-08-24T10:00:00Z")
    assert run_event(store, config, extra)["status"] == "skipped_monthly_cadence"


def test_remaining_daily_allowance_is_used_without_mutating_input(config, events):
    config["execution"]["sizing_policy"] = "bounded"
    config["risk"]["max_daily_buy_eur"] = "2000"
    account = new_account("5000")
    account["daily_volume"][DAY] = {"buy": "1900", "sell": "0"}
    before = deepcopy(account)
    result = plan_with_limits(account, events[0]["market"], weights(config), config, DAY)
    assert result["orders"] == []
    assert all(note["remaining_notional_budget_eur"] == "100.00" for note in result["constraints"])
    assert account == before


def test_turnover_budget_limits_whole_plan_not_each_order(store, config, events):
    config["execution"]["sizing_policy"] = "bounded"
    config["risk"]["max_turnover_weight"] = "0.10"
    result = run_event(store, config, events[0])
    assert result["status"] == "completed"
    for account in store.read()["accounts"].values():
        fills = [row for row in account["ledger"] if row["type"] == "fill"]
        assert sum(number(row["notional"]) for row in fills) <= Decimal("500")
    assert all(
        "turnover" in note["reasons"]
        for notes in result["execution_constraints"].values()
        for note in notes
    )


def test_fractional_cent_turnover_budget_cannot_round_up(config):
    config["execution"]["sizing_policy"] = "bounded"
    config["execution"]["spread_bps"] = "0"
    config["risk"]["max_turnover_weight"] = "0.73"
    market = {"prices": dict.fromkeys(config["assets"], "0.01")}
    result = plan_with_limits(new_account("5100.01"), market, weights(config), config, DAY)
    gross = sum(money(number(order["quantity"]) * Decimal("0.01")) for order in result["orders"])
    assert gross == Decimal("3723.00")
    assert gross <= Decimal("5100.01") * Decimal("0.73")


def test_feasible_partial_sell_is_planned_with_original_sell_cap(config):
    config["execution"]["sizing_policy"] = "bounded"
    config["risk"]["max_daily_sell_eur"] = "500"
    account = new_account("2000")
    account["portfolio"]["positions"]["SIM_GLOBAL_EQUITY"] = {
        "quantity": "30",
        "cost_eur": "3000.00",
    }
    market = {"prices": dict.fromkeys(config["assets"], "100")}
    result = plan_with_limits(account, market, dict.fromkeys(config["assets"], "0"), config, DAY)
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["side"] == "sell" and number(order["quantity"]) < 30
    assert money(number(order["quantity"]) * Decimal("99.95")) <= Decimal("500")
    assert result["constraints"][0]["status"] == "resized"


def test_sizing_never_waives_projected_exposure_check(config):
    config["execution"]["sizing_policy"] = "bounded"
    config["risk"]["max_daily_sell_eur"] = "500"
    account = new_account("500")
    account["portfolio"]["positions"]["SIM_GLOBAL_EQUITY"] = {
        "quantity": "45",
        "cost_eur": "4500.00",
    }
    before = deepcopy(account)
    market = {"prices": dict.fromkeys(config["assets"], "100")}
    with pytest.raises(SafetyError, match="projected_position_limit_exceeded"):
        plan_with_limits(account, market, dict.fromkeys(config["assets"], "0"), config, DAY)
    assert account == before


@pytest.mark.parametrize("policy", ["automatic", 1, None, {}])
def test_unrecognized_sizing_policy_is_rejected(config, policy):
    config["execution"]["sizing_policy"] = policy
    with pytest.raises(SafetyError, match="unsupported_sizing_policy"):
        validate_config(config)


def test_legacy_plan_interface_and_explicit_reject_match(config, events):
    account = new_account("5000")
    original = plan(account, events[0]["market"], weights(config), config, DAY)
    config["execution"]["sizing_policy"] = "reject"
    checked = plan_with_limits(account, events[0]["market"], weights(config), config, DAY)
    assert checked == {"orders": original, "constraints": []}


def test_sizing_policy_cannot_change_existing_paper_account(store, config, events):
    run_event(store, config, events[0])
    config["execution"]["sizing_policy"] = "bounded"
    before = store.read()
    with pytest.raises(SafetyError, match="state_config_mismatch"):
        run_event(store, config, events[1])
    assert store.read() == before
