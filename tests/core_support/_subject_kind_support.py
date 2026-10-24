"""How a test world gets Subjects: core creates them (identity D1/D6).

Every new Subject must be of a kind an accepted SubjectKind declares, and must
carry core's assignment: its id is never chosen. A world that writes Subjects
straight into a candidate tree runs them through core first (``core_assigned``):
each is created by its id as its kind's slug key, so the world keeps readable
ids that core assigned.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any, TypeVar

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.subject_kinds import (
    SubjectKind,
    SubjectKindKeys,
    SubjectKindMinted,
    subject_kind_path,
)


def minted_subject_kind(kind: str) -> SubjectKind:
    return SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=kind),
        identity_rule=SubjectKindMinted(),
    )


def keyed_subject_kind(
    kind: str, keys: tuple[str, ...], *, uniqueness: str = "unique"
) -> SubjectKind:
    return SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=kind),
        identity_rule=SubjectKindKeys(keys=keys, uniqueness=uniqueness),  # type: ignore[arg-type]
    )


def core_assigned(
    instance: Any,
    updates: MutableMapping[str, bytes],
    *,
    base_tree: Mapping[str, bytes] | None = None,
    base: Any = None,
    timestamp: str = "2026-01-01T00:00:00.000000Z",
    actor_id: str = "owner",
) -> MutableMapping[str, bytes]:
    """Have core create each new Subject ``updates`` writes raw, by its slug (identity D1).

    A world that writes Subjects straight into a candidate tree gets them the
    way every route must: each kind is declared keyed by ``<kind>.slug``
    (unique) unless already declared with one key, and each Subject is created
    by its id as that key through the shared lowering, whose output -- the same
    Subject shell bytes, its key Claim, the declaration -- is merged into
    ``updates``. An id must be its own slug's rendering. A kind ``updates``
    declares itself, a minted or composite-keyed kind, a grandfathered kind and
    an instance on a compiler before SubjectKinds are left as written.
    """

    from cruxible_client.contracts.authoring.models import (
        ChangeSetAuthoringPayload,
        ClaimTypeAuthoringPayload,
        SubjectCreationAuthoringPayload,
        SubjectKindAuthoringPayload,
        authoring_member_identity,
    )
    from cruxible_client.contracts.claim_types import claim_type_path
    from cruxible_client.contracts.subject_kinds import SubjectKeyPart, live_subject_kind
    from cruxible_client.contracts.subjects import parse_subject
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.indexes.subject_identity import admits_subject_kinds
    from cruxible_core.proposals.proposals import AuthenticatedActor

    head = base if base is not None else instance.accepted_coordinate()
    if not admits_subject_kinds(head.compiler):
        return updates
    tree = base_tree if base_tree is not None else instance.tree_at(head.git_oid)
    new: dict[str, Any] = {}
    for path, content in updates.items():
        if (
            path.startswith("subjects/")
            and path.endswith(".json")
            and path not in tree
            and content is not None
        ):
            shell = parse_subject(content, path=path)
            if shell.lifecycle.state == "live" and shell.assignment is None:
                new[path] = shell
    held: set[tuple[str, str]] = set()
    if new:
        # A Subject core already created into ``updates`` holds its key Claim there.
        from cruxible_client.contracts.claims import parse_claim

        for path, content in updates.items():
            if path.startswith("claims/") and content is not None and path not in tree:
                statement = parse_claim(content, path=path).statement
                held.add((statement.subject.artifact_path, statement.predicate))
    members: list[Any] = []
    for kind in sorted({shell.subject_kind for shell in new.values()}):
        if subject_kind_path(kind) in updates and updates[subject_kind_path(kind)] != tree.get(
            subject_kind_path(kind)
        ):
            continue  # the world declares the kind itself
        declared = live_subject_kind(tree, kind)
        if declared is None:
            if subject_kind_path(kind) in tree:
                continue
            predicate = f"{kind}.slug"
            if claim_type_path(predicate) not in tree and claim_type_path(predicate) not in updates:
                slug = slug_claim_type(kind)
                from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER

                if head.compiler != LIGHT_ARTIFACTS_COMPILER:
                    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicyV3

                    policy = slug.evidence_admission_policy.model_dump(mode="json")
                    policy["tag"] = "playbill-claim-evidence-admission-policy-v3"
                    for rule in policy["rules"]:
                        rule["evidence_kinds"] = ["self_asserted"]
                        for ref in rule["capture_contracts"]:
                            ref.pop("logical_sources", None)
                    legacy_policy = ClaimEvidenceAdmissionPolicyV3.model_validate(policy)
                    slug = slug.model_copy(update={"evidence_admission_policy": legacy_policy})
                members.append(ClaimTypeAuthoringPayload(claim_type=slug))
            members.append(
                SubjectKindAuthoringPayload(name=kind, keys=(predicate,), uniqueness="unique")
            )
        elif declared.minted or len(declared.keys) != 1:
            continue
        else:
            predicate = declared.keys[0]
        for path, shell in new.items():
            if shell.subject_kind == kind and (path, predicate) not in held:
                members.append(
                    SubjectCreationAuthoringPayload(
                        subject_kind=kind,
                        key=(SubjectKeyPart(predicate=predicate, value=shell.subject_id),),
                        rationale=f"{kind}/{shell.subject_id} is named by its slug.",
                    )
                )
    if not members:
        return updates
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        )
    )
    # Lowered as a submit lowers it, from a stored intent: the key Claims cite the
    # author's own words, whose Capture names that intent as its producer.
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id=actor_id)
    created = coordinator.create(actor=actor, payload=payload, canonical_timestamp=timestamp)
    with instance.capture_verification_request():
        computed, _bound = coordinator._compute_and_bind_preflight(  # noqa: SLF001
            created.intent.intent_id, actor=actor
        )
    assert computed.lowered is not None, computed.result.frontier
    for path, content in computed.lowered.changed_members:
        if path.startswith("subjects/"):
            assert path in new, f"core names this Subject {path}, not as the world wrote it"
        updates[path] = content
    return updates


@contextmanager
def history_before_identity(compiler: object | None) -> Iterator[None]:
    """Build history an older build accepted, on a compiler before Subject identity.

    An instance pinned to a compiler before SubjectKinds creates no new Subject
    (the boundary refuses it, review F-005); a world modelling such an instance
    holds Subjects only because a build before the rule accepted them. Inside
    this context the candidate boundary's identity check is that older build's:
    none. On a compiler that admits SubjectKinds it changes nothing, unless
    ``compiler`` is None: a ledger builder that writes history as earlier builds
    wrote it, whatever compiler it is pinned to.
    """

    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_core.indexes.subject_identity import admits_subject_kinds

    if compiler is not None and admits_subject_kinds(compiler):  # type: ignore[arg-type]
        yield
        return
    original = proposals_module.boundary_identity_issues
    proposals_module.boundary_identity_issues = lambda *_a, **_k: ()  # type: ignore[assignment]
    try:
        yield
    finally:
        proposals_module.boundary_identity_issues = original


_F = TypeVar("_F", bound=Callable[..., Any])


def world_history(function: _F) -> _F:
    """Run a world-building helper as history on an instance before Subject identity.

    On an instance pinned to a compiler before SubjectKinds, the world's Subjects
    are history an older build accepted (``history_before_identity``); on any
    other, nothing changes and they go through core (``core_assigned``).
    """

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        instance = kwargs.get("instance", args[0] if args else None)
        compiler = instance.accepted_coordinate().compiler  # type: ignore[union-attr]
        with history_before_identity(compiler):
            return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def declare_created_kinds(
    base_tree: Mapping[str, bytes],
    updates: MutableMapping[str, bytes],
    *,
    instance: Any,
    base: Any = None,
    compiler: object | None = None,
    actor_id: str = "owner",
) -> MutableMapping[str, bytes]:
    """``core_assigned`` over ``base_tree`` (the tree the change is proposed against)."""

    del compiler  # the base's compiler decides
    return core_assigned(instance, updates, base_tree=base_tree, base=base, actor_id=actor_id)


__all__ = [
    "bare_slug_claim_type",
    "create_by_slug_input",
    "declare_by_slug",
    "create_by_key",
    "key_by_slug",
    "slug_claim_type",
    "declare_created_kinds",
    "core_assigned",
    "history_before_identity",
    "world_history",
    "keyed_subject_kind",
    "minted_subject_kind",
]


SLUG_FORM = ("nfkc", "trim", "collapse_whitespace", "casefold")


def slug_claim_type(kind: str) -> object:
    """The ``<kind>.slug`` key ClaimType: a one-valued v8 string backed by own words."""

    from tests.core_support._identity_support import identity_claim_type

    return identity_claim_type(
        "slug",
        canonical_form=SLUG_FORM,
        literal_schema={"minLength": 1, "type": "string"},
        predicate=f"{kind}.slug",
        subject_kinds=(kind,),
    )


def key_by_slug(instance: object, kind: str, *, timestamp: str, actor_id: str = "owner") -> None:
    """Accept ``<kind>.slug`` and a SubjectKind keying ``kind`` by it, uniquely.

    A world whose Subjects exist already (seeded under a minted declaration)
    keeps them; from here on a new Subject of the kind is created by its slug,
    and its id is the slug's rendering.
    """

    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectKindInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.subject_kinds import live_subject_kind
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.core_support._support import client_material
    from tests.test_ledger.test_activation import _sign

    head = instance.accepted_coordinate()  # type: ignore[attr-defined]
    held = live_subject_kind(instance.tree_at(head.git_oid), kind)  # type: ignore[attr-defined]
    if held is not None and held.keys == (f"{kind}.slug",) and held.unique:
        return  # core already keyed the kind by its slug when it created its Subjects
    payload = lower_authoring_input(
        ChangeSetInput(
            kind="change_set",
            members=(
                ClaimTypeInput(kind="claim_type", claim_type=slug_claim_type(kind)),  # type: ignore[arg-type]
                SubjectKindInput(
                    kind="subject_kind", name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
                ),
            ),
        )
    )
    actor = AuthenticatedActor(actor_id=actor_id)
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(  # type: ignore[arg-type]
        actor=actor, payload=payload, canonical_timestamp=timestamp
    )
    proposal_id = submitted.status.proposal_id
    assert proposal_id is not None, submitted.intent.last_preflight
    if submitted.status.candidate_digest is not None:
        try:
            approval = _sign(
                client_material(instance.root.parent, instance),  # type: ignore[attr-defined]
                submitted.status.candidate_digest,
                instance.accepted_coordinate().semantic_root,  # type: ignore[attr-defined]
            )
            service_submit_playbill_approval(
                instance,  # type: ignore[arg-type]
                proposal_id=proposal_id,
                attestation=approval.attestation,
                authenticated_submitter=actor_id,
            )
        except Exception:  # noqa: BLE001 - a world without an independent reviewer key
            pass
    receipt = service_activate_playbill_proposal(
        instance,  # type: ignore[arg-type]
        proposal_id=proposal_id,
        activated_by=actor_id,
    )
    assert receipt.status == "accepted", receipt


def create_by_key(kind: str, slug: str, *, rationale: str | None = None) -> object:
    """The change-set member that creates ``kind/<rendered slug>`` by its slug key."""

    from cruxible_client.contracts.authoring.models import SubjectCreationAuthoringPayload
    from cruxible_client.contracts.subject_kinds import SubjectKeyPart

    return SubjectCreationAuthoringPayload(
        subject_kind=kind,
        key=(SubjectKeyPart(predicate=f"{kind}.slug", value=slug),),
        rationale=rationale,
    )


def bare_slug_claim_type(kind: str) -> object:
    """``<kind>.slug`` as the shipped example spells it: no evidence rules to resolve.

    For worlds that author their vocabulary through a public surface and hold no
    coordinator self-source CaptureContract to name.
    """

    from cruxible_client.authoring.examples import subject_kind_example
    from cruxible_client.contracts.artifacts import ArtifactIdentity as _Identity

    template = subject_kind_example().members[0].claim_type  # type: ignore[union-attr]
    return template.model_copy(
        update={
            "identity": _Identity(kind="ClaimType", name=f"{kind}.slug"),
            "predicate": f"{kind}.slug",
            "allowed_subject_kinds": (kind,),
            "description": f"The {kind}'s readable name; its key.",
        }
    )


def declare_by_slug(draft: object, *kinds: str) -> None:
    """Add, to an SDK change set, each kind's slug key ClaimType and its keyed SubjectKind."""

    for kind in kinds:
        draft.claim_type(bare_slug_claim_type(kind))  # type: ignore[attr-defined]
        draft.subject_kind(  # type: ignore[attr-defined]
            name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
        )


def create_by_slug_input(kind: str, slug: str, *, declare: bool = False) -> object:
    """The public authoring input creating ``kind/<rendered slug>`` by its slug.

    With ``declare``, the same change also accepts ``<kind>.slug`` (as the shipped
    example spells it) and a SubjectKind keying ``kind`` by it, uniquely: the
    shape a CLI or HTTP world authors once before its first Subject.
    """

    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectInput,
        SubjectKindInput,
    )

    creation = SubjectInput(kind="subject", subject_kind=kind, key=slug)
    if not declare:
        return creation
    return ChangeSetInput(
        kind="change_set",
        members=(
            ClaimTypeInput(kind="claim_type", claim_type=bare_slug_claim_type(kind)),  # type: ignore[arg-type]
            SubjectKindInput(
                kind="subject_kind", name=kind, keys=(f"{kind}.slug",), uniqueness="unique"
            ),
            creation,
        ),
    )


def fixture_claim_paths(tree: Mapping[str, bytes]) -> list[str]:
    """The tree's Claim paths in path order, without the key Claims core writes on creation.

    A world's Subjects are created by their slug (``core_assigned``), so each holds
    a key Claim beside whatever the world states about it; a test reading "the
    world's Claims" means those it stated.
    """

    from cruxible_client.contracts.claims import parse_claim

    keys = {
        predicate
        for path, content in tree.items()
        if path.startswith("subject-kinds/") and path.endswith(".json")
        for predicate in _declared_keys(content, path)
    }
    return sorted(
        path
        for path, content in tree.items()
        if path.startswith("claims/")
        and parse_claim(content, path=path).statement.predicate not in keys
    )


def _declared_keys(content: bytes, path: str) -> tuple[str, ...]:
    from cruxible_client.contracts.subject_kinds import parse_subject_kind

    return parse_subject_kind(content, path=path).keys


def fixture_key_claims(tree: Mapping[str, bytes]) -> list[str]:
    """The key Claim paths core wrote when it created the world's Subjects."""

    stated = set(fixture_claim_paths(tree))
    return sorted(path for path in tree if path.startswith("claims/") and path not in stated)


def _is_key(item: Any) -> bool:
    predicate = getattr(item, "predicate", None)
    statement = getattr(item, "statement", None)
    if predicate is None and statement is not None:
        predicate = getattr(statement, "predicate", None)
    facts = getattr(item, "facts", None)
    if predicate is None and isinstance(facts, tuple):
        return any(_is_key(fact.get("value")) for fact in facts if isinstance(fact, Mapping))
    if predicate is None and isinstance(item, Mapping):
        predicate = item.get("predicate")
        if predicate is None and isinstance(item.get("statement"), Mapping):
            predicate = item["statement"].get("predicate")
    return isinstance(predicate, str) and (predicate == "slug" or predicate.endswith(".slug"))


def without_keys(items: Any) -> list[Any]:
    """Rows, cards or Claims without the slug key Claims core writes on creation."""

    return [item for item in items if not _is_key(item)]
