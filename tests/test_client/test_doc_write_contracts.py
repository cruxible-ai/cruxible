"""Typed document wires, bounded bytes, and unchanged older write shapes."""

import base64
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.authoring.models import (
    SectionSelector,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import FOREIGN_SOURCE_MAX_BYTES
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.get_reads import GetCoordinate
from cruxible_client.contracts.write import (
    AppendSection,
    DocEditObservation,
    DocOp,
    DocOutcome,
    Evidence,
    ReplaceSection,
    SectionEvidence,
    WriteOutcome,
    WriteRequest,
)


def _observation(content: bytes = b"# Reason\nWords\n") -> WorkingSelectionObservation:
    return WorkingSelectionObservation(
        source_id="repo.notes",
        coordinate=WorkingDigestCoordinate(
            source_content_digest=section_digest(content), source_byte_length=len(content)
        ),
        selected_content_base64=base64.b64encode(content).decode(),
        selected_bytes_digest=section_digest(content),
        selector=WorkingAnchorWindow(
            anchor="# Reason",
            start_byte=0,
            end_byte=len(content),
            observed_occurrence_count=1,
            section=SectionSelector(path=("Reason",)),
        ),
    )


def _edit(content: bytes = b"# Reason\nWords\n") -> dict[str, Any]:
    return {
        "source_id": "repo.notes",
        "op": "append",
        "section": ["Reason"],
        "after_digest": section_digest(content),
        "content_base64": base64.b64encode(content).decode(),
        "after_coordinate": {
            "kind": "observed_digest",
            "source_content_digest": section_digest(content),
            "source_byte_length": len(content),
        },
    }


@pytest.mark.parametrize(
    "operation",
    [
        AppendSection(file="agent-chosen.md", under=("Plan",), text="## Reason\nWords"),
        ReplaceSection(
            file="agent-chosen.md",
            section=("Plan", "Reason"),
            text="## New",
            expect="sha256:" + "a" * 64,
        ),
        ReplaceSection(file="agent-chosen.md", section=(), text=""),
    ],
)
def test_doc_op_discriminated_round_trip(operation: AppendSection | ReplaceSection) -> None:
    adapter = TypeAdapter(DocOp)
    assert adapter.validate_json(adapter.dump_json(operation)) == operation


def test_type_is_not_a_parameter_and_text_is_not_decorated() -> None:
    with pytest.raises(ValidationError):
        AppendSection.model_validate({"file": "notes.md", "text": "# A", "type": "decision"})
    text = "---\nagent: selected\n---\n# A\n"
    assert AppendSection(file="notes.md", text=text).text == text


@pytest.mark.parametrize("path", [(" untrimmed",), ("embedded\nheading",)])
def test_paths_are_exact_trimmed_heading_text(path: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        ReplaceSection(file="notes.md", section=path, text="# A")


def test_section_evidence_round_trip_and_observed_path_agreement() -> None:
    evidence = SectionEvidence(file="notes.md", section=("Reason",), observation=_observation())
    adapter = TypeAdapter(Evidence)
    assert adapter.validate_json(adapter.dump_json(evidence)) == evidence
    assert evidence.own_words is None
    assert SectionEvidence(file="notes.md", section=(), own_words=True).own_words is True
    with pytest.raises(ValidationError, match="heading path"):
        SectionEvidence(file="notes.md", section=("Other",), observation=_observation())


def test_edit_observation_is_locator_free_and_round_trips_in_request() -> None:
    edit = DocEditObservation.model_validate(_edit())
    assert edit.content == b"# Reason\nWords\n"
    request = WriteRequest(because="Record the rationale", docs=(edit,))
    assert not request.changes
    assert WriteRequest.model_validate_json(request.model_dump_json()) == request
    assert isinstance(request.docs[0], DocEditObservation)
    for field in ("file", "path", "locator"):
        with pytest.raises(ValidationError):
            DocEditObservation.model_validate({**_edit(), field: "notes.md"})
    with pytest.raises(ValidationError):
        DocEditObservation.model_validate({**_edit(), "source_id": "file:///notes.md"})


@pytest.mark.parametrize(
    "patch",
    [
        {"after_digest": "sha256:" + "0" * 64},
        {"content_base64": "not base64"},
        {"content_base64": "YQ==\n"},
        {"section": []},
        {"op": "replace"},
        {
            "after_coordinate": {
                "kind": "observed_digest",
                "source_byte_length": 1,
                "source_content_digest": "sha256:" + "a" * 64,
            }
        },
    ],
)
def test_edit_observation_refuses_inconsistent_bytes_and_coordinates(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocEditObservation.model_validate({**_edit(), **patch})


def test_subtree_budget_matches_foreign_source_budget() -> None:
    assert (
        len(DocEditObservation.model_validate(_edit(b"x" * FOREIGN_SOURCE_MAX_BYTES)).content)
        == FOREIGN_SOURCE_MAX_BYTES
    )
    with pytest.raises(ValidationError):
        DocEditObservation.model_validate(_edit(b"x" * (FOREIGN_SOURCE_MAX_BYTES + 1)))


def test_replace_preamble_observation_may_be_empty() -> None:
    raw = _edit(b"")
    raw.update(
        op="replace",
        section=[],
        before_digest=section_digest(b"old"),
        before_coordinate={
            "kind": "observed_digest",
            "source_byte_length": 3,
            "source_content_digest": section_digest(b"old"),
        },
    )
    assert DocEditObservation.model_validate(raw).content == b""


def test_write_requires_changes_or_docs_and_existing_request_bytes_stay_the_same() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        WriteRequest(because="Empty")
    request = WriteRequest(
        because="Words", changes=({"op": "set", "field": "status", "value": "ready"},)
    )
    raw = request.model_dump(mode="json")
    assert "docs" not in raw
    assert WriteRequest.model_validate(raw).model_dump(mode="json") == raw
    explicit = WriteRequest.model_validate({**raw, "docs": []})
    assert explicit.model_dump(mode="json")["docs"] == []
    docs_only = WriteRequest(because="Words", docs=(AppendSection(file="notes.md", text="# A"),))
    assert WriteRequest.model_validate_json(docs_only.model_dump_json()) == docs_only


def test_outcome_round_trip_and_old_outcome_field_presence() -> None:
    outcome = WriteOutcome(
        status="accepted", coordinate=GetCoordinate(git_oid="a" * 12, generation=1)
    )
    assert "docs" not in outcome.model_dump(mode="json")
    doc = DocOutcome(
        file="notes.md",
        section=("Reason",),
        after_digest=section_digest(b"# Reason\n"),
        written=True,
    )
    updated = outcome.model_copy(update={"docs": (doc,)})
    assert WriteOutcome.model_validate_json(updated.model_dump_json()) == updated
    assert updated.docs[0].written


@pytest.mark.parametrize("explicit", [{"file": "notes.md"}, {"section": []},
                                      {"file": "notes.md", "section": ["A"]}])
def test_ref_and_explicit_address_are_mutually_exclusive(explicit):
    with pytest.raises(ValidationError, match="cannot be combined"):
        SectionEvidence.model_validate({"kind": "section", "ref": "basis", **explicit})


def test_ref_only_evidence_round_trips():
    evidence = SectionEvidence(ref="basis")
    assert SectionEvidence.model_validate_json(evidence.model_dump_json()) == evidence
