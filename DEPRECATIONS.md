# Deprecations

Cruxible follows deprecate-then-remove for shipped surfaces. Deprecated inputs
remain accepted for the stated window, delegate to or teach the replacement,
and emit the same structured warning shape on every supported transport:
`{surface, replacement, removal_version}`. Removal versions are commitments;
changing one requires updating the registry, this table, tests, and changelog
together.

This schedule starts at the v1 baseline. Surfaces retired before it, and
surfaces that never shipped, are recorded in `CHANGELOG.md` only. From the
baseline on, a row stays after its surface is removed: this table is the
historical schedule, not a list of what is still accepted.

| Surface | Replacement | Deprecated in | Removal version |
| --- | --- | --- | --- |

No surface is deprecated at the v1 baseline.
