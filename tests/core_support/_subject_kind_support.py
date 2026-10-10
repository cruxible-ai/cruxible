"""Declare the Subject kinds a test world creates Subjects of (identity D6).

Every new Subject must be of a kind an accepted SubjectKind declares. A world
that writes Subjects straight into a candidate tree declares each such kind as
``minted`` in the same change: core never re-derives an existing id, and a
minted kind's ids are not read for a rule at candidate evaluation, so the
world's own Subject ids stand.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.subject_kinds import (
    SubjectKind,
    SubjectKindKeys,
    SubjectKindMinted,
    render_subject_kind,
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


def declare_created_kinds(
    base_tree: Mapping[str, bytes],
    updates: MutableMapping[str, bytes],
    *,
    compiler: object | None = None,
) -> MutableMapping[str, bytes]:
    """Add a minted SubjectKind for each kind ``updates`` creates a Subject of, if undeclared.

    ``updates`` may be a whole candidate tree or only the changed members; a
    kind already declared in ``base_tree`` or ``updates`` is left as it is. A
    world on a compiler that admits no SubjectKind (``compiler``) declares none:
    it creates Subjects as it always did.
    """

    from cruxible_core.indexes.subject_identity import admits_subject_kinds

    if compiler is not None and not admits_subject_kinds(compiler):  # type: ignore[arg-type]
        return updates
    for path in list(updates):
        if not path.startswith("subjects/") or path in base_tree:
            continue
        kind = path.removeprefix("subjects/").partition("/")[0]
        declared = subject_kind_path(kind)
        if declared not in updates and declared not in base_tree:
            updates[declared] = render_subject_kind(minted_subject_kind(kind))
    return updates


def declared_kind_files(*kinds: str) -> dict[str, bytes]:
    """Minted SubjectKind files for ``kinds``, at their ledger paths."""

    return {
        subject_kind_path(kind): render_subject_kind(minted_subject_kind(kind)) for kind in kinds
    }


__all__ = [
    "bare_slug_claim_type",
    "create_by_slug_input",
    "declare_by_slug",
    "create_by_key",
    "key_by_slug",
    "slug_claim_type",
    "declare_created_kinds",
    "declared_kind_files",
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
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.core_support._support import client_material
    from tests.test_ledger.test_activation import _sign

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
