"""Mark-to-market reports: unsold positions, fees and operations remain visible."""

from .domain import ZERO, SafetyError, money, number
from .engine import account_operations
from .operations import deterministic_diagnosis
from .paper import equity, reconcile


def summary(state):
    allocations = state["operations"].get("allocations", [])
    pending = sum(row["status"] == "reserved" for row in allocations)
    used = {
        event["allocation_id"]
        for event in state["events"].values()
        if event.get("allocation_id") and event["result"]["status"] == "completed"
    }
    choices = [row for row in allocations if row["id"] in used and row["source"] != "deterministic"]
    constrained = sum(
        bool(event["result"].get("execution_constraints")) for event in state["events"].values()
    )
    if state["last_market"] is None:
        return {
            "mode": "paper",
            "source": "synthetic_fixture",
            "halt": state["halt"],
            "diagnosis": deterministic_diagnosis(state["halt"]),
            "accounts": {},
            "pending_allocations": pending,
            "constrained_events": constrained,
        }
    prices = state["last_market"]["prices"]
    capital = number(state["config"]["starting_cash"])
    accounts = {}
    for name, account in state["accounts"].items():
        portfolio = account["portfolio"]
        gross = equity(portfolio, prices)
        costs = money(account_operations(state, name))
        net = gross - costs
        positions = []
        for asset, position in portfolio["positions"].items():
            value = money(number(position["quantity"]) * number(prices[asset]))
            positions.append(
                {
                    "asset": asset,
                    **position,
                    "market_value_eur": str(value),
                    "unrealized_pnl_eur": str(value - money(position["cost_eur"])),
                }
            )
        try:
            reconciliation = reconcile(account, capital)["status"]
        except SafetyError as exc:
            reconciliation = str(exc)
        peak = max(number(account["peak_nav"]), net / capital)
        drawdown = max(ZERO, 1 - net / capital / peak)
        fills = [row for row in account["ledger"] if row["type"] == "fill"]
        accounts[name] = {
            "cash_eur": portfolio["cash"],
            "equity_before_operations_eur": str(gross),
            "operations_eur": str(costs),
            "net_equity_eur": str(net),
            "net_pnl_eur": str(net - capital),
            "return_percent": str((net / capital - 1) * 100),
            "max_drawdown_percent": str(max(number(account["max_drawdown"]), drawdown) * 100),
            "broker_fees_eur": portfolio["fees"],
            "spread_cost_eur": portfolio["spread"],
            "realized_pnl_eur": portfolio["realized_pnl"],
            "orders": len(fills),
            "turnover_eur": str(sum(number(fill["notional"]) for fill in fills)),
            "reconciliation": reconciliation,
            "positions": positions,
            "valuation_dates": [row["at"] for row in account["marks"]],
        }
    successful = all(
        row["result"]["status"] in {"completed", "skipped_monthly_cadence"}
        for row in state["events"].values()
    )
    effective_halt = state["halt"] or next(
        (
            account["reconciliation"]
            for account in accounts.values()
            if account["reconciliation"] != "matched"
        ),
        None,
    )
    return {
        "mode": "paper",
        "source": "synthetic_fixture",
        "halt": effective_halt,
        "as_of": state["last_market"]["as_of"],
        "config_hash": state["config_hash"],
        "external_cash_flows_eur": str(capital),
        "cash_flow_policy": "initial_deposit_only",
        "diagnosis": deterministic_diagnosis(effective_halt),
        "accounts": accounts,
        "pending_allocations": pending,
        "constrained_events": constrained,
        "allocation_choices": [
            {key: row[key] for key in ("event_id", "source", "strategy", "reserved_cost_eur")}
            for row in choices
        ],
        "comparable": successful
        and not pending
        and not state["halt"]
        and all(a["reconciliation"] == "matched" for a in accounts.values()),
        "limitations": [
            "Synthetic workflow demonstration; no investment-performance evidence.",
            "Monthly sampled drawdown; intramonth losses can be greater.",
            "No external deposits/withdrawals after initialization are supported yet.",
            "EUR only; no FX. Fund expenses/dividends/taxes are not modeled in fixtures.",
            "Operations are supplied estimates/reservations, not an AWS billing feed.",
            "Real ETF identities, total-return history and account entitlements remain unverified.",
            "A forward workflow halt stops all accounts; such comparisons are marked invalid.",
        ],
    }


def markdown(state):
    report = summary(state)
    status = report["halt"] or (
        "awaiting_allocation_result" if report["pending_allocations"] else "operational"
    )
    lines = [
        "# ETF Autopilot — paper workflow",
        "",
        "**Synthetic fixtures. Live trading unavailable. No profitability claim.**",
        "",
        f"Status: {status}; source: synthetic_fixture.",
        "",
    ]
    if not report["accounts"]:
        return "\n".join(lines + ["No valid market valuation available."])
    lines += [
        f"As of: {report['as_of']}. Initial cash flow: EUR {report['external_cash_flows_eur']}.",
        "",
        "| Account | Net equity EUR | Net P&L EUR | Return | Max drawdown | Fees EUR | "
        "Spread EUR | Operations EUR | Orders |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, account in report["accounts"].items():
        lines.append(
            f"| {name} | {account['net_equity_eur']} | {account['net_pnl_eur']} | "
            f"{number(account['return_percent']):.2f}% | "
            f"{number(account['max_drawdown_percent']):.2f}% | {account['broker_fees_eur']} | "
            f"{account['spread_cost_eur']} | {account['operations_eur']} | {account['orders']} |"
        )
    account = report["accounts"]["strategy"]
    lines += [
        "",
        "## Current strategy holdings",
        "",
        "| Asset | Quantity | Market value EUR | Book cost EUR | Unrealized P&L EUR |",
        "|---|---:|---:|---:|---:|",
    ]
    for position in account["positions"]:
        lines.append(
            f"| {position['asset']} | {position['quantity']} | "
            f"{position['market_value_eur']} | {position['cost_eur']} | "
            f"{position['unrealized_pnl_eur']} |"
        )
    lines += [
        "",
        f"Cash EUR {account['cash_eur']}; realized P&L EUR "
        f"{account['realized_pnl_eur']}; gross turnover EUR {account['turnover_eur']}.",
        "",
        f"Reconciliation: {account['reconciliation']}. "
        f"Comparisons valid for this workflow: {report['comparable']}.",
        "",
        "Passive uses the same starting cash, dates, fees, reserve and risk constraints; "
        "it keeps the fixed allocation without the trend filter and assumes no app charges. "
        "Without-AI shares routine operating costs and removes only inference charges. "
        + (
            "The strategy account used bounded allocation choices; "
            "without-AI kept its configured rule."
            if report["allocation_choices"]
            else "No model controls allocation."
        ),
        "",
        "## Limits",
        "",
    ]
    if report["constrained_events"]:
        lines.append(
            f"Constrained events: {report['constrained_events']}. Inspect recorded sizing "
            "constraints and unmet targets; all orders remain subject to the configured caps."
        )
    for choice in report["allocation_choices"]:
        lines.append(
            f"Allocation {choice['event_id']}: {choice['strategy']} from {choice['source']}; "
            f"reserved inference expense EUR {choice['reserved_cost_eur']}."
        )
    lines.extend(f"- {line}" for line in report["limitations"])
    return "\n".join(lines) + "\n"
