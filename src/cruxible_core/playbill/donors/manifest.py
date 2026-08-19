"""Auditable inventory of legacy code temporarily retained as Playbill donors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DonorEntry:
    module_prefix: str
    removal_batch: str
    rationale: str
    adapter: str | None = None


DONOR_MANIFEST_VERSION = "playbill-donor-manifest-v1"

DONOR_MANIFEST: tuple[DonorEntry, ...] = (
    DonorEntry(
        "cruxible_core.procedure",
        "PC-H",
        "frozen graph-format v1/v2 corpus verifier; PC-H settles whether it becomes a "
        "permanent non-donor verifier package",
    ),
    DonorEntry(
        "cruxible_core.config",
        "PC-H",
        "step, query, provider, and contract schema donor pinned by the Procedure "
        "definition digest; the whole package is labelled rather than schema.py alone "
        "because carving a module out of it risks moving that frozen digest",
    ),
    DonorEntry(
        "cruxible_core.predicate",
        "PC-H",
        "typed comparison and coercion donor; 100% of its remaining consumers are the "
        "Procedure guard grammar and the config schema it validates",
    ),
    DonorEntry(
        "cruxible_core.query",
        "PC-H",
        "residual query vocabulary the config schema reaches when validating a named "
        "query -- enums, predicates, types, profiles, relationship_state; the engine, "
        "evaluation, filter, projection, continuation, layout, and read-surface donors "
        "left in PC-F",
    ),
    DonorEntry(
        "cruxible_core.graph",
        "PC-H",
        "residual graph vocabulary reached through the same config-schema validator "
        "chain -- types, entity_graph, assertion_state, provenance -- plus the "
        "EvidenceRef behavior the Procedure and workflow lock types depend on; the "
        "mutable graph operations, diff, and identity donors left in PC-F",
    ),
    DonorEntry(
        "cruxible_core.workflow",
        "PC-H",
        "residual lock/plan types only: procedure/pins.py describes what a pin records "
        "in terms of WorkflowLock, LockedProvider, and LockedArtifact, so the module "
        "leaves with the Procedure donor; the compiler and the rest of the query-oracle "
        "spine left in PC-F and the Receipt tree was already rehomed to "
        "cruxible_core.receipt_tree",
    ),
    DonorEntry(
        "cruxible_core.provider",
        "PC-G",
        "residual provider contract/trace types only; the last consumers are the "
        "un-transplanted readers in cruxible_core.providers (providers/common/* is "
        "written against ProviderContext), so it leaves with them rather than with the "
        "registry and payload donors that left in PC-F",
    ),
    DonorEntry(
        "cruxible_core.providers",
        "PC-G",
        "un-transplanted tabular/document/identity readers; native source connectors "
        "land with the vertical slice",
    ),
)


def donor_for(module_name: str) -> DonorEntry | None:
    """Return the most specific declared donor containing *module_name*."""

    matches = (
        entry
        for entry in DONOR_MANIFEST
        if module_name == entry.module_prefix or module_name.startswith(f"{entry.module_prefix}.")
    )
    return max(matches, key=lambda entry: len(entry.module_prefix), default=None)


__all__ = [
    "DONOR_MANIFEST",
    "DONOR_MANIFEST_VERSION",
    "DonorEntry",
    "donor_for",
]
