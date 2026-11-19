"""A security-shaped world of pair Subjects, declared and written through authoring and writes.

Ends: ``sec.inventory`` (key ``name``), ``sec.component`` (``purl``),
``sec.vuln`` (``vid``), ``sec.host`` (minted) and ``sec.tag`` (``name``, a
``hint`` kind). Pairs, each ``unique`` and keyed by Subject-valued parts:

- ``sec.occurrence`` (inventory, component), fact ``scope``;
- ``sec.exposure`` (component, vulnerability), fact ``justification``
  (``vulnerable_code_not_present``, R4);
- ``sec.app_exposure`` (occurrence, vulnerability): a pair of a pair, fact
  ``justification`` (the four app-level VEX reasons, R4);
- ``sec.run`` (host): a one-part pair over a minted end;
- ``sec.tagging`` (tag, component): pairs over further holders of a hint key;
- ``sec.advisory``, ``sec.boot`` and ``sec.label``: twins of ``exposure``,
  ``run`` and ``tagging`` whose kind names sort before their ends', so an
  authored change set (members sorted by identity, kind first) holds the pair
  before its end, where the others hold it after.

``sec.component.affected_by`` is many-valued (a ``sec.vuln``) with an
object-keyed inverse: ``none of V``. Every ClaimType admits the coordinator's
self evidence, so a write's ``because`` backs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    ClaimTypeInput,
    SubjectKindInput,
    lower_authoring_input,
)
from cruxible_client.contracts.claim_types import ClaimInverse
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteOutcome, WriteRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from tests.core_support._identity_support import OWNER, identity_claim_type
from tests.core_support._support import initialize_local

INVENTORY = "sec.inventory"
COMPONENT = "sec.component"
VULN = "sec.vuln"
HOST = "sec.host"
TAG = "sec.tag"
OCCURRENCE = "sec.occurrence"
EXPOSURE = "sec.exposure"
APP_EXPOSURE = "sec.app_exposure"
RUN = "sec.run"
TAGGING = "sec.tagging"
ADVISORY = "sec.advisory"
BOOT = "sec.boot"
LABEL = "sec.label"
AFFECTED_BY = f"{COMPONENT}.affected_by"
PACKAGE_REASONS = ("vulnerable_code_not_present",)
APP_REASONS = (
    "component_not_present",
    "vulnerable_code_not_in_execute_path",
    "vulnerable_code_cannot_be_controlled_by_adversary",
    "inline_mitigations_already_exist",
)
#: Each pair kind's parts, in key order: (short name, end kind).
PAIR_PARTS: dict[str, tuple[tuple[str, str], ...]] = {
    OCCURRENCE: (("inventory", INVENTORY), ("component", COMPONENT)),
    EXPOSURE: (("component", COMPONENT), ("vulnerability", VULN)),
    APP_EXPOSURE: (("occurrence", OCCURRENCE), ("vulnerability", VULN)),
    RUN: (("host", HOST),),
    TAGGING: (("tag", TAG), ("component", COMPONENT)),
    ADVISORY: (("component", COMPONENT), ("vulnerability", VULN)),
    BOOT: (("host", HOST),),
    LABEL: (("tag", TAG), ("component", COMPONENT)),
}
TIMESTAMP = "2026-11-06T12:00:00.000000Z"


def _literal_key(kind: str, field: str) -> Any:
    return identity_claim_type(
        field,
        value_type={"type": "string"},
        canonical_form=("nfkc", "trim", "collapse_whitespace", "casefold"),
        predicate=f"{kind}.{field}",
        subject_kinds=(kind,),
    )


def _end_part(kind: str, field: str, end: str) -> Any:
    return identity_claim_type(
        field,
        value_type=(end,),
        canonical_form=None,
        predicate=f"{kind}.{field}",
        subject_kinds=(kind,),
    )


def _fact(kind: str, field: str, members: tuple[str, ...]) -> Any:
    return identity_claim_type(
        field,
        value_type={"enum": list(members), "type": "string"},
        canonical_form=None,
        predicate=f"{kind}.{field}",
        subject_kinds=(kind,),
    )


def _affected_by() -> Any:
    base = identity_claim_type(
        "affected_by",
        value_type=(VULN,),
        canonical_form=None,
        cardinality="many",
        predicate=AFFECTED_BY,
        subject_kinds=(COMPONENT,),
    )
    return base.model_copy(
        update={
            "inverse": ClaimInverse.model_validate(
                {"evidence_rules": ["self"], "key": {"kind": "object"}}
            )
        }
    )


def vocabulary_members() -> tuple[Any, ...]:
    types = [
        _literal_key(INVENTORY, "name"),
        _literal_key(COMPONENT, "purl"),
        _literal_key(VULN, "vid"),
        _literal_key(TAG, "name"),
        _affected_by(),
        _fact(OCCURRENCE, "scope", ("required", "optional", "excluded")),
        _fact(EXPOSURE, "justification", PACKAGE_REASONS),
        _fact(APP_EXPOSURE, "justification", APP_REASONS),
    ]
    kinds: list[SubjectKindInput] = [
        SubjectKindInput(kind="subject_kind", name=INVENTORY, keys=(f"{INVENTORY}.name",)),
        SubjectKindInput(kind="subject_kind", name=COMPONENT, keys=(f"{COMPONENT}.purl",)),
        SubjectKindInput(kind="subject_kind", name=VULN, keys=(f"{VULN}.vid",)),
        SubjectKindInput(kind="subject_kind", name=HOST, minted=True),
        SubjectKindInput(kind="subject_kind", name=TAG, keys=(f"{TAG}.name",), uniqueness="hint"),
    ]
    for pair, parts in PAIR_PARTS.items():
        for field, end in parts:
            types.append(_end_part(pair, field, end))
        kinds.append(
            SubjectKindInput(
                kind="subject_kind",
                name=pair,
                keys=tuple(f"{pair}.{field}" for field, _end in parts),
            )
        )
    return (
        *(ClaimTypeInput(kind="claim_type", claim_type=item) for item in types),
        *kinds,
    )


def submit(instance: PlaybillInstance, payload: Any) -> Any:
    return AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=OWNER, payload=payload, canonical_timestamp=TIMESTAMP
    )


def accept(instance: PlaybillInstance, payload: Any) -> Any:
    submitted = submit(instance, payload)
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    return submitted


def change_set(*members: Any) -> Any:
    return lower_authoring_input(ChangeSetInput(kind="change_set", members=members))


def pair_world(tmp_path: Path) -> PlaybillInstance:
    instance, _owner = initialize_local(tmp_path)
    accept(instance, change_set(*vocabulary_members()))
    return instance


def write(instance: PlaybillInstance, *changes: dict[str, Any], **request: Any) -> WriteOutcome:
    body = {"because": "Recorded by hand.", "changes": list(changes), **request}
    return service_playbill_write(
        instance,
        request=WriteRequest.model_validate(body),
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )


def create(label: str, kind: str, key: Any = None, **extra: Any) -> dict[str, Any]:
    """A create change; a dict ``key`` lists its parts by short name, in any order."""

    if isinstance(key, dict):
        key = [{"predicate": f"{kind}.{name}", "value": value} for name, value in key.items()]
    return {
        "op": "create",
        "label": label,
        "subject_kind": kind,
        **({} if key is None else {"key": key}),
        **extra,
    }


def accepted(outcome: WriteOutcome) -> WriteOutcome:
    assert outcome.status == "accepted", outcome.refusal or outcome
    return outcome


def head_tree(instance: PlaybillInstance) -> Any:
    return instance.tree_at(instance.accepted_coordinate().git_oid)


def live_subjects(instance: PlaybillInstance, kind: str) -> list[str]:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return [
            str(row[0]).removeprefix("subjects/").removesuffix(".json")
            for row in projection.typed.connection.execute(
                "SELECT path FROM subjects WHERE path>=? AND path<? AND lifecycle='live' "
                "ORDER BY path",
                (f"subjects/{kind}/", f"subjects/{kind}0"),
            )
        ]


def ends_of(instance: PlaybillInstance, ref: str) -> dict[str, str]:
    """A pair's live key Claims: part predicate -> the end it names (``kind/id``)."""

    kind, _, subject_id = ref.partition("/")
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        return {
            str(predicate): str(path).removeprefix("subjects/").removesuffix(".json")
            for predicate, path in projection.typed.connection.execute(
                "SELECT predicate, object_path FROM claims WHERE subject_path=? "
                "AND lifecycle='live' AND object_kind='subject'",
                (subject_path(kind, subject_id),),
            )
        }


__all__ = [
    "ADVISORY",
    "AFFECTED_BY",
    "BOOT",
    "LABEL",
    "PAIR_PARTS",
    "APP_EXPOSURE",
    "APP_REASONS",
    "COMPONENT",
    "EXPOSURE",
    "HOST",
    "INVENTORY",
    "OCCURRENCE",
    "PACKAGE_REASONS",
    "RUN",
    "TAG",
    "TAGGING",
    "VULN",
    "accept",
    "accepted",
    "change_set",
    "create",
    "ends_of",
    "head_tree",
    "live_subjects",
    "pair_world",
    "submit",
    "write",
]
