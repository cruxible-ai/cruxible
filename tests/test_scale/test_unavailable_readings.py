"""A limit-one read pays for its page, even with damaged reading history."""

from types import SimpleNamespace

import pytest

from cruxible_client.contracts.procedures.readings import ProcedureReadingsRequest
from cruxible_core.exhaust.payloads import UnavailablePayload
from cruxible_core.service.procedures import measurements
from tests.test_procedures.test_procedure_measurement_readings import RECORD_AT, _world


@pytest.mark.parametrize("population", [10, 100])
def test_unavailable_reading_history_has_bounded_warm_work(tmp_path, monkeypatch, population):
    instance, _owner, procedure = _world(tmp_path)
    rows = [
        SimpleNamespace(
            record_digest=f"record-{sequence}",
            record=SimpleNamespace(event_kind="procedure_reading"),
        )
        for sequence in range(population)
    ]
    parsed = []
    selected = []

    class Journal:
        def read_head(self, *_args):
            return SimpleNamespace(sequence=len(rows))

        def all_records(self, *_args):
            selected.extend(rows)
            return rows

        def select_records(self, *_args, first_sequence, last_sequence, **_kwargs):
            result = rows[first_sequence - 1 : last_sequence]
            selected.extend(result)
            return result

    def parse(_instance, stored):
        parsed.append(stored)
        return UnavailablePayload("sha256:" + f"{rows.index(stored):064x}")

    journal = Journal()
    original_index = measurements.reading_partition_index

    def index(*_args, **_kwargs):
        return original_index(instance, journal=journal, stream=None, partition_id="lost-readings")

    monkeypatch.setattr(measurements, "_parse_reading", parse)
    monkeypatch.setattr(measurements, "reading_partition_index", index)
    cold = index()
    assert len(parsed) == population
    assert cold.unavailable_count == population
    assert len(cold.unavailable) == 1
    parsed.clear()
    selected.clear()
    result = measurements.service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=ProcedureReadingsRequest(limit=1),
        evaluation_time=RECORD_AT,
    )
    assert parsed == selected == []
    assert index() is cold
    assert result.readings == ()
    assert result.unavailable_reading_count == population
    assert len(result.unavailable_payloads) == 1
    assert all(contract.status == "unavailable" for contract in result.contracts)
    without_unavailability = result.model_copy(
        update={"unavailable_payloads": (), "unavailable_reading_count": 0}
    )
    # Contract summaries have their own fixed size. Measure the output added
    # by damaged history, which must stay bounded at both population sizes.
    assert len(result.model_dump_json()) - len(without_unavailability.model_dump_json()) < 200

    # Catch-up updates the count from one new record; unavailable metadata is
    # still one representative, with no historical unavailable-body traversal.
    rows.append(
        SimpleNamespace(
            record_digest="new-record", record=SimpleNamespace(event_kind="procedure_reading")
        )
    )
    extended = index()
    assert len(parsed) == 1
    assert len(selected) == 2  # prefix identity plus the new suffix
    assert extended.unavailable_count == population + 1
    assert extended.unavailable == cold.unavailable
