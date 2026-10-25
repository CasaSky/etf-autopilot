# ETF Autopilot

**Build AI-assisted ETF investing systems whose decisions and results can be audited.**

ETF Autopilot is an experimental Python toolkit for paper investing, deterministic
risk checks and reproducible strategy evaluation. Its central question is whether
AI can improve an investment process after trading and operating costs.

**Early development · Paper only · Python 3.13+ · MIT**

The paper engine and evaluation tools work today. AI allocation, verified market
feeds and the recurring investment loop are still being implemented. Current
examples use synthetic data and make no investment-performance claim.

## Try it with one command

From a checkout, with Python 3.13 or later:

```sh
python3 examples/paper_demo.py
```

No installation, credentials, model, broker account or AWS resources are needed.
The example simulates orders, reconciles its ledger and writes a comparison report
at `var/quickstart/report.md`. Run it again: duplicate events add no trades or fees.
Read the [saved example report](docs/paper-demo.md) or the
[short example source](examples/paper_demo.py).

## What makes this project useful

- **Measure the AI contribution.** Compare against passive investing and the same
  strategy without AI. Keep costs, risk and unsuccessful experiments visible.
- **Keep financial controls deterministic.** Allocation limits, cash, fees, execution
  checks and reconciliation live in ordinary Python, outside model authority.
- **Make experiments reproducible.** Bind results to data, configuration and code;
  track which evaluation periods have already been examined.
- **Use AI selectively.** Reserve bounded model calls for work that needs them.
  Routine accounting and checks run without a model.
- **Make failures inspectable.** Unknown outcomes and partial fills stop execution;
  reconciliation recovers confirmed fills without placing another order.

These are engineering goals and implemented controls, not evidence of extra returns.

## A small core

```mermaid
flowchart LR
    Data[Data] --> Strategy[Strategy]
    Strategy --> Checks[Risk and cost checks]
    Checks --> Paper[Paper execution]
    Paper --> Ledger[Ledger and reconciliation]
    Ledger --> Compare[Baseline comparison]
```

The paper and evaluation core uses the Python standard library. The official
Bitpanda Public MCP SDK and AWS support are optional integrations. No agent
framework, server or cloud deployment is required to understand the core.

## Current status

| Area | Status |
|---|---|
| Paper orders, accounting and recovery | Verified locally and in the AWS paper worker |
| Historical import and chronological evaluation | Working; supplied examples are synthetic |
| Bitpanda Public MCP | Read-only boundary and mock contracts; account mapping unverified |
| AI diagnosis | Optional library adapter; disabled by default |
| AI allocation and experiment orchestration | Planned |
| Recurring AWS investment operation | Planned; the paper worker is verified, health schedule disabled |
| Live trading | Unavailable |

## Explore or contribute

- [Documentation](docs/README.md): choose a guide for the task you want to do.
- [Architecture](docs/ARCHITECTURE.md): understand the core and its boundaries.
- [Data and evaluation](docs/DATA_EVALUATION.md): import data and run a fixed protocol.
- [Roadmap](docs/ROADMAP.md): see the next useful pieces and starter contributions.
- [Contributing](CONTRIBUTING.md): set up development and make a focused change.
- [AWS deployment](deploy/README.md): reproduce the isolated paper worker with Terraform.
- [Security](SECURITY.md) and [community standards](CODE_OF_CONDUCT.md).

For an installed CLI, run `python3 -m pip install -e .` from a checkout.
Optional broker setup is described in [the broker guide](docs/BROKER.md).
The project is licensed under [MIT](LICENSE).
