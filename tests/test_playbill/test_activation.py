"""PB-D change-set, settlement binding, signed generation, and root tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    ApprovalSubmission,
    approval_statement_bytes,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_client.contracts.types import GenerationDescriptor, PlaybillTrustRoot
from cruxible_core.playbill.activation import ActivationPublisher
from cruxible_core.playbill.bootstrap import generation_root, prepare_genesis
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.compiler import current_compiler_coordinate
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import (
    ALLOWED_SIGNERS_FILE,
    GeneratedKeyMaterial,
    generate_daemon_key,
)
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.serving import SERVING_MANIFEST_FILE, bind_current_projection
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    SettlementBinding,
    change_set_digest,
    compute_semantic_root,
    prepare_generation,
)
from cruxible_core.playbill.witness import WitnessRecord

from ._support import FIXED_TIMESTAMP, generate_client

TIMESTAMP = "2026-08-12T14:00:00.000000Z"
DOCUMENT_PATH = "documents/design.yaml"


def _instance(tmp_path: Path):
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
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_activation",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner, reviewer


def _candidate(
    instance: PlaybillInstance,
    *,
    title: str = "Accepted design",
    body_content: bytes = b"# Accepted design\n",
):
    body = instance.store_document_body(body_content)
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title=title,
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    base = instance.accepted_coordinate()
    tree = {**instance._ledger.read_tree(base.git_oid), DOCUMENT_PATH: render_document(shell)}
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/design",
            proposed_base_oid=base.git_oid,
            source_compilation_digest="sha256:" + "77" * 32,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    assert result.candidate is not None
    return base, tree, result.candidate


class MemoryWitness:
    def __init__(self) -> None:
        self.records: list[WitnessRecord] = []

    def publish(self, record: WitnessRecord) -> None:
        self.records.append(record)

    def latest(self, instance_id: str) -> WitnessRecord | None:
        matches = [record for record in self.records if record.instance_id == instance_id]
        return matches[-1] if matches else None


def _sign(
    material: GeneratedKeyMaterial,
    candidate_digest: str,
    parent_root: str,
) -> ApprovalSubmission:
    private = serialization.load_ssh_private_key(
        material.private_key_path.read_bytes(),
        password=None,
    )
    assert isinstance(private, Ed25519PrivateKey)
    statement = ApprovalStatement(
        signer_id=material.principal.principal_id,
        signing_semantic_root=parent_root,
        payload_digest=candidate_digest,
    )
    return ApprovalSubmission(
        submitted_by="approval-relay",
        attestation=ApprovalAttestation(
            **statement.model_dump(),
            sig=private.sign(approval_statement_bytes(statement)).hex(),
        ),
    )


def test_prepare_generation_binds_complete_change_set_without_advancing_main(
    tmp_path: Path,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)

    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=(),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(
            actor_id="owner",
            source_compilation_digest="sha256:" + "77" * 32,
        ),
        proposal_actor_id="owner",
        sequence=1,
    )

    assert instance._ledger.read_main() == base.git_oid
    assert bundle.settlement.c_s_digest == candidate.candidate_digest
    assert bundle.settlement.base_oid == base.git_oid
    assert instance._ledger.parent_of(bundle.oid) == base.git_oid
    assert instance._ledger.verify_commit(bundle.oid)
    assert bundle.record.candidate == candidate.candidate
    assert bundle.record.candidate_digest == candidate.candidate_digest
    assert bundle.record.approval_requirements == ()
    assert bundle.record.approvals == ()
    assert bundle.approvals == ()
    assert change_set_digest(bundle.record).tagged == bundle.record.changeset_digest
    assert bundle.record_path == "changesets/cs-00000000000000000001.json"
    stored_tree = instance._ledger.read_tree(bundle.oid)
    assert bundle.record_path in stored_tree
    assert b"base_oid" not in stored_tree[bundle.record_path]
    assert bundle.semantic_root.tagged != base.semantic_root
    assert bundle.descriptor.git_oid == bundle.oid
    assert bundle.descriptor.semantic_root == bundle.semantic_root.value
    assert bundle.generation_root.tagged != base.generation_root


def test_prepare_generation_binds_settlement_actor_to_proposal_admission(
    tmp_path: Path,
) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    submissions = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)

    with pytest.raises(
        SettlementIntegrityError,
        match="settlement actor binding differs from the proposal admission actor",
    ):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approval_submissions=submissions,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="reviewer"),
            proposal_actor_id="owner",
            sequence=1,
        )


def test_prebuild_is_unserved_until_winning_cas_then_switches_atomically(
    tmp_path: Path,
) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    submissions = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=submissions,
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=1,
    )
    publication = Path(instance.inspect().storage_directories["projections"])
    witness = MemoryWitness()
    publisher = ActivationPublisher(
        instance._ledger,
        publication_directory=publication,
        bodies=instance.body_store(),
        witness=witness,
    )

    projection = publisher.prebuild(bundle, base=base)

    assert Path(projection.manifest_path).is_file()
    assert not (publication / SERVING_MANIFEST_FILE).exists()
    assert instance._ledger.read_main() == base.git_oid

    result = publisher.activate(bundle, projection, base=base)

    assert result.status == "accepted"
    assert result.accepted is not None
    assert instance._ledger.read_main() == bundle.oid
    assert instance._ledger.read_generation_note(bundle.oid) is not None
    assert witness.records == [
        WitnessRecord(
            instance_id=base.instance_id,
            object_format=base.git_object_format,
            head_oid=bundle.oid,
            semantic_root=bundle.semantic_root.tagged,
            generation_root=bundle.generation_root.tagged,
            sequence=1,
        )
    ]
    with bind_current_projection(publication, expected=result.accepted) as handle:
        view = handle.document(
            "document:design",
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )
        assert view is not None
        metadata = next(
            fact for fact in view.facts if fact.schema_id == "playbill.document.metadata"
        )
        assert metadata.value["title"] == "Accepted design"


def test_two_candidates_from_one_base_leave_one_winner_and_no_loser_projection(
    tmp_path: Path,
) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    base, first_tree, first = _candidate(instance, title="First", body_content=b"first")
    _same_base, second_tree, second = _candidate(
        instance,
        title="Second",
        body_content=b"second",
    )

    def prepare(tree, candidate):
        submissions = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
        return prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approval_submissions=submissions,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=1,
        )

    first_bundle = prepare(first_tree, first)
    second_bundle = prepare(second_tree, second)
    publication = Path(instance.inspect().storage_directories["projections"])
    witness = MemoryWitness()
    publisher = ActivationPublisher(
        instance._ledger,
        publication_directory=publication,
        bodies=instance.body_store(),
        witness=witness,
    )
    first_projection = publisher.prebuild(first_bundle, base=base)
    second_projection = publisher.prebuild(second_bundle, base=base)
    loser_piece_paths = tuple(
        Path(second_projection.manifest_path).parent / piece.name
        for piece in second_projection.manifest.pieces
    )

    winner = publisher.activate(first_bundle, first_projection, base=base)
    loser = publisher.activate(second_bundle, second_projection, base=base)

    assert winner.status == "accepted"
    assert loser.status == "lost_cas"
    assert instance._ledger.read_main() == first_bundle.oid
    assert instance._ledger.read_generation_note(second_bundle.oid) is None
    assert not instance._ledger.object_exists(second_bundle.oid)
    assert not Path(second_projection.manifest_path).exists()
    assert all(not path.exists() for path in loser_piece_paths)
    assert [record.head_oid for record in witness.records] == [first_bundle.oid]


def test_prepare_generation_refuses_tampered_law_or_candidate_tree(tmp_path: Path) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    submissions = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
    tampered_law = candidate.model_copy(
        update={"law_digests": {"playbill.document.v1": "sha256:" + "00" * 32}}
    )
    with pytest.raises(SettlementIntegrityError, match="cannot be reproduced"):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=tampered_law,
            approval_submissions=submissions,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=1,
        )

    with pytest.raises(SettlementIntegrityError, match="no longer passes|cannot be reproduced"):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree={**tree, DOCUMENT_PATH: tree[DOCUMENT_PATH][:-1]},
            candidate=candidate,
            approval_submissions=submissions,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=1,
        )


def test_settlement_semantic_and_generation_preimages_match_golden() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "goldens" / "playbill" / "settlement-roots-v1.json"
        ).read_bytes()
    )
    binding = SettlementBinding(
        c_s_digest="sha256:" + "11" * 32,
        base_oid="aa" * 20,
    )
    semantic = compute_semantic_root(
        manifest_root_value="sha256:" + "22" * 32,
        changeset_digest_value="sha256:" + "33" * 32,
        approval_digests=("sha256:" + "44" * 32, "sha256:" + "55" * 32),
        parent_semantic_root="sha256:" + "66" * 32,
    )
    descriptor = GenerationDescriptor(
        semantic_root=semantic.value,
        git_oid="aa" * 20,
        parent_generation_root="77" * 32,
    )

    assert (
        canonical_bytes(binding.model_dump(mode="json")).decode() == fixture["settlement_preimage"]
    )
    assert semantic.tagged == fixture["semantic_root"]
    assert (
        canonical_bytes(descriptor.model_dump(mode="json")).decode()
        == fixture["generation_descriptor_preimage"]
    )
    assert generation_root(descriptor).tagged == fixture["generation_root"]


def test_qualified_git_formats_preserve_candidate_changeset_and_semantic_root(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed-placeholder"
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
    credentials = tmp_path / "daemon"
    daemon = generate_daemon_key(credentials)
    trust = PlaybillTrustRoot(
        instance_id="inst_cross_format_activation",
        daemon_public_key=daemon.principal.public_key,
        principals=tuple(
            sorted(
                (daemon.principal, owner.principal, reviewer.principal),
                key=lambda item: item.principal_id,
            )
        ),
    )
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    bodies = ContentAddressedBodyStore(cas_root)
    body = bodies.store(b"# Cross-format semantic state\n")
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Cross-format design",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )

    prepared = []
    candidates = []
    for object_format in ("sha1", "sha256"):
        ledger = GitLedger.initialize(
            tmp_path / f"ledger-{object_format}.git",
            object_format=object_format,
            signing_key_path=daemon.private_key_path,
            allowed_signers_path=credentials / ALLOWED_SIGNERS_FILE,
        )
        genesis = prepare_genesis(ledger, trust_root=trust, timestamp=FIXED_TIMESTAMP)
        base = AcceptedProjectionCoordinate(
            instance_id=trust.instance_id,
            repository_path=str(ledger.path.resolve()),
            git_object_format=object_format,
            git_oid=genesis.oid,
            semantic_root=genesis.semantic_root.tagged,
            generation_root=genesis.generation_root.tagged,
            compiler=current_compiler_coordinate(),
        )
        tree = {**genesis.tree, DOCUMENT_PATH: render_document(shell)}
        evaluated = evaluate_proposal_tree(
            base_tree=genesis.tree,
            current_tree=genesis.tree,
            proposed_tree=tree,
            current=base,
            bodies=bodies,
            timestamp=TIMESTAMP,
            rebased=False,
        )
        assert evaluated.candidate is not None
        candidate = evaluated.candidate
        candidates.append(candidate)
        submissions = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
        prepared.append(
            prepare_generation(
                ledger,
                base=base,
                candidate_tree=tree,
                candidate=candidate,
                approval_submissions=submissions,
                bodies=bodies,
                actor_binding=ChangeActorBinding(actor_id="owner"),
                proposal_actor_id="owner",
                sequence=1,
            )
        )

    sha1, sha256 = prepared
    # `C_s` is the locator-free object reviewers sign, and it stays identical
    # under both Git object formats: the same members, the same diff, the same
    # merkle manifest root, and therefore the same candidate digest and the same
    # approval payload. The record *around* it binds the coordinate its member
    # laws were evaluated at, which is a Git object ID and so differs, and every
    # value derived from the whole record differs with it.
    assert candidates[0].candidate == candidates[1].candidate
    assert candidates[0].candidate_digest == candidates[1].candidate_digest
    assert [
        (item.path, item.disposition, item.candidate_artifact_digest)
        for item in candidates[0].members
    ] == [
        (item.path, item.disposition, item.candidate_artifact_digest)
        for item in candidates[1].members
    ]
    assert [item.evaluation_coordinate.git_oid for item in candidates[0].law_evidence] != [
        item.evaluation_coordinate.git_oid for item in candidates[1].law_evidence
    ]
    assert sha1.record.changeset_digest != sha256.record.changeset_digest
    assert sha1.semantic_root != sha256.semantic_root
    assert sha1.oid != sha256.oid
    assert sha1.generation_root != sha256.generation_root
