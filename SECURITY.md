# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x | Yes |
| Earlier than 0.5 | No |

Security fixes land in the latest 0.5.x release. Earlier lines get no fixes;
upgrade to the latest release (see [Upgrading](docs/upgrading.md)).

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Report it privately, in either of these ways:

- Open a private advisory on GitHub:
  <https://github.com/cruxible-ai/cruxible/security/advisories/new>
- Email **support@cruxible.ai**.

Use either one for any package in scope below, even if its code lives in
another repository. Please include:

- what the vulnerability is and which package and version it affects
- steps to reproduce
- the impact you expect
- a suggested fix, if you have one

## What to expect

The maintainer aims to acknowledge a report within 48 hours and to ship
a fix or a mitigation for a confirmed issue within 30 days. These are intentions,
not promises: a complex issue can take longer, and you will hear how it is going.
Once a fix is released, the issue is disclosed in a GitHub security advisory,
crediting the reporter unless they ask not to be named.

## Scope

This policy covers:

- **Core**: the `cruxible` package, including the daemon (`cruxible server
  start`) and the MCP server (`cruxible mcp`, `cruxible-mcp`)
- **Client**: the `cruxible-client` package (Python SDK and typed HTTP client)
- **Provider runtime**: the `cruxible-provider-runtime` package
- **First-party providers**: the `cruxible-provider-*` packages published by
  Cruxible, and the providers shipped inside first-party kits
- **First-party kits**: the kits published under `ghcr.io/cruxible-ai/kits`

Third-party providers and kits are out of scope; report those to their
authors. What each part reads, writes and connects to is listed in
[Data and network](docs/data-and-network.md).

## Trust boundary

A daemon on a Unix socket runs without authentication by default. In that mode
any local process of the same user can act as any principal, and approvals are
signed with keys that process can read: review by a separate principal is a
convention, not a control. Reports that depend on one local process using
another principal's identity or key in this mode are not vulnerabilities. Start
the daemon with `--auth` (required on TCP) and keep each principal's key in its
own custody when principals must be told apart; see
[Review and governance](docs/review-and-governance.md).

On-disk integrity checks protect against accidents, not against a process
running as the daemon's user; see [Architecture](docs/architecture.md#integrity-and-isolation).
