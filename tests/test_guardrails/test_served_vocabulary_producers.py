"""Every frozen closed-vocabulary member has a production producer.

The v1 wire freeze makes an un-emitted member permanent: a caller can never
observe it, and removing it later costs a ratified succession. The guard scans
the shipped source for the exact code string, ignoring the modules that only
declare or classify the vocabularies, so a member whose only producer is a test
double reads as absent here.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalCodeV1,
    ProcedureInternalFailureCodeV1,
    ProcedureOperationalFailureCodeV1,
    ProcedureSettlementRefusalCodeV1,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "cruxible_core"

# Modules that carry a vocabulary member without producing it: the source-owned
# catalog, the provider refusal wire vocabulary, and the outcome map that
# translates one vocabulary into another.
_DECLARATION_ONLY = {
    "service/playbill_refusal_catalog.py",
    "playbill/provider_runtime_contract.py",
    "playbill/provider_outcomes.py",
}


def _production_modules() -> Iterator[Path]:
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        if path.relative_to(_SOURCE_ROOT).as_posix() in _DECLARATION_ONLY:
            continue
        yield path


def _produced_strings() -> frozenset[str]:
    produced: set[str] = set()
    for path in _production_modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                produced.add(node.value)
    return frozenset(produced)


def test_every_admission_refusal_member_is_produced_in_production_source() -> None:
    """U1 froze two Line codes with zero producers; the whole Literal is guarded."""

    produced = _produced_strings()
    missing = sorted(
        code for code in get_args(ProcedureAdmissionRefusalCodeV1) if code not in produced
    )
    assert missing == []


def test_every_settlement_refusal_member_is_produced_in_production_source() -> None:
    produced = _produced_strings()
    missing = sorted(
        code for code in get_args(ProcedureSettlementRefusalCodeV1) if code not in produced
    )
    assert missing == []


# Members frozen into v1 with no production producer today. The inventory is
# pinned exactly, so a new member cannot join the closed vocabulary without
# either a producer or a recorded decision, and it can only shrink. It is a
# debt, not a licence: `cache_integrity` is deliberately absent from it.
INTERNAL_FAILURE_WITHOUT_PRODUCTION_PRODUCER = frozenset(
    {
        "provider_refusal_taxonomy_unknown",
        "unknown_manifest_field",
        "manifest_divergence",
        "interface_digest_mismatch",
        "bucket_fixture_missing",
        "invalid_bucket_vocabulary",
        "unknown_run_context_field",
        "lock_mismatch",
        "lock_missing_hash",
        "lock_ambiguous_fork",
        "index_not_pinned",
        "index_redirect",
        "non_finite_output",
        "non_finite_result",
        "image_provenance_mismatch",
    }
)
OPERATIONAL_FAILURE_WITHOUT_PRODUCTION_PRODUCER = frozenset(
    {
        "journal_read_failed",
        "unresolvable_source",
        "air_gapped_cache_miss",
        "network_disabled",
        "cache_permissions",
        "unresolved_secret_ref",
    }
)


def test_internal_failure_producer_debt_is_pinned_and_can_only_shrink() -> None:
    """U6 de-emitted `cache_integrity` inside the batch that froze it."""

    produced = _produced_strings()
    missing = frozenset(
        code for code in get_args(ProcedureInternalFailureCodeV1) if code not in produced
    )
    assert missing == INTERNAL_FAILURE_WITHOUT_PRODUCTION_PRODUCER
    assert "cache_integrity" not in INTERNAL_FAILURE_WITHOUT_PRODUCTION_PRODUCER


def test_operational_failure_producer_debt_is_pinned_and_can_only_shrink() -> None:
    produced = _produced_strings()
    missing = frozenset(
        code for code in get_args(ProcedureOperationalFailureCodeV1) if code not in produced
    )
    assert missing == OPERATIONAL_FAILURE_WITHOUT_PRODUCTION_PRODUCER
