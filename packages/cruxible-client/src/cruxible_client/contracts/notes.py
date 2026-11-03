"""Note artifact: a workspace section linked ``about`` Subjects (D-R12, D-R17).

A note is prose in a workspace file, attached to a symbolic point: one section
(the catalogued source and its heading path) linked ``about`` the Subjects it
discusses. It is a link, never a Claim: it states no value, and nothing reads
its prose as a fact. The section's own body at linking time is retained as the
workspace Capture the note cites (the same Capture a section citation makes),
so a later edit of the section reads as "changed since cited".

One note per section: its identity derives from the source and the heading
path, so linking a section again revises that note (the link moves to the
section's current bytes) and unlinking every Subject retires it. A retired note
may be revived by linking the section again; a link carries no decision a
revival could contradict.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.doc_sections import SectionPath
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.semantic import SemanticAddress

NOTE_ARTIFACT_FORMAT = "playbill-note-v1"
#: The one relation a note carries; core indexes it as a backlink.
NOTE_RELATION = "about"
#: Subjects one note may be about: a bound on the link set, not on prose.
MAX_NOTE_SUBJECTS = 64
_NOTE_NAME_RE = re.compile(r"^NOTE-[0-9a-f]{32}$")
#: The one path a note artifact lives at; any other path under ``notes/`` is no note.
NOTE_PATH_RE = re.compile(r"^notes/NOTE-[0-9a-f]{32}\.json$")
_SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class NoteFormatError(FormatError):
    """A note artifact that is not canonical, or names no section."""


class _StrictNoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def note_id(source_id: str, section: SectionPath) -> str:
    """The note of one section: the same source and heading path name the same note."""

    digest = typed_digest(
        Sha256Value,
        "cruxible-note-id-v1",
        {"section": list(section), "source_id": source_id},
    ).tagged.removeprefix("sha256:")
    return f"NOTE-{digest[:32]}"


def note_identity(source_id: str, section: SectionPath) -> ArtifactIdentity:
    return ArtifactIdentity(kind="Note", name=note_id(source_id, section))


def _subject_identity(value: ArtifactIdentity) -> ArtifactIdentity:
    if value.kind != "Subject" or "/" not in value.name:
        raise ValueError("a note is about Subjects (Subject:kind/id)")
    return value


class Note(_StrictNoteModel):
    """One workspace section linked about Subjects; the section's bytes are a Capture."""

    artifact_format: Literal["playbill-note-v1"] = "playbill-note-v1"
    identity: ArtifactIdentity
    source_id: str = Field(description="The catalogued workspace source (files.yaml name).")
    section: SectionPath = Field(min_length=1, description="The heading path of the section.")
    capture_digest: str = Field(description="The workspace Capture of the section's own body.")
    section_digest: str = Field(description="sha256 of the section's own body when linked.")
    about: tuple[ArtifactIdentity, ...] = Field(min_length=1, max_length=MAX_NOTE_SUBJECTS)
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("source_id")
    @classmethod
    def _source_id(cls, value: str) -> str:
        if not _SOURCE_ID_RE.fullmatch(value):
            raise ValueError("note source_id must be a canonical catalog name")
        return value

    @field_validator("capture_digest")
    @classmethod
    def _capture(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value

    @field_validator("section_digest")
    @classmethod
    def _section_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("about")
    @classmethod
    def _about(cls, value: tuple[ArtifactIdentity, ...]) -> tuple[ArtifactIdentity, ...]:
        for item in value:
            _subject_identity(item)
        keys = [item.qualified.encode("utf-8") for item in value]
        if keys != sorted(set(keys)):
            raise ValueError("note Subjects must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _identity(self) -> Note:
        if self.identity != note_identity(self.source_id, self.section):
            raise ValueError("note identity must derive from its source and section")
        return self


def note_path(name: str) -> str:
    if not _NOTE_NAME_RE.fullmatch(name):
        raise NoteFormatError("note identity is not path-addressable")
    return f"notes/{name}.json"


def render_note(note: Note) -> bytes:
    return pretty_canonical_bytes(note.model_dump(mode="json"))


def note_digest(note: Note) -> ArtifactDigest:
    return typed_digest(ArtifactDigest, "playbill-note-artifact-v1", note.model_dump(mode="json"))


def parse_note(content: bytes, *, path: str) -> Note:
    try:
        note = Note.model_validate(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NoteFormatError("note failed strict validation") from exc
    if note_path(note.identity.name) != path:
        raise NoteFormatError("note identity/path disagreement")
    if render_note(note) != content:
        raise NoteFormatError("note is not in canonical wire form")
    return note


def capture_section(envelope: object) -> tuple[str, SectionPath, str] | None:
    """(source, heading path, bytes digest) a workspace section Capture records.

    Read from the Capture's typed coordinate metadata -- the selector a client
    observed -- never from its bytes. None for any other Capture.
    """

    from cruxible_client.contracts.captures import FOREIGN_SOURCE_SELECTOR_TYPE
    from cruxible_client.contracts.source_references import ExternalSourceReference

    source = getattr(envelope, "source", None)
    if (
        not isinstance(source, ExternalSourceReference)
        or source.selector_type != FOREIGN_SOURCE_SELECTOR_TYPE
    ):
        return None
    selector = source.model_dump(mode="json")["selector"]
    window = selector.get("working_selection") if isinstance(selector, dict) else None
    section = window.get("section") if isinstance(window, dict) else None
    path = section.get("path") if isinstance(section, dict) else None
    if not isinstance(path, list) or not path or not all(isinstance(p, str) for p in path):
        return None
    commitment = getattr(envelope, "commitment", None)
    digest = getattr(commitment, "digest", None)
    if not isinstance(digest, str):
        return None
    return source.source_identity, tuple(path), digest


class AcceptedNote(_StrictNoteModel):
    path: str
    note: Note
    artifact_digest: str


class NoteLawResult(_StrictNoteModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> NoteLawResult:
    return NoteLawResult(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_note_law(
    note: Note,
    *,
    path: str,
    predecessor: AcceptedNote | None,
    live_subjects: frozenset[str],
    capture_section: tuple[str, SectionPath, str] | None,
) -> NoteLawResult:
    """Judge one note: its succession, its Subjects, and the Capture it cites.

    ``live_subjects`` are the note's ``about`` identities live in the candidate.
    ``capture_section`` is (source, heading path, section digest) the cited
    Capture records, or None when it is absent or is not a section Capture.
    A retiring note is judged for its succession only.
    """

    if path != note_path(note.identity.name):
        return _refusal(
            "cruxible.note.path_mismatch", "Note identity/path disagreement.", path=path
        )
    if predecessor is None:
        if note.lifecycle.predecessor_digest is not None:
            return _refusal(
                "cruxible.note.invalid_genesis", "A new note cannot name a predecessor.", path=path
            )
        if note.lifecycle.state == "retired":
            return _refusal(
                "cruxible.note.invalid_genesis", "A new note cannot begin retired.", path=path
            )
    elif note.lifecycle.predecessor_digest != predecessor.artifact_digest:
        return _refusal(
            "cruxible.note.predecessor_mismatch",
            "A note successor does not pin its exact predecessor.",
            path=path,
        )
    if note.lifecycle.state == "live":
        missing = [item.qualified for item in note.about if item.qualified not in live_subjects]
        if missing:
            return _refusal(
                "cruxible.note.subject_unavailable",
                f"A note links only live Subjects; not live here: {', '.join(missing[:5])}.",
                path=path,
            )
        if capture_section != (note.source_id, note.section, note.section_digest):
            return _refusal(
                "cruxible.note.capture_mismatch",
                "The note's Capture is not the observed body of its own section.",
                path=path,
            )
    return NoteLawResult(
        verdict="accepted",
        artifact_digest=note_digest(note).tagged,
        required_tier="governed_write",
    )


__all__ = [
    "MAX_NOTE_SUBJECTS",
    "NOTE_ARTIFACT_FORMAT",
    "NOTE_PATH_RE",
    "NOTE_RELATION",
    "AcceptedNote",
    "Note",
    "NoteFormatError",
    "NoteLawResult",
    "capture_section",
    "evaluate_note_law",
    "note_digest",
    "note_id",
    "note_identity",
    "note_path",
    "parse_note",
    "render_note",
]
