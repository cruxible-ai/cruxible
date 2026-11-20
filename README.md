<p align="center">
  <a href="https://cruxible.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-white.svg">
      <img src="https://raw.githubusercontent.com/cruxible-ai/cruxible/main/assets/brand/cruxible-wordmark-black.svg" alt="Cruxible" width="360">
    </picture>
  </a>
</p>

# Cruxible

**Governed, cross-source state for AI agents.**

Cruxible is a local daemon that lets AI agents and people maintain typed,
evidence-backed state across sources—so observations and judgments compound
across sessions instead of getting re-derived from scratch or going quietly
stale.

- **Automated pipelines** re-check external sources on a schedule and settle
  mechanical facts without an agent in the loop.
- **Agents record judgments** through a typed Python SDK, MCP server, or CLI,
  citing the exact source data or code passages they rest on, subject to signed
  review.
- **When inputs move**, only the affected facts and judgments surface for
  re-checking—everything else stays current and untouched.

No LLM runs inside Cruxible. Accepted state is a signed Git ledger with
per-field history and provenance. Runs on Linux (x86-64) and macOS (Apple
silicon) with Python 3.11+; on Windows, use WSL2. [Install and
start](#install-and-start).

## See it work: vulnerability triage across five sources

Deciding whether a CVE actually affects an application combines an SBOM, OSV,
CISA KEV, EPSS, your own source code, and a team's waiver rationale. No single
scanner owns the whole picture, and Markdown notes drift as soon as a
dependency or file changes.

With Cruxible's **security kit** on a Django app with 32 components:

### 1. Pull external facts and surface what needs judgment

One Runbook reads the SBOM and queries OSV, CISA KEV, and EPSS. Copying facts
from declared sources is mechanical, so those settle under explicit, expiring
mandates—recording both what was found and verified absences (like "not in KEV"
only when the full catalog was read). Seven unjudged exposures above the
exploit-likelihood threshold land in the inbox:

```text
$ cruxible runbook run security.start
   1  Line:security.sbom-import.app              written 32 (settled 32, proposed 0)
   2  Line:security.osv-query                    written 181 (settled 181, proposed 0)
   3  Line:security.kev-triage                   written 181 (settled 181, proposed 0)
   4  Line:security.epss-triage                  written 543 (settled 543, proposed 0)
   5  Line:security.exposure-attention.unjudged  inbox 7 new
```

### 2. Record a judgment citing exact code; review and sign it

Judging reachability is the agent's job. It inspects the repository, finds that
the app's SBOM pins `PyYAML 5.1` while the only `yaml.load` lives in a separate
container, and proposes a VEX justification citing that exact line:

```python
from cruxible_client import Cruxible
from cruxible_client.contracts.write import FileEvidence

cx = Cruxible.connect()
batch = cx.changes(
    because="CVE-2020-14343: the only yaml.load in the repo is in dockerized_labs/a9_uckv_lab, "
            "a separate container with its own requirements; the Django app never imports yaml.",
)
pair = batch.create(
    "security.app_exposure",
    label="pair",
    key={
        "occurrence": "security.occurrence/sbom.cdx.json--pkg-pypi-pyyaml-5.1",
        "vulnerability": "security.vulnerability/cve-2020-14343",
    },
)
batch.set(
    pair,
    "justification",
    "vulnerable_code_not_in_execute_path",
    evidence=FileEvidence(
        file="dockerized_labs/a9_uckv_lab/app.py#data = yaml.load(file.stream, Loader=yaml.Loader)"
    ),
)
batch.write(accept="never")  # -> proposal; a second principal reviews and signs it
```

A second agent (or human) acting as `reviewer` sees the domain guidance, the
proposed verdict, and the cited line before signing off:

```text
$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review sha256:54251d28...
Guidance for ClaimType:security.app_exposure.justification: document:security.guidance.dependency (Judging a dependency exposure)
Values (4):
  create security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 justification: vulnerable_code_not_in_execute_path  [CLM-34cb42d7...; verdict supported]
    cites CAP-0e7901adf211 dockerized_labs/a9_uckv_lab/app.py#data = yaml.load(file.stream, Loader=yaml.Loader)
      | data = yaml.load(file.stream, Loader=yaml.Loader)
```

### 3. When inputs move, re-check only what changed

When `PyYAML` is upgraded and the Runbook runs again, only the changed
dependency and vulnerability are written; every other component and judgment is
re-checked and left alone:

```text
$ cruxible runbook run security.start
   1  Line:security.sbom-import.app              written 1; unchanged 31; retired 1
   2  Line:security.osv-query                    written 1; unchanged 178
   3  Line:security.kev-triage                   written 0; unchanged 179
   4  Line:security.epss-triage                  written 0; unchanged 537
```

If someone instead edits the cited line in `dockerized_labs/a9_uckv_lab/app.py`,
`cruxible next` brings that specific judgment back as `citation_drifted` so a
later session re-verifies it instead of trusting a stale waiver. Every field
keeps its signed history and provenance:

```text
$ cruxible get security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 --detail history
justification
  proposed 2026-11-18 by owner, approved by reviewer  stated  vulnerable_code_not_in_execute_path
      reason: CVE-2020-14343: the only yaml.load in the repo is in dockerized_labs/a9_uckv_lab, a
      separate container with its own requirements; the Django app this SBOM describes never imports yaml.
```

See the full [security walkthrough](docs/security-walkthrough.md) to run this
on your own repository. `cruxible console` opens a read-only view of the same
state in your browser.

## Install and start

Requirements: Python 3.11+ and Git. Linux x86-64 and macOS on Apple silicon; on
Windows, use WSL2.

```bash
pip install cruxible        # or: uv tool install cruxible
cd your-project
cruxible init --principal-id owner
cruxible orient             # map of kinds, fields, and counts
cruxible next               # what needs attention right now
```

`cruxible init` starts a local daemon if none is running and creates this
project's instance. For two-principal **cosign**—where a second agent or person
must approve every proposal before it counts—initialize with a reviewer:

```bash
cruxible init --principal-id owner \
  --reviewer-key-dir ../reviewer-keys --require-independent-approval
```

Continue with the [Quickstart](docs/quickstart.md) to install the security kit
or define your own vocabulary.

## Why Cruxible

Autonomous, multi-session work hits structural limits with plain notes and
single-purpose tools alike:

- **Notes + grep/RAG cannot track invalidation or exact sets.** Markdown holds
  reasoning well, but it cannot wake up when an upstream dependency or cited
  line changes, reliably answer closed-world questions (`NOT EXISTS`, exact
  counts), resolve contradictions across append-only files, or refuse a bad
  write. Benchmarks show both sides of the trade-off: in
  [LongMemEval](https://arxiv.org/abs/2410.10813) (ICLR 2025), keeping only
  extracted facts lowered accuracy while indexing text alongside structured
  facts beat text alone; in
  [MemoryAgentBench](https://arxiv.org/abs/2507.05257), tested memory systems
  frequently failed to prefer newer statements over older contradicting ones.
- **Single-purpose tools do not own cross-source judgments.** Your repos,
  databases, and SaaS tools remain the sources of truth for their own records.
  Cruxible sits at the judgment layer across them—joining external feeds, local
  files, and team decisions without copying whole tables or locking your
  decision history inside one vendor.

Cruxible combines typed state, linked prose, and deterministic automation:

- **Typed state linked to prose.** Each fact has one current value (with full
  history) and types grow as your domain does. Reasoning stays in your own
  Markdown pages, and facts cite the exact passages they rest on.
- **Freshness and completeness, not silent drift.** Pipelines re-check facts on
  a schedule, `stale_after` flags expired evidence, `citation_drifted` catches
  edited files, and Cruxible distinguishes **found**, verified **none** (from a
  complete source read), and **unknown**—so a failed lookup never reads as
  "safe."
- **Graduated automation (`observe` → `propose` → `settle`).** Pipelines start
  by observing or proposing for review, and only settle named fields on their
  own under explicit, scoped, expiring mandates.
- **Governed and verifiable.** Every change is a proposal checked
  deterministically under your approval policy, recorded in a signed Git ledger
  where any earlier generation can be read back as it stood.
- **Stable subassemblies across sessions.** Like Hora in Herbert Simon's
  [watchmakers](https://faculty.sites.iastate.edu/tesfatsi/archive/tesfatsi/ArchitectureOfComplexity.HSimon1962.pdf#page=5),
  agents build reusable, verified intermediate facts instead of re-deriving
  context from scratch every time a window resets.

## Use it from your agent

One daemon owns the state; three clients talk to it:

- **MCP:** configure your MCP client to launch `cruxible mcp` (`uvx cruxible
  mcp` from the registry listing). See [MCP tools](docs/mcp-tools.md).
- **CLI:** every command above, with `--json` for scripts and shell-capable
  agents. See the [CLI reference](docs/cli-reference.md).
- **Python SDK:** `cruxible-client`, installed with `cruxible`, loads your
  vocabulary into typed Python handles and atomic write batches. See the
  [SDK reference](packages/cruxible-client/README.md).

All three share the same verbs: `orient`, `query` and `get` read; `set` and
`write` change values; `authoring` changes definitions. `cruxible init` starts
a local daemon automatically; `cruxible server start` runs one explicitly.

The [agent plugin](plugins/README.md) for Claude Code and Codex adds five
skills: `survey` works out what in a project is worth holding as state,
`adopt-kit` starts from a kit, and `create-state`, `automate-state` and
`review-state` cover ongoing work. See [For AI agents](docs/for-ai-agents.md).

## How it works

- **Claims, Subjects, and Evidence.** Each fact is a Claim about a Subject
  under a ClaimType that declares its value type, cardinality, freshness, and
  admitted evidence: the author's own words, a cited passage in a workspace
  file, or a retained Capture under a declared contract.
  ([Evidence and admission](docs/evidence-and-admission.md),
  [Asking questions](docs/asking-questions.md))
- **Proposals and signed history.** Every mutation is a proposal checked
  deterministically and accepted under your approval policy with signed
  approvals into a Git ledger. High-rate run telemetry stays in operational
  exhaust outside the ledger.
  ([Review and governance](docs/review-and-governance.md),
  [Architecture](docs/architecture.md))
- **Procedures, Lines, and Triggers.** A Procedure is a reviewed, replayable
  graph of steps that reads accepted state, invokes pinned provider builds
  through capability slots, extracts typed records, and proposes, settles, or
  posts to the inbox. A Line binds a Procedure's inputs, budgets, and authority
  ceiling to Triggers (cron, cadence, capture landing, or accepted changes).
  ([Lines and automation](docs/lines-and-automation.md))
- **Kits.** A domain—its vocabulary, queries, guidance documents, Procedures,
  and bundled providers—travels between instances as a kit, installed and
  upgraded as reviewed changes. ([Kits](docs/kits.md))
- **Credentials and principals.** A credential is a daemon bearer token with a
  permission tier (`read_only`, `governed_write`, `graph_write`, `admin`); a
  principal is a signing key that attributes acts and signs approvals.
  ([Who acts](docs/concepts.md#who-acts))

The [concepts guide](docs/concepts.md) walks through all of these in order.

## Documentation

- [Quickstart](docs/quickstart.md) and the
  [security walkthrough](docs/security-walkthrough.md)
- [Concepts](docs/concepts.md) and [Architecture](docs/architecture.md)
- [For AI agents](docs/for-ai-agents.md) and
  [Building team knowledge](docs/building-team-knowledge.md)
- [Asking questions](docs/asking-questions.md),
  [Documents and notes](docs/documents-and-notes.md),
  [Evidence and admission](docs/evidence-and-admission.md),
  [Keeping knowledge current](docs/keeping-knowledge-current.md)
- [Review and governance](docs/review-and-governance.md),
  [Lines and automation](docs/lines-and-automation.md),
  [Kits](docs/kits.md)
- [CLI reference](docs/cli-reference.md), [MCP tools](docs/mcp-tools.md),
  [Python SDK](packages/cruxible-client/README.md)
- [Upgrading](docs/upgrading.md), [Data and network](docs/data-and-network.md)

## Develop

```bash
git clone https://github.com/cruxible-ai/cruxible
cd cruxible
uv sync --all-packages --all-extras
uv run pytest -n auto --dist loadfile
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for provider-runtime tests, lint,
format, and type checks.

Apache-2.0 licensed.

<!-- mcp-name: io.github.cruxible-ai/cruxible-core -->
