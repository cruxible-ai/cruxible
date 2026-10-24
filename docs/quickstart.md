# Quickstart

Point Cruxible at one of your projects, let an agent pull in what you ship and
run, and find out which of your dependencies carry a vulnerability attackers
are exploiting right now. Every answer traces back to its source, and nothing
is recorded until it has been reviewed.

You'll need Python 3.11+, Git, a coding agent that can run commands in your
repository, and network access to `pypi.org`, `api.osv.dev`, `www.cisa.gov`
and `epss.empiricalsecurity.com`.

## 1. Install and start Cruxible

~~~bash
pip install cruxible        # or: uv tool install cruxible
cruxible server start --socket ~/.cruxible/run/daemon.sock    # in its own terminal; leave it running
~~~

## 2. Attach your project

~~~bash
cd your-project
export CRUXIBLE_SERVER_SOCKET=~/.cruxible/run/daemon.sock
cruxible init
~~~

## 3. Install the security kit

<!-- publish: kit tag -->
~~~bash
cruxible kit add security:1.3.0                      # shows what it would install
cruxible kit add security:1.3.0 --commit --at OID    # OID: the coordinate the preview printed
~~~

## 4. Adopt its checks

The kit's checks come as an example, `examples/automation.json`, that you
adopt deliberately. Fill in today's date and an expiry 90 days out, submit it,
and turn on the checks the intake uses:

<!-- update: bundle examples -->
<!-- publish: kit tag -->
~~~bash
curl -O https://raw.githubusercontent.com/cruxible-ai/cruxible-kits/security-1.3.0/kits/security/examples/automation.json
sed -i.bak "s/\"NOW\"/\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"/g; s/\"EXPIRES\"/\"$(date -u -v+90d +%Y-%m-%dT%H:%M:%SZ)\"/g" automation.json
cruxible authoring submit automation.json --brief
for l in kev-daily kev-triage kev-attention epss-daily epss-triage sbom-import osv-applicability cve-applicability inventory-import.prod credential-import; do
  cruxible line enable security.$l
done
~~~

(On Linux, use `date -u -d '+90 days' …` for the expiry.)

## 5. Let your agent pull in your state

Give your agent the kit's intake skill. `.claude/skills/` is where Claude Code
looks; point any other agent at the file:

<!-- update: bundle examples -->
<!-- publish: kit tag -->
~~~bash
mkdir -p .claude/skills/security-intake
curl -o .claude/skills/security-intake/SKILL.md https://raw.githubusercontent.com/cruxible-ai/cruxible-kits/security-1.3.0/kits/security/skills/security-intake/SKILL.md
~~~

Then ask it:

> Use the security-intake skill on this repository.

It writes an SBOM and reads it in, asks OSV, KEV and EPSS about your
dependencies, and asks you where the project runs, which services and
credentials it has, and where their logs are. Each read is proposed first;
the agent shows you what it found before it approves anything.

## 6. Ask the first question

**Which exploited vulnerabilities do we run, and where?**

~~~text
$ cruxible query --name security.urgent_exposures
subject                                         ...  asset                component                criticality  cve_id         ...  internet_exposed  ...  kev_listed  ...  service
security.assessment/myapp-1.0.0--cve-2025-3248  ...  app-1.example.com    pkg:pypi/langflow@1.2.0  critical     CVE-2025-3248  ...  true              ...  true        ...  storefront
security.assessment/myapp-1.0.0--cve-2025-3248  ...  app-2.example.com    pkg:pypi/langflow@1.2.0  critical     CVE-2025-3248  ...  false             ...  true        ...  storefront
security.assessment/myapp-1.0.0--cve-2025-3248  ...  stage-1.example.com  pkg:pypi/langflow@1.2.0  -            CVE-2025-3248  ...  -                 ...  true        ...  -
...
~~~

KEV-listed first, then by how likely exploitation is. Here langflow 1.2.0 is
exploited in the wild and runs on an internet-facing host of a critical
service: upgrade it first. `cruxible next` lists what needs attention as the
daily checks run.

## What's next

<!-- publish: kit tag -->

- The [Security walkthrough](security-walkthrough.md) does the same by hand,
  step by step, and goes on to an incident: what's exposed, what's patched,
  and what to rotate.
- [Concepts](concepts.md) explains the ideas behind what you just did.
- The [security kit's README](https://github.com/cruxible-ai/cruxible-kits/blob/security-1.3.0/kits/security/README.md)
  covers everything else it answers.
- [For AI agents](for-ai-agents.md) covers connecting an agent in depth.
