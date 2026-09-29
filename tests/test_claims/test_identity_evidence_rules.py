"""ClaimType v6 names CaptureContracts by identity, so contracts can be improved.

A compatible contract successor goes through as a change set of one artifact:
identity rules follow it, the Claims that cite the old version keep it as
provenance, and new evidence is captured under the new version. A breaking
change is a new identity; retiring a named contract, or moving one that exact
digests still name, is refused and names what it would strand.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    CaptureContractV1,
    capture_contract_digest,
    capture_contract_path,
    capture_contract_successor_break,
    evaluate_capture_contract_law,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicyV2,
    ClaimEvidenceAdmissionPolicyV3,
    ClaimEvidenceAdmissionRuleV2,
    ClaimEvidenceAdmissionRuleV3,
    EvidenceAdmissionInputV1,
    evaluate_claim_evidence_admission,
)
from cruxible_client.contracts.proposal_models import ProposalResult
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.claims.artifact_references import move_references
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_inspect_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.claims.evidence_rule_upgrade import service_upgrade_evidence_rules
from tests.core_support._support import client_material, initialize_local
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_claims import _claim_type, _subject
from tests.test_ledger.test_activation import _sign

SOURCE = "repo.work-items"
ORIGINAL = foreign_source_capture_contract(SOURCE)
IDENTITY = ORIGINAL.identity
CONTRACT_PATH = capture_contract_path(IDENTITY.name)
PREDICATE = _claim_type().predicate


def _digest(contract: CaptureContractV1) -> str:
    return capture_contract_digest(contract).tagged


def _successor(previous: CaptureContractV1, **changes: object) -> CaptureContractV1:
    update = {
        "selection_budget": previous.selection_budget.model_copy(
            update={"max_bytes": previous.selection_budget.max_bytes + 1024}
        ),
        **changes,
        "lifecycle": ArtifactLifecycle(predecessor_digest=_digest(previous)),
    }
    return previous.model_copy(update=update)


def _identity_rule(*identities: ArtifactIdentity) -> ClaimEvidenceAdmissionRuleV3:
    return ClaimEvidenceAdmissionRuleV3(
        rule_id="source",
        claim_roles=("normative", "observation"),
        capture_contracts=tuple(
            ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=identity) for identity in identities
        ),
        evidence_kinds=("self_asserted",),
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _digest_rule(*digests: str, rule_id: str = "source") -> ClaimEvidenceAdmissionRuleV2:
    return ClaimEvidenceAdmissionRuleV2(
        rule_id=rule_id,
        claim_roles=("normative", "observation"),
        capture_contract_digests=tuple(sorted(digests)),
        evidence_kinds=("self_asserted",),
        admission="direct",
        subject_binding="exact_claim_subject",
    )


def _v6_type() -> ClaimType:
    return _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v6",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV3(
                rules=(_identity_rule(IDENTITY),)
            ),
        }
    )


def _v5_type(*rules: ClaimEvidenceAdmissionRuleV2) -> ClaimType:
    return _claim_type().model_copy(
        update={
            "artifact_format": "playbill-claim-type-v5",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2(rules=rules),
        }
    )


# --- The client contracts ---------------------------------------------------


def test_an_identity_rule_admits_every_version_and_an_exact_rule_one() -> None:
    evidence = EvidenceAdmissionInputV1(
        claim_role="observation",
        capture_contract_digest=_digest(_successor(ORIGINAL)),
        capture_contract_identity=IDENTITY.qualified,
        evidence_kind="self_asserted",
        source_subject_bound=True,
    )
    identity_policy = ClaimEvidenceAdmissionPolicyV3(rules=(_identity_rule(IDENTITY),))
    exact_policy = ClaimEvidenceAdmissionPolicyV2(rules=(_digest_rule(_digest(ORIGINAL)),))

    assert evaluate_claim_evidence_admission(identity_policy, evidence).verdict == "eligible"
    assert evaluate_claim_evidence_admission(exact_policy, evidence).verdict == "refused"
    other = evidence.model_copy(update={"capture_contract_identity": "CaptureContract:other"})
    assert evaluate_claim_evidence_admission(identity_policy, other).verdict == "refused"


def test_a_successor_may_only_widen_what_it_accepts() -> None:
    assert capture_contract_successor_break(ORIGINAL, _successor(ORIGINAL)) is None
    narrowed = _successor(ORIGINAL, evidence_kinds=())
    assert capture_contract_successor_break(ORIGINAL, narrowed) == "evidence_kinds"
    regraded = _successor(ORIGINAL, epistemic_grade="predicted")
    assert capture_contract_successor_break(ORIGINAL, regraded) == "epistemic_grade"
    smaller = ORIGINAL.model_copy(
        update={
            "selection_budget": ORIGINAL.selection_budget.model_copy(
                update={"max_bytes": ORIGINAL.selection_budget.max_bytes - 1}
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=_digest(ORIGINAL)),
        }
    )
    assert capture_contract_successor_break(ORIGINAL, smaller) == "selection_budget.max_bytes"


def test_the_revision_4_law_refuses_breaking_successors_and_revival_but_not_retirement() -> None:
    accepted = AcceptedCaptureContract(
        path=CONTRACT_PATH, contract=ORIGINAL, artifact_digest=_digest(ORIGINAL)
    )

    def law(contract: CaptureContractV1, previous: AcceptedCaptureContract) -> str | None:
        result = evaluate_capture_contract_law(
            contract, path=CONTRACT_PATH, predecessor=previous, compatible_succession=True
        )
        return None if result.verdict == "accepted" else result.diagnostics[0].code

    assert law(_successor(ORIGINAL), accepted) is None
    assert (
        law(_successor(ORIGINAL, epistemic_grade="predicted"), accepted)
        == "playbill.capture_contract.incompatible_successor"
    )
    retired = ORIGINAL.model_copy(
        update={
            "evidence_kinds": (),
            "lifecycle": ArtifactLifecycle(state="retired", predecessor_digest=_digest(ORIGINAL)),
        }
    )
    assert law(retired, accepted) is None
    revived = retired.model_copy(
        update={"lifecycle": ArtifactLifecycle(predecessor_digest=_digest(retired))}
    )
    retired_version = AcceptedCaptureContract(
        path=CONTRACT_PATH, contract=retired, artifact_digest=_digest(retired)
    )
    assert law(revived, retired_version) == "playbill.capture_contract.revival_refused"
    # The historical revision-3 law keeps accepting what it always accepted.
    assert (
        evaluate_capture_contract_law(
            _successor(ORIGINAL, epistemic_grade="predicted"),
            path=CONTRACT_PATH,
            predecessor=accepted,
        ).verdict
        == "accepted"
    )


def test_a_v6_claim_type_names_contracts_only_through_its_rules() -> None:
    v6 = _v6_type()
    assert v6.evidence_admission_policy.rules[0].names_capture_contract(
        digest="sha256:" + "0" * 64, identity=IDENTITY.qualified
    )
    pin = ArtifactPin(role="capture-contract", target=IDENTITY, artifact_digest=_digest(ORIGINAL))
    with pytest.raises(ValueError, match="never by an exact pin"):
        ClaimType.model_validate({**v6.model_dump(), "pins": (pin,)})
    with pytest.raises(ValueError, match="evidence policy v3"):
        ClaimType.model_validate(
            {**v6.model_dump(), "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2()}
        )


def test_moving_a_claims_references_never_rewrites_its_contract_provenance() -> None:
    old, new = _digest(ORIGINAL), _digest(_successor(ORIGINAL))
    type_old, type_new = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    payload = {
        "statement": {"claim_type_digest": type_old},
        "pins": [
            {"role": "capture-contract", "target": {}, "artifact_digest": old},
            {"role": "claim-type", "target": {}, "artifact_digest": type_old},
        ],
    }
    moved = move_references("claims/x.json", payload, {old: new, type_old: type_new})
    assert moved["statement"]["claim_type_digest"] == type_new
    assert [pin["artifact_digest"] for pin in moved["pins"]] == [old, type_new]


# --- A governed instance ----------------------------------------------------


class _World:
    def __init__(self, tmp_path: Path) -> None:
        self.instance, _owner = initialize_local(tmp_path)
        self._clock = itertools.count(10)
        self._claims = itertools.count(1)
        exhaust = self.instance.root / self.instance.descriptor.storage.exhaust
        tokens = itertools.count(1)
        self.coordinator = AuthoringIntentCoordinator(
            instance=self.instance,
            store=AuthoringIntentStore(exhaust, token_factory=lambda: f"{next(tokens):032x}"),
            claim_id_factory=lambda: f"CLM-{next(self._claims):032x}",
        )

    def timestamp(self) -> str:
        return f"2026-08-21T12:{next(self._clock):02d}:00.000000Z"

    def tree(self) -> dict[str, bytes]:
        return dict(self.instance.tree_at(self.instance.accepted_coordinate().git_oid))

    def propose(self, tree: dict[str, bytes], *, name: str) -> ProposalResult:
        base = self.instance.accepted_coordinate()
        return self.instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
            ),
            candidate_tree=tree,
            timestamp=self.timestamp(),
        )

    def accept(self, tree: dict[str, bytes], *, name: str) -> None:
        result = self.propose(tree, name=name)
        assert result.candidate is not None, [item.code for item in result.evaluation.diagnostics]
        self.activate(result)

    def activate(self, result: ProposalResult) -> None:
        candidate = result.candidate
        assert candidate is not None and result.evaluation.evaluated_tree_oid is not None
        base = self.instance.accepted_coordinate()
        bundle = self.instance.prepare_generation(
            base=base,
            candidate_tree=self.instance.proposal_tree(result.evaluation.evaluated_tree_oid),
            candidate=candidate,
            approvals=(
                _sign(
                    client_material(self.instance.root.parent, self.instance),
                    candidate.candidate_digest,
                    base.semantic_root,
                ),
            ),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=len(self.instance.accepted_history()),
        )
        publisher = self.instance.activation_publisher()
        projection = publisher.prebuild(bundle, base=base)
        assert publisher.activate(bundle, projection, base=base).status == "accepted"
        self.instance.refresh()

    def activate_proposal(self, proposal_id: str) -> None:
        inspected = service_inspect_playbill_proposal(self.instance, proposal_id=proposal_id)
        candidate = inspected.proposal.candidate
        assert candidate is not None
        if candidate.approval_requirements:
            approval = _sign(
                client_material(self.instance.root.parent, self.instance),
                candidate.candidate_digest,
                self.instance.accepted_coordinate().semantic_root,
            )
            service_submit_playbill_approval(
                self.instance,
                proposal_id=proposal_id,
                attestation=approval.attestation,
                authenticated_submitter="owner",
            )
        activated = service_activate_playbill_proposal(
            self.instance, proposal_id=proposal_id, activated_by="owner"
        )
        assert activated.status == "accepted"
        self.instance.refresh()

    def refusals(self, tree: dict[str, bytes], *, name: str) -> set[str]:
        result = self.propose(tree, name=name)
        assert result.candidate is None
        return {item.code for item in result.evaluation.diagnostics}

    def seed(self, claim_type: ClaimType) -> None:
        tree = self.tree()
        subject = _subject()
        tree[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(subject)
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
        tree[CONTRACT_PATH] = render_capture_contract(ORIGINAL)
        self.accept(tree, name="seed")

    def observe(self, text: bytes, *, claim_ref: str | None = None) -> str:
        """Author one observation Claim from a working selection; returns its id."""

        digest = "sha256:" + hashlib.sha256(text).hexdigest()
        payload = ClaimAuthoringPayloadV1(
            statement=_self_source_payload().statement,
            rationale="The repository snapshot says the work is ready.",
            source=WorkingSelectionObservationV1(
                source_id=SOURCE,
                coordinate=WorkingDigestCoordinateV1(
                    source_content_digest=digest, source_byte_length=len(text)
                ),
                selected_content_base64=base64.b64encode(text).decode("ascii"),
                selected_bytes_digest=digest,
                selector=WorkingAnchorWindowV1(
                    anchor=text.decode("ascii"),
                    start_byte=0,
                    end_byte=len(text),
                    observed_occurrence_count=1,
                ),
            ),
            citation_role="evidence",
            revises=claim_ref,
        )
        actor = AuthenticatedActor(actor_id="owner")
        intent = self.coordinator.create(
            actor=actor, payload=payload, canonical_timestamp=self.timestamp()
        ).intent
        lowered = lower_authoring(self.instance, intent=intent, actor_id="owner")
        path = next(p for p, _content in lowered.changed_members if p.startswith("claims/"))
        self.accept(dict(lowered.proposed_tree), name=f"observe-{next(self._clock)}")
        return parse_claim(lowered.proposed_tree[path], path=path).identity.name

    def contract_pins(self, claim_id: str) -> list[str]:
        claim = parse_claim(self.tree()[claim_path(claim_id)], path=claim_path(claim_id))
        return [pin.artifact_digest for pin in claim.pins if pin.role == "capture-contract"]


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_a_compatible_contract_successor_needs_no_other_change(world: _World) -> None:
    world.seed(_v6_type())
    first = world.observe(b"status: ready")
    improved = _successor(ORIGINAL)

    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(improved)
    world.accept(tree, name="improve-contract")

    # The Claim keeps the version its evidence used.
    assert world.contract_pins(first) == [_digest(ORIGINAL)]
    # New evidence is captured under the head, and revising the Claim with it
    # keeps both versions as provenance.
    world.observe(b"status: done", claim_ref=first)
    assert world.contract_pins(first) == sorted([_digest(ORIGINAL), _digest(improved)])


def test_a_breaking_contract_change_is_refused_as_a_successor(world: _World) -> None:
    world.seed(_v6_type())
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(_successor(ORIGINAL, epistemic_grade="predicted"))
    assert "playbill.capture_contract.incompatible_successor" in world.refusals(
        tree, name="break-contract"
    )


def test_retiring_a_contract_an_identity_rule_names_is_refused(world: _World) -> None:
    world.seed(_v6_type())
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(
        ORIGINAL.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=_digest(ORIGINAL)
                )
            }
        )
    )
    assert "playbill.capture_contract.dependents_not_settled" in world.refusals(
        tree, name="retire-contract"
    )


def test_an_exact_digest_rule_makes_a_successor_loud_until_it_moves_too(world: _World) -> None:
    exact = _v5_type(_digest_rule(_digest(ORIGINAL)))
    world.seed(exact)
    improved = _successor(ORIGINAL)
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(improved)
    assert "playbill.capture_contract.dependents_not_settled" in world.refusals(
        tree, name="strand-exact-rule"
    )

    moved = exact.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2(
                rules=(_digest_rule(_digest(improved)),)
            ),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(exact).tagged),
        }
    )
    tree[claim_type_path(PREDICATE)] = render_claim_type(moved)
    world.accept(tree, name="move-exact-rule")


def test_the_upgrade_converts_exact_rules_and_carries_the_claims(world: _World) -> None:
    world.seed(_v5_type(_digest_rule(_digest(ORIGINAL))))
    claim_id = world.observe(b"status: ready")

    result = service_upgrade_evidence_rules(
        world.instance, actor_id="owner", timestamp=world.timestamp()
    )

    assert result.status == "proposed", result
    assert [item.claim_type for item in result.converted] == [f"ClaimType:{PREDICATE}"]
    assert result.converted[0].widened_versions == ()
    assert result.carried_claims == 1
    assert result.proposal_id is not None
    type_path = claim_type_path(PREDICATE)
    # Proposing lands nothing; the ordinary approval and activation do.
    assert parse_claim_type(world.tree()[type_path], path=type_path).artifact_format == (
        "playbill-claim-type-v5"
    )
    world.activate_proposal(result.proposal_id)

    upgraded = parse_claim_type(world.tree()[type_path], path=type_path)
    assert upgraded.artifact_format == "playbill-claim-type-v6"
    assert upgraded.evidence_admission_policy.rules[0].names_capture_contract(
        digest=_digest(ORIGINAL), identity=IDENTITY.qualified
    )
    carried = parse_claim(world.tree()[claim_path(claim_id)], path=claim_path(claim_id))
    assert carried.statement.claim_type_digest == claim_type_digest(upgraded).tagged
    assert world.contract_pins(claim_id) == [_digest(ORIGINAL)]
    # After the upgrade a compatible contract successor needs nothing else.
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(_successor(ORIGINAL))
    world.accept(tree, name="improve-after-upgrade")


def test_the_upgrade_refuses_rules_that_would_start_matching_the_same_evidence(
    world: _World,
) -> None:
    improved = _successor(ORIGINAL)
    world.seed(_v5_type(_digest_rule(_digest(ORIGINAL))))
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(improved)
    before = parse_claim_type(tree[claim_type_path(PREDICATE)], path=claim_type_path(PREDICATE))
    split = _v5_type(
        _digest_rule(_digest(improved), rule_id="new"),
        _digest_rule(_digest(ORIGINAL), rule_id="old"),
    ).model_copy(
        update={"lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(before).tagged)}
    )
    tree[claim_type_path(PREDICATE)] = render_claim_type(split)
    world.accept(tree, name="split-rules")

    result = service_upgrade_evidence_rules(
        world.instance, actor_id="owner", timestamp=world.timestamp()
    )

    assert result.status == "unchanged"
    assert [item.claim_type for item in result.refused] == [f"ClaimType:{PREDICATE}"]
    assert "both match the same evidence" in result.refused[0].reason


def test_moving_a_query_a_claim_type_corroborates_through_needs_the_claim_type_too(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.query.definitions import (
        query_definition_digest,
        query_definition_path,
        render_query_definition,
    )
    from tests.test_claims.test_claim_corroboration_integration import (
        _corroborated_type,
        _query,
        _seed_vocabulary,
    )

    world = _World(tmp_path)
    query = _query()
    claim_type = _corroborated_type(query_definition_digest(query).tagged)
    _seed_vocabulary(world.instance, None, claim_types=(claim_type,), query=query)
    world.instance.refresh()
    revised = query.model_copy(
        update={
            "description": "Whether the work item exists.",
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=query_definition_digest(query).tagged
            ),
        }
    )
    tree = world.tree()
    tree[query_definition_path(query.identity.name)] = render_query_definition(revised)
    assert "playbill.query_definition.corroboration_dependents_not_settled" in world.refusals(
        tree, name="move-query"
    )

    tree[claim_type_path(claim_type.predicate)] = render_claim_type(
        _corroborated_type(
            query_definition_digest(revised).tagged,
            predecessor_digest=claim_type_digest(claim_type).tagged,
        )
    )
    world.accept(tree, name="move-query-and-type")


# --- Review regressions -------------------------------------------------------


def test_replay_finds_every_historical_version_in_any_order(world: _World) -> None:
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.ledger.recovery import _LedgerArtifactVersions

    world.seed(_v6_type())
    versions = [ORIGINAL]
    for step in range(2):
        versions.append(_successor(versions[-1]))
        tree = world.tree()
        tree[CONTRACT_PATH] = render_capture_contract(versions[-1])
        world.accept(tree, name=f"improve-{step}")
    at = AcceptedCoordinate.from_internal(world.instance.accepted_coordinate())
    digests = [_digest(item) for item in versions]
    for order in (digests, list(reversed(digests)), [digests[1], digests[0], digests[2]]):
        lookup = _LedgerArtifactVersions(world.instance._ledger)
        for digest in order:
            found = lookup(at, digest, family="capture-contracts/")
            assert found is not None and found[0] == CONTRACT_PATH, digest


def test_a_two_version_claim_passes_the_cold_projection_proof(world: _World) -> None:
    from cruxible_core.indexes import sqlite as playbill_projection

    world.seed(_v6_type())
    first = world.observe(b"status: ready")
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(_successor(ORIGINAL))
    world.accept(tree, name="improve-contract")
    world.observe(b"status: done", claim_ref=first)
    assert len(world.contract_pins(first)) == 2

    coordinate = world.instance.accepted_coordinate()
    with world.instance.bind_accepted_projection(coordinate) as handle:
        stamps = handle.index_path.parent / playbill_projection.SOURCE_AUTHENTICATION_STAMPS
    stamps.unlink(missing_ok=True)
    playbill_projection.reset_projection_verification_memo()
    with world.instance.bind_accepted_projection(coordinate):
        pass


def test_a_provenance_pin_must_name_the_identity_its_version_belongs_to(world: _World) -> None:
    from cruxible_client.contracts.claims import render_claim

    world.seed(_v6_type())
    claim_id = world.observe(b"status: ready")
    path = claim_path(claim_id)
    claim = parse_claim(world.tree()[path], path=path)
    wrong = ArtifactIdentity(kind="CaptureContract", name="never-accepted")
    pins = [
        pin.model_copy(update={"target": wrong}) if pin.role == "capture-contract" else pin
        for pin in claim.pins
    ]
    pins.sort(key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest))
    forged = claim.model_copy(
        update={
            "pins": tuple(pins),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(claim).tagged),
        }
    )
    tree = world.tree()
    tree[path] = render_claim(forged)
    assert "playbill.claim.capture_contract_pin_unresolved" in world.refusals(
        tree, name="forged-provenance"
    )


def _dependents(*claim_types: ClaimType, windows: tuple[object, ...] = ()) -> tuple[str, ...]:
    from types import SimpleNamespace

    from cruxible_core.proposals.proposals import _capture_contract_dependents

    context = SimpleNamespace(
        resolved=SimpleNamespace(
            claim_types={
                item.identity.qualified: SimpleNamespace(claim_type=item) for item in claim_types
            },
            resolution_contracts={f"r{index}": item for index, item in enumerate(windows)},
        )
    )
    return _capture_contract_dependents(
        context,  # type: ignore[arg-type]
        identity=IDENTITY.qualified,
        previous_digest=_digest(ORIGINAL),
        successor_digest=_digest(_successor(ORIGINAL)),
    )


def test_a_rule_for_other_roles_does_not_cover_a_stranded_exact_rule() -> None:
    observation = _digest_rule(_digest(ORIGINAL), rule_id="observed").model_copy(
        update={"claim_roles": ("observation",)}
    )
    normative = _digest_rule(_digest(_successor(ORIGINAL)), rule_id="stated").model_copy(
        update={"claim_roles": ("normative",)}
    )
    assert _dependents(_v5_type(observation, normative)) == (f"ClaimType:{PREDICATE}",)
    covering = normative.model_copy(update={"claim_roles": ("normative", "observation")})
    assert _dependents(_v5_type(observation, covering)) == ()


def test_only_live_resolution_contracts_hold_a_contract_in_place() -> None:
    from cruxible_client.contracts.procedures.windows import (
        CaptureEventSelectorV1,
        CaptureEventWindowV1,
    )
    from cruxible_client.contracts.resolution_contracts import ResolutionContractV1

    def window(state: str) -> ResolutionContractV1:
        return ResolutionContractV1.model_construct(
            identity=ArtifactIdentity(kind="ResolutionContract", name=state),
            window=CaptureEventWindowV1(
                event=CaptureEventSelectorV1(
                    capture_contract_identity=IDENTITY,
                    capture_contract_digest=_digest(ORIGINAL),
                ),
                duration_seconds=60,
            ),
            lifecycle=ArtifactLifecycle(state=state),  # type: ignore[arg-type]
        )

    assert _dependents(windows=(window("live"), window("retired"))) == ("ResolutionContract:live",)


def test_the_upgrade_refuses_versions_that_would_newly_match_two_rules() -> None:
    from cruxible_core.service.claims.evidence_rule_upgrade import _convert, _Refused

    other = foreign_source_capture_contract("repo.other")
    improved = _successor(ORIGINAL)
    by_digest = {
        _digest(item): AcceptedCaptureContract(
            path=capture_contract_path(item.identity.name),
            contract=item,
            artifact_digest=_digest(item),
        )
        for item in (ORIGINAL, improved, other)
    }

    class Lineages:
        def version(self, digest: str) -> AcceptedCaptureContract:
            return by_digest[digest]

        def lineage(self, identity: str) -> tuple[AcceptedCaptureContract, ...]:
            return tuple(
                item for item in by_digest.values() if item.contract.identity.qualified == identity
            )

    # Both rules already share the other contract, but C1 and C2 each matched one.
    first = _digest_rule(_digest(other), _digest(ORIGINAL), rule_id="a")
    second = _digest_rule(_digest(other), _digest(improved), rule_id="b")
    with pytest.raises(_Refused, match="both match the same evidence"):
        _convert(_v5_type(first, second), Lineages())  # type: ignore[arg-type]


def test_a_stricter_or_ambiguous_successor_rule_does_not_cover_an_exact_rule() -> None:
    previous = _digest_rule(_digest(ORIGINAL), rule_id="old")
    successor = _digest_rule(_digest(_successor(ORIGINAL)), rule_id="new")
    assert _dependents(_v5_type(successor, previous)) == ()

    stricter = successor.model_copy(update={"attestation_requirement": "verified_principal"})
    assert _dependents(_v5_type(stricter, previous)) == (f"ClaimType:{PREDICATE}",)

    twin = successor.model_copy(update={"rule_id": "newer"})
    assert _dependents(_v5_type(successor, twin, previous)) == (f"ClaimType:{PREDICATE}",)
