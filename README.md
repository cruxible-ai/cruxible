<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible

Cruxible is a toolset that lets AI agents create, update and manage what
they’ve learned through a typed, code-like interface, so they model the world
rather than solely write notes about it. Their observations and judgments
become building blocks that later sessions reuse instead of re-deriving.

Runs on Linux (x86-64) and macOS (Apple silicon) with Python 3.11+; on
Windows, use WSL2. [Install and start](#install-and-start).

<!-- TODO(0.5.0): the examples below are from one run on the integration build
(a shop's delivery promises; 2026-11-18, outputs and definitions in
wt-i/promise), trimmed: ids shortened; sequence numbers, generation ids and
times of day cut; the query's flags column cut. The run wrote the evidence as
a dict, not FileEvidence. Re-run them on the release build. -->

An agent recording a judgment with the Python SDK:

```python
w = cx.world()                        # typed handles for this shop's own vocabulary

# what a scheduled check last read from the supplier
widget = w.shop.item["blue-widget"]
stock = widget.in_stock[0]            # 40

# the judgment: promise Friday delivery, citing the stock line it rests on
w.shop.order.define("ord-1042", item=widget, needed=12).set(
    promised_for="2026-11-20",
    promise="open",
    evidence=FileEvidence(file='supplier/stock.json#{"sku": "blue-widget", "in_stock": 40}'),
).write(because=f"Friday delivery for order 1042: {stock.value} in stock, we need 12.")
# -> a proposal; it takes effect when the reviewer approves it
```

Agents today have two places to put what they learn. Notes in markdown are
easy to write and hold reasoning well, but they can only be searched, not
queried, and they pile up: a later note can contradict an earlier one, and
nothing marks which is current. Structured data can be checked and queried,
and a new value replaces the old one, but reducing everything an agent learns
to fields loses detail. Research on agent memory finds both costs. In
[LongMemEval](https://arxiv.org/abs/2410.10813) (ICLR 2025), storing only the
facts extracted from a conversation, in place of its text, lowered accuracy,
while keeping the text and indexing it with those facts beat the text alone.
In [MemoryAgentBench](https://arxiv.org/abs/2507.05257), when a later
statement contradicted an earlier one, the memory systems tested often failed
to answer from the newer one.

In Cruxible a fact keeps the reasoning it was recorded with:

```text
$ cruxible get shop.order/ord-1042 --detail history --field promise
promise
  proposed 2026-11-18 by owner, approved by reviewer  stated  open
      reason: Friday delivery for order 1042: 40 in stock, we need 12.
```

And a later statement replaces the earlier one, while the history keeps both:

```text
$ cruxible get shop.item/blue-widget --detail history --field in_stock
in_stock
  proposed 2026-11-18 by owner  changed  40 -> 5
      reason: As the supplier's stock feed says.  (Procedure shop.stock-check, run RUN-ab9c47be...)
  proposed 2026-11-18 by owner  stated  40
      reason: As the supplier's stock feed says.  (Procedure shop.stock-check, run RUN-c632bf87...)
```

Cruxible is designed to keep the strengths of both. Facts are typed and
queryable like structured data, and types can grow as agents learn more
about your domain. Each fact has one current value: a new one replaces it, and
the earlier values are kept as history. Reasoning stays in prose, in your own pages, and facts cite
the passages they rest on. Your files, repos and services remain the source
of truth: Cruxible points to them and keeps a snapshot only of the evidence a
fact was drawn from, so every claim can be traced back.

That stock level comes from a pipeline the shop approved, which re-reads the
supplier's feed on a schedule. When what it reads no longer covers a promise,
a saved query flags the order
([Lines and automation](docs/lines-and-automation.md) shows how both are
defined):

```text
$ cruxible line run shop.stock-check
Counts: written 1 (settled 1, proposed 0); unchanged 0; refused 0; inbox 0

$ cruxible query --name shop.promises_at_risk
subject              in_stock  needed  promised_for  sku
shop.order/ord-1042  5         12      2026-11-20    blue-widget
```

It runs unattended as a local daemon. Pipelines you approve re-check sources
on a schedule, and when the evidence behind a fact changes, the fact is
flagged instead of going quietly stale. Agents reach it through a Python SDK,
an MCP server or the CLI; all three talk to the same daemon, so they share one
record.

## Why this beats notes alone

- **Fresh, not silently stale.** Pipelines re-check facts on the schedule you
  set. When a source changes, the fact is updated or flagged, and a fact whose
  evidence is older than the window you set reads as stale instead of passing
  as current.
- **Knowledge compounds.** A fact checked once is reused by every later
  session and every agent until its evidence moves; work goes where the world
  changed.
- **Agents collaborate through one record.** Many agents and people share one
  governed ledger instead of overwriting each other's notes. Every change is a
  proposal; your approval policy decides what lands at once and what waits for
  a second agent's review.
- **Deterministic.** No LLM runs inside Cruxible. Agents and people propose
  and judge; Cruxible applies declared rules deterministically.

Herbert Simon's
[watchmakers](https://faculty.sites.iastate.edu/tesfatsi/archive/tesfatsi/ArchitectureOfComplexity.HSimon1962.pdf#page=5)
show why complex systems tend to be built from stable intermediate parts.
Tempus builds each watch in one go, and every interruption makes him start
over. Hora builds stable subassemblies, so an interruption costs only the
piece in hand. Agent sessions with fresh context windows work like Tempus:
context is pulled in on demand, and facts are re-derived and re-interpreted
each time. Cruxible gives agents Hora's subassemblies. Each accepted fact
has a fixed place: it belongs to the thing it describes, links to the things
it relates to, and keeps the evidence it was derived from. Ask about one
thing and everything known about it comes back together, so the cost of
working something out is paid once and knowledge accumulates.

## The security kit: the same loop on real work

A kit is a ready-made domain. The security kit keeps a project's dependencies,
their known vulnerabilities and your team's judgments about them current. One
Runbook reads the SBOM and asks OSV, CISA KEV and EPSS; here on a deliberately
vulnerable Django app with 32 components:

<!-- TODO(0.5.0): from the upgrade run on the integration build (2026-11-18,
wt-i/upgr/out/01, 02, 03, 11), trimmed: each Line's two output lines joined,
run ids, "succeeded", zero counts and the sixth Line cut (in the rerun also
the settled/proposed counts and Lines 5-6); ids shortened; the reason wrapped.
Re-run on the release build, and add the console screenshots here (the state
graph, and the judgment with what it rests on). -->

```text
$ cruxible runbook run security.start
   1  Line:security.sbom-import.app              written 32 (settled 32, proposed 0)
   2  Line:security.osv-query                    written 181 (settled 181, proposed 0)
   3  Line:security.kev-triage                   written 181 (settled 181, proposed 0)
   4  Line:security.epss-triage                  written 543 (settled 543, proposed 0)
   5  Line:security.exposure-attention.unjudged  inbox 7 new
```

Facts copied from sources settle without review. The seven inbox items are
unjudged exposures above the kit's exploit-likelihood threshold, and judging
one is the agent's work: it reads
the code, records its verdict with the passage it rests on, and a second agent
reviews it.

```text
$ cruxible get security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 --detail history
justification
  proposed 2026-11-18 by owner, approved by reviewer  stated  vulnerable_code_not_in_execute_path
      reason: CVE-2020-14343: the only yaml.load in the repo is in dockerized_labs/a9_uckv_lab, a
      separate container with its own requirements; the Django app this SBOM describes never imports yaml.
```

When PyYAML is upgraded and the Runbook runs again, only what changed is
written; everything else is re-checked and left as it is:

```text
$ cruxible runbook run security.start
   1  Line:security.sbom-import.app              written 1; unchanged 31; retired 1
   2  Line:security.osv-query                    written 1; unchanged 178
   3  Line:security.kev-triage                   written 0; unchanged 179
   4  Line:security.epss-triage                  written 0; unchanged 537
```

The [security walkthrough](docs/security-walkthrough.md) goes through all of
it on your own project. `cruxible console` opens a read-only view of the same
state in your browser.

## Install and start

Requirements: Python 3.11+ and Git. Linux x86-64 and macOS on Apple silicon;
on Windows, use WSL2.

```bash
pip install cruxible        # or: uv tool install cruxible
cd your-project
cruxible init --principal-id owner \
  --reviewer-key-dir ../reviewer-keys --require-independent-approval
cruxible orient          # what this instance holds
cruxible next            # what needs attention
```

`init` starts a local daemon if none is running and creates this project's
instance. The two reviewer options set up a second principal whose approval
every proposal needs; `cruxible init --principal-id owner` alone lets one
owner approve everything. The [Quickstart](docs/quickstart.md) continues from
here with the security kit.

## Use it from your agent

One daemon owns the state; three clients talk to it:

- **MCP:** an MCP client launches `cruxible mcp` (`uvx cruxible mcp` from the
  registry listing). See [MCP tools](docs/mcp-tools.md).
- **CLI:** every command above, for agents with a shell and for people.
- **Python SDK:** `cruxible-client`, installed with the package. It reads your
  vocabulary into typed Python, as in the example at the top. See the
  [SDK reference](packages/cruxible-client/README.md).

The verbs are the same on all three: `orient`, `query` and `get` read; `set`
and `write` change values; `authoring` changes definitions. `init` starts a
local daemon for you; `cruxible server start` runs one yourself.

The [plugin](plugins/README.md) for Claude Code and Codex adds five general
skills: `survey` works out what in a project with no kit is worth holding as
state, `adopt-kit` starts from a kit, and `create-state`, `automate-state` and
`review-state` cover the work after that.
[For AI agents](docs/for-ai-agents.md) covers how an agent should read and
write.

## How it works

- **Facts, vocabulary and evidence.** Each fact is a Claim about a Subject,
  under a ClaimType that says what the value may be and what may back it: the
  author's own words, a cited passage of a file, or a Capture (the slice of a
  source's response a fact rests on) under a declared contract. Agents extend
  the vocabulary as they learn, and queries follow the links between things.
  [Evidence and admission](docs/evidence-and-admission.md),
  [asking questions](docs/asking-questions.md).
- **Reviewed history.** Every change is a proposal, checked deterministically
  and accepted under your approval policy with signed approvals. Accepted
  state is a signed Git ledger. [Review and governance](docs/review-and-governance.md).
- **Procedures.** A Procedure is how repeatable work gets done with no agent
  in the loop. It is a reviewed, typed sequence of steps that reads accepted
  state, calls tools through declared interfaces (parse a dependency list,
  read a scanner's report, query an API), turns the results into typed
  records, and writes them back as facts, proposals or inbox items. The
  security kit's Procedures import an SBOM, match it against three
  vulnerability sources and flag the unjudged exposures worth attention. Runs
  are deterministic and replayable: each tool is pinned to an exact build,
  inputs and outputs are checked against contracts, and nothing reads the
  clock. A Procedure does only what it has been granted: observe, propose for
  review, or settle named fields on its own.
- **Lines and attention.** A Line runs a Procedure with its inputs, budgets
  and an authority ceiling, on Triggers (a schedule, a new capture, an
  accepted change), once you enable it. Agents author new ones as they find
  sources worth watching. `next` lists what needs attention; `cruxible link`
  attaches notes to Subjects and review guidance to ClaimTypes.
  [Lines and automation](docs/lines-and-automation.md).
- **Kits.** A domain (vocabulary, pipelines, guidance) travels between
  instances as a kit, installed and upgraded as one reviewed change.
  [Kits](docs/kits.md).

The [concepts guide](docs/concepts.md) explains all of this in order.

## Credentials and principals

A credential is a daemon bearer token with a permission tier; a principal is a
signing key that attributes acts and signs approvals. See
[Who acts](docs/concepts.md#who-acts).

## Documentation

- [Quickstart](docs/quickstart.md) and the
  [security walkthrough](docs/security-walkthrough.md)
- [Concepts](docs/concepts.md)
- [For AI agents](docs/for-ai-agents.md)
- [Asking questions](docs/asking-questions.md),
  [evidence and admission](docs/evidence-and-admission.md),
  [review and governance](docs/review-and-governance.md),
  [lines and automation](docs/lines-and-automation.md),
  [kits](docs/kits.md)
- [CLI reference](docs/cli-reference.md), [MCP tools](docs/mcp-tools.md),
  [Python SDK](packages/cruxible-client/README.md)
- [Upgrading](docs/upgrading.md), [data and network](docs/data-and-network.md)

## Develop

```bash
git clone https://github.com/cruxible-ai/cruxible
cd cruxible
uv sync --all-packages --all-extras
uv run pytest -n auto --dist loadfile
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the provider-runtime tests, lint,
format and type checks.

Apache-2.0 licensed.

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
