# Historical data import and evaluation

Implemented 25 October 2026. This implements the existing passive and slow-trend
hypotheses in the existing strategy. It preserves the strategy research and does not select actual
ETFs, fit new rules or enable trading. The original paper demo remains available.

## The distinction that matters

The original demo proves that orders, accounting and recovery fit together. The
new importer/evaluator can replay supplied historical total-return data under a
fixed, auditable protocol. It does not supply or independently verify a data feed.
The included 48-period CSV is explicitly synthetic and is a software contract
fixture, not historical market evidence. No genuine historical performance result
has been produced by this delivery.

The [daily export adapter](PROVIDER_IMPORT.md) now supports operator-supplied EODHD
files offline. Real source access/licensing, point-in-time availability, the exact
share class and the meaning of adjusted returns must be checked. The original
Public MCP decision remains unchanged; historical research data is a separate
input, and Bitpanda remains the broker route.

The [issuer/ECB adapter](ISSUER_IMPORT.md) also supports freely accessible local
fund-growth exports with explicit reference FX and shared-date handling. Its
version-3 contract retains retrospective-series and assumed-availability limits;
the included data is fictional. A successful source import does not select funds.

## Why the data contract has several timestamps

For a monthly decision, the engine needs three different facts:

1. `signal_at`: when the completed period's observation was measured.
2. `available_at`: when that observation was actually available to the strategy.
3. `execution_at`: a later observation at which the hypothetical order is modeled.

Availability must precede execution. Version 1 records declared timestamps;
versions 2 and 3 explicitly record an assumed 24-hour lag and retrospective
adjusted-close or issuer-growth proxies. None proves point-in-time availability
merely by supplying a timestamp.
A signal measured at month end is followed
by an execution observation in the next calendar month, no more than seven days
later. This is an input-quality bound, not a market-calendar implementation or a
promise of an executable quote. A provider adapter must supply genuine timestamps
and must not fabricate an opening value from a closing value.

The slow-trend rule compares the latest available signal index with the mean of
ten preceding monthly signal indices, preserving the existing rule. It never uses
the later execution index to decide whether the trend is on. The execution index
is used for hypothetical sizing, fees/spread and valuation. A future data value
cannot affect an earlier decision. Missing months or asset observations are
rejected; the importer does not silently fill or interpolate them.

## Total-return indices versus broker shares

A raw ETF price can fall when it distributes income. A total-return series accounts
for distributions as specified by its provider. This research interface requires
EUR total-return indices with reinvested distributions and fund expenses embedded.
It models investor taxes as excluded and cash return as zero, explicitly.

Indices represent **unitized research proxies**, not actual ETF share prices.
Simulated quantities cannot be sent to a broker. The ordinary paper workflow
rejects a historical research config, and live execution remains unavailable.
Investor-level reinvestment timing/costs, tax withholding, FX transaction costs,
real quotes, opening liquidity and actual broker fills are not reproduced here.
Currency movements already embedded in an EUR index remain part of its returns.

The import manifest records provider declarations, public source/documentation URL,
retrieval timestamp, instrument IDs and assumptions. ISIN validation for declared
historical data checks format only; UCITS status, share class and broker eligibility
remain independently unverified. No actual funds are selected by the fixture.

## Input files

The source CSV uses these exact columns:

```text
period,asset_id,signal_at,available_at,execution_at,signal_index,execution_index
```

`period` is the signal month (`YYYY-MM`), while all timestamps include a timezone.
Each consecutive month has exactly one record for each of two declared instruments:
one broad equity role and one bond role. The current default weights, lookback,
turnover, cash floor, drawdown stop and fees are copied unchanged from the paper
configuration into an isolated research configuration. Asset identities are mapped
by role; they are not added to the real broker universe.

The manifest shape is illustrated in
[the synthetic contract manifest](../fixtures/history_contract/manifest.json).
Use provider-specific evidence to populate real metadata and index values; do not
turn the fixture into historical data simply by changing its origin label.
Public source URLs must not contain credentials, query strings or fragments.

The importer rejects duplicate observations, missing months, late data, same-period
execution, nonfinite/nonpositive or unrepresentable index values, unaligned asset
timestamps, unknown assets, legacy `security` entries and unsupported assumptions.
Imports are bounded to 5 MB and 1,200 monthly periods. Normalized datasets retain
source/configuration hashes and a content-derived ID and are revalidated on use.
An existing output file with different content cannot be overwritten.

## Chronological evaluation protocol

A protocol fixes dataset identity, the complete research config hash, engine source
fingerprint, operating cost assumptions and three nonoverlapping chronological
windows: development, validation and holdout. Window dates refer to **execution
months**, not signal months. At least ten prior signal months and two valuation
observations per window are required.

Each account/window restarts at the same EUR 5,000 capital. The last observation
only values existing holdings, without opening new positions or forcing terminal
sales. This avoids charging end-of-test turnover with no subsequent holding period.
The windows are independent evaluation runs, not a continuously funded combined
performance record. No annualized short-sample return or strategy ranking is inferred.

Strategy, passive and without-AI comparators use the same dates and initial cash.
Passive assumes zero application operating charges; strategy and without-AI share
routine charges. No AI allocation/model is invoked in this layer, so the no-AI
comparison deliberately reports zero incremental AI effect. It is preparation for
future AI evaluation, not evidence that AI improves returns.

Each comparator has an independent ledger, risk status and reconciliation. If one
hits a risk stop, its existing holdings continue to be marked and any later losses
remain visible. It stops submitting orders; the other comparators continue under
their own policies. Halting does not mean liquidation or freezing portfolio value.
Fees, estimated spread, turnover, cash, book cost, unsold units, realized P&L,
net equity, sampled drawdown and decision records are retained in the result.

## Registration and holdout exposure

Register the complete protocol before running results. Validation requires completed
development; holdout requires both earlier windows and an explicit command. The
registry reserves examined periods before computing, so interruption does not make
a viewed period fresh again. The identical run can resume or return its cached
result without changing assumptions.

All examined windows count as exposure. An already examined development, validation
or holdout period cannot be relabelled as a fresh holdout for the same ISIN pair,
including after CSV revisions, changed costs/risk or changed code. A cached result
is also rejected if source logic, Python major/minor version or Decimal context
changes; a new protocol is required. Original results remain stored. Cosmetic source
changes may also change the conservative engine fingerprint.

These safeguards are local to the retained registry and exact ISIN pair. They
cannot establish that a human has never read the data, prevent deletion/copying of
the registry or prove independence of another correlated universe. They are an
exposure audit, not cryptographic proof of blind research. Preserve this registry
and do not use a fresh file to bypass a real holdout's history. Runs from an older
prototype without exposure records must first be registered again against their
original dataset/config to backfill the audit; they are not discarded.

No candidate is automatically promoted. These are two fixed hypotheses with a
preregistered replay. The separate [bounded paper choice](AI_DECISIONS.md) does not
run in this historical evaluator. Experiment management and promotion evaluation
remain later integrations in the original system plan.

## Run the synthetic contract check

From the repository root with the existing virtual environment:

```sh
.venv/bin/etf-autopilot history-import \
  fixtures/history_contract/manifest.json fixtures/history_contract/monthly.csv \
  --out var/history-contract-current/dataset.json

.venv/bin/etf-autopilot evaluation-plan var/history-contract-current/dataset.json \
  --development 2022-12:2023-11 --validation 2023-12:2024-11 \
  --holdout 2024-12:2026-01 \
  --out var/history-contract-current/protocol.json

.venv/bin/etf-autopilot evaluation-register \
  var/history-contract-current/dataset.json var/history-contract-current/protocol.json \
  --registry var/history-contract-current/registry.sqlite3

.venv/bin/etf-autopilot evaluate \
  var/history-contract-current/dataset.json var/history-contract-current/protocol.json \
  --registry var/history-contract-current/registry.sqlite3 \
  --window development --out-dir var/history-contract-current/results
```

Run validation with `--window validation` after inspecting development. Holdout is a
separate, explicit `--window holdout` invocation after both earlier windows. These
example dates cover a synthetic fixture, not an investment result. Repeating an
identical command returns the cached run and unchanged JSON/Markdown artifacts.

For actual future experiments, keep the research registry at a stable path, use
verified source data and dates that have not been examined, and predeclare the
protocol. Do not use source/config changes or a new output directory to claim a
previously viewed period is untouched. The stored result includes every comparator's
decisions, simulated broker operations, ledger and valuation history for inspection.

## What this enables next

A verified provider adapter can feed the same interface without changing risk,
execution accounting or the research strategy. Read-only Bitpanda validation then
establishes real instrument/account mappings. The bounded choice and finite cycle
are implemented separately; a verified forward feed and unattended operation
remain subsequent parts of the existing plan. No cloud,
broker write permission, paid subscription or model call is needed to exercise the
new importer and evaluation controls locally.
