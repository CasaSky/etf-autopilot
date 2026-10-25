from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from etf_autopilot.domain import SafetyError, money, number
from etf_autopilot.engine import reconcile_state, run_event
from etf_autopilot.paper import equity
from etf_autopilot.report import markdown, summary


def test_monthly_slice_accounts_for_unsold_positions_and_compares_same_cashflows(
    store, config, events
):
    for event in events:
        result = run_event(store, config, event)
        assert result["status"] == "completed", result
    state = store.read()
    report = summary(state)
    assert report["comparable"]
    assert report["source"] == "synthetic_fixture"
    accounts = report["accounts"]
    assert accounts["strategy"]["net_pnl_eur"] == accounts["without_ai"]["net_pnl_eur"]
    assert accounts["strategy"]["valuation_dates"] == accounts["passive"]["valuation_dates"]
    assert accounts["strategy"]["orders"] >= 3
    assert accounts["strategy"]["reconciliation"] == "matched"
    assert money(accounts["strategy"]["broker_fees_eur"]) > 0
    assert money(accounts["strategy"]["spread_cost_eur"]) > 0
    assert number(accounts["strategy"]["max_drawdown_percent"]) > 0
    for account in accounts.values():
        unrealized = sum(money(p["unrealized_pnl_eur"]) for p in account["positions"])
        assert money(account["realized_pnl_eur"]) + unrealized - money(
            account["operations_eur"]
        ) == money(account["net_pnl_eur"])
    assert "No profitability claim" in markdown(state)


def test_duplicate_event_does_not_add_fills_fees_marks_or_cost(store, config, events):
    result = run_event(store, config, events[0])
    before = store.read()
    duplicate = run_event(store, config, events[0])
    assert duplicate == {**result, "duplicate": True}
    assert store.read() == before


def test_duplicate_payload_conflict_fails_closed(store, config, events):
    run_event(store, config, events[0])
    events[0]["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "101"
    before = store.read()
    with pytest.raises(SafetyError, match="event_id_payload_conflict"):
        run_event(store, config, events[0])
    assert store.read() == before


def test_concurrent_duplicate_delivery_commits_once(store, config, events):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_event(store, config, events[0]), range(2)))
    assert sorted(r["duplicate"] for r in results) == [False, True]
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == 2


def test_new_id_in_same_month_never_rebalances_twice(store, config, events):
    run_event(store, config, events[0])
    extra = deepcopy(events[0])
    extra.update(id="second-delivery-new-id", at="2026-08-24T10:00:00Z")
    result = run_event(store, config, extra)
    assert result["status"] == "skipped_monthly_cadence"
    assert result["orders"] == 0


@pytest.mark.parametrize("fault", ["unknown", "partial"])
def test_unknown_and_partial_require_reconciliation_never_resubmit(store, config, events, fault):
    result = run_event(store, config, events[0], fault=fault)
    assert result["status"] == "halted"
    before = store.read()
    assert len(before["accounts"]["strategy"]["broker"]["operations"]) == 1
    assert run_event(store, config, events[0])["duplicate"]
    assert reconcile_state(store, config)["status"] == "matched"
    recovered = store.read()
    assert len(recovered["accounts"]["strategy"]["broker"]["operations"]) == 1
    assert len(recovered["accounts"]["strategy"]["ledger"]) == 2
    assert reconcile_state(store, config)["status"] == "matched"
    assert len(store.read()["accounts"]["strategy"]["ledger"]) == 2
    assert run_event(store, config, events[0])["duplicate"]


def test_unknown_with_no_broker_evidence_remains_halted(store, config, events):
    run_event(store, config, events[0], fault="unresolved")
    assert reconcile_state(store, config) == {"status": "halted", "diagnosis": "unresolved_intent"}
    assert run_event(store, config, events[1])["status"] == "halted"
    assert store.read()["accounts"]["strategy"]["broker"]["operations"] == {}


def test_quote_expiry_never_fills_or_retries(store, config, events):
    result = run_event(store, config, events[0], fault="expired_quote")
    assert result["diagnosis"] == "quote_expired"
    assert store.read()["accounts"]["strategy"]["broker"]["operations"] == {}
    assert reconcile_state(store, config)["status"] == "matched"
    assert run_event(store, config, events[0])["duplicate"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("cash", "cash_reconciliation_failed"),
        ("holdings", "holdings_reconciliation_failed"),
        ("ledger", "ledger_replay_mismatch"),
    ],
)
def test_failed_reconciliation_blocks_all_further_orders(store, config, events, mutation, code):
    run_event(store, config, events[0])

    def corrupt(state):
        account = state["accounts"]["strategy"]
        if mutation == "cash":
            account["broker"]["portfolio"]["cash"] = "1.00"
        elif mutation == "holdings":
            account["broker"]["portfolio"]["positions"]["SIM_GLOBAL_EQUITY"]["quantity"] = "0"
        else:
            account["portfolio"]["cash"] = "1.00"
        return None, state

    store.update(corrupt)
    before = len(store.read()["accounts"]["strategy"]["broker"]["operations"])
    assert run_event(store, config, events[1])["diagnosis"] == code
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == before
    assert reconcile_state(store, config)["status"] == "halted"


@pytest.mark.parametrize(
    "case,code",
    [
        ("stale", "stale_or_future_market"),
        ("future", "stale_or_future_market"),
        ("nan", "non_finite_decimal"),
        ("negative", "nonpositive_price"),
        ("history", "nonconsecutive_monthly_history"),
        ("lookahead", "lookahead_history"),
        ("source", "fixture_eur_market_required"),
    ],
)
def test_invalid_data_never_places_orders(store, config, events, case, code):
    event = events[0]
    if case == "stale":
        event["market"]["as_of"] = "2026-08-20T10:00:00Z"
    elif case == "future":
        event["market"]["as_of"] = "2026-08-24T10:00:00Z"
    elif case == "nan":
        event["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "NaN"
    elif case == "negative":
        event["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "-1"
    elif case == "history":
        event["market"]["history"][0] = deepcopy(event["market"]["history"][1])
    elif case == "lookahead":
        event["market"]["history"][0]["at"] = event["at"]
    elif case == "source":
        event["market"]["source"] = "live"
    result = run_event(store, config, event)
    assert result["diagnosis"] == code
    assert all(not account["broker"]["operations"] for account in store.read()["accounts"].values())


def test_risk_limit_rejects_whole_plan_before_any_order(store, config, events):
    config["risk"]["max_daily_buy_eur"] = "2000"
    result = run_event(store, config, events[0])
    assert result["diagnosis"] == "daily_volume_limit_exceeded"
    assert all(not a["broker"]["operations"] for a in store.read()["accounts"].values())


def test_drawdown_halts_without_silently_selling(store, config, events):
    run_event(store, config, events[0])
    events[1]["market"]["prices"]["SIM_GLOBAL_EQUITY"] = "40"
    result = run_event(store, config, events[1])
    assert result["diagnosis"] == "drawdown_limit_exceeded"
    assert len(store.read()["accounts"]["strategy"]["broker"]["operations"]) == 2


def test_cash_reserve_is_maintained_when_prices_rise_within_rebalance_band(store, config, events):
    for event in events[:2]:
        assert run_event(store, config, event)["status"] == "completed"
        portfolio = store.read()["accounts"]["strategy"]["portfolio"]
        assert number(portfolio["cash"]) >= equity(portfolio, event["market"]["prices"]) * number(
            config["risk"]["min_cash_weight"]
        )


def test_config_cannot_change_under_existing_account(store, config, events):
    run_event(store, config, events[0])
    config["risk"]["max_drawdown"] = "0.9"
    with pytest.raises(SafetyError, match="state_config_mismatch"):
        run_event(store, config, events[1])


def test_live_mode_is_not_configurable(store, config, events):
    config["mode"] = "live"
    with pytest.raises(SafetyError, match="paper_eur_only"):
        run_event(store, config, events[0])


def test_ops_cost_budget_stops_before_order(store, config, events):
    events[0]["operating_cost_eur"] = "1.01"
    result = run_event(store, config, events[0])
    assert result["diagnosis"] == "operating_budget_exhausted"
    assert result["orders"] == 0


def test_interrupted_workflow_report_does_not_claim_valid_strategy_comparison(
    store, config, events
):
    run_event(store, config, events[0], fault="unknown")
    report = summary(store.read())
    assert report["comparable"] is False
    assert report["accounts"]["strategy"]["reconciliation"] != "matched"


def test_reconciliation_compares_operation_contents_not_only_identifiers(store, config, events):
    run_event(store, config, events[0])

    def corrupt(state):
        operation = next(iter(state["accounts"]["strategy"]["broker"]["operations"].values()))
        operation["fee"] = "99"
        return None, state

    store.update(corrupt)
    assert reconcile_state(store, config)["diagnosis"] == "operation_content_mismatch"


def test_out_of_order_event_cannot_rewind_valuation(store, config, events):
    run_event(store, config, events[0])
    run_event(store, config, events[1])
    last_market = store.read()["last_market"]
    older = deepcopy(events[0])
    older["id"] = "old-delivery-new-id"
    result = run_event(store, config, older)
    assert result["diagnosis"] == "nonmonotonic_event"
    assert store.read()["last_market"] == last_market


def test_report_flags_reconciliation_failure_before_explicit_recovery(store, config, events):
    run_event(store, config, events[0])

    def corrupt(state):
        state["accounts"]["strategy"]["broker"]["portfolio"]["cash"] = "1.00"
        return None, state

    store.update(corrupt)
    report = summary(store.read())
    assert report["halt"] == "cash_reconciliation_failed"
    assert report["comparable"] is False
