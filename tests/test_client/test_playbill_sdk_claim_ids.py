"""World-qualified Claim identities compose with revision authoring."""

from pathlib import Path

import pytest

from cruxible_client import ClaimRef, ClaimRole, Disposition, Playbill
from tests.test_client.test_playbill_sdk import _Client, _workspace


@pytest.fixture
def pb(tmp_path: Path):
    _workspace(tmp_path)
    return Playbill._from_client(_Client(), instance_id="inst_test", workspace=tmp_path)


def draft(pb, revises, dispositions):
    return pb.claim(
        subject="sec.vuln/cve-2026-0001",
        predicate="sec.vuln.affects_package",
        value="sec.package/demo",
        role=ClaimRole.OBSERVATION,
        rationale="Repair observed value.",
        supported_by=None,
        copied_from=None,
        self_source="affected package",
        qualifier=None,
        effective_period=None,
        revises=revises,
        dispositions=dispositions,
        subject_definition=None,
        claim_type_definition=None,
    )


@pytest.mark.parametrize("typed", [False, True])
def test_bare_and_qualified_revision_have_equal_payload_and_typed_assertions(pb, typed):
    claim_id = "CLM-" + "a" * 32
    bare = ClaimRef(claim_id, pb.coordinate) if typed else claim_id
    qualified = ClaimRef("Claim:" + claim_id, pb.coordinate) if typed else "Claim:" + claim_id
    first = draft(pb, bare, {bare: Disposition.CONTRADICT})
    second = draft(pb, qualified, {qualified: Disposition.CONTRADICT})
    assert first.payload == second.payload
    assert first.payload.claim_ref == claim_id
    assert first.payload.existing_claim_dispositions[0].claim_id == claim_id
    assert first.reference_expectations == second.reference_expectations
    assert len(first.reference_expectations) == (2 if typed else 0)
    assert first.program_stamp == second.program_stamp


def test_duplicate_normalized_dispositions_are_refused(pb):
    claim_id = "CLM-" + "a" * 32
    with pytest.raises(ValueError, match="duplicate normalized Claim disposition"):
        draft(
            pb,
            claim_id,
            {claim_id: Disposition.CONTRADICT, "Claim:" + claim_id: Disposition.CONTRADICT},
        )
