# Operating and recovery guide

## Start, inspect and stop locally

`etf-autopilot demo` executes a finite offline workflow and exits. Nothing runs in
background. `report --json` exposes confirmed ledger values, costs, reconciliation
and limitations. All CLI commands take `--config` / `--state` before the subcommand.
`run PATH_TO_EVENT.json` processes a single event using the same contract.

Stopping requires no daemon shutdown. Keep the state database for audit and resume
with the same event ID after a process interruption. Back up the SQLite database
while the finite worker is stopped. A new evaluation uses a new state filename.
A changed config cannot silently operate on an existing account.

## Order sizing within fixed limits

The default `execution.sizing_policy` is `reject` when omitted. A proposed plan
that exceeds a daily buy/sell or total turnover cap halts before placing orders.
The original paper configuration keeps this behavior.

A separate experiment can set `"sizing_policy": "bounded"` in `execution`.
The planner reduces each order to the remaining daily allowance and turnover
budget, floors quantities, includes fees in cash checks, and skips orders below
the minimum size. Sells come first, then asset ID order within each side; this is
deterministic priority, not an optimal allocation solver. Residual cash and unmet
target weights can remain. No remainder is automatically retried that month.

The complete reduced plan must still pass cash, position and equity limits before
its first order. If reducing a sale leaves an unsafe portfolio, it still halts.
Daily and turnover caps are never increased. This is planned order sizing;
unexpected broker partial fills retain their separate reconciliation procedure.
Event results record `execution_constraints`, and reports count constrained events.

Use a new state file and a newly registered historical protocol when changing this
policy. Preserve the old results and record when the change was motivated by an
observed limit interaction. An execution change is not evidence of investment alpha.

## Halts

- `broker_outcome_unknown`: do not submit again. Run `reconcile`; it imports a
  terminal broker fill by intent ID if evidence exists. If evidence is absent,
  the account stays halted. The simulator tests both cases.
- `partial_fill_reconcile_required`: reconcile the executed portion. The remaining
  quantity is not retried, and the decision month remains consumed.
- `quote_expired`: no paper fill was accepted. Reconciliation can clear the halt,
  but neither replaying the event nor using a new ID bypasses the monthly slot.
- Cash/holdings/operation/journal mismatches: investigate persisted snapshots and
  operations. A report flags invalid comparisons. `reconcile` never adjusts cash
  or erases unmatched history merely to make the numbers agree.
- Stale/future data, missing history, excessive turnover, exhausted budgets or
  drawdown: correct the input or review the policy in a separate evaluation. Use
  reconciliation to inspect account consistency; it cannot authorize extra risk.
- `credentials_expired`: renew the read key personally, store it securely and update
  the runtime expiry. No automatic quote/request retry occurs.
- `concurrent_state_change_replay_same_event` or unknown S3 write: inspect current
  state, then replay the identical event ID/payload. All effects in this release
  are simulated; do not apply this whole-state retry pattern to live trading.

Explicit recovery preserves the original event/diagnosis audit. Replaying a halted
historical event still returns its original result with `duplicate: true`.
A new event in an unconsumed future month may proceed after reconciliation.

## AI and operating budgets

Defaults: zero model calls, EUR 1 monthly estimated operations budget, 40 new
market events per month, at most 2,000 input-token budget and 400 output-token
budget. Prompts/responses also have conservative UTF-8 byte caps; failures are
charged the full configured reservation. Model use is an explicit integration,
not a background loop. The CLI's `paper-decision` / `paper-cycle` commands support
explicit model opt-in; the AWS worker has no configured model or model permission.
See [bounded decisions](AI_DECISIONS.md) for the choice contract, scripted example,
durable reservation and unknown-inference recovery.

The optional diagnosis API persists a positive worst-case cost reservation and
call count before inference. Repeating its ID cannot spend again; a crash after
reservation stays reserved. Only an allowed diagnostic action can be proposed;
model errors leave portfolio, policy and broker state unchanged. Bedrock Converse
is supported as an injected provider with a required model ID and output cap.
Model selection, approved rates, an end-to-end input token bound and AWS permission
scope must be reviewed before using it. Disable SDK automatic retries for paid
providers. No repair, budget change, write tool or strategy promotion is executed
from its output.

Application budgets stop optional work, not AWS billing. Health reads, rejected
invocations, storage and logs can still cost money. Reserved concurrency, timeouts,
no async retries, finite data size and short log retention bound normal workload;
AWS billing alarms are additional notifications, not hard account caps.

## AWS operations

See [deployment guide](../deploy/README.md). The daily health schedule is disabled
on deployment and only reads state; it never initiates orders or model calls.
Inspect CloudWatch logs and the failure queue for infrastructure failures. Handled
business halts are logged as structured `status: halted` results, so an empty
failure queue does not imply healthy trading. The initial Terraform module has no external
notification destination; wire the user's chosen alert route before unattended use.

Only app-scoped resources are accessed. There are no cross-project permissions,
SSM/secret reads, broad infrastructure changes, paid data subscriptions or NAT.
