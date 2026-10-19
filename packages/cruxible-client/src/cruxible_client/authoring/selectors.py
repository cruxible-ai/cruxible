"""Deterministic local source selectors for Flow A and Flow B."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.sdk_types import SourceSelectionError
from cruxible_client.contracts.authoring.models import (
    WorkingAnchorWindow,
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.declared_blocks import stamped_projection_windows
from cruxible_client.contracts.file_catalog import (
    FileCatalog,
    FileCatalogEntry,
    FileCatalogEntryAny,
    ProcedureProjectionCatalogEntry,
    merge_file_catalogs,
    refuse_legacy_file_catalogs,
)
from cruxible_client.contracts.workspace_layout import workspace_path


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _validation_path(location: tuple[str | int, ...]) -> str:
    path = "$"
    for item in location:
        if isinstance(item, str) and item.startswith("function-after["):
            continue
        path += f"[{item}]" if isinstance(item, int) else f".{item}"
    return path


def _format_catalog_validation(error: ValidationError) -> str:
    rendered = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        rendered.append(f"{_validation_path(item['loc'])}: {item['msg']}")
    return "; ".join(rendered)


def _offsets(content: bytes, needle: bytes) -> tuple[int, ...]:
    if not needle:
        raise SourceSelectionError("an anchor must not be empty")
    found: list[int] = []
    cursor = 0
    while cursor <= len(content) - len(needle):
        offset = content.find(needle, cursor)
        if offset < 0:
            break
        found.append(offset)
        cursor = offset + 1
    return tuple(found)


def _unique_window(content: bytes, anchor: str) -> tuple[int, int]:
    encoded = anchor.encode("utf-8")
    found = _offsets(content, encoded)
    if len(found) != 1:
        raise SourceSelectionError(
            f"anchor resolved to {len(found)} occurrences at byte offsets {list(found)}"
        )
    return found[0], found[0] + len(encoded)


def source_content_for_observation(content: bytes) -> str | None:
    """The whole source, base64, when it declares a projection block; else nothing.

    A citation into a page that carries stamped blocks has to be proved outside
    every window by the daemon, not just by this client, and the daemon holds
    only the selected bytes. Sending the page lets it read the windows itself:
    the capture is the manifest. A page with no stamped block sends nothing, so
    the common citation costs what it always did.
    """

    if not stamped_projection_windows(content):
        return None
    return base64.b64encode(content).decode("ascii")


@dataclass(frozen=True)
class EvidenceSelection:
    path: Path
    source_id: str
    content: bytes
    anchor_text: str
    start_byte: int
    end_byte: int
    section_path: tuple[str, ...] | None = None

    def observation(self) -> WorkingSelectionObservation:
        selected = self.content[self.start_byte : self.end_byte]
        selector = WorkingAnchorWindow(
            anchor=self.anchor_text,
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            observed_occurrence_count=1,
        )
        if self.section_path is not None:
            from cruxible_client.contracts.authoring.models import SectionSelector

            offsets = _offsets(self.content, self.anchor_text.encode("utf-8"))
            chosen = next(
                (
                    i + 1
                    for i, offset in enumerate(offsets)
                    if self.start_byte <= offset < self.end_byte
                ),
                None,
            )
            selector = selector.model_copy(
                update={
                    "section": SectionSelector(path=self.section_path),
                    "observed_occurrence_count": len(offsets),
                    "selected_occurrence": chosen if len(offsets) > 1 else None,
                }
            )
        return WorkingSelectionObservation(
            source_id=self.source_id,
            coordinate=WorkingDigestCoordinate(
                source_content_digest=_digest(self.content),
                source_byte_length=len(self.content),
            ),
            source_content_base64=source_content_for_observation(self.content),
            selected_content_base64=base64.b64encode(selected).decode("ascii"),
            selected_bytes_digest=_digest(selected),
            selector=selector,
        )


@dataclass(frozen=True)
class FileSelector:
    path: Path
    source_id: str
    content: bytes

    def section(self, *path: str) -> EvidenceSelection:
        """Select a heading and its own body. Next: supported_by= on a Claim."""
        from cruxible_client.contracts.doc_sections import parse_sections

        document = parse_sections(self.content, file=str(self.path))
        selected = document.section(path)
        body = document.own_body(path)
        if not body:
            raise SourceSelectionError("the selected preamble is empty; cite a nonempty section")
        anchor = body.splitlines()[0].decode("utf-8").strip()
        if not anchor:
            anchor = body.decode("utf-8").strip()
        return EvidenceSelection(
            self.path,
            self.source_id,
            self.content,
            anchor,
            selected.start,
            selected.own_end,
            path,
        )

    def anchor(self, text: str) -> EvidenceSelection:
        start, end = _unique_window(self.content, text)
        return EvidenceSelection(
            path=self.path,
            source_id=self.source_id,
            content=self.content,
            anchor_text=text,
            start_byte=start,
            end_byte=end,
        )


@lru_cache(maxsize=32)
def _cached_workspace_files(workspace: Path, signature: tuple[object, ...]) -> WorkspaceFiles:
    return WorkspaceFiles(workspace)


def workspace_files(workspace: Path) -> WorkspaceFiles:
    """Parse/index a catalog once per revision; validity costs three stats, not a fold."""
    root = workspace.expanduser().resolve()
    signature: list[object] = []
    for path in (
        workspace_path(root, "files.yaml"),
        root / "files.yaml",
        workspace_path(root, "files.local.yaml"),
    ):
        try:
            stat = path.stat()
            signature.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            signature.append(None)
    return _cached_workspace_files(root, tuple(signature))


class WorkspaceFiles:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        refuse_legacy_file_catalogs(self.workspace)
        portable_paths = tuple(
            path
            for path in (
                workspace_path(self.workspace, "files.yaml"),
                self.workspace / "files.yaml",
            )
            if path.is_file()
        )
        if len(portable_paths) != 1:
            raise SourceSelectionError(
                "workspace must contain exactly one .cruxible/files.yaml or files.yaml"
            )
        try:
            portable = FileCatalog.model_validate(
                yaml.safe_load(read_regular_file(portable_paths[0]))
            )
            local_path = workspace_path(self.workspace, "files.local.yaml")
            local = (
                None
                if not local_path.is_file()
                else FileCatalog.model_validate(yaml.safe_load(read_regular_file(local_path)))
            )
            self.catalog = merge_file_catalogs(portable, local)
        except ValidationError as exc:
            raise SourceSelectionError(
                f"file catalog is invalid: {_format_catalog_validation(exc)}"
            ) from exc
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise SourceSelectionError(f"file catalog is invalid: {exc}") from exc
        self._file_entries = tuple(
            entry for entry in self.catalog.entries if isinstance(entry, FileCatalogEntry)
        )
        self._by_name = {entry.name: entry for entry in self._file_entries}
        self._by_path: dict[Path, list[FileCatalogEntry]] = {}
        for entry in self.file_entries:
            if entry.root_alias is None:
                try:
                    resolved = self._resolved_path(entry)
                except SourceSelectionError:
                    # Inventory callers report/refuse an invalid entry on its
                    # own; it must not abort unrelated catalogued sources.
                    continue
                self._by_path.setdefault(resolved, []).append(entry)

    def entries_for_path(self, path: Path) -> tuple[FileCatalogEntry, ...]:
        resolved = path.resolve()
        matches = []
        for entry in self._by_path.get(resolved, ()):
            try:
                if self._resolved_path(entry) == resolved:
                    matches.append(entry)
            except SourceSelectionError:
                continue
        return tuple(matches)

    @property
    def file_entries(self) -> tuple[FileCatalogEntry, ...]:
        return self._file_entries

    @property
    def procedure_projection_entries(self) -> tuple[ProcedureProjectionCatalogEntry, ...]:
        return tuple(
            entry
            for entry in self.catalog.entries
            if isinstance(entry, ProcedureProjectionCatalogEntry)
        )

    def _resolved_path(self, entry: FileCatalogEntryAny) -> Path:
        label = (
            entry.name
            if isinstance(entry, FileCatalogEntry)
            else entry.procedure_identity.qualified
        )
        if entry.root_alias is not None:
            raise SourceSelectionError(
                f"catalog entry {label!r} uses root_alias; S1 requires a merged absolute overlay"
            )
        locator = Path(entry.locator)
        path = locator if locator.is_absolute() else self.workspace / locator
        resolved = path.expanduser().resolve()
        if not locator.is_absolute() and not resolved.is_relative_to(self.workspace):
            raise SourceSelectionError(f"catalog entry {label!r} escapes the workspace")
        return resolved

    def select(self, requested: str | Path) -> FileSelector:
        path = Path(requested)
        resolved = (path if path.is_absolute() else self.workspace / path).expanduser().resolve()
        matches = self.entries_for_path(resolved)
        if len(matches) != 1:
            raise SourceSelectionError(
                f"path {requested!s} maps to {len(matches)} catalogued files; exactly one required"
            )
        try:
            content = read_regular_file(resolved)
        except OSError as exc:
            raise SourceSelectionError(f"could not read {requested!s}: {exc}") from exc
        return FileSelector(path=resolved, source_id=matches[0].name, content=content)

    def entry_for_file(self, file: str) -> FileCatalogEntry:
        """The one catalog entry named ``file``, or a refusal naming the known ones."""

        matches = () if file not in self._by_name else (self._by_name[file],)
        if len(matches) != 1:
            known = ", ".join(entry.name for entry in self.file_entries) or "none"
            raise SourceSelectionError(
                f"catalogued file {file!r} maps to {len(matches)} local files "
                f"(catalogued files: {known})"
            )
        return matches[0]

    def path_for_file(self, file: str) -> Path:
        return self._resolved_path(self.entry_for_file(file))

    def coverage_bindings(self) -> dict[str, str]:
        """Each catalogued workspace file, relative to the workspace, to its source.

        The catalog is the one path-to-source mapping, so coverage reads it as its
        bindings (``external:<name>``, the identity file evidence is cited under).
        Entries outside the workspace or with a root alias are not workspace
        files and bind nothing.
        """

        bindings: dict[str, str] = {}
        for entry in self.file_entries:
            if entry.root_alias is not None or Path(entry.locator).is_absolute():
                continue
            path = self._resolved_path(entry)
            bindings[path.relative_to(self.workspace).as_posix()] = f"external:{entry.name}"
        return bindings

    def path_for_procedure(self, procedure_identity: str) -> Path:
        matches = tuple(
            entry
            for entry in self.procedure_projection_entries
            if entry.procedure_identity.qualified == procedure_identity
        )
        if len(matches) != 1:
            raise SourceSelectionError(
                f"Procedure {procedure_identity!r} maps to {len(matches)} local projections"
            )
        return self._resolved_path(matches[0])


__all__ = [
    "EvidenceSelection",
    "FileSelector",
    "WorkspaceFiles",
]
