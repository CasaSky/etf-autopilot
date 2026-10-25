"""Locked, chronological evaluation of the handoff's existing fixed strategies.

No fitting, new hypotheses, live execution, AI allocation or automatic promotion.
An independent simulated account is used for each comparator and each window.
"""

import json
import sqlite3
import sys
from copy import deepcopy
from decimal import getcontext
from pathlib import Path

from .domain import ZERO, SafetyError, canonical, digest, instant, money, number, validate_config
from .history import config_for_dataset, period_number, validate_dataset
from .paper import accept_paper_quote, equity, make_quote, new_account, reconcile, record_fill
from .strategy import allocation, plan

WINDOWS = ("development", "validation", "holdout")
MAX_RESULT_BYTES = 5_000_000


def engine_fingerprint():
    import hashlib

    modules = ("domain.py", "history.py", "strategy.py", "paper.py", "evaluation.py")
    return digest(
        {
            "modules": {
                name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in modules
            },
            "python": list(sys.version_info[:2]),
            "decimal_precision": getcontext().prec,
            "decimal_rounding": getcontext().rounding,
        }
    )


def validate_protocol(protocol, dataset, base_config):
    validate_dataset(dataset)
    config = config_for_dataset(base_config, dataset)
    validate_config(config)
    required = {
        "version",
        "dataset_id",
        "config_hash",
        "engine_fingerprint",
        "windows",
        "operating_cost_per_observation_eur",
    }
    if set(protocol) != required or protocol["version"] != 1:
        raise SafetyError("unsupported_evaluation_protocol")
    if protocol["dataset_id"] != dataset["dataset_id"] or protocol["config_hash"] != digest(config):
        raise SafetyError("evaluation_dataset_or_config_changed")
    if protocol["engine_fingerprint"] != engine_fingerprint():
        raise SafetyError("evaluation_engine_changed_register_new_protocol")
    if set(protocol["windows"]) != set(WINDOWS):
        raise SafetyError("three_chronological_windows_required")
    cost = number(protocol["operating_cost_per_observation_eur"])
    if not ZERO <= cost <= number(config["operations"]["monthly_budget_eur"]):
        raise SafetyError("evaluation_operating_cost_budget_exceeded")
    if config["operations"]["max_events_per_month"] < 1:
        raise SafetyError("evaluation_event_budget_exhausted")
    available = {
        instant(row["execution_at"]).strftime("%Y-%m"): index
        for index, row in enumerate(dataset["rows"])
    }
    previous_end = -1
    for name in WINDOWS:
        window = protocol["windows"][name]
        if set(window) != {"start", "end"}:
            raise SafetyError("unsupported_evaluation_window")
        start, end = period_number(window["start"]), period_number(window["end"])
        if start > end or start <= previous_end:
            raise SafetyError("overlapping_or_nonchronological_windows")
        if window["start"] not in available or window["end"] not in available:
            raise SafetyError("evaluation_window_outside_dataset")
        if available[window["start"]] < config["lookback_months"]:
            raise SafetyError("evaluation_window_missing_warmup")
        if available[window["end"]] - available[window["start"]] < 1:
            raise SafetyError("evaluation_window_requires_two_valuations")
        previous_end = end
    return config


def protocol_template(dataset, base_config, windows, operating_cost="0.01"):
    validate_dataset(dataset)
    result = {
        "version": 1,
        "dataset_id": dataset["dataset_id"],
        "config_hash": digest(config_for_dataset(base_config, dataset)),
        "engine_fingerprint": engine_fingerprint(),
        "windows": deepcopy(windows),
        "operating_cost_per_observation_eur": operating_cost,
    }
    validate_protocol(result, dataset, base_config)
    return result


def _simulate_account(dataset, config, protocol, window_name, role):
    window = protocol["windows"][window_name]
    selected = [
        (index, row)
        for index, row in enumerate(dataset["rows"])
        if window["start"] <= instant(row["execution_at"]).strftime("%Y-%m") <= window["end"]
    ]
    account = new_account(config["starting_cash"])
    capital, costs = number(config["starting_cash"]), ZERO
    halt, decisions = None, []
    strategy = "passive" if role == "passive" else config["strategy"]
    for observation, (index, row) in enumerate(selected):
        prices, at = row["executions"], row["execution_at"]
        day = instant(at).date().isoformat()
        if role != "passive":
            costs += number(protocol["operating_cost_per_observation_eur"])
        current = equity(account["portfolio"], prices) - money(costs)
        peak = max(number(account["peak_nav"]), current / capital)
        current_drawdown = max(ZERO, 1 - current / capital / peak)
        final_mark = observation == len(selected) - 1
        entry = {
            "at": at,
            "signal_at": row["signal_at"],
            "available_at": row["available_at"],
            "period": row["period"],
            "status": "valuation_only" if final_mark else "hold",
            "targets": {},
            "fills": [],
        }
        if halt is None and current_drawdown > number(config["risk"]["max_drawdown"]):
            halt = "drawdown_limit_exceeded"
        if not final_mark and halt is None:
            try:
                reconcile(account, capital)
                history = dataset["rows"][index - config["lookback_months"] : index]
                market = {
                    "prices": prices,
                    "signal_prices": row["signals"],
                    "history": [
                        {"at": item["signal_at"], "prices": item["signals"]} for item in history
                    ],
                }
                entry["targets"] = allocation(market, config, strategy)
                orders = plan(account, market, entry["targets"], config, day)
                for order_index, order in enumerate(orders):
                    intent_id = digest(
                        {
                            "dataset": dataset["dataset_id"],
                            "config": digest(config),
                            "window": window_name,
                            "role": role,
                            "at": at,
                            "order": order_index,
                        }
                    )
                    intent = {**order, "id": intent_id, "status": "SUBMITTING"}
                    account["intents"][intent_id] = intent
                    quote = make_quote(
                        account, intent, prices[order["asset"]], config["execution"], at
                    )
                    fill = accept_paper_quote(account, quote, at)
                    record_fill(account, fill)
                    intent["status"] = fill["status"]
                    volume = account["daily_volume"].setdefault(day, {"buy": "0", "sell": "0"})
                    volume[fill["side"]] = str(
                        number(volume[fill["side"]]) + number(fill["notional"])
                    )
                    entry["fills"].append(fill["id"])
                reconcile(account, capital)
                entry["status"] = "traded" if orders else "hold"
            except SafetyError as exc:
                halt = str(exc)
        if halt:
            entry.update(status="halted_valuation_only", diagnosis=halt)
        # Continue valuing existing holdings after a risk stop. Do not erase later losses.
        gross = equity(account["portfolio"], prices)
        net = gross - money(costs)
        nav = net / capital
        peak = max(number(account["peak_nav"]), nav)
        drawdown = max(ZERO, 1 - nav / peak)
        if halt is None and drawdown > number(config["risk"]["max_drawdown"]):
            halt = "drawdown_limit_exceeded"
            entry.update(
                status="halted_after_execution" if entry["fills"] else "halted_valuation_only",
                diagnosis=halt,
            )
        account["peak_nav"] = str(peak)
        account["max_drawdown"] = str(max(number(account["max_drawdown"]), drawdown))
        entry.update(gross_equity_eur=str(gross), net_equity_eur=str(net), drawdown=str(drawdown))
        account["marks"].append({"at": at, "net_equity_eur": str(net), "drawdown": str(drawdown)})
        decisions.append(entry)
    prices = selected[-1][1]["executions"]
    portfolio = account["portfolio"]
    net = equity(portfolio, prices) - money(costs)
    positions = [
        {
            "asset": asset,
            **position,
            "market_value_eur": str(money(number(position["quantity"]) * number(prices[asset]))),
        }
        for asset, position in portfolio["positions"].items()
    ]
    fills = [entry for entry in account["ledger"] if entry["type"] == "fill"]
    snapshot = {
        "strategy": strategy,
        "starting_capital_eur": str(money(capital)),
        "first_valuation_at": selected[0][1]["execution_at"],
        "last_valuation_at": selected[-1][1]["execution_at"],
        "valuations": len(selected),
        "net_equity_eur": str(net),
        "net_pnl_eur": str(net - capital),
        "return_percent": str((net / capital - 1) * 100),
        "max_drawdown_percent": str(number(account["max_drawdown"]) * 100),
        "cash_eur": portfolio["cash"],
        "fees_eur": portfolio["fees"],
        "spread_eur": portfolio["spread"],
        "operations_eur": str(money(costs)),
        "orders": len(fills),
        "turnover_eur": str(sum(number(f["notional"]) for f in fills)),
        "realized_pnl_eur": portfolio["realized_pnl"],
        "positions": positions,
        "halt": halt,
        "reconciliation": reconcile(account, capital)["status"],
    }
    return {"summary": snapshot, "decisions": decisions, "account": account}


def evaluate_window(dataset, base_config, protocol, window_name):
    config = validate_protocol(protocol, dataset, base_config)
    if window_name not in WINDOWS:
        raise SafetyError("unsupported_evaluation_window")
    accounts = {
        role: _simulate_account(dataset, config, protocol, window_name, role)
        for role in ("strategy", "passive", "without_ai")
    }
    strategy, passive, without_ai = [
        accounts[role]["summary"] for role in ("strategy", "passive", "without_ai")
    ]
    return {
        "version": 1,
        "protocol_id": digest(protocol),
        "dataset_id": dataset["dataset_id"],
        "config_hash": digest(config),
        "engine_fingerprint": protocol["engine_fingerprint"],
        "window": window_name,
        "origin": dataset["manifest"]["origin"],
        "price_basis": "unitized_total_return",
        "valuation_start": strategy["first_valuation_at"],
        "valuation_end": strategy["last_valuation_at"],
        "model_calls": 0,
        "ai_effect_tested": False,
        "accounts": accounts,
        "strategy_minus_passive_eur": str(
            money(strategy["net_equity_eur"]) - money(passive["net_equity_eur"])
        ),
        "strategy_minus_without_ai_eur": str(
            money(strategy["net_equity_eur"]) - money(without_ai["net_equity_eur"])
        ),
        "automatic_promotion": False,
        "limitations": [
            "Provider declarations and instrument metadata are not independently verified.",
            "Total-return index units are research proxies, not executable ETF share prices.",
            "Indices assume reinvested distributions; investor taxes are excluded.",
            "Cash earns zero; fees/spread and operating costs are modeled assumptions.",
            "Monthly observation drawdown omits intramonth losses and real fill uncertainty.",
            "Each window restarts with the same capital; the last observation is valuation only.",
            "No AI allocation was evaluated and no strategy is promoted automatically.",
        ],
    }


class EvaluationRegistry:
    """Local preregistration and holdout-exposure audit, across protocols and data revisions.

    This records usage in this registry. It cannot prove that a human has never
    inspected the input data, or prevent somebody from deleting/copying the registry.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS protocols (id TEXT PRIMARY KEY, body TEXT)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, protocol_id TEXT, "
                "window TEXT, status TEXT, result TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS exposures (run_id TEXT PRIMARY KEY, "
                "universe TEXT, start_month INTEGER, end_month INTEGER)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=20)

    def register(self, protocol, dataset, config):
        validate_protocol(protocol, dataset, config)
        protocol_id, body = digest(protocol), canonical(protocol)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO protocols (id,body) VALUES (?,?)", (protocol_id, body)
            )
            # Backfill any interrupted/older run's exposure without erasing results.
            universe = digest(
                sorted(item["isin"] for item in dataset["manifest"]["instruments"].values())
            )
            for run_id, window in conn.execute(
                "SELECT id,window FROM runs WHERE protocol_id=?", (protocol_id,)
            ):
                selected = protocol["windows"][window]
                conn.execute(
                    "INSERT OR IGNORE INTO exposures VALUES (?,?,?,?)",
                    (
                        run_id,
                        universe,
                        period_number(selected["start"]),
                        period_number(selected["end"]),
                    ),
                )
        return {"protocol_id": protocol_id, "status": "registered", "automatic_promotion": False}

    def run(self, protocol, dataset, config, window):
        validate_protocol(protocol, dataset, config)
        if window not in WINDOWS:
            raise SafetyError("unsupported_evaluation_window")
        protocol_id = digest(protocol)
        run_id = digest({"protocol_id": protocol_id, "window": window})
        # ISINs rather than dataset hashes prevent a CSV correction/new hash from
        # making an already exposed holdout appear fresh inside this registry.
        universe = digest(
            sorted(item["isin"] for item in dataset["manifest"]["instruments"].values())
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            saved = conn.execute("SELECT body FROM protocols WHERE id=?", (protocol_id,)).fetchone()
            if not saved or saved[0] != canonical(protocol):
                raise SafetyError("register_protocol_before_evaluation")
            prior = conn.execute("SELECT status,result FROM runs WHERE id=?", (run_id,)).fetchone()
            if prior and prior[0] == "completed":
                return {**json.loads(prior[1]), "cached": True}
            completed = {
                row[0]
                for row in conn.execute(
                    "SELECT window FROM runs WHERE protocol_id=? AND status='completed'",
                    (protocol_id,),
                )
            }
            if window == "validation" and "development" not in completed:
                raise SafetyError("complete_development_before_validation")
            selected = protocol["windows"][window]
            start, end = period_number(selected["start"]), period_number(selected["end"])
            if window == "holdout":
                if not {"development", "validation"} <= completed:
                    raise SafetyError("complete_development_and_validation_before_holdout")
                unmapped = conn.execute(
                    "SELECT r.id FROM runs r LEFT JOIN exposures e ON r.id=e.run_id "
                    "WHERE e.run_id IS NULL AND r.id!=? LIMIT 1",
                    (run_id,),
                ).fetchone()
                if unmapped:
                    raise SafetyError("legacy_exposure_requires_original_protocol_registration")
                exposed = conn.execute(
                    "SELECT run_id FROM exposures WHERE universe=? AND start_month<=? "
                    "AND end_month>=? AND run_id!=?",
                    (universe, end, start, run_id),
                ).fetchone()
                if exposed:
                    raise SafetyError("holdout_already_exposed_use_new_future_period")
            # All examined windows count as exposure, including development and
            # validation. Relabelling a previously examined window is not a holdout.
            conn.execute(
                "INSERT OR IGNORE INTO exposures VALUES (?,?,?,?)", (run_id, universe, start, end)
            )
            conn.execute(
                "INSERT OR IGNORE INTO runs VALUES (?,?,?,'reserved',NULL)",
                (run_id, protocol_id, window),
            )
            # Reserve the complete holdout BEFORE computation; a crash does not
            # make an examined window available to another modified protocol.
            conn.commit()
        result = {**evaluate_window(dataset, config, protocol, window), "run_id": run_id}
        encoded = canonical(result)
        if len(encoded.encode()) > MAX_RESULT_BYTES:
            raise SafetyError("evaluation_result_size_limit")
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status='completed',result=? WHERE id=?", (encoded, run_id)
            )
        return {**result, "cached": False}


def evaluation_markdown(result):
    fixture = result["origin"] == "synthetic_fixture"
    label = (
        "SYNTHETIC CONTRACT CHECK — no investment-performance evidence"
        if fixture
        else ("HISTORICAL RESEARCH SIMULATION — provider data not independently verified")
    )
    lines = [
        "# ETF strategy evaluation",
        "",
        f"**{label}.**",
        "",
        f"Window: {result['window']}; valuations: {result['valuation_start']} to "
        f"{result['valuation_end']}.",
        "",
        "Prices are unitized total-return research indices, not broker quotes.",
        "",
        "| Account | Net equity EUR | Net P&L EUR | Return | Max drawdown | Fees EUR | "
        "Spread EUR | Operations EUR | Orders | Halt |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in ("strategy", "passive", "without_ai"):
        account = result["accounts"][name]
        s = account["summary"]
        lines.append(
            f"| {name} | {s['net_equity_eur']} | {s['net_pnl_eur']} | "
            f"{number(s['return_percent']):.2f}% | "
            f"{number(s['max_drawdown_percent']):.2f}% | {s['fees_eur']} | "
            f"{s['spread_eur']} | {s['operations_eur']} | {s['orders']} | {s['halt'] or 'none'} |"
        )
    lines += [
        "",
        f"Strategy minus passive: EUR {result['strategy_minus_passive_eur']}. "
        f"Strategy minus without-AI: EUR {result['strategy_minus_without_ai_eur']}.",
        "",
        "Model calls: 0. AI allocation has not been tested. No automatic promotion.",
        "",
        f"Dataset: `{result['dataset_id']}`. Protocol: `{result['protocol_id']}`.",
        f"Engine: `{result['engine_fingerprint']}`.",
        "",
        "## Assumptions and limits",
        "",
    ]
    lines.extend(f"- {line}" for line in result["limitations"])
    return "\n".join(lines) + "\n"
