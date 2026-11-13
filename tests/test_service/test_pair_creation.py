"""Pair Subjects created through authoring and the write verbs (slice S2).

A pair kind is keyed by its ends (Subject-valued key parts). A write names an
end as ``kind/id`` (an id, alias or single key) or ``{created: label}`` (R7);
authoring names it as a Subject object (the Subject, its creation member, or
its key). Ends are named before their pairs, layer by layer, in the one
creation order lowering and the candidate boundary share; every case here is
accepted through admission and activation, so the boundary's re-derivation
agrees with lowering's ids in each member order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import SubjectInput, SubjectObjectInput
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    SubjectCreationAuthoringPayload,
    authoring_member_identity,
)
from cruxible_client.contracts.claims import SubjectClaimObject, parse_claim
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subject_kinds import SubjectKeyPart, SubjectKeyReference
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._pair_world import (
    ADVISORY,
    APP_EXPOSURE,
    BOOT,
    COMPONENT,
    EXPOSURE,
    HOST,
    INVENTORY,
    LABEL,
    OCCURRENCE,
    RUN,
    TAG,
    TAGGING,
    VULN,
    accept,
    accepted,
    change_set,
    create,
    ends_of,
    head_tree,
    live_subjects,
    pair_world,
    write,
)

PURL = "pkg:pypi/requests@2.31.0"
C1 = f"{COMPONENT}/pkg-pypi-requests-2.31.0"
V1 = f"{VULN}/cve-2024-35195"
E11 = f"{EXPOSURE}/pkg-pypi-requests-2.31.0--cve-2024-35195"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return pair_world(tmp_path)


def _refusal(outcome: Any) -> Any:
    assert outcome.status == "refused", outcome
    assert outcome.refusal is not None
    return outcome.refusal


def _codes(outcome: Any) -> set[str]:
    refusal = _refusal(outcome)
    return {refusal.code, *(item.code for item in getattr(refusal, "diagnostics", ()) or ())}


def _ends(instance: PlaybillInstance) -> None:
    accepted(
        write(
            instance,
            create("c", COMPONENT, PURL),
            create("v", VULN, "CVE-2024-35195"),
        )
    )


# -- the write verbs: R7 spelling ------------------------------------------------------


@pytest.mark.parametrize("pair_first", [True, False], ids=["pair-first", "ends-first"])
def test_one_write_creates_the_ends_and_their_pair(
    instance: PlaybillInstance, pair_first: bool
) -> None:
    pair = create("e", EXPOSURE, {"component": {"created": "c"}, "vulnerability": {"created": "v"}})
    ends = [create("c", COMPONENT, PURL), create("v", VULN, "CVE-2024-35195")]
    outcome = accepted(write(instance, *([pair, *ends] if pair_first else [*ends, pair])))
    assert sorted(item.subject for item in outcome.changes) == sorted([C1, V1, E11])
    assert ends_of(instance, E11) == {
        f"{EXPOSURE}.component": C1,
        f"{EXPOSURE}.vulnerability": V1,
    }
    # The key Claims are links: Subject objects naming the ends' paths.
    tree = head_tree(instance)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        identities = [
            str(row[0]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=?",
                (subject_path(EXPOSURE, E11.split("/")[1]),),
            )
        ]
    objects = {
        parse_claim(
            tree[f"claims/{item[4:6]}/{item}.json"], path=f"claims/{item[4:6]}/{item}.json"
        ).statement.object.address.artifact_path  # type: ignore[union-attr]
        for item in identities
    }
    assert objects == {subject_path(*C1.split("/")), subject_path(*V1.split("/"))}


def test_a_pair_of_existing_ends_is_named_by_text_and_lands_on_itself(
    instance: PlaybillInstance,
) -> None:
    _ends(instance)
    # The component by its key text, the vulnerability by id with '@': R7.
    first = accepted(
        write(
            instance,
            create("e", EXPOSURE, {"component": f"{COMPONENT}/{PURL}", "vulnerability": f"@{V1}"}),
        )
    )
    assert [(item.subject, item.resolved) for item in first.changes] == [(E11, "created")]
    again = accepted(write(instance, create("e", EXPOSURE, {"component": C1, "vulnerability": V1})))
    assert [(item.subject, item.resolved) for item in again.changes] == [(E11, "existing")]
    assert live_subjects(instance, EXPOSURE) == [E11]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        (
            [create("e", EXPOSURE, {"component": {"created": "e"}, "vulnerability": V1})],
            "cruxible.write.key_part_self",
        ),
        (
            [
                create("a", OCCURRENCE, {"inventory": {"created": "b"}, "component": C1}),
                create("b", OCCURRENCE, {"inventory": {"created": "a"}, "component": C1}),
            ],
            "cruxible.write.key_part_self",
        ),
        (
            [create("e", EXPOSURE, {"component": V1, "vulnerability": V1})],
            "cruxible.write.value_kind_not_admitted",
        ),
        (
            [
                create(
                    "v2",
                    VULN,
                    "CVE-2",
                ),
                create("e", EXPOSURE, {"component": {"created": "v2"}, "vulnerability": V1}),
            ],
            "cruxible.write.value_kind_not_admitted",
        ),
        (
            [create("e", EXPOSURE, {"component": f"{COMPONENT}/nope", "vulnerability": V1})],
            "cruxible.write.value_subject_not_found",
        ),
        (
            [create("e", EXPOSURE, {"component": "requests", "vulnerability": V1})],
            "cruxible.write.value_type_mismatch",
        ),
        (
            [
                create("c2", COMPONENT, "pkg:npm/x@1"),
                create(
                    "i", INVENTORY, [{"predicate": f"{INVENTORY}.name", "value": {"created": "c2"}}]
                ),
            ],
            "cruxible.write.value_type_mismatch",
        ),
        (
            [create("e", EXPOSURE, {"component": {"created": "ghost"}, "vulnerability": V1})],
            "cruxible.write.label_unknown",
        ),
    ],
    ids=[
        "self",
        "cycle",
        "wrong-kind-ref",
        "wrong-kind-created",
        "missing-end",
        "not-a-ref",
        "created-literal-part",
        "unknown-label",
    ],
)
def test_a_pair_create_refuses_ends_it_cannot_name(
    instance: PlaybillInstance, changes: list[dict[str, Any]], code: str
) -> None:
    _ends(instance)
    before = instance.accepted_coordinate().git_oid
    assert _refusal(write(instance, *changes)).code == code
    assert instance.accepted_coordinate().git_oid == before


def test_a_pair_is_never_named_or_created_by_bare_text(instance: PlaybillInstance) -> None:
    accepted(write(instance, create("h", HOST)))
    # A one-part pair kind has no single key reading: kind/<text> is no create.
    refused = write(
        instance,
        {"op": "set", "subject": f"{RUN}/whatever", "field": "host", "value": f"{HOST}/x"},
    )
    assert _refusal(refused).code == "cruxible.write.subject_not_found"


def test_a_pair_to_a_retired_end_refuses(instance: PlaybillInstance) -> None:
    _ends(instance)
    accepted(write(instance, {"op": "retire", "target": V1}))
    refused = write(instance, create("e", EXPOSURE, {"component": C1, "vulnerability": V1}))
    assert "cruxible.subject.key_endpoint_retired" in _codes(refused), refused


def test_an_endpoint_is_never_revised_through_a_write(instance: PlaybillInstance) -> None:
    _ends(instance)
    accepted(write(instance, create("e", EXPOSURE, {"component": C1, "vulnerability": V1})))
    accepted(write(instance, create("v", VULN, "CVE-9")))
    refused = write(
        instance,
        {"op": "set", "subject": E11, "field": "vulnerability", "value": f"{VULN}/cve-9"},
    )
    assert refused.status == "refused"
    assert ends_of(instance, E11)[f"{EXPOSURE}.vulnerability"] == V1


# -- member tokens: distinct ends never share a pair; one end named twice is one ---------


def _minted_runs(pair_kind: str, *, pair_first: bool) -> list[dict[str, Any]]:
    hosts = [create("h1", HOST), create("h2", HOST)]
    pairs = [
        create("r1", pair_kind, {"host": {"created": "h1"}}),
        create("r2", pair_kind, {"host": {"created": "h2"}}),
    ]
    return [*pairs, *hosts] if pair_first else [*hosts, *pairs]


@pytest.mark.parametrize("pair_first", [True, False], ids=["pair-first", "ends-first"])
def test_two_minted_ends_each_take_their_own_pair_in_a_write(
    instance: PlaybillInstance, pair_first: bool
) -> None:
    outcome = accepted(write(instance, *_minted_runs(RUN, pair_first=pair_first)))
    hosts = live_subjects(instance, HOST)
    runs = live_subjects(instance, RUN)
    assert len(hosts) == 2 and len(runs) == 2
    assert {ends_of(instance, run)[f"{RUN}.host"] for run in runs} == set(hosts)
    # Each run lands where its own host does: r1 on h1's, r2 on h2's.
    by_label = dict(zip(["h1", "h2", "r1", "r2"], _labelled(outcome, pair_first), strict=True))
    assert ends_of(instance, by_label["r1"])[f"{RUN}.host"] == by_label["h1"]
    assert ends_of(instance, by_label["r2"])[f"{RUN}.host"] == by_label["h2"]


def _labelled(outcome: Any, pair_first: bool) -> list[str]:
    subjects = [item.subject for item in outcome.changes]
    return [*subjects[2:], *subjects[:2]] if pair_first else subjects


@pytest.mark.parametrize("pair_first", [True, False], ids=["pair-first", "ends-first"])
def test_two_further_holders_of_a_hint_end_each_take_their_own_pair_in_a_write(
    instance: PlaybillInstance, pair_first: bool
) -> None:
    _ends(instance)
    tags = [
        create("t1", TAG, "triage", create_new=True),
        create("t2", TAG, "triage", create_new=True),
    ]
    pairs = [
        create("p1", TAGGING, {"tag": {"created": "t1"}, "component": C1}),
        create("p2", TAGGING, {"tag": {"created": "t2"}, "component": C1}),
    ]
    accepted(write(instance, *([*pairs, *tags] if pair_first else [*tags, *pairs])))
    held = live_subjects(instance, TAG)
    taggings = live_subjects(instance, TAGGING)
    assert len(held) == 2 and len(taggings) == 2
    assert {ends_of(instance, item)[f"{TAGGING}.tag"] for item in taggings} == set(held)


@pytest.mark.parametrize("pair_first", [True, False], ids=["pair-first", "ends-first"])
def test_one_keyed_end_named_by_two_creates_has_one_pair_in_a_write(
    instance: PlaybillInstance, pair_first: bool
) -> None:
    accepted(write(instance, create("v", VULN, "CVE-2024-35195")))
    ends = [create("c1", COMPONENT, PURL), create("c2", COMPONENT, f"  {PURL.upper()} ")]
    pairs = [
        create("e1", EXPOSURE, {"component": {"created": "c1"}, "vulnerability": V1}),
        create("e2", EXPOSURE, {"component": {"created": "c2"}, "vulnerability": V1}),
    ]
    outcome = accepted(write(instance, *([*pairs, *ends] if pair_first else [*ends, *pairs])))
    assert {item.subject for item in outcome.changes} == {C1, E11}
    assert live_subjects(instance, EXPOSURE) == [E11]


# -- authoring: the same cases as creation members, in both member orders ---------------


def _creation(kind: str, key: dict[str, Any] | None = None, *, why: str = "") -> Any:
    return SubjectCreationAuthoringPayload(
        subject_kind=kind,
        key=tuple(
            SubjectKeyPart(predicate=f"{kind}.{name}", value=value)
            for name, value in (key or {}).items()
        ),
        rationale=f"Created for the pair tests{why}.",
    )


def _member_of(creation: Any) -> SubjectClaimObject:
    kind, _, digest = creation.member_identity.removeprefix("SubjectCreation:").rpartition("@")
    return SubjectClaimObject(address=SemanticAddress.subject_creation(kind, digest))


def _whole(ref: str) -> SubjectClaimObject:
    return SubjectClaimObject(address=SemanticAddress.whole_artifact(subject_path(*ref.split("/"))))


def _authored(*members: Any) -> ChangeSetAuthoringPayload:
    return ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda item: authoring_member_identity(item).encode("utf-8"))
        ),
        rationale="Pairs by authoring.",
    )


def _order(payload: ChangeSetAuthoringPayload, pair_kind: str, end_kind: str) -> str:
    kinds = [getattr(member, "subject_kind", None) for member in payload.members]
    return "pair-first" if kinds.index(pair_kind) < kinds.index(end_kind) else "ends-first"


@pytest.mark.parametrize(("pair_kind", "order"), [(BOOT, "pair-first"), (RUN, "ends-first")])
def test_two_minted_ends_each_take_their_own_pair_by_authoring(
    instance: PlaybillInstance, pair_kind: str, order: str
) -> None:
    h1, h2 = _creation(HOST, why=" (one)"), _creation(HOST, why=" (two)")
    payload = _authored(
        h1,
        h2,
        _creation(pair_kind, {"host": _member_of(h1)}),
        _creation(pair_kind, {"host": _member_of(h2)}),
    )
    assert _order(payload, pair_kind, HOST) == order
    accept(instance, payload)
    hosts, pairs = live_subjects(instance, HOST), live_subjects(instance, pair_kind)
    assert len(hosts) == 2 and len(pairs) == 2
    assert {ends_of(instance, item)[f"{pair_kind}.host"] for item in pairs} == set(hosts)


@pytest.mark.parametrize(("pair_kind", "order"), [(LABEL, "pair-first"), (TAGGING, "ends-first")])
def test_two_further_holders_of_a_hint_end_each_take_their_own_pair_by_authoring(
    instance: PlaybillInstance, pair_kind: str, order: str
) -> None:
    _ends(instance)
    t1 = _creation(TAG, {"name": "triage"}, why=" (one)").model_copy(update={"create_new": True})
    t2 = _creation(TAG, {"name": "triage"}, why=" (two)").model_copy(update={"create_new": True})
    payload = _authored(
        t1,
        t2,
        _creation(pair_kind, {"tag": _member_of(t1), "component": _whole(C1)}),
        _creation(pair_kind, {"tag": _member_of(t2), "component": _whole(C1)}),
    )
    assert _order(payload, pair_kind, TAG) == order
    accept(instance, payload)
    held, pairs = live_subjects(instance, TAG), live_subjects(instance, pair_kind)
    assert len(held) == 2 and len(pairs) == 2
    assert {ends_of(instance, item)[f"{pair_kind}.tag"] for item in pairs} == set(held)


@pytest.mark.parametrize(
    ("pair_kind", "order"), [(ADVISORY, "pair-first"), (EXPOSURE, "ends-first")]
)
def test_one_keyed_end_named_by_two_members_has_one_pair_by_authoring(
    instance: PlaybillInstance, pair_kind: str, order: str
) -> None:
    accepted(write(instance, create("v", VULN, "CVE-2024-35195")))
    c1 = _creation(COMPONENT, {"purl": PURL}, why=" (one)")
    c2 = _creation(COMPONENT, {"purl": f"  {PURL.upper()} "}, why=" (two)")
    payload = _authored(
        c1,
        c2,
        _creation(pair_kind, {"component": _member_of(c1), "vulnerability": _whole(V1)}),
        _creation(pair_kind, {"component": _member_of(c2), "vulnerability": _whole(V1)}),
    )
    assert _order(payload, pair_kind, COMPONENT) == order
    accept(instance, payload)
    pairs = live_subjects(instance, pair_kind)
    assert pairs == [f"{pair_kind}/pkg-pypi-requests-2.31.0--cve-2024-35195"]
    assert live_subjects(instance, COMPONENT) == [C1]


def test_an_input_change_set_names_a_new_end_by_its_key(instance: PlaybillInstance) -> None:
    """The JSON input names a same-change end by key: the one its subject member creates."""

    accepted(write(instance, create("v", VULN, "CVE-2024-35195")))
    accept(
        instance,
        change_set(
            SubjectInput(kind="subject", subject_kind=COMPONENT, key={"purl": PURL}),
            SubjectInput(
                kind="subject",
                subject_kind=EXPOSURE,
                key={
                    "component": SubjectObjectInput(
                        kind="subject",
                        subject=SubjectKeyReference(subject_kind=COMPONENT, key=PURL),
                    ),
                    "vulnerability": SubjectObjectInput(kind="subject", subject=V1),
                },
            ),
        ),
    )
    assert ends_of(instance, E11) == {f"{EXPOSURE}.component": C1, f"{EXPOSURE}.vulnerability": V1}


def test_an_end_named_by_a_key_the_change_moves_refuses(instance: PlaybillInstance) -> None:
    """``identity_reference_unstable``: a key part frozen at the base must name the same end."""

    from tests.core_support._pair_world import submit

    _ends(instance)
    claim_id = next(
        identity
        for identity, predicate in _claims(instance, C1)
        if predicate == f"{COMPONENT}.purl"
    )
    revision = _purl_revision(instance, claim_id, "pkg:pypi/requests@9")
    payload = _authored(
        revision,
        _creation(
            EXPOSURE,
            {
                "component": SubjectClaimObject(
                    address=SemanticAddress.subject_key(COMPONENT, PURL)
                ),
                "vulnerability": _whole(V1),
            },
        ),
    )
    submitted = submit(instance, payload)
    assert submitted.status.proposal_id is None
    codes = {item.code for item in submitted.intent.last_preflight.frontier.diagnostics}
    assert "cruxible.authoring.identity_reference_unstable" in codes, codes


def _claims(instance: PlaybillInstance, ref: str) -> list[tuple[str, str]]:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return [
            (str(identity).removeprefix("Claim:"), str(predicate))
            for identity, predicate in projection.typed.connection.execute(
                "SELECT identity, predicate FROM claims WHERE subject_path=? AND lifecycle='live'",
                (subject_path(*ref.split("/")),),
            )
        ]


def _purl_revision(instance: PlaybillInstance, claim_id: str, value: str) -> Any:
    import base64

    from cruxible_client.contracts.authoring.models import (
        AuthoringClaimStatement,
        ClaimAuthoringPayload,
        SelfSourceBody,
    )
    from cruxible_client.contracts.claims import LiteralClaimObject

    return ClaimAuthoringPayload(
        statement=AuthoringClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(*C1.split("/"))),
            predicate=f"{COMPONENT}.purl",
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale="Re-keyed.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"Re-keyed.").decode("ascii")),
        revises=claim_id,
    )


# -- retiring an end with live pairs: refuse, list the pairs, retire them in one write (R5) --

INV = f"{INVENTORY}/web"


def _sbom(instance: PlaybillInstance) -> tuple[str, str]:
    """An inventory holding C1 (an occurrence with a scope) and an app-level judgment of V1."""

    _ends(instance)
    made = accepted(
        write(
            instance,
            create("i", INVENTORY, "web"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": C1}),
            {"op": "set", "subject": {"created": "o"}, "field": "scope", "value": "required"},
            create("a", APP_EXPOSURE, {"occurrence": {"created": "o"}, "vulnerability": V1}),
            {
                "op": "set",
                "subject": {"created": "a"},
                "field": "justification",
                "value": "vulnerable_code_not_in_execute_path",
            },
        )
    )
    occurrence, app = made.changes[1].subject, made.changes[3].subject
    assert occurrence == f"{OCCURRENCE}/web--pkg-pypi-requests-2.31.0"
    assert app == f"{APP_EXPOSURE}/web--pkg-pypi-requests-2.31.0--cve-2024-35195"
    return occurrence, app


def test_retiring_an_end_with_live_pairs_refuses_and_lists_them(
    instance: PlaybillInstance,
) -> None:
    occurrence, app = _sbom(instance)
    refusal = _refusal(write(instance, {"op": "retire", "target": INV, "with_claims": True}))
    assert refusal.code == "cruxible.write.subject_referenced"
    assert refusal.candidates == (occurrence,)
    assert "end of a live pair" in refusal.message
    # The repair retires the pair with its Claims; an endpoint is never re-pointed.
    assert refusal.repair == (
        "Retire the pair(s) with their Claims in this write: "
        f'cruxible_retire(target="{occurrence}", with_claims=true, reason="superseded")'
    )
    # The occurrence is itself the end of the app-level pair.
    deeper = _refusal(
        write(
            instance,
            {"op": "retire", "target": INV, "with_claims": True},
            {"op": "retire", "target": occurrence, "with_claims": True},
        )
    )
    assert deeper.code == "cruxible.write.subject_referenced"
    assert deeper.candidates == (app,)


def test_one_write_retires_an_end_and_every_pair_on_it(instance: PlaybillInstance) -> None:
    occurrence, app = _sbom(instance)
    # The end first: the planner sees the pairs this same write retires.
    accepted(
        write(
            instance,
            {"op": "retire", "target": INV, "with_claims": True},
            {"op": "retire", "target": occurrence, "with_claims": True},
            {"op": "retire", "target": app, "with_claims": True},
        )
    )
    assert live_subjects(instance, INVENTORY) == []
    assert live_subjects(instance, OCCURRENCE) == []
    assert live_subjects(instance, APP_EXPOSURE) == []
    assert live_subjects(instance, COMPONENT) == [C1]
    assert _claims(instance, app) == [] and _claims(instance, occurrence) == []


# -- the security judgment: one write, atomic -------------------------------------------


def _affected(instance: PlaybillInstance) -> str:
    _ends(instance)
    accepted(write(instance, {"op": "add", "subject": C1, "field": "affected_by", "value": V1}))
    (claim_id,) = [
        item for item, predicate in _claims(instance, C1) if predicate.endswith("affected_by")
    ]
    return claim_id


def _judgment(claim_id: str, justification: str) -> list[dict[str, Any]]:
    return [
        {"op": "retire", "target": claim_id, "reason": "was-wrong"},
        {"op": "add", "subject": C1, "field": "affected_by", "value": {"none": V1}},
        create("e", EXPOSURE, {"component": C1, "vulnerability": V1}),
        {
            "op": "set",
            "subject": {"created": "e"},
            "field": "justification",
            "value": justification,
        },
    ]


def _affected_values(instance: PlaybillInstance) -> list[tuple[str, str | None]]:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return [
            (str(kind), None if path is None else str(path))
            for kind, path in projection.typed.connection.execute(
                "SELECT object_kind, object_path FROM claims WHERE subject_path=? "
                "AND predicate=? AND lifecycle='live' ORDER BY object_kind",
                (subject_path(*C1.split("/")), f"{COMPONENT}.affected_by"),
            )
        ]


@pytest.mark.parametrize("pair_exists", [False, True], ids=["new-pair", "existing-pair"])
def test_the_package_judgment_lands_in_one_write(
    instance: PlaybillInstance, pair_exists: bool
) -> None:
    claim_id = _affected(instance)
    if pair_exists:
        accepted(write(instance, create("e", EXPOSURE, {"component": C1, "vulnerability": V1})))
    outcome = accepted(write(instance, *_judgment(claim_id, "vulnerable_code_not_present")))
    assert outcome.changes[2].subject == E11
    assert outcome.changes[2].resolved == ("existing" if pair_exists else "created")
    assert _affected_values(instance) == [("none", None)]
    facts = {predicate for _claim, predicate in _claims(instance, E11)}
    assert facts == {
        f"{EXPOSURE}.component",
        f"{EXPOSURE}.vulnerability",
        f"{EXPOSURE}.justification",
    }


def test_the_package_judgment_refuses_as_a_whole(instance: PlaybillInstance) -> None:
    claim_id = _affected(instance)
    before = instance.accepted_coordinate().git_oid
    # An app-level reason is not a package-level justification (R4).
    refused = write(instance, *_judgment(claim_id, "component_not_present"))
    assert refused.status == "refused"
    assert instance.accepted_coordinate().git_oid == before
    assert live_subjects(instance, EXPOSURE) == []
    assert _affected_values(instance) == [("subject", subject_path(*V1.split("/")))]


def test_the_app_judgment_lands_and_refuses_as_a_whole(instance: PlaybillInstance) -> None:
    _ends(instance)
    made = accepted(
        write(
            instance,
            create("i", INVENTORY, "web"),
            create("o", OCCURRENCE, {"inventory": {"created": "i"}, "component": C1}),
        )
    )
    occurrence = made.changes[1].subject

    def judge(justification: str) -> Any:
        return write(
            instance,
            create("a", APP_EXPOSURE, {"occurrence": occurrence, "vulnerability": V1}),
            {
                "op": "set",
                "subject": {"created": "a"},
                "field": "justification",
                "value": justification,
            },
        )

    before = instance.accepted_coordinate().git_oid
    assert judge("vulnerable_code_not_present").status == "refused"  # package-level only (R4)
    assert instance.accepted_coordinate().git_oid == before
    assert live_subjects(instance, APP_EXPOSURE) == []
    outcome = accepted(judge("inline_mitigations_already_exist"))
    app = outcome.changes[0].subject
    assert ends_of(instance, app) == {
        f"{APP_EXPOSURE}.occurrence": occurrence,
        f"{APP_EXPOSURE}.vulnerability": V1,
    }
    # The app-level judgment never writes the global none (R4).
    assert _affected_values(instance) == []


def test_the_sdk_sends_a_pair_end_as_a_subject_object() -> None:
    """``cx.new_subject`` key parts: a draft, a key reference or a ref become Subject objects."""

    import cruxible_client.authoring.sdk as sdk
    from cruxible_client.authoring.sdk import _key_part_value

    end = _creation(COMPONENT, {"purl": PURL})

    draft = object.__new__(sdk.SubjectCreationDraft)
    object.__setattr__(draft, "creation", end)
    assert _key_part_value(draft) == _member_of(end)
    assert _key_part_value(SubjectKeyReference(subject_kind=COMPONENT, key=PURL)) == (
        SubjectClaimObject(address=SemanticAddress.subject_key(COMPONENT, PURL))
    )
    assert _key_part_value("plain") == "plain"


# -- an end named by key is the key's one holder (review F1) ----------------------------


def _tag_holder(why: str) -> Any:
    return _creation(TAG, {"name": "triage"}, why=why).model_copy(update={"create_new": True})


def _pair_by_tag_key(pair_kind: str) -> Any:
    return _creation(
        pair_kind,
        {
            "tag": SubjectClaimObject(address=SemanticAddress.subject_key(TAG, "triage")),
            "component": _whole(C1),
        },
    )


def _refused_codes(instance: PlaybillInstance, payload: Any) -> set[str]:
    from tests.core_support._pair_world import submit

    submitted = submit(instance, payload)
    assert submitted.status.proposal_id is None
    return {item.code for item in submitted.intent.last_preflight.frontier.diagnostics}


@pytest.mark.parametrize("pair_kind", [LABEL, TAGGING])
def test_an_end_named_by_a_key_two_new_holders_hold_refuses(
    instance: PlaybillInstance, pair_kind: str
) -> None:
    _ends(instance)
    payload = _authored(_tag_holder(" (one)"), _tag_holder(" (two)"), _pair_by_tag_key(pair_kind))
    assert "cruxible.subject.key_ambiguous" in _refused_codes(instance, payload)
    assert live_subjects(instance, TAG) == [] and live_subjects(instance, pair_kind) == []


@pytest.mark.parametrize("pair_kind", [LABEL, TAGGING])
def test_an_end_named_by_a_key_an_old_and_a_new_holder_hold_refuses(
    instance: PlaybillInstance, pair_kind: str
) -> None:
    _ends(instance)
    accepted(write(instance, create("t", TAG, "triage")))
    (held,) = live_subjects(instance, TAG)
    payload = _authored(_tag_holder(" (new)"), _pair_by_tag_key(pair_kind))
    assert "cruxible.subject.key_ambiguous" in _refused_codes(instance, payload)
    assert live_subjects(instance, TAG) == [held]
    # Two accepted holders, and no creation: still ambiguous, never the first.
    accepted(write(instance, create("t", TAG, "triage", create_new=True)))
    assert "cruxible.subject.key_ambiguous" in _refused_codes(
        instance, _authored(_pair_by_tag_key(pair_kind))
    )


@pytest.mark.parametrize("pair_kind", [LABEL, TAGGING])
def test_an_end_named_by_its_creation_member_picks_one_of_two_new_holders(
    instance: PlaybillInstance, pair_kind: str
) -> None:
    _ends(instance)
    first, second = _tag_holder(" (one)"), _tag_holder(" (two)")
    payload = _authored(
        first,
        second,
        _creation(pair_kind, {"tag": _member_of(second), "component": _whole(C1)}),
    )
    accept(instance, payload)
    held = live_subjects(instance, TAG)
    assert len(held) == 2
    (pair,) = live_subjects(instance, pair_kind)
    # The member's own Subject: the first holder (in member order) takes the
    # key's rendering, the other is a further holder named from its member.
    second_is_first = second.member_identity < first.member_identity
    chosen = ends_of(instance, pair)[f"{pair_kind}.tag"]
    assert (chosen == f"{TAG}/triage") is second_is_first
    assert chosen in held


def test_a_write_names_one_of_two_new_holders_by_its_label(instance: PlaybillInstance) -> None:
    _ends(instance)
    outcome = accepted(
        write(
            instance,
            create("t1", TAG, "triage", create_new=True),
            create("t2", TAG, "triage", create_new=True),
            create("p", TAGGING, {"tag": {"created": "t2"}, "component": C1}),
        )
    )
    t2, pair = outcome.changes[1].subject, outcome.changes[2].subject
    assert t2 != outcome.changes[0].subject
    assert ends_of(instance, pair)[f"{TAGGING}.tag"] == t2


# -- an end named by key text never silently picks one reading (review F2) ---------------


def _pair_naming(component: SubjectClaimObject) -> Any:
    return _authored(
        _creation(EXPOSURE, {"component": component, "vulnerability": _whole(V1)}),
    )


def test_a_key_text_naming_one_subject_by_id_and_another_by_key_refuses(
    instance: PlaybillInstance,
) -> None:
    """After literal re-keys, id ``shared`` holds ``alpha`` and ``beta`` holds ``shared``."""

    accepted(
        write(
            instance,
            create("s", COMPONENT, "shared"),
            create("b", COMPONENT, "beta"),
            create("v", VULN, "CVE-2024-35195"),
        )
    )
    shared, beta = f"{COMPONENT}/shared", f"{COMPONENT}/beta"
    accepted(write(instance, {"op": "set", "subject": shared, "field": "purl", "value": "alpha"}))
    accepted(write(instance, {"op": "set", "subject": beta, "field": "purl", "value": "shared"}))
    by_text = SubjectClaimObject(address=SemanticAddress.subject_key(COMPONENT, "shared"))
    codes = _refused_codes(instance, _pair_naming(by_text))
    assert "cruxible.subject.key_ambiguous" in codes, codes
    assert live_subjects(instance, EXPOSURE) == []
    # A structured key reads only the key; a whole-Subject address only the id.
    by_key = SubjectClaimObject(address=SemanticAddress.subject_key(COMPONENT, {"purl": "shared"}))
    accept(instance, _pair_naming(by_key))
    accept(instance, _pair_naming(_whole(shared)))
    pairs = live_subjects(instance, EXPOSURE)
    assert {ends_of(instance, pair)[f"{EXPOSURE}.component"] for pair in pairs} == {shared, beta}


def test_an_end_creation_landing_through_an_alias_holds_no_requested_key(
    instance: PlaybillInstance,
) -> None:
    _ends(instance)
    accepted(
        write(
            instance, {"op": "add", "subject": C1, "field": "semantic.alias", "value": "requests"}
        )
    )
    # The creation lands on C1 through its alias: no Subject holds key "requests".
    end = _creation(COMPONENT, {"purl": "requests"})
    by_key = SubjectClaimObject(
        address=SemanticAddress.subject_key(COMPONENT, {"purl": "requests"})
    )
    payload = _authored(
        end, _creation(EXPOSURE, {"component": by_key, "vulnerability": _whole(V1)})
    )
    assert "cruxible.subject.key_missing" in _refused_codes(instance, payload)
    # Named by the creation member, the pair lands on the Subject the alias names.
    accept(
        instance,
        _authored(
            end, _creation(EXPOSURE, {"component": _member_of(end), "vulnerability": _whole(V1)})
        ),
    )
    assert ends_of(instance, E11)[f"{EXPOSURE}.component"] == C1
