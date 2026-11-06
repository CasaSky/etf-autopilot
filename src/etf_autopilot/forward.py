"""Causal paper research: commit targets now, simulate only a later observed NAV.

This runner uses index units, not broker shares. It never contacts a broker/model,
accepts a real quote, or turns a historical dataset into a forward execution.
"""

import calendar
import hashlib
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import getcontext
from pathlib import Path

from .domain import ZERO, SafetyError, digest, instant, money, number, validate_config
from .engine import ACCOUNTS, account_operations, execute_paper_plans, mark
from .forward_selection import reserve_choice, verify_choice
from .observations import validate_observations
from .paper import equity, new_account, reconcile
from .selection import SYSTEM, decision_context, validate_provider_options
from .strategy import allocation, plan_with_limits

MODE = "forward_research_paper"


def trial_engine_fingerprint():
    names = (
        "forward.py",
        "forward_selection.py",
        "selection.py",
        "engine.py",
        "domain.py",
        "paper.py",
        "strategy.py",
        "observations.py",
        "history.py",
        "nav.py",
    )
    return digest(
        {
            "modules": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in names
            },
            "python": list(sys.version_info[:2]),
            "decimal_precision": getcontext().prec,
            "decimal_rounding": getcontext().rounding,
        }
    )


def _identity(data):
    manifest = data["manifest"]
    return digest(
        {
            "origin": manifest["origin"],
            "version": manifest["version"],
            "instruments": manifest["instruments"],
            "assumptions": manifest["assumptions"],
            "policy": data["policy"],
            "anchor_date": data["rows"][0]["date"],
            "sources": {
                asset: {
                    key: source.get(key)
                    for key in (
                        "fund_name",
                        "native_currency",
                        "source_format",
                        "metadata_basis",
                        "reviewed_share_class_launch_date",
                    )
                }
                for asset, source in manifest["provenance"]["sources"].items()
            },
            "fx_conversion": manifest["provenance"]["fx"]["conversion"],
        }
    )


def _initial(base, data, now):
    config = deepcopy(base)
    roles = {item["class"]: item for item in base["assets"].values()}
    config["assets"] = {
        asset: {
            **deepcopy(item),
            "weight": roles[item["class"]]["weight"],
            "source": "historical_total_return",
        }
        for asset, item in data["manifest"]["instruments"].items()
    }
    config["data_contract"] = {
        "dataset_id": data["observation_id"],
        "origin": data["manifest"]["origin"],
        "price_basis": "unitized_issuer_return_proxy",
        "broker_mapping": "unverified",
    }
    validate_config(config)
    return {
        "version": 1,
        "mode": MODE,
        "trial_started_at": now.isoformat(),
        "base_config_hash": digest(base),
        "config": config,
        "config_hash": digest(config),
        "engine_fingerprint": trial_engine_fingerprint(),
        "identity_hash": _identity(data),
        "origin": data["manifest"]["origin"],
        "accounts": {name: new_account(config["starting_cash"]) for name in ACCOUNTS},
        "operations": {"months": {}},
        "events": {},
        "decisions": {},
        "slots": {},
        "pending": None,
        "halt": None,
        "audit": [],
        "observed_count": 0,
        "observed_prefix_hash": digest([]),
        "last_received_at": None,
        "last_runtime_at": None,
        "last_prices": None,
        "last_value_date": None,
    }


def _bind(state, base, data):
    if (
        state["mode"] != MODE
        or state["version"] != 1
        or state["base_config_hash"] != digest(base)
        or state["config_hash"] != digest(state["config"])
    ):
        raise SafetyError("forward_trial_config_changed")
    if state["engine_fingerprint"] != trial_engine_fingerprint():
        raise SafetyError("forward_trial_engine_changed_review_required")
    if state["identity_hash"] != _identity(data):
        raise SafetyError("forward_trial_identity_or_basis_changed")


def _signal_market(data, config, now):
    current = now.year * 12 + now.month
    points = []
    for month in range(current - 11, current):
        year, number_month = divmod(month - 1, 12)
        number_month += 1
        period = f"{year:04d}-{number_month:02d}"
        rows = [row for row in data["rows"] if row["date"].startswith(period)]
        if (
            len(rows) < 10
            or calendar.monthrange(year, number_month)[1] - int(rows[-1]["date"][-2:]) > 4
        ):
            raise SafetyError("forward_completed_month_history_required")
        points.append(rows[-1])
    return {
        "prices": data["rows"][-1]["values"],
        "signal_prices": points[-1]["values"],
        "history": [{"at": row["date"], "prices": row["values"]} for row in points[:-1]],
    }, points[-1]["date"]


def _mark_row(state, row, data, now):
    event = {
        "id": data["observation_id"] + ":" + row["date"],
        "at": now.isoformat(),
        "market": {"prices": row["values"]},
    }
    for name, account in state["accounts"].items():
        mark(account, event, state["config"]["starting_cash"], account_operations(state, name))
        account["marks"][-1]["value_date"] = row["date"]
    state["last_prices"] = deepcopy(row["values"])
    state["last_value_date"] = row["date"]


def _execute(state, pending, row, data, now, result, fault):
    plan = pending["plan"]
    if digest(plan) != pending["plan_hash"] or plan["config_hash"] != state["config_hash"]:
        raise SafetyError("forward_committed_plan_changed")
    config = state["config"]
    selected = verify_choice(pending, config)
    targets = {**plan["targets"], "strategy": plan["choices"][selected]}
    plans = {}
    for name, account in state["accounts"].items():
        reconcile(account, config["starting_cash"])
        nav = (
            equity(account["portfolio"], row["values"]) - money(account_operations(state, name))
        ) / number(config["starting_cash"])
        peak = max(number(account["peak_nav"]), nav)
        if 1 - nav / peak > number(config["risk"]["max_drawdown"]):
            raise SafetyError("drawdown_limit_exceeded")
        checked = plan_with_limits(
            account, {"prices": row["values"]}, targets[name], config, row["date"]
        )
        plans[name] = checked["orders"]
        if checked["constraints"]:
            result.setdefault("execution_constraints", {})[name] = checked["constraints"]
    event = {
        "id": pending["id"],
        "at": now.isoformat(),
        "modeled_execution_date": row["date"],
        "market": {"prices": row["values"]},
    }
    # Terminal intent is persisted even if acknowledgement/partial simulation halts.
    pending.update(
        status="execution_halted",
        recorded_at=now.isoformat(),
        modeled_value_date=row["date"],
        execution_observation_id=data["observation_id"],
    )
    execute_paper_plans(state, config, event, plans, result, fault=fault)
    for account in state["accounts"].values():
        reconcile(account, config["starting_cash"])
    pending["status"] = "executed"
    state["pending"] = None
    result.update(status="executed", decision_id=pending["id"], modeled_value_date=row["date"])


def run_forward_cycle(
    store, base_config, data, *, now=None, operating_cost_eur="0.01", fault=None, selection=None
):
    """One finite deterministic cycle. Injected clocks/faults are for fixtures only."""
    validate_config(base_config)
    if "data_contract" in base_config:
        raise SafetyError("forward_base_config_required")
    validate_observations(data)
    now = instant((now or datetime.now(UTC)).isoformat())
    captured = instant(data["captured_at"])
    if captured > now:
        raise SafetyError("forward_future_capture")
    cost = number(operating_cost_eur)
    if cost < ZERO:
        raise SafetyError("negative_operating_cost")
    if fault not in {None, "unknown", "partial", "expired_quote", "unresolved"}:
        raise SafetyError("unsupported_paper_fault")
    if selection is not None:
        if not isinstance(selection, dict) or set(selection) != {
            "provider_name",
            "reserved_cost_eur",
        }:
            raise SafetyError("invalid_forward_selection_options")
        validate_provider_options(True, selection["provider_name"], selection["reserved_cost_eur"])
    fingerprint = digest(
        {
            "observation_id": data["observation_id"],
            "operating_cost_eur": str(cost),
            "selection": selection,
        }
    )

    def transition(existing):
        initial = existing is None
        state = existing if existing is not None else _initial(base_config, data, now)
        _bind(state, base_config, data)
        prior = state["events"].get(data["observation_id"])
        if prior:
            if prior["hash"] != fingerprint:
                raise SafetyError("forward_event_context_conflict")
            return {**prior["result"], "duplicate": True}, state
        count = state["observed_count"]
        if (
            len(data["rows"]) < count
            or digest(data["rows"][:count]) != state["observed_prefix_hash"]
        ):
            raise SafetyError("forward_observed_history_revised")
        if state["last_received_at"] and (
            captured < instant(state["last_received_at"]) or now < instant(state["last_runtime_at"])
        ):
            raise SafetyError("forward_nonmonotonic_clock")
        config = state["config"]
        month = now.strftime("%Y-%m")
        bucket = state["operations"]["months"].setdefault(
            month, {"events": 0, "spend_eur": "0", "inference_spend_eur": "0", "model_calls": 0}
        )
        budget_ok = bucket["events"] < config["operations"]["max_events_per_month"]
        if budget_ok:
            bucket["events"] += 1
        result = {
            "event_id": data["observation_id"],
            "mode": MODE,
            "status": "observed",
            "orders": 0,
            "duplicate": False,
            "diagnosis": None,
        }
        latest = data["rows"][-1]
        age = (now - instant(latest["date"] + "T00:00:00Z")).total_seconds()
        stale = (
            number(str(age)) > number(config["risk"]["max_data_age_hours"]) * 3600
            or number(str((now - captured).total_seconds()))
            > number(config["risk"]["max_data_age_hours"]) * 3600
        )
        try:
            for account in state["accounts"].values():
                reconcile(account, config["starting_cash"])
        except SafetyError as exc:
            state["halt"] = state["halt"] or str(exc)
        new_rows = data["rows"][count:] if not initial else [latest]
        pending = state["decisions"].get(state["pending"])
        finished = False
        execution = None
        if pending and pending["status"] == "ready" and not state["halt"]:
            committed_at = max(instant(pending["plan"]["at"]), instant(pending["choice_at"]))
            if now - instant(pending["plan"]["at"]) > timedelta(days=7):
                pending["status"] = "expired"
                state["pending"] = None
                finished = True
                result.update(status="decision_expired", decision_id=pending["id"])
            elif not stale and budget_ok:
                execution = next(
                    (
                        row
                        for row in new_rows
                        if row["date"] > committed_at.date().isoformat()
                        and captured > committed_at
                        and number(str((now - instant(row["date"] + "T00:00:00Z")).total_seconds()))
                        <= number(config["risk"]["max_data_age_hours"]) * 3600
                    ),
                    None,
                )
        for row in new_rows:
            if execution is row and not state["halt"]:
                try:
                    _execute(state, pending, row, data, now, result, fault)
                except SafetyError as exc:
                    state["halt"] = str(exc)
                    pending["status"] = "execution_halted"
                    result.update(status="halted", diagnosis=str(exc), decision_id=pending["id"])
                finished = True
            _mark_row(state, row, data, now)
        if state["halt"]:
            result.update(status="halted", diagnosis=state["halt"])
        elif not budget_ok:
            result.update(status="waiting_for_input", diagnosis="event_budget_exhausted")
        elif not finished:
            if stale:
                result.update(
                    status="waiting_for_input", diagnosis="fresh_forward_observation_required"
                )
            elif pending:
                result.update(
                    status=(
                        "awaiting_selection"
                        if pending["status"] == "reserved"
                        else "waiting_for_future_observation"
                    ),
                    decision_id=pending["id"],
                )
            elif month in state["slots"]:
                result["status"] = "monthly_decision_already_committed"
            else:
                try:
                    market, signal_date = _signal_market(data, config, now)
                    if number(bucket["spend_eur"]) + cost > number(
                        config["operations"]["monthly_budget_eur"]
                    ):
                        raise SafetyError("operating_budget_exhausted")
                    targets = {
                        name: allocation(
                            market, config, "passive" if name == "passive" else config["strategy"]
                        )
                        for name in ACCOUNTS
                    }
                    decision_id = "forward-decision:" + month
                    plan = {
                        "at": now.isoformat(),
                        "config_hash": state["config_hash"],
                        "observation_id": data["observation_id"],
                        "capture_id": data["capture_id"],
                        "signal_date": signal_date,
                        "targets": targets,
                        "choices": {
                            name: allocation(market, config, name)
                            for name in ("passive", "slow_trend")
                        },
                        "context": decision_context(config, {"market": market}),
                        "system_hash": digest(SYSTEM),
                        "selection": deepcopy(selection),
                        "strategy": config["strategy"],
                        "requested_provider": selection["provider_name"] if selection else None,
                    }
                    decision = {
                        "id": decision_id,
                        "status": "ready",
                        "plan": plan,
                        "plan_hash": digest(plan),
                    }
                    bucket["spend_eur"] = str(number(bucket["spend_eur"]) + cost)
                    reserve_choice(decision, config, bucket, selection)
                    state["decisions"][decision_id] = decision
                    state["slots"][month] = decision_id
                    state["pending"] = (
                        None if decision["status"] == "selection_blocked" else decision_id
                    )
                    result.update(
                        status={
                            "ready": "decision_committed",
                            "reserved": "decision_reserved",
                            "selection_blocked": "selection_blocked",
                        }[decision["status"]],
                        decision_id=decision_id,
                    )
                    for key in ("failure_code", "inference_skipped"):
                        if key in decision:
                            result[key] = decision[key]
                except SafetyError as exc:
                    result.update(status="waiting_for_input", diagnosis=str(exc))
        state.update(
            observed_count=len(data["rows"]),
            observed_prefix_hash=digest(data["rows"]),
            last_received_at=captured.isoformat(),
            last_runtime_at=now.isoformat(),
        )
        state["events"][data["observation_id"]] = {"hash": fingerprint, "result": deepcopy(result)}
        state["audit"].append({"at": now.isoformat(), "type": "forward_cycle", **result})
        return result, state

    return store.update(transition)


def reconcile_forward(store):
    def transition(state):
        if state is None or state.get("mode") != MODE:
            raise SafetyError("forward_trial_required")
        try:
            for account in state["accounts"].values():
                reconcile(account, state["config"]["starting_cash"], recover=True)
        except SafetyError as exc:
            state["halt"] = str(exc)
            return {"status": "halted", "diagnosis": str(exc)}, state
        state["halt"] = None
        if (
            state["pending"]
            and state["decisions"][state["pending"]]["status"] == "execution_halted"
        ):
            state["pending"] = None
        result = {"status": "matched", "remainder_retry": False}
        state["audit"].append({"type": "forward_reconciled", **result})
        return result, state

    return store.update(transition)


def forward_summary(state):
    if state.get("mode") != MODE:
        raise SafetyError("forward_trial_required")
    capital = number(state["config"]["starting_cash"])
    accounts = {}
    for name, account in state["accounts"].items():
        try:
            status = reconcile(account, capital)["status"]
        except SafetyError as exc:
            status = str(exc)
        net = equity(account["portfolio"], state["last_prices"]) - money(
            account_operations(state, name)
        )
        peak = max(number(account["peak_nav"]), net / capital)
        accounts[name] = {
            "net_equity_eur": str(net),
            "return_percent": str((net / capital - 1) * 100),
            "cash_eur": account["portfolio"]["cash"],
            "fees_eur": account["portfolio"]["fees"],
            "spread_eur": account["portfolio"]["spread"],
            "operations_eur": str(money(account_operations(state, name))),
            "orders": sum(row["type"] == "fill" for row in account["ledger"]),
            "reconciliation": status,
            "max_drawdown_percent": str(
                max(number(account["max_drawdown"]), 1 - net / capital / peak) * 100
            ),
            "positions": deepcopy(account["portfolio"]["positions"]),
        }
    completed = any(row["status"] == "executed" for row in state["decisions"].values())
    interrupted = any(
        row["status"]
        in {
            "execution_halted",
            "expired",
            "selection_blocked",
            "selection_failed",
            "selection_abandoned",
        }
        for row in state["decisions"].values()
    )
    return {
        "mode": MODE,
        "origin": state["origin"],
        "units": "research_index_units_not_broker_shares",
        "trial_started_at": state["trial_started_at"],
        "last_value_date": state["last_value_date"],
        "last_received_at": state["last_received_at"],
        "pending": state["pending"],
        "decisions": [
            {
                "id": decision["id"],
                "status": decision["status"],
                "selected_strategy": decision.get("selected_strategy"),
                "source": decision.get("source"),
                "committed_at": decision["plan"]["at"],
                "choice_at": decision.get("choice_at"),
                "reserved_cost_eur": decision.get("reserved_cost_eur", "0"),
                "failure_code": decision.get("failure_code"),
            }
            for decision in state["decisions"].values()
        ],
        "halt": state["halt"],
        "accounts": accounts,
        "model_calls": sum(
            bucket["model_calls"] for bucket in state["operations"]["months"].values()
        ),
        "comparable": completed
        and not interrupted
        and not state["halt"]
        and not any(row["status"] == "reserved" for row in state["decisions"].values())
        and all(row["reconciliation"] == "matched" for row in accounts.values()),
        "execution_policy": "committed_targets_at_first_later_fresh_nav_within_seven_days",
        "limitations": [
            "NAV dates and receipt times do not verify intraday publication or broker execution.",
            "Taxes excluded; fees, spread, cash yield and costs are research assumptions.",
            "Daily drawdown sampling differs from the monthly study; halts retain holdings.",
            "Model choices measure paper hypotheses only; historical results stay separate.",
        ],
    }


def forward_markdown(state):
    report = forward_summary(state)
    lines = [
        "# Forward paper research",
        "",
        "**Research index units. No broker execution or profitability claim.**",
        "",
        (
            "Fictional fixture workflow."
            if report["origin"] == "synthetic_fixture"
            else "Observed issuer/ECB data with modeled NAV execution."
        ),
        "",
        f"Trial started: {report['trial_started_at']}.",
        f"Latest value date: {report['last_value_date']}.",
        f"Latest source receipt: {report['last_received_at']}.",
        "",
        "| Account | Net EUR | Return | Drawdown | Fees EUR | Operations EUR | "
        "Orders | Reconciliation |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in ACCOUNTS:
        row = report["accounts"][name]
        lines.append(
            f"| {name} | {row['net_equity_eur']} | {number(row['return_percent']):.2f}% | "
            f"{number(row['max_drawdown_percent']):.2f}% | {row['fees_eur']} | "
            f"{row['operations_eur']} | {row['orders']} | {row['reconciliation']} |"
        )
    lines += [
        "",
        f"Pending: {report['pending'] or 'none'}. Halt: {report['halt'] or 'none'}.",
        f"Comparable completed workflow: {report['comparable']}.",
        f"Reserved model attempts (including unknown/failed): {report['model_calls']}.",
        "",
    ]
    lines += ["- " + value for value in report["limitations"]]
    return "\n".join(lines) + "\n"
