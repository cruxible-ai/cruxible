# Playbill coverage in Claude Code

Transparent coverage delivery for Claude Code, via a `PostToolUse` hook.

**Read this first: this integration annotates `Grep` results only.** That is a
limitation of the harness, not of Playbill, and the section below documents it
precisely so nobody has to rediscover it. If you control your own tool executor
— a benchmark harness, an agent framework, anything where you call the tools
yourself — use the middleware instead; it delivers coverage on all four tool
kinds including same-turn edit drift.

## Install

Merge `settings.hooks.json` into `.claude/settings.json` in your project, and
put a `.playbill/coverage.json` at the workspace root:

```json
{
  "tag": "playbill-coverage-workspace-config-v1",
  "instance_id": "inst_0123456789abcdef",
  "rules": [
    {
      "tag": "playbill-coverage-path-prefix-rule-v1",
      "path_prefix": "corpus/",
      "plane": "external",
      "identity_prefix": "corpus.",
      "normalizer": "playbill-coverage-path-identity-v1"
    },
    {
      "tag": "playbill-coverage-exact-path-rule-v1",
      "path": "docs/handbook.md",
      "plane": "external",
      "identity": "workspace.handbook"
    }
  ]
}
```

Bindings are **declared, never inferred.** Playbill will not guess that
`handbook.md` is the accepted source `documents/handbook.md`, because identical
bytes in another file are precisely not the same source — that guess is the
whole failure mode source-occurrence verification exists to prevent. A path with
no rule is simply not covered, silently: no card, no warning, no line of output.

Two rule forms:

- **Exact** — one path, one logical source, spelled out. The only form that can
  bind a `ledger` source, whose identity is a canonical artifact path.
- **Prefix** — everything under `path_prefix` was authored under
  `identity_prefix` through the named normalizer. The rule is a declaration that
  both sides used the same transformation, written down so both can be checked
  against it.

`playbill-coverage-path-identity-v1` is deliberately non-lossy: strip the
declared prefix, replace `/` with `.`, prepend the declared identity prefix,
stop. Nothing is lowercased and no extension is dropped, because every lossy
step is a way for two distinct working files to collide onto one accepted
source. `corpus/handbook.md` under the rule above is `corpus.handbook.md`,
extension and all — and that string must be exactly the
`logical_source_identity` the Claim's Capture was authored against. A produced
identity that does not satisfy the identity grammar binds nothing.

Exact rules beat prefix rules; among prefix rules, the longest match wins.

## What actually gets annotated, and why

The hook returns `hookSpecificOutput.updatedToolOutput`. That is **not** a
string appended to the tool result: Claude Code validates it against the tool's
own output schema and then renders the model-visible text through the tool's own
mapper, which builds that text from typed fields. Across the four tools there is
exactly one field that survives the trip as free text.

| Tool | Result shape | Rendered by | Coverage delivery |
|---|---|---|---|
| **Grep** (`mode: "content"`) | `{mode, numFiles, filenames, content, numLines, totalLines, …}` | `content`, verbatim | **Annotated.** Cards appended to `content`; every other field passed through unchanged |
| **Read** (`type: "text"`) | `{type, file: {filePath, content, numLines, startLine, totalLines}}` | `file.content`, through a **line numberer** | Observed only |
| **Edit** | `{filePath, oldString, newString, originalFile, structuredPatch, userModified, replaceAll}` | synthesized: `` `The file ${filePath} has been updated successfully.` `` | Observed only |
| **Write** | `{type, filePath, content, structuredPatch, …}` | synthesized: `` `File created successfully at: ${filePath}` `` | Observed only |

Appending to Read's `file.content` would present coverage cards as **numbered
file lines that do not exist in the file** — fabricated source content, and a
trap for the next edit that targets those line numbers. Edit and Write have no
free-text slot at all. So all three are consumed for observation: their paths
are resolved, which refreshes the local freshness manifest and its epoch, so the
next Grep answers against a current snapshot. Their output is returned
unmodified.

**`additionalContext` is never used.** It is the one channel that could carry
cards for all four tools, and Claude Code renders it inside a
`<system-reminder>` — the instruction channel, not the data channel. Coverage
cards are data about working files; delivering them as instructions would let a
card read as an order. This is a hard line, not a preference.

### Envelope version

This integration is written against the Claude Code **2.1.234** hook envelope,
pinned as `ENVELOPE_VERSION` in
`cruxible_core/playbill/coverage/claude_code.py`. The relevant facts, which the
published documentation did not state consistently:

- stdin carries `tool_response` (not `tool_result`), alongside `tool_name`,
  `tool_input`, `tool_use_id`, `hook_event_name`, and `duration_ms`;
- stdout's `hookSpecificOutput` requires `hookEventName`, and its PostToolUse
  member accepts `additionalContext`, `updatedToolOutput`, and
  `updatedMCPToolOutput`;
- `updatedToolOutput` that fails the tool's output schema is discarded with a
  warning and the original output is used.

A payload this table does not recognize produces no event and no output change.
Nothing here adapts to an unknown envelope by guessing.

## Failure behavior

The command always exits 0 and always emits one JSON object. If the daemon is
unreachable, a working file is unreadable, or the configuration is missing, the
tool result is returned unchanged plus — where a channel exists — one
`Playbill coverage: unavailable  [<code>]` line. A broken hook never breaks the
agent's tool call.

Semantics fail closed even while infrastructure fails open: a degraded delivery
carries no cards at all rather than a downgraded guess, and no coverage card
ever grants the working material a governance fact.

## The full-fidelity path

`cruxible_core.playbill.coverage.middleware` is the vendor-neutral adapter for a
harness that owns its tool executor:

```python
from pathlib import Path

from cruxible_core.playbill.coverage.middleware import (
    HarnessToolEventV1,
    coverage_middleware,
)

middleware = coverage_middleware(root=Path("."), resolve=my_resolver)

delivery = middleware.after_tool(
    HarnessToolEventV1(kind="edit", paths=("corpus/handbook.md",), original_output=tool_text)
)
tool_text = delivery.spliced()  # or splice the two halves yourself
```

`before_tool`, `after_tool`, and `after_filesystem_change` all return the
original output and the appended coverage text as **two separate strings**; the
caller does the splice. That is what makes "original tool output is preserved
and annotated, not replaced or suppressed" a structural property rather than a
promise — and it is why the middleware never touches a model channel at all.
