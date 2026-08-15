# Playbill migration oracles

DP-0 is destructive only at the served architecture boundary. These commits
remain the immutable behavioral references while the legacy runtime is removed:

| Oracle | Branch | Exact commit | Preserved behavior |
| --- | --- | --- | --- |
| Family-1 | `playbill` | `e3fe35b360d098f14a5d59bf770ffee401224f0c` | PB-A through PB-E canonicalization, bootstrap, proposal, approval, settlement, projection, source catalog, and explanation goldens |
| Procedure graph-program | `dev/0.4` | `986307d56649eb51747ca227228fbe19f73e3895` | accepted procedure contracts, readings, execution receipts, and deterministic workflow behavior used as Claims + Procedures donors |

Tests must reference the matching checked-in fixture metadata rather than a
moving branch name. A later batch may add a newer oracle, but must not silently
retarget either entry.
