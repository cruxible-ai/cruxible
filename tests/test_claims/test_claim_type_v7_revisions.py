"""Claim law v2 revision 8 / v3 revision 10 under ClaimType v7, on a governed instance.

- A carry (byte-identical backing) keeps all of its backing, whatever the rule.
- Under ``replace`` a revision that changes its statement carries exactly the
  evidence it cites; one that states the same thing again accumulates.
- ``captured`` needs a Capture under a declared contract; ``none`` is supported by
  the Claim's own origin.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
    SelfSourceBodyV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import render_capture_contract
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claim_verdicts import (
    claim_adjudication_rule,
    claim_adjudication_rule_digest,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactAny,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    inherited_capture_digests,
    parse_claim,
    render_claim,
)
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDispositionV3,
    build_dependent_closure_candidate,
    dependent_closure_inventory,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.evidence.evidence import (
    ClaimReadHistoryIndex,
    _claim_read_history_index,
    _reproduced_claim_adjudication_rule,
    service_evaluate_playbill_claim_verdict,
)
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_identity_evidence_rules import (
    CONTRACT_PATH,
    IDENTITY,
    ORIGINAL,
    PREDICATE,
    SOURCE,
    _digest,
    _successor,
    _v6_type,
    _World,
)

TYPE_PATH = claim_type_path(PREDICATE)
WHEN = datetime(2026, 8, 22, tzinfo=timezone.utc)


def v7_type(*, predecessor: ClaimType | None = None, **update: object) -> ClaimType:
    fields: dict[str, object] = {
        "artifact_format": "playbill-claim-type-v7",
        "evidence_requirement": "self",
        "revision_evidence": "replace",
        "lifecycle": ArtifactLifecycle(
            predecessor_digest=None
            if predecessor is None
            else claim_type_digest(predecessor).tagged
        ),
    }
    fields.update(update)
    return ClaimType.model_validate({**_v6_type().model_dump(mode="python"), **fields})


class _V7World(_World):
    _serial = 0

    def name(self, prefix: str) -> str:
        self._serial += 1
        return f"{prefix}-{self._serial}"

    def author(
        self,
        source: object,
        *,
        value: str = "ready",
        claim_ref: str | None = None,
        citation_role: str | None = "evidence",
        predicate: str = PREDICATE,
    ) -> tuple[dict[str, bytes], str]:
        """Lower one Claim (or revision) and return the candidate and its path."""

        statement = _self_source_payload().statement.model_copy(
            update={"object": LiteralClaimObject(value=value), "predicate": predicate}
        )
        assert isinstance(statement, AuthoringClaimStatementV1)
        payload_type = (
            ClaimAuthoringPayloadV3
            if isinstance(source, ExistingCaptureCitationSourceV1)
            else ClaimAuthoringPayloadV1
        )
        extra = (
            {"dependency_drafts": ClaimDependencyDraftsV1()}
            if payload_type is ClaimAuthoringPayloadV3
            else {}
        )
        payload = payload_type(
            statement=statement,
            rationale="The writer checked it.",
            source=source,  # type: ignore[arg-type]
            citation_role=citation_role,  # type: ignore[arg-type]
            revises=claim_ref,
            **extra,  # type: ignore[arg-type]
        )
        actor = AuthenticatedActor(actor_id="owner")
        intent = self.coordinator.create(
            actor=actor, payload=payload, canonical_timestamp=self.timestamp()
        ).intent
        lowered = lower_authoring(self.instance, intent=intent, actor_id="owner")
        path = next(p for p, _content in lowered.changed_members if p.startswith("claims/"))
        return dict(lowered.proposed_tree), path

    def say(self, text: bytes, *, value: str = "ready", claim_ref: str | None = None) -> str:
        tree, path = self.author(_selection(text), value=value, claim_ref=claim_ref)
        self.accept(tree, name=self.name("say"))
        return parse_claim(tree[path], path=path).identity.name

    def claim(self, claim_id: str) -> ClaimArtifactAny:
        return parse_claim(self.tree()[claim_path(claim_id)], path=claim_path(claim_id))

    def claim_type(self) -> ClaimType:
        from cruxible_client.contracts.claim_types import parse_claim_type

        return parse_claim_type(self.tree()[TYPE_PATH], path=TYPE_PATH)

    def succeed(self, successor: ClaimType) -> None:
        """Accept a ClaimType successor carrying every dependent Claim."""

        tree = self.tree()
        changed = {TYPE_PATH: render_claim_type(successor)}
        inventory = dependent_closure_inventory(
            tree, roots=(successor.identity,), fixed_paths=frozenset(changed)
        )
        settled, _ = build_dependent_closure_candidate(
            tree=tree,
            changed=changed,
            inventory=inventory,
            dispositions=tuple(
                ClaimTypeDependentDispositionV3(identity=item.identity, disposition="successor")
                for item in inventory
            ),
        )
        self.accept({**tree, **settled, **changed}, name=self.name("succeed"))

    def verdict(self, claim_id: str) -> str:
        result = service_evaluate_playbill_claim_verdict(
            self.instance, claim_identity=f"Claim:{claim_id}", evaluation_time=WHEN
        )
        return result.verdict.verdict

    def law_evidence(self, claim_id: str) -> object:
        index = _claim_read_history_index(
            self.instance, coordinate=self.instance.accepted_coordinate()
        )
        return index.law_evidence[claim_path(claim_id)]


def _selection(text: bytes) -> WorkingSelectionObservationV1:
    digest = "sha256:" + hashlib.sha256(text).hexdigest()
    return WorkingSelectionObservationV1(
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
    )


def _own_words(text: bytes) -> SelfSourceBodyV1:
    return SelfSourceBodyV1(content_base64=base64.b64encode(text).decode("ascii"))


@pytest.fixture
def world(tmp_path: Path) -> _V7World:
    return _V7World(tmp_path)


def _backing_bytes(claim: ClaimArtifactAny) -> bytes:
    return canonical_bytes(claim.backing.model_dump(mode="json"))


# --- Carries keep their backing (the distro's rule b) --------------------------------


def test_a_claim_type_succession_carries_evidence_byte_identical_under_replace(
    world: _V7World,
) -> None:
    world.seed(v7_type())
    claim_id = world.say(b"status: ready")
    world.say(b"status: done", value="done", claim_ref=claim_id)
    before = world.claim(claim_id)
    assert len(before.backing.capture_digests) == 1

    successor = v7_type(predecessor=world.claim_type(), description="Where the work stands.")
    world.succeed(successor)

    carried = world.claim(claim_id)
    assert carried.statement.claim_type_digest == claim_type_digest(successor).tagged
    assert _backing_bytes(carried) == _backing_bytes(before)
    assert [pin for pin in carried.pins if pin.role == "capture-contract"] == [
        pin for pin in before.pins if pin.role == "capture-contract"
    ]
    assert world.verdict(claim_id) == "supported"


def test_an_accumulated_claim_keeps_every_capture_when_its_type_moves_to_replace(
    world: _V7World,
) -> None:
    world.seed(_v6_type())
    claim_id = world.say(b"status: ready")
    world.say(b"status: done", value="done", claim_ref=claim_id)
    before = world.claim(claim_id)
    assert len(before.backing.capture_digests) == 2

    world.succeed(v7_type(predecessor=world.claim_type()))

    assert _backing_bytes(world.claim(claim_id)) == _backing_bytes(before)


# --- replace ---------------------------------------------------------------------------


def test_a_changed_statement_under_replace_carries_only_what_it_cites(world: _V7World) -> None:
    world.seed(v7_type())
    claim_id = world.say(b"status: ready")
    first = world.claim(claim_id)
    world.say(b"status: done", value="done", claim_ref=claim_id)

    revised = world.claim(claim_id)
    assert set(revised.backing.capture_digests).isdisjoint(first.backing.capture_digests)
    assert len(revised.backing.capture_digests) == 1
    assert len(revised.backing.citations) == 1  # type: ignore[union-attr]
    assert len(revised.backing.source_mappings) == 1
    evidence = world.law_evidence(claim_id)
    assert {item.capture_digest for item in evidence.verdict_captures} == set(  # type: ignore[attr-defined]
        revised.backing.capture_digests
    )
    assert world.verdict(claim_id) == "supported"


def test_a_statement_equal_revision_under_replace_keeps_its_evidence(world: _V7World) -> None:
    world.seed(v7_type())
    claim_id = world.say(b"status: ready")
    world.say(b"status: ready (checked again)", claim_ref=claim_id)
    assert len(world.claim(claim_id).backing.capture_digests) == 2


def test_under_accumulate_a_changed_statement_keeps_every_capture(world: _V7World) -> None:
    world.seed(v7_type(revision_evidence="accumulate"))
    claim_id = world.say(b"status: ready")
    world.say(b"status: done", value="done", claim_ref=claim_id)
    assert len(world.claim(claim_id).backing.capture_digests) == 2


def _dropping(world: _V7World, claim_id: str, text: bytes) -> dict[str, bytes]:
    """An accumulating lowering of a changed statement, with the old evidence cut."""

    tree, path = world.author(_selection(text), value="done", claim_ref=claim_id)
    claim = parse_claim(tree[path], path=path)
    old = world.claim(claim_id)
    kept = tuple(
        item for item in claim.backing.capture_digests if item not in old.backing.capture_digests
    )
    backing = claim.backing.model_copy(
        update={
            "capture_digests": kept,
            "citations": tuple(
                item
                for item in claim.backing.citations  # type: ignore[union-attr]
                if item.capture_digest in kept
            ),
            "source_mappings": claim.backing.source_mappings[-1:],
        }
    )
    tree[path] = render_claim(claim.model_copy(update={"backing": backing}))
    return tree


def test_under_accumulate_dropping_backing_is_still_refused(world: _V7World) -> None:
    world.seed(v7_type(revision_evidence="accumulate"))
    claim_id = world.say(b"status: ready")
    refused = world.refusals(_dropping(world, claim_id, b"status: done"), name="drop")
    assert "playbill.claim.required_backing_dropped" in refused


def test_under_replace_a_surplus_contract_pin_is_refused(world: _V7World) -> None:
    world.seed(v7_type())
    claim_id = world.say(b"status: ready")
    improved = _successor(ORIGINAL)
    tree = world.tree()
    tree[CONTRACT_PATH] = render_capture_contract(improved)
    world.accept(tree, name="improve-contract")

    tree, path = world.author(_selection(b"status: done"), value="done", claim_ref=claim_id)
    claim = parse_claim(tree[path], path=path)
    assert [pin.artifact_digest for pin in claim.pins if pin.role == "capture-contract"] == [
        _digest(improved)
    ]
    surplus = ArtifactPin(
        role="capture-contract", target=IDENTITY, artifact_digest=_digest(ORIGINAL)
    )
    pins = tuple(
        sorted(
            (*claim.pins, surplus),
            key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
        )
    )
    tree[path] = render_claim(claim.model_copy(update={"pins": pins}))
    assert "playbill.claim.capture_contract_pin_unbacked" in world.refusals(tree, name="surplus")


def test_a_recited_capture_still_counts_as_inherited(world: _V7World) -> None:
    world.seed(v7_type())
    claim_id = world.say(b"status: ready")
    first = world.claim(claim_id)
    (capture,) = first.backing.capture_digests

    tree, path = world.author(
        ExistingCaptureCitationSourceV1(capture_digest=capture), value="done", claim_ref=claim_id
    )
    revised = parse_claim(tree[path], path=path)
    assert revised.backing.capture_digests == (capture,)
    predecessor = AcceptedClaim(
        path=path,
        claim=first,
        statement_digest=claim_statement_digest(first.statement).tagged,
        artifact_digest=claim_artifact_digest(first).tagged,
    )
    assert inherited_capture_digests(revised, path=path, predecessor=predecessor) == {capture}
    world.accept(tree, name="recite")


# --- The evidence requirement -------------------------------------------------------------


def test_captured_refuses_own_words_in_the_law_and_accepts_a_capture(world: _V7World) -> None:
    world.seed(v7_type(evidence_requirement="captured"))
    tree, _path = world.author(_own_words(b"status: ready\n"), citation_role=None)
    assert "playbill.claim.captured_evidence_required" in world.refusals(tree, name="own")
    world.say(b"status: ready")


def test_captured_exempts_a_carry(world: _V7World) -> None:
    world.seed(v7_type())
    tree, path = world.author(_own_words(b"status: ready\n"), citation_role=None)
    world.accept(tree, name="own")
    claim_id = parse_claim(tree[path], path=path).identity.name
    before = world.claim(claim_id)

    world.succeed(v7_type(predecessor=world.claim_type(), evidence_requirement="captured"))

    assert _backing_bytes(world.claim(claim_id)) == _backing_bytes(before)


def test_none_is_supported_by_the_origin_alone(world: _V7World) -> None:
    world.seed(v7_type(evidence_requirement="none"))
    tree, path = world.author(_own_words(b"status: ready\n"), citation_role=None)
    world.accept(tree, name="own")
    claim_id = parse_claim(tree[path], path=path).identity.name
    assert world.verdict(claim_id) == "supported"


def test_self_leaves_own_words_uncovered_as_before(world: _V7World) -> None:
    world.seed(v7_type())
    tree, path = world.author(_own_words(b"status: ready\n"), citation_role=None)
    world.accept(tree, name="own")
    assert world.verdict(parse_claim(tree[path], path=path).identity.name) == "uncovered"


# --- The adjudication rule reproduces across successions ----------------------------


def _history(*claim_types: ClaimType) -> ClaimReadHistoryIndex:
    return ClaimReadHistoryIndex(
        instance=SimpleNamespace(),  # type: ignore[arg-type]
        generation_oids=(),
        law_evidence={},
        _claim_types={(TYPE_PATH, claim_type_digest(item).tagged): item for item in claim_types},
        _contract_identity=lambda digest: None,
    )


def _rule_digest(claim_type: ClaimType) -> str:
    return claim_adjudication_rule_digest(
        claim_adjudication_rule(claim_type, claim_type_digest=claim_type_digest(claim_type).tagged)
    )


def test_a_description_only_or_v6_to_v7_succession_reproduces_the_rule() -> None:
    v6 = _v6_type()
    upgraded = v7_type(predecessor=v6, revision_evidence="accumulate")
    described = v7_type(
        predecessor=upgraded,
        revision_evidence="replace",
        description="Where the work stands.",
        default_role="observation",
        member_descriptions=({"member": "done", "description": "Finished."},),
    )
    rule = _reproduced_claim_adjudication_rule(
        claim_type=described, evidence_digest=_rule_digest(v6), history=_history(v6, upgraded)
    )
    assert rule.claim_type_digest == claim_type_digest(described).tagged

    from cruxible_client.contracts.errors import ProposalIntegrityError

    origin = v7_type(predecessor=upgraded, evidence_requirement="none")
    with pytest.raises(ProposalIntegrityError, match="does not reproduce"):
        _reproduced_claim_adjudication_rule(
            claim_type=origin, evidence_digest=_rule_digest(v6), history=_history(v6, upgraded)
        )
