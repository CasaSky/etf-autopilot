"""Event-driven deterministic paper workflow with durable intent/outcome handling."""

from copy import deepcopy
from datetime import timedelta

from .domain import ZERO, SafetyError, digest, instant, money, number, validate_config
from .paper import (
    accept_paper_quote,
    equity,
    make_quote,
    new_account,
    reconcile,
    record_fill,
)
from .strategy import allocation, plan, validate_market

ACCOUNTS = ("strategy", "passive", "without_ai")


def initial_state(config):
    return {
        "version": 1,
        "mode": "paper",
        "config_hash": digest(config),
        "config": deepcopy(config),
        "source": "synthetic_fixture",
        "halt": None,
        "accounts": {name: new_account(config["starting_cash"]) for name in ACCOUNTS},
        "events": {},
        "slots": {},
        "last_at": None,
        "last_market": None,
        "operations": {"months": {}, "diagnoses": []},
        "audit": [],
    }


def assert_bound(state, config):
    if state["version"] != 1 or state["mode"] != "paper" or state["config_hash"] != digest(config):
        raise SafetyError("state_config_mismatch_new_evaluation_required")


def mark(account, event, starting_cash, operating_cost):
    gross = equity(account["portfolio"], event["market"]["prices"])
    net = gross - money(operating_cost)
    nav = net / number(starting_cash)
    peak = max(number(account["peak_nav"]), nav)
    drawdown = max(ZERO, 1 - nav / peak)
    account["peak_nav"] = str(peak)
    account["max_drawdown"] = str(max(number(account["max_drawdown"]), drawdown))
    row = {
        "event_id": event["id"],
        "at": event["at"],
        "equity_eur": str(gross),
        "net_equity_eur": str(net),
        "drawdown": str(drawdown),
    }
    account["marks"].append(row)


def operations_total(state):
    return sum(number(row["spend_eur"]) for row in state["operations"]["months"].values())


def account_operations(state, name):
    if name == "passive":
        return ZERO
    total = operations_total(state)
    if name == "without_ai":
        total -= sum(
            number(row.get("inference_spend_eur", "0"))
            for row in state["operations"]["months"].values()
        )
    return total


def run_event(store, config, event, *, fault=None):
    """fault is test-only paper simulation; never read from an event or model output."""
    validate_config(config)
    if "data_contract" in config:
        raise SafetyError("historical_contract_requires_evaluation_runner")
    if fault not in {None, "unknown", "partial", "expired_quote", "unresolved"}:
        raise SafetyError("unsupported_paper_fault")
    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
        raise SafetyError("invalid_event_id")
    fingerprint = digest(event)

    def transition(existing):
        state = existing if existing is not None else initial_state(config)
        assert_bound(state, config)
        prior = state["events"].get(event["id"])
        if prior:
            if prior["hash"] != fingerprint:
                raise SafetyError("event_id_payload_conflict")
            return {**prior["result"], "duplicate": True}, state
        result = {
            "event_id": event["id"],
            "mode": "paper",
            "status": "completed",
            "orders": 0,
            "diagnosis": None,
            "duplicate": False,
        }
        valid_market = False
        try:
            try:
                validate_market(event, config)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise SafetyError("malformed_market_event") from None
            if state["last_at"] and instant(event["at"]) <= instant(state["last_at"]):
                raise SafetyError("nonmonotonic_event")
            valid_market = True
            state["last_at"] = event["at"]
            if state["halt"]:
                raise SafetyError("account_halted_reconcile_required")
            month = instant(event["at"]).strftime("%Y-%m")
            day = instant(event["at"]).date().isoformat()
            limits = config["operations"]
            bucket = state["operations"]["months"].setdefault(
                month, {"events": 0, "spend_eur": "0", "inference_spend_eur": "0", "model_calls": 0}
            )
            if bucket["events"] >= limits["max_events_per_month"]:
                raise SafetyError("event_budget_exhausted")
            bucket["events"] += 1
            projected_cost = number(bucket["spend_eur"]) + number(event["operating_cost_eur"])
            if projected_cost > number(limits["monthly_budget_eur"]):
                raise SafetyError("operating_budget_exhausted")
            bucket["spend_eur"] = str(projected_cost)
            for account in state["accounts"].values():
                reconcile(account, config["starting_cash"])
            if month in state["slots"]:
                result["status"] = "skipped_monthly_cadence"
            else:
                plans = {}
                for name, account in state["accounts"].items():
                    cost = account_operations(state, name)
                    current_nav = (
                        equity(account["portfolio"], event["market"]["prices"]) - cost
                    ) / number(config["starting_cash"])
                    peak = max(number(account["peak_nav"]), current_nav)
                    if 1 - current_nav / peak > number(config["risk"]["max_drawdown"]):
                        raise SafetyError("drawdown_limit_exceeded")
                    strategy = "passive" if name == "passive" else config["strategy"]
                    targets = allocation(event["market"], config, strategy)
                    plans[name] = plan(account, event["market"], targets, config, day)
                # Reserve the month before any submission, including uncertain/partial outcomes.
                state["slots"][month] = event["id"]
                state["last_at"] = event["at"]
                for name, account in state["accounts"].items():
                    for index, order in enumerate(plans[name]):
                        intent_id = f"{name}:{event['id']}:{index}:{order['asset']}:{order['side']}"
                        intent = {**order, "id": intent_id, "status": "SUBMITTING"}
                        account["intents"][intent_id] = intent
                        quote = make_quote(
                            account,
                            intent,
                            event["market"]["prices"][order["asset"]],
                            config["execution"],
                            event["at"],
                        )
                        outcome = fault if name == "strategy" and index == 0 else None
                        accepted_at = event["at"]
                        if outcome == "expired_quote":
                            accepted_at = (
                                instant(quote["expires_at"]) + timedelta(seconds=1)
                            ).isoformat()
                        try:
                            if outcome == "unresolved":
                                raise SafetyError("broker_outcome_unknown")
                            fill = accept_paper_quote(account, quote, accepted_at, outcome)
                            record_fill(account, fill)
                            intent["status"] = fill["status"]
                            result["orders"] += 1
                            volume = account["daily_volume"].setdefault(
                                day, {"buy": "0", "sell": "0"}
                            )
                            side = order["side"]
                            volume[side] = str(number(volume[side]) + number(fill["notional"]))
                            if fill["status"] == "PARTIAL":
                                raise SafetyError("partial_fill_reconcile_required")
                        except SafetyError as exc:
                            if str(exc) == "broker_outcome_unknown":
                                intent["status"] = "UNKNOWN"
                            elif str(exc) == "quote_expired":
                                intent["status"] = "REJECTED"
                            raise
                for account in state["accounts"].values():
                    reconcile(account, config["starting_cash"])
        except SafetyError as exc:
            result.update(status="halted", diagnosis=str(exc))
            if str(exc) != "account_halted_reconcile_required":
                state["halt"] = str(exc)
        if valid_market:
            # Unconfirmed broker executions do not become confirmed ledger equity.
            # Report flags these valuations until an explicit reconciliation succeeds.
            for name, account in state["accounts"].items():
                cost = account_operations(state, name)
                mark(account, event, config["starting_cash"], cost)
            state["last_market"] = deepcopy(event["market"])
        state["events"][event["id"]] = {"hash": fingerprint, "result": deepcopy(result)}
        state["audit"].append({"type": "event", **result})
        return result, state

    return store.update(transition)


def reconcile_state(store, config):
    validate_config(config)

    def transition(state):
        if state is None:
            raise SafetyError("state_not_initialized")
        assert_bound(state, config)
        results = {}
        try:
            for name, account in state["accounts"].items():
                results[name] = reconcile(account, config["starting_cash"], recover=True)
                # Rebuild daily consumption from broker operations, including recovered fills.
                volume = {}
                for fill in account["broker"]["operations"].values():
                    day = instant(fill["at"]).date().isoformat()
                    daily = volume.setdefault(day, {"buy": "0", "sell": "0"})
                    daily[fill["side"]] = str(
                        number(daily[fill["side"]]) + number(fill["notional"])
                    )
                account["daily_volume"] = volume
            state["halt"] = None
            result = {"status": "matched", "accounts": results}
        except SafetyError as exc:
            state["halt"] = str(exc)
            result = {"status": "halted", "diagnosis": str(exc)}
        state["audit"].append({"type": "reconciliation", **result})
        return result, state

    return store.update(transition)
