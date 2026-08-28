"""PC-B coordinate-bound expand/open-source behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.discovery import ExpandRequestV1, ExpansionBudgetV1
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import OpenSourceRequestV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    service_expand_playbill_semantic,
    service_explain_playbill_claim,
    service_open_playbill_source,
)
from tests.test_playbill._claim_authoring_support import (
    TIMESTAMP,
    _activate_direct_claim,
    _authoring,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import initialize_local


def test_expand_is_coordinate_bound_and_open_source_enforces_access_and_budget(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="semantic-read",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, proposed)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    capsule = service_expand_playbill_semantic(
        instance,
        request=ExpandRequestV1(
            address=SemanticAddress.claim_statement(proposed.claim_path),
            at=accepted,
            evaluation_time=TIMESTAMP,
            facets=("claim_context", "governance", "provenance", "sources", "summary"),
            budget=ExpansionBudgetV1(max_bytes=2048, max_relations=0, max_source_handles=1),
        ),
    )
    assert capsule.at == accepted
    assert capsule.coverage.requested_facets[-1] == "summary"

    foreign = accepted.model_copy(update={"semantic_root": "sha256:" + "ff" * 32})
    with pytest.raises(PlaybillFormatError):
        service_expand_playbill_semantic(
            instance,
            request=ExpandRequestV1(
                address=SemanticAddress.claim_statement(proposed.claim_path),
                at=foreign,
                evaluation_time=TIMESTAMP,
                facets=("summary",),
            ),
        )

    handle = service_explain_playbill_claim(
        instance,
        identity=proposed.claim_identity,
    ).source_handles[0]
    denied = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=4096),
        access=BodyAccessContext(principal_id="reader", can_read_body=False),
    )
    assert denied.status == "denied"
    truncated = service_open_playbill_source(
        instance,
        request=OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=1),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert truncated.status == "unavailable"
    assert truncated.coverage.reason_codes == ("resource_budget_exceeded",)
