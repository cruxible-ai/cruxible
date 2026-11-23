# Quickstart

Point Cruxible at one of your projects, read in its dependencies, and find out
which of them carry a known vulnerability, which of those attackers are
exploiting (CISA's KEV catalog) and how likely exploitation is (FIRST's EPSS
score). Every answer traces back to its source.

The steps below start with a single owner (`self_approval_allowed`) so you
reach your first answer in a few commands, then turn on two-principal **cosign**
(step 7) before bringing in an agent so every judgment waits for a reviewer.

You'll need Python 3.11+, Git, a project in a Git repository, a tool that
writes a CycloneDX SBOM for it (step 3), and network access to `pypi.org`,
`api.osv.dev`, `www.cisa.gov` and `api.first.org`. Until the security kit's
registry release, you also need a clone of the
[kits repository](https://github.com/cruxible-ai/cruxible-kits); the commands
below assume it sits beside your project as `../cruxible-kits`.

## Supported platforms

Supported: Linux x86-64 and macOS on Apple silicon; on Windows, use WSL2.
Cruxible does not run natively on Windows yet: install Python and Cruxible
inside your WSL2 Linux distribution and run every command there.

## 1. Install Cruxible and attach your project

~~~bash
pip install cruxible        # or: uv tool install cruxible
cd your-project
cruxible init --principal-id owner
~~~

`init` starts a daemon if none is running (`cruxible server stop` stops it),
creates this project's instance with you as its owner under
`self_approval_allowed`, installs the base kit at once, and remembers the
connection so later commands in this folder need no flags. (To start with
two-principal cosign from the first command instead, see step 7 or the
[Security walkthrough](security-walkthrough.md).)

What lands in `.cruxible/`, and what to commit:

| File | Written by | Commit? |
|---|---|---|
| `coverage.json` | `init`: this folder's daemon transport and instance | no: machine-local, added to `.git/info/exclude` |
| `identity.json` | `init`: the owner's principal ID and key directory path (never a key) | no: machine-local, added to `.git/info/exclude` |
| `world.pyi` | the daemon's floor delivery: the vocabulary as Python types | no: derived, added to `.git/info/exclude` |
| `floor/` | the daemon's floor delivery: one file per Subject, for grep | no: derived; its own `.gitignore` ignores it |
| `files.yaml` | the first file you catalogue | yes: the portable file catalog (`files.yaml.lock` is excluded) |
| `vocabulary.yaml` | `cruxible vocabulary export` | yes: your kinds and fields as one editable file |

In a Git repository the daemon also mirrors the ledger into the repository as
the `cruxible-ledger` remote (`cruxible-ledger/accepted` and
`cruxible-ledger/proposals/*`, in `.git/config`, nothing to commit):
`git diff cruxible-ledger/accepted...cruxible-ledger/proposals/<key>` shows a
proposal's change, which `cruxible proposal review --detail` prints.

## 2. Install the security kit

<!-- publish: kit tag -->
~~~bash
cruxible kit add ../cruxible-kits/kits/security                   # shows what it would install; writes nothing
cruxible kit add ../cruxible-kits/kits/security --commit --at OID # OID: the coordinate the preview printed
~~~

After the registry release, `cruxible kit add security:1.3.0` does the same.
Under `self_approval_allowed`, `--commit` installs the kit's three provider
packages (OSV, a web fetcher for KEV and EPSS, a SARIF reader) and its
definitions in one step.

## 3. Write an SBOM

The kit reads your dependencies from a CycloneDX JSON SBOM in the repository.
For a `requirements.txt`:

~~~bash
uvx --from cyclonedx-bom cyclonedx-py requirements requirements.txt --of JSON -o sbom.cdx.json
~~~

[Step 3 of the walkthrough](security-walkthrough.md#step-3-describe-your-dependencies)
names generators for other ecosystems.

## 4. Adopt the automation

The kit brings definitions only. The checks that run on their own, called
**Lines**, come as a template, `automation.json` in the kit directory, which
you copy and tailor: the SBOM path (`relative_path`, `sbom.cdx.json` as
shipped), each **Mandate**'s `valid_from` and `expires_at`, and the daily cron
time (14:00 UTC as shipped). Then submit it:

~~~bash
cp ../cruxible-kits/kits/security/automation.json .
# edit relative_path, the Mandate dates and the cron time
cruxible authoring submit automation.json --brief
~~~

This lands the Lines, Triggers and Mandates so the four source Lines (SBOM
import, OSV, KEV, EPSS) can settle what they copy from their sources; no
Mandate settles a judgment.

## 5. Run it once, then keep it current

~~~bash
cruxible runbook preview security.start     # what each step reads, writes and calls; runs nothing
cruxible line enable security.osv-unkeyed --runbook security.start   # the seven Lines
cruxible runbook run security.start         # SBOM import, OSV, KEV, EPSS, then the attention Lines
~~~

The run's calls are real and took about half a minute on the project below.
With the seven Lines enabled, the `security.maintain-daily` Trigger runs OSV,
KEV, EPSS and the attention Lines once a day while the daemon is running, and
`security.osv-unkeyed` runs whenever an OSV result lands (enable it before the
first run, or that run's result posts nothing). The SBOM import is not on the
clock: rerun `cruxible runbook run security.start` when the SBOM changes.

## 6. Ask the first question

**Which vulnerabilities do we look at first?**

~~~text
$ cruxible query --name security.urgent_exposures --limit 5
subject                                component            cve_id          epss_percentile  epss_score  inventory      kev_date_added  kev_due_date  kev_ransomware_use  occurrence                          scope     flags
security.vulnerability/cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  0.99008          0.53447     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-39614  pkg:pypi/django@4.2  CVE-2024-39614  0.98169          0.28637     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-57833  pkg:pypi/django@4.2  CVE-2025-57833  0.98149          0.28271     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
...
~~~

This lists every exposure nobody has judged: each vulnerability OSV lists for
a release your SBOM names, KEV-listed first (the `kev_` columns), then by EPSS
score. On [pygoat](https://github.com/adeyosemanputra/pygoat), the project the
walkthrough uses, none is on KEV, and Django 4.2's CVE-2023-46695 is the most
likely to be exploited. `cruxible next` lists what needs attention: here the
exposures the attention Line posted to your inbox, and one summary item for
the OSV advisories that have no CVE id yet.

Whether the vulnerable code is reachable in your project is a judgment: a
write that cites the code. [Step 7 of the walkthrough](security-walkthrough.md#step-7-judge-an-exposure)
records one.

## 7. Turn on two-principal cosign

Before letting an agent propose judgments, switch the instance to
`independent_approval_required` so nothing its author proposes activates until
a second principal (`reviewer`) reviews and signs it. Add the reviewer first,
then submit the approval policy:

~~~bash
cruxible principal add reviewer --key-dir ../reviewer-keys --signer-key OWNER_KEY --mode graph_write
cruxible authoring example approval-policy > policy.json
cruxible authoring submit policy.json
~~~

Add the reviewer before submitting `policy.json`: once
`independent_approval_required` lands, the owner can no longer activate a
change alone. From then on, `CRUXIBLE_PRINCIPAL_ID=reviewer` makes one command
act as the reviewer (`cruxible context use --principal reviewer` switches the
shell until you switch back):

~~~bash
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next                                 # what waits for the reviewer
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review PROPOSAL_ID          # inspect the change and cited evidence
CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve PROPOSAL_ID --yes   # sign and activate it
~~~

On a new project, `cruxible init --principal-id owner
--require-independent-approval` sets up both principals and the policy from
the start: every setup step then waits for the reviewer, as in the
[Security walkthrough](security-walkthrough.md). The reviewer's key goes
beside the owner's in your per-user config directory (`--reviewer-key-dir DIR`
puts it elsewhere), and init prints the `cruxible context use --principal
reviewer` that switches to it. Cosign between agents on
one machine is a convention, not a security boundary: both keys are files your
OS user can read. See [Review and governance](review-and-governance.md).

## 8. Bring in your agent

Connect a coding agent to the same instance through MCP or the CLI
([For AI agents](for-ai-agents.md)). Started in this project, it acts as the
owner with no further setup. `cruxible orient` and `cruxible next` show it
what is there and what needs attention, and the kit's governed guidance
(`cruxible get Document:security.guidance.dependency`) says how to judge an
exposure. With cosign enabled, what it proposes waits for a second agent (or
person) running as `reviewer`, as above. The agent plugin for Claude Code and
Codex adds five general skills (`survey`, `adopt-kit`, `create-state`,
`automate-state` and `review-state`); [For AI agents](for-ai-agents.md#connect)
says where to find it.

## 9. Look around in the console

~~~bash
cruxible console
~~~

This opens a read-only console in your browser: the dashboard, what each kind
of thing holds, open proposals, the Lines and their runs, the ledger. It runs
on your machine only (`127.0.0.1`), reads from the daemon `init` started, and
cannot change anything; approving and the like stay on the command line. The
link it prints signs one browser in, once; Ctrl-C stops it.

## If something goes wrong

- **The daemon warns `Provider lane unavailable` when it starts.** The daemon
  runs the kit's providers in a separate process reached through a Unix socket
  under its state root, and that path is too long, with no `TMPDIR` (macOS) or
  `XDG_RUNTIME_DIR` (Linux) to fall back on. Everything else works, but
  `kit add` and the runs refuse with the same cause. The warning names the
  budget: the directory may be at most 57 bytes long. Set
  `TMPDIR=$(getconf DARWIN_USER_TEMP_DIR)` on macOS or
  `XDG_RUNTIME_DIR=/run/user/$UID` on Linux (any short private directory you
  own, mode 0700, works), or use a shorter `--state-root`, and restart the
  daemon.
- **A proposal does not activate.** Under `independent_approval_required` its
  author's own `proposal approve` is refused
  (`cruxible.approval.creator_forbidden`): the reviewer approves it.
  `CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next` lists what waits.
- **The daily run stops at its first step.** A run the Trigger starts runs
  each step under its Line's own enablement, and a step whose Line is not
  enabled stops the run there; `next` lists a Line that was never enabled as
  `line_not_enabled`. Enable the Lines (step 5). (A `runbook run` you start
  yourself runs every step as you, enabled or not.)
- **The daily run did not happen.** A scheduled Trigger fires only while the
  daemon is running, and an instant missed while it was down is not run
  afterwards: `next` shows a `trigger_instants_missed` row for
  `security.maintain-daily`. Run `cruxible runbook run security.maintain` to
  catch up.
- **New dependencies have no answer yet.** OSV is asked once a day. After
  writing a new SBOM, run `cruxible runbook run security.start`.

## What's next

<!-- publish: kit tag -->

- The [Security walkthrough](security-walkthrough.md) goes through the same
  steps with their outputs, then judges three exposures, from the SDK, the CLI
  and MCP, each approved by the reviewer.
- [Concepts](concepts.md) explains the ideas behind what you just did.
- The [security kit's README](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/README.md)
  covers everything else it answers, including the audit loop for findings in
  your own code.
- [For AI agents](for-ai-agents.md) covers connecting an agent in depth.
