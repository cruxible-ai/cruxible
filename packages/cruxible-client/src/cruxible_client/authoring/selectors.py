"""Deterministic local source selectors for Flow A and Flow B."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

from cruxible_client.authoring.sdk_types import InsertionOperation, SourceSelectionError
from cruxible_client.contracts.authoring.models import (
    InsertionAnchorWindowV1,
    InsertionTargetV2,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.source_catalog import (
    SourceCatalog,
    SourceCatalogEntry,
    merge_source_catalogs,
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


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


def _line_window(
    content: bytes,
    *,
    start: int,
    end: int,
    surrounding_lines: int,
) -> tuple[int, int]:
    if isinstance(surrounding_lines, bool) or surrounding_lines < 0:
        raise SourceSelectionError("surrounding_lines must be a nonnegative integer")
    window_start = content.rfind(b"\n", 0, start) + 1
    closing = content.find(b"\n", end - 1)
    window_end = len(content) if closing < 0 else closing + 1
    for _ in range(surrounding_lines):
        if window_start == 0:
            break
        window_start = content.rfind(b"\n", 0, window_start - 1) + 1
    for _ in range(surrounding_lines):
        if window_end >= len(content):
            break
        closing = content.find(b"\n", window_end)
        window_end = len(content) if closing < 0 else closing + 1
    return window_start, window_end


@dataclass(frozen=True)
class EvidenceSelection:
    path: Path
    source_id: str
    content: bytes
    anchor_text: str
    start_byte: int
    end_byte: int

    def observation(self) -> WorkingSelectionObservationV1:
        selected = self.content[self.start_byte : self.end_byte]
        return WorkingSelectionObservationV1(
            source_id=self.source_id,
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest=_digest(self.content),
                source_byte_length=len(self.content),
            ),
            selected_content_base64=base64.b64encode(selected).decode("ascii"),
            selected_bytes_digest=_digest(selected),
            selector=WorkingAnchorWindowV1(
                anchor=self.anchor_text,
                start_byte=self.start_byte,
                end_byte=self.end_byte,
                observed_occurrence_count=1,
            ),
        )


@dataclass(frozen=True)
class InsertionSelection:
    path: Path
    source_id: str
    content: bytes
    operation: InsertionOperation
    anchor_text: str
    start_byte: int
    end_byte: int

    def target(self, inserted: bytes) -> InsertionTargetV2:
        """Freeze only the initial source and selector; publication frames after acceptance."""

        del inserted
        if self.operation is InsertionOperation.BEFORE:
            offset = self.start_byte
        elif self.operation is InsertionOperation.AFTER:
            offset = self.end_byte
        elif self.operation is InsertionOperation.REPLACE:
            offset = self.start_byte
        else:
            offset = len(self.content)
        anchor = self.content[self.start_byte : self.end_byte]
        return InsertionTargetV2(
            source_id=self.source_id,
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest=_digest(self.content),
                source_byte_length=len(self.content),
            ),
            initial_preimage_digest=_digest(self.content),
            initial_preimage_byte_length=len(self.content),
            selector=InsertionAnchorWindowV1(
                anchor_content_base64=base64.b64encode(anchor).decode("ascii"),
                anchor_bytes_digest=_digest(anchor),
                start_byte=self.start_byte,
                end_byte=self.end_byte,
                insertion_offset=offset,
                observed_occurrence_count=1,
            ),
            operation=self.operation.value,
        )


@dataclass(frozen=True)
class FileSelector:
    path: Path
    source_id: str
    content: bytes

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

    def anchor_window(self, *, text: str, surrounding_lines: int) -> EvidenceSelection:
        start, end = _unique_window(self.content, text)
        start, end = _line_window(
            self.content,
            start=start,
            end=end,
            surrounding_lines=surrounding_lines,
        )
        return EvidenceSelection(
            path=self.path,
            source_id=self.source_id,
            content=self.content,
            anchor_text=text,
            start_byte=start,
            end_byte=end,
        )

    def insertion(
        self,
        *,
        operation: InsertionOperation,
        anchor: str,
    ) -> InsertionSelection:
        if operation is InsertionOperation.APPEND:
            raise SourceSelectionError("use append() for an append selection")
        start, end = _unique_window(self.content, anchor)
        return InsertionSelection(
            path=self.path,
            source_id=self.source_id,
            content=self.content,
            operation=operation,
            anchor_text=anchor,
            start_byte=start,
            end_byte=end,
        )

    def append(self) -> InsertionSelection:
        end = len(self.content)
        start = max(0, end - 4096)
        anchor = self.content[start:end]
        if anchor and self.content.count(anchor) != 1:
            raise SourceSelectionError("append tail anchor is ambiguous within the source")
        return InsertionSelection(
            path=self.path,
            source_id=self.source_id,
            content=self.content,
            operation=InsertionOperation.APPEND,
            anchor_text=anchor.decode("utf-8", errors="replace"),
            start_byte=start,
            end_byte=end,
        )


class WorkspaceSources:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        portable_paths = tuple(
            path
            for path in (
                self.workspace / ".playbill" / "sources.yaml",
                self.workspace / "sources.yaml",
            )
            if path.is_file()
        )
        if len(portable_paths) != 1:
            raise SourceSelectionError(
                "workspace must contain exactly one .playbill/sources.yaml or sources.yaml"
            )
        try:
            portable = SourceCatalog.model_validate(yaml.safe_load(portable_paths[0].read_bytes()))
            local_path = self.workspace / ".playbill" / "sources.local.yaml"
            local = (
                None
                if not local_path.is_file()
                else SourceCatalog.model_validate(yaml.safe_load(local_path.read_bytes()))
            )
            self.catalog = merge_source_catalogs(portable, local)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise SourceSelectionError(f"source catalog is invalid: {exc}") from exc

    def _resolved_path(self, entry: SourceCatalogEntry) -> Path:
        if entry.root_alias is not None:
            raise SourceSelectionError(
                f"source {entry.name!r} uses root_alias; S1 requires a merged absolute overlay"
            )
        locator = Path(entry.locator)
        path = locator if locator.is_absolute() else self.workspace / locator
        resolved = path.expanduser().resolve()
        if not locator.is_absolute() and not resolved.is_relative_to(self.workspace):
            raise SourceSelectionError(f"source {entry.name!r} escapes the workspace")
        return resolved

    def select(self, requested: str | Path) -> FileSelector:
        path = Path(requested)
        resolved = (path if path.is_absolute() else self.workspace / path).expanduser().resolve()
        matches = tuple(
            entry for entry in self.catalog.entries if self._resolved_path(entry) == resolved
        )
        if len(matches) != 1:
            raise SourceSelectionError(
                f"path {requested!s} maps to {len(matches)} logical sources; exactly one required"
            )
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            raise SourceSelectionError(f"could not read {requested!s}: {exc}") from exc
        return FileSelector(path=resolved, source_id=matches[0].name, content=content)

    def path_for_source(self, source_id: str) -> Path:
        matches = tuple(entry for entry in self.catalog.entries if entry.name == source_id)
        if len(matches) != 1:
            raise SourceSelectionError(
                f"logical source {source_id!r} maps to {len(matches)} local files"
            )
        return self._resolved_path(matches[0])


__all__ = [
    "EvidenceSelection",
    "FileSelector",
    "InsertionSelection",
    "WorkspaceSources",
]
