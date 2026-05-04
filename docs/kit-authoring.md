# Kit Authoring And Distribution

A Cruxible kit is a versioned bundle with a `cruxible-kit.yaml` manifest,
an entry config, provider code, optional data, and a bundled
`cruxible.lock.yaml`.

For runnable examples, see [Kit Walkthroughs](kit-walkthroughs.md).

Minimal manifest:

```yaml
schema_version: cruxible.kit.v1
kit_id: kev-triage
version: 0.2.0
role: overlay
target_world: kev-reference
entry_config: config.yaml
provider_paths:
  - providers
copy_paths:
  - data
  - skills
  - README.md
requires_extras: []
```

Rules:

- `role` is `standalone` or `overlay`.
- `role: overlay` requires `target_world`.
- `role: standalone` must not set `target_world`.
- 0.2 supports one `entry_config` per kit.
- `requires_extras` is metadata only. Cruxible does not install kit
  dependencies automatically.

Provider refs use `kit://`:

```yaml
ref: kit://providers/reference.py::normalize_public_kev_reference
```

`kit://` paths are relative to the materialized kit root. Absolute paths,
`..`, symlinks, and paths outside declared `provider_paths` are rejected.
Python providers run in the current Cruxible Python environment and may import
stdlib, `cruxible_core`, installed Cruxible dependencies or extras, and files
under declared provider paths.

Bundle behavior:

- The bundle digest covers every non-junk regular file in sorted POSIX-relative
  order, including path and bytes.
- Junk such as `__pycache__/`, `*.pyc`, `.DS_Store`, `.ruff_cache/`, and
  `.pytest_cache/` is ignored.
- Symlinks are rejected.
- Bundles are cached under `CRUXIBLE_KIT_CACHE_DIR` or
  `${XDG_CACHE_HOME:-~/.cache}/cruxible/kits`.
- Cache installs are locked and atomic by bundle digest.
- Materialization copies the cached kit into the instance root.
- Consumers should not silently regenerate bundled locks. Rebuild the kit lock
  before publishing or distributing a changed kit.

Built-in aliases such as `kev-reference` resolve to versioned OCI kit refs in
installed packages, with local source-checkout kits overriding those aliases
during development. Publishing the matching OCI bundles is a 0.2 release
precondition.

Until Cruxible ships a first-class `cruxible kit lock` command, maintainers
should refresh a bundled lock by copying or materializing the kit into a temp
workspace, running `cruxible lock` there, and copying the resulting
`cruxible.lock.yaml` back into the kit directory before publishing.

Vocabulary:

- Use **overlay** for a local instance tracking a published upstream world.
- Use **clone** for a point-in-time state copy from a snapshot.
- Use **local** for customer-owned seeded or runtime state.
- Do not use clone for kit distribution. Use pull, cache, materialize, or
  install.
