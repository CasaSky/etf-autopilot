"""Decimal-only financial primitives and strict, versioned paper configuration."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

ZERO = Decimal("0")
CENT = Decimal("0.01")
UNIT = Decimal("0.000001")


class SafetyError(Exception):
    """Fail closed with a stable code suitable for diagnosis, never raw upstream text."""


def number(value) -> Decimal:
    if isinstance(value, (float, bool)):
        raise SafetyError("decimal_string_required")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SafetyError("invalid_decimal") from exc
    if not result.is_finite():
        raise SafetyError("non_finite_decimal")
    return result


def money(value) -> Decimal:
    return number(value).quantize(CENT, rounding=ROUND_HALF_UP)


def units(value) -> Decimal:
    return number(value).quantize(UNIT, rounding=ROUND_DOWN)


def instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SafetyError("invalid_timestamp") from exc
    if result.tzinfo is None:
        raise SafetyError("timezone_required")
    return result.astimezone(UTC)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(path: str | Path):
    return json.loads(Path(path).read_text(), parse_float=str)


def validate_config(config: dict) -> None:
    expected = {
        "version",
        "mode",
        "currency",
        "starting_cash",
        "strategy",
        "lookback_months",
        "assets",
        "risk",
        "execution",
        "operations",
    }
    if set(config) not in (expected, expected | {"data_contract"}) or config["version"] != 1:
        raise SafetyError("unsupported_config")
    if config["mode"] != "paper" or config["currency"] != "EUR":
        raise SafetyError("paper_eur_only")
    if config["strategy"] not in {"passive", "slow_trend"} or config["lookback_months"] != 10:
        raise SafetyError("unvalidated_strategy")
    if number(config["starting_cash"]) <= ZERO:
        raise SafetyError("invalid_starting_cash")
    contract = config.get("data_contract")
    if contract is None:
        if set(config["assets"]) != {"SIM_GLOBAL_EQUITY", "SIM_EUR_BONDS"}:
            raise SafetyError("fixture_universe_only")
        source = "synthetic_fixture"
    else:
        if (
            set(contract) != {"dataset_id", "origin", "price_basis", "broker_mapping"}
            or not re.fullmatch(r"[0-9a-f]{64}", contract["dataset_id"])
            or contract["origin"] not in {"synthetic_fixture", "declared_historical"}
            or contract["price_basis"]
            not in {
                "unitized_total_return",
                "unitized_adjusted_close_proxy",
                "unitized_issuer_return_proxy",
            }
            or contract["broker_mapping"] != "unverified"
            or len(config["assets"]) != 2
            or {a["class"] for a in config["assets"].values()} != {"equity", "bond"}
        ):
            raise SafetyError("unsupported_historical_research_contract")
        source = "historical_total_return"
    for asset in config["assets"].values():
        if (asset["type"], asset["currency"], asset["ucits"], asset["source"]) != (
            "equity_security",
            "EUR",
            True,
            source,
        ):
            raise SafetyError("ineligible_asset")
        if number(asset["weight"]) <= ZERO or asset["class"] not in {"equity", "bond"}:
            raise SafetyError("invalid_asset_weight_or_class")
    risk = config["risk"]
    required_risk = {
        "min_cash_weight",
        "max_position_weight",
        "max_equity_weight",
        "max_drawdown",
        "max_turnover_weight",
        "max_daily_buy_eur",
        "max_daily_sell_eur",
        "min_order_eur",
        "max_data_age_hours",
        "max_spread_bps",
        "rebalance_band_weight",
    }
    if set(risk) != required_risk:
        raise SafetyError("unsupported_risk_config")
    for key in (
        "min_cash_weight",
        "max_position_weight",
        "max_equity_weight",
        "max_drawdown",
        "max_turnover_weight",
        "rebalance_band_weight",
    ):
        if not ZERO <= number(risk[key]) <= 1:
            raise SafetyError("invalid_risk_fraction")
    for key in required_risk - {
        "min_cash_weight",
        "max_position_weight",
        "max_equity_weight",
        "max_drawdown",
        "max_turnover_weight",
        "rebalance_band_weight",
    }:
        if number(risk[key]) <= ZERO:
            raise SafetyError("invalid_risk_limit")
    weights = [number(a["weight"]) for a in config["assets"].values()]
    if sum(weights) + number(risk["min_cash_weight"]) > 1:
        raise SafetyError("weights_exceed_capital")
    if any(w > number(risk["max_position_weight"]) for w in weights):
        raise SafetyError("position_weight_exceeded")
    equity_weight = sum(
        number(a["weight"]) for a in config["assets"].values() if a["class"] == "equity"
    )
    if equity_weight > number(risk["max_equity_weight"]):
        raise SafetyError("equity_weight_exceeded")
    execution = config["execution"]
    if set(execution) != {"fee_eur", "spread_bps", "quote_ttl_seconds"}:
        raise SafetyError("unsupported_execution_config")
    if number(execution["fee_eur"]) < ZERO or number(execution["spread_bps"]) < ZERO:
        raise SafetyError("negative_cost")
    if number(execution["spread_bps"]) > number(risk["max_spread_bps"]):
        raise SafetyError("spread_limit_exceeded")
    if not 1 <= execution["quote_ttl_seconds"] <= 300:
        raise SafetyError("invalid_quote_ttl")
    operations = config["operations"]
    if set(operations) != {
        "monthly_budget_eur",
        "max_model_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_events_per_month",
    }:
        raise SafetyError("unsupported_operations_config")
    if number(operations["monthly_budget_eur"]) < ZERO:
        raise SafetyError("negative_cost_budget")
    for key in set(operations) - {"monthly_budget_eur"}:
        if type(operations[key]) is not int or operations[key] < 0:
            raise SafetyError("invalid_operation_limit")
