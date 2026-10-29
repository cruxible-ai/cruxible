"""The compiler writes v4 identity evidence policies; v3 ones are only the genesis seeds.

A fresh instance's only v3-policy ClaimTypes are its unedited genesis seeds. They
keep working as they are and are not pending work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicyV3
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claim_type_upgrade import service_upgrade_claim_types
from cruxible_core.service.discovery.next import service_playbill_next
from tests.core_support._support import initialize_local
from tests.test_claims.test_claims import TIMESTAMP
from tests.test_integration.test_next_closed_loop import _request

SEEDS = ("semantic.alias", "semantic.distinct_from")


def _upgrade_claim_types(instance: PlaybillInstance, **request: Any) -> ClaimTypeUpgradeResult:
    return service_upgrade_claim_types(
        instance,
        request=ClaimTypeUpgradeRequest.model_validate(request),
        actor_id="owner",
        timestamp=TIMESTAMP,
    )


def test_the_genesis_seeds_keep_v3_policies_and_pend_nothing(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for predicate in SEEDS:
        path = claim_type_path(predicate)
        policy = parse_claim_type(tree[path], path=path).evidence_admission_policy
        assert isinstance(policy, ClaimEvidenceAdmissionPolicyV3)
    # The unedited genesis seeds are not pending work of a new instance.
    status = service_playbill_next(instance, request=_request(instance)).status
    assert status.compiler.state == "current"
