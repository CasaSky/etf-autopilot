# ETF Autopilot — paper workflow

**Synthetic fixtures. Live trading unavailable. No profitability claim.**

Status: operational; source: synthetic_fixture.

As of: 2026-10-23T10:00:00Z. Initial cash flow: EUR 5000.00.

| Account | Net equity EUR | Net P&L EUR | Return | Max drawdown | Fees EUR | Spread EUR | Operations EUR | Orders |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| passive | 4762.69 | -237.31 | -4.75% | 7.42% | 4.00 | 2.52 | 0.00 | 4 |
| strategy | 4761.30 | -238.70 | -4.77% | 7.45% | 4.00 | 3.88 | 0.03 | 4 |
| without_ai | 4761.30 | -238.70 | -4.77% | 7.45% | 4.00 | 3.88 | 0.03 | 4 |

## Current strategy holdings

| Asset | Quantity | Market value EUR | Book cost EUR | Unrealized P&L EUR |
|---|---:|---:|---:|---:|
| SIM_EUR_BONDS | 20.000000 | 1020.00 | 1001.50 | 18.50 |
| SIM_GLOBAL_EQUITY | 0.000000 | 0.00 | 0.00 | 0.00 |

Cash EUR 3741.33; realized P&L EUR -257.17; gross turnover EUR 7741.33.

Reconciliation: matched. Comparisons valid for this workflow: True.

Passive uses the same starting cash, dates, fees, reserve and risk constraints; it keeps the fixed allocation without the trend filter and assumes no app charges. Without-AI shares routine operating costs and removes only inference charges. No model controls allocation.

## Limits

- Synthetic workflow demonstration; no investment-performance evidence.
- Monthly sampled drawdown; intramonth losses can be greater.
- No external deposits/withdrawals after initialization are supported yet.
- EUR only; no FX. Fund expenses/dividends/taxes are not modeled in fixtures.
- Operations are supplied estimates/reservations, not an AWS billing feed.
- Real ETF identities, total-return history and account entitlements remain unverified.
