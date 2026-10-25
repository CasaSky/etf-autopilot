"""Two fixed hypotheses. No fitting, model-selected assets or strategy promotion."""

from copy import deepcopy
from decimal import ROUND_DOWN, Decimal

from .domain import CENT, ZERO, SafetyError, instant, money, number, units
from .paper import apply_fill, equity


def validate_market(event, config):
    if set(event) != {"id", "at", "market", "operating_cost_eur"}:
        raise SafetyError("unsupported_event")
    if not isinstance(event["id"], str) or not 1 <= len(event["id"]) <= 128:
        raise SafetyError("invalid_event_id")
    now = instant(event["at"])
    market = event["market"]
    if set(market) != {"source", "as_of", "currency", "prices", "history"}:
        raise SafetyError("unsupported_market")
    if market["source"] != "synthetic_fixture" or market["currency"] != "EUR":
        raise SafetyError("fixture_eur_market_required")
    age = (now - instant(market["as_of"])).total_seconds()
    if age < 0 or Decimal(str(age)) > number(config["risk"]["max_data_age_hours"]) * 3600:
        raise SafetyError("stale_or_future_market")
    if set(market["prices"]) != set(config["assets"]):
        raise SafetyError("market_universe_mismatch")
    if any(number(price) <= ZERO for price in market["prices"].values()):
        raise SafetyError("nonpositive_price")
    if number(event["operating_cost_eur"]) < ZERO:
        raise SafetyError("negative_operating_cost")
    history = market["history"]
    if len(history) != config["lookback_months"]:
        raise SafetyError("insufficient_history")
    months = []
    for row in history:
        stamp = instant(row["at"])
        if stamp >= instant(market["as_of"]):
            raise SafetyError("lookahead_history")
        if set(row["prices"]) != set(config["assets"]):
            raise SafetyError("history_universe_mismatch")
        if any(number(price) <= ZERO for price in row["prices"].values()):
            raise SafetyError("invalid_history_price")
        months.append(stamp.year * 12 + stamp.month)
    current_month = now.year * 12 + now.month
    if months != list(range(current_month - config["lookback_months"], current_month)):
        raise SafetyError("nonconsecutive_monthly_history")


def allocation(market, config, strategy):
    result = {}
    for asset, metadata in config["assets"].items():
        mean = sum(number(row["prices"][asset]) for row in market["history"]) / len(
            market["history"]
        )
        signal_prices = market.get("signal_prices", market["prices"])
        on = strategy == "passive" or number(signal_prices[asset]) > mean
        result[asset] = metadata["weight"] if on else "0"
    return result


def plan(account, market, weights, config, day):
    """Compatibility interface returning only the fully checked feasible orders."""
    return plan_with_limits(account, market, weights, config, day)["orders"]


def plan_with_limits(account, market, weights, config, day):
    """Sells first; cash reserve and total fees constrain buys; floor fractional units."""
    portfolio = deepcopy(account["portfolio"])
    prices, risk, execution = market["prices"], config["risk"], config["execution"]
    capital = equity(portfolio, prices)
    if capital <= ZERO:
        raise SafetyError("nonpositive_equity")
    reserve = money(capital * number(risk["min_cash_weight"]))
    fee = money(execution["fee_eur"])
    half_spread = number(execution["spread_bps"]) / Decimal("20000")
    candidates = []
    for asset, weight in weights.items():
        mid = number(prices[asset])
        qty = number(portfolio["positions"].get(asset, {}).get("quantity", "0"))
        target = units(capital * number(weight) / mid)
        delta = target - qty
        difference = abs(delta * mid)
        if difference < max(
            number(risk["min_order_eur"]), capital * number(risk["rebalance_band_weight"])
        ):
            continue
        candidates.append(
            {"asset": asset, "side": "buy" if delta > ZERO else "sell", "quantity": str(abs(delta))}
        )
    # Rising holdings can push cash below its hard minimum even within the
    # rebalance band. Raise at least one minimum-size sale rather than weakening
    # the reserve or emitting an uneconomic tiny order.
    if number(portfolio["cash"]) < reserve and not any(
        order["side"] == "sell" for order in candidates
    ):
        held = sorted(
            portfolio["positions"],
            key=lambda a: number(portfolio["positions"][a]["quantity"]) * number(prices[a]),
            reverse=True,
        )
        for asset in held:
            price = number(prices[asset]) * (1 - half_spread)
            required = max(number(risk["min_order_eur"]), reserve - number(portfolio["cash"]) + fee)
            quantity = units(required / price) + Decimal("0.000001")
            if quantity <= number(portfolio["positions"][asset]["quantity"]):
                candidates = [order for order in candidates if order["asset"] != asset]
                candidates.append({"asset": asset, "side": "sell", "quantity": str(quantity)})
                break
    orders, constraints = [], []
    gross_volume = ZERO
    daily = deepcopy(account["daily_volume"].get(day, {"buy": "0", "sell": "0"}))
    for order in sorted(candidates, key=lambda o: (o["side"] != "sell", o["asset"])):
        side, asset = order["side"], order["asset"]
        price = number(prices[asset]) * (1 + half_spread if side == "buy" else 1 - half_spread)
        if price <= ZERO:
            raise SafetyError("nonpositive_execution_price")
        quantity = number(order["quantity"])
        if side == "buy":
            available = max(ZERO, number(portfolio["cash"]) - reserve - fee)
            quantity = min(quantity, units(available / price))
            # Rounding to cents must never overspend the cash reserve.
            while quantity > ZERO and money(quantity * price) + fee > available + fee:
                quantity -= Decimal("0.000001")
        note = None
        if execution.get("sizing_policy", "reject") == "bounded":
            remaining = {
                "daily_volume": max(
                    ZERO, number(risk[f"max_daily_{side}_eur"]) - number(daily[side])
                ),
                "turnover": max(ZERO, capital * number(risk["max_turnover_weight"]) - gross_volume),
            }
            # A cent-denominated cap cannot round a fill above a fractional-cent limit.
            budget = min(remaining.values()).quantize(CENT, rounding=ROUND_DOWN)
            bounded = min(quantity, units(budget / price))
            if bounded < quantity:
                requested = money(quantity * price)
                note = {
                    "asset": asset,
                    "side": side,
                    "reasons": [name for name, value in remaining.items() if requested > value],
                    "requested_quantity": str(quantity),
                    "planned_quantity": str(bounded),
                    "remaining_notional_budget_eur": str(budget),
                    "status": "resized",
                }
                if not note["reasons"]:
                    note["reasons"] = ["notional_rounding"]
                constraints.append(note)
            quantity = bounded
        notional = money(quantity * price)
        if quantity <= ZERO or notional < number(risk["min_order_eur"]):
            if note is not None:
                note.update(planned_quantity="0", status="skipped_below_minimum_order")
            continue
        gross_volume += notional
        if gross_volume > capital * number(risk["max_turnover_weight"]):
            raise SafetyError("turnover_limit_exceeded")
        daily[side] = str(number(daily[side]) + notional)
        if number(daily[side]) > number(risk[f"max_daily_{side}_eur"]):
            raise SafetyError("daily_volume_limit_exceeded")
        order["quantity"] = str(quantity)
        apply_fill(
            portfolio, {**order, "notional": str(notional), "fee": str(fee), "spread_eur": "0"}
        )
        orders.append(order)
    final_equity = equity(portfolio, prices)
    if number(portfolio["cash"]) + Decimal("0.01") < (
        final_equity * number(risk["min_cash_weight"])
    ):
        raise SafetyError("cash_reserve_breached")
    equity_exposure = ZERO
    for asset, position in portfolio["positions"].items():
        value = number(position["quantity"]) * number(prices[asset])
        if value > final_equity * number(risk["max_position_weight"]):
            raise SafetyError("projected_position_limit_exceeded")
        if config["assets"][asset]["class"] == "equity":
            equity_exposure += value
    if equity_exposure > final_equity * number(risk["max_equity_weight"]):
        raise SafetyError("projected_equity_limit_exceeded")
    return {"orders": orders, "constraints": constraints}
