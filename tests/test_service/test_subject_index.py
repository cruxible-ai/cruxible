"""The Subject index names exactly the Subjects the full listing compiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.service.claims.subjects import (
    service_get_playbill_subject,
    service_list_playbill_subject_index,
    service_list_playbill_subjects,
)
from tests.core_support._knowledge_loop_support import seed_claims


def _listed(view) -> tuple[str, str, str, str]:
    facts = {fact["schema_id"]: fact["value"] for fact in view.facts}
    identity = facts["playbill.subject.identity"]
    state = facts["playbill.subject.lifecycle"]["lifecycle"]["state"]
    return (
        str(view.envelope["identity"]),
        identity["subject_kind"],
        identity["subject_id"],
        state,
    )


def test_the_index_and_the_listing_name_the_compiled_subjects_without_compiling_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = seed_claims(tmp_path)
    index = service_list_playbill_subject_index(instance)
    # What each Subject's compiled facts say it is, read one at a time.
    expected = sorted(
        _listed(service_get_playbill_subject(instance, identity=row.identity))
        for row in index.subjects
    )
    assert expected

    def compiled(*args, **kwargs):
        pytest.fail("neither the Subject index nor the list may compile Subject facts")

    monkeypatch.setattr(TypedStateReader, "facts_for", compiled)
    index = service_list_playbill_subject_index(instance)
    listing = service_list_playbill_subjects(instance)

    assert index.coordinate == listing.coordinate
    assert (
        sorted(
            (row.identity, row.subject_kind, row.subject_id, row.lifecycle)
            for row in index.subjects
        )
        == expected
    )
    assert (
        sorted(
            (f"Subject:{row.subject_kind}/{row.subject_id}", row.subject_kind, row.subject_id)
            + (row.lifecycle,)
            for row in listing.subjects
        )
        == expected
    )
