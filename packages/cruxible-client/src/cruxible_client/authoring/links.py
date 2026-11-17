"""``cruxible link``: a note's ``about`` link on a section, or a page's ``guides`` link.

Two links in two lanes, one verb:

- ``link FILE#Heading/Path --about kind/id`` links a *section* about Subjects:
  a note, saying what the text is about. A value-lane write (an ``about``
  change), under the same governance as ``write``; ``backlinks`` on the
  Subject lists it.
- ``link FILE --guides ClaimType:NAME`` (or ``Procedure:NAME``) links a
  *whole registered page* to a definition as its review guidance. A
  definition-lane change: the link is recorded on the file's catalog entry in
  ``files.yaml`` and the Document's next revision is proposed from the file's
  current bytes plus its links, exactly as ``files propose`` proposes it (and
  every later ``files propose`` keeps the links). ``proposal review`` shows it.

FILE is a path or a catalog name, resolved as every citation resolves it.
Arguments that mix the two refuse with a typed refusal saying which is which.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict

from cruxible_client import contracts
from cruxible_client._error_base import CoreError
from cruxible_client.authoring.files import (
    _catalog_lock,
    _portable_catalog_path,
    _read_portable,
    _write_portable,
    compile_client_file_context,
    media_type_for,
)
from cruxible_client.authoring.selectors import WorkspaceFiles, workspace_files
from cruxible_client.contracts.authoring_landing import AuthoringAccept
from cruxible_client.contracts.documents import DocumentLink
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.file_catalog import (
    FileCatalog,
    FileCatalogEntry,
    FileCompilationBundle,
)
from cruxible_client.contracts.workspace_layout import workspace_path
from cruxible_client.contracts.write import (
    SUBJECT_REF_PATTERN,
    AboutChange,
    WriteAccept,
    WriteRequest,
    subject_reference,
)

#: What a note is and what guidance is, said in every refusal that mixes them.
NOTE_VS_GUIDANCE = (
    "A note links a section --about Subjects and says what that text is about "
    "(value lane; backlinks on the Subject list it). Guidance links a whole registered "
    "page --guides a ClaimType or Procedure as its review guidance (definition lane; "
    "proposal review shows it)."
)

#: The refusals of mismatched ``link`` arguments; each names its repair.
LINK_GUIDES_NEEDS_DEFINITION = "cruxible.link.guides_needs_definition"
LINK_ABOUT_NEEDS_SUBJECT = "cruxible.link.about_needs_subject"
LINK_ABOUT_NEEDS_SECTION = "cruxible.link.about_needs_section"
LINK_GUIDES_NEEDS_PAGE = "cruxible.link.guides_needs_page"
LINK_FILE_UNREGISTERED = "cruxible.link.file_unregistered"
LINK_FILE_AMBIGUOUS = "cruxible.link.file_ambiguous"
LINK_FILE_OVERLAID = "cruxible.link.file_overlaid"
LINK_ONE_LANE = "cruxible.link.one_lane"

#: A Document a ``link --guides`` declares on an evidence-only catalog entry.
GUIDANCE_DOCUMENT_KIND = "guidance"
GUIDANCE_SCOPE = "guidance"

_DEFINITION_KINDS = ("ClaimType", "Procedure")


class _FileContextProposer(Protocol):
    def file_context(
        self, instance_id: str, *, documents: Sequence[str] | None = None
    ) -> contracts.FileContext: ...

    def propose_file_bundle(
        self,
        instance_id: str,
        *,
        bundle: dict[str, object],
        file: str,
        proposal_name: str,
        dry_run: bool | None = None,
        at: str | None = None,
        accept: AuthoringAccept = "if_allowed",
    ) -> contracts.ProposalInspection: ...


class GuidanceLinkOutcome(BaseModel):
    """A ``link --guides``: the catalog entry's links and the Document proposal made from them.

    ``catalog_written`` says whether ``files.yaml`` now records ``links`` (never
    on a dry run, nor when the proposal was refused). ``declared_document``
    says the entry was evidence-only and the link declared its Document
    (``document_id`` is the file's catalog name), so the file still compiles to
    one Document, never a second.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    document_id: str
    links: tuple[DocumentLink, ...]
    catalog_written: bool
    #: The proposal stood but the entry changed in files.yaml meanwhile, so it
    #: was left as it now reads; run the link again to record it.
    catalog_conflict: bool = False
    declared_document: bool = False
    proposal: contracts.ProposalInspection


def split_link_source(source: str) -> tuple[str, tuple[str, ...] | None]:
    """``FILE#Heading/Path`` to its file and heading path; a bare ``FILE`` to (FILE, None)."""

    file, mark, rest = source.partition("#")
    if not file:
        raise WriteRefusalError(
            LINK_ABOUT_NEEDS_SECTION,
            f"{source!r} names no file",
            repair_line="Name a section as FILE#Heading/Path, or a whole page as FILE",
        )
    if not mark:
        return file, None
    return file, tuple(part for part in rest.split("/") if part)


def _refuse_mixed(code: str, message: str, repair: str) -> WriteRefusalError:
    return WriteRefusalError(code, f"{message}. {NOTE_VS_GUIDANCE}", repair_line=repair)


def about_subjects(values: Iterable[str]) -> tuple[str, ...]:
    """Each ``--about`` value as a Subject reference, refusing a definition given instead."""

    subjects: list[str] = []
    for value in values:
        kind, colon, rest = value.partition(":")
        if colon and kind in _DEFINITION_KINDS:
            raise _refuse_mixed(
                LINK_ABOUT_NEEDS_SUBJECT,
                f"--about takes a Subject (kind/id), and {value} is a {kind}",
                f"Link a page as its guidance instead: cruxible link FILE --guides {value}",
            )
        reference = rest if colon and kind == "Subject" else value
        reference = str(subject_reference(reference))
        if not re.fullmatch(SUBJECT_REF_PATTERN, reference):
            raise WriteRefusalError(
                LINK_ABOUT_NEEDS_SUBJECT,
                f"--about takes a Subject as kind/id; {value!r} is not one",
                repair_line="Name the Subject as kind/id, e.g. --about project.work_item/wi-1",
            )
        subjects.append(reference)
    return tuple(subjects)


def guides_targets(values: Iterable[str]) -> tuple[str, ...]:
    """Each ``--guides`` value as ``ClaimType:NAME`` or ``Procedure:NAME``."""

    targets: list[str] = []
    for value in values:
        kind, colon, rest = value.partition(":")
        if colon and kind in _DEFINITION_KINDS and rest:
            targets.append(value)
            continue
        if (colon and kind == "Subject") or (not colon and "/" in value):
            subject = rest if colon else value
            raise _refuse_mixed(
                LINK_GUIDES_NEEDS_DEFINITION,
                f"--guides takes a ClaimType or Procedure, and {value} is a Subject",
                f"Link a section about it instead: cruxible link FILE#Heading --about {subject} "
                '--because "..."',
            )
        raise WriteRefusalError(
            LINK_GUIDES_NEEDS_DEFINITION,
            f"--guides takes ClaimType:NAME or Procedure:NAME; {value!r} names neither",
            repair_line=f"Name the definition with its kind: --guides ClaimType:{value}",
        )
    return tuple(targets)


def about_link_request(
    source: str,
    *,
    subjects: Sequence[str],
    unlink: bool,
    because: str,
    dry_run: bool = False,
    accept: WriteAccept = "if_allowed",
    at: object = None,
    surface: Literal["cli", "mcp", "sdk"] = "cli",
) -> WriteRequest:
    """The write a ``link FILE#Heading --about`` sends: one about change on that section."""

    file, section = split_link_source(source)
    refs = about_subjects(subjects)
    if not section:
        raise _refuse_mixed(
            LINK_ABOUT_NEEDS_SECTION,
            f"--about links a section, and {source} names "
            + ("the preamble" if section == () else "a whole file"),
            f"Name the section: cruxible link {file}#Heading --about {' --about '.join(refs)} "
            '--because "..."; or link the page as guidance with --guides',
        )
    fields: dict[str, object] = {
        "because": because,
        "changes": (
            AboutChange(
                file=file,
                section=section,
                subjects=() if unlink else refs,
                unlink=refs if unlink else (),
            ),
        ),
        "dry_run": dry_run,
        "accept": accept,
        "surface": surface,
    }
    if at is not None:
        fields["at"] = at
    return WriteRequest.model_validate(fields)


def _title(path: Path, fallback: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and line[2:].strip():
                return line[2:].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return fallback


def _overlay_entry(root: Path, name: str) -> FileCatalogEntry | None:
    """``name``'s entry in the local overlay catalog (``.cruxible/files.local.yaml``), if any."""

    path = workspace_path(root, "files.local.yaml")
    if not path.is_file():
        return None
    try:
        catalog = FileCatalog.model_validate(yaml.safe_load(path.read_bytes()))
    except (ValueError, OSError, yaml.YAMLError) as exc:
        raise WriteRefusalError(
            LINK_FILE_OVERLAID,
            f".cruxible/files.local.yaml cannot be read: {exc}",
            repair_line="Repair or remove .cruxible/files.local.yaml, then link again",
        ) from None
    return next(
        (
            item
            for item in catalog.entries
            if isinstance(item, FileCatalogEntry) and item.name == name
        ),
        None,
    )


def _guidance_entry(
    files: WorkspaceFiles, file: str, *, root: Path
) -> tuple[FileCatalogEntry, Path]:
    """The one catalog entry ``file`` (a path or name) names, or a typed refusal.

    Resolved once, by the rule every citation uses (an existing path wins,
    otherwise a catalog name), then matched by path: a resolved path is never
    read again as a name.
    """

    try:
        path = files.locate(file)
        matches = files.entries_for_path(path)
    except ValueError:
        matches = ()
    if len(matches) > 1:
        names = ", ".join(entry.name for entry in matches)
        raise WriteRefusalError(
            LINK_FILE_AMBIGUOUS,
            f"{file} is catalogued under several names ({names}); guidance links the one "
            "page its Document compiles from",
            repair_line=f"Keep one entry: cruxible files remove {matches[-1].name}",
        )
    if not matches:
        raise WriteRefusalError(
            LINK_FILE_UNREGISTERED,
            f"{file} is not a registered workspace file; guidance links a page in the "
            "file catalog, the one its Document compiles from",
            repair_line=f"Register it first: cruxible files add {shlex.quote(file)}",
        )
    return matches[0], path


def link_guides(
    client: _FileContextProposer,
    instance_id: str,
    *,
    workspace: Path,
    source: str,
    guides: Sequence[str],
    unlink: bool = False,
    proposal_name: str | None = None,
    dry_run: bool = False,
    accept: AuthoringAccept = "if_allowed",
    at: str | None = None,
) -> GuidanceLinkOutcome:
    """Record ``guides`` links on FILE's catalog entry and propose its Document's next revision.

    The proposal is ``files propose``'s, compiled from the file's current bytes
    and the entry's links as they will read. The catalog is written only after
    the proposal stands (landed, or awaiting approval), never on a dry run, so
    a refused link leaves ``files.yaml`` as it was. The catalog lock is held to
    read the entry and again to write it, not across the daemon call: an entry
    changed in between is left alone (``catalog_conflict``). An evidence-only entry
    (one a write catalogued, ``doc-<path>``) declares its Document in place,
    under its catalog name: the file keeps one entry and compiles to one
    Document.
    """

    file, section = split_link_source(source)
    targets = guides_targets(guides)
    if section is not None:
        raise _refuse_mixed(
            LINK_GUIDES_NEEDS_PAGE,
            f"--guides links a whole registered page, and {source} names a section",
            f"Link the page: cruxible link {file} --guides {' --guides '.join(targets)}; "
            "or link the section about Subjects with --about",
        )
    root = workspace.expanduser().resolve()
    with _catalog_lock(root):
        try:
            files = workspace_files(root)
        except ValueError:
            raise WriteRefusalError(
                LINK_FILE_UNREGISTERED,
                f"{file} is not a registered workspace file (the workspace has no file catalog)",
                repair_line=f"Register it first: cruxible files add {shlex.quote(file)}",
            ) from None
        entry, path = _guidance_entry(files, file, root=root)
        catalog_path = _portable_catalog_path(root)
        portable = _read_portable(catalog_path)
        original = next(
            (
                item
                for item in portable
                if isinstance(item, FileCatalogEntry) and item.name == entry.name
            ),
            None,
        )
        if original is None:
            raise WriteRefusalError(
                LINK_FILE_UNREGISTERED,
                f"{entry.name} is catalogued only in the local overlay; guidance is recorded "
                "in the portable catalog (.cruxible/files.yaml)",
                repair_line=f"Register it there: cruxible files add {entry.name} {entry.locator}",
            )
        overlay = _overlay_entry(root, entry.name)
        if overlay is not None:
            # The overlay replaces the whole entry when catalogs merge, links
            # included: links recorded in the portable entry would be hidden.
            raise WriteRefusalError(
                LINK_FILE_OVERLAID,
                f"{entry.name} is also catalogued in .cruxible/files.local.yaml, whose entry "
                "replaces the portable one (links included) when the catalogs merge; guidance "
                "links are recorded in the portable catalog",
                repair_line=f"Drop {entry.name} from .cruxible/files.local.yaml, then link again",
            )
        # The merged entry is the portable one (no overlay names it).
        entry = original
        current = {link.target_identity: link for link in entry.links if link.relation == "guides"}
        others = tuple(link for link in entry.links if link.relation != "guides")
        if unlink:
            for target in targets:
                current.pop(target, None)
        else:
            for target in targets:
                current[target] = DocumentLink(relation="guides", target_identity=target)
        fields = entry.model_dump(exclude={"links"})
        declared = not entry.is_document
        if declared:
            fields.update(
                document_id=entry.name,
                document_kind=GUIDANCE_DOCUMENT_KIND,
                title=_title(path, entry.locator),
                media_type=media_type_for(path) or "text/plain",
                governance_scope=(GUIDANCE_SCOPE,),
            )
        revised = FileCatalogEntry.model_validate({**fields, "links": (*others, *current.values())})
    assert revised.document_id is not None
    # Only the linked page compiles, against only its own accepted Document:
    # the work is this page's, not the catalog's or the instance's.
    bundle: FileCompilationBundle = compile_client_file_context(
        client,
        instance_id,
        catalog=FileCatalog(catalog_kind="portable", entries=(revised,)),
        repository_root=root,
        aliases={},
        documents=(revised.document_id,),
    )
    inspection = client.propose_file_bundle(
        instance_id,
        bundle=bundle.model_dump(mode="json"),
        file=revised.name,
        proposal_name=proposal_name or f"link-{revised.name}"[:120],
        dry_run=True if dry_run else None,
        at=at,
        accept=accept,
    )
    # The proposal stands when it was admitted as a candidate: landed, or
    # waiting on an approver. A refused evaluation records nothing.
    evaluation = inspection.proposal.get("evaluation")
    standing = (
        inspection.status == "admitted"
        and isinstance(evaluation, dict)
        and evaluation.get("verdict") == "candidate"
    )
    # Compare-and-write: the lock is not held across the daemon call, so the
    # entry is re-read under it and replaced only if nobody changed it since.
    written = conflict = False
    if standing and not dry_run:
        # The daemon admitted the proposal: from here no local catalog trouble
        # (an unreadable overlay or catalog, a failed write) may lose its
        # receipt. Any of it is reported as a catalog_conflict to re-run.
        try:
            with _catalog_lock(root):
                portable = _read_portable(catalog_path)
                now = [
                    item
                    for item in portable
                    if isinstance(item, FileCatalogEntry) and item.name == entry.name
                ]
                if now == [original] and _overlay_entry(root, original.name) is None:
                    _write_portable(
                        catalog_path,
                        tuple(revised if item == original else item for item in portable),
                    )
                    written = True
                else:
                    conflict = True
        except (CoreError, OSError, ValueError):
            conflict = True
    assert revised.document_id is not None
    return GuidanceLinkOutcome(
        file=revised.name,
        document_id=revised.document_id,
        links=revised.links,
        catalog_written=written,
        catalog_conflict=conflict,
        declared_document=declared,
        proposal=inspection,
    )


__all__ = [
    "GUIDANCE_DOCUMENT_KIND",
    "GUIDANCE_SCOPE",
    "GuidanceLinkOutcome",
    "LINK_ABOUT_NEEDS_SECTION",
    "LINK_ABOUT_NEEDS_SUBJECT",
    "LINK_FILE_AMBIGUOUS",
    "LINK_FILE_OVERLAID",
    "LINK_FILE_UNREGISTERED",
    "LINK_GUIDES_NEEDS_DEFINITION",
    "LINK_GUIDES_NEEDS_PAGE",
    "LINK_ONE_LANE",
    "NOTE_VS_GUIDANCE",
    "about_link_request",
    "about_subjects",
    "guides_targets",
    "link_guides",
    "split_link_source",
]
