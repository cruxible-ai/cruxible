"""One identity rule, every comparison: the consistency matrix (identity-design-1009).

Every comparison site (item 2) and every Subject-reference entry point (item 3)
is one row. For each row, a spelling equal under the predicate's declared
canonical form -- or an alias of the Subject -- answers exactly as the stored
(canonical) spelling does, and a spelling distinct under the form answers
differently. Every row reads, or writes as a dry run, so the rows share one
seeded instance.

Not a row: Procedure ``filter_items``/``dedupe_items``/``join_items``. Their
item fields declare no predicate (transform specs and item contracts carry
none), so there is no declared form to apply; a Procedure's values meet a
predicate only at its terminal, where the item lowers through the rows below.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDrafts,
    SelfSourceBody,
)
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject, parse_claim
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.line_inbox import InboxLinkSubject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.read_refusals import ReadRefusalError
from tests.core_support._identity_support import KIND, OWNER, seed_identity_surface

ALPHA, BETA = f"{KIND}/alpha", f"{KIND}/beta"


@pytest.fixture(scope="module")
def instance(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    seeded = seed_identity_surface(tmp_path_factory.mktemp("identity-matrix"))
    _write(
        seeded,
        {"op": "set", "subject": ALPHA, "field": "code", "value": "pkg one"},
        {"op": "set", "subject": ALPHA, "field": "state", "value": "open"},
        {"op": "add", "subject": ALPHA, "field": "tags", "value": "infra"},
        {"op": "add", "subject": BETA, "field": "semantic.alias", "value": "b-pkg"},
        {"op": "add", "subject": ALPHA, "field": "depends_on", "value": BETA},
    )
    return seeded


def _write(instance: PlaybillInstance, *changes: dict[str, Any], dry_run: bool = False) -> Any:
    request = WriteRequest.model_validate(
        {"because": "Matrix.", "changes": list(changes), "dry_run": dry_run}
    )
    outcome = service_playbill_write(
        instance,
        request=request,
        caller=WriteCaller(actor=OWNER, may_activate=True, may_approve=True),
    )
    if not dry_run:
        assert outcome.status == "accepted", outcome
    return outcome


def _ids(instance: PlaybillInstance, where: list[dict[str, Any]]) -> object:
    try:
        result = service_playbill_query(
            instance, request=QueryRequest.model_validate({"kind": KIND, "where": where})
        )
    except ReadRefusalError as refused:
        return ("refused", refused.error_code)
    return tuple(row["subject_id"] for row in result.rows)


def _dry(instance: PlaybillInstance, change: dict[str, Any]) -> object:
    outcome = _write(instance, change, dry_run=True)
    if outcome.refusal is not None:
        return ("refused", outcome.refusal.code)
    (item,) = outcome.changes
    return (outcome.status, item.already_live, outcome.subjects_added)


def _lowered(instance: PlaybillInstance, statement: AuthoringClaimStatement) -> object:
    member = ClaimAuthoringPayloadV2(
        statement=statement,
        rationale="Matrix.",
        source=SelfSourceBody(content_base64=base64.b64encode(b"matrix").decode()),
        dependency_drafts=ClaimDependencyDrafts(),
    )
    _intent, computed = AuthoringIntentCoordinator.for_instance(instance).preview(
        actor=OWNER,
        payload=ChangeSetAuthoringPayload(members=(member,)),
        canonical_timestamp="2026-10-09T12:00:00.000000Z",
    )
    if computed.lowered is None:
        return ("refused", tuple(item.code for item in computed.result.frontier.diagnostics))
    claims = [
        parse_claim(content, path=path)
        for path, content in computed.lowered.changed_members
        if path.startswith("claims/")
    ]
    return tuple(
        (claim.statement.subject.artifact_path, claim.statement.object.model_dump(mode="json"))
        for claim in claims
    )


def _engine(spelling: str) -> object:
    from cruxible_client.contracts.query.grammar import (
        QueryClaimValueRef,
        QueryComparisonFilter,
        QueryLiteralRef,
    )
    from tests.test_claims.test_claim_query_engine import (
        facts,
        run,
        status_claim,
        typed_item_query,
    )
    from tests.test_query.test_query_definitions import STATUS_PREDICATE

    query = typed_item_query(
        STATUS_PREDICATE,
        name="project.matrix",
        where=QueryComparisonFilter(
            left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
            operator="eq",
            right=QueryLiteralRef(value=spelling),
            value_type="string",
        ),
    )
    row = status_claim(1, "wi-1", "pkg one").model_copy(
        update={"canonical_form": ("nfkc", "trim", "collapse_whitespace", "casefold")}
    )
    return tuple(item.fields[0].value for item in run(query, facts((row,))).rows)


def _engine_parameter(instance: PlaybillInstance, spelling: str) -> object:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.query.definitions import (
        AcceptedQueryDefinition,
        QueryDefinition,
        QueryEvaluationPolicy,
    )
    from cruxible_client.contracts.query.grammar import (
        QueryBudgets,
        QueryEntry,
        QueryParameterDeclaration,
        QueryParameterRef,
    )
    from cruxible_core.service.discovery.query import parameters_through_aliases

    definition = QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="inv.matrix"),
        entry=QueryEntry(
            binding="item", subject_kinds=(KIND,), subject_id=QueryParameterRef(parameter="id")
        ),
        parameters=(QueryParameterDeclaration(name="id", value_type="string"),),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
    )
    accepted = AcceptedQueryDefinition.model_construct(
        path="queries/inv/matrix.json", query=definition, artifact_digest="sha256:" + "0" * 64
    )
    return dict(
        parameters_through_aliases(
            instance,
            accepted,
            coordinate=instance.accepted_coordinate(),
            parameters={"id": spelling.partition("/")[2]},
        )
    )


def _inbox(instance: PlaybillInstance, spelling: str) -> object:
    from cruxible_client.contracts.errors import SubjectNotFoundError
    from cruxible_core.service.procedures.line_inbox import _existing_target

    try:
        return _existing_target(instance, InboxLinkSubject(subject=spelling))
    except SubjectNotFoundError:
        return ("refused", "cruxible.subject_not_found")


def _mandate(instance: PlaybillInstance, spelling: str) -> object:
    from cruxible_core.authoring.lowering import _mandate_alias_resolver

    head = instance.accepted_coordinate()
    resolve = _mandate_alias_resolver(instance, head, instance.immutable_tree_at(head.git_oid))
    return resolve(f"Subject:{spelling}", "condition.fixed_parameters.target")


def _slot(instance: PlaybillInstance, spelling: str) -> object:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.canonical_forms import canonicalize_literal
    from cruxible_core.claims.claim_slots import classify_claim_slot
    from tests.core_support._identity_support import CODE_FORM

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    (stored,) = [
        claim
        for path, content in tree.items()
        if path.startswith("claims/")
        for claim in (parse_claim(content, path=path),)
        if claim.statement.predicate == f"{KIND}.code" and claim.lifecycle.state == "live"
    ]
    # A second Claim of the slot, holding the spelling as every write stores it.
    written = stored.model_copy(
        update={
            "identity": ArtifactIdentity(kind="Claim", name="CLM-" + "f" * 32),
            "statement": stored.statement.model_copy(
                update={
                    "object": LiteralClaimObject(value=canonicalize_literal(spelling, CODE_FORM))
                }
            ),
        }
    )
    return classify_claim_slot((stored, written)).resolution


@dataclass(frozen=True)
class Row:
    site: str
    canonical: str
    equal: str
    distinct: str
    answer: Callable[[PlaybillInstance, str], object]


CODE = ("pkg one", " PKG   One ", "pkg-one")
STATE = ("open", " OPEN", "opened")
TAGS = ("infra", " INFRA ", "infra2")
REF = (BETA, f"{KIND}/b-pkg", f"{KIND}/gamma")

ROWS = (
    # -- item 2: comparison sites ---------------------------------------------------
    Row(
        "compact query eq",
        *CODE,
        lambda inst, value: _ids(inst, [{"field": "code", "eq": value}]),
    ),
    Row(
        "compact query ne",
        *CODE,
        lambda inst, value: _ids(inst, [{"field": "code", "ne": value}]),
    ),
    Row(
        "compact query in",
        *CODE,
        lambda inst, value: _ids(inst, [{"field": "code", "in": [value, "zzz"]}]),
    ),
    Row(
        "compact query enum member",
        *STATE,
        lambda inst, value: _ids(inst, [{"field": "state", "eq": value}]),
    ),
    Row(
        "QueryDefinition literal (settle conditions evaluate here)",
        *CODE,
        lambda _inst, value: _engine(value),
    ),
    Row(
        "write expect",
        *CODE,
        lambda inst, value: _dry(
            inst,
            {"op": "set", "subject": ALPHA, "field": "code", "value": "pkg two", "expect": value},
        ),
    ),
    Row(
        "write value already present",
        *TAGS,
        lambda inst, value: _dry(
            inst, {"op": "add", "subject": ALPHA, "field": "tags", "value": value}
        ),
    ),
    Row(
        "write enum member",
        *STATE,
        lambda inst, value: _dry(
            inst, {"op": "set", "subject": ALPHA, "field": "state", "value": value}
        ),
    ),
    Row(
        "change-set literal lowering",
        *CODE,
        lambda inst, value: _lowered(
            inst,
            AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, "beta")),
                predicate=f"{KIND}.code",
                object=LiteralClaimObject(value=value),
                role="observation",
            ),
        ),
    ),
    Row("slot classification", *CODE, _slot),
    # -- item 3: Subject-reference entry points -------------------------------------
    Row(
        "write subject",
        *REF,
        lambda inst, value: _dry(
            inst, {"op": "set", "subject": value, "field": "plain", "value": "z"}
        ),
    ),
    Row(
        "write Subject value",
        *REF,
        lambda inst, value: _dry(
            inst, {"op": "add", "subject": ALPHA, "field": "depends_on", "value": value}
        ),
    ),
    Row(
        "write Subject expect",
        *REF,
        lambda inst, value: _dry(
            inst,
            {"op": "retire", "target": {"subject": ALPHA, "field": "depends_on"}, "expect": value},
        ),
    ),
    Row(
        "change-set Claim subject",
        *REF,
        lambda inst, value: _lowered(
            inst,
            AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, value.partition("/")[2])),
                predicate=f"{KIND}.plain",
                object=LiteralClaimObject(value="matrix"),
                role="observation",
            ),
        ),
    ),
    Row(
        "change-set Claim object",
        *REF,
        lambda inst, value: _lowered(
            inst,
            AuthoringClaimStatement(
                subject=SemanticAddress.whole_artifact(subject_path(KIND, "beta")),
                predicate=f"{KIND}.depends_on",
                object=SubjectClaimObject(
                    address=SemanticAddress.whole_artifact(
                        subject_path(KIND, value.partition("/")[2])
                    )
                ),
                role="observation",
            ),
        ),
    ),
    Row(
        "compact query subject_id",
        *REF,
        lambda inst, value: _ids(inst, [{"field": "subject_id", "eq": value.partition("/")[2]}]),
    ),
    Row(
        "compact query Subject-valued filter",
        *REF,
        lambda inst, value: _ids(inst, [{"field": "depends_on", "eq": value}]),
    ),
    Row("named query entry parameter", *REF, _engine_parameter),
    Row("ProcedureMandate Subject parameter", *REF, _mandate),
    Row("inbox link --subject", *REF, _inbox),
)


@pytest.mark.parametrize("row", ROWS, ids=[row.site for row in ROWS])
def test_one_identity_rule_at_every_comparison(instance: PlaybillInstance, row: Row) -> None:
    canonical = row.answer(instance, row.canonical)
    assert row.answer(instance, row.equal) == canonical, row.site
    assert row.answer(instance, row.distinct) != canonical, row.site


# -- E-b: creation by key, one rule on every route (identity D1/D4) ------------------
#
# A creation names a kind and its key, never an id. Every create-by-key route --
# a change-set Subject member, the write verb's ``kind/<text>`` on a single-key
# kind, a Procedure terminal's Subject item (and a settle terminal's, which
# lowers the same item; its mandate coverage is tested with settlement) --
# resolves a key the same way: no live holder creates, one resolves to it, two
# or more refuse as ambiguous. ``create_new`` makes a further holder of a hint
# key and is refused on a unique kind; a minted kind takes a minted id; a
# grandfathered (declared, then retired) kind creates nothing.

UNIT, HINT, MINT, OLD = "dev.unit", "dev.hint", "dev.mint", "dev.old"


@pytest.fixture(scope="module")
def creation_world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    from cruxible_client.contracts.authoring.inputs import (
        ClaimTypeInput,
        SubjectInput,
        SubjectKindInput,
    )
    from tests.core_support._support import initialize_local
    from tests.test_service.test_subject_identity import _field, _key, accept, change_set

    world, _owner = initialize_local(tmp_path_factory.mktemp("creation-matrix"))
    accept(
        world,
        change_set(
            ClaimTypeInput(kind="claim_type", claim_type=_key(UNIT, "slug")),
            ClaimTypeInput(kind="claim_type", claim_type=_field(UNIT, "note")),
            SubjectKindInput(
                kind="subject_kind", name=UNIT, keys=(f"{UNIT}.slug",), uniqueness="unique"
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_key(HINT, "slug")),
            ClaimTypeInput(kind="claim_type", claim_type=_field(HINT, "note")),
            SubjectKindInput(
                kind="subject_kind", name=HINT, keys=(f"{HINT}.slug",), uniqueness="hint"
            ),
            ClaimTypeInput(kind="claim_type", claim_type=_field(MINT, "note")),
            SubjectKindInput(kind="subject_kind", name=MINT, minted=True),
            ClaimTypeInput(kind="claim_type", claim_type=_field(OLD, "note")),
            SubjectKindInput(kind="subject_kind", name=OLD, minted=True),
            SubjectInput(kind="subject", subject_kind=UNIT, key="one_x"),
            SubjectInput(kind="subject", subject_kind=HINT, key="one_x"),
            SubjectInput(kind="subject", subject_kind=HINT, key="two_x"),
            SubjectInput(kind="subject", subject_kind=OLD),
        ),
    )
    accept(
        world,
        change_set(
            SubjectInput(kind="subject", subject_kind=HINT, key="Two_X", create_new=True),
            SubjectKindInput(kind="subject_kind", name=OLD, minted=True, retire=True),
        ),
    )
    return world


def _created_or_resolved(resolved: dict[str, Any]) -> tuple[str, str]:
    subject = str(resolved["subject"])
    return ("resolved" if resolved.get("resolved") else "created", subject)


def _by_member(world: PlaybillInstance, kind: str, key: str | None, create_new: bool) -> object:
    from cruxible_client.contracts.authoring.inputs import SubjectInput, lower_authoring_input

    payload = lower_authoring_input(
        SubjectInput(
            kind="subject",
            subject_kind=kind,
            **({} if key is None else {"key": key}),
            create_new=create_new,
        )
    )
    _intent, computed = AuthoringIntentCoordinator.for_instance(world).preview(
        actor=OWNER, payload=payload, canonical_timestamp="2026-10-09T12:00:00.000000Z"
    )
    if computed.lowered is None:
        return ("refused", tuple(item.code for item in computed.result.frontier.diagnostics))
    members = computed.lowered.resolved_authoring["members"]
    (member,) = [item for item in members if "subject" in item]  # type: ignore[union-attr]
    return _created_or_resolved(member)


def _by_write(world: PlaybillInstance, kind: str, key: str | None, create_new: bool) -> object:
    if create_new:
        return ("refused", "no create_new on a write")
    outcome = _write(
        world,
        {"op": "set", "subject": f"{kind}/{key or 'x'}", "field": "note", "value": "m"},
        dry_run=True,
    )
    if outcome.refusal is not None:
        return ("refused", (outcome.refusal.code,))
    if outcome.subjects_added:
        (added,) = outcome.subjects_added
        return ("created", added)
    (item,) = outcome.changes
    return ("resolved", item.subject)


def _by_create(world: PlaybillInstance, kind: str, key: str | None, create_new: bool) -> object:
    create: dict[str, Any] = {"op": "create", "label": "it", "subject_kind": kind}
    if key is not None:
        create["key"] = key
    if create_new:
        create["create_new"] = True
    outcome = _write(
        world,
        create,
        {"op": "set", "subject": {"created": "it"}, "field": "note", "value": "m"},
        dry_run=True,
    )
    if outcome.refusal is not None:
        return ("refused", (outcome.refusal.code,))
    created = outcome.changes[0]
    return ("resolved" if created.resolved == "existing" else "created", created.subject)


def _by_procedure_item(
    world: PlaybillInstance, kind: str, key: str | None, create_new: bool
) -> object:
    from cruxible_client.contracts.procedures.proposal_items import ProcedureSubjectProposalItem
    from cruxible_core.authoring.lowering import AuthoringLoweringError, subject_creation_paths
    from cruxible_core.procedures.proposal_delivery import _subject_creation

    item = ProcedureSubjectProposalItem(
        subject_kind=kind,
        key={} if key is None else {"slug": key},
        rationale="Matrix.",
        create_new=create_new,
    )
    head = world.accepted_coordinate()
    member = _subject_creation(item, capture=None)
    try:
        ((path, reused),) = subject_creation_paths(
            world,
            operation="AIT-" + "0" * 32,
            base=head,
            base_tree=world.immutable_tree_at(head.git_oid),
            members=(member,),
        ).values()
    except AuthoringLoweringError as exc:
        return ("refused", (exc.code,))
    return (
        "resolved" if reused else "created",
        path.removeprefix("subjects/").removesuffix(".json"),
    )


CREATION_ROUTES = {
    "change-set Subject member": _by_member,
    "write kind/<text>": _by_write,
    "write create": _by_create,
    "Procedure Subject item": _by_procedure_item,
}

#: (case, kind, key, create_new) -> what every route answers; ``None`` = not a route case.
CREATION_CASES: tuple[tuple[str, str, str | None, bool, object], ...] = (
    ("unique, no holder", UNIT, "zero_x", False, ("created", f"{UNIT}/zero-x")),
    ("unique, one holder", UNIT, "one_x", False, ("resolved", f"{UNIT}/one-x")),
    ("hint, no holder", HINT, "zero_x", False, ("created", f"{HINT}/zero-x")),
    ("hint, one holder", HINT, "one_x", False, ("resolved", f"{HINT}/one-x")),
    (
        "hint, two holders",
        HINT,
        "two_x",
        False,
        ("refused", ("cruxible.subject.key_ambiguous",)),
    ),
    (
        "unique, create_new",
        UNIT,
        "one_x",
        True,
        ("refused", ("cruxible.subject.create_new_refused",)),
    ),
    ("minted", MINT, None, False, ("created", "minted")),
    ("grandfathered", OLD, None, False, ("refused", ("cruxible.subject.kind_undeclared",))),
)


@pytest.mark.parametrize("route", sorted(CREATION_ROUTES))
@pytest.mark.parametrize(
    ("case", "kind", "key", "create_new", "expected"),
    CREATION_CASES,
    ids=[case[0] for case in CREATION_CASES],
)
def test_one_creation_rule_on_every_route(
    creation_world: PlaybillInstance,
    route: str,
    case: str,
    kind: str,
    key: str | None,
    create_new: bool,
    expected: object,
) -> None:
    answer = CREATION_ROUTES[route](creation_world, kind, key, create_new)
    if route == "write kind/<text>" and kind == MINT:
        # A write never creates a minted Subject: it has no key to name one by.
        assert answer[0] == "refused", answer  # type: ignore[index]
        return
    if route == "write kind/<text>" and create_new:
        return  # the write verb has no create_new; it never makes a further holder
    if route == "write create" and expected == ("created", "minted"):
        # A minted id comes from the submitted operation: a dry run names none.
        assert answer == ("created", None), answer
        return
    if expected == ("created", "minted"):
        assert answer[0] == "created" and str(answer[1]).startswith(f"{MINT}/sub-"), answer  # type: ignore[index]
        return
    assert answer == expected, (route, case)


@pytest.mark.parametrize("route", ["change-set Subject member", "Procedure Subject item"])
def test_create_new_makes_a_further_holder_of_a_hint_key(
    creation_world: PlaybillInstance, route: str
) -> None:
    status, ref = CREATION_ROUTES[route](creation_world, HINT, "one_x", True)  # type: ignore[misc]
    assert status == "created"
    assert str(ref).startswith(f"{HINT}/one-x-") and len(str(ref)) == len(f"{HINT}/one-x-") + 8


def test_a_key_claim_write_that_would_name_a_second_unique_holder_refuses(
    creation_world: PlaybillInstance,
) -> None:
    """Every key-Claim write route meets the unique-key boundary (key_conflict)."""

    from tests.test_service.test_subject_identity import compile_

    outcome = _write(
        creation_world,
        {"op": "set", "subject": f"{UNIT}/one-x", "field": "slug", "value": "one_x"},
        dry_run=True,
    )
    # Restating a Subject's own key changes nothing.
    assert outcome.refusal is None, outcome.refusal
    # Another unique Subject taking an existing key is a conflict, whichever door.
    other = _write(
        creation_world,
        {"op": "set", "subject": f"{UNIT}/zero_x", "field": "note", "value": "made"},
        dry_run=True,
    )
    assert other.subjects_added == (f"{UNIT}/zero-x",)
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        SubjectInput,
        lower_authoring_input,
    )

    both = compile_(
        creation_world,
        lower_authoring_input(
            ChangeSetInput(
                kind="change_set",
                members=(
                    SubjectInput(kind="subject", subject_kind=UNIT, key="Three"),
                    SubjectInput(kind="subject", subject_kind=UNIT, key="three"),
                ),
            )
        ),
    )
    # Two creations of one key in one change: the canonical key resolves the
    # second to the first, or the change refuses -- never two holders.
    codes = tuple(item.code for item in both.frontier.diagnostics)
    assert both.verdict == "refused" or not codes, codes
