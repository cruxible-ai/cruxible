# Quickstart

Point Cruxible at one of your projects, read in its dependencies, and find out
which of them carry a known vulnerability, which of those attackers are
exploiting (CISA's KEV catalog) and how likely exploitation is (FIRST's EPSS
score). Every answer traces back to its source, and a judgment waits for
review before it counts.

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
creates this project's instance with you as its owner, installs the base kit,
and remembers the connection, so later commands in this folder need no flags.

## 2. Install the security kit

<!-- publish: kit tag -->
~~~bash
cruxible kit add ../cruxible-kits/kits/security                   # shows what it would install; writes nothing
cruxible kit add ../cruxible-kits/kits/security --commit --at OID # OID: the coordinate the preview printed
~~~

After the registry release, `cruxible kit add security:1.3.0` does the same.
The commit installs the kit's provider packages (OSV, a web fetcher for KEV
and EPSS, a SARIF reader) and then its definitions.

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
time (14:00 UTC as shipped). Then submit it, read it, and approve it:

~~~bash
cp ../cruxible-kits/kits/security/automation.json .
# edit relative_path, the Mandate dates and the cron time
cruxible authoring submit automation.json --no-accept --brief
cruxible proposal review PROPOSAL_ID              # the proposal the submit printed
cruxible proposal approve PROPOSAL_ID --yes
~~~

Approving it lets the four source Lines (SBOM import, OSV, KEV, EPSS) settle
what they copy from their sources; no Mandate settles a judgment. With one
owner (`self_approval_allowed`, the default), your own approval signs and
activates it.

## 5. Run it once, then keep it current

~~~bash
cruxible runbook preview security.start     # what each step reads, writes and calls; runs nothing
cruxible runbook run security.start         # SBOM import, OSV, KEV, EPSS, then the attention Lines
for l in sbom-import.app osv-query kev-triage epss-triage exposure-attention.unjudged exposure-attention.contested; do
  cruxible line enable security.$l
done
~~~

The run's calls are real and take about a minute. Once the Lines are enabled,
the `security.maintain-daily` Trigger runs OSV, KEV, EPSS and the attention
Lines once a day while the daemon is running. The SBOM import is not on the
clock: rerun `cruxible runbook run security.start` when the SBOM changes.

## 6. Ask the first question

**Which vulnerabilities do we look at first?**

~~~text
$ cruxible query --name security.urgent_exposures --limit 5
subject                                component            cve_id          epss_percentile  epss_score  inventory      kev_date_added  kev_due_date  kev_ransomware_use  occurrence                          scope     flags
security.vulnerability/cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  0.99007          0.53447     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-39614  pkg:pypi/django@4.2  CVE-2024-39614  0.98168          0.28637     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-57833  pkg:pypi/django@4.2  CVE-2025-57833  0.98147          0.28271     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
...
~~~

This lists every exposure nobody has judged: each vulnerability OSV lists for
a release your SBOM names, KEV-listed first (the `kev_` columns), then by EPSS
score. On [pygoat](https://github.com/adeyosemanputra/pygoat), the project the
walkthrough uses, none is on KEV, and Django 4.2's CVE-2023-46695 is the most
likely to be exploited. `cruxible next` lists what needs attention, starting
with the exposures the attention Line posted to your inbox.

Whether the vulnerable code is reachable in your project is a judgment: a
write that cites the code and waits for review.
[Step 7 of the walkthrough](security-walkthrough.md#step-7-judge-an-exposure)
records one.

## 7. Bring in your agent

Connect a coding agent to the same instance through MCP or the CLI
([For AI agents](for-ai-agents.md)). Started in this project, it acts as the
owner with no further setup. `cruxible orient` and `cruxible next` show it
what is there and what needs attention, and the kit's governed guidance
(`cruxible get Document:security.guidance.dependency`) says how to judge an
exposure.

## 8. Look around in the console

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
- **The daily run stops at its first step.** A Runbook step whose Line is not
  enabled stops the run there, and `next` lists the Line as
  `line_not_enabled`. Enable the Lines (step 5).
- **The daily run did not happen.** A scheduled Trigger fires only while the
  daemon is running, and an instant missed while it was down is not run
  afterwards. Run `cruxible runbook run security.maintain` to catch up.
- **New dependencies have no answer yet.** OSV is asked once a day. After
  writing a new SBOM, run `cruxible runbook run security.start`.

## What's next

<!-- publish: kit tag -->

- The [Security walkthrough](security-walkthrough.md) goes through the same
  steps with their outputs, then judges two exposures, one of them reviewed by
  a second principal.
- [Concepts](concepts.md) explains the ideas behind what you just did.
- The [security kit's README](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/README.md)
  covers everything else it answers, including the audit loop for findings in
  your own code.
- [For AI agents](for-ai-agents.md) covers connecting an agent in depth.
