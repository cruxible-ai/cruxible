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
        "PC-E2",
        "definition, digest, static-law, and run/read donor through the final transplant",
    ),
    DonorEntry(
        "cruxible_core.workflow",
        "PC-E2",
        "compiler, contract, transform, and executor donor through line runtime parity",
    ),
    DonorEntry(
        "cruxible_core.config.schema",
        "PC-F",
        "selected step, query, provider, and contract schema donor",
    ),
    DonorEntry(
        "cruxible_core.predicate",
        "PC-F",
        "typed comparison and coercion donor through query parity",
    ),
    DonorEntry(
        "cruxible_core.query",
        "PC-F",
        "traversal, filtering, and projection behavior donor",
    ),
    DonorEntry(
        "cruxible_core.graph",
        "PC-F",
        "query-oracle types and EvidenceRef behavior donor",
    ),
    DonorEntry("cruxible_core.receipt", "PC-E1", "exhaust receipt-tree donor"),
    DonorEntry(
        "cruxible_core.attestation",
        "PC-C",
        "stance, disposition, and idempotency semantics donor",
    ),
    DonorEntry(
        "cruxible_core.resolution_contracts",
        "PC-E1",
        "resolution declaration, activation, and disposition donor",
    ),
    DonorEntry(
        "cruxible_core.source_artifacts.markdown",
        "PC-C",
        "deterministic markdown span-extraction donor",
    ),
    DonorEntry(
        "cruxible_core.provider",
        "PC-E2",
        "provider contract, registry, and trace donor through line runtime parity",
    ),
    DonorEntry(
        "cruxible_core.providers",
        "PC-E2",
        "provider implementations retained through line runtime parity",
    ),
    DonorEntry(
        "cruxible_core.group",
        "PC-D",
        "frozen propose_group_from verifier support only",
    ),
    DonorEntry(
        "cruxible_core.kits",
        "PC-D",
        "old-compiler helper pieces retained for frozen verification only",
    ),
    DonorEntry(
        "cruxible_core.runtime.instance",
        "PC-F",
        "temporary donor-parity harness",
    ),
    DonorEntry(
        "cruxible_core.storage.sqlite",
        "PC-F",
        "temporary donor-parity storage harness",
    ),
    DonorEntry(
        "cruxible_core.instance_protocol",
        "PC-F",
        "temporary donor-parity interface harness",
    ),
    DonorEntry(
        "cruxible_core.governance.actors",
        "PC-A1",
        "governed identity until the Playbill principal context lands",
        "cruxible_core.playbill.donors.actors",
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
