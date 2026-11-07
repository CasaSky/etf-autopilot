# Forward paper on AWS

**Implemented and tested with fixtures; deployment and alert delivery require an
owner-approved plan. Live trading is unavailable.** This extends the existing
[forward paper trial](FORWARD_PAPER.md), preserving its accounts, comparison dates,
monthly decision slots and consumed model budget.

## What runs

One short daily Lambda invocation collects the reviewed issuer/ECB files. Daily
collection observes new values; it does not imply daily trading. A monthly decision
chooses between the two fixed allocation rules. Optional AI is consulted only when
their allocations differ, with at most one durably reserved attempt per UTC month.
It cannot change funds, weights, risk limits, data freshness or permissions.
Execution remains a simulation at a later, previously unseen research value.

The daily schedule is UTC 08:00, off by default. It passes a stable scheduled time
and accepts events no more than five minutes old. Publisher delays, weekends and
holidays can leave the data too old under the unchanged freshness rule. Waiting is
an expected result; neither a report date nor a download time replaces a NAV date.

The optional worker uses 512 MB, a 180-second timeout and concurrency one.
Scheduler, Lambda async delivery and SDK request retries are disabled. No resident
agent, broker key, live quote or permanent model session is involved. The existing
synthetic health schedule stays independent and disabled unless explicitly chosen.

## Evidence and interruptions

The worker reads three fixed private S3 objects under `paper/forward/inputs/`:
`runtime.json`, `config.json` and `plan.json`. A reviewed runtime digest in the
Lambda configuration binds the fund configuration, capture plan and trial identity.
Worker IAM cannot replace these inputs. Requests cannot select other keys, URLs,
models or budgets.

The existing ledger moves to `paper/forward/state.json`, with conditional writes.
Each UTC day has an immutable `paper/forward/ticks/YYYY-MM-DD/` directory. The worker:

1. Reserves capture and monthly operating estimates in the ledger before HTTP.
2. Captures once, retaining original bytes, hashes and receipt times.
3. Writes a `ready.json` manifest last, before any model invocation.
4. Uses the existing deterministic decision, reservation and accounting functions.
5. Writes one `outcome.json` containing the report and state receipt.

A cold start restores only allowlisted filenames with bounded sizes and verified
hashes. A consumed capture reservation without a completed manifest cannot repeat
downloads automatically. A lost model acknowledgement retains its reservation;
replay reads the saved result or stops for review. It never makes another model
attempt to resolve uncertainty. Paper fills and cost postings remain conditional
ledger transitions. This is not a claim of exactly-once external execution.

## Alerts

Opting in creates one SNS topic, a private email subscription and two standard
CloudWatch alarms: worker errors and retained failure-queue messages. Subscription
confirmation and one controlled delivery test must succeed before enabling the
daily schedule. The email is a sensitive Terraform variable; keep settings, plans
and state private. Do not put it into public examples or issues.

Ordinary stale-data waits do not send mail. Missing capture evidence, failed or
unknown inference, expired decisions, reconciliation failures and data older than
seven days require review. Application alerts reserve one send per day before SNS;
an unknown publish result is not retried. The delivery test uses a separate marker.
CloudWatch alarms can also notify on state changes, so total email delivery is not
limited by the application marker. SNS accepting a publish does not prove inbox
delivery: the owner must check receipt.

## Operating estimate

Reviewed planning assumptions, 7 November 2026, Frankfurt, excluding free tiers:

| Item | Monthly assumption | Estimated USD |
|---|---|---:|
| Two standard metric alarms | USD 0.10 each | 0.20 |
| Lambda compute | 31 × 180 seconds × 0.5 GB, USD 0.000025/GB-s allowance | 0.070 |
| New retained raw data | about 0.31 GB normally; up to 1.7 GB at source caps | 0.008–0.042 per retained month |
| Requests, small logs and email | bounded daily runs, small failure volume | under 0.02 |

The accounting model reserves **EUR 0.30/month** for fixed monitoring/storage,
**EUR 0.002 per capture attempt**, **EUR 0.01 per decision** and **EUR 0.02 per
model attempt**. Thirty-one captures and one model decision reserve EUR 0.392.
Shared operating estimates apply equally to AI and without-AI accounts; inference
cost applies only to AI. The passive counterfactual has no application costs.

These are estimates, not invoice reconciliation or a hard AWS spending cap.
The EUR 1 application monthly limit cannot prevent storage or alarm charges, or
charges from repeated failed invocations. Current audit originals have no automatic
deletion: retained storage grows each month. Review actual usage after the first
month and before extending operation beyond three months. Preserve or export audit
evidence before considering any separately authorized retention change.

Sources: [Lambda pricing](https://aws.amazon.com/lambda/pricing/),
[Frankfurt S3 catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/eu-central-1/index.json),
[Frankfurt CloudWatch catalog](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/current/eu-central-1/index.json),
[SNS pricing](https://aws.amazon.com/sns/pricing/).

## Prepare without activating

Use the trial's existing private configuration, capture plan and SQLite state.
Do not initialize a replacement trial to clear old reservations. Prepare the sealed
runtime without changing local financial state or writing to AWS:

```sh
etf-autopilot --config var/trial/config.json --state var/trial/paper.sqlite3 \
  forward-runtime var/trial/capture-plan.json --model-enabled \
  --out var/cloud/runtime.json
```

Review Terraform with `enable_forward_runtime = true`, its exact runtime hash,
the private alert email, `worker_reserved_concurrency = 0`, and all schedules and
model permission still off. Build the package immediately before planning. Keep
the existing project's Terraform state mapping; never plan against an empty state
for an already deployed installation. Approve the exact plan before applying it.

Only after that approval, upload the three reviewed inputs to their fixed keys.
Use a separately reviewed update to concurrency one, retaining schedules and model
permission off, for `{"action":"forward_alert_test"}`. Check both the invocation
result and actual email delivery. Confirm the subscription first. These are
operational tests, with zero financial decisions or inference.

## Transfer the existing trial

Financial code is fingerprinted. If an old trial has never made a decision, spent
money or changed its starting portfolios, `forward-review-code` supports an
explicit audit of a reviewed code update using the old state hash and fingerprint.
It preserves original events, source prefixes and history. It refuses invested
trials, used model attempts, pending work or prior spend. An invested trial needs
a separate migration review; there is no automatic fingerprint reset.

After approval and review, freeze local ownership before installing the same
ledger in AWS:

```sh
etf-autopilot --config var/trial/config.json --state var/trial/paper.sqlite3 \
  forward-transfer-freeze --target aws --expected-state-hash REVIEWED_STATE_HASH \
  --out var/cloud/handoff.json
etf-autopilot --config var/trial/config.json --state var/trial/paper.sqlite3 \
  forward-transfer-install var/cloud/handoff.json --allow-cloud-state \
  --profile YOUR_PROFILE --account-id YOUR_ACCOUNT_ID --region eu-central-1 \
  --bucket YOUR_ETF_AUTOPILOT_BUCKET
etf-autopilot --config var/trial/config.json --state var/trial/paper.sqlite3 \
  forward-transfer-complete var/cloud/handoff.json --allow-cloud-state \
  --profile YOUR_PROFILE --account-id YOUR_ACCOUNT_ID --region eu-central-1 \
  --bucket YOUR_ETF_AUTOPILOT_BUCKET
```

Each command verifies explicit cloud identity and bucket ownership. The destination
cannot create a fresh financial ledger, overwrite an active controller or reset
costs. Local execution stays fenced after a lost upload acknowledgement. Repeating
the same handoff checks the persisted proof rather than overwriting newer state.

Before installation, `forward-transfer-abort` fences the destination first, then
unfreezes the source. It refuses an already installed transfer; a late install and
abort cannot activate both controllers. After installation, return by freezing
the AWS controller with `--owner aws --target local`, installing that newer bundle
locally, and completing the reverse transfer. Carry all fills and costs back.
Never resume an old local snapshot or manually remove its ownership marker.

With correct ownership, a read-only `{"action":"forward_health"}` checks status.
Only then review the final opt-in model IAM and daily schedule plan. It requires
`forward_alert_confirmed = true` and concurrency one. The exact EU Nova Micro
profile is allowed through four reviewed EU routes; no wildcard model, streaming,
secrets or broker permissions are granted.

## Stop and recover

Set `enable_forward_schedule = false` and `worker_reserved_concurrency = 0` in an
approved Terraform update. Keep the original health schedule off too. This stops
invocations while retaining state and evidence; alarms and storage still cost.
Deleting resources requires a separate reviewed removal plan.

Do not change a day or event ID to retry a failed step. Inspect state plus the
original day's evidence. A complete manifest permits a controlled same-day resume
within the five-minute scheduled-event window; use the current invocation time
with the same UTC day. A missing manifest or changed context requires review.
Never redrive old failure-queue messages as new decisions.

For an unknown model outcome, keep the consumed attempt. The owner can explicitly
close it with `forward-cloud-choice-abandon DECISION_ID --expected-state-hash HASH`
and the same explicit cloud parameters. This neither refunds costs nor opens
another monthly decision slot. Resolve reconciliation issues before transferring.

## Runtime compatibility

Production targets Python 3.14, the newest generally available Lambda Python
runtime reviewed on 7 November 2026. CI also checks Python 3.15.2; Lambda's Python
3.15 offering is still public preview. The official MCP SDK 2.3 client is tested
against protocol fixtures. Earlier real-account receipts remain historical; they
do not claim a new live SDK 2 account check. The cloud source-capture worker never
loads a broker key or needs MCP dependencies in its deployment package.

Sources: [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html),
[official MCP v2 migration](https://py.sdk.modelcontextprotocol.io/migration/),
[conditional S3 writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[inference profile IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html).
