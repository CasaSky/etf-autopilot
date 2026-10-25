# Contributing

Contributions are welcome in code, documentation, examples and reproducible data
adapters. Start with the [roadmap](docs/ROADMAP.md), then choose a small change with
a clear outcome. No broker account, cloud subscription or model access is needed.

## Set up development

From a checkout with Python 3.13 or later:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
pytest -q
```

The lock file includes optional integrations and development tools so the complete
suite can run. For a quick introduction without installing anything, run
`python3 examples/paper_demo.py`. The source checkout is required for fixtures and
configuration; this is not a published PyPI distribution yet.

## Design principles

1. Prefer a small, readable function to a new framework or layer. Add an abstraction
   when a concrete second use needs it, and explain the trade-off.
2. Keep the core useful without network access, credentials or third-party packages.
   Put broker, cloud and model dependencies behind explicit optional interfaces.
3. Keep money, portfolio rules, limits and reconciliation deterministic. Model output
   is data to validate, not permission to place an order or change a risk limit.
4. Preserve source provenance and evaluation history. Use synthetic fixtures for
   public reproductions, and keep point-in-time data assumptions explicit.
5. Document behavior and the reason for a non-obvious choice. Keep the README short;
   put implementation detail in the relevant guide and link to it.
6. Describe the current feature honestly. A working fixture, an API contract and a
   verified real-account integration are different milestones.

## Make a change

For a substantial feature, open an issue describing the problem, a small example
and the proposed scope before implementing it. Documentation fixes and small bug
fixes can go straight to a pull request. The repository uses `codex/` for local
agent branches; human contributors can use another descriptive branch prefix.

Keep the pull request focused. Explain what changes for a user, why it is useful,
and how you verified it. Include meaningful regression checks for financial,
execution or persistence behavior. For a documentation-only change, check links
and actually run any changed command; a new unit test is usually unnecessary.

```sh
ruff check .
ruff format --check .
pytest -q
python -S examples/paper_demo.py
```

For deployment changes, also run `cfn-lint deploy/template.yaml` and
`python scripts/package_lambda.py`. Do not use live quote acceptance as a test.
AI-assisted contributions follow the same review and verification requirements;
contributors need to understand the code and its assumptions.

## Data, privacy and scope

Share only data you have permission to redistribute. Public examples must not
contain account holdings, API keys, cloud credentials, private research notes or
identifying logs. See [SECURITY.md](SECURITY.md) for private reports.

The scope is low-frequency ETF investment and operations. Keep strategy changes
separate from unrelated refactors. Explain proposed hypotheses and evaluation
criteria; do not silently change a benchmark, raise risk or promote a fitted rule.
The working system remains paper-only.

Contribute material you have the right to share under the project's [MIT license](LICENSE).
Follow the [code of conduct](CODE_OF_CONDUCT.md) in all project spaces.

## Keep private context out of commits

Activate the repository's local privacy hooks after cloning:

```sh
git config --local core.hooksPath .githooks
python scripts/check_private_files.py --staged --history
```

The pre-commit check rejects private paths even when force-added. The pre-push
check also inspects earlier commits and the exact objects being pushed. See
[privacy boundaries](docs/PRIVACY.md) for the distinction between ignored,
currently tracked and historically committed files.
