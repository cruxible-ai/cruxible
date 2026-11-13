"""A change's own installed CaptureContract counts as family membership (D1 follow-up).

D1 made ``capture_contract_families`` membership resolve through accepted
state: a rule's family entry admits a Capture only when its exact contract
digest is itself accepted for the Capture's claimed logical source. That
accepted-state lookup, at both the lowering-time check and the proposal
verdict, was bound only to the write's BASE (the referent before this change),
so it could never see a foreign-source CaptureContract this exact same write
installs as its own dependency (a Claim's first working-selection citation of
a source, which installs that source's deterministic contract unconditionally
on first use -- ``authoring/lowering.py``'s ``install_contract`` write).

This refused a Claim's first-ever citation of a catalogued workspace file
outright (``cruxible.claim.captured_evidence_required``), even though the
exact same write installed the contract the family rule would otherwise
admit. A second citation, made once that install was itself accepted,
resolved fine -- only the very first write establishing a source was broken.

The fix threads this change's own staged/resolved CaptureContracts into the
same keyed, digest-bound resolver as an additional source of "accepted
membership", alongside -- never instead of -- real accepted history: a
contract neither accepted at the base nor installed by this exact change
still refuses.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayload,
    ExistingCaptureCitationSource,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import (
    DirectForeignSourceSelection,
    build_foreign_source_capture,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import AuthoringLoweringError, lower_authoring
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.service.claims.claims import service_get_playbill_claim
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._support import client_material, initialize_local
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_claims import _claim_type, _subject
from tests.test_ledger.test_activation import _sign

SOURCE = "repo.app-db"


def _family_type() -> object:
    """The work-item status type whose one rule names only the workspace_file family.

    ``evidence_requirement='captured'`` is what makes a non-admitted citation
    a hard refusal rather than a quiet origin-only fallback: the schema law
    (``_requirement_law_refusal``) accepts a family-only rule as satisfying
    'captured', same as a named one.
    """

    return _claim_type().model_copy(
        update={
            "evidence_requirement": "captured",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="workspace",
                        claim_roles=("normative", "observation"),
                        capture_contract_families=("workspace_file",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
        }
    )


class _World:
    """A real governed instance, mirroring ``test_identity_evidence_rules._World``.

    Unlike that world's ``seed``, this one never pre-installs the source's
    foreign-source CaptureContract: the whole point is that no governed
    change has ever accepted it before the Claim's own first citation does.
    """

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

    def propose(self, tree: dict[str, bytes], *, name: str):
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

    def activate(self, result) -> None:
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

    def seed_without_contract(self, claim_type) -> None:
        tree = self.tree()
        subject = _subject()
        tree[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(subject)
        core_assigned(self.instance, tree)
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
        self.accept(tree, name="seed")

    def observe(self, text: bytes, *, claim_ref: str | None = None):
        """Author and accept one observation Claim from a first-time working selection."""

        digest = "sha256:" + hashlib.sha256(text).hexdigest()
        payload = ClaimAuthoringPayload(
            statement=_self_source_payload().statement,
            rationale="The repository snapshot says the work is ready.",
            source=WorkingSelectionObservation(
                source_id=SOURCE,
                coordinate=WorkingDigestCoordinate(
                    source_content_digest=digest, source_byte_length=len(text)
                ),
                selected_content_base64=base64.b64encode(text).decode("ascii"),
                selected_bytes_digest=digest,
                selector=WorkingAnchorWindow(
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
        changed_paths = {p for p, _content in lowered.changed_members}
        result = self.propose(dict(lowered.proposed_tree), name=f"observe-{next(self._clock)}")
        self.activate(result)
        claim_id = parse_claim(lowered.proposed_tree[path], path=path).identity.name
        return claim_id, changed_paths


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def test_a_first_citation_installs_and_is_admitted_end_to_end(world: _World) -> None:
    """lowering -> acceptance -> served account shows admitted, for the same write."""

    world.seed_without_contract(_family_type())

    claim_id, changed_paths = world.observe(b"status: ready")

    # The very same write installed the foreign-source CaptureContract this
    # family rule would otherwise need pre-accepted.
    assert any(path.startswith("capture-contracts/") for path in changed_paths)

    view = service_get_playbill_claim(world.instance, identity=claim_id)
    assert len(view.admission_accounts) == 1
    account = view.admission_accounts[0]
    assert account.status == "admitted"


def test_a_second_citation_after_acceptance_still_works(world: _World) -> None:
    """Revising with a second citation of the (now-accepted) source stays admitted."""

    world.seed_without_contract(_family_type())
    first, _ = world.observe(b"status: ready")
    second, _ = world.observe(b"status: done", claim_ref=first)
    assert second == first

    view = service_get_playbill_claim(world.instance, identity=first)
    assert len(view.admission_accounts) == 2
    assert all(account.status == "admitted" for account in view.admission_accounts)


def test_an_uninstalled_uncatalogued_profile_still_refuses(world: _World) -> None:
    """A contract neither accepted at base nor installed by this change stays refused.

    The membership rule is never loosened: citing a Capture under a DIFFERENT
    logical source that no governed change -- not even this one -- ever
    installed or accepted still fails closed, exactly as before D1's follow-up.
    """

    world.seed_without_contract(_family_type())

    store = world.instance.body_store()
    text = b"status: ready"
    stored = store.store(text)
    built = build_foreign_source_capture(
        store=store,
        actor_id="owner",
        claim_id="CLM-" + "a" * 32,
        rationale="cited from an uncatalogued, uninstalled source",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        accepted_coordinate=world.instance.accepted_coordinate(),
        selection=DirectForeignSourceSelection(
            logical_source_identity="repo.never-installed",
            span=ContentSpan(content_digest=stored.digest, start_byte=0, end_byte=len(text)),
        ),
    )
    payload = ClaimAuthoringPayload(
        statement=_self_source_payload().statement,
        rationale="cite the never-installed capture",
        source=ExistingCaptureCitationSource(capture_digest=built.capture_digest),
        citation_role="evidence",
    )
    actor = AuthenticatedActor(actor_id="owner")
    intent = world.coordinator.create(
        actor=actor, payload=payload, canonical_timestamp=world.timestamp()
    ).intent
    with pytest.raises(AuthoringLoweringError) as refusal:
        lower_authoring(world.instance, intent=intent, actor_id="owner")
    assert refusal.value.code == "cruxible.authoring.capture_contract_unresolved"
