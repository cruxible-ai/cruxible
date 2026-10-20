"""The ontology reads a kind page makes, answered from maintained counts and typed definitions.

Subject counts on every orient answer and on the SubjectKind card come from
``query_subject_counts``, which Subject writes maintain; no read recounts a kind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.query_selection import LIVE_SUBJECT_COUNT_SQL, LIVE_SUBJECT_COUNTS_SQL
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

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
