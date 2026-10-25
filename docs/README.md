# Documentation

Start with the task you want to do. The project is in early development; current
examples are synthetic and the default runtime is paper-only.

| Task | Guide |
|---|---|
| Run the system without setup | [Paper demo](../examples/paper_demo.py) and [sample report](paper-demo.md) |
| Understand the design | [Architecture](ARCHITECTURE.md) |
| Import data and evaluate a strategy | [Data and evaluation](DATA_EVALUATION.md) |
| Convert daily provider exports privately | [Daily export adapter](PROVIDER_IMPORT.md) |
| Convert free issuer fund-growth exports | [Issuer and ECB adapter](ISSUER_IMPORT.md) |
| Try bounded choices and finite recurring cycles | [Paper decisions](AI_DECISIONS.md) |
| Recover a stopped or uncertain paper run | [Operations](OPERATIONS.md) |
| Try an explicitly read-only account connection | [Broker integration](BROKER.md) |
| Review the optional AWS paper worker | [Deployment](../deploy/README.md) |
| Find a useful contribution | [Roadmap](ROADMAP.md) and [contribution guide](../CONTRIBUTING.md) |
| Maintain the public repository | [Publishing](PUBLISHING.md) |
| Keep private files out of commits and history | [Privacy](PRIVACY.md) |

The core source is intentionally small and direct:

- `domain.py`: financial values, configuration and stable error codes.
- `strategy.py`: the existing allocation rules and deterministic policy checks.
- `paper.py`: simulated quotes/fills, accounting and reconciliation.
- `engine.py` / `store.py`: durable paper-event processing.
- `selection.py` / `cycle.py`: bounded choices and a finite recurring entry point.
- `history.py` / `evaluation.py`: imported data, fixed protocols and exposure tracking.
- `instruments.py` / `providers.py`: broker identity checks and offline daily-export conversion.
- `issuer.py`: offline fund-growth exports, explicit reference FX and shared-date provenance.
- `market.py`: typed ticker evidence that distinguishes retrieval time from price time.
- `operations.py` / `bitpanda.py`: optional diagnosis and read-only broker boundaries.

Read the example first, then follow a single event into the relevant module. Most
contributions do not need the broker or cloud integrations.
