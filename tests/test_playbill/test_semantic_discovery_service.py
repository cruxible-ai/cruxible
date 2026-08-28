"""PC-G-S1a accepted-state semantic discovery over the governed naming layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.discovery import DiscoveryBudgetV1
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.query.semantic_discovery import DiscoveryError
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
)
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_discovery import (
    PlaybillInterfaceInventoryV1,
    build_accepted_discovery_vocabulary,
    service_discover_playbill_semantic,
)
from tests.test_playbill._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._pc_c_support import capture_contract, provider
from tests.test_playbill._support import initialize_local


def _instance_with_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection, sequence=3)
    return instance, owner


def test_vocabulary_covers_accepted_subjects_claim_types_and_queries(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    vocabulary = build_accepted_discovery_vocabulary(
        instance,
        coordinate=instance.accepted_coordinate(),
    )

    kinds = {entry.kind for entry in vocabulary.entries}
    assert {"Subject", "ClaimType", "QueryDefinition"} <= kinds
    assert vocabulary.at.git_oid == instance.accepted_coordinate().git_oid
    assert tuple(item.entrypoint_name for item in vocabulary.entrypoints()) == (QUERY_NAME,)


def test_lexical_query_finds_the_claim_type_that_names_the_term(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        query="status",
        evaluation_time=EVALUATION_TIME,
        profile="interfaces",
    )

    addresses = {hit.address.artifact_path for hit in result.page.hits}
    assert claim_type_path(PREDICATE) in addresses
    assert result.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert result.page.coordinate_kind == "accepted"
    assert result.page.receipt_digest.startswith("sha256:")


def test_entrypoint_selection_resolves_the_accepted_query_definition(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        entrypoint=QUERY_NAME,
        evaluation_time=EVALUATION_TIME,
        profile="all",
    )

    assert [(hit.kind, hit.label) for hit in result.page.hits] == [("QueryDefinition", QUERY_NAME)]


def test_discovery_page_is_byte_stable_for_one_coordinate_and_request(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    first = service_discover_playbill_semantic(
        instance, query="work_item", evaluation_time=EVALUATION_TIME, profile="all"
    )
    second = service_discover_playbill_semantic(
        instance, query="work_item", evaluation_time=EVALUATION_TIME, profile="all"
    )

    assert first.page == second.page


def test_budget_clipping_is_stated_rather_than_silently_narrowing(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        query="work_item",
        evaluation_time=EVALUATION_TIME,
        profile="all",
        budget=DiscoveryBudgetV1(max_hits=1),
    )

    assert len(result.page.hits) == 1
    assert result.page.coverage.truncated_facets == ("hits",)
    assert "hit_budget_exceeded" in result.page.coverage.reason_codes


def test_empty_interfaces_request_returns_an_honest_not_installed_inventory(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)

    result = service_discover_playbill_semantic(
        instance,
        evaluation_time=EVALUATION_TIME,
    )

    assert isinstance(result, PlaybillInterfaceInventoryV1)
    assert result.provider_status == "not_installed"
    assert result.interfaces == ()


def test_other_empty_discovery_profiles_remain_refused(tmp_path: Path) -> None:
    """Still refused, now as a typed refusal rather than a raw model error.

    The refusal used to escape from DiscoveryRequestV1's own validator, which
    is not a CoreError, so over HTTP it reached the caller as an opaque 500.
    """
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(PlaybillFormatError, match="needs a query or an entrypoint"):
        service_discover_playbill_semantic(
            instance,
            evaluation_time=EVALUATION_TIME,
            profile="subjects",
        )


def test_interfaces_inventory_uses_the_linespec_interface_pin_projection(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    contract = capture_contract()
    contract_digest = capture_contract_digest(contract).tagged
    provider_artifact = provider(contract).model_copy(
        update={
            "pins": tuple(
                sorted(
                    (
                        *provider(contract).pins,
                        ArtifactPin(
                            role="interface",
                            target=contract.identity,
                            artifact_digest=contract_digest,
                        ),
                    ),
                    key=lambda item: (item.role, item.target.qualified),
                )
            )
        }
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/provider-interface",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree={
            **instance.tree_at(base.git_oid),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            provider_path(provider_artifact.identity.name): render_provider(provider_artifact),
        },
        timestamp=TIMESTAMP,
    )
    accept_proposal(
        instance,
        owner,
        PlaybillProposalInspection(
            proposal=proposed,
            accepted_coordinate=PlaybillAcceptedCoordinate.from_internal(base),
        ),
        sequence=1,
    )

    result = service_discover_playbill_semantic(
        instance,
        evaluation_time=EVALUATION_TIME,
    )

    assert isinstance(result, PlaybillInterfaceInventoryV1)
    assert result.provider_status == "installed"
    assert result.interfaces[0].identity == provider_artifact.identity.qualified
    assert result.interfaces[0].artifact_digest == provider_digest(provider_artifact).tagged
    assert result.interfaces[0].interface_digest == contract_digest
    assert result.interfaces[0].interface_basis == "explicit_interface_pin"


def test_blank_query_is_refused_rather_than_listing_everything(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    with pytest.raises(DiscoveryError, match="blank discovery query"):
        service_discover_playbill_semantic(instance, query="   ", evaluation_time=EVALUATION_TIME)


def test_discovery_is_pinned_to_the_requested_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    result = service_discover_playbill_semantic(
        instance,
        query="status",
        evaluation_time=EVALUATION_TIME,
        at=accepted,
    )

    assert result.coordinate == accepted
    assert result.page.at == accepted
