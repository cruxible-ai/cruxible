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
        "cruxible_core.workflow",
        "PC-F",
        "query-oracle spine for PC-F parity; ReceiptBuilder/Receipt rehome required first",
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
    DonorEntry(
        "cruxible_core.provider",
        "PC-F",
        "provider contract/trace donor; last consumers are workflow and service types",
    ),
    DonorEntry(
        "cruxible_core.providers",
        "PC-G",
        "un-transplanted tabular/document/identity readers; native source connectors "
        "land with the vertical slice",
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
        "temporary donor-parity interface, snapshot-metadata, and integrity harness",
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
