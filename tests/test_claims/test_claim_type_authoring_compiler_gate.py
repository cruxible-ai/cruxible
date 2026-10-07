"""ClaimType authoring writes v7 with identity rules, or refuses toward a compiler upgrade.

A compiler before revision 31 admits neither ClaimType v7 nor identity evidence
rules. Authoring there used to fall back to a v5 exact-digest ClaimType; it now
refuses with a typed repair naming `cruxible compiler upgrade`, on both tagless
roads: a ClaimType input proposal and a tagless succession.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeRequiresCompilerUpgrade,
    identity_rules_supported,
    lower_claim_type_input,
)
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeMigrationRequest,
    service_migrate_claim_type,
)
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    TRIGGER_CAPTURE_COMPILER,
    current_compiler_coordinate,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.server.errors import error_to_response
from cruxible_core.service.claims.claim_types import service_propose_playbill_claim_type_input
from tests.core_support._claim_type_support import defaulted_claim_type_input_example
from tests.test_ledger.test_activation import TIMESTAMP
from tests.test_ledger.test_compiler_upgrade import old_instance


def _assert_upgrade_refusal(refusal: ClaimTypeRequiresCompilerUpgrade) -> None:
    assert refusal.error_code == "cruxible.claim_type.compiler_upgrade_required"
    assert refusal.repair == RepairOperation(
        operation="cruxible.compiler.upgrade",
        arguments={
            "to": current_compiler_coordinate().rule_digest,
            "name": "upgrade-for-claim-type-v7",
        },
    )
    status, response = error_to_response(refusal)
    assert status == 400
    assert response.error_code == "cruxible.claim_type.compiler_upgrade_required"
    assert response.repair == refusal.repair


def test_only_revision_31_and_later_support_identity_rules() -> None:
    assert identity_rules_supported(AUTHORITY_VERBS_COMPILER)
    assert identity_rules_supported(GOVERNED_TRIGGERS_COMPILER)
    assert not identity_rules_supported(TRIGGER_CAPTURE_COMPILER)


def test_an_input_proposal_on_an_older_compiler_refuses_instead_of_writing_v5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, _reviewer = old_instance(
        tmp_path, monkeypatch, compiler=TRIGGER_CAPTURE_COMPILER
    )
    assert instance.accepted_coordinate().compiler == TRIGGER_CAPTURE_COMPILER
    before = instance.accepted_coordinate()

    with pytest.raises(ClaimTypeRequiresCompilerUpgrade) as refused:
        service_propose_playbill_claim_type_input(
            instance,
            input=defaulted_claim_type_input_example(),
            actor_id="owner",
            proposal_name="older-compiler",
            timestamp=TIMESTAMP,
            dry_run=False,
        )

    _assert_upgrade_refusal(refused.value)
    assert instance.accepted_coordinate() == before


def test_a_tagless_succession_on_an_older_compiler_refuses_instead_of_writing_v5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, _reviewer = old_instance(
        tmp_path, monkeypatch, compiler=TRIGGER_CAPTURE_COMPILER
    )

    with pytest.raises(ClaimTypeRequiresCompilerUpgrade) as refused:
        service_migrate_claim_type(
            instance,
            request=ClaimTypeMigrationRequest(
                mode="preflight", successor=defaulted_claim_type_input_example()
            ),
            actor=AuthenticatedActor(actor_id="owner"),
        )

    _assert_upgrade_refusal(refused.value)


def test_lowering_writes_v7_with_identity_rules_where_supported() -> None:
    lowered = lower_claim_type_input(
        defaulted_claim_type_input_example(), tree={}, identity_rules=True
    )

    assert lowered.artifact_format == "playbill-claim-type-v7"
    assert all(rule.capture_contracts for rule in lowered.evidence_admission_policy.rules)
    with pytest.raises(ClaimTypeRequiresCompilerUpgrade):
        lower_claim_type_input(defaulted_claim_type_input_example(), tree={})
