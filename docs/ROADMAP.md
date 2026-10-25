# Roadmap

The goal is an autonomous ETF investment and operations system that makes its
reasoning, limits, costs and results inspectable. The existing investment direction
stays intact. Progress is judged by working behavior and reproducible evidence.

## Working now

- A credential-free paper example with durable state and a comparison report.
- Deterministic allocation/policy checks, simulated execution and reconciliation.
- Dataset import, chronological evaluation and persistent holdout-exposure tracking.
- Optional read-only Public MCP and bounded diagnosis library interfaces.
- CI definition, Terraform safety tests and a verified AWS paper-worker deployment;
  recurring investment decisions remain unimplemented.

## Next: connect verified data

Add one useful historical provider adapter with source/provenance and redistribution
terms documented. Verify actual eligible ETF identities through the official broker
interface. Keep provider parsing out of the financial engine. Include small public
or synthetic contract fixtures so contributors can reproduce a bug without an account.

Acceptance: the dataset is reproducible, timestamps support later execution,
missing data fails clearly, and imported performance is never presented as actual
broker fills. Actual instrument/account compatibility is reported separately.

## Then: make the AI contribution measurable

Add one bounded allocation interface that can choose among explicitly validated
strategies and leave an evidence record. Keep the first implementation small:
validated input, a limited response, a reserved inference budget and deterministic
policy/execution checks. Preserve passive and without-AI comparison paths.

Acceptance: invalid or unavailable model output cannot place an order or alter a
limit; calls/costs are attributed; comparisons use the same data and cash flows;
proposed strategy changes do not promote themselves. Model-free contribution and
local development remain possible.

## Then: run the paper system repeatedly

Connect scheduled/event-driven paper decisions, verified account reads, reconciliation
and the selected alert route. AWS/model choices and spend must be explicit. Exercise
interruption and recovery before calling the operation unattended. Live execution
requires its own authorization and external-outcome/disclosure work.

## Small starter contributions

These are suggested issue scopes, not already-created GitHub issues:

| Contribution | Useful completion criterion |
|---|---|
| Improve CSV import diagnostics | A rejected row identifies its location without exposing private values; a synthetic regression reproduces it |
| Explain one recovery path | A short, runnable synthetic example and its expected ledger behavior |
| Check platform setup instructions | The documented command is verified on the named platform; unsupported platforms stay explicit |
| Add a provider contract fixture | Format/provenance are documented and redistribution is permitted; no real holdings or credentials |

Open an issue for a substantial feature so maintainers can agree on scope. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for the development loop. New frameworks,
services and abstractions need a concrete benefit to the current workflow.
