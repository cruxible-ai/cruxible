"""Round-3 next rows: expiring leads, the cited contract first, every required rebind operand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.claim_verdicts import EvidenceFreshnessExpiration
from cruxible_core.service.discovery.next import (
    DEFAULT_EXPIRING_WITHIN_MICROSECONDS,
    _cited_first,
    _expiring_lead,
    _repair_command,
)

CLAIM = "CLM-" + "a" * 32
OBSERVED = datetime(2026, 10, 1, tzinfo=UTC)


def _expiration(horizon: timedelta, *, reobserved: timedelta | None = None):  # type: ignore[no-untyped-def]
    return EvidenceFreshnessExpiration(
        capture_digest="sha256:" + "d" * 64,
        observed_at=OBSERVED,
        expires_at=OBSERVED + (reobserved or timedelta()) + horizon,
        **(
            {}
            if reobserved is None
            else {"reobserved_by": "sha256:" + "e" * 64, "reobserved_at": OBSERVED + reobserved}
        ),
    )


def test_a_short_horizon_is_expiring_only_in_its_last_quarter() -> None:
    week = CanonicalDuration(microseconds=DEFAULT_EXPIRING_WITHIN_MICROSECONDS)
    # A 12 h horizon under the default 7 d lead warns for its last 3 h, not always.
    assert _expiring_lead(_expiration(timedelta(hours=12)), week) == timedelta(hours=3)
    # A long horizon keeps the requested lead.
    assert _expiring_lead(_expiration(timedelta(days=90)), week) == timedelta(days=7)
    # A re-observation restarts the horizon it is measured over.
    assert _expiring_lead(
        _expiration(timedelta(hours=8), reobserved=timedelta(days=30)), week
    ) == timedelta(hours=2)


def test_the_cited_captures_contracts_come_first() -> None:
    admitted = ("security.pypi-release-v1", "security.runtime-inventory-v1")
    assert _cited_first(admitted, ("security.runtime-inventory-v1",)) == (
        "security.runtime-inventory-v1",
        "security.pypi-release-v1",
    )
    # A cited contract the role does not admit is not offered.
    assert _cited_first(admitted, ("other.contract",)) == admitted


def test_rebind_and_retire_repairs_show_every_required_operand() -> None:
    erased = {"claim_id": CLAIM, "citation": "e" * 12}
    operands = ("capture", "because")
    assert _repair_command("cruxible.rebind", arguments=erased, operands=operands) == (
        f"cruxible rebind {CLAIM} --citation {'e' * 12} --capture CAP --because TEXT"
    )
    assert _repair_command(
        "cruxible.rebind", arguments=erased, operands=operands, surface="sdk"
    ) == (f'cx.rebind("{CLAIM}", citation="{"e" * 12}", capture="CAP", because="TEXT")')
    mcp = _repair_command("cruxible.rebind", arguments=erased, operands=operands, surface="mcp")
    assert mcp == (
        'cruxible_write(changes=[{"op": "rebind", "target": "'
        + CLAIM
        + '", "citation": "'
        + "e" * 12
        + '", "evidence": {"kind": "capture", "capture": "CAP"}}], because="TEXT")'
    )
    file_drift = _repair_command("cruxible.rebind", arguments=erased, operands=("file", "because"))
    assert file_drift == (
        f"cruxible rebind {CLAIM} --citation {'e' * 12} --file FILE#ANCHOR --because TEXT"
    )
    retire = {"claim_id": CLAIM}
    assert _repair_command("cruxible.claim.retire", arguments=retire, operands=("because",)) == (
        f"cruxible retire {CLAIM} --because TEXT"
    )
    assert _repair_command(
        "cruxible.claim.retire", arguments=retire, operands=("because",), surface="mcp"
    ) == (f'cruxible_retire(target="{CLAIM}", because="TEXT")')


def test_a_claim_that_cannot_resolve_reads_ineligible_not_refused() -> None:
    from typing import get_args

    from cruxible_client.contracts.compact_query import QueryCellClaimStatus, QueryClaimStatus
    from cruxible_client.contracts.orient import OrientClaimCounts
    from cruxible_core.service.discovery.claim_status import ClaimStatus

    for vocabulary in (ClaimStatus, QueryClaimStatus, QueryCellClaimStatus):
        assert "ineligible" in get_args(vocabulary) and "refused" not in get_args(vocabulary)
    assert "ineligible" in OrientClaimCounts.model_fields
    assert "refused" not in OrientClaimCounts.model_fields
