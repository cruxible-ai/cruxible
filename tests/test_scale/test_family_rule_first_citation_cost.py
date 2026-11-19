"""A family rule's first-citation cost tracks an exact rule's, not the unrelated count.

D1 follow-up review (round 2, F1): the first cut of ``historical_capture_contract``'s
staged-membership fallback re-keyed ``_ResolvedArtifacts.capture_contracts`` --
a mapping lazily backed by *every* CaptureContract the candidate can resolve
(named-rule lookups need that reach) -- by calling ``.values()`` on it. That
forced the lazy mapping to materialize its whole backing table: every Claim
judged in a change paid O(every accepted CaptureContract in the instance),
family selector or not, and a multi-Claim change repeated the enumeration
once per Claim.

The fix (``_changed_capture_contracts_by_digest``) walks ``scope`` -- the
change set's own changed paths -- instead, and is computed once per change,
not once per Claim.

A first citation of a brand-new source beside many unrelated, already-
accepted CaptureContracts has its own baseline cost that is NOT this fix's
concern (closure and dependency-pin checks over the candidate's declared
artifacts): it shows up even for an exact-named rule, which structurally can
never reach ``historical_capture_contract`` at all. So this guard does not
assert each route is flat in isolation; it asserts the family route's cost,
and its growth from 100 to 1,000 unrelated contracts, tracks the exact
route's -- and that a multi-Claim change costs a linear multiple of one
Claim, not a multiple of an already-inflated per-Claim cost. Before the fix,
a family rule's cost at 1,000 unrelated contracts was roughly 1,000 times an
exact rule's (the review's own "102 vs 1,003 SQL statements" finding); after
it, they move together.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import (
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._support import client_material, initialize_local
from tests.support.claim_types import claim_type as _build_claim_type
from tests.test_authoring.test_authoring_preflight import _self_source_payload
from tests.test_claims.test_claims import _subject
from tests.test_ledger.test_activation import _sign
from tests.test_scale import _counters
from tests.test_scale._counters import COUNTERS, counting, trace_sqlite

SMALL, LARGE = 100, 1_000
BATCH = 200
_DESCRIPTOR_ALIASES = ("/dev/fd", "/proc/self/fd")
COUNTED = tuple(
    name for name in COUNTERS if name not in {"git_spawns", "other_spawns", "file_opens"}
)


#: The one foreign source the exact-named route's rule names ahead of time. A
#: named rule may name a source not yet installed (the first Claim bound to
#: it installs that dependency itself, same as a family rule's source), so
#: no pre-seeding is needed -- only one world ever cites it, exactly once.
EXACT_SOURCE = "repo.measured-exact"


def _exact_type() -> object:
    """A ClaimType whose one rule names a specific foreign-source contract by identity.

    No ``capture_contract_families`` entry at all: ``_policy_needs_verified_shape``
    short-circuits before any staged-membership resolver ever runs, so this
    route proves the fix adds no cost to the ordinary, named-identity case
    either. A distinct predicate from ``_family_type`` -- both are
    ``project.work_item.*`` so they share ``_subject``'s kind, but two
    ClaimTypes can never share one predicate's artifact path.
    """

    return _build_claim_type(
        "project.work_item.status_exact",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="direct",
                    claim_roles=("normative", "observation"),
                    capture_contracts=(
                        foreign_source_capture_contract(EXACT_SOURCE).identity.name,
                    ),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )


def _family_type() -> object:
    return _build_claim_type(
        "project.work_item.status_family",
        evidence_requirement="captured",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
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
    )


def _multi_subject(offset: int) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"project.work_item/wi-multi-{offset}"),
        subject_kind="project.work_item",
        subject_id=f"wi-multi-{offset}",
    )


@dataclass
class _World:
    name: str
    root: Path
    instance: PlaybillInstance
    coordinator: AuthoringIntentCoordinator
    clock: int = 10
    claim_ordinal: int = 1
    token_ordinal: int = 1


@dataclass
class _Session:
    worlds: dict[str, _World]
    measured: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    failed: dict[str, BaseException] = field(default_factory=dict)


def _timestamp(world: _World) -> str:
    world.clock += 1
    return f"2026-08-21T12:{world.clock:02d}:00.000000Z"


def _accept(world: _World, tree: dict[str, bytes], *, name: str) -> None:
    base = world.instance.accepted_coordinate()
    result = world.instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp=_timestamp(world),
    )
    candidate = result.candidate
    assert candidate is not None and result.evaluation.evaluated_tree_oid is not None, [
        item.code for item in result.evaluation.diagnostics
    ]
    bundle = world.instance.prepare_generation(
        base=base,
        candidate_tree=world.instance.proposal_tree(result.evaluation.evaluated_tree_oid),
        candidate=candidate,
        approvals=(
            _sign(
                client_material(world.instance.root.parent, world.instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(world.instance.accepted_history()),
    )
    publisher = world.instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    world.instance.refresh()


def _tree(world: _World) -> dict[str, bytes]:
    return dict(world.instance.tree_at(world.instance.accepted_coordinate().git_oid))


def _build(root: Path, name: str, unrelated: int) -> _World:
    root.mkdir(parents=True)
    instance, _owner = initialize_local(root)
    exhaust = instance.root / instance.descriptor.storage.exhaust
    tokens = itertools.count(1)
    claims = itertools.count(1)
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: f"{next(tokens):032x}"),
        claim_id_factory=lambda: f"CLM-{next(claims):032x}",
    )
    world = _World(name=name, root=root, instance=instance, coordinator=coordinator)

    tree = _tree(world)
    subject = _subject()
    tree[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(subject)
    # Three more Subjects, one per Claim the multi-Claim route's change set
    # authors: each needs its own (subject, predicate) slot, never one shared
    # by several live Claims of one cardinality-one ClaimType.
    for offset in range(3):
        extra = _multi_subject(offset)
        tree[subject_path(extra.subject_kind, extra.subject_id)] = render_subject(extra)
    core_assigned(instance, tree)
    # ``_exact_type`` is NOT seeded here: its dependency draft rides along with
    # its own first citing Claim (``_exact_first_citation``), exactly the
    # ``install_builtin_capture_dependencies`` path the original bug named.
    tree[claim_type_path(_family_type().predicate)] = render_claim_type(_family_type())
    _accept(world, tree, name="seed")

    # Everything else: unrelated, already-accepted CaptureContracts no Claim
    # ever cites -- exactly what review F1's regression enumerated per Claim.
    for start in range(0, unrelated, BATCH):
        tree = _tree(world)
        for index in range(start, min(unrelated, start + BATCH)):
            contract = foreign_source_capture_contract(f"repo.unrelated-{index}")
            tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
        _accept(world, tree, name=f"unrelated-{start}")
    return world


def _observe_payload(
    world: _World,
    source_id: str,
    *,
    predicate: str,
    claim_type_draft: object | None = None,
    subject: SubjectShell | None = None,
) -> ClaimAuthoringPayload:
    text = f"status for {source_id}".encode()
    digest = "sha256:" + hashlib.sha256(text).hexdigest()
    update: dict[str, object] = {"predicate": predicate}
    if subject is not None:
        update["subject"] = SemanticAddress.whole_artifact(
            subject_path(subject.subject_kind, subject.subject_id)
        )
    statement = _self_source_payload().statement.model_copy(update=update)
    return ClaimAuthoringPayload(
        statement=statement,
        rationale="First citation of a distinct source for this route.",
        source=WorkingSelectionObservation(
            source_id=source_id,
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
        dependency_drafts=(
            ClaimDependencyDrafts()
            if claim_type_draft is None
            else ClaimDependencyDrafts(claim_type=claim_type_draft)
        ),
    )


def _lower(world: _World, payload: ClaimAuthoringPayload) -> tuple[dict[str, bytes], str]:
    intent = world.coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=payload,
        canonical_timestamp=_timestamp(world),
    ).intent
    lowered = lower_authoring(world.instance, intent=intent, actor_id="owner")
    return dict(lowered.proposed_tree), f"route-{world.claim_ordinal}"


def _exact_first_citation(world: _World) -> object:
    world.claim_ordinal += 1
    exact_type = _exact_type()
    payload = _observe_payload(
        world, EXACT_SOURCE, predicate=exact_type.predicate, claim_type_draft=exact_type
    )
    tree, name = _lower(world, payload)
    return _accept(world, tree, name=name)


def _family_first_citation(world: _World) -> object:
    world.claim_ordinal += 1
    source_id = f"repo.measured-family-{world.claim_ordinal}"
    payload = _observe_payload(world, source_id, predicate=_family_type().predicate)
    tree, name = _lower(world, payload)
    return _accept(world, tree, name=name)


def _multi_claim_first_citation(world: _World) -> object:
    """One change set, three Claims, each a first citation of its own new source.

    Review F1's regression repeated the whole-table enumeration once per
    Claim in the change; this proves the fixed, change-bounded build happens
    once, not three times, by holding the SAME flatness bound as a single
    Claim beside the unrelated contracts.
    """

    world.claim_ordinal += 1
    base_ordinal = world.claim_ordinal
    merged: dict[str, bytes] | None = None
    for offset in range(3):
        source_id = f"repo.measured-multi-{base_ordinal}-{offset}"
        payload = _observe_payload(
            world,
            source_id,
            predicate=_family_type().predicate,
            subject=_multi_subject(offset),
        )
        tree, _name = _lower(world, payload)
        merged = tree if merged is None else {**merged, **tree}
    assert merged is not None
    return _accept(world, merged, name=f"multi-{base_ordinal}")


ROUTES: dict[str, Callable[[_World], object]] = {
    "first_citation.exact": _exact_first_citation,
    "first_citation.family": _family_first_citation,
    "first_citation.multi_claim": _multi_claim_first_citation,
}


def _measure(session: _Session, run: Callable[[_World], object]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for name, world in session.worlds.items():
        print(f"measuring {name}", file=sys.stderr, flush=True)
        with counting(world.root) as window:
            run(world)
        assert window.unexplained == [], window.unexplained[:5]
        found[name] = {counter: window.counts[counter] for counter in COUNTED}
    return found


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Session]:
    root = tmp_path_factory.mktemp("family-first-citation-cost")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PlaybillInstance, "_start_advertiser_locked", lambda self: None)
        prefixes = _counters._prefixes  # noqa: SLF001
        patch.setattr(_counters, "_prefixes", lambda path: (*prefixes(path), *_DESCRIPTOR_ALIASES))
        with trace_sqlite(root):
            built = _Session(
                worlds={
                    "small": _build(root / "small", "small", SMALL),
                    "large": _build(root / "large", "large", LARGE),
                }
            )
            for route, run in ROUTES.items():
                print(f"route {route}", file=sys.stderr, flush=True)
                try:
                    built.measured[route] = _measure(built, run)
                except Exception as exc:  # noqa: BLE001 - reported by the route's cases
                    built.failed[route] = exc
            yield built


#: A first citation of a brand-new source, beside many unrelated already-accepted
#: CaptureContracts, has its own baseline cost unrelated to this fix (closure and
#: dependency-pin checks over the candidate's declared artifacts) -- present for
#: ``first_citation.exact`` too, which structurally can never reach
#: ``historical_capture_contract`` at all (``_policy_needs_verified_shape`` short
#: -circuits before it: no ``capture_contract_families`` entry in its policy).
#: So the right proof that review F1's fix removed the family-specific
#: enumeration is not "every route is flat in isolation" (the shared baseline
#: is not this fix's to flatten) -- it is that a family rule's own cost, and
#: its growth from 100 to 1,000 unrelated contracts, tracks the exact-rule
#: route's, not the unrelated-contract count. Before the fix, family's cost at
#: LARGE was ~N times exact's (the "102 vs 1,003 SQL statements" pattern);
#: after it, they move together.
PARITY_RATIO = 2.0
PARITY_SLACK = 64
GROWTH_SLACK_FACTOR = 1.5
#: Three Claims, each a first citation of its own new source, in one change:
#: review F1 found the old staging build repeated once per Claim, so a
#: multi-Claim change paid a multiple of the per-Claim cost on top of the
#: per-Claim regression itself. The fix computes it once per change; three
#: Claims should cost at most a linear multiple of one, same shape as the
#: per-change guard's own multi-item routes (``LINEAR`` in
#: ``test_pair_write_cost.py``).
MULTI_CLAIM_LINEAR = 1 + 2 * 1.5
MULTI_CLAIM_SLACK = 64


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("size", ("small", "large"))
def test_a_family_rules_first_citation_costs_about_what_an_exact_rules_does(
    session: _Session, size: str, counter: str
) -> None:
    """Family's own cost, at one size, tracks the exact-named route's -- not the
    unrelated-contract count. The old bug made family specifically, not exact,
    scale with it."""

    for route in ("first_citation.exact", "first_citation.family"):
        if route in session.failed:
            raise session.failed[route]
    exact = session.measured["first_citation.exact"][size][counter]
    family = session.measured["first_citation.family"][size][counter]
    assert family <= exact * PARITY_RATIO + PARITY_SLACK, (size, counter, exact, family)


@pytest.mark.parametrize("counter", COUNTED)
def test_a_family_rules_growth_from_100_to_1000_unrelated_contracts_tracks_exacts(
    session: _Session, counter: str
) -> None:
    """Family's SMALL-to-LARGE growth is no worse than exact's own growth.

    Flat against each other, even where neither route is flat in absolute
    terms against the (unrelated, out-of-scope) baseline cost of a first
    citation beside many already-accepted CaptureContracts.
    """

    for route in ("first_citation.exact", "first_citation.family"):
        if route in session.failed:
            raise session.failed[route]
    exact_small = session.measured["first_citation.exact"]["small"][counter]
    exact_large = session.measured["first_citation.exact"]["large"][counter]
    family_small = session.measured["first_citation.family"]["small"][counter]
    family_large = session.measured["first_citation.family"]["large"][counter]
    # A counter that starts at (or near) zero for the exact route has no
    # meaningful growth ratio to compare against; the parity test above
    # already bounds it directly.
    if exact_small <= 4:
        pytest.skip(f"{counter} is near zero for the exact route; see the parity test")
    exact_growth = exact_large / exact_small
    family_growth = family_large / max(family_small, 1)
    assert family_growth <= exact_growth * GROWTH_SLACK_FACTOR + 0.5, (
        counter,
        exact_small,
        exact_large,
        family_small,
        family_large,
    )


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("size", ("small", "large"))
def test_a_multi_claim_changes_cost_is_linear_in_its_claims_not_multiplied(
    session: _Session, size: str, counter: str
) -> None:
    """Three first citations in one change cost a linear multiple of one, at
    either size -- the staged-contract build runs once per change, not once
    per Claim (review F1's exact finding: "a multi-Claim change repeats the
    enumeration... once per Claim")."""

    for route in ("first_citation.family", "first_citation.multi_claim"):
        if route in session.failed:
            raise session.failed[route]
    one = session.measured["first_citation.family"][size][counter]
    multi = session.measured["first_citation.multi_claim"][size][counter]
    assert multi <= MULTI_CLAIM_LINEAR * one + MULTI_CLAIM_SLACK, (size, counter, one, multi)


@pytest.mark.parametrize("counter", COUNTED)
@pytest.mark.parametrize("route", tuple(ROUTES))
def test_the_counts_are_reported(session: _Session, route: str, counter: str) -> None:
    """Not a cost assertion -- always passes; ``-s`` prints every measured count
    for the review record (the baseline growth this file's docstring and
    comments explain is visible here too, named rather than hidden)."""

    if route in session.failed:
        raise session.failed[route]
    small = session.measured[route]["small"][counter]
    large = session.measured[route]["large"][counter]
    print(f"{route} {counter}: small={small} large={large}", file=sys.stderr)
