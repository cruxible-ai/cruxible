# Security walkthrough

The [Quickstart](quickstart.md) hands the intake to an agent. This walkthrough
does it by hand, one step at a time, so you can see what each step does. We'll
point Cruxible at one of your projects and find out which of its dependencies
carry a vulnerability that attackers are exploiting right now: the ones on
CISA's Known Exploited Vulnerabilities (KEV) catalog. Every answer will trace
back to the source it came from, and nothing will be recorded until you've
looked at it.

We'll use the **security kit**, a ready-made set of definitions and checks for
exactly this job. Along the way you'll see the core ideas of Cruxible at work:
knowledge kept as typed facts with their evidence, checks that propose changes
rather than make them, and answers you can trust because you can see what they
rest on.

You'll need Python 3.11+, Git, a project to look at, a tool that writes a
CycloneDX SBOM (step 4; for JavaScript dependencies that means Node.js with
`npm`), and network access to `pypi.org`, `api.osv.dev`,
`www.cisa.gov` and `epss.empiricalsecurity.com`.

## Step 1: Install and start Cruxible

~~~bash
pip install cruxible        # or: uv tool install cruxible
~~~

Cruxible keeps knowledge in a small background server, the **daemon**. Start
it in its own terminal and leave it running:

~~~bash
cruxible server start --socket ~/.cruxible/run/daemon.sock
~~~

## Step 2: Attach Cruxible to your project

In a second terminal, go to your project's repository and initialize:

~~~bash
cd your-project
export CRUXIBLE_SERVER_SOCKET=~/.cruxible/run/daemon.sock
cruxible init
~~~

`init` creates an **instance**, this project's own body of knowledge, makes
you its owner, and remembers the connection, so commands here need no extra
flags.

## Step 3: Install the security kit

A **kit** is a vocabulary and its checks, published so any instance can
install them. `kit add` shows you what it would install before it does
anything:

<!-- publish: kit tag -->
~~~text
$ cruxible kit add security:1.3.0
security 1.3.0: would_propose (install)
  provider cruxible-provider-web (cruxible-provider-web 0.2.2): would install (by name from the provider index at exactly 0.2.2, refusing any other build; refused where provider_repository is configured)
  provider security-analysis-provider (security-analysis-provider 1.3.0): would install
  provider security-sources-provider (security-sources-provider 1.2.0): would install
...
Preview at 6013a96d7ff4; nothing was written.
Commit it: rerun the same command with --commit --at 6013a96d7ff4
~~~

The **providers** are the packages its checks use to fetch and parse sources.
Commit the install at the coordinate the preview printed:

~~~text
$ cruxible kit add security:1.3.0 --commit --at 6013a96d7ff4
security 1.3.0: accepted (install)
...
~~~

The kit brings definitions only. Its checks, which Cruxible calls **Lines**,
come as an example you adopt deliberately, because what runs on your machine
and when is your decision. `kit pull` fetches the release's files (its
README, examples and agent skills) without installing anything. Copy the
example, fill in today's date and an expiry 90 days out, and submit it:

<!-- publish: kit tag -->
~~~bash
cruxible kit pull security:1.3.0 --out security-kit
cp security-kit/examples/automation.json .
sed -i.bak "s/\"NOW\"/\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"/g; s/\"EXPIRES\"/\"$(date -u -v+90d +%Y-%m-%dT%H:%M:%SZ)\"/g" automation.json
cruxible authoring submit automation.json --brief
~~~

(On Linux, use `date -u -d '+90 days' …` for the expiry.) Then turn on the
checks this walkthrough uses:

~~~bash
for l in kev-daily kev-triage kev-attention epss-daily epss-triage sbom-import osv-applicability cve-applicability; do
  cruxible line enable security.$l
done
~~~

From now on these run on their own every day. Each one only **proposes**:
what it finds waits for your review before it becomes part of what the
project knows.

## Step 4: Describe your dependencies

The kit reads your dependencies from a CycloneDX JSON **SBOM** (software bill
of materials). It asks OSV.dev about every package the SBOM names, so it
covers whatever ecosystems your SBOM generator covers: PyPI, npm, Maven, Go,
crates.io and the rest, each named by its package URL (`pkg:pypi/…`).

Write one into the repository with a generator for your ecosystem:

~~~bash
mkdir -p sboms
uvx --from cyclonedx-bom cyclonedx-py requirements requirements.txt \
    --pyproject pyproject.toml --of JSON -o sboms/app.cdx.json            # a requirements.txt
~~~

For other ecosystems, use a general-purpose generator:

<!-- maintainer: check the syft and uv lines (not run here) -->
~~~bash
syft dir:. -o cyclonedx-json > sboms/app.cdx.json                         # any ecosystem
uv export --format cyclonedx1.5 > sboms/app.cdx.json                      # a uv project
~~~

[cdxgen](https://github.com/CycloneDX/cdxgen) is another good option. The
SBOM's top-level component (`metadata.component`) names your project and its
version; the kit records it as a **release**.

Now read the SBOM in:

~~~bash
echo '{"relative_path": "sboms/app.cdx.json"}' > sbom.json
cruxible procedure run security.sbom-acquire sbom.json
~~~

Reading it starts the import check, which proposes your release and its
components. The read returns as soon as the SBOM is captured, before the
import has proposed anything, so if `proposal list` is still empty, give it a
moment and list again. Look at the proposal before you approve it:

~~~text
$ cruxible proposal list --status open
STATUS  TERMINAL_REASON  PROPOSAL_ID  ...
open  -  sha256:6165c5fa…  ...

$ cruxible proposal review sha256:6165c5fa…
...
Changes (9):
  set security.release/myapp--1.0.0 component: pkg:pypi/requests@2.31.0  [CLM-1bed72b6…]
  set security.release/myapp--1.0.0 component: pkg:pypi/langflow@1.2.0  [CLM-1e7b932e…]
  ...
  set security.release/myapp--1.0.0 component: pkg:pypi/jinja2@3.1.2  [CLM-e9f27041…]
  create subject subjects/security.product/myapp.json
  create subject subjects/security.release/myapp--1.0.0.json

$ cruxible proposal approve sha256:6165c5fa…
...
Activated at c97194dc4e44…
~~~

`proposal approve` shows what you're signing and asks before it signs;
`--yes` skips the question, and an agent or a script, with no terminal to
answer it, must pass it.

Each line is a **Claim**: one fact about one thing, with an ID (`CLM-…`) and
the evidence behind it, here the SBOM as it was read.

## Step 5: Ask OSV, KEV and EPSS

Run the three checks once now instead of waiting for tomorrow, approving what
each proposes. First OSV, for the vulnerabilities your components have:

~~~text
$ cruxible line run security.osv-applicability
RUN-34bb5d6d…: succeeded
Result: {"affected": 39, "changed": 234, "inconclusive": 0, ...}
...
Proposal propose: sha256:95a12abe… candidate sha256:4a281e64…

$ cruxible proposal approve sha256:95a12abe…
~~~

OSV runs once a day on its own; run it again like this whenever you read in a
new SBOM or host inventory, so the new releases are assessed now rather than
tomorrow.

Then KEV, for which of those are being exploited, and EPSS, for how likely
exploitation is. Each fetch lands its catalog, and a triage check proposes the
facts for your vulnerabilities:

~~~bash
cruxible line run security.kev-daily
cruxible proposal list --status open        # the KEV triage proposal
cruxible proposal approve PROPOSAL_ID
cruxible line run security.epss-daily
cruxible proposal list --status open        # the EPSS triage proposal
cruxible proposal approve PROPOSAL_ID
~~~

## Step 6: Ask your questions

**Which of my dependencies' vulnerabilities are in KEV?**

~~~text
$ cruxible query security.vulnerability --where kev_listed=true --select kev_date_added,kev_due_date,epss_score
subject                                kev_date_added  kev_due_date  epss_score  flags
security.vulnerability/cve-2025-3248   2025-05-05      2025-05-26    0.99993     -
security.vulnerability/cve-2025-34291  2026-05-21      2026-06-04    0.92808     -
security.vulnerability/cve-2026-0770   2026-07-21      2026-07-24    0.62562     -
security.vulnerability/cve-2026-33017  2026-03-25      2026-04-08    0.24755     -
security.vulnerability/cve-2026-55255  2026-07-07      2026-07-10    0.00887     -
~~~

**Which dependency brings each one in?** Each assessment names the component
OSV found the vulnerability in, so one query answers it:

~~~text
$ cruxible query security.assessment --follow vulnerability:v --where v.kev_listed=true --select cve_id,component,v.kev_due_date
subject                                          cve_id          component                v.kev_due_date  flags
security.assessment/myapp-1.0.0--cve-2025-3248   CVE-2025-3248   pkg:pypi/langflow@1.2.0  2025-05-26      -
security.assessment/myapp-1.0.0--cve-2025-34291  CVE-2025-34291  pkg:pypi/langflow@1.2.0  2026-06-04      -
security.assessment/myapp-1.0.0--cve-2026-0770   CVE-2026-0770   pkg:pypi/langflow@1.2.0  2026-07-24      -
security.assessment/myapp-1.0.0--cve-2026-33017  CVE-2026-33017  pkg:pypi/langflow@1.2.0  2026-04-08      -
security.assessment/myapp-1.0.0--cve-2026-55255  CVE-2026-55255  pkg:pypi/langflow@1.2.0  2026-07-10      -
~~~

And every answer rests on evidence you can read. The assessment cites OSV's
answer, which says which fix to take:

~~~text
$ cruxible get CLM-45ec491a… --detail evidence      # the assessment's status Claim
value: affected
capture CAP-3e551f0536d5  CaptureContract:security.osv-query-v1 v1  source=osv.dev.query  ...  evidence: admitted
rationale: OSV lists CVE-2025-3248 as affecting pkg:pypi/langflow@1.2.0 (GHSA-rvqx-wpfh-mfx7, fixed in 1.3.0); pkg:pypi/langflow@1.2.0 (PYSEC-2025-36, fixed in 1.3.0).
~~~

(`cruxible query security.assessment --where cve_id=CVE-2025-3248 --select
status --claims` gives the status Claim's ID.)

**What do we fix first?** The kit's saved question ranks every affected
release, KEV-listed first, then by EPSS score:

~~~text
$ cruxible query --name security.urgent_exposures
subject                                          ...  component                ...  cve_id          ...  epss_score  ...  kev_due_date  kev_listed  ...
security.assessment/myapp-1.0.0--cve-2025-3248   ...  pkg:pypi/langflow@1.2.0  ...  CVE-2025-3248   ...  0.99993     ...  2025-05-26    true        ...
security.assessment/myapp-1.0.0--cve-2025-34291  ...  pkg:pypi/langflow@1.2.0  ...  CVE-2025-34291  ...  0.92808     ...  2026-06-04    true        ...
...
~~~

(Columns trimmed here.) In this example, upgrading langflow past 1.2.0 clears
the top of the list.

## Step 7: When it's an incident

When a vulnerability is being exploited and you run the affected software,
the kit tracks the response: where it's running, whether each deployment is
patched, whether you were attacked, and which keys to rotate. Here are the
first two, for the langflow CVE.

Declare an incident for it, with a window starting before the earliest known
exploitation. Save this as `incident.yaml`:

~~~yaml
because: Langflow 1.2.0 is exploited in the wild (CVE-2025-3248); one response for it.
subject: security.incident/Langflow RCE
changes:
  - {op: set, field: window_start, value: "2025-04-01T00:00:00Z"}
  - {op: set, field: rotation_policy, value: on_found_or_unknown}
  - {op: set, field: status, value: open}
  - {op: add, field: vulnerability, value: security.vulnerability/cve-2025-3248}
~~~

~~~bash
cruxible write incident.yaml
~~~

Its **respond** Line asks the incident's questions and posts what it finds to
the inbox. Save this as `respond.json`, then add the Line and turn it on:

~~~json
{"kind": "line", "name": "security.respond.langflow-rce", "procedure_name": "security.respond",
 "max_authority": "observe",
 "parameters": {"incident": "Langflow RCE", "inventory_pull": "security.inventory-acquire",
                "log_pull": "security.log-acquire", "credential_pull": "security.credential-acquire",
                "record_pull": "security.cve-record-acquire"}}
~~~

~~~bash
cruxible authoring submit respond.json --brief
cruxible line enable security.respond.langflow-rce
~~~

Exposure is about where the software runs, so tell the kit with a scan of
your deployments, saved as `scans/prod.json` (here in the kit's own format; a
Syft or Trivy SBOM of each host, tagged with its scope and hostname, works
too):

~~~json
{"scope": "prod", "looked_for": ["myapp"], "coverage": "full",
 "assets": [{"hostname": "app-1.example.com", "runs": ["pkg:generic/myapp@1.0.0"]}]}
~~~

~~~bash
cruxible line enable security.inventory-import.prod
echo '{"relative_path": "scans/prod.json"}' > scan.json
cruxible procedure run security.inventory-acquire scan.json
cruxible proposal list --status open        # the inventory import
cruxible proposal approve PROPOSAL_ID
~~~

Now ask:

~~~text
$ cruxible line run security.respond.langflow-rce
RUN-1c53f06b…: succeeded
Result: {"answers": [{"question": "RQ1", "state": "found", "subject": "app-1.example.com: myapp 1.0.0", "why": "runs an affected release"}, {"question": "RQ2", "state": "found", "subject": "app-1.example.com: myapp 1.0.0", "why": "no patch"}], ...}
...

$ cruxible inbox list --line security.respond.langflow-rce
STATUS  ITEM  LINE  SEEN  LAST_POSTED
unread  INB-b2eca16850c9  Line:security.respond.langflow-rce  -  …
  {"asset": "app-1.example.com", "cves": ["CVE-2025-3248"], "incident": "Langflow RCE", "internet_exposed": false, "kind": "exposed", "question": "RQ1", "release": "myapp 1.0.0"}
unread  INB-75f2384765c1  Line:security.respond.langflow-rce  -  …
  {"asset": "app-1.example.com", "cves": ["CVE-2025-3248"], "incident": "Langflow RCE", "internet_exposed": false, "kind": "patch_missing", "question": "RQ2", "release": "myapp 1.0.0"}
~~~

`app-1.example.com` runs the affected release, and nothing patches it yet.
From here the kit carries the rest: raising a patch that someone else
approves, closing it only when a fresh scan shows the host moved, auditing
your logs for the incident's window, and rotating the credentials the service
holds. Each step is proved by evidence, never by a self-report. The kit's
<!-- publish: kit tag -->
[Respond to an incident](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/README.md#respond-to-an-incident)
walks through all of it.

## When a fetch fails: unknown is not "none"

Every answer is one of three things, and it matters which:

- **Found**: a source was read and lists the problem.
- **None**: a source was read and doesn't.
- **Unknown**: nobody has read it yet, or the read failed.

A failed fetch never becomes "not affected". When a check can't reach its
source (the network is down, or a host blocks it), the run fails and proposes
nothing. Here, fetching a CVE record from a host that doesn't resolve:

~~~text
$ cruxible procedure run security.cve-record-acquire cve.json
RUN-fb30fa54…: operational_failed
Refused: provider_execution_error: Provider execution failed for an operational reason.
Repair: hand edit refusal/provider_execution_error: Read the failure cause. A transport error (connect, DNS, timeout): rerun once the target is reachable. An HTTP status: revise the URL input, then rerun.
Cause: record: ConnectError: could not resolve 'cve-records.invalid': ...
Next: terminal
...
~~~

The run exits 1, so a script's `&&` stops there, and `Cause` says what the
provider hit.

A daily check that can't reach OSV, KEV or EPSS fails the same way. Nothing
about your components changes, so whatever that run would have answered is
still unknown. Check that the machine can reach the hosts listed
at the top of this page, then run the check again. When OSV answers for most
packages but fails for some, the release's assessment for those reads
`inconclusive` rather than missing. Before you read an empty answer as
"none", check the last run of the check behind it:

~~~bash
cruxible get Line:security.osv-applicability       # recent runs and their status
~~~

## What's next

You've installed a kit, adopted its checks, reviewed what they proposed, and
asked questions whose answers carry their evidence. From here:

<!-- publish: kit tag -->

- `cruxible next` lists what needs attention, including proposals the daily
  checks make. [Keeping knowledge current](keeping-knowledge-current.md)
  explains each kind of row.
- The [security kit's README](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/README.md)
  covers the rest of what it answers: your CI's publish paths, what your
  registries serve, findings in your own code, and recording your own
  judgments about whether a vulnerability really affects you.
- [Concepts](concepts.md) explains the ideas behind what you just did, and the
  guides cover each in depth, starting with [Building team
  knowledge](building-team-knowledge.md).
- [For AI agents](for-ai-agents.md) connects an agent to the same instance,
  and the kit's [security intake
  skill](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/skills/security-intake/SKILL.md)
  is these steps written for one, plus your hosts, services, credentials and
  logs. Install it with its partner, the security analyst skill, from the
  pulled kit: `cp -R security-kit/skills/security-intake
  security-kit/skills/security-analyst .claude/skills/`.
