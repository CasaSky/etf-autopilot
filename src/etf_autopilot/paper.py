"""Paper broker state is separate from the portfolio ledger.

A lost acknowledgement leaves the fill in broker operations. Reconciliation
imports it by intent ID without resubmitting. No code here contacts a broker.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from .domain import CENT, ZERO, SafetyError, instant, money, number, units


def empty_portfolio(cash):
    return {
        "cash": str(money(cash)),
        "positions": {},
        "fees": "0.00",
        "spread": "0.00",
        "realized_pnl": "0.00",
    }


def new_account(cash):
    portfolio = empty_portfolio(cash)
    return {
        "portfolio": portfolio,
        "broker": {"portfolio": deepcopy(portfolio), "operations": {}, "active_quote": None},
        "ledger": [{"type": "deposit", "id": "initial-capital", "amount": str(money(cash))}],
        "intents": {},
        "marks": [],
        "daily_volume": {},
        "peak_nav": "1",
        "max_drawdown": "0",
    }


def equity(portfolio, prices):
    return money(
        number(portfolio["cash"])
        + sum(
            number(position["quantity"]) * number(prices[asset])
            for asset, position in portfolio["positions"].items()
        )
    )


def apply_fill(portfolio, fill):
    qty, value, fee = number(fill["quantity"]), money(fill["notional"]), money(fill["fee"])
    if qty <= ZERO or value <= ZERO or fee < ZERO:
        raise SafetyError("invalid_fill")
    asset = fill["asset"]
    position = portfolio["positions"].setdefault(asset, {"quantity": "0", "cost_eur": "0.00"})
    old_qty, old_cost = number(position["quantity"]), money(position["cost_eur"])
    cash = money(portfolio["cash"])
    if fill["side"] == "buy":
        if cash < value + fee:
            raise SafetyError("insufficient_cash")
        position.update(quantity=str(old_qty + qty), cost_eur=str(old_cost + value + fee))
        portfolio["cash"] = str(cash - value - fee)
    elif fill["side"] == "sell":
        if qty > old_qty:
            raise SafetyError("short_sale_blocked")
        removed_cost = old_cost if qty == old_qty else money(old_cost * qty / old_qty)
        position.update(quantity=str(old_qty - qty), cost_eur=str(old_cost - removed_cost))
        portfolio["cash"] = str(cash + value - fee)
        portfolio["realized_pnl"] = str(
            money(portfolio["realized_pnl"]) + value - fee - removed_cost
        )
    else:
        raise SafetyError("invalid_side")
    portfolio["fees"] = str(money(portfolio["fees"]) + fee)
    portfolio["spread"] = str(money(portfolio["spread"]) + money(fill["spread_eur"]))


def record_fill(account, fill):
    if any(row["id"] == fill["id"] for row in account["ledger"]):
        return
    apply_fill(account["portfolio"], fill)
    account["ledger"].append(deepcopy(fill))


def make_quote(account, intent, mid, execution, now):
    broker = account["broker"]
    active = broker["active_quote"]
    if active and instant(active["expires_at"]) > instant(now):
        raise SafetyError("one_active_quote_only")
    half_spread = number(execution["spread_bps"]) / Decimal("20000")
    multiplier = 1 + half_spread if intent["side"] == "buy" else 1 - half_spread
    price = number(mid) * multiplier
    quote = {
        "id": f"paper-quote:{intent['id']}",
        "intent_id": intent["id"],
        "asset": intent["asset"],
        "side": intent["side"],
        "quantity": intent["quantity"],
        "price": str(price),
        "mid": str(mid),
        "fee": execution["fee_eur"],
        "expires_at": (
            instant(now) + timedelta(seconds=execution["quote_ttl_seconds"])
        ).isoformat(),
    }
    broker["active_quote"] = quote
    return quote


def accept_paper_quote(account, quote, now, outcome="filled"):
    broker = account["broker"]
    if broker["active_quote"] != quote:
        raise SafetyError("unknown_quote")
    if instant(now) >= instant(quote["expires_at"]):
        broker["active_quote"] = None
        raise SafetyError("quote_expired")
    if quote["intent_id"] in broker["operations"]:
        raise SafetyError("intent_already_submitted")
    quantity = number(quote["quantity"])
    if outcome == "partial":
        quantity = units(quantity / 2)
    value = money(quantity * number(quote["price"]))
    fill = {
        "type": "fill",
        "id": f"paper-fill:{quote['intent_id']}",
        "intent_id": quote["intent_id"],
        "asset": quote["asset"],
        "side": quote["side"],
        "quantity": str(quantity),
        "price": quote["price"],
        "notional": str(value),
        "fee": quote["fee"],
        "spread_eur": str(money(abs(number(quote["price"]) - number(quote["mid"])) * quantity)),
        "at": now,
        "terminal": True,
        "status": "PARTIAL" if outcome == "partial" else "FILLED",
    }
    apply_fill(broker["portfolio"], fill)
    broker["operations"][quote["intent_id"]] = fill
    broker["active_quote"] = None
    if outcome == "unknown":
        # The simulated broker filled the order; its acknowledgement was lost.
        raise SafetyError("broker_outcome_unknown")
    return fill


def reconcile(account, starting_cash, recover=False):
    unresolved = []
    for intent_id, intent in account["intents"].items():
        if intent["status"] not in {"SUBMITTING", "UNKNOWN", "PARTIAL"}:
            continue
        fill = account["broker"]["operations"].get(intent_id)
        if fill and (
            fill["intent_id"] != intent_id
            or fill["asset"] != intent["asset"]
            or fill["side"] != intent["side"]
            or not ZERO < number(fill["quantity"]) <= number(intent["quantity"])
        ):
            raise SafetyError("fill_intent_mismatch")
        if recover and fill and fill["terminal"]:
            record_fill(account, fill)
            intent["status"] = "RECONCILED_PARTIAL" if fill["status"] == "PARTIAL" else "FILLED"
        else:
            unresolved.append(intent_id)
    # Replay the journal to detect ledger/snapshot corruption independently of holdings checks.
    replayed = empty_portfolio(starting_cash)
    seen = set()
    for row in account["ledger"]:
        if row["id"] in seen:
            raise SafetyError("duplicate_ledger_entry")
        seen.add(row["id"])
        if row["type"] == "fill":
            apply_fill(replayed, row)
    if replayed != account["portfolio"]:
        raise SafetyError("ledger_replay_mismatch")
    local, remote = account["portfolio"], account["broker"]["portfolio"]
    if abs(money(local["cash"]) - money(remote["cash"])) > CENT:
        raise SafetyError("cash_reconciliation_failed")
    for asset in set(local["positions"]) | set(remote["positions"]):
        lqty = number(local["positions"].get(asset, {}).get("quantity", "0"))
        rqty = number(remote["positions"].get(asset, {}).get("quantity", "0"))
        if lqty != rqty:
            raise SafetyError("holdings_reconciliation_failed")
    ledger_ids = {row["id"] for row in account["ledger"] if row["type"] == "fill"}
    operation_ids = {row["id"] for row in account["broker"]["operations"].values()}
    if ledger_ids != operation_ids:
        raise SafetyError("operation_reconciliation_failed")
    broker_rows = {row["id"]: row for row in account["broker"]["operations"].values()}
    if any(row != broker_rows[row["id"]] for row in account["ledger"] if row["type"] == "fill"):
        raise SafetyError("operation_content_mismatch")
    if unresolved:
        raise SafetyError("unresolved_intent")
    return {"status": "matched", "cash": local["cash"], "fills": len(operation_ids)}
