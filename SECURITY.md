# Security

This project is in early development and provides paper execution only. There is
no supported live-trading release or promised incident-response service level.

## Report a vulnerability privately

If private vulnerability reporting is enabled on the public GitHub repository,
use **Security → Report a vulnerability**. Otherwise, ask a maintainer for a private
channel without including exploit details in a public issue. A maintainer contact
and the repository reporting feature must be configured before public launch.

Include affected behavior, a minimal reproduction using synthetic data, expected
impact and any suggested correction. Redact credentials, account identifiers,
holdings, cloud resources and private research from all attachments and logs.

## Integration boundaries

Broker credentials are supplied at runtime outside model prompts and tool arguments.
The initial broker command permits explicit reads only. Model output cannot enable
live trading, change limits or bypass accounting and reconciliation.

New integrations should grant only the access they need and retain the paper mode
boundary. See [architecture](docs/ARCHITECTURE.md) and
[broker setup](docs/BROKER.md) before changing a broker or model interface.
