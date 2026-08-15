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
    DonorEntry("cruxible_core.procedure", "PC-A2", "procedure domain seed"),
    DonorEntry("cruxible_core.workflow", "PC-C2", "procedure execution seed"),
    DonorEntry("cruxible_core.config", "PC-A1", "selected schema vocabulary only"),
    DonorEntry("cruxible_core.predicate", "PC-A2", "guard predicate seed"),
    DonorEntry("cruxible_core.query", "PC-D", "projection/query implementation seed"),
    DonorEntry("cruxible_core.graph", "PC-D", "projection/query implementation seed"),
    DonorEntry("cruxible_core.receipt", "PC-C2", "procedure receipt seed"),
    DonorEntry("cruxible_core.attestation", "PC-B2", "attestation model seed"),
    DonorEntry(
        "cruxible_core.resolution_contracts",
        "PC-A2",
        "resolution contract seed",
    ),
    DonorEntry(
        "cruxible_core.source_artifacts",
        "PC-B1",
        "source-reference implementation seed",
    ),
    DonorEntry("cruxible_core.markdown", "PC-B1", "source parser seed"),
    DonorEntry("cruxible_core.provider", "PC-C2", "procedure provider seed"),
    DonorEntry("cruxible_core.providers", "PC-C2", "procedure provider seed"),
    DonorEntry("cruxible_core.group", "PC-B2", "attestation grouping seed"),
    DonorEntry("cruxible_core.kits", "PC-G", "kit migration seed"),
    DonorEntry("cruxible_core.runtime.instance", "DP-0C", "storage migration donor"),
    DonorEntry("cruxible_core.storage.sqlite", "DP-0C", "storage migration donor"),
    DonorEntry("cruxible_core.instance_protocol", "DP-0C", "storage migration donor"),
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
