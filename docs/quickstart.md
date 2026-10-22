# Quickstart

Welcome! In about ten minutes, we'll point Cruxible at one of your projects
and find out which of its dependencies carry a vulnerability that attackers
are exploiting right now: the ones on CISA's Known Exploited Vulnerabilities
(KEV) catalog. Every answer will trace back to the source it came from, and
nothing will be recorded until you've looked at it.

We'll use the **security kit**, a ready-made set of definitions and checks for
exactly this job. Along the way you'll see the core ideas of Cruxible at work:
knowledge kept as typed facts with their evidence, checks that propose changes
rather than make them, and answers you can trust because you can see what they
rest on.

You'll need Python 3.11+, Git, a project to look at, a tool that writes a
CycloneDX SBOM (step 4), and network access to `pypi.org`, `api.osv.dev`,
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
$ cruxible kit add security:1.2.0
security 1.2.0: would_propose (install)
  provider cruxible-provider-web (cruxible-provider-web 0.2.2): would install (by name from the provider index at exactly 0.2.2, refusing any other build; refused where provider_repository is configured)
  provider security-analysis-provider (security-analysis-provider 1.2.0): would install
  provider security-sources-provider (security-sources-provider 1.1.0): would install
...
Preview at a08207130719; nothing was written.
Commit it: rerun the same command with --commit --at a08207130719
~~~

The **providers** are the packages its checks use to fetch and parse sources.
Commit the install at the coordinate the preview printed:

~~~text
$ cruxible kit add security:1.2.0 --commit --at a08207130719
security 1.2.0: accepted (install)
...
~~~

The kit brings definitions only. Its checks, which Cruxible calls **Lines**,
come as an example you adopt deliberately, because what runs on your machine
and when is your decision. Fetch the example for this release, fill in today's
date and an expiry 90 days out, and submit it:

<!-- publish: kit tag -->
~~~bash
curl -O https://raw.githubusercontent.com/cruxible-ai/cruxible-kits/security-1.2.0/kits/security/examples/automation.json
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
components. Look at the proposal before you approve it:

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

Each line is a **Claim**: one fact about one thing, with an ID (`CLM-…`) and
the evidence behind it, here the SBOM as it was read.

## Step 5: Ask OSV, KEV and EPSS

Run the three checks once now instead of waiting for tomorrow, approving what
each proposes. First OSV, for the vulnerabilities your components have:

~~~text
$ cruxible line run security.osv-applicability
RUN-34bb5d6d…: succeeded
Result: {"affected": 39, "changed": 195, "inconclusive": 0, ...}
...
Proposal propose: sha256:95a12abe… candidate sha256:4a281e64…

$ cruxible proposal approve sha256:95a12abe…
~~~

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

**Which package brings that in, and how do we know?** Every answer rests on
evidence you can read. The assessment that this release is affected cites
OSV's answer, and says which package and which fix:

~~~text
$ cruxible query security.assessment --where cve_id=CVE-2025-3248 --select status --claims
...
claim security.assessment/myapp-1.0.0--cve-2025-3248 status: CLM-57485ce1… accepted supported observation

$ cruxible get CLM-57485ce1… --detail evidence
value: affected
capture CAP-5d2caf3528f9  CaptureContract:security.osv-query-v1 v1  source=osv.dev.query  observed=2026-10-22T03:01:41.041250+00:00  evidence: admitted
rationale: OSV lists CVE-2025-3248 as affecting pkg:pypi/langflow@1.2.0 (GHSA-rvqx-wpfh-mfx7, fixed in 1.3.0); pkg:pypi/langflow@1.2.0 (PYSEC-2025-36, fixed in 1.3.0).
~~~

**What do we fix first?** The kit's saved question ranks every affected
release, KEV-listed first, then by EPSS score:

~~~text
$ cruxible query --name security.urgent_exposures
subject                                          ...  cve_id          epss_percentile  epss_score  ...  kev_due_date  kev_listed  product_name  ...
security.assessment/myapp-1.0.0--cve-2025-3248   ...  CVE-2025-3248   0.99987          0.99993     ...  2025-05-26    true        myapp         ...
security.assessment/myapp-1.0.0--cve-2025-34291  ...  CVE-2025-34291  0.99832          0.92808     ...  2026-06-04    true        myapp         ...
...
~~~

(Columns trimmed here.) In this example, upgrading langflow past 1.2.0 clears
the top of the list.

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
RUN-8c616fc7…: operational_failed
Refused: provider_execution_error: Provider execution failed for an operational reason.
...
~~~

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

<!-- update: kit 1.3.0 -->

## What's next

You've installed a kit, adopted its checks, reviewed what they proposed, and
asked questions whose answers carry their evidence. From here:

- `cruxible next` lists what needs attention, including proposals the daily
  checks make. [Keeping knowledge current](keeping-knowledge-current.md)
  explains each kind of row.
- The [security kit's README](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.2.0/kits/security/README.md)
  covers the rest of what it answers: your CI's publish paths, what your
  registries serve, findings in your own code, and recording your own
  judgments about whether a vulnerability really affects you.
- [Concepts](concepts.md) explains the ideas behind what you just did, and the
  guides cover each in depth, starting with [Building team
  knowledge](building-team-knowledge.md).
- [For AI agents](for-ai-agents.md) connects an agent to the same instance.
