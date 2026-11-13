"""One writer-side observe/submit/CAS seam shared by CLI, MCP and SDK.

Work is bounded by the supplied files/operations and their section bytes. Catalog
parsing/indexing is cached by revision; nothing inventories instance Claims.
"""

from __future__ import annotations

import base64
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.files import FileCatalogEditError, add_catalog_entry
from cruxible_client.authoring.page_writes import PageWriteError, replace_page_file
from cruxible_client.authoring.selectors import (
    FileSelector,
    source_content_for_observation,
    workspace_files,
)
from cruxible_client.contracts.authoring.models import WorkingDigestCoordinate
from cruxible_client.contracts.declared_blocks import (
    declares_projection_block,
    stamped_projection_windows,
)
from cruxible_client.contracts.doc_sections import (
    DocSectionError,
    SectionEdit,
    append_section,
    parse_sections,
    replace_section,
    section_digest,
)
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.file_catalog import FileCatalogEntry
from cruxible_client.contracts.workspace_layout import workspace_path
from cruxible_client.contracts.write import (
    AboutChange,
    AddChange,
    AppendSection,
    Change,
    DocEditObservation,
    DocOp,
    DocOutcome,
    FileEvidence,
    ReplaceSection,
    SectionEvidence,
    SetChange,
    WriteOutcome,
    WriteRefusal,
    WriteRequest,
)


def _path(workspace: Path, file: str) -> Path:
    candidate = Path(file).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace) or any(
        part.casefold() in {".cruxible", ".git"} for part in (*candidate.parts, *resolved.parts)
    ):
        raise DocSectionError(
            "protected_path", "Choose a workspace Markdown path outside .cruxible and .git."
        )
    # The reader below opens the agent's path with O_NOFOLLOW, including when
    # the real name check above followed a final symlink.
    if candidate.is_symlink():
        raise DocSectionError("protected_path", "Choose a regular Markdown file, not a symlink.")
    return resolved


def _coordinate(content: bytes) -> WorkingDigestCoordinate:
    return WorkingDigestCoordinate(
        source_content_digest=section_digest(content), source_byte_length=len(content)
    )


def _read(path: Path) -> bytes | None:
    try:
        return read_regular_file(path)
    except FileNotFoundError:
        return None


def _apply(content: bytes | None, op: DocOp) -> tuple[SectionEdit, int, int]:
    document = parse_sections(content or b"", file=op.file)
    if isinstance(op, AppendSection):
        parent = document.section(op.under)
        start = end = parent.end if op.under else len(content or b"")
        result = append_section(content, under=op.under, text=op.text, file=op.file)
    else:
        if content is None:
            raise DocSectionError("section_not_found", "Replace requires an existing file.")
        result = replace_section(
            content, section=op.section, text=op.text, expect=op.expect, file=op.file
        )
        target = document.section(op.section if result.changed else result.section)
        start, end = target.start, target.end
    overlapping = [
        w
        for w in stamped_projection_windows(content or b"")
        if (start < w.end_byte and end > w.start_byte)
        or (start == end and w.start_byte <= start < w.end_byte)
    ]
    if declares_projection_block(op.text.encode()) or overlapping:
        raise DocSectionError(
            "projection_edit",
            "A doc write edits prose only; a projection block changes through its backing",
            repair=(
                "cruxible block repin "
                f"{shlex.quote(op.file)} {shlex.quote(overlapping[0].block_id)}"
                if overlapping
                else "write the section without block markers; declare blocks with cruxible block"
            ),
        )
    return result, start, end


@dataclass
class _File:
    path: Path
    source_id: str
    original: bytes | None
    current: bytes | None
    entry: FileCatalogEntry | None
    ops: list[DocOp] = field(default_factory=list)
    edits: list[SectionEdit] = field(default_factory=list)
    record_indices: list[int] = field(default_factory=list)
    citations: dict[tuple[str, ...], bytes] = field(default_factory=dict)


def _observe_about(
    change: AboutChange,
    *,
    sections: dict[str, tuple[str, tuple[str, ...]]],
    load: Callable[[str], _File],
) -> AboutChange:
    """Observe an about link's section exactly as section evidence observes it.

    A pure unlink of a section that is gone (or a file no longer on disk)
    goes without an observation: the note is named by its source and heading
    path, and unlinking needs no bytes.
    """

    file, section = change.file, change.section
    if change.ref is not None:
        target = sections.get(change.ref)
        if target is None:
            raise DocSectionError("section_ref_unknown", f"No doc operation has id {change.ref!r}.")
        file, section = target
    state = load(file)
    observation = None
    try:
        if state.current is None:
            raise DocSectionError(
                "file_missing", "Read or create the Markdown file before linking it."
            )
        selection = FileSelector(state.path, state.source_id, state.current).section(*section)
        observation = selection.observation()
        state.citations[section] = observation.selected_content
    except ValueError:
        # DocSectionError and the selector's refusals are both ValueErrors.
        if change.subjects:
            raise
    return AboutChange(
        file=state.source_id,
        section=section,
        subjects=change.subjects,
        unlink=change.unlink,
        observation=observation,
    )


def write_workspace(
    request: WriteRequest,
    *,
    workspace: Path,
    submit: Callable[[WriteRequest], WriteOutcome],
) -> WriteOutcome:
    """Observe Q, submit without paths, then write Q only after admission.

    A file that moved is rebased once when all target sections and cited own
    bodies still agree. Conflicts after admission report written=false; accepted
    Claims retain their basis and the outcome names the repair.
    """
    if not request.docs and not any(
        isinstance(c, AboutChange)
        or isinstance(getattr(c, "evidence", None), FileEvidence | SectionEvidence)
        for c in request.changes
    ):
        return submit(request)
    workspace = workspace.expanduser().resolve()
    files: dict[Path, _File] = {}
    records: list[tuple[str, _File, DocEditObservation]] = []
    catalog = None
    if any(
        p.is_file() for p in (workspace_path(workspace, "files.yaml"), workspace / "files.yaml")
    ):
        catalog = workspace_files(workspace)

    def load(file: str, *, name: str | None = None, editing: bool = False) -> _File:
        path = _path(workspace, file)
        if path in files:
            return files[path]
        matches = () if catalog is None else catalog.entries_for_path(path)
        if len(matches) > 1:
            raise DocSectionError(
                "catalog_ambiguous", "The file has multiple catalog names; choose one binding."
            )
        entry = matches[0] if matches else None
        if entry is None and not editing:
            raise DocSectionError(
                "file_uncatalogued", "Catalogue the file before citing its existing sections."
            )
        source_id = (
            entry.name
            if entry
            else name
            or "doc-"
            + re.sub(r"[^a-z0-9_.-]+", "-", path.relative_to(workspace).as_posix().lower()).strip(
                "-."
            )[:120]
        )
        if entry is None and catalog is not None:
            if source_id in catalog._by_name:
                raise DocSectionError(
                    "catalog_name_exists", "Choose a distinct name for the new file."
                )
        if any(f.source_id == source_id and f.path != path for f in files.values()):
            raise DocSectionError(
                "catalog_name_exists", "Choose distinct catalog names for the new files."
            )
        content = _read(path)
        state = _File(path, source_id, content, content, entry)
        files[path] = state
        return state

    try:
        observations: list[DocEditObservation] = []
        sections: dict[str, tuple[str, tuple[str, ...]]] = {}
        for op in request.docs:
            if isinstance(op, DocEditObservation):
                raise DocSectionError(
                    "already_observed",
                    "Supply local doc operations to a workspace adapter, not wire observations.",
                )
            state = load(
                op.file, name=op.name if isinstance(op, AppendSection) else None, editing=True
            )
            before = state.current
            result, start, end = _apply(before, op)
            if op.id is not None:
                if op.id in sections:
                    raise DocSectionError("section_ref_ambiguous", f"Duplicate doc id {op.id!r}.")
                sections[op.id] = (op.file, result.section)
            observation = DocEditObservation(
                source_id=state.source_id,
                op=op.op,
                section=result.section,
                before_digest=result.before_digest,
                before_content_base64=(
                    base64.b64encode((before or b"")[start:end]).decode()
                    if isinstance(op, ReplaceSection)
                    else None
                ),
                before_section=(op.section if result.changed else result.section)
                if isinstance(op, ReplaceSection)
                else None,
                after_digest=result.after_digest,
                content_base64=base64.b64encode(
                    parse_sections(result.content, file=op.file).subtree(result.section)
                ).decode(),
                before_coordinate=None if before is None else _coordinate(before),
                after_coordinate=_coordinate(result.content),
                start_byte=start,
                end_byte=end,
                source_content_base64=source_content_for_observation(before or b""),
            )
            state.current = result.content
            state.ops.append(op)
            state.edits.append(result)
            observations.append(observation)
            state.record_indices.append(len(records))
            records.append((op.file, state, observation))
        changes: list[Change] = []
        for change in request.changes:
            if isinstance(change, AboutChange):
                changes.append(_observe_about(change, sections=sections, load=load))
                continue
            evidence = getattr(change, "evidence", None)
            if isinstance(evidence, SectionEvidence) and evidence.ref is not None:
                target = sections.get(evidence.ref)
                if target is None:
                    raise DocSectionError(
                        "section_ref_unknown", f"No doc operation has id {evidence.ref!r}."
                    )
                evidence = SectionEvidence(
                    file=target[0], section=target[1], own_words=evidence.own_words
                )
            if isinstance(change, SetChange | AddChange) and isinstance(
                evidence, SectionEvidence | FileEvidence
            ):
                state = load(
                    evidence.file if isinstance(evidence, SectionEvidence) else evidence.path
                )
                if state.current is None:
                    raise DocSectionError(
                        "file_missing", "Read or create the Markdown file before citing it."
                    )
                selector = FileSelector(state.path, state.source_id, state.current)
                if isinstance(evidence, SectionEvidence):
                    selection = selector.section(*evidence.section)
                    authored = any(
                        (not edit.section and not evidence.section)
                        or bool(
                            edit.section and evidence.section[: len(edit.section)] == edit.section
                        )
                        for edit in state.edits
                    )
                    state.citations[evidence.section] = selection.observation().selected_content
                    evidence = evidence.model_copy(
                        update={
                            "observation": selection.observation(),
                            "own_words": authored
                            if evidence.own_words is None
                            else evidence.own_words,
                            "file": state.source_id,
                        }
                    )
                else:
                    evidence = evidence.model_copy(
                        update={"observation": selector.anchor(evidence.anchor).observation()}
                    )
                change = change.model_copy(update={"evidence": evidence})
            changes.append(change)
    except (ValueError, OSError) as exc:
        # Each code carries its own repair; a refusal whose message already says
        # what to do (a protected path, an invalid section) carries none.
        raise WriteRefusalError(
            exc.code if isinstance(exc, DocSectionError) else "cruxible.doc.file_unavailable",
            str(exc).rstrip("."),
            repair_line=exc.repair if isinstance(exc, DocSectionError) else None,
        ) from exc

    observed = request.model_copy(
        update={
            "changes": tuple(changes),
            **({"docs": tuple(observations)} if request.docs else {}),
        }
    )
    outcome = submit(observed)
    if not records:
        return outcome
    outcomes = [
        DocOutcome(
            file=file,
            source_id=state.source_id,
            section=obs.section,
            before_digest=obs.before_digest,
            after_digest=obs.after_digest,
            confirmation=outcome.docs[index].confirmation if index < len(outcome.docs) else None,
        )
        for index, (file, state, obs) in enumerate(records)
    ]
    if request.dry_run or outcome.status not in ("accepted", "awaiting_approval"):
        return outcome.model_copy(update={"docs": tuple(outcomes)})
    confirmations: list[str] = []
    for state in files.values():
        if not state.ops:
            continue
        failure = None
        followup = None
        try:
            applied = [True] * len(state.ops)
            _path(workspace, str(state.path))
            state.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                replace_page_file(
                    state.path, expected=state.original, replacement=state.current or b""
                )
            except PageWriteError:
                rebase_original = moved = _read(state.path)
                for index, (op, previous) in enumerate(zip(state.ops, state.edits, strict=True)):
                    rebased, _, _ = _apply(moved, op)
                    applied[index] = rebased.changed
                    if (
                        isinstance(op, ReplaceSection)
                        and rebased.changed
                        and rebased.before_digest != previous.before_digest
                    ):
                        raise DocSectionError(
                            "section_changed",
                            "The target section changed while the write was admitted.",
                        )
                    moved = rebased.content
                assert moved is not None
                for section, selected in state.citations.items():
                    if parse_sections(moved, file=str(state.path)).own_body(section) != selected:
                        raise DocSectionError(
                            "section_changed", "Rebase would change cited section bytes."
                        )
                replace_page_file(state.path, expected=rebase_original, replacement=moved)
            confirmations.extend(
                token
                for index, changed in zip(state.record_indices, applied, strict=True)
                if changed and (token := outcomes[index].confirmation) is not None
            )
            if state.entry is None:
                # Refresh by catalog revision after file creation. Retry after a
                # crash between these steps adds the single missing binding.
                existing = None
                if any(
                    p.is_file()
                    for p in (workspace_path(workspace, "files.yaml"), workspace / "files.yaml")
                ):
                    existing = workspace_files(workspace).entries_for_path(state.path)
                if not existing:
                    add_catalog_entry(workspace, name=state.source_id, path=state.path, fields={})
            elif state.entry.is_document:
                followup = (
                    f"cruxible files propose --file {shlex.quote(state.source_id)} "
                    f"--name doc-revision --root {shlex.quote(str(workspace))}"
                )
        except (OSError, ValueError, FileCatalogEditError) as exc:
            failure = WriteRefusal(
                code="cruxible.doc.doc_write_conflict",
                message=str(exc),
                repair=(
                    "Re-read this section, revise or re-cite the retained Claim, "
                    "and retry with its current expect digest"
                ),
            )
        for index in state.record_indices:
            outcomes[index] = outcomes[index].model_copy(
                update={"written": failure is None, "refusal": failure, "next": followup}
            )
    if confirmations:
        confirmed = submit(
            WriteRequest(
                because=request.because,
                doc_confirmations=tuple(confirmations),
                surface=request.surface,
                full_coordinate=request.full_coordinate,
            )
        )
        if confirmed.status != "accepted":
            raise WriteRefusalError(
                "cruxible.doc.history_unconfirmed",
                "The file was written, but some history confirmations were refused",
                repair_line=(
                    confirmed.refusal.repair
                    if confirmed.refusal is not None
                    else (
                        "Inspect each doc_confirmations result, then resend only the "
                        "tokens still unconfirmed: " + ", ".join(confirmations)
                    )
                ),
                outcome=confirmed,
            )
    next_steps = [
        item.refusal.repair if item.refusal else item.next
        for item in outcomes
        if item.refusal or item.next
    ]
    return outcome.model_copy(
        update={
            "docs": tuple(outcomes),
            **(
                {"next": "; ".join(filter(None, [outcome.next, *next_steps]))} if next_steps else {}
            ),
        }
    )
