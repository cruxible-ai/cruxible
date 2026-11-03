"""Notes: a workspace section linked `about` Subjects through `write` (D-R12, D-R17).

A note is a link, never a Claim: the section's own body is retained as its
workspace Capture, the link lands as one governed note artifact, linking again
moves it to the section's current bytes, and unlinking every Subject retires it.
"""

from __future__ import annotations

import base64
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    SectionSelector,
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.notes import note_id, note_path, parse_note
from cruxible_client.contracts.write import AboutChange, WriteRequest
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from tests.core_support._write_support import KIND, caller, seed_write_surface

SOURCE = "repo.notes"


def observed(text: bytes = b"# Reason\nWords\n", path: tuple[str, ...] = ("Reason",)):
    return WorkingSelectionObservation(
        source_id=SOURCE,
        coordinate=WorkingDigestCoordinate(
            source_content_digest=section_digest(text), source_byte_length=len(text)
        ),
        selected_content_base64=base64.b64encode(text).decode(),
        selected_bytes_digest=section_digest(text),
        selector=WorkingAnchorWindow(
            anchor=text.splitlines()[0].decode(),
            start_byte=0,
            end_byte=len(text),
            observed_occurrence_count=1,
            section=SectionSelector(path=path),
        ),
    )


def about(*subjects: str, unlink: tuple[str, ...] = (), text: bytes = b"# Reason\nWords\n"):
    return AboutChange(
        file=SOURCE,
        section=("Reason",),
        subjects=subjects,
        unlink=unlink,
        observation=observed(text),
    )


def note_at(instance, name: str):
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    content = tree.get(note_path(name))
    return None if content is None else parse_note(content, path=note_path(name))


def test_about_links_a_section_as_a_note_not_a_claim(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    result = service_playbill_write(
        instance,
        request=WriteRequest(because="Context for wi-1", changes=(about(f"{KIND}/wi-1"),)),
        caller=caller(),
    )
    assert result.status == "accepted", result
    (change,) = result.changes
    name = note_id(SOURCE, ("Reason",))
    assert (change.op, change.note, change.about) == ("about", name, (f"{KIND}/wi-1",))
    assert change.claim is None and change.verdict is None
    note = note_at(instance, name)
    assert note is not None and note.lifecycle.state == "live"
    assert [item.qualified for item in note.about] == [f"Subject:{KIND}/wi-1"]
    assert note.section_digest == section_digest(b"# Reason\nWords\n")
    assert instance.body_store().verify(note.capture_digest)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    assert not [path for path in tree if path.startswith("claims/") and "Reason" in path]


def test_link_again_unions_unlink_retires_and_restating_is_a_no_op(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    name = note_id(SOURCE, ("Reason",))

    def write(change: AboutChange):
        result = service_playbill_write(
            instance, request=WriteRequest(because="Note", changes=(change,)), caller=caller()
        )
        assert result.status == "accepted", result
        return result

    first = write(about(f"{KIND}/wi-1"))
    again = write(about(f"{KIND}/wi-1"))
    assert again.coordinate == first.coordinate
    write(about(f"{KIND}/wi-2", text=b"# Reason\nWords, revised\n"))
    note = note_at(instance, name)
    assert [item.qualified for item in note.about] == [
        f"Subject:{KIND}/wi-1",
        f"Subject:{KIND}/wi-2",
    ]
    assert note.section_digest == section_digest(b"# Reason\nWords, revised\n")
    assert note.lifecycle.predecessor_digest is not None
    write(AboutChange(file=SOURCE, section=("Reason",), unlink=(f"{KIND}/wi-1", f"{KIND}/wi-2")))
    assert note_at(instance, name).lifecycle.state == "retired"


def test_about_refuses_unknown_subject_and_unobserved_section(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    missing = service_playbill_write(
        instance,
        request=WriteRequest(because="Note", changes=(about(f"{KIND}/wi-404"),)),
        caller=caller(),
    )
    assert missing.refusal is not None
    assert missing.refusal.code in (
        "cruxible.write.subject_not_found",
        "cruxible.write.subject_unknown",
    ), missing.refusal
    unobserved = service_playbill_write(
        instance,
        request=WriteRequest(
            because="Note",
            changes=(AboutChange(file=SOURCE, section=("Reason",), subjects=(f"{KIND}/wi-1",)),),
        ),
        caller=caller(),
    )
    assert unobserved.refusal.code == "cruxible.write.section_evidence_unobserved"


def test_about_defaults_to_the_write_subject(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    result = service_playbill_write(
        instance,
        request=WriteRequest(
            because="Note",
            subject=f"{KIND}/wi-3",
            changes=(AboutChange(file=SOURCE, section=("Reason",), observation=observed()),),
        ),
        caller=caller(),
    )
    assert result.status == "accepted", result
    assert result.changes[0].about == (f"{KIND}/wi-3",)


def test_note_law_refuses_a_capture_of_another_section() -> None:
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.notes import Note, evaluate_note_law, note_identity

    note = Note(
        identity=note_identity(SOURCE, ("Reason",)),
        source_id=SOURCE,
        section=("Reason",),
        capture_digest="sha256:" + "1" * 64,
        section_digest=section_digest(b"x"),
        about=(ArtifactIdentity(kind="Subject", name=f"{KIND}/wi-1"),),
    )
    path = note_path(note.identity.name)
    live = frozenset({f"Subject:{KIND}/wi-1"})
    other = evaluate_note_law(
        note,
        path=path,
        predecessor=None,
        live_subjects=live,
        capture_section=(SOURCE, ("Other",), section_digest(b"x")),
    )
    assert other.diagnostics[0].code == "cruxible.note.capture_mismatch"
    gone = evaluate_note_law(
        note,
        path=path,
        predecessor=None,
        live_subjects=frozenset(),
        capture_section=(SOURCE, ("Reason",), section_digest(b"x")),
    )
    assert gone.diagnostics[0].code == "cruxible.note.subject_unavailable"
    ok = evaluate_note_law(
        note,
        path=path,
        predecessor=None,
        live_subjects=live,
        capture_section=(SOURCE, ("Reason",), section_digest(b"x")),
    )
    assert ok.verdict == "accepted" and ok.required_tier == "governed_write"


def test_unlink_without_an_observation_keeps_the_retained_bytes_then_retires(
    tmp_path: Path,
) -> None:
    """A section (or file) that is gone: unlinking one Subject edits links only."""

    instance, _ = seed_write_surface(tmp_path)
    name = note_id(SOURCE, ("Reason",))
    linked = service_playbill_write(
        instance,
        request=WriteRequest(because="Note", changes=(about(f"{KIND}/wi-1", f"{KIND}/wi-2"),)),
        caller=caller(),
    )
    assert linked.status == "accepted", linked
    before = note_at(instance, name)

    def unlink(*subjects: str):
        return service_playbill_write(
            instance,
            request=WriteRequest(
                because="Unlink",
                changes=(AboutChange(file=SOURCE, section=("Reason",), unlink=subjects),),
            ),
            caller=caller(),
        )

    partial = unlink(f"{KIND}/wi-1")
    assert partial.status == "accepted", partial
    assert partial.changes[0].about == (f"{KIND}/wi-2",)
    after = note_at(instance, name)
    assert [item.qualified for item in after.about] == [f"Subject:{KIND}/wi-2"]
    assert (after.capture_digest, after.section_digest) == (
        before.capture_digest,
        before.section_digest,
    )
    assert after.lifecycle.state == "live" and after.lifecycle.predecessor_digest is not None
    full = unlink(f"{KIND}/wi-2")
    assert full.status == "accepted", full
    assert note_at(instance, name).lifecycle.state == "retired"
    gone = unlink(f"{KIND}/wi-2")
    assert gone.refusal is not None and gone.refusal.code == "cruxible.write.note_not_found"
