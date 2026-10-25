# Bounded paper decisions

The optional model chooses between two implemented hypotheses: `passive` and
`slow_trend`. Both remain subject to deterministic risk checks. This creates an
auditable experiment; it does not establish that either choice improves returns.

## Try the workflow without a model

```sh
python3 examples/ai_paper_demo.py
```

The example uses a **scripted fixture**, synthetic prices and a simulated EUR 0.02
inference reservation per decision. No network request or actual inference occurs.
It writes a separate state and report under `var/ai-quickstart/`. Repeating it makes
zero scripted calls and adds no orders, fees or expenses.

With an installed CLI, run one prepared event without a model:

```sh
etf-autopilot --state var/decision.sqlite3 paper-decision var/event.json
```

The default configuration still permits **zero model calls**. Its identity and the
original investment rules are unchanged. Use a separate state for a different
configuration; editing the config cannot silently change an existing experiment.

## What the model can decide

The prompt contains role-based trend gaps, latest monthly changes and the two
deterministic candidate weight sets. It contains no broker tool descriptions,
credentials, holdings, dates or ETF identifiers. The response must contain exactly
one key: `{"strategy":"passive"}` or `{"strategy":"slow_trend"}`. Extra keys,
duplicate JSON keys, unsupported values and oversized output fail closed.

Only the `strategy` account uses this choice. The `without_ai` account retains the
configured rule and routine operating costs; `passive` retains its fixed weights
and assumes no application costs. Inference reservations affect only the strategy
account's net equity. All three retain the same dates, capital and execution rules.

The model cannot choose assets, invent orders, change weights/risk limits, remove
losses, enable broker writes or promote new code. A name in this interface means a
software-tested hypothesis, not a financially validated or profitable strategy.

## Calls, costs and interruption

The application binds the input, config, provider identity and derived context to
a receipt. It persists the call count and full cost reservation **before** invoking
the provider, outside the state transaction. At most one allocation is reserved
per UTC month, within the shared diagnosis/allocation call and spend budgets.

Input is conservatively capped in UTF-8 bytes against the configured input-token
budget, including the system text and a 512-byte framing allowance. Output has a
byte cap and the model request has an output-token cap. Provider token accounting
and regional prices still need review when setting a worst-case EUR reservation.
Reservations are estimates, not reconciled invoices or a cloud billing cap.

An interrupted or unknown inference remains `reserved`. Replaying that event
returns `awaiting_allocation_result`; it never repeats the call automatically.
Inspect the receipt and explicitly close an unresolvable call with:

```sh
etf-autopilot --config var/experiment.json --state var/decision.sqlite3 \
  allocation-abandon THE_ORIGINAL_EVENT_ID
```

Replay the original event with the same provider/configuration afterwards. It
records a halted decision, submits no orders and retains the full reservation.
Reconciliation is explicit. No second allocation is permitted for that month.
Invalid output or provider errors follow the same no-order, charged-reservation
path. Valid market data still values unsold positions; a failure never freezes
losses. Pending or halted runs are flagged as unsuitable for return comparison.

## Optional Bedrock call

The CLI supports an explicit model ID, region, positive worst-case reservation and
`--allow-model-call`, together with a separate config that permits model calls.
The optional `aws` dependency and the operator's model access are required. There
is no default model and no model call in the deployed worker. The SDK client is
created after reservation, with one total attempt and bounded timeouts. Converse
has no tools or broker access. No actual Bedrock inference has been used as a test.
The request uses AWS's [Converse interface](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).

```sh
etf-autopilot --config var/experiment.json --state var/decision.sqlite3 \
  paper-decision var/event.json --model-id YOUR_REVIEWED_MODEL_ID \
  --region YOUR_REVIEWED_REGION --profile YOUR_AWS_PROFILE \
  --reserved-cost-eur YOUR_WORST_CASE_EUR_COST --allow-model-call
```

Historical evaluation deliberately rejects this forward decision path. A modern
model may know historical outcomes even if dates/names are removed. A replay with
an LLM is therefore not proof of a blind historical AI advantage. Use registered
historical rule comparisons and separately audited forward paper experiments.

## Recurring operation

`paper-cycle var/event.json` uses the same provider options and runs one finite
cycle. It additionally checks that the decision belongs to the runtime's current
UTC month, is not in the future, and that the market observation is fresh relative
to the actual clock. Stale input returns `waiting_for_input` without state changes,
calls or orders. It does not rewrite timestamps or clear a halt.

Repeated delivery uses the existing event and allocation receipts. The financial
engine reconciles before and after simulated execution, then the cycle returns a
report. It exits; an external scheduler supplies future ticks. The AWS worker's
`cycle` action reads only its fixed prepared input key and makes no model call.

This entry point currently accepts the synthetic forward-event contract. A real
feed, account mapping, approved model configuration and alert destination remain
required before unattended paper operation. The deployed health schedule remains
disabled and does not call this action. No real investment schedule is enabled.
