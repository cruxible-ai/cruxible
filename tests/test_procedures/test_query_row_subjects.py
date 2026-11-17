"""A step iterating a Query read's rows reads each row's Subject and bindings.

Every client gets a Query row's ``result_subject_identity`` and ``bindings``;
inside a Procedure an inbox or claims step whose ``for_each`` is a read's
``rows`` reads them as ``$item.subject`` and ``$item.bindings.<name>`` beside
the row's declared columns (``$item.<column>``). Anything else a row does not
offer refuses at authoring, listing what the row offers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import (
    InboxStep,
    ReadStep,
    ReturnFields,
    lower_procedure_spec,
)
from cruxible_client.contracts.query.definitions import (
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.shapes import (
    ContractRef,
    SubjectType,
    contract_digest,
    contract_path,
    render_contract,
    validate_contract,
)
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.procedures.query_rows import (
    ItemReferenceUnresolved,
    query_row_items,
    row_reference_type,
    validate_row_references,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._knowledge_loop_support import SUBJECT_KIND, seed_claims, work_item_query
from tests.support.operation_world import (
    NOW,
    Invoker,
    Operator,
    actor,
    author_line,
    author_procedure,
)
from tests.support.procedures import procedure_definition, procedure_spec, ref

ROW = "test.work-row"
ITEM = "test.work-inbox"
QUERY = "project.work_items"


def _read_view(rows: list[dict[str, Any]], served: list[dict[str, Any]]) -> dict[str, Any]:
    return {"rows": rows, "result": {"rows": served}, "definition_digest": "sha256:" + "0" * 64}


def _served(subject: str | None, **bindings: str | None) -> dict[str, Any]:
    return {
        "result_subject_identity": None if subject is None else f"Subject:{subject}",
        "bindings": [
            {"binding": name, "subject_identity": None if value is None else f"Subject:{value}"}
            for name, value in bindings.items()
        ],
    }


def test_a_read_rows_fan_out_offers_each_rows_subject_and_bindings() -> None:
    from cruxible_core.procedures.execution import _resolve_reference

    view = _read_view(
        [{"status": "ready"}, {"status": "done", "subject": "a column"}],
        [
            _served("project.work_item/wi-42", item="project.work_item/wi-42", owner=None),
            _served("project.work_item/wi-43", item="project.work_item/wi-43"),
        ],
    )

    first, second = query_row_items("$steps.work.rows", {"work": view}, view["rows"]) or ()

    def read(reference: str, item: Any) -> object:
        return _resolve_reference(reference, input_payload={}, parameters={}, outputs={}, item=item)

    assert read("$item.subject", first) == "project.work_item/wi-42"
    # An optional binding that bound nothing is left out.
    assert read("$item.bindings", first) == {"item": "project.work_item/wi-42"}
    assert read("$item.status", first) == "ready"
    # A column present in the row wins.
    assert read("$item.subject", second) == "a column"
    # Only exactly a Query read's rows gain them.
    assert query_row_items("$steps.work.result.rows", {"work": view}, view["rows"]) is None
    assert query_row_items("$steps.other.rows", {"other": {"rows": []}}, []) is None


def test_a_declared_column_withholds_the_offer_even_when_absent() -> None:
    from cruxible_core.procedures.execution import ExecutionError, _resolve_reference

    served = {
        **_served("project.work_item/wi-42", item="project.work_item/wi-42"),
        "fields": [
            {"name": "subject", "state": "absent", "value": None},
            {"name": "bindings", "state": "conflict", "value": None},
        ],
    }
    view = _read_view([{}], [served])
    (row,) = query_row_items("$steps.work.rows", {"work": view}, view["rows"]) or ()

    for reference in ("$item.subject", "$item.bindings.item"):
        # The column is declared but has no value: never the row's Subject instead.
        with pytest.raises(ExecutionError, match="does not resolve"):
            _resolve_reference(reference, input_payload={}, parameters={}, outputs={}, item=row)


def test_a_whole_row_is_exactly_the_row_it_was(tmp_path: Path) -> None:
    """``$item`` and every whole-row use see the projected columns only, as before."""

    from cruxible_client.contracts.canonical import normalize_canonical
    from cruxible_core.procedures.execution import _resolve_template

    view = _read_view([{"status": "ready"}], [_served("project.work_item/wi-42", item=None)])
    (row,) = query_row_items("$steps.work.rows", {"work": view}, view["rows"]) or ()

    assert row == {"status": "ready"}
    whole = _resolve_template(
        {"payload": "$item"}, input_payload={}, parameters={}, outputs={}, item=row
    )
    assert whole == {"payload": {"status": "ready"}}
    assert type(normalize_canonical(row)) is dict


def _query(row: Any) -> Any:
    return work_item_query().model_copy(
        update={
            "row_contract": ContractRef(name=ROW, version=1, digest=contract_digest(row).tagged)
        }
    )


def test_row_references_are_typed_as_subjects_of_their_bindings_kinds() -> None:
    query = work_item_query()

    assert row_reference_type(query, ["subject"]) == SubjectType(subject_kinds=(SUBJECT_KIND,))
    assert row_reference_type(query, ["bindings", "item"]) == SubjectType(
        subject_kinds=(SUBJECT_KIND,)
    )
    assert row_reference_type(query, ["bindings", "missing"]) is None
    assert row_reference_type(query, ["status"]) is None  # a column keeps its own type


def _inbox_definition(fields: dict[str, str]) -> Any:
    spec = procedure_spec(
        "test.row-inbox",
        input={},
        output={},
        steps=[
            ReadStep(read="work", query=QUERY),
            InboxStep(
                inbox=f"{ITEM}@1",
                for_each=ref("$steps.work.rows"),
                fields={name: ref(value) for name, value in fields.items()},
                id="post",
            ),
        ],
    )
    return procedure_definition(**lower_procedure_spec(spec).definition)


def _lookup(identity: Any) -> Any:
    return work_item_query() if identity.qualified == f"QueryDefinition:{QUERY}" else None


def test_an_offered_row_reference_passes_the_authoring_check() -> None:
    validate_row_references(
        _inbox_definition(
            {"item": "$item.subject", "bound": "$item.bindings.item", "status": "$item.status"}
        ),
        _lookup,
    )


@pytest.mark.parametrize(
    "reference",
    ["$item.subject_id", "$item.bindings.owner", "$item.fields.status", "$item.subject.kind"],
)
def test_an_unoffered_row_reference_refuses_listing_what_the_row_offers(reference: str) -> None:
    with pytest.raises(ItemReferenceUnresolved) as refused:
        validate_row_references(_inbox_definition({"item": reference}), _lookup)

    message = str(refused.value)
    assert reference in message and QUERY in message
    for offered in ("$item.item_id", "$item.status", "$item.subject", "$item.bindings.item"):
        assert offered in message
    assert (refused.value.node_id, refused.value.element) == ("post", "fields.item")
    assert "column" in refused.value.repair


# -- a kit-style Procedure: one inbox item per row, naming the row's Subject --------------


@dataclass(frozen=True)
class _Clock:
    at: datetime

    def now(self) -> datetime:
        return self.at

    def monotonic_ns(self) -> int:
        import time

        return time.monotonic_ns()


def _row_world(
    tmp_path: Path,
    fields: dict[str, Any],
    *,
    narrowing: tuple[Any, ...] = (),
    for_each: str = "$steps.work.rows",
    item_fields: dict[str, Any] | None = None,
) -> Any:
    instance, _owner = seed_claims(tmp_path)
    row = validate_contract(
        {"name": ROW, "version": 1, "fields": {"item_id": "string", "status": "string"}}
    )
    item = validate_contract(
        {
            "name": ITEM,
            "version": 1,
            "fields": item_fields
            or {
                "item": f"subject<{SUBJECT_KIND}>",
                "bound": f"subject<{SUBJECT_KIND}>",
                "status": "string",
            },
        }
    )
    query = _query(row)
    accept_tree(
        instance,
        {
            contract_path(ROW, 1): render_contract(row),
            contract_path(ITEM, 1): render_contract(item),
            query_definition_path(query.identity.name): render_query_definition(query),
        },
        name="row-subjects",
    )
    author_procedure(
        instance,
        procedure_spec(
            procedure="test.row-inbox",
            input={},
            output={},
            steps=[
                ReadStep(read="work", query=QUERY),
                *narrowing,
                InboxStep(
                    inbox=f"{ITEM}@1",
                    for_each=ref(for_each),
                    fields=fields,
                    dedupe_key=tuple(item_fields or ("item",))[:1],
                    id="post",
                ),
            ],
            returns=ReturnFields(fields={}),
        ),
    )
    return instance


def _posted(instance: Any) -> list[dict[str, Any]]:
    """Run the observe Line over the Procedure once; the inbox payloads, by item."""

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )

    author_line(instance, name="test.row-inbox-line", procedure="test.row-inbox", parameters={})
    state = service_run_playbill_line(
        instance,
        path_identity_digest="test.row-inbox-line",
        request=LineRunRequest(line="test.row-inbox-line"),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW),
        provider_runtime_operator=Operator(Invoker()),
    )
    assert state.status == "succeeded", state.terminal
    store = LineInboxStore(instance)
    entries, _cursor = store.page(line=None, status=None, limit=50, cursor=None)
    return sorted(
        (store.get(entry.envelope.item_id).payload for entry in entries),
        key=lambda payload: str(sorted(payload.items())),
    )


def test_a_line_posts_one_inbox_item_per_row_naming_the_rows_subject(tmp_path: Path) -> None:
    instance = _row_world(
        tmp_path,
        {
            "item": ref("$item.subject"),
            "bound": ref("$item.bindings.item"),
            "status": ref("$item.status"),
        },
    )
    payloads = _posted(instance)
    assert payloads == [
        {
            "item": f"{SUBJECT_KIND}/wi-42",
            "bound": f"{SUBJECT_KIND}/wi-42",
            "status": "ready",
        },
        {
            "item": f"{SUBJECT_KIND}/wi-43",
            "bound": f"{SUBJECT_KIND}/wi-43",
            "status": "blocked",
        },
    ]


def _narrowing(kind: str, alias: str, source: str, spec: dict[str, Any]) -> Any:
    from cruxible_client.contracts.procedures.authored import TransformStep

    return TransformStep.model_validate(
        {"transform": alias, "kind": kind, "spec": {"input": ref(source), **spec}}
    )


READY = {
    "where": {
        "op": "eq",
        "left": {"$ref": "item", "path": ["fields", "status"]},
        "right": {"literal": "ready"},
    }
}


def test_filter_and_dedupe_over_the_rows_keep_each_rows_subject_and_bindings(
    tmp_path: Path,
) -> None:
    instance = _row_world(
        tmp_path,
        {
            "item": ref("$item.subject"),
            "bound": ref("$item.bindings.item"),
            "status": ref("$item.status"),
        },
        narrowing=(
            _narrowing("filter", "ready", "$steps.work.rows", READY),
            _narrowing("dedupe", "unique", "$steps.ready.rows", {"keys": ["item_id"]}),
        ),
        for_each="$steps.unique.rows",
    )

    assert _posted(instance) == [
        {"item": f"{SUBJECT_KIND}/wi-42", "bound": f"{SUBJECT_KIND}/wi-42", "status": "ready"}
    ]


def _nested_row() -> Any:
    row = validate_contract(
        {"name": ROW, "version": 1, "fields": {"item_id": "string", "status": "string"}}
    )
    pin = {"name": ROW, "version": 1, "digest": contract_digest(row).tagged}
    return {"type": {"kind": "contract", "contract": pin}}


def test_a_whole_row_posted_through_a_filter_is_exactly_its_columns(tmp_path: Path) -> None:
    """A Contract typed by the Query's closed row Contract still takes the whole row."""

    instance = _row_world(
        tmp_path,
        {"payload": ref("$item")},
        narrowing=(_narrowing("filter", "ready", "$steps.work.rows", READY),),
        for_each="$steps.ready.rows",
        item_fields={"payload": _nested_row()},
    )

    assert _posted(instance) == [{"payload": {"item_id": "wi-42", "status": "ready"}}]


def test_a_step_that_rebuilds_the_rows_refuses_naming_it() -> None:
    spec = procedure_spec(
        "test.row-inbox",
        input={},
        output={},
        steps=[
            ReadStep(read="work", query=QUERY),
            _narrowing(
                "shape",
                "shaped",
                "$steps.work.rows",
                {"fields": {"status": {"from": ref("$item.fields.status")}}},
            ),
            InboxStep(
                inbox=f"{ITEM}@1",
                for_each=ref("$steps.shaped.rows"),
                fields={"item": ref("$item.subject"), "status": ref("$item.status")},
                id="post",
            ),
        ],
    )
    definition = procedure_definition(**lower_procedure_spec(spec).definition)

    with pytest.raises(ItemReferenceUnresolved) as refused:
        validate_row_references(definition, _lookup)

    message = str(refused.value)
    assert "'shaped'" in message and "only filter and dedupe keep them" in message
    assert refused.value.element == "fields.item"
    # A column the rebuilt rows still carry is left to the run.
    validate_row_references(
        procedure_definition(
            **lower_procedure_spec(
                spec.model_copy(
                    update={
                        "steps": (
                            *spec.steps[:2],
                            spec.steps[2].model_copy(
                                update={"fields": {"status": ref("$item.status")}}
                            ),
                        )
                    }
                )
            ).definition
        ),
        _lookup,
    )


def test_authoring_refuses_a_row_reference_the_row_does_not_offer(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    with pytest.raises(AuthoringLoweringError) as refused:
        _row_world(tmp_path, {"item": ref("$item.subject_id"), "status": ref("$item.status")})

    assert refused.value.code == "cruxible.authoring.item_reference_unresolved"
    assert refused.value.offending_element == "$.steps[1].fields.item"
    assert "$item.subject" in refused.value.message
    assert "$item.bindings.item" in refused.value.message
    (repair,) = refused.value.repairs
    assert "column" in repair.description


# -- a claims step over the rows types the row's Subject like a column ------------------


def _claims_check(value_type: str, value: str) -> None:
    from cruxible_core.procedures.claims_typing import validate_claims_templates
    from tests.core_support._subject_kind_support import keyed_subject_kind
    from tests.test_procedures.test_claims_terminal import KIND, claim_type

    row = validate_contract(
        {
            "name": ROW,
            "version": 1,
            "fields": {"item_id": "string{max_len: 256}", "status": "string"},
        }
    )
    predicate = KIND + ".work_item"
    spec = procedure_spec(
        "test.row-claims",
        reference_text=True,
        input={},
        output={},
        steps=[
            {"read": "work", "query": QUERY},
            {
                "id": "write",
                "for_each": "$steps.work.rows",
                "subject": {"kind": KIND, "key": {"cve_id": "$item.item_id"}},
                "claims": [{"type": predicate, "value": value, "rationale": "Read from the row."}],
            },
        ],
    )
    definition = procedure_definition(**lower_procedure_spec(spec).definition)
    definitions = {
        f"QueryDefinition:{QUERY}": _query(row),
        row.identity.qualified: row,
        f"SubjectKind:{KIND}": keyed_subject_kind(KIND, (KIND + ".cve_id",)),
        f"ClaimType:{KIND}.cve_id": claim_type(KIND + ".cve_id", "string"),
        f"ClaimType:{predicate}": claim_type(predicate, value_type),
    }
    lookup = lambda identity: definitions.get(identity.qualified)  # noqa: E731
    validate_row_references(definition, lookup)
    validate_claims_templates(definition, lookup)


@pytest.mark.parametrize("value", ["$item.subject", "$item.bindings.item"])
def test_a_claims_step_types_a_rows_subject_against_the_claim_value(value: str) -> None:
    from cruxible_core.procedures.claims_typing import ClaimsTypingRefused

    _claims_check(f"subject<{SUBJECT_KIND}>", value)
    with pytest.raises(ClaimsTypingRefused, match="not assignable"):
        _claims_check("subject<other.kind>", value)


# -- a claims run over Query rows: a derivation from an exact Claim read -----------------


def test_a_derivation_over_query_rows_writes_each_rows_subject(tmp_path: Path) -> None:
    """A Claim written from Query rows is a derivation: it cites the Claim it read.

    The row's Subject is the Claim's subject by key (a column) and its value
    (``$item.bindings.vuln``); the status Claim read is the derivation's input.
    """

    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from cruxible_client.contracts.query.grammar import (
        QueryClaimValueRef,
        QueryEntry,
        QueryProjection,
        QueryProjectionField,
    )
    from tests.test_procedures.test_claims_terminal import (
        KIND,
        PREDICATE,
        activate,
        candidate_claims,
        claim_type,
        claims_spec,
        claims_world,
        install,
        run,
    )

    same = claim_type(
        KIND + ".same_as",
        f"subject<{KIND}>",
        permitted_roles=("derivation",),
        default_role=None,
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="derived",
                    claim_roles=("derivation",),
                    capture_contracts=("test.orders-v1",),
                    admission="derivational",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": "CVE-2026-0001", "status": "open"}]},
        types=(claim_type(), same),
    )
    install(instance, claims_spec())
    first = run(instance, root)
    (status,) = candidate_claims(instance, first)
    activate(instance, first)
    subject_path = status.statement.subject.artifact_path
    subject_id = subject_path.removeprefix(f"subjects/{KIND}/").removesuffix(".json")

    row = validate_contract({"name": ROW, "version": 1, "fields": {"cve": "string{max_len: 256}"}})
    query = work_item_query("security.vulnerabilities").model_copy(
        update={
            "entry": QueryEntry(binding="vuln", subject_kinds=(KIND,)),
            "result_binding": "vuln",
            "projection": QueryProjection(
                fields=(
                    QueryProjectionField(
                        name="cve",
                        value=QueryClaimValueRef(binding="vuln", predicate=KIND + ".cve_id"),
                    ),
                )
            ),
            "row_contract": ContractRef(name=ROW, version=1, digest=contract_digest(row).tagged),
        }
    )
    query = type(query).model_validate(query.model_dump(mode="python", by_alias=True))
    accept_tree(
        instance,
        {
            contract_path(ROW, 1): render_contract(row),
            query_definition_path(query.identity.name): render_query_definition(query),
            claim_type_path(same.predicate): render_claim_type(same),
        },
        name="row-derivation",
    )
    install(
        instance,
        procedure_spec(
            "test.row-derivation",
            reference_text=True,
            input={},
            output={},
            steps=[
                {"read": "status", "claim_type": PREDICATE, "subject": subject_id},
                {"read": "vulns", "query": query.identity.name},
                {
                    "id": "write",
                    "for_each": "$steps.vulns.rows",
                    "subject": {"kind": KIND, "key": {"cve_id": "$item.cve"}},
                    "claims": [
                        {
                            "type": same.predicate,
                            "value": "$item.bindings.vuln",
                            "role": "derivation",
                            "inputs": ["$steps.status"],
                            "rationale": "The row's Subject; status {$steps.status.value}.",
                        }
                    ],
                },
            ],
        ),
        line_name="row-derivation-line",
    )

    second = run(instance, root, minute=1, line="row-derivation-line")

    (claim,) = candidate_claims(instance, second)
    assert claim.statement.predicate == same.predicate
    assert claim.statement.role == "derivation"
    assert claim.statement.subject.artifact_path == subject_path
    assert claim.statement.object.kind == "subject"
    assert claim.statement.object.address.artifact_path == subject_path


# -- the row-reference check judges new proposals, never a recorded law's replay ----------


@pytest.mark.parametrize("historical", [False, True])
def test_the_row_reference_check_never_runs_under_a_recorded_law(
    monkeypatch: pytest.MonkeyPatch, historical: bool
) -> None:
    from types import SimpleNamespace

    from cruxible_client.contracts.laws import ACCEPTANCE_LAWS
    from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
    from cruxible_core.proposals import proposals
    from tests.support.procedures import procedure_artifact

    artifact = procedure_artifact(_inbox_definition({"item": "$item.subject_id"}))
    law = ACCEPTANCE_LAWS.resolve_member(artifact_tag=artifact.artifact_format)
    reached: list[str] = []
    monkeypatch.setattr(
        proposals,
        "evaluate_procedure_law",
        lambda *_a, **_k: (
            reached.append("law") or SimpleNamespace(verdict="refused", diagnostics=())
        ),
    )
    context = SimpleNamespace(
        path=procedure_path(artifact.identity.name),
        content=render_procedure(artifact),
        parent_content=None,
        candidate_tree={
            query_definition_path(QUERY): render_query_definition(work_item_query()),
        },
        current=SimpleNamespace(compiler=None),
        acceptance_laws=ACCEPTANCE_LAWS,
        historical_law_coordinate=(
            (law.coordinate.identifier, law.coordinate.digest) if historical else None
        ),
        rechecking=False,
    )

    verdict = proposals._procedure_member(context)

    codes = [diagnostic.code for diagnostic in verdict.diagnostics]
    if historical:
        # Replayed under its recorded law, the member meets only that law's checks.
        assert "cruxible.procedure.item_reference_unresolved" not in codes
        assert reached == ["law"]
    else:
        assert codes == ["cruxible.procedure.item_reference_unresolved"]
        assert reached == []


# -- a path beneath a column is checked against the column's declared type --------------


def _owner_pin() -> dict[str, Any]:
    owner = validate_contract({"name": "test.owner", "version": 1, "fields": {"name": "string"}})
    return {"name": owner.name, "version": 1, "digest": contract_digest(owner).tagged}


def _typed_lookup(extra: dict[str, Any] | None = None) -> Any:
    from cruxible_client.contracts.query.grammar import QueryClaimValueRef, QueryProjectionField

    owner = validate_contract({"name": "test.owner", "version": 1, "fields": {"name": "string"}})
    columns = {
        "owner": {"type": {"kind": "contract", "contract": _owner_pin()}},
        **(extra or {}),
    }
    row = validate_contract(
        {
            "name": ROW,
            "version": 1,
            "fields": {"item_id": "string", "status": "string", **columns},
        }
    )
    query = _query(row)
    assert query.projection is not None
    added = tuple(
        QueryProjectionField(
            name=name, value=QueryClaimValueRef(binding="item", predicate=f"{SUBJECT_KIND}.{name}")
        )
        for name in columns
    )
    query = query.model_copy(
        update={
            "projection": query.projection.model_copy(
                update={"fields": (*query.projection.fields, *added)}
            )
        }
    )
    definitions = {
        f"QueryDefinition:{QUERY}": query,
        row.identity.qualified: row,
        owner.identity.qualified: owner,
    }
    return lambda identity: definitions.get(identity.qualified)


@pytest.mark.parametrize("reference", ["$item.status", "$item.owner.name", "$item.owner"])
def test_a_path_the_column_type_has_passes(reference: str) -> None:
    validate_row_references(_inbox_definition({"item": reference}), _typed_lookup())


@pytest.mark.parametrize(
    ("reference", "said"),
    [
        ("$item.status.kind", "column status is string"),
        ("$item.item_id.missing", "column item_id is string"),
        ("$item.owner.email", "declares no field 'email'"),
    ],
)
def test_a_path_beneath_a_column_its_type_lacks_refuses(reference: str, said: str) -> None:
    with pytest.raises(ItemReferenceUnresolved) as refused:
        validate_row_references(_inbox_definition({"item": reference}), _typed_lookup())

    assert reference in str(refused.value) and said in str(refused.value)
    assert refused.value.element == "fields.item"


# -- a kept row's Subject is its own, whatever its projected columns spell ---------------


def _record_shaped(locator: str, key: str, status: str) -> dict[str, Any]:
    """A Query row whose declared columns ``record`` and ``fields`` look like a transform record."""

    return {
        "record": locator,
        "fields": {"k": {"tag": "present", "value": key, "at": "/k"}},
        "status": status,
    }


@pytest.mark.parametrize(
    ("kind", "spec", "expected"),
    [
        # Both kept in order: a misleading locator must not swap their Subjects.
        ("dedupe", {"keys": ["k"]}, ["wi-42", "wi-43"]),
        # The first dropped: the kept row is the second served row, not "/0".
        (
            "filter",
            {
                "where": {
                    "op": "eq",
                    "left": {"$ref": "item", "path": ["fields", "k"]},
                    "right": {"literal": "b"},
                }
            },
            ["wi-43"],
        ),
        # A duplicate key dropped after the first: the kept row is the first.
        ("dedupe", {"keys": ["status"]}, ["wi-42"]),
    ],
)
def test_a_kept_rows_subject_is_its_own_through_filter_and_dedupe(
    kind: str, spec: dict[str, Any], expected: list[str]
) -> None:
    from cruxible_client.contracts.procedures.nodes import (
        TransformDedupeSpec,
        TransformFilterSpec,
    )
    from cruxible_core.procedures.execution import _resolve_reference
    from cruxible_core.procedures.transforms import apply_closed_transform

    rows = [_record_shaped("/1", "a", "same"), _record_shaped("x/0", "b", "same")]
    if kind == "filter":
        rows[1]["record"] = "/0"
    view = _read_view(
        rows,
        [
            _served(f"{SUBJECT_KIND}/wi-42", item=f"{SUBJECT_KIND}/wi-42"),
            _served(f"{SUBJECT_KIND}/wi-43", item=f"{SUBJECT_KIND}/wi-43"),
        ],
    )
    model = TransformFilterSpec if kind == "filter" else TransformDedupeSpec
    parsed = model.model_validate({"input": "$steps.work.rows", **spec})
    kept: list[int] = []
    value, _complete = apply_closed_transform(
        kind, parsed, lambda _reference: view["rows"], kept=kept
    )
    outputs = {"work": view, "narrowed": value}
    row_inputs = {"narrowed": ("$steps.work.rows", tuple(kept))}

    items = query_row_items("$steps.narrowed.rows", outputs, value["rows"], row_inputs) or []

    assert [
        _resolve_reference("$item.subject", input_payload={}, parameters={}, outputs={}, item=item)
        for item in items
    ] == [f"{SUBJECT_KIND}/{name}" for name in expected]
    # The rows themselves are exactly what the transform kept.
    assert [dict(item) for item in items] == value["rows"]


# -- a field-result column is its wire record, as the run reads it ----------------------


def _field_result_lookup() -> Any:
    pinned = {"kind": "contract", "contract": _owner_pin()}
    return _typed_lookup(
        {
            "seen": {"type": {"kind": "field_result", "value": {"kind": "string"}}},
            "held": {"type": {"kind": "field_result", "value": pinned}},
            "many": {
                "type": {
                    "kind": "list",
                    "max_items": 4,
                    "item": {"kind": "field_result", "value": {"kind": "string"}},
                }
            },
        }
    )


ROW_VALUE = {
    "seen": {"tag": "present", "value": "ready", "at": "/seen"},
    "held": {"tag": "present", "value": {"name": "ana"}, "at": "/held"},
    "many": [{"tag": "present", "value": "a", "at": "/many/0"}],
}


@pytest.mark.parametrize(
    ("reference", "resolved"),
    [
        ("$item.seen.value", "ready"),
        ("$item.seen.tag", "present"),
        ("$item.seen.at", "/seen"),
        ("$item.held.value.name", "ana"),
        ("$item.many.0.value", "a"),
    ],
)
def test_a_field_result_path_the_run_resolves_passes(reference: str, resolved: object) -> None:
    from cruxible_core.procedures.execution import _resolve_reference

    validate_row_references(_inbox_definition({"item": reference}), _field_result_lookup())
    assert (
        _resolve_reference(reference, input_payload={}, parameters={}, outputs={}, item=ROW_VALUE)
        == resolved
    )


@pytest.mark.parametrize(
    ("reference", "said"),
    [
        ("$item.held.name", "read its .value"),
        ("$item.seen.kind", "read its .value"),
        ("$item.seen.tag.more", "is text"),
        ("$item.held.value.email", "declares no field 'email'"),
        ("$item.many.0.name", "read its .value"),
    ],
)
def test_a_field_result_path_the_run_cannot_resolve_refuses(reference: str, said: str) -> None:
    from cruxible_core.procedures.execution import ExecutionError, _resolve_reference

    with pytest.raises(ItemReferenceUnresolved) as refused:
        validate_row_references(_inbox_definition({"item": reference}), _field_result_lookup())
    assert said in str(refused.value)
    with pytest.raises(ExecutionError):
        _resolve_reference(reference, input_payload={}, parameters={}, outputs={}, item=ROW_VALUE)
