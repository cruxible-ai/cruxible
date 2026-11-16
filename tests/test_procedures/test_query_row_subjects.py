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
    view = _read_view(
        [{"status": "ready"}, {"status": "done", "subject": "a column"}],
        [
            _served("project.work_item/wi-42", item="project.work_item/wi-42", owner=None),
            _served("project.work_item/wi-43", item="project.work_item/wi-43"),
        ],
    )

    items = query_row_items("$steps.work.rows", {"work": view}, view["rows"])

    assert items == [
        {
            "subject": "project.work_item/wi-42",
            # An optional binding that bound nothing is left out.
            "bindings": {"item": "project.work_item/wi-42"},
            "status": "ready",
        },
        {
            # A declared column of the same name wins.
            "subject": "a column",
            "bindings": {"item": "project.work_item/wi-43"},
            "status": "done",
        },
    ]
    # Only exactly a Query read's rows gain them.
    assert query_row_items("$steps.work.result.rows", {"work": view}, view["rows"]) is None
    assert query_row_items("$steps.other.rows", {"other": {"rows": []}}, []) is None


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


def _row_world(tmp_path: Path, fields: dict[str, Any]) -> Any:
    instance, _owner = seed_claims(tmp_path)
    row = validate_contract(
        {"name": ROW, "version": 1, "fields": {"item_id": "string", "status": "string"}}
    )
    item = validate_contract(
        {
            "name": ITEM,
            "version": 1,
            "fields": {
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
                InboxStep(
                    inbox=f"{ITEM}@1",
                    for_each=ref("$steps.work.rows"),
                    fields=fields,
                    dedupe_key=("item",),
                    id="post",
                ),
            ],
            returns=ReturnFields(fields={}),
        ),
    )
    return instance


def test_a_line_posts_one_inbox_item_per_row_naming_the_rows_subject(tmp_path: Path) -> None:
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )

    instance = _row_world(
        tmp_path,
        {
            "item": ref("$item.subject"),
            "bound": ref("$item.bindings.item"),
            "status": ref("$item.status"),
        },
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
    payloads = sorted(
        (store.get(entry.envelope.item_id).payload for entry in entries),
        key=lambda payload: payload["item"],
    )
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
