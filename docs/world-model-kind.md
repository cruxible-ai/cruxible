# World Models And Config Kind

Cruxible uses **world model** as the product metaphor: the governed universe an
agent can inspect, query, and safely change through Cruxible surfaces.

`CoreConfig.kind` is narrower. It is a capability profile for one config file:

- `ontology` means schema-only and non-executable. Use it for shared entity,
  relationship, enum, query, and validation definitions that should not run
  workflows or providers by themselves.
- `world_model` means the config can participate in an operational world model.
  It may define executable workflows, providers, artifacts, policies, local
  state, and proposal or decision flows.

Kit distribution roles do not live in `kind`. They live in `cruxible-kit.yaml`.
A kit can be `standalone` or `overlay`, regardless of whether its entry config
uses `kind: ontology` or `kind: world_model`.

Example:

- `kits/kev-reference` is a standalone `world_model` kit. It builds the public
  KEV reference world from pinned CISA KEV, EPSS, and NVD data.
- `kits/kev-triage` is an overlay `world_model` kit targeting `kev-reference`.
  It adds local assets, services, owners, controls, and governed proposal
  workflows that connect public vulnerabilities to customer-owned operational
  state.

Future robotics, digital-twin, or simulation-oriented kits can still use the
same top-level metaphor. They may be shaped very differently, but the assembled
result is still the world model exposed to an agent.
