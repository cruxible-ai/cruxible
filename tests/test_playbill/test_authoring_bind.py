"""Client-side Flow-A binding and model-generated authoring examples."""

from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.authoring.bind import (
    AuthoringBindAmbiguityError,
    AuthoringBindError,
    bind_working_selection_input,
)
from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_FACTORIES,
    claim_flow_a_example,
)
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimDispositionInput, ClaimInput
from cruxible_client.authoring.sdk_types import Disposition
from cruxible_client.contracts.authoring.models import AuthoringExistingClaimDispositionV1


def _input() -> ClaimInput:
    return claim_flow_a_example()


def test_bind_derives_every_mechanical_field_from_one_byte_buffer() -> None:
    content = b"before\nstatus: ready\nafter\n"
    payload = bind_working_selection_input(
        _input(),
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
    payload = bind_working_selection_input(
        _input(),
        content=content,
        anchor="status: ready",
        window_lines=1,
    )

    assert payload.source.selected_content == b"one\nstatus: ready\nthree"


def test_ambiguous_anchor_names_all_overlapping_candidate_offsets() -> None:
    with pytest.raises(AuthoringBindAmbiguityError) as caught:
        bind_working_selection_input(_input(), content=b"aaa", anchor="aa")

    assert caught.value.observed_occurrence_count == 2
    assert caught.value.candidate_byte_offsets == (0, 1)
    assert "playbill.authoring.anchor_ambiguous" in str(caught.value)
    assert "--occurrence" in str(caught.value)


def test_explicit_occurrence_selects_the_requested_overlapping_anchor() -> None:
    payload = bind_working_selection_input(
        _input(),
        content=b"aaa",
        anchor="aa",
        occurrence=2,
    )

    assert payload.source.selector.start_byte == 1
    assert payload.source.selector.end_byte == 3
    assert payload.source.selected_content == b"aa"
    assert payload.source.selector.observed_occurrence_count == 2


@pytest.mark.parametrize("occurrence", [0, 3])
def test_explicit_occurrence_must_name_an_observed_anchor(occurrence: int) -> None:
    with pytest.raises(AuthoringBindError, match="--occurrence"):
        bind_working_selection_input(
            _input(),
            content=b"aaa",
            anchor="aa",
            occurrence=occurrence,
        )


def test_every_example_is_constructed_as_a_valid_authoring_union_member() -> None:
    adapter = TypeAdapter(AuthoringInputV1)

    for factory in AUTHORING_EXAMPLE_FACTORIES.values():
        model = factory()
        assert adapter.validate_python(model.model_dump(mode="json")) == model


@pytest.mark.parametrize("disposition", ["not_tested", "support", "contradict", "unsure"])
def test_bind_preserves_the_exact_frozen_claim_disposition_vocabulary(disposition: str) -> None:
    item = ClaimDispositionInput(
        claim_id="CLM-" + "a" * 32,
        disposition=disposition,  # type: ignore[arg-type]
    )
    value = _input().model_copy(update={"dispositions": (item,)})

    payload = bind_working_selection_input(
        value,
        content=b"status: ready",
        anchor="status: ready",
    )

    assert payload.existing_claim_dispositions[0].disposition == disposition
    assert disposition in {member.value for member in Disposition}


def test_supersede_is_not_a_claim_input_or_governed_disposition() -> None:
    values = {"claim_id": "CLM-" + "a" * 32, "disposition": "supersede"}

    with pytest.raises(ValidationError, match="not_tested.*support.*contradict.*unsure"):
        ClaimDispositionInput.model_validate(values)
    with pytest.raises(ValidationError, match="not_tested.*support.*contradict.*unsure"):
        AuthoringExistingClaimDispositionV1.model_validate(values)
    assert "supersede" not in {member.value for member in Disposition}
