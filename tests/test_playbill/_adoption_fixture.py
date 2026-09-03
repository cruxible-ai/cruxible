"""A deterministic adoption-scale Playbill fixture.

This module builds a real accepted ledger -- not a mock -- at a declared
profile: N Subjects, N ClaimTypes across deterministic Claim shards, a seeded
Claim population, Documents, QueryDefinitions, and then a run of accepted
generations each settling a representative multi-member Claim closure.

Everything the generator decides is a pure function of the profile and its seed:
identities come from a keyed hash, timestamps advance by sequence rather than by
clock, and nothing is read from the environment. Determinism stops exactly where
the instance's own identity begins -- each build mints fresh principal keys, so
its genesis coordinate and every coordinate chained from it differ, and a
Claim's Capture backing commits to the coordinate it was observed at. The
population, its paths, its identities, and the bytes of every member that does
not commit to a coordinate reproduce exactly.

It lives beside the tests rather than under `src/` because it is verification
input, and it carries no `test_` prefix so pytest never collects it. The
adoption-scale benchmark drives it at the Tier-1 profile; `test_adoption_scale`
drives it at a miniature profile so the generator cannot rot unnoticed.

The generator deliberately does not build a projection per generation. A
projection is disposable state that recovery rebuilds, and prebuilding one per
accepted generation would make fixture construction cost far more than the
replay it exists to measure.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    ApprovalSubmission,
    approval_statement_bytes,
)
from cruxible_client.contracts.candidates import (
    PRODUCED_CANDIDATE_VERSION,
    CandidateWireVersion,
)
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    build_direct_claim_capture,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, render_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimBackingV2,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    build_claim_citation,
    claim_path,
    claim_statement_address,
    render_claim,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_path,
    render_document,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimValueRefV1,
    QueryEntryV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.subjects import (
    SubjectShell,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_client.contracts.types import PlaybillTrustRoot
from cruxible_core.playbill.checkpoints import (
    DEFAULT_CHECKPOINT_INTERVAL,
    checkpoint_body,
    write_checkpoint,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial, generate_client_principal_key
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    prepare_generation,
    render_change_set,
    render_generation_descriptor,
)
from tests.test_playbill._support import client_material

SUBJECT_KIND = "project.work_item"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOOTSTRAP_TIMESTAMP = "2026-01-01T00:00:00+00:00"
CLAIM_VALUES = ("blocked", "done", "ready")


@dataclass(frozen=True)
class AdoptionFixtureProfile:
    """A named, reproducible fixture size.

    `TIER_1` is the PC-F filing-gate profile: five thousand active members, of
    which every one is a registered dependency artifact and the Claim population
    alone carries three pins each, over a thousand accepted generations of
    multi-member closures.
    """

    name: str
    subjects: int
    claim_types: int
    documents: int
    query_definitions: int
    seed_claims: int
    generations: int
    claims_per_generation: int = 3
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL
    seed: str = "playbill-adoption-v1"

    @property
    def expected_members(self) -> int:
        """Every semantic member the finished fixture holds, principals included."""

        return (
            self.subjects
            + self.claim_types
            + self.documents
            + self.query_definitions
            + self.seed_claims
            + self.generations * self.claims_per_generation
            + 1  # the governed approval-policy singleton
            + 1  # the governed Procedure runtime-policy singleton
            + 1  # the direct self-asserted capture contract
            + 3  # the daemon, owner, and independent reviewer principal records
        )


TIER_1 = AdoptionFixtureProfile(
    name="tier-1",
    subjects=500,
    claim_types=64,
    documents=20,
    query_definitions=16,
    seed_claims=1_398,
    generations=1_000,
)

MINIATURE = AdoptionFixtureProfile(
    name="miniature",
    subjects=6,
    claim_types=3,
    documents=2,
    query_definitions=2,
    seed_claims=6,
    generations=4,
)


@dataclass
class PhaseTimings:
    """Wall time per construction phase, for the fixture's own runtime report."""

    seconds: dict[str, float] = field(default_factory=dict)

    def record(self, name: str, seconds: float) -> None:
        self.seconds[name] = self.seconds.get(name, 0.0) + seconds


@dataclass(frozen=True)
class AdoptionFixture:
    profile: AdoptionFixtureProfile
    managed_root: Path
    instance: PlaybillInstance
    owner: GeneratedKeyMaterial
    head_sequence: int
    member_count: int
    timings: PhaseTimings


def _digest_id(profile: AdoptionFixtureProfile, *parts: object) -> str:
    payload = ":".join([profile.seed, *(str(part) for part in parts)]).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def _timestamp(sequence: int) -> str:
    return (EPOCH + timedelta(seconds=sequence)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _observed_at(index: int) -> datetime:
    return EPOCH + timedelta(minutes=index)


def _predicate(index: int) -> str:
    return f"{SUBJECT_KIND}.attribute_{index:04d}"


def _subject(index: int) -> SubjectShell:
    subject_id = f"wi-{index:05d}"
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{subject_id}"),
        subject_kind=SUBJECT_KIND,
        subject_id=subject_id,
    )


def _claim_type(index: int) -> ClaimType:
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    predicate = _predicate(index)
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=(SUBJECT_KIND,),
        object_kind="literal",
        literal_schema={"enum": list(CLAIM_VALUES), "type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(
            rules=(
                ClaimEvidenceAdmissionRuleV1(
                    rule_id="direct-self-asserted",
                    claim_roles=("normative", "observation"),
                    capture_contract_digests=(contract_digest,),
                    evidence_kinds=("self_asserted",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _claim_type_path(claim_type: ClaimType) -> str:
    namespace, _, leaf = claim_type.predicate.rpartition(".")
    return f"claim-types/{namespace}/{leaf}.json"


def _query_definition(index: int, claim_type: ClaimType) -> QueryDefinitionV1:
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=f"project.reading_{index:04d}"),
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(SUBJECT_KIND,),
            subject_id=QueryParameterRefV1(parameter="item_id"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="value",
                    value=QueryClaimValueRefV1(binding="item", predicate=claim_type.predicate),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="item_id", value_type="string"),),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="refuse_on_conflict",
        ),
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        pins=(
            ArtifactPin(
                role="claim-type",
                target=claim_type.identity,
                artifact_digest=claim_type_digest(claim_type).tagged,
            ),
        ),
    )


def _document(index: int, body_digest: str) -> DocumentShell:
    return DocumentShell(
        identity=f"document:note{index:05d}",
        document_kind="design",
        title=f"Adoption note {index:05d}",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _claim(
    *,
    claim_id: str,
    subject: SubjectShell,
    claim_type: ClaimType,
    value: str,
    observed_at: datetime,
    capture_digest: str,
    source_digest: str,
    source_length: int,
) -> ClaimArtifactV2:
    path = claim_path(claim_id)
    return ClaimArtifactV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                subject_path(subject.subject_kind, subject.subject_id)
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        backing=ClaimBackingV2(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_digest(subject).tagged,
                observed_at=observed_at,
            ),
            capture_digests=(capture_digest,),
            citations=(
                build_claim_citation(
                    ArtifactIdentity(kind="Claim", name=claim_id),
                    capture_digest=capture_digest,
                    role="evidence",
                    origin="self_source",
                ),
            ),
            source_mappings=(
                SourceMapping(
                    subject=claim_statement_address(path),
                    spans=(
                        ContentSpan(
                            content_digest=source_digest,
                            start_byte=0,
                            end_byte=source_length,
                        ),
                    ),
                ),
            ),
        ),
        pins=tuple(
            sorted(
                (
                    ArtifactPin(
                        role="capture-contract",
                        target=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
                        artifact_digest=capture_contract_digest(
                            DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
                        ).tagged,
                    ),
                    ArtifactPin(
                        role="claim-type",
                        target=claim_type.identity,
                        artifact_digest=claim_type_digest(claim_type).tagged,
                    ),
                    ArtifactPin(
                        role="subject",
                        target=subject.identity,
                        artifact_digest=subject_digest(subject).tagged,
                    ),
                ),
                key=lambda item: (item.role.encode(), item.target.qualified.encode()),
            )
        ),
    )


class _Builder:
    """Threads the accepted coordinate forward without re-replaying history."""

    def __init__(
        self,
        instance: PlaybillInstance,
        owner: GeneratedKeyMaterial,
        *,
        approver: GeneratedKeyMaterial | None = None,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
    ) -> None:
        self.instance = instance
        self.owner = owner
        self.approver = approver or client_material(instance.root.parent, instance)
        self.base = instance.accepted_coordinate()
        self.tree = dict(instance.tree_at(self.base.git_oid))
        self.sequence = 0
        self.checkpoint_interval = checkpoint_interval
        self.timings = PhaseTimings()

    def _sign(self, candidate_digest: str) -> ApprovalSubmission:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = serialization.load_ssh_private_key(
            self.approver.private_key_path.read_bytes(),
            password=None,
        )
        assert isinstance(private, Ed25519PrivateKey)
        statement = ApprovalStatement(
            signer_id=self.approver.principal.principal_id,
            signing_semantic_root=self.base.semantic_root,
            payload_digest=candidate_digest,
        )
        return ApprovalSubmission(
            submitted_by="adoption-fixture",
            attestation=ApprovalAttestation(
                **statement.model_dump(),
                sig=private.sign(approval_statement_bytes(statement)).hex(),
            ),
        )

    def accept(
        self,
        members: dict[str, bytes],
        *,
        phase: str,
        wire_version: CandidateWireVersion = PRODUCED_CANDIDATE_VERSION,
    ) -> None:
        """Settle one generation, optionally in a superseded wire version.

        `wire_version` exists so a fixture can build the accepted history a
        pre-succession build would have left behind, which is the only way to
        test that a ledger spanning the boundary replays end to end. Nothing a
        real daemon does passes it.
        """

        started = time.monotonic()
        sequence = self.sequence + 1
        candidate_tree = {**self.tree, **members}
        evaluation = evaluate_proposal_tree(
            base_tree=self.tree,
            current_tree=self.tree,
            proposed_tree=candidate_tree,
            current=self.base,
            bodies=self.instance.body_store(),
            timestamp=_timestamp(sequence),
            rebased=False,
            actor_id="owner",
            wire_version=wire_version,
        )
        if evaluation.candidate is None or evaluation.diagnostics:
            raise AssertionError(f"fixture candidate refused: {evaluation.diagnostics}")
        # The daemon re-commits the evaluated tree (proposals.ProposalService.propose)
        # and settles that OID, so the fixture must settle the same bytes: the
        # evaluated tree carries the derivative cards the settlement re-derives.
        candidate_tree = dict(evaluation.tree)
        bundle = prepare_generation(
            self.instance._ledger,
            base=self.base,
            candidate_tree=candidate_tree,
            candidate=evaluation.candidate,
            approval_submissions=(self._sign(evaluation.candidate.candidate_digest),),
            bodies=self.instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=sequence,
        )
        parent_generation_root = self.base.generation_root
        ledger = self.instance._ledger
        if not ledger.compare_and_set_main(bundle.oid, expected_oid=bundle.settlement.base_oid):
            raise AssertionError("fixture lost an uncontended settlement race")
        ledger.write_generation_note(bundle.oid, render_generation_descriptor(bundle.descriptor))
        self.base = AcceptedProjectionCoordinate(
            instance_id=self.base.instance_id,
            repository_path=self.base.repository_path,
            git_object_format=self.base.git_object_format,
            git_oid=bundle.oid,
            semantic_root=bundle.semantic_root.tagged,
            generation_root=bundle.generation_root.tagged,
            compiler=self.base.compiler,
        )
        self.tree = {**candidate_tree, bundle.record_path: render_change_set(bundle.record)}
        if sequence % self.checkpoint_interval == 0:
            # Exactly the stride a daemon writes on, so the fixture exercises the
            # same write path a real instance would leave behind.
            write_checkpoint(
                self.instance._checkpoint_directory(self.instance.root),
                checkpoint_body(
                    instance_id=self.base.instance_id,
                    object_format=self.base.git_object_format,
                    compiler=self.base.compiler,
                    genesis=self.instance.descriptor.genesis,
                    sequence=sequence,
                    git_oid=bundle.oid,
                    semantic_root=bundle.semantic_root.tagged,
                    generation_root=bundle.generation_root.tagged,
                    parent_generation_root=parent_generation_root,
                    tree=bundle.tree,
                ),
                written_at=_timestamp(sequence),
            )
        self.sequence = sequence
        self.timings.record(phase, time.monotonic() - started)


TRUST_ROOT_FILE = "trust-root.json"


def trust_root_path(root: Path) -> Path:
    return root / TRUST_ROOT_FILE


def _existing_owner(root: Path, instance: PlaybillInstance) -> GeneratedKeyMaterial:
    """Rebind the owner key a previous build left in custody.

    A resumed build must sign with the key the accepted history already trusts,
    so the record comes from the replayed principal registry rather than from
    anything on the custody side.
    """

    custody = root / "owner-custody"
    principal = instance.accepted_history()[-1].principals.require_active("owner")
    return GeneratedKeyMaterial(
        principal=principal,
        private_key_path=custody / "owner.ed25519",
        public_key_path=custody / "owner.ed25519.pub",
    )


def _existing_reviewer(root: Path, instance: PlaybillInstance) -> GeneratedKeyMaterial:
    """Rebind the independent approval key a previous build left in custody."""

    custody = root / "reviewer-custody"
    principal = instance.accepted_history()[-1].principals.require_active("reviewer")
    return GeneratedKeyMaterial(
        principal=principal,
        private_key_path=custody / "reviewer.ed25519",
        public_key_path=custody / "reviewer.ed25519.pub",
    )


def build_fixture(
    root: Path,
    profile: AdoptionFixtureProfile,
    *,
    resume: bool = False,
) -> AdoptionFixture:
    """Build one complete accepted ledger at `profile`, deterministically.

    With `resume`, an interrupted build continues from whatever it accepted
    rather than starting over. A Tier-1 build runs for the better part of an
    hour, and a generator that could only start from nothing would make every
    interruption cost the whole of it. Resumption reads its position out of the
    accepted tree -- how many Claims are already there -- so it needs no
    progress file that could disagree with the ledger.
    """

    managed_root = root / f"managed-{profile.name}"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if resume and managed_root.exists():
        trust_root = PlaybillTrustRoot.model_validate_json(
            trust_root_path(root).read_text(encoding="utf-8")
        )
        instance = PlaybillInstance.open(managed_root, trust_root=trust_root)
        owner = _existing_owner(root, instance)
        reviewer = _existing_reviewer(root, instance)
        builder = _Builder(
            instance,
            owner,
            approver=reviewer,
            checkpoint_interval=profile.checkpoint_interval,
        )
        builder.sequence = instance.accepted_history()[-1].sequence
    else:
        owner = generate_client_principal_key(
            root / "owner-custody",
            principal_id="owner",
            kind="ordinary",
            forbidden_roots=(workspace, managed_root),
        )
        reviewer = generate_client_principal_key(
            root / "reviewer-custody",
            principal_id="reviewer",
            kind="ordinary",
            forbidden_roots=(workspace, managed_root),
        )
        instance = PlaybillInstance.initialize(
            managed_root,
            instance_id=f"inst_adoption_{profile.name.replace('-', '_')}",
            client_principals=(owner.principal, reviewer.principal),
            workspace_roots=(workspace,),
            timestamp=BOOTSTRAP_TIMESTAMP,
        )
        # Persisted the moment it exists, not at the end of the build: the trust
        # root is out-of-band input, and a resumed build has to be handed the
        # same one rather than reconstruct it from the instance it is verifying.
        trust_root_path(root).write_text(
            json.dumps(instance.trust_root.model_dump(mode="json")),
            encoding="utf-8",
        )
        builder = _Builder(
            instance,
            owner,
            approver=reviewer,
            checkpoint_interval=profile.checkpoint_interval,
        )
    builder.timings.record("initialize", time.monotonic() - started)

    subjects = [_subject(index) for index in range(profile.subjects)]
    claim_types = [_claim_type(index) for index in range(profile.claim_types)]
    accepted_claims = sum(1 for path in builder.tree if path.startswith("claims/"))

    def claim_member(index: int) -> tuple[str, bytes]:
        claim_id = f"CLM-{_digest_id(profile, 'claim', index)}"
        subject = subjects[index % len(subjects)]
        claim_type = claim_types[index % len(claim_types)]
        value = CLAIM_VALUES[index % len(CLAIM_VALUES)]
        capture = build_direct_claim_capture(
            store=instance.body_store(),
            actor_id="owner",
            claim_id=claim_id,
            value=value,
            rationale=f"Deterministic adoption observation {index:07d}.",
            observed_at=_observed_at(index),
            accepted_coordinate=AcceptedCoordinate.from_internal(builder.base),
        )
        length = capture.envelope.commitment.byte_length
        if length is None:
            raise AssertionError("direct capture commitment must declare its byte length")
        claim = _claim(
            claim_id=claim_id,
            subject=subject,
            claim_type=claim_type,
            value=value,
            observed_at=_observed_at(index),
            capture_digest=capture.capture_digest,
            source_digest=capture.source_body_digest,
            source_length=length,
        )
        return claim_path(claim_id), render_claim(claim)

    if accepted_claims == 0:
        started = time.monotonic()
        vocabulary: dict[str, bytes] = {
            capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
            )
        }
        for claim_type in claim_types:
            vocabulary[_claim_type_path(claim_type)] = render_claim_type(claim_type)
        for subject in subjects:
            vocabulary[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(
                subject
            )
        builder.timings.record("vocabulary-render", time.monotonic() - started)
        builder.accept(vocabulary, phase="seed-vocabulary")

        readings: dict[str, bytes] = {}
        for index in range(profile.query_definitions):
            query = _query_definition(index, claim_types[index % len(claim_types)])
            readings[query_definition_path(query.identity.name)] = render_query_definition(query)
        for index in range(profile.documents):
            body = instance.store_document_body(f"# adoption note {index:05d}\n".encode())
            shell = _document(index, body.digest)
            readings[document_path(shell.identity.split(":", 1)[1])] = render_document(shell)
        builder.accept(readings, phase="seed-readings")

    minted = accepted_claims
    chunk = 350
    while minted < profile.seed_claims:
        upper = min(minted + chunk, profile.seed_claims)
        builder.accept(
            dict(claim_member(index) for index in range(minted, upper)),
            phase="seed-claims",
        )
        minted = upper

    completed = max(0, (minted - profile.seed_claims) // profile.claims_per_generation)
    for step in range(completed, profile.generations):
        builder.accept(
            dict(
                claim_member(profile.seed_claims + step * profile.claims_per_generation + offset)
                for offset in range(profile.claims_per_generation)
            ),
            phase="accept-generations",
        )

    member_count = sum(1 for path in builder.tree if not path.startswith("changesets/"))
    return AdoptionFixture(
        profile=profile,
        managed_root=managed_root,
        instance=instance,
        owner=owner,
        head_sequence=builder.sequence,
        member_count=member_count,
        timings=builder.timings,
    )


__all__ = [
    "MINIATURE",
    "TIER_1",
    "AdoptionFixture",
    "AdoptionFixtureProfile",
    "PhaseTimings",
    "TRUST_ROOT_FILE",
    "build_fixture",
    "trust_root_path",
]
