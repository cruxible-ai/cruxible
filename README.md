<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible

A knowledge layer for AI agents that models the world instead of writing
notes about it, so their work compounds instead of piling up.

It is the long-term memory your agents share, kept true by checking it
against the world. Agents record what they learn as typed facts, each with
its evidence. Automation pipelines you define keep those facts current: they
fetch the sources you point them at, extract what matters and re-check the
facts that depend on them. When evidence changes (an advisory is revised, a
cited file is edited, a scan finds something new), Cruxible flags the facts
resting on it, and the conclusions built on those facts too. What hasn't moved
stays settled, so the next session, or the next agent, starts from what is
already known instead of working it out again.

## Why this beats notes alone

Notes are good at prose: reasoning, context, decisions in your own words.
Cruxible keeps them, and facts cite them. What notes alone can't do is stay
true. Agent sessions produce endless markdown, each page true when written and
never checked again, so every new session reads and re-derives it. Retrieval
finds relevant text; Cruxible tracks the facts your agents record from it, the
evidence behind them, and what needs another look when that evidence changes.
You keep the strengths of notes and lose their weaknesses:

- **Fresh, not silently stale.** Pipelines re-check facts on the schedule you
  set, and every answer shows how fresh its evidence is.
- **Changes ripple to what depends on them.** When a fact moves, the
  judgments and derived facts built on it are flagged as well, naming the
  input that changed, so second-order conclusions don't quietly go wrong.
- **Knowledge compounds.** A fact checked once is reused by every later
  session and every agent until its evidence moves; work goes where the world
  changed.
- **Agents collaborate through one record.** Many agents and people share one
  governed ledger instead of overwriting each other's notes. Every change is a
  proposal; your approval policy decides what lands at once and what waits for
  a second agent's review.
- **Every belief has a receipt.** What was concluded, from which evidence,
  accepted by whom and when. Each fact keeps its own history, and earlier
  values can be read back.
- **Never a second source of truth.** Your files, repos and services stay
  authoritative. Cruxible holds the conclusions drawn from them, with pointers
  back to the source, and keeps only the slices of evidence each conclusion
  rests on, so you can always see what was seen and when.
- **Deterministic.** No LLM runs inside Cruxible. Agents and people propose
  and judge; Cruxible applies declared rules deterministically.

Herbert Simon's watchmakers make the point. Tempus builds each watch in one
go, and every interruption scatters the parts; Hora builds stable
subassemblies, so an interruption costs only the piece in hand. Agent
sessions are interrupted all the time. Notes are Tempus. Each accepted fact
in Cruxible is one of Hora's subassemblies: it stays put, so the work done so
far survives and the next session builds on it.

## Don't replace your markdown, bring it to life

Your pages stay where they are. Facts cite passages in them, and long prose
stays in pages instead of being forced into fields. Tables generated from
facts are flagged when the facts behind them change and re-rendered on sync,
and you can browse the current facts as plain files you can grep.

## A short example

The security kit keeps a project's dependencies, their known vulnerabilities
and your team's judgments about them current.

<!-- TODO: replace with output from the security walkthrough rerun after integration. -->

~~~text
$ cruxible kit add ../cruxible-kits/kits/security --commit --at OID
$ cruxible runbook run security.start          # read the SBOM, check OSV, KEV and EPSS
$ cruxible query --name security.urgent_exposures --limit 3
<rows: the unjudged exposures, most likely to be exploited first>

$ cruxible set <exposure> verdict not_affected \
    --because "We never call the vulnerable function; see the scanner result."
<proposal; a second agent reviews and approves it>
~~~

A day later the scheduled run finds that the advisory behind that judgment has
changed:

~~~text
$ cruxible next
<one row: the judgment, the advisory that changed, and what to do>
~~~

The hundreds of facts whose sources were unchanged were re-checked and stay
settled; the run that re-checked them stored almost nothing.
<!-- TODO: keep the storage clause only if the release measurement confirms it. -->
<!-- TODO: confirm in the rerun how far the dependency flags reach in one step. -->


## Install and start

Requirements: Python 3.11+ and Git. Linux x86-64 and macOS on Apple silicon;
on Windows, use WSL2.

~~~bash
pip install cruxible        # or: uv tool install cruxible
cruxible server start --socket ~/.cruxible/run/daemon.sock
~~~

In another shell, inside the Git repository you want to work in:

~~~bash
export CRUXIBLE_SERVER_SOCKET=~/.cruxible/run/daemon.sock
cruxible init
cruxible orient          # what this instance holds
cruxible next            # what needs attention
~~~

The [Quickstart](docs/quickstart.md) continues with the security kit on your
own project.

## Use it from your agent

One daemon owns the state; three clients talk to it:

- **MCP:** an MCP client launches `cruxible mcp` (`uvx cruxible mcp` from the
  registry listing). See [MCP tools](docs/mcp-tools.md).
- **CLI:** every command above, for agents with a shell and for people.
- **Python SDK:** `cruxible-client`, installed with the package. See the
  [SDK reference](packages/cruxible-client/README.md).

Knowledge as code: the SDK reads your vocabulary into typed Python, so an
agent writes facts the way it writes code. Defining something that already
exists lands on it instead of making a duplicate.

~~~python
from cruxible_client import Cruxible

with Cruxible.connect() as cx:
    w = cx.world()
    requests = w.pypi.release.define(package="requests", version="2.32.4")
    w.write(
        requests,
        w.app.service.define("payments").add(depends_on=requests),
        because="Pinned in the payments service's requirements.txt.",
    )
~~~

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
- **Pipelines and attention.** A Procedure is a governed, deterministic
  pipeline: read state, call providers (fetch a URL, read a file, run a
  scanner), extract typed records, and propose or confirm facts. A Line runs
  it on Triggers (a schedule, a new capture, an accepted change), and agents
  author new ones as they find sources worth watching. `next` lists what needs
  attention; `cruxible link` attaches notes to Subjects and review guidance to
  ClaimTypes. [Lines and automation](docs/lines-and-automation.md).
- **Kits.** A domain (vocabulary, pipelines, guidance) travels between
  instances as a kit, installed and upgraded as one reviewed change.
  [Kits](docs/kits.md).

The [concepts guide](docs/concepts.md) explains all of this in order.

## Credentials and principals

A credential (`cruxible credential`) is a daemon bearer token with a tier; a
principal (`cruxible principal`) is a signing key that attributes acts and
signs approvals, and its private key never leaves the client. A Unix-socket
daemon runs with auth off and says so; a TCP daemon refuses to start without
`--auth`. [Who acts](docs/concepts.md#who-acts).

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

~~~bash
git clone https://github.com/cruxible-ai/cruxible
cd cruxible
uv sync --all-packages --all-extras
uv run pytest -n auto --dist loadfile
~~~

See [CONTRIBUTING.md](CONTRIBUTING.md) for the provider-runtime tests, lint,
format and type checks.

Apache-2.0 licensed.

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
