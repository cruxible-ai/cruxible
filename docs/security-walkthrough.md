# Security walkthrough

The [Quickstart](quickstart.md) gets to the first answer in a few commands.
This walkthrough takes the same steps one at a time, with their outputs, so you
can see what each step does. We'll
point Cruxible at one of your projects, find which of its dependencies carry a
known vulnerability, rank them by whether attackers exploit them (CISA's Known
Exploited Vulnerabilities catalog, KEV) and how likely they are to (FIRST's
EPSS score), and then record a judgment about one of them that a second agent
reviews. Every answer traces back to the source it came from.

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
remembers the connection, so later commands here need no extra flags:

~~~text
$ cd your-project
$ cruxible init --principal-id owner
Started a Cruxible daemon on unix socket .../state/run/daemon.sock (...); `cruxible server stop` stops it.
...
Cruxible initialized: inst_aca64cfe04584590 at 3891ad6370f0
...
Approval policy: self_approval_allowed
Base kit: base 1.0.0 installed
More: cruxible context show
~~~

The **base kit** holds what every instance needs to read workspace files and
fetch web pages. With one owner (`self_approval_allowed`, the default), your
own approval is enough for anything you propose.

## Step 2: Install the security kit

A **kit** is a vocabulary and the definitions around it, published so any
instance can install them. `kit add` shows you what it would install before it
does anything:

<!-- publish: kit tag -->
~~~text
$ cruxible kit add cruxible-kits/kits/security
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
Preview at 3891ad6370f0; nothing was written.
Commit it: rerun the same command with --commit --at 3891ad6370f0
~~~

Until the registry release of 1.3.0, install it from a checkout of the
[kits repository](https://github.com/cruxible-ai/cruxible-kits); afterwards
`cruxible kit add security:1.3.0` does the same. The **providers** are the
packages that fetch and parse sources: OSV for vulnerabilities, a web
provider for KEV and EPSS, and a SARIF reader for scanner output. Commit the
install at the coordinate the preview printed:

<!-- publish: kit tag -->
~~~text
$ cruxible kit add cruxible-kits/kits/security --commit --at 3891ad6370f0
security 1.3.0: accepted (install)
  provider cruxible-provider-web (cruxible-provider-web 0.7.0): install (Installation and registration grant no execution permissions. Runtime resources require verification.)
  provider security-osv-provider (security-osv-provider 0.1.0.dev0): install
  provider security-sarif-provider (security-sarif-provider 0.1.0.dev0): install
  requires base >= 1.0.0: met (installed 1.0.0)
...
Proposal: sha256:ddd20f95a263a70365e95825291e030f87f8c0b8396aef53cf3e4b37bcaa39f2

$ cruxible kit status
base 1.0.0 content sha256:07e64748e385c0dc51a69cb154b8c3fb3d57906ab59aba9d1dac93bbf3b60845; up to date
  source: bundled
security 1.3.0 content sha256:7545c96454bd7daca431d978cebba64f15f3ead59c08ca0edee9c4f6ab82584b; update check local source
...
  requires base >= 1.0.0: met (installed 1.0.0)
~~~

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
outcome: submitted (ready_to_activate)
intent: AIT-c70cbead72824d739414f640464b1894
proposal: sha256:792b9e1dd42606a7dab6cef56a56976f75c469324e18def449eccc9afb6ec4e1
reason: not activated: the caller requested accept="never"
next: cruxible proposal activate sha256:792b9e1dd42606a7dab6cef56a56976f75c469324e18def449eccc9afb6ec4e1
~~~

The one decision here is what may land without asking you. `proposal review`
shows each Line (what it runs, bound to which provider build, with which
parameters and budget) and each Mandate (what it lets a Line settle):

~~~text
$ cruxible proposal review sha256:792b9e1d…
...
Changes: 17 created / 0 revised / 0 retired
  line: 6 created / 0 revised / 0 retired
  procedure-mandate: 8 created / 0 revised / 0 retired
  runbook: 2 created / 0 revised / 0 retired
  trigger: 1 created / 0 revised / 0 retired
Definitions (17):
...
  Line security.osv-query: created
    procedure    security.osv-query
    authority    settle
    binding      match -> security-osv-provider/4c33b644bb75#osv.match
    binding      query -> security-osv-provider/4c33b644bb75#osv.query
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
Stored fields grouped by kind: --json. Every member's artifact and delta: --detail
~~~

The four source Lines (SBOM import, OSV, KEV, EPSS) may settle what they copy
from their sources, within the ClaimTypes and bases each Mandate lists. The
two attention Lines only post to the inbox. No Mandate settles a judgment.
Approve it:

~~~text
$ cruxible proposal approve sha256:792b9e1d… --yes
Signing sha256:8cfa7eeb62c9eb37605590d7eefbb444bd927fe5bfb3609041083259190260c4 (sha256:792b9e1dd42606a7dab6cef56a56976f75c469324e18def449eccc9afb6ec4e1): 17 changes (17 create)
Signing key for owner: ...
Approved sha256:8cfa7eeb62c9eb37605590d7eefbb444bd927fe5bfb3609041083259190260c4 (sha256:792b9e1dd42606a7dab6cef56a56976f75c469324e18def449eccc9afb6ec4e1) as owner
Activated at 80d15d26d24891fcd1c0bf442a654e5e49ac0c16
~~~

`proposal approve` shows what you're signing and asks before it signs;
`--yes` skips the question, and an agent or a script, with no terminal to
answer it, must pass it.

## Step 5: Run it once

The template's `security.start` **Runbook** runs the six Lines in order: SBOM
import, OSV, KEV, EPSS, then the two attention Lines. `runbook preview` shows
what each step reads, writes, calls and needs, and runs nothing:

~~~text
$ cruxible runbook preview security.start
Runbook security.start (preview: definitions, and whether each workspace file is there; nothing ran)
   1  Line:security.sbom-import.app  Procedure:security.sbom-import
      writes security.occurrence: security.occurrence.scope
      calls  read -> Provider:cruxible-builtin workspace.file  (capability workspace.file@1)
      calls  derive -> Provider:security-osv-provider/4c33b644bb75 sbom.components  (capability security.sbom.components@1)
      needs  file sbom.cdx.json (json, at most 1 MiB, parameter relative_path): present
...
   3  Line:security.kev-triage  Procedure:security.kev-triage
      reads  query security.watched_vulnerabilities: security.component.affected_by, security.occurrence.component, security.occurrence.scope, security.vulnerability.cve_id
      writes security.vulnerability: security.vulnerability.kev_date_added, security.vulnerability.kev_due_date, security.vulnerability.kev_ransomware_use, security.vulnerability.listed_in
      calls  fetch -> Provider:cruxible-provider-web/f3543a2990f6 web.fetch  (capability web.fetch@1)
...
~~~

Run it. The calls are real: OSV, CISA and FIRST answer over the network, and
the run took about a minute here:

~~~text
$ cruxible runbook run security.start
Runbook security.start: completed; needs attention: step 5  [RBR-9ae4b3e68a7d498c82fe6d992a03ac5e]
   1  succeeded   Line:security.sbom-import.app  (RUN-cf189271f6badbe3c3d55ba74c1a69bd35aa30e1ba72735e874b818c29d0bdc3)
      written 32 (settled 32, proposed 0); unchanged 0; refused 0; inbox 0
   2  succeeded   Line:security.osv-query  (RUN-8337acbca5eec54f090868030e7e4993be2625da76830f250525c3c0b0529f76)
      written 181 (settled 181, proposed 0); unchanged 0; refused 0; inbox 0
   3  succeeded   Line:security.kev-triage  (RUN-d81bd785fcab3c59ad6ea17fc7953806a6c7d3c14f852ce8b1fe5122f9b5ed98)
      written 181 (settled 181, proposed 0); unchanged 0; refused 0; inbox 0
   4  succeeded   Line:security.epss-triage  (RUN-7c1da3d37ab4636e54bfa21889f9dd176eee2d6434768ee0643148095980a5c2)
      written 543 (settled 543, proposed 0); unchanged 0; refused 0; inbox 0
   5  succeeded   Line:security.exposure-attention.unjudged  (RUN-546d62d60c48169b91f4104fb440b9cfe79312ac19fc333d9728ec5ebe100395)
    ! written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 7 new
   6  succeeded   Line:security.exposure-attention.contested  (RUN-da8c28e02bb8e3ff4964d2d89db6b876f823a5f9176b550bdcbe65928871c64d)
      written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 0
~~~

32 components, 181 vulnerabilities OSV lists for them, KEV's answer for each
(none of these is on it) and its EPSS score, all settled under the Mandates
you approved. Step 5 posted seven exposures to the inbox.

To keep the facts current, enable the Lines; the `security.maintain-daily`
Trigger then runs OSV, KEV, EPSS and the attention Lines once a day, each under
its own Line's enablement:

~~~bash
for l in sbom-import.app osv-query kev-triage epss-triage exposure-attention.unjudged exposure-attention.contested; do
  cruxible line enable security.$l
done
~~~

The SBOM import is not on the clock: rerun `cruxible runbook run
security.start` when the SBOM changes. (It is enabled anyway so `next` stops
listing it as `line_not_enabled`.) The Trigger fires only while the daemon is
running: a daily instant missed while it was down is not run afterwards, so run
`cruxible runbook run security.maintain` yourself after downtime.

## Step 6: Ask your questions

**What do we look at first?** The kit's saved question lists every exposure
nobody has judged, KEV-listed first, then by EPSS score:

~~~text
$ cruxible query --name security.urgent_exposures --limit 5
subject                                component            cve_id          epss_percentile  epss_score  inventory      kev_date_added  kev_due_date  kev_ransomware_use  occurrence                          scope     flags
security.vulnerability/cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  0.99007          0.53447     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-39614  pkg:pypi/django@4.2  CVE-2024-39614  0.98168          0.28637     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-57833  pkg:pypi/django@4.2  CVE-2025-57833  0.98147          0.28271     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2024-45230  pkg:pypi/django@4.2  CVE-2024-45230  0.98001          0.25773     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
security.vulnerability/cve-2025-64459  pkg:pypi/django@4.2  CVE-2025-64459  0.97382          0.19257     sbom.cdx.json  -               -             -                   sbom.cdx.json--pkg-pypi-django-4.2  required  -
truncated: 176 more rows; next: cruxible query --name security.urgent_exposures --limit 5 --cursor q2.7f1121ed423667c2.hhhflksevyd.d3952d60b31f.1af7fdbe35ee.5
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
  epss_percentile  0.98147
  epss_score       0.28271
  epss_score_date  2026-11-15
  listed_in        (no value)
                   none of security.catalog/cisa-kev  CLM-02eb2bf2b25de68ec70ebd4190b24893 [supported]
...

$ cruxible get CLM-02eb2bf2b25de68ec70ebd4190b24893 --detail evidence
value: {"none": "security.catalog/cisa-kev"}
capture CAP-66a2733822f0  CaptureContract:security.kev-catalog v1  source=cisa.kev.catalog  observed=2026-11-16T02:45:41.075731+00:00  evidence: admitted
rationale: Complete source establishes the declared inverse.

$ cruxible get CLM-088d27304d906b22182aeb2ff043a8dc --detail evidence
value: security.vulnerability/cve-2026-1312
capture CAP-64226617e319  CaptureContract:security.osv-matches v1  source=osv.dev.query  observed=2026-11-16T02:45:26.569419+00:00  evidence: admitted
rationale: OSV GHSA-6426-9fv3-65x8 lists CVE-2026-1312 for pkg:pypi/django@4.2.
~~~

**What arrived for me?** `next` lists what needs attention, here the inbox
the attention Line posted to:

~~~text
$ cruxible next
repair  line_inbox_unread  Line:security.exposure-attention.unjudged  next=cruxible.inbox.list
  repair: cruxible inbox list --line security.exposure-attention.unjudged --status unread

$ cruxible inbox list
STATUS  ITEM  LINE  SEEN  LAST_POSTED
unread  INB-f414715538fc  Line:security.exposure-attention.unjudged  -  2026-11-16T02:45:50.271108+00:00
  {"component": "pkg:pypi/django@4.2", "cve_id": "CVE-2024-39614", "inventory": "sbom.cdx.json", "occurrence": "sbom.cdx.json--pkg-pypi-django-4.2", "state": "unjudged"}
...
unread  INB-47fe039489bc  Line:security.exposure-attention.unjudged  -  2026-11-16T02:45:50.271108+00:00
  {"component": "pkg:pypi/django@4.2", "cve_id": "CVE-2023-46695", "inventory": "sbom.cdx.json", "occurrence": "sbom.cdx.json--pkg-pypi-django-4.2", "state": "unjudged"}
...
~~~

The inbox is never accepted state: it is where a Line hands you something it
cannot decide.

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
`verdict` `affected`. It is written with `accept: never`, and a different agent
reviews and approves it.

First register the reviewer, a second principal with its own key. (A
reviewer registered after the author writes also works: its approval lands
the proposal at the head first, then signs it. A kit change does not land
that way; its author commits the kit again.)

~~~text
$ cruxible principal add reviewer --key-dir ../reviewer-keys --signer-key OWNER_KEY
Principal reviewer: registered and active
Proposal: sha256:c1c0a6e3f51eef67a35cbfc9b5e3ff3d10d33fb85e309d7d7fff98b497cae2aa
Private key retained locally at: ../reviewer-keys/reviewer.ed25519
...
~~~

Keep the key directory outside the repository. Its `cruxible.env` holds the
settings the reviewer's session loads.

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
awaiting approval (generation 12, 480f7027b060)
  catalogued dockerized_labs/a9_uckv_lab/app.py (as doc-dockerized_labs-a9_uckv_lab-app.py)
  create security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 justification: vulnerable_code_not_in_execute_path  [CLM-41547aa62b3442b578d499b4759e4894; verdict supported]
  + subject security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343
proposal: sha256:0995dfa8ca0741f33576b1d261a97d6237aab13ee4e6f3be068f51cca9357c24 (ready_to_activate)
awaiting: kept (accept=never)
next: cx.proposal("sha256:0995dfa8ca0741f33576b1d261a97d6237aab13ee4e6f3be068f51cca9357c24").activate()
~~~

On the CLI, `cruxible write FILE` takes the same write as a YAML or JSON
file. For MCP-only clients, it is one `cruxible_write` call:

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

The reviewer works in its own session, with the settings `principal add`
wrote, and reads the proposal before approving it. The review leads with the
values and the text each one cites:

~~~text
$ cruxible proposal review sha256:0995dfa8…
...
Values (4):
  create security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343 justification: vulnerable_code_not_in_execute_path  [CLM-41547aa62b3442b578d499b4759e4894; verdict supported]
    cites CAP-4c445de5d9b1 dockerized_labs/a9_uckv_lab/app.py#data = yaml.load(file.stream, Loader=yaml.Loader)
      | data = yaml.load(file.stream, Loader=yaml.Loader)
...

$ cruxible proposal approve sha256:0995dfa8… --yes
...
Approved sha256:10ddf6b49f48971508650c0b819cd884afd8e1d10bd03a51e3082561420af9e3 (sha256:0995dfa8…) as reviewer
Activated at be0c6190a25f75ed7bc947ea0fad96167674fb37
~~~

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
awaiting approval (generation 10, 7f1121ed4236)
  catalogued introduction/forms.py (as doc-introduction-forms.py)
  create security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053: created
  set security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053 verdict: affected  [CLM-c53cda9e3d7ee1ecb5f1a23635a1572a; verdict supported]
  + subject security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053
proposal: sha256:a432c30bb336ebc8f4dca9452a5248b1e9827e903312e8f99c57891d3affe775 (ready_to_activate)
awaiting: kept (accept=never)
next: cruxible proposal activate sha256:a432c30bb336ebc8f4dca9452a5248b1e9827e903312e8f99c57891d3affe775
~~~

Once approved, the pairs leave the worklists. A not_affected judgment moves
its row to `security.covered_app_exposures`, and an `affected` verdict to
`security.confirmed_exposures`, the list to remediate, until the inventory's
SBOM no longer lists the vulnerable release:

~~~text
$ cruxible query --name security.app_not_affected_judgments
subject                                                       app_exposure                                        component            cve_id          inventory      justification                        flags
security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--c…  sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-46695  pkg:pypi/django@4.2  CVE-2023-46695  sbom.cdx.json  vulnerable_code_not_in_execute_path  -
security.app_exposure/sbom.cdx.json--pkg-pypi-pyyaml-5.1--c…  sbom.cdx.json--pkg-pypi-pyyaml-5.1--cve-2020-14343  pkg:pypi/pyyaml@5.1  CVE-2020-14343  sbom.cdx.json  vulnerable_code_not_in_execute_path  -

$ cruxible query --name security.confirmed_exposures
subject                                                       app_exposure                                        component            cve_id          epss_percentile  epss_score  inventory      ...
security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--c…  sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-36053  pkg:pypi/django@4.2  CVE-2023-36053  0.87487          0.02978     sbom.cdx.json  ...
~~~

(The CVE-2023-46695 judgment was written over MCP.) Then record
what you decided about the inbox item: link it to the pair the write created,
`dismiss` it with a reason, or `ack` it:

~~~text
$ cruxible inbox link INB-47fe039489bc --subject security.app_exposure/sbom.cdx.json--pkg-pypi-django-4.2--cve-2023-46695
linked  INB-47fe039489bc  Line:security.exposure-attention.unjudged  -  2026-11-16T02:45:50.271108+00:00
~~~

When the daily run posts a pair again, the item is counted as seen again and
keeps your decision:

~~~text
$ cruxible runbook run security.maintain
Runbook security.maintain: completed  [RBR-10eac0f11194464b8e0cd56fe1b37099]
   1  succeeded   Line:security.osv-query  (RUN-7dfda1e44ba8f5a7f5af6318bd3bb607481c690751850f624bef26afc721e1c6)
      written 0 (settled 0, proposed 0); unchanged 181; refused 0; inbox 0
...
   4  succeeded   Line:security.exposure-attention.unjudged  (RUN-bb37f00253f061c794a0a0f6b97daf8707d888ae62edee4c8f0215e9d5314bf7)
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
RUN-461b30558b670fdcd085aaa52666e86051386c21335c062979acc0905630c9a2  succeeded  started 2026-11-16T05:11:00.601053+00:00  nodes_done=5  Procedure:security.osv-query

$ cruxible get RUN-461b30558b670fdcd085aaa52666e86051386c21335c062979acc0905630c9a2
...
nodes:
  components read succeeded: 32 rows
  osv call succeeded: 338 captures CAP-21dd7fccfe8a CAP-fa09735787f4 ...
  matched call succeeded: 1 capture CAP-1794680ef26b
  affected extract succeeded: 181 records, complete_when 181/181
  write claims succeeded: 181 written, 181 subjects created; settled
...
~~~

One gap no run reports: the kit keys vulnerabilities by CVE id, so an OSV
advisory with no CVE alias (a GHSA-only advisory) appears only in the OSV match
document's `unkeyed` list, with no vulnerability Subject and no worklist row.
On pygoat that is 10 advisories (for certifi, cryptography, pillow, pyjwt and
sqlparse). Read the match document's Capture to see them
([Reading a Capture's bytes](evidence-and-admission.md#reading-a-captures-bytes)).

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
