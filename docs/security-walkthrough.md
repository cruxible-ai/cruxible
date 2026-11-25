# Security walkthrough

The [Quickstart](quickstart.md) gets to the first answer in a few commands.
This walkthrough takes the same steps one at a time, with their outputs, so you
can see what each step does. We'll
point Cruxible at one of your projects, find which of its dependencies carry a
known vulnerability, rank them by whether attackers exploit them (CISA's Known
Exploited Vulnerabilities catalog, KEV) and how likely they are to (FIRST's
EPSS score), and then record judgments about three of them, each approved by
a second principal. Every answer traces back to the source it came from.

We'll use the **security kit**, a ready-made set of definitions for exactly
this job. Along the way you'll see the core ideas of Cruxible at work:
knowledge kept as typed facts with their evidence, automation whose authority
you grant explicitly, and judgments that wait for review.

The outputs below come from running these steps on
[pygoat](https://github.com/adeyosemanputra/pygoat), a deliberately
vulnerable Django application, with Cruxible 0.5 and security kit 1.3.0.

You'll need Python 3.11+, Git, a project to look at, a tool that writes a
CycloneDX SBOM (step 3), and network access to `pypi.org`, `api.osv.dev`,
`www.cisa.gov` and `api.first.org`.

## Step 1: Install Cruxible and attach it to your project

~~~bash
pip install cruxible        # or: uv tool install cruxible
~~~

Cruxible keeps knowledge in a small background server, the **daemon**. In
your project's repository, `init` starts one if none is running, creates an
**instance** (this project's own body of knowledge), makes you its owner and
remembers the connection, so later commands here need no extra flags. The two
reviewer options set up **cosign**: a second principal, `reviewer`, whose key
is written to a directory outside the repository, and an approval policy
under which nothing activates on its author's approval alone:

~~~text
$ cd your-project
$ cruxible init --principal-id owner --reviewer-key-dir ../reviewer-keys --require-independent-approval
Started a Cruxible daemon on unix socket .../state/run/daemon.sock (...); `cruxible server stop` stops it.
...
Cruxible initialized: inst_e4c09942a5fd412e at 2ccaf29d197e
Owner: owner, private key .../keys/inst_e4c09942a5fd412e/owner/owner.ed25519
...
Approval policy: independent_approval_required
Base kit: base 1.0.0 awaits approval, proposal sha256:0b5b6186… (cruxible proposal approve sha256:0b5b6186… --signer-id reviewer, as a second principal)
Reviewer: reviewer, private key .../reviewer-keys/reviewer.ed25519 (cruxible context use --principal reviewer)
More: cruxible context show
~~~

The **base kit** holds what every instance needs to read workspace files and
fetch web pages. Under `independent_approval_required` even it waits: `init`
proposed it as the owner, so the reviewer approves it.
`CRUXIBLE_PRINCIPAL_ID=reviewer` makes one command act as the reviewer, with
the key and settings `init` wrote to `../reviewer-keys`, and the reviewer's
`next` lists what waits for it:

~~~text
$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible next
...
repair  proposal_awaiting_approval  sha256:0b5b6186ce2dec43be515afb2a2af15a83b887f6260c515118f4b77b5cd798fa  next=cruxible.proposal.approve
  repair: cruxible proposal approve sha256:0b5b6186ce2dec43be515afb2a2af15a83b887f6260c515118f4b77b5cd798fa --signer-id reviewer

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:0b5b6186… --yes
Signing sha256:760331c45c0d908c5b68c755c0d9bfdb849f99e9d95eb6b4eca561fc1ab9b4c3 (sha256:0b5b6186ce2dec43be515afb2a2af15a83b887f6260c515118f4b77b5cd798fa): 29 changes (29 create)
Signing key for reviewer: .../reviewer-keys/reviewer.ed25519 (from remembered settings .../reviewer-keys/cruxible.env)
Approved sha256:760331c45c0d908c5b68c755c0d9bfdb849f99e9d95eb6b4eca561fc1ab9b4c3 (sha256:0b5b6186ce2dec43be515afb2a2af15a83b887f6260c515118f4b77b5cd798fa) as reviewer
Activated at b0a3ac708d13e024573ded3abe361450abd7374a
~~~

`proposal approve` shows what you're signing and asks before it signs;
`--yes` skips the question, and an agent or a script, with no terminal to
answer it, must pass it. The repair `next` prints (`--signer-id reviewer`)
does the same from the owner's session. A person reviewing at their own
terminal can switch instead with `cruxible context use --principal reviewer`,
which holds until `cruxible context use --principal owner`.

The reviewer is normally a second agent: it reads each proposal (`cruxible
proposal review`) and approves or rejects it, so no agent lands its own work.
That is a convention between agents, not a security boundary: both keys are
files your OS user can read, and nothing stops one process from using both.

**One owner, no reviewer.** `cruxible init --principal-id owner` alone leaves
the policy `self_approval_allowed`: `init` prints `Base kit: base 1.0.0
installed`, and your own `proposal approve` activates what you propose. The
steps below then need no reviewer command.
[Review and governance](review-and-governance.md) covers both setups and how
to add a reviewer to an instance that started without one.

## Step 2: Install the security kit

A **kit** is a vocabulary and the definitions around it, published so any
instance can install them. `kit add` shows you what it would install before it
does anything:

~~~text
$ cruxible kit add security:1.3.0
security 1.3.0: would_propose (install)
  provider cruxible-provider-web (cruxible-provider-web 0.7.0): would install
  provider security-osv-provider (security-osv-provider 0.1.0.dev0): would install
  provider security-sarif-provider (security-sarif-provider 0.1.0.dev0): would install
  requires base >= 1.0.0: met (installed 1.0.0)
...
Capability:
  add: Capability:security.osv.match/v1
  add: Capability:security.osv.query/v1
  add: Capability:security.sarif.findings/v1
  add: Capability:security.sbom.components/v1
...
a commit installs the bundled providers first, then proposes these definitions, which are checked against the installed providers
...
Preview at b0a3ac708d13; nothing was written.
Commit it: rerun the same command with --commit --at b0a3ac708d13
~~~

You can also install from a local checkout of the
[kits repository](https://github.com/cruxible-ai/cruxible-kits) by passing its
path (`../cruxible-kits/kits/security`) instead of `security:1.3.0`. The
**providers** are the packages that fetch and parse sources: OSV for
vulnerabilities, a web provider for KEV and EPSS, and a SARIF reader for
scanner output. Commit the install at the coordinate the preview printed. A kit
that carries providers is several proposals, not one: each provider install is
its own, and the kit's definitions are proposed only once the providers are
installed, because they are checked against them:

~~~text
$ cruxible kit add security:1.3.0 --commit --at b0a3ac708d13
security 1.3.0: awaiting_providers (install)
  provider cruxible-provider-web (cruxible-provider-web 0.7.0): awaiting approval, proposal sha256:e4d45ca1ff3348365e599fc2d8a953e9078b4fde62163de5028f0f1c3a3c5b28
  provider security-osv-provider (security-osv-provider 0.1.0.dev0): awaiting approval, proposal sha256:c7ba6329cb58c481b5c6ce3d8293139db2f28d1f4cf7eb85753f51c5419d9a5b
  provider security-sarif-provider (security-sarif-provider 0.1.0.dev0): awaiting approval, proposal sha256:d351f95412ed026b4b2db585343c26b00dfb052b7e1a130dbf9e76924446c601
  requires base >= 1.0.0: met (installed 1.0.0)
...
the kit's definitions are proposed once its bundled providers are installed: ...; the definitions are proposed automatically, as you, once these installs activate (kit status shows the pending resume)
~~~

The owner cannot approve them; the reviewer approves the three installs, one
at a time:

~~~text
$ cruxible proposal approve sha256:d351f954… --yes
Error: cruxible.approval.creator_forbidden: independent_approval_required mode needs an active ordinary approver other than the candidate creator, whose approval activates it

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:d351f954… --yes
...
Approved sha256:3af9c2366540ced32b4b5813c63eb0b0a12323f19d0d9fa7100f0f2fbbfe5090 (sha256:d351f95412ed026b4b2db585343c26b00dfb052b7e1a130dbf9e76924446c601) as reviewer
Activated at fc52d509fed86acbf912328664ee8d226c7e0504

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:c7ba6329… --yes
...
Landed at the head as sha256:28b994ba45ffb3f4e1a1b3768a24ceba7e41fdb0d79718063a6186c194b22386: the same change set (proposed as sha256:c7ba6329cb58c481b5c6ce3d8293139db2f28d1f4cf7eb85753f51c5419d9a5b); signing it
Approved sha256:2d4f2b5f796c6c1633148212514616509291cc4982bcb883f5590ad2298e4641 (sha256:28b994ba45ffb3f4e1a1b3768a24ceba7e41fdb0d79718063a6186c194b22386) as reviewer
Activated at f706d5490b0d7be2cf0af75b4380916c3e363b5c
~~~

The second and third were proposed before the first landed, so each approval
first lands the same change set at the new head and says so, then signs it.
After the third, the definitions are proposed, and `kit status` names the
proposal:

~~~text
$ cruxible kit status --offline
security 1.3.0: kit add proposed (as owner); proposal sha256:11b0a727f61032db1a428c4233021df5aaab6a138cd85f7f7eae80d20b55b0f1
...

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review sha256:11b0a727…
Proposal: sha256:11b0a727f61032db1a428c4233021df5aaab6a138cd85f7f7eae80d20b55b0f1
Candidate: sha256:2490b0425ef5acaf200338a1fe0401d4b8a06dacfb20e840a3ecb26539d8f475
Why: Install kit security 1.3.0.
...
Changes: 183 created / 0 revised / 0 retired
...

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:11b0a727… --yes
...
Activated at 353785f2e945c9057e1b6c8697ed1348ee124097

$ cruxible kit status
base 1.0.0 content sha256:07e64748e385c0dc51a69cb154b8c3fb3d57906ab59aba9d1dac93bbf3b60845; up to date
  source: bundled
security 1.3.0 content sha256:7273e50106d70fac67e04dec3a603d42e067cde167385b1503e0d78ad947c729; update check local source
...
  provider cruxible-provider-web (cruxible-provider-web 0.7.0): installed
  provider security-osv-provider (security-osv-provider 0.1.0.dev0): installed
  provider security-sarif-provider (security-sarif-provider 0.1.0.dev0): installed
  requires base >= 1.0.0: met (installed 1.0.0)
~~~

A kit proposal is planned against the instance as it was when `kit add` ran.
If something else lands before the reviewer approves the definitions, commit
the kit again at the new coordinate and have the reviewer approve the new
proposal. (With one owner and `self_approval_allowed`, the commit installs the
providers and the definitions in one step and prints `security 1.3.0:
accepted (install)`.)

`cruxible orient` now lists the kit's ten kinds (inventory, component,
occurrence, vulnerability, the two judgment pairs, findings, coverage units,
code facts and catalogs) and its 30 saved questions; nothing is in them yet.

## Step 3: Describe your dependencies

The kit reads your dependencies from a CycloneDX JSON **SBOM** (software bill
of materials). It asks OSV.dev about every package the SBOM names, so it
covers whatever ecosystems your SBOM generator covers, each package named by
its package URL (`pkg:pypi/…`). Write one into the repository with a generator
for your ecosystem; for a `requirements.txt`:

~~~bash
uvx --from cyclonedx-bom cyclonedx-py requirements requirements.txt --of JSON -o sbom.cdx.json
~~~

For other ecosystems, use a general-purpose generator:

<!-- maintainer: check the syft and uv lines (not run here) -->
~~~bash
syft dir:. -o cyclonedx-json > sbom.cdx.json                         # any ecosystem
uv export --format cyclonedx1.5 > sbom.cdx.json                      # a uv project
~~~

The SBOM's root component (`metadata.component.purl`) names the
**inventory**. `cyclonedx-py requirements` writes none, so here the inventory
is named after the file, `sbom.cdx.json`. An SBOM covers what its generator
read: this one lists pygoat's root `requirements.txt`, not the lab containers
that carry their own requirements files, so their packages are on no worklist
unless you import an SBOM for each.

## Step 4: Adopt the automation

The kit brings definitions only. The checks that run on their own, which
Cruxible calls **Lines**, come as a template you adopt deliberately, because
what runs on your machine, when, and with how much authority is your
decision. Copy the kit's `automation.json` into your repository and tailor
it: the SBOM path (`relative_path`, `sbom.cdx.json` as shipped), each
**Mandate**'s `valid_from` and `expires_at`, and the daily cron time. Then
submit it for review:

~~~text
$ cruxible authoring submit automation.json --no-accept --brief
outcome: submitted (awaiting_external_approval)
intent: AIT-14347b81a027ed75c81905f5dc2bb096
proposal: sha256:6e3265c187748b641d31266b27a4187c943340476b8b3f99858477049545a7e7
reason: not activated: the candidate needs an external approval from one of: reviewer. That signer's approval activates it.
next: cruxible proposal approve sha256:6e3265c187748b641d31266b27a4187c943340476b8b3f99858477049545a7e7 --signer-id reviewer
~~~

The one decision here is what may land without asking anyone. `proposal
review` shows each Line (what it runs, bound to which provider build, with
which parameters and budget) and each Mandate (what it lets a Line settle):

~~~text
$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review sha256:6e3265c1…
...
Changes: 19 created / 0 revised / 0 retired
  line: 7 created / 0 revised / 0 retired
  procedure-mandate: 8 created / 0 revised / 0 retired
  runbook: 2 created / 0 revised / 0 retired
  trigger: 2 created / 0 revised / 0 retired
Definitions (19):
...
  Line security.osv-query: created
    procedure    security.osv-query
    authority    settle
    binding      match -> security-osv-provider/5c37c973e439#osv.match
    binding      query -> security-osv-provider/5c37c973e439#osv.query
    parameter    logical_source = osv.dev.query
    acquisition  osv.responses: https://api.osv.dev
    budget       max_capture_bytes=67108864  max_items=50000  max_provider_calls=2  max_wall_clock_microseconds=300000000
    inbox        extract_refused, duplicate_key, incomplete_source, held_disagrees, slot_contested, claim_not_in_source
...
  ProcedureMandate security.osv-query-settle: created
    line        security.osv-query
    grants      settle
    valid       2026-11-06T00:00:00Z .. 2027-02-05T00:00:00Z
    namespace   claims
    may create  security.vulnerability
    ClaimType                       create     revise     retire
    security.component.affected_by  extracted  extracted  absence
...
  Trigger security.maintain-daily: created
    schedule  on cron 0 14 * * * (UTC)
    target    Runbook:security.maintain
  Trigger security.osv-matches-landed: created
    schedule  when CaptureContract:security.osv-matches from osv.dev.query lands
    target    Line:security.osv-unkeyed
Stored fields grouped by kind: --json. Every member's artifact and delta: --detail
~~~

The four source Lines (SBOM import, OSV, KEV, EPSS) may settle what they copy
from their sources, within the ClaimTypes and bases each Mandate lists. The
other three (the two attention Lines and `security.osv-unkeyed`) only post to
the inbox. No Mandate settles a judgment. The reviewer approves it:

~~~text
$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:6e3265c1… --yes
Signing sha256:5856e7cab08271b4134b2cf4c19b01eb912734961db737350fc17dd44351556a (sha256:6e3265c187748b641d31266b27a4187c943340476b8b3f99858477049545a7e7): 19 changes (19 create)
Signing key for reviewer: ...
Approved sha256:5856e7cab08271b4134b2cf4c19b01eb912734961db737350fc17dd44351556a (sha256:6e3265c187748b641d31266b27a4187c943340476b8b3f99858477049545a7e7) as reviewer
Activated at c034a7537dcef23a83baa2ff4b059643928bd164
~~~

## Step 5: Run it once

The template's `security.start` **Runbook** runs six Lines in order: SBOM
import, OSV, KEV, EPSS, then the two attention Lines. `runbook preview` shows
what each step reads, writes, calls and needs, and runs nothing:

~~~text
$ cruxible runbook preview security.start
Runbook security.start (preview: definitions, and whether each workspace file is there; nothing ran)
   1  Line:security.sbom-import.app  Procedure:security.sbom-import
      writes security.occurrence: security.occurrence.scope
      calls  read -> Provider:cruxible-builtin workspace.file  (capability workspace.file@1)
      calls  derive -> Provider:security-osv-provider/5c37c973e439 sbom.components  (capability security.sbom.components@1)
      needs  file sbom.cdx.json (json, at most 1 MiB per its CaptureContract, parameter relative_path): present
...
   3  Line:security.kev-triage  Procedure:security.kev-triage
      reads  query security.watched_vulnerabilities: security.component.affected_by, security.occurrence.component, security.occurrence.scope, security.vulnerability.cve_id
      writes security.vulnerability: security.vulnerability.kev_date_added, security.vulnerability.kev_due_date, security.vulnerability.kev_ransomware_use, security.vulnerability.listed_in
      calls  fetch -> Provider:cruxible-provider-web/f3543a2990f6 web.fetch  (capability web.fetch@1)
...
~~~

A Line starts disabled. Enable the seven before the first run:

~~~bash
cruxible line enable security.osv-unkeyed --runbook security.start
~~~

`--runbook security.start` enables the six Lines its steps name, after the
Lines named first. The seventh, `security.osv-unkeyed`, is not a Runbook step: its own Trigger
runs it whenever the OSV step lands a result, and only while it is enabled,
so enable it first or the first result posts nothing. Then run the Runbook.
The calls are real: OSV, CISA and FIRST answer over the network, and the run
took about half a minute here:

~~~text
$ cruxible runbook run security.start
Runbook security.start: completed; needs attention: step 5  [RBR-41ae12e5ba38447789431eb8b2712fc5]
   1  succeeded   Line:security.sbom-import.app  (RUN-fa589b00f641944727ec9202e9f0eec8e6028fab054db1e000868c6bb7e5c4ec)
      written 32 (settled 32, proposed 0); unchanged 0; refused 0; inbox 0
   2  succeeded   Line:security.osv-query  (RUN-8c2fa0e801008886d59a2c38beaa05fa8537ff9067b3a349d7ac19e7ec64023d)
      written 181 (settled 181, proposed 0); unchanged 0; refused 0; inbox 0
   3  succeeded   Line:security.kev-triage  (RUN-720a3fed3b76c35b6f1fbff8dcc6ea6983dee6a4371ba8224a4426463678657b)
      written 181 (settled 181, proposed 0); unchanged 0; refused 0; inbox 0
   4  succeeded   Line:security.epss-triage  (RUN-396be8aff3e25ba519e3a7d15a0c0d3ec661beb3ce2bc8a5edf7b2059947c722)
      written 543 (settled 543, proposed 0); unchanged 0; refused 0; inbox 0
   5  succeeded   Line:security.exposure-attention.unjudged  (RUN-c1e52648a728c60e0beed99bcca7274eab500157750d76ea5e3e10eec3c96e8b)
    ! written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 7 new
   6  succeeded   Line:security.exposure-attention.contested  (RUN-53623eaf06928fcbb939c5d1ac8062b66312d694a60cbaa7662d4d174122efc9)
      written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 0
~~~

32 components, 181 vulnerabilities OSV lists for them, KEV's answer for each
(none of these is on it) and its EPSS score, all settled under the Mandates
the reviewer approved. Step 5 posted seven exposures to the inbox.

With the Lines enabled, the facts also stay current: the
`security.maintain-daily` Trigger runs OSV, KEV, EPSS and the attention Lines
once a day, each under its own Line's enablement:

~~~text
$ cruxible orient --section lines
...
LINE  LIFECYCLE  PROCEDURE  AUTHORITY  TRIGGER  ENABLEMENT  DUE  WAITING
Line:security.epss-triage  live  Procedure:security.epss-triage  settle  runbook Trigger:security.maintain-daily  running  0  0
...
Line:security.osv-unkeyed  live  Procedure:security.osv-unkeyed  observe  capture_landing  running  0  0
Line:security.sbom-import.app  live  Procedure:security.sbom-import  settle  manual  running  0  0

$ cruxible get Trigger:security.maintain-daily
trigger: Trigger:security.maintain-daily
lifecycle: live
sched: cron 0 14 * * *
next_fire: 2026-11-18T14:00:00Z
target: Runbook:security.maintain
next: cruxible get Trigger:security.maintain-daily --detail history
~~~

The SBOM import is not on the clock: rerun `cruxible runbook run
security.start` when the SBOM changes. (It is enabled anyway so `next` stops
listing it as `line_not_enabled`.) The Trigger fires only while the daemon is
running: a daily instant missed while it was down is not run afterwards, and
`next` then shows a `trigger_instants_missed` row for the Trigger, so run
`cruxible runbook run security.maintain` yourself after downtime.

## Step 6: Ask your questions

**What do we look at first?** The kit's saved question lists every exposure
nobody has judged, KEV-listed first, then by EPSS score:

~~~text
$ cruxible query --name security.urgent_exposures --limit 5
subject                                component            cve_id          epss_percentile  epss_score  inventory      kev_date_added  kev_due_date  kev_ransomware_use  occurrence                          scope     flags
security.vulnerability/cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  0.99008          0.53447     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-39614  pkg:pypi/django@4.2  CVE-2024-39614  0.98169          0.28637     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-57833  pkg:pypi/django@4.2  CVE-2025-57833  0.98149          0.28271     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-45230  pkg:pypi/django@4.2  CVE-2024-45230  0.98002          0.25773     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-64459  pkg:pypi/django@4.2  CVE-2025-64459  0.97385          0.19257     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
truncated: 176 more rows; next: cruxible query --name security.urgent_exposures --limit 5 --cursor q2.bb4afd8c9e8eeb58.hhhhmjadqt6.d3952d60b31f.f908c9a55a99.5
~~~

`security.kev_triage_backlog` is the same list limited to KEV-listed
vulnerabilities (empty for pygoat), and `cruxible orient --section queries`
lists the other saved questions with their parameters.

**What does each answer rest on?** A vulnerability's card shows its values,
and each Claim's evidence shows the source as it was read. The KEV Claim
records that the catalog was read completely and does not list this CVE; the
OSV Claim cites OSV's answer:

~~~text
$ cruxible get security.vulnerability/cve-2025-57833
security.vulnerability/cve-2025-57833  (live, 1 incoming)
  cve_id           CVE-2025-57833
  epss_percentile  0.98149
  epss_score       0.28271
  epss_score_date  2026-11-17
  listed_in        (no value)
                   none of security.catalog/cisa-kev  CLM-71b0e514276e63767512c26c0b3ee41b [supported]
...

$ cruxible get CLM-71b0e514276e63767512c26c0b3ee41b --detail evidence
value: {"none": "security.catalog/cisa-kev"}
capture CAP-1ca83f90829b  CaptureContract:security.kev-catalog v1  source=cisa.kev.catalog  observed=2026-11-17T22:53:58.980422+00:00  evidence: admitted
rationale: Complete source establishes the declared inverse.

$ cruxible get security.vulnerability/cve-2025-57833 --detail incoming
security.component/pkg-pypi-django-4.2  security.component.affected_by  CLM-2df9a2ea9ddbbc40debc4b02eefb51ba

$ cruxible get CLM-2df9a2ea9ddbbc40debc4b02eefb51ba --detail evidence
value: security.vulnerability/cve-2025-57833
capture CAP-895d061ac923  CaptureContract:security.osv-matches v1  source=osv.dev.query  observed=2026-11-17T22:53:44.183703+00:00  evidence: admitted
rationale: OSV GHSA-6w2r-r2m5-xq5w lists CVE-2025-57833 for pkg:pypi/django@4.2.
~~~

**What arrived for me?** `next` lists what needs attention, here the two
inboxes the Lines posted to:

~~~text
$ cruxible next
repair  line_inbox_unread  Line:security.exposure-attention.unjudged  next=cruxible.inbox.list
  repair: cruxible inbox list --line security.exposure-attention.unjudged --status unread
repair  line_inbox_unread  Line:security.osv-unkeyed  next=cruxible.inbox.list
  repair: cruxible inbox list --line security.osv-unkeyed --status unread

$ cruxible inbox list
STATUS  ITEM  LINE  KIND  SEEN  TITLE
unread  INB-f414715538fc  Line:security.exposure-attention.unjudged  post  1  component=pkg:pypi/django@4.2 cve_id=CVE-2024-39614 inventory=sbom.cdx.json
...
unread  INB-47fe039489bc  Line:security.exposure-attention.unjudged  post  1  component=pkg:pypi/django@4.2 cve_id=CVE-2023-46695 inventory=sbom.cdx.json
...
unread  INB-0573b8a3bf53  Line:security.osv-unkeyed  post  1  count=10 matches=sha256:895d061ac923a62732f1d03757e194c42f1fa9562c6061b1cd7b826…
~~~

`cruxible inbox get ITEM` prints one item whole. The inbox is never accepted
state: it is where a Line hands you something it cannot decide. An item is a
call for attention, not storage: the attention Line posts one item per pair
because each pair needs its own decision, while `security.osv-unkeyed` posts
one summary item that points at the list it is about (the last section reads
it).

## Step 7: Judge an exposure

A worklist row says OSV lists this vulnerability for this exact release and
this inventory holds it. That is a lead, not a finding: whether the
vulnerable code can be reached here is a judgment, and the kit's governed
guidance says how to make one:

~~~text
$ cruxible get Document:security.guidance.dependency
document: security.guidance.dependency
title: Judging a dependency exposure
...
links:
  guides  ClaimType:security.app_exposure.justification
  guides  ClaimType:security.app_exposure.rests_on
  guides  ClaimType:security.app_exposure.verdict
  guides  ClaimType:security.exposure.justification
next: cruxible get Document:security.guidance.dependency --detail body
...
~~~

In short: read the code that uses the package, and cite the passage that
decides it. A judgment is one write of two changes: `create` the
`security.app_exposure` pair keyed by the occurrence and the vulnerability,
and `set` either its `justification` (not affected, with a VEX reason) or its
`verdict` `affected`. It is written with `accept: never`, and the reviewer
from step 1, a different agent, reviews and approves it.

pygoat pins PyYAML 5.1, which OSV lists for CVE-2020-14343 (arbitrary code
through `yaml.full_load`). The Django application this SBOM describes never
imports `yaml`: the only `yaml.load` in the repository is in a separate lab
container with its own requirements. From Python (the `cruxible` install
includes the SDK), in the project directory:

~~~python
from cruxible_client import Cruxible
from cruxible_client.contracts.write import FileEvidence

cx = Cruxible.connect()
batch = cx.changes(
    because="CVE-2020-14343: the only yaml.load in the repo is in dockerized_labs/a9_uckv_lab, a separate container with its own requirements; the Django app this SBOM describes never imports yaml.",
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
outcome = batch.write(accept="never")
outcome.status    # 'awaiting_approval'
~~~

`print(outcome)` shows what it did. The cited file is catalogued by the write
itself:

~~~text
awaiting approval (generation 10, bb4afd8c9e8e)
  catalogued dockerized_labs/a9_uckv_lab/app.py (as doc-dockerized_labs-a9_uckv_lab-app.py)
  create security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 justification: vulnerable_code_not_in_execute_path  [CLM-34cb42d700cd69862c47456014f0634f; verdict supported]
  + subject security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343
proposal: sha256:54251d28f92133c36a9de073190d93ba6412bfc3ac4aed94d8d87f083ec38998 (awaiting_external_approval)
awaiting: independent approval; approvers: reviewer
next: cx.proposal("sha256:54251d28…").approve(signer=cx.signer("reviewer"), reviewed=cx.proposal("sha256:54251d28…").review())
~~~

The same write over MCP is one `cruxible_write` call. This is the write
above through a different client, not a second judgment: sent first, its
result's `status` is `awaiting_approval` and its `next` names
`cruxible_proposal_approve` with `signer_id` `reviewer`; sent after the SDK's
write has landed, it finds the pair and the value already live and changes
nothing:

~~~json
{
  "because": "CVE-2020-14343: the only yaml.load in the repo is in dockerized_labs/a9_uckv_lab, a separate container with its own requirements; the Django app this SBOM describes never imports yaml.",
  "accept": "never",
  "changes": [
    {
      "op": "create",
      "label": "pair",
      "subject_kind": "security.app_exposure",
      "key": {
        "occurrence": "security.occurrence/sbom.cdx.json--pkg-pypi-pyyaml-5.1",
        "vulnerability": "security.vulnerability/cve-2020-14343"
      }
    },
    {
      "op": "set",
      "subject": {"created": "pair"},
      "field": "justification",
      "value": "vulnerable_code_not_in_execute_path",
      "evidence": {
        "kind": "file",
        "file": "dockerized_labs/a9_uckv_lab/app.py#data = yaml.load(file.stream, Loader=yaml.Loader)"
      }
    }
  ]
}
~~~

The reviewer works in its own session, as `reviewer`, and reads the proposal
before approving it. Its `next` lists the proposal
(`proposal_awaiting_approval`), and the review leads with the values and the
text each one cites:

~~~text
$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal review sha256:54251d28…
...
Guidance for ClaimType:security.app_exposure.justification: document:security.guidance.dependency (Judging a dependency exposure)
...
Values (4):
  create security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 justification: vulnerable_code_not_in_execute_path  [CLM-34cb42d700cd69862c47456014f0634f; verdict supported]
    cites CAP-0e7901adf211 dockerized_labs/a9_uckv_lab/app.py#data = yaml.load(file.stream, Loader=yaml.Loader)
      | data = yaml.load(file.stream, Loader=yaml.Loader)
...

$ CRUXIBLE_PRINCIPAL_ID=reviewer cruxible proposal approve sha256:54251d28… --yes
...
Approved sha256:b31e4f5d835970ff827a0ef93da51ffb1da0bd01350fed6c543557973493c09e (sha256:54251d28f92133c36a9de073190d93ba6412bfc3ac4aed94d8d87f083ec38998) as reviewer
Activated at b262ac828637ab6fbdc3df3a8806c9bc214a933f
~~~

A reviewer that disagrees rejects instead (`cruxible proposal reject
PROPOSAL_ID --reason TEXT`), and the author writes again.

An exposure the code does reach gets the `verdict` `affected` instead, citing
the passage on the call path. pygoat's registration form validates an
attacker-supplied address with Django's `EmailField`, which CVE-2023-36053
makes a denial of service on Django 4.2. As a file for `cruxible write`:

~~~yaml
because: "CVE-2023-36053: NewUserForm validates an attacker-supplied email with EmailField on django 4.2 (fixed in 4.2.3); the registration view reaches EmailValidator."
changes:
  - op: create
    label: pair
    subject_kind: security.app_exposure
    key: {occurrence: security.occurrence/sbom.cdx.json--pkg-pypi-django-4.2, vulnerability: security.vulnerability/cve-2023-36053}
  - op: set
    subject: {created: pair}
    field: verdict
    value: affected
    evidence: {kind: file, file: "introduction/forms.py#email = forms.EmailField(required=True)"}
~~~

~~~text
$ cruxible write cve-2023-36053.yaml --no-accept
awaiting approval (generation 11, b262ac828637)
  catalogued introduction/forms.py (as doc-introduction-forms.py)
  create security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053 verdict: affected  [CLM-2c1eebb499ac88f0f3ea0ab7131d75f4; verdict supported]
  + subject security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053
proposal: sha256:5e06bd1cbc8f251fc9156639336dfa47f76f39cacc19fede4145271aa47dea9b (awaiting_external_approval)
awaiting: independent approval; approvers: reviewer
next: cruxible proposal approve sha256:5e06bd1cbc8f251fc9156639336dfa47f76f39cacc19fede4145271aa47dea9b --signer-id reviewer
~~~

A third judgment here was written over MCP, in the `cruxible_write` form shown
above: the `verdict` `affected` on Django's CVE-2023-46695, the top row of the
worklist, citing `introduction/forms.py#class NewUserForm(UserCreationForm):`
(the form's username field is the affected `UsernameField`, and the README
documents running natively on Windows, where the slow normalization applies).
An MCP server acts as the principal it was started as, the owner here, so the
reviewer approves from its own session: the CLI as above, or an MCP server
started with `CRUXIBLE_PRINCIPAL_ID=reviewer`.

Once the reviewer has approved all three, the pairs leave the worklists. A not_affected judgment moves
its row to `security.covered_app_exposures`, and an `affected` verdict to
`security.confirmed_exposures`, the list to remediate, until the inventory's
SBOM no longer lists the vulnerable release:

~~~text
$ cruxible query --name security.app_not_affected_judgments
subject                                                       app_exposure                                        component            cve_id          inventory      justification                        flags
security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--c…  sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343  pkg:pypi/pyyaml@5.1  CVE-2020-14343  sbom.cdx.json  vulnerable_code_not_in_execute_path  -

$ cruxible query --name security.confirmed_exposures
subject                                                       app_exposure                                        component            cve_id          epss_percentile  epss_score  inventory      ...
security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--c…  sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  0.99008          0.53447     sbom.cdx.json  ...
security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--c…  sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053  pkg:pypi/django@4.2  CVE-2023-36053  0.87501          0.02978     sbom.cdx.json  ...
~~~

Then record what you decided about the inbox item that raised
CVE-2023-46695: link it to the pair the write created, `dismiss` it with a
reason, or `ack` it:

~~~text
$ cruxible inbox link INB-47fe039489bc --subject security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-46695
linked  INB-47fe039489bc  Line:security.exposure-attention.unjudged  -  2026-11-17T22:54:07.820414+00:00
~~~

The daily run no longer posts a judged pair. A pair it posts again is the
same item, counted as seen again, with its status and decision kept:

~~~text
$ cruxible runbook run security.maintain
Runbook security.maintain: completed  [RBR-02296f3a09244938babce18dd4463dd2]
   1  succeeded   Line:security.osv-query  (RUN-8abfc81035a6fc29906c0b2538eb31a3a7c5235a6df55d91c02c10c366784d4b)
      written 0 (settled 0, proposed 0); unchanged 181; refused 0; inbox 0
...
   4  succeeded   Line:security.exposure-attention.unjudged  (RUN-7c3038f9629b7acdd0325e5a2d6f060be99259cd07a181f81e7eb1f38102f29e)
      written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 0 new, 6 seen again
...
~~~

## When a source fails: unknown is not "none"

Every answer is one of three things, and it matters which:

- **Found**: a source was read and lists the problem.
- **None**: a source was read and doesn't, like the KEV Claim above: the
  catalog was read completely, so "not listed" is recorded, with the Capture
  it rests on.
- **Unknown**: nobody has read it yet, or the read failed or was incomplete.

A failed or incomplete read never becomes "not affected". KEV records a
watched CVE as not listed only when the catalog's records match its stated
count; an OSV answer that failed or came back incomplete never retires an
`affected_by` Claim; an EPSS request that fails leaves the run incomplete, and
a CVE FIRST has not scored yet gets no score. Check the run before you read
an empty answer as "none". `runbook run` marks a step that needs attention,
an incomplete one included, with `!`. `get Line:NAME --detail runs` lists a
Line's runs by status only; the run itself says whether each step was
complete:

~~~text
$ cruxible get Line:security.osv-query --detail runs
RUN-8abfc81035a6fc29906c0b2538eb31a3a7c5235a6df55d91c02c10c366784d4b  succeeded  started 2026-11-17T22:55:04.013652+00:00  nodes_done=5  Procedure:security.osv-query
RUN-8c2fa0e801008886d59a2c38beaa05fa8537ff9067b3a349d7ac19e7ec64023d  succeeded  started 2026-11-17T22:53:38.631764+00:00  nodes_done=5  Procedure:security.osv-query

$ cruxible get RUN-8c2fa0e801008886d59a2c38beaa05fa8537ff9067b3a349d7ac19e7ec64023d
...
nodes:
  components read succeeded: 32 rows
  osv call succeeded: 338 captures CAP-221640220b96 CAP-d4feb52eb7a1 ...
  matched call succeeded: 1 capture CAP-895d061ac923
  affected extract succeeded: 181 records, complete_when 181/181
  write claims succeeded: 181 written, 181 subjects created; settled
...
~~~

One gap has no worklist row: the kit keys vulnerabilities by CVE id, so an
OSV advisory with no CVE alias (a GHSA-only advisory) has no vulnerability
Subject and no Claim. The `security.osv-unkeyed` Line posts one summary item
for them, however many there are, pointing at the OSV match document that
lists every one under `unkeyed`. On pygoat that is 10 advisories (for
certifi, cryptography, pillow, pyjwt and sqlparse):

~~~text
$ cruxible inbox list --line security.osv-unkeyed
STATUS  ITEM  LINE  KIND  SEEN  TITLE
unread  INB-0573b8a3bf53  Line:security.osv-unkeyed  post  1  count=10 matches=sha256:895d061ac923a62732f1d03757e194c42f1fa9562c6061b1cd7b826…

$ cruxible inbox get INB-0573b8a3bf53
...
{
  "count": 10,
  "matches": "sha256:895d061ac923a62732f1d03757e194c42f1fa9562c6061b1cd7b826b319ddefb",
  "sample": [
    {
      "osv_id": "GHSA-6q48-6c7w-9gw2",
      "purl": "pkg:pypi/certifi@2022.12.7"
    },
...
  ],
  "set_digest": "sha256:22f6ca2f0b162c8deca5f882cfefe07da3b77b2d7464b901cc69fa35158b7696"
}
~~~

`cruxible capture read <matches>` returns the match document
([Reading a Capture's bytes](evidence-and-admission.md#reading-a-captures-bytes)).
Read the advisories and decide (upgrade, or `ack` the item). The item posts
only while the Line is enabled when an OSV result lands; a rerun that finds
the same set is the same item, seen again.

## What's next

- `cruxible next` lists what needs attention, including judgments whose cited
  code changed (`citation_drifted`). [Keeping knowledge
  current](keeping-knowledge-current.md) explains each kind of row.
- The security kit's README (in the kits repository, beside the kit) covers
  the rest: per-inventory thresholds for the attention Line, package-level
  judgments, and the **audit loop**, where agents report findings in your own
  code by key (or import a scanner's SARIF file through
  `security.sarif-import`), a second agent judges each one, and coverage units
  record what was checked against which code.
- [Concepts](concepts.md) explains the ideas behind what you just did, and
  [Review and governance](review-and-governance.md) how a second agent's
  review works.
- [For AI agents](for-ai-agents.md) connects an agent to the same instance.
