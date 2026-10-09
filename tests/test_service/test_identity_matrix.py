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
        instance, request=request, caller=WriteCaller(actor=OWNER, may_activate=True)
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
