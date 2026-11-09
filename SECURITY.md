# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Cruxible, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, email **support@cruxible.ai** with:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

We will acknowledge your report within 48 hours and work with you to understand and address the issue.

## Scope

This policy applies to the `cruxible` and `cruxible-client` Python packages, including the daemon (`cruxible server start`) and the MCP server (`cruxible mcp`, `cruxible-mcp`).

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

## Supported Versions

Security fixes are applied to the latest release only.
