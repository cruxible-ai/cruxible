"""Shared exact PC-C contract fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import ArtifactDigest, Sha256Value, typed_digest
from cruxible_core.playbill.captures import (
    CaptureContractV1,
    CaptureRetentionErasurePolicyV1,
    CaptureRunCoordinateV1,
    CaptureSelectionBudgetV1,
    capture_contract_digest,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.providers import ProviderSigningKeyV1, ProviderV1, provider_digest

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def digest(domain: str, value: str) -> str:
    return typed_digest(Sha256Value, domain, {"value": value}).tagged


def artifact_digest(domain: str, value: str) -> str:
    return typed_digest(ArtifactDigest, domain, {"value": value}).tagged


def pin(role: str, name: str, *, digest_value: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind="Contract", name=name),
        artifact_digest=digest_value or artifact_digest("test-contract", name),
    )


def capture_contract(
    *,
    name: str = "test.orders-v1",
    epistemic_grade: str = "observed",
    selector_privacy: str = "direct_allowed",
    erasure: bool = False,
) -> CaptureContractV1:
    replay = artifact_digest("test-rule", f"{name}:replay")
    provenance = artifact_digest("test-rule", f"{name}:provenance")
    mapping = artifact_digest("test-rule", f"{name}:mapping")
    erasure_rule = artifact_digest("test-rule", f"{name}:erasure") if erasure else None
    registry_pins = [
        pin("provenance-rule", f"{name}.provenance", digest_value=provenance),
        pin("replay-policy", f"{name}.replay", digest_value=replay),
        pin("source-subject-mapping", f"{name}.mapping", digest_value=mapping),
    ]
    if erasure_rule is not None:
        registry_pins.append(pin("erasure-rule", f"{name}.erasure", digest_value=erasure_rule))
    return CaptureContractV1(
        identity=ArtifactIdentity(kind="CaptureContract", name=name),
        allowed_source_kinds=("cas", "external", "ledger"),
        logical_source_identities=("commerce.production.orders",),
        coordinate_schema_pins=(pin("coordinate-schema", "postgres-lsn-v1"),),
        selector_schema_pins=(pin("selector-schema", "relation-primary-key-v1"),),
        commitment_canonicalizer=pin("commitment-canonicalizer", "canonical-json-v1"),
        allowed_materialization_modes=("cas", "external", "ledger", "none"),
        selection_budget=CaptureSelectionBudgetV1(
            max_bytes=4096,
            max_rows=4,
            max_items=4,
        ),
        retention_erasure_policy=CaptureRetentionErasurePolicyV1(
            body_retention="optional",
            erasure="authorized_by_rule" if erasure else "prohibited",
            erasure_rule_digest=erasure_rule,
            selector_privacy=selector_privacy,
        ),
        replay_policy_digest=replay,
        epistemic_grade=epistemic_grade,  # type: ignore[arg-type]
        provenance_rule_digest=provenance,
        evidence_kinds=("database_record",),
        source_subject_mapping_digest=mapping,
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
        pins=tuple(sorted(registry_pins, key=lambda item: (item.role, item.target.qualified))),
    )


def provider(
    contract: CaptureContractV1,
    *,
    public_key: str = "11" * 32,
    name: str = "acme.orders",
    control_domain: str = "acme",
) -> ProviderV1:
    return ProviderV1(
        identity=ArtifactIdentity(kind="Provider", name=name),
        control_domain=control_domain,
        signing_keys=(
            ProviderSigningKeyV1(
                key_id="primary-2026",
                public_key=public_key,
                valid_from=NOW,
            ),
        ),
        capture_contract_digests=(capture_contract_digest(contract).tagged,),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
    )


def provider_run(
    provider_artifact: ProviderV1, *, run_id: str = "provider-run-1"
) -> CaptureRunCoordinateV1:
    return CaptureRunCoordinateV1(
        run_kind="provider",
        run_id=run_id,
        bound_generation=digest("test-generation", "g1"),
        executable_identity=provider_artifact.identity,
        executable_digest=provider_digest(provider_artifact).tagged,
    )


def body_store(tmp_path: Path) -> ContentAddressedBodyStore:
    root = tmp_path / "cas"
    root.mkdir(parents=True)
    return ContentAddressedBodyStore(root)


__all__ = [
    "NOW",
    "artifact_digest",
    "body_store",
    "capture_contract",
    "digest",
    "pin",
    "provider",
    "provider_run",
]
