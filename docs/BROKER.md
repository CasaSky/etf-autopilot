# Optional Bitpanda Public MCP integration

The offline example and evaluation core work without a broker account. This
integration uses Bitpanda's official hosted Public MCP and official Python SDK.
It does not implement or host another MCP server.

## Install the optional client

In your Python environment, from a source checkout:

```sh
python -m pip install -e '.[broker]'
```

Development contributors using `requirements.lock` already have these dependencies.
The command reports a clear missing-dependency code if this extra is absent.

## Configure an explicitly read-only check

Complete any required real securities onboarding, verification and tax information
personally in your existing account. Create a dedicated API key with only the read
scopes needed for asset/pricing, balances and operation history. No trade-write,
Earn, margin or transfer access is required for this stage.

Run the check in an interactive terminal, with your key's actual UTC expiry:

```sh
etf-autopilot broker-check --allow-account-read \
  --expires-at YOUR_ACTUAL_UTC_EXPIRY_TIMESTAMP
```

The key is entered at a hidden prompt and is not stored by the command. Managed
runtimes can inject `BITPANDA_API_KEY` and `BITPANDA_KEY_EXPIRES_AT` securely; the
application does not automatically load `.env` files. Do not put secrets in issue
reports, tool arguments, model prompts, command history or source control.

The client discovers actual tool schemas, filters its read allowlist and validates
arguments before a holdings read. It returns a small receipt without account
balances, holdings or the key. A protocol read is not verification of real ETF
identity, complete response mapping or account eligibility. These remain integration
work until checked against the actual account and provider contracts.

## Boundaries

The allowlist contains asset/currency discovery, ticker, holdings, portfolio
history and operation history. Quote creation, quote acceptance and trading-limit
tools are excluded. Broker budget inspection itself requires a different permission
level; no write permissions should be added merely to run the initial check.

The runtime checks expiry before reads, limits requests and returns fixed error
codes without upstream response bodies. The initial project has no live execution
implementation, and tests never accept real quotes. The separate historical-data
interface uses research indices, not prices or quantities that can be sent to the
broker.

Sources: [official hosted MCP setup](https://docs.bitpanda.com/mcp-server-371916m0),
[Public MCP tools](https://docs.public.bitpanda.com/getting-started-589286f0),
[API key setup](https://docs.public.bitpanda.com/api-key-generation).
