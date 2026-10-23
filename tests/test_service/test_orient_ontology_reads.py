"""The ontology reads a kind page makes, answered from maintained counts and typed definitions.

Subject counts on every orient answer and on the SubjectKind card come from
``query_subject_counts``, which Subject writes maintain; no read recounts a kind.
Full ClaimType descriptors and the ClaimType card carry the contention policy,
format version, revision and ledger path. Mandate reads carry their typed scope.
``orient(section="pins")`` pages the pins between live definitions. Per-predicate
Claim and Subject counts and the cited-Capture count come from counters the
projection's triggers maintain, never a recount on read.
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
from cruxible_core.indexes.evidence.citation_sql import CAPTURE_COUNT_SQL
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
    # The default map names a kind's fields only; descriptors are orient(kind=...)'s.
    kinds = service_playbill_orient(instance).kinds
    summary = next(item for item in kinds if item.kind == KIND).model_dump(mode="json")
    assert set(summary) <= {"kind", "subjects", "identity", "fields", "more_fields"}


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


# -- K1 (and K0's Capture count): counters Claim and Capture writes maintain ------------


def _claim_row(identity: str, *, predicate: str, subject: str, live: bool = True) -> tuple:
    """A minimal literal-null Claim row: only the counted columns matter here."""

    return (
        identity, f"claims/{identity}.json", "playbill-claim-v1", "digest", None, 1,
        "live" if live else "retired", "statement", f"ClaimType:{predicate}", "ct", subject,
        "path", "", predicate, None, "observation", "literal", None, None, None, None, None,
        None, "null", None, None, None, None, None, None,
    )  # fmt: skip


def _counts(connection: Any) -> tuple[list[Any], list[Any]]:
    return (
        connection.execute("SELECT * FROM predicate_counts ORDER BY 1,2").fetchall(),
        connection.execute("SELECT * FROM predicate_subjects ORDER BY 1,2").fetchall(),
    )


def _recount(connection: Any) -> tuple[list[Any], list[Any]]:
    """The oracle: what the counters must equal, by a full recount of live Claims."""

    kind = (
        "CASE WHEN substr(subject_path,1,9)='subjects/' AND instr(substr(subject_path,10),'/')>0 "
        "THEN substr(subject_path,10,instr(substr(subject_path,10),'/')-1) ELSE '' END"
    )
    return (
        connection.execute(
            f"SELECT {kind}, predicate, count(*), count(DISTINCT subject_path) FROM claims "
            "WHERE lifecycle='live' GROUP BY 1,2 ORDER BY 1,2"
        ).fetchall(),
        connection.execute(
            "SELECT predicate, subject_path, count(*) FROM claims WHERE lifecycle='live' "
            "GROUP BY 1,2 ORDER BY 1,2"
        ).fetchall(),
    )


def test_predicate_counts_follow_claim_inserts_moves_retirements_and_deletes() -> None:
    import random
    import sqlite3

    from cruxible_core.indexes.typed_state import schema_sql

    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    one, two = "subjects/k/one.json", "subjects/k/two.json"
    insert = "INSERT INTO claims VALUES (" + ",".join("?" * 30) + ")"
    connection.execute(insert, _claim_row("a", predicate="p", subject=one))
    connection.execute(insert, _claim_row("b", predicate="p", subject=one))
    connection.execute(insert, _claim_row("c", predicate="p", subject=two))
    assert _counts(connection) == ([("k", "p", 3, 2)], [("p", one, 2), ("p", two, 1)])
    connection.execute("UPDATE claims SET lifecycle='retired' WHERE identity='b'")
    assert _counts(connection)[0] == [("k", "p", 2, 2)]
    connection.execute("DELETE FROM claims WHERE identity='a'")
    assert _counts(connection) == ([("k", "p", 1, 1)], [("p", two, 1)])
    connection.execute("UPDATE claims SET subject_path=?, predicate='q' WHERE identity='c'", (one,))
    assert _counts(connection) == ([("k", "q", 1, 1)], [("q", one, 1)])
    connection.execute("UPDATE claims SET lifecycle='live' WHERE identity='b'")
    connection.execute(insert, _claim_row("d", predicate="p", subject="documents/x.json"))
    assert _counts(connection)[0] == [("", "p", 1, 1), ("k", "p", 1, 1), ("k", "q", 1, 1)]
    connection.commit()
    connection.execute("DELETE FROM claims")
    connection.rollback()
    assert _counts(connection) == _recount(connection)

    # Any sequence of writes leaves the counters equal to a recount.
    rng = random.Random(20261020)
    subjects = [f"subjects/{kind}/{n}.json" for kind in ("k", "m") for n in range(4)]
    for step in range(300):
        identity = f"r{rng.randrange(40)}"
        exists = connection.execute("SELECT 1 FROM claims WHERE identity=?", (identity,))
        if exists.fetchone() is None:
            connection.execute(
                insert,
                _claim_row(
                    identity,
                    predicate=rng.choice("pqr"),
                    subject=rng.choice(subjects),
                    live=rng.random() < 0.8,
                ),
            )
        elif step % 3 == 0:
            connection.execute("DELETE FROM claims WHERE identity=?", (identity,))
        else:
            connection.execute(
                "UPDATE claims SET lifecycle=?, predicate=?, subject_path=? WHERE identity=?",
                (
                    rng.choice(("live", "retired")),
                    rng.choice("pqr"),
                    rng.choice(subjects),
                    identity,
                ),
            )
    assert _counts(connection) == _recount(connection)


def test_the_capture_counter_follows_capture_inserts_and_deletes() -> None:
    import sqlite3

    from cruxible_core.indexes.typed_sqlite import complete_schema_sql

    connection = sqlite3.connect(":memory:")
    connection.executescript(complete_schema_sql())

    def count() -> Any:
        return connection.execute(CAPTURE_COUNT_SQL).fetchone()

    assert count() is None
    for digest in ("d1", "d2", "d3"):
        connection.execute(
            "INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (digest, "c", "e", None, None, "s", "p", 1, "public", "o", "commitment"),
        )
    connection.execute(
        "INSERT OR IGNORE INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("d1", "c", "e", None, None, "s", "p", 1, "public", "o", "commitment"),
    )
    assert count() == (3,)
    connection.execute("DELETE FROM captures WHERE capture_digest IN ('d1','d2')")
    assert count() == (1,)
    connection.execute("DELETE FROM captures")
    assert count() is None
    assert _plan_of(connection, CAPTURE_COUNT_SQL) == [
        "SEARCH capture_count USING INTEGER PRIMARY KEY (rowid=?)"
    ]


def _plan_of(connection: Any, sql: str, args: tuple[object, ...] = ()) -> list[str]:
    return [str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, args)]


def test_kind_and_claim_type_reads_count_from_the_predicate_counters(
    instance: PlaybillInstance,
) -> None:
    title, governs = f"{KIND}.title", f"{KIND}.governs"
    _write(
        instance,
        {"op": "add", "subject": f"{KIND}/a", "field": "governs", "value": f"{KIND}/b"},
        {"op": "add", "subject": f"{KIND}/a", "field": "governs", "value": f"{KIND}/c"},
        {"op": "add", "subject": f"{KIND}/b", "field": "governs", "value": f"{KIND}/c"},
    )

    def truth(predicate: str) -> tuple[int, int]:
        ((claims, subjects),) = _sql(
            instance,
            "SELECT count(*), count(DISTINCT subject_path) FROM claims "
            "WHERE predicate=? AND lifecycle='live'",
            (predicate,),
        )
        return int(claims), int(subjects)

    assert truth(governs) == (3, 2)
    detail = service_playbill_orient(instance, kind=KIND).kind_detail
    assert detail is not None
    by_name = {row.predicate: row for row in detail.predicates}
    for predicate in (title, governs, f"{KIND}.status"):
        row = by_name[predicate]
        assert (row.live_claims, row.subjects_with_value) == truth(predicate), predicate
        card = _get(instance, f"ClaimType:{predicate}")
        assert (card.live_claims, card.subjects_with_value) == truth(predicate), predicate
    _write(instance, {"op": "retire", "target": {"subject": f"{KIND}/b", "field": "governs"}})
    card = _get(instance, f"ClaimType:{governs}")
    assert (card.live_claims, card.subjects_with_value) == truth(governs) == (2, 1)
    # The default map's kind line carries field names, neither count.
    kinds = service_playbill_orient(instance).kinds
    summary = next(item for item in kinds if item.kind == KIND)
    assert governs.removeprefix(f"{KIND}.") in summary.fields
    assert "subjects_with_value" not in summary.model_dump(mode="json")


def test_the_predicate_counters_are_key_seeks(instance: PlaybillInstance) -> None:
    from cruxible_core.indexes.query_selection import (
        KIND_PREDICATE_COUNTS_SQL,
        PREDICATE_COUNTS_SQL,
    )

    assert _plan(instance, KIND_PREDICATE_COUNTS_SQL, (KIND,)) == [
        "SEARCH predicate_counts USING PRIMARY KEY (subject_kind=?)"
    ]
    (plan,) = _plan(instance, PREDICATE_COUNTS_SQL, (f"{KIND}.title",))
    assert plan.startswith("SEARCH predicate_counts USING ") and plan.endswith(
        "INDEX predicate_counts_by_predicate (predicate=?)"
    ), plan
    assert _plan(instance, CAPTURE_COUNT_SQL) == [
        "SEARCH capture_count USING INTEGER PRIMARY KEY (rowid=?)"
    ]
