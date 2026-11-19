"""Claim slot lookups seek one slot on a skewed projection, whatever the predicate's size.

SQLite plans these statements with no ``sqlite_stat1`` (an ANALYZE would break
``verify_schema``), so its heuristics choose between ``claims_by_subject_predicate``
and the wider ``claims_by_predicate`` / ``claims_by_lifecycle``: an ``ORDER BY``
the narrow index cannot serve, or an equality on ``lifecycle``, could tip it onto
a walk of every live Claim of the predicate. The accepted-Claim slot lookup did
exactly that (0.5 ms -> 11-27 ms per lookup at 22k Claims). Each statement on the
lowering, admission and write path is pinned here on a piece where one predicate
holds nearly every Claim: its plan names the slot index, and its VM steps per
lookup (a progress handler's count) do not grow from 1k to 10k Subjects.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.indexes import claim_units, subject_identity, typed_state
from cruxible_core.indexes.evaluated_state import LIVE_SLOT_CLAIM_PATHS_SQL
from cruxible_core.indexes.sqlite import claim_list_query
from cruxible_core.service.proposals import staleness

KEY, NOTE, POINTS = (
    "project.work_item.number",
    "project.work_item.note",
    "project.work_item.part_of",
)


def _subject(index: int) -> str:
    return f"subjects/project.work_item/item-{index:07d}.json"


def _piece(subjects: int) -> sqlite3.Connection:
    """A projection piece whose key predicate holds a Claim on every Subject."""

    connection = sqlite3.connect(":memory:")
    connection.executescript(typed_state.schema_sql())
    rows = []
    for index in range(subjects):
        for predicate in (KEY, NOTE, POINTS):
            if predicate == NOTE and index % 10:
                continue
            if predicate == POINTS and index % 5:
                continue
            identity = f"CLM-{index:024x}{len(predicate):08x}"
            pointing = predicate == POINTS
            rows.append(
                (
                    f"Claim:{identity}",
                    f"claims/{index % 256:02x}/{identity}.json",
                    _subject(index),
                    predicate,
                    "subject" if pointing else "literal",
                    _subject((index + 1) % subjects) if pointing else None,
                    "artifact-v1" if pointing else None,
                    "whole" if pointing else None,
                    None if pointing else "string",
                    None if pointing else f"ITEM-{index}",
                )
            )
    connection.executemany(
        "INSERT INTO claims(identity,path,format_tag,artifact_digest,revision,lifecycle,"
        "statement_digest,claim_type_identity,claim_type_digest,subject_path,"
        "subject_selector_scheme,subject_selector_value,predicate,role,object_kind,object_path,"
        "object_selector_scheme,object_selector_value,literal_type,literal_text) VALUES "
        "(?,?,'t','d',1,'live','s','ct','ctd',?,'artifact-v1','whole',?,'observation',?,?,?,?,?,?)",
        rows,
    )
    return connection


def _slot(index: int) -> tuple[str, ...]:
    return (_subject(index), KEY, "artifact-v1", "whole")


_LIST_BY_SUBJECT = claim_list_query(
    subject=SemanticAddress.whole_artifact(_subject(0)),
    predicate=None,
    include_retired=False,
    subject_kind=None,
)[0]
_LIST_BY_SLOT = claim_list_query(
    subject=SemanticAddress.whole_artifact(_subject(0)),
    predicate=KEY,
    include_retired=False,
    subject_kind=None,
)[0]

#: name -> (statement, its parameters for Subject ``i``, the index its plan must name).
STATEMENTS: dict[str, tuple[str, Callable[[int], tuple[object, ...]], str]] = {
    "evaluated_state.claim_items": (
        LIVE_SLOT_CLAIM_PATHS_SQL.format(claims="main.claims"),
        _slot,
        "claims_by_subject_predicate",
    ),
    "typed_state.LIVE_SELECTED_SLOT_CLAIMS_SQL": (
        typed_state.LIVE_SELECTED_SLOT_CLAIMS_SQL,
        _slot,
        "claims_by_subject_predicate",
    ),
    "typed_state.LIVE_SLOT_CLAIMS_SQL": (
        typed_state.LIVE_SLOT_CLAIMS_SQL,
        lambda i: (_subject(i), KEY, None),
        "claims_by_subject_predicate",
    ),
    "typed_state.LIVE_SLOT_CLAIM_DIGESTS_SQL": (
        typed_state.LIVE_SLOT_CLAIM_DIGESTS_SQL,
        lambda i: (_subject(i), KEY, None),
        "claims_by_subject_predicate",
    ),
    "typed_state.LIVE_SUBJECT_PREDICATE_CLAIMS_SQL": (
        typed_state.LIVE_SUBJECT_PREDICATE_CLAIMS_SQL,
        lambda i: (_subject(i), KEY),
        "claims_by_subject_predicate",
    ),
    "subject_identity.LIVE_KEY_CLAIMS_SQL": (
        subject_identity.LIVE_KEY_CLAIMS_SQL,
        lambda i: (_subject(i), KEY),
        "claims_by_subject_predicate",
    ),
    "claim_units._KEY_VALUES_SQL": (
        claim_units._KEY_VALUES_SQL,  # noqa: SLF001
        lambda i: (_subject(i), KEY),
        "claims_by_subject_predicate",
    ),
    "claim_units._POINTING_SQL": (
        claim_units._POINTING_SQL,  # noqa: SLF001
        lambda i: (_subject(i),),
        "claims_by_object_subject",
    ),
    "staleness._SLOT_CLAIMS_SQL": (
        staleness._SLOT_CLAIMS_SQL,  # noqa: SLF001
        lambda i: (_subject(i), KEY),
        "claims_by_subject_predicate",
    ),
    "sqlite.list_claims(subject)": (
        _LIST_BY_SUBJECT,
        lambda i: (_subject(i), "artifact-v1", "whole"),
        "claims_by_subject_predicate",
    ),
    "sqlite.list_claims(subject, predicate)": (
        _LIST_BY_SLOT,
        lambda i: (_subject(i), "artifact-v1", "whole", KEY),
        "claims_by_subject_predicate",
    ),
}

SIZES = (1_000, 10_000)
LOOKUPS = 40


@pytest.fixture(scope="module")
def pieces() -> dict[int, sqlite3.Connection]:
    return {size: _piece(size) for size in SIZES}


def _steps_per_lookup(
    connection: sqlite3.Connection, sql: str, parameters: Callable[[int], tuple[object, ...]]
) -> float:
    size = connection.execute("SELECT count(DISTINCT subject_path) FROM claims").fetchone()[0]
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(tick, 1)
    try:
        for lookup in range(LOOKUPS):
            connection.execute(sql, parameters(lookup * (size // LOOKUPS))).fetchall()
    finally:
        connection.set_progress_handler(None, 1)
    return steps / LOOKUPS


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_slot_statement_plans_on_the_slot_index(
    pieces: dict[int, sqlite3.Connection], name: str
) -> None:
    sql, parameters, index = STATEMENTS[name]
    for connection in pieces.values():
        plan = " | ".join(
            str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters(5))
        )
        assert f"USING INDEX {index}" in plan or f"USING COVERING INDEX {index}" in plan, plan
        assert "claims_by_predicate" not in plan and "claims_by_lifecycle" not in plan, plan


@pytest.mark.parametrize("name", sorted(STATEMENTS))
def test_slot_statement_steps_do_not_grow_with_the_predicate(
    pieces: dict[int, sqlite3.Connection], name: str
) -> None:
    sql, parameters, _index = STATEMENTS[name]
    small, large = (_steps_per_lookup(pieces[size], sql, parameters) for size in SIZES)
    # A seek is one step at any size; a deeper B-tree or a longer key is not growth.
    assert large <= small * 1.25 + 8, (name, small, large)
    assert large < 200, (name, large)
