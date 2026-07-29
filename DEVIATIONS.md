# Deviations

## wi-deprecation-mechanics

- Feedback action `approve` was not registered as a compatibility alias. The
  current canonical write vocabulary is `approve`, `reject`, and `correct`, and
  `approve` remains the primary adjudication action across service, CLI, MCP,
  HTTP, and client surfaces. Deprecating it would contradict the current
  contracts rather than preserve an older caller.
