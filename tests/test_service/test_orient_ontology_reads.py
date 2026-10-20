"""The ontology reads a kind page makes, answered from maintained counts and typed definitions.

Subject counts on every orient answer and on the SubjectKind card come from
``query_subject_counts``, which Subject writes maintain; no read recounts a kind.
Full ClaimType descriptors and the ClaimType card carry the contention policy,
format version, revision and ledger path. Mandate reads carry their typed scope.
``orient(section="pins")`` pages the pins between live definitions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claim_types import (
    ClaimTypeContention,
    claim_type_contention,
    claim_type_format_version,
    claim_type_path,
)
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.orient import ORIENT_PIN_SOURCE_KINDS
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.query_selection import LIVE_SUBJECT_COUNT_SQL, LIVE_SUBJECT_COUNTS_SQL
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import CLAIM_TYPES, KIND, caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> Any:
    request = WriteRequest.model_validate({"because": "Ontology.", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _title(name: str) -> dict[str, Any]:
    return {"op": "set", "subject": f"{KIND}/{name}", "field": "title", "value": name}


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    instance = seed_write_surface(tmp_path)[0]
    _write(instance, *(_title(name) for name in ("a", "b", "c")))
    return instance


def _get(instance: PlaybillInstance, ref: str) -> Any:
    read = service_playbill_get(instance, request=GetRequest(ref=ref), access=_ACCESS)
    assert read.card is not None, read
    return read.card


def _sql(instance: PlaybillInstance, sql: str, args: tuple[object, ...] = ()) -> list[Any]:
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        return [tuple(row) for row in projection.typed.connection.execute(sql, args)]


def _plan(instance: PlaybillInstance, sql: str, args: tuple[object, ...] = ()) -> list[str]:
    return [str(row[3]) for row in _sql(instance, "EXPLAIN QUERY PLAN " + sql, args)]


# -- K0: Subject counts from the maintained counters ------------------------------------


def test_subject_counts_read_the_maintained_counters(instance: PlaybillInstance) -> None:
    (truth,) = _sql(
        instance, "SELECT count(*) FROM subjects WHERE subject_kind=? AND lifecycle='live'", (KIND,)
    )[0]
    assert truth >= 3
    kinds = service_playbill_orient(instance).kinds
    assert {item.kind: item.subjects for item in kinds}[KIND] == truth
    assert _get(instance, f"SubjectKind:{KIND}").live_subjects == truth
    _write(instance, _title("d"))
    assert _get(instance, f"SubjectKind:{KIND}").live_subjects == truth + 1
    detail = service_playbill_orient(instance, kind=KIND).kind_detail
    assert detail is not None and detail.subjects == truth + 1


def test_subject_counts_are_key_reads_of_the_counter_table(instance: PlaybillInstance) -> None:
    assert _plan(instance, LIVE_SUBJECT_COUNTS_SQL) == ["SCAN query_subject_counts"]
    assert _plan(instance, LIVE_SUBJECT_COUNT_SQL, (KIND,)) == [
        "SEARCH query_subject_counts USING PRIMARY KEY (subject_kind=? AND lifecycle=?)"
    ]


# -- K5: contention policy, format, revision and path on ClaimType reads ----------------


def test_claim_type_reads_carry_contention_format_revision_and_path(
    instance: PlaybillInstance,
) -> None:
    title, governs = f"{KIND}.title", f"{KIND}.governs"
    section = service_playbill_orient(instance, section="claim_types", limit=50).claim_types
    detail = service_playbill_orient(instance, kind=KIND).kind_detail
    assert section is not None and detail is not None
    rows = {row.predicate: row for row in section}
    by_kind = {row.predicate: row for row in detail.predicates}
    for predicate, selector in ((title, "only_contender"), (governs, "all")):
        expected = {
            "contention": {"selector": selector, "on_conflict": "unresolved"},
            "format": 1,
            "revision": 1,
            "path": claim_type_path(predicate),
        }
        for row in (rows[predicate], by_kind[predicate]):
            dumped = row.model_dump(mode="json")
            assert {key: dumped[key] for key in expected} == expected, dumped
        card = _get(instance, f"ClaimType:{predicate}").model_dump(mode="json")
        assert {key: card[key] for key in expected} == expected, card
    # The compact map's descriptors stay compact.
    kinds = service_playbill_orient(instance).kinds
    compact = next(item for item in kinds if item.kind == KIND).predicates[0]
    assert compact.contention is None and compact.path is None and compact.format is None


def test_contention_counts_admission_requirements() -> None:
    from cruxible_client.contracts.policies import (
        ClaimAdmissionPolicy,
        CorroborationRequirement,
        FreezeRequirement,
    )

    claim_type = CLAIM_TYPES[0]
    assert claim_type_contention(claim_type) == ClaimTypeContention(
        selector="only_contender", on_conflict="unresolved"
    )
    assert claim_type_contention(claim_type).model_dump(mode="json") == {
        "selector": "only_contender",
        "on_conflict": "unresolved",
    }
    refusing = claim_type.model_copy(
        update={
            "resolution_policy": claim_type.resolution_policy.model_copy(
                update={"conflict_result": "refuse"}
            ),
            "admission_policy": ClaimAdmissionPolicy(
                corroboration_requirements=(
                    CorroborationRequirement(
                        requirement_id="seen-twice",
                        query_definition_digest="sha256:" + "0" * 64,
                        min_count=2,
                    ),
                ),
                freeze_requirements=(
                    FreezeRequirement(
                        requirement_id="when-done",
                        while_predicate=f"{KIND}.status",
                        while_values=("done",),
                        frozen_predicates=(f"{KIND}.title",),
                    ),
                ),
            ),
        }
    )
    assert claim_type_contention(refusing).model_dump(mode="json") == {
        "selector": "only_contender",
        "on_conflict": "refuse",
        "corroboration": 1,
        "freeze": 1,
    }
    assert claim_type_format_version(claim_type) == 1


# -- K6: typed Mandate scope on the mandate reads -------------------------------------


def test_mandate_reads_carry_the_typed_scope_and_condition(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from tests.test_proposals.test_delegated_settlement import STATUS
    from tests.test_proposals.test_delegated_settlement import _world as settle_world

    instance, _mandate = settle_world(tmp_path)
    when = datetime(2026, 6, 1, tzinfo=UTC)
    scope = [
        {
            "claim_type": f"ClaimType:{STATUS}",
            "change_kinds": ["create", "revise"],
            "binds": "subject",
        }
    ]
    rows = service_playbill_orient(instance, section="mandates", evaluation_time=when).mandates
    assert rows is not None
    (row,) = (item for item in rows if item.mandate == "ProcedureMandate:triage")
    dumped = row.model_dump(mode="json")
    assert dumped["scope"] == scope
    assert dumped["condition_query"] == "QueryDefinition:project.settle-condition"
    assert "suspended" not in dumped and "subject_creation" not in dumped
    assert (dumped["grants"], dumped["state"]) == ("settle", "active")

    read = service_playbill_get(
        instance, request=GetRequest(ref="Mandate:triage", evaluation_time=when), access=_ACCESS
    )
    assert read.card is not None
    card = read.card.model_dump(mode="json")
    assert card["scope"] == scope
    assert card["condition"] == {
        "query": "QueryDefinition:project.settle-condition",
        "binding_parameter": "item_id",
        "required_fields": ["id"],
        "fallback": "propose",
    }
    assert "subject_scope" not in card and "suspended" not in card


# -- K3: the pins between live definitions ----------------------------------------------


def _definition_pins(instance: PlaybillInstance) -> set[tuple[str, str, str, str]]:
    """Every live definition's required pins, by a full read: the oracle."""

    from cruxible_core.indexes.typed_state import OWNER_BY_IDENTITY_KIND

    found: set[tuple[str, str, str, str]] = set()
    for kind in ORIENT_PIN_SOURCE_KINDS:
        table = OWNER_BY_IDENTITY_KIND[kind].table
        found.update(
            (str(a), str(b), str(c), str(d))
            for a, b, c, d in _sql(
                instance,
                "SELECT p.source_identity, p.pin_role, p.target_identity, p.target_digest "
                f"FROM pins p JOIN {table} s ON s.identity=p.source_identity "
                "WHERE p.edge_kind='required_pin' AND s.lifecycle='live'",
            )
        )
    return found


def test_the_pins_section_pages_every_live_definition_pin_once(tmp_path: Path) -> None:
    from tests.test_proposals.test_delegated_settlement import STATUS
    from tests.test_proposals.test_delegated_settlement import _world as settle_world

    instance, _mandate = settle_world(tmp_path)
    pages = [service_playbill_orient(instance, section="pins", limit=2)]
    while pages[-1].next_cursor is not None:
        pages.append(
            service_playbill_orient(instance, section="pins", limit=2, cursor=pages[-1].next_cursor)
        )
    rows = [row for page in pages for row in page.pins or ()]
    assert all(len(page.pins or ()) <= 2 for page in pages)
    assert [page.truncated for page in pages] == [True] * (len(pages) - 1) + [False]
    keys = [(row.source, row.role, row.target, row.target_digest) for row in rows]
    assert len(keys) == len(set(keys)) and set(keys) == _definition_pins(instance)
    assert [row.source for row in rows] == sorted(row.source for row in rows)
    assert not any(row.source.startswith(("Claim:", "Subject:")) for row in rows)
    mandate = {(row.role, row.target) for row in rows if row.source == "ProcedureMandate:triage"}
    assert ("claim-type", f"ClaimType:{STATUS}") in mandate
    assert ("condition-query", "QueryDefinition:project.settle-condition") in mandate
    assert any(role == "procedure" and target.startswith("Procedure:") for role, target in mandate)
    assert all(row.current for row in rows if row.source == "ProcedureMandate:triage")
    whole = service_playbill_orient(instance, section="pins", limit=500)
    assert whole.pins == tuple(rows) and not whole.truncated
    assert whole.next[0] == f"cruxible get {rows[0].source}"


def test_the_pins_section_seeks_definition_rows_only(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import _keyset_cursor
    from cruxible_core.service.discovery.pins import _current, pins_sql
    from cruxible_core.service.list_pages import ListCursorMismatch

    instance = seed_write_surface(tmp_path)[0]
    prefixes = [f"{kind}:".encode() for kind in ORIENT_PIN_SOURCE_KINDS]
    assert prefixes == sorted(prefixes)
    for kind in ORIENT_PIN_SOURCE_KINDS:
        plan = _plan(instance, pins_sql(kind), (f"{kind}:", -1, f"{kind};", 5))
        assert plan[0].startswith("SEARCH p USING INDEX sqlite_autoindex_pins_1"), plan
        assert not any(line.startswith("SCAN") for line in plan), plan
    title = f"ClaimType:{KIND}.title"
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        (digest,) = connection.execute(
            "SELECT artifact_digest FROM claim_types WHERE identity=?", (title,)
        ).fetchone()
        assert _current(connection, title, str(digest)) is True
        assert _current(connection, title, "sha256:" + "0" * 64) is False
        assert _current(connection, "ApprovalPolicy:instance", str(digest)) is None
    first = service_playbill_orient(instance, section="pins", limit=1)
    for key in (["x"], ["ClaimType:a", "one"]):
        foreign = _keyset_cursor(view="pins", served=first.coordinate, last_key=key)
        with pytest.raises(ListCursorMismatch):
            service_playbill_orient(instance, section="pins", cursor=foreign)
