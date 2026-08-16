from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.captures import evaluate_capture_contract_law, verify_capture
from cruxible_core.playbill.providers import provider_digest
from cruxible_core.playbill.source_readers import (
    ExternalSourceReadRequestV1,
    FakeVersionedExternalSourceReader,
    ProducerBindingV1,
)
from tests.test_playbill._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


def test_general_contract_and_exact_external_capture_do_not_copy_a_table(tmp_path: Path) -> None:
    contract = capture_contract()
    accepted = evaluate_capture_contract_law(
        contract,
        path="capture-contracts/test.orders-v1.yaml",
        actor_roles=("owner",),
        predecessor=None,
    )
    assert accepted.verdict == "accepted"
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "postgres-v1"),
    )
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="0/16B6C50",
        selector_type="relation-primary-key-v1",
        selector={"relation": "orders", "key": {"order_id": "ord-482"}},
        value={"order_id": "ord-482", "status": "released"},
    )
    store = body_store(tmp_path)
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate="0/16B6C50",
            selector_type="relation-primary-key-v1",
            selector={"relation": "orders", "key": {"order_id": "ord-482"}},
            materialization="external",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=store,
    )
    assert result.envelope.commitment.materialization == "external"
    assert result.canonical_material is None
    assert store.verify(result.capture_digest)
    assert not store.verify(result.envelope.commitment.digest)
    verified = verify_capture(
        result.capture_digest,
        store=store,
        contract=contract,
        producer_artifact_digests={
            provider_artifact.identity.qualified: provider_digest(provider_artifact).tagged
        },
    )
    assert verified.source == result.envelope.source


def test_attested_only_acquisition_remains_honest_when_source_cannot_replay(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "api-v1"),
    )
    reader = FakeVersionedExternalSourceReader()
    selector = {"relation": "orders", "key": {"order_id": "ord-latest"}}
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="latest",
        selector_type="relation-primary-key-v1",
        selector=selector,
        value={"order_id": "ord-latest", "status": "pending"},
        replayability="attested_only",
    )
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate="latest",
            selector_type="relation-primary-key-v1",
            selector=selector,
            materialization="none",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=body_store(tmp_path),
    )
    assert result.envelope.source.replayability == "attested_only"  # type: ignore[union-attr]
    assert result.envelope.commitment.materialization == "none"
    assert not reader.replay_available(result.envelope.source)  # type: ignore[arg-type]


def test_external_coordinates_refuse_secrets_and_unpinned_schemas() -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "postgres-v1"),
    )
    payload = {
        "contract": contract,
        "provider": provider_artifact,
        "binding": binding,
        "coordinate_type": "postgres-lsn-v1",
        "coordinate": {"connection_string": "postgres://secret"},
        "selector_type": "relation-primary-key-v1",
        "selector": {"id": "1"},
        "materialization": "external",
        "run_coordinate": provider_run(provider_artifact),
        "observed_at": NOW,
        "resource_budget": contract.selection_budget,
    }
    with pytest.raises(ValidationError, match="secret-bearing|locators"):
        ExternalSourceReadRequestV1.model_validate(payload)
    payload["coordinate"] = "0/1"
    payload["coordinate_type"] = "unregistered-v1"
    with pytest.raises(ValidationError, match="not pinned"):
        ExternalSourceReadRequestV1.model_validate(payload)
