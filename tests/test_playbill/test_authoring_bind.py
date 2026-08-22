"""Client-side Flow-A binding and model-generated authoring examples."""

from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import TypeAdapter

from cruxible_core.playbill.authoring.bind import (
    AuthoringBindAmbiguityError,
    bind_working_selection,
)
from cruxible_core.playbill.authoring.examples import (
    AUTHORING_EXAMPLE_FACTORIES,
    claim_self_source_example,
)
from cruxible_core.playbill.authoring.models import AuthoringPayloadV1


def _stub() -> dict[str, object]:
    payload = claim_self_source_example().model_dump(mode="json")
    payload["source"] = {
        "tag": "playbill-working-selection-observation-v1",
        "source_id": "repo.work-items",
    }
    payload["citation_role"] = "evidence"
    return payload


def test_bind_derives_every_mechanical_field_from_one_byte_buffer() -> None:
    content = b"before\nstatus: ready\nafter\n"
    payload = bind_working_selection(
        _stub(),
        content=content,
        anchor="status: ready",
    )

    source = payload.source
    assert source.tag == "playbill-working-selection-observation-v1"
    assert source.coordinate.source_content_digest == (
        "sha256:" + hashlib.sha256(content).hexdigest()
    )
    assert source.coordinate.source_byte_length == len(content)
    assert source.selector.start_byte == content.index(b"status: ready")
    assert source.selector.end_byte == source.selector.start_byte + len(b"status: ready")
    assert base64.b64decode(source.selected_content_base64) == b"status: ready"


def test_window_lines_select_complete_surrounding_lines_without_inventing_newlines() -> None:
    content = b"zero\none\nstatus: ready\nthree"
    payload = bind_working_selection(
        _stub(),
        content=content,
        anchor="status: ready",
        window_lines=1,
    )

    assert payload.source.selected_content == b"one\nstatus: ready\nthree"


def test_ambiguous_anchor_names_all_overlapping_candidate_offsets() -> None:
    with pytest.raises(AuthoringBindAmbiguityError) as caught:
        bind_working_selection(_stub(), content=b"aaa", anchor="aa")

    assert caught.value.observed_occurrence_count == 2
    assert caught.value.candidate_byte_offsets == (0, 1)
    assert "playbill.authoring.anchor_ambiguous" in str(caught.value)


def test_every_example_is_constructed_as_a_valid_authoring_union_member() -> None:
    adapter = TypeAdapter(AuthoringPayloadV1)

    for factory in AUTHORING_EXAMPLE_FACTORIES.values():
        model = factory()
        assert adapter.validate_python(model.model_dump(mode="json")) == model
