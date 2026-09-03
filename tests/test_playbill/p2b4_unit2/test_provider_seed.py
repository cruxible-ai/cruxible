from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from tests.test_playbill._support import generate_client, initialize_local

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    ProviderLocalDistributionPinV1,
    ProviderRuntimeArtifactPayloadV1,
    ProviderV2,
    parse_provider,
    provider_digest,
    provider_expected_implementation_records,
    provider_path,
    render_provider,
)
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.seed_artifacts.workspace_file import (
    WORKSPACE_FILE_FIXTURES,
    WORKSPACE_FILE_IMPLEMENTATION_DIGEST,
    WORKSPACE_FILE_INTERFACE_DIGEST,
    WORKSPACE_FILE_INTERFACE_ID,
    WORKSPACE_FILE_PROTOCOL_FIXTURE_DIGEST,
    WORKSPACE_FILE_PROVIDER_ID,
    WORKSPACE_FILE_SEED_MANIFEST,
    WorkspaceFileBucketClassifier,
    workspace_file_interface_registration,
    workspace_file_provider,
)
from cruxible_core.playbill.service.documents import service_activate_playbill_proposal
from cruxible_core.playbill.service.provider_seed import service_seed_workspace_file_provider
from cruxible_core.runtime.provider_runtime import ProviderSeedMaterializationConfigV1

STAMP_1 = "2026-09-02T12:00:00.000000Z"
STAMP_2 = "2026-09-02T12:00:01.000000Z"
STAMP_3 = "2026-09-02T12:00:02.000000Z"


def test_seed_exactly_pins_the_provider_owned_inputs_and_core_double() -> None:
    fixture_bytes = Path("tests/fixtures/provider_runtime_contract_v1.json").read_bytes()
    assert f"sha256:{hashlib.sha256(fixture_bytes).hexdigest()}" == (
        WORKSPACE_FILE_PROTOCOL_FIXTURE_DIGEST
    )

    registration = workspace_file_interface_registration()
    interface_artifact_digest = provider_interface_digest(registration).tagged
    provider = workspace_file_provider(interface_artifact_digest=interface_artifact_digest)

    assert registration.interface_digest == WORKSPACE_FILE_INTERFACE_DIGEST
    assert provider.implementations[0].implementation_digest == (
        WORKSPACE_FILE_IMPLEMENTATION_DIGEST
    )
    assert provider.signing_keys == ()
    assert isinstance(provider.runtime_artifact.distribution, ProviderLocalDistributionPinV1)
    distribution = provider.runtime_artifact.distribution.model_dump(mode="json")
    assert distribution["materialization_source"] == "local"
    assert "url" not in distribution and "index_url" not in distribution
    assert provider.runtime_artifact.local_env is not None
    assert tuple(provider.runtime_artifact.local_env.materialization_digests.items()) == (
        WORKSPACE_FILE_SEED_MANIFEST.materialization_digests
    )

    accepted = AcceptedProviderInterfaceRegistrationV1(
        path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID),
        registration=registration,
        artifact_digest=interface_artifact_digest,
    )
    registry = ProviderBucketClassifierRegistry()
    installed = registry.install(accepted, WorkspaceFileBucketClassifier())
    assert len(installed.results) == 6
    for fixture in WORKSPACE_FILE_FIXTURES:
        assert (
            registry.require(registration.classifier_digest).classify(
                fixture.canonical_input  # type: ignore[arg-type]
            )
            == fixture.measured_bucket_id
        )


def test_seed_is_an_idempotent_ordinary_proposal_and_generation_one(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    assert instance.accepted_history()[-1].sequence == 0

    seeded = service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=STAMP_1,
    )

    assert seeded.status == "activated"
    assert seeded.changed_paths == (
        "provider-interfaces/workspace.file.json",
        "providers/cruxible-provider-workspace.json",
    )
    assert instance.accepted_history()[-1].sequence == 1
    assert seeded.proposal_id is not None
    candidate = instance.proposal_evidence().read_candidate(seeded.candidate_digest or "")
    assert tuple(member.path for member in candidate.members) == seeded.changed_paths

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    registration = parse_provider_interface(
        tree[provider_interface_path(WORKSPACE_FILE_INTERFACE_ID)],
        path=provider_interface_path(WORKSPACE_FILE_INTERFACE_ID),
    )
    provider = parse_provider(
        tree[provider_path(WORKSPACE_FILE_PROVIDER_ID)],
        path=provider_path(WORKSPACE_FILE_PROVIDER_ID),
    )
    assert provider.pins[0].artifact_digest == provider_interface_digest(registration).tagged
    assert str(tmp_path).encode() not in b"".join(tree.values())

    repeated = service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=STAMP_2,
    )
    assert repeated.status == "already_current"
    assert repeated.proposal_id is None
    assert instance.accepted_history()[-1].sequence == 1


def test_independent_policy_retains_the_seed_candidate(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    from cruxible_core.playbill.instance import PlaybillInstance

    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_provider_seed_independent",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        require_independent_approval=True,
        timestamp="2026-09-02T11:00:00+00:00",
    )

    seeded = service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=STAMP_1,
    )

    assert seeded.status == "proposed"
    assert seeded.approval_required is True
    assert seeded.proposal_id is not None
    assert instance.accepted_history()[-1].sequence == 0
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert provider_path(WORKSPACE_FILE_PROVIDER_ID) not in tree


def test_local_checkout_is_operational_and_must_reproduce_seed_pins(tmp_path: Path) -> None:
    checkout = (tmp_path / "providers-checkout").resolve()
    checkout.mkdir()
    configured = ProviderSeedMaterializationConfigV1(
        provider_id=WORKSPACE_FILE_PROVIDER_ID,
        checkout_path=str(checkout),
        provider_commit=WORKSPACE_FILE_SEED_MANIFEST.provider_commit,
        environment_pin_key=WORKSPACE_FILE_SEED_MANIFEST.materialization_digests[0][0],
        materialization_digest=WORKSPACE_FILE_SEED_MANIFEST.materialization_digests[0][1],
    )
    instance, _owner = initialize_local(tmp_path)

    with pytest.raises(
        ProposalIntegrityError,
        match="cannot reproduce its local materialization",
    ):
        service_seed_workspace_file_provider(
            instance,
            actor_id="owner",
            timestamp=STAMP_1,
            configured_materialization=configured,
        )

    assert instance.accepted_history()[-1].sequence == 0


def test_repin_is_a_provider_successor_not_a_compiler_bypass(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    first = service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=STAMP_1,
    )
    assert first.status == "activated"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    artifact_path = provider_path(WORKSPACE_FILE_PROVIDER_ID)
    current = parse_provider(tree[artifact_path], path=artifact_path)
    assert isinstance(current, ProviderV2)
    payload_data = current.runtime_artifact.model_dump(mode="json")
    payload_data["distribution"]["sha256"] = f"sha256:{'1' * 64}"  # type: ignore[index]
    changed_payload = ProviderRuntimeArtifactPayloadV1.model_validate(payload_data)
    changed_data = current.model_dump(mode="json")
    changed_data["runtime_artifact"] = changed_payload.model_dump(mode="json")
    changed_data["implementations"] = [
        item.model_dump(mode="json")
        for item in provider_expected_implementation_records(changed_payload)
    ]
    changed_data["lifecycle"] = ArtifactLifecycle(
        predecessor_digest=provider_digest(current).tagged
    ).model_dump(mode="json")
    changed = ProviderV2.model_validate(changed_data)
    changed_tree = dict(tree)
    changed_tree[artifact_path] = render_provider(changed)
    base = instance.accepted_coordinate()
    proposal = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/provider-repin-probe",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=changed_tree,
        timestamp=STAMP_2,
    )
    assert proposal.evaluation.verdict == "candidate"
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"

    restored = service_seed_workspace_file_provider(
        instance,
        actor_id="owner",
        timestamp=STAMP_3,
    )
    assert restored.status == "activated"
    assert restored.changed_paths == (artifact_path,)
    restored_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    restored_provider = parse_provider(restored_tree[artifact_path], path=artifact_path)
    assert restored_provider.lifecycle.predecessor_digest == provider_digest(changed).tagged
    assert isinstance(restored_provider, ProviderV2)
    assert restored_provider.implementations[0].implementation_digest == (
        WORKSPACE_FILE_IMPLEMENTATION_DIGEST
    )
