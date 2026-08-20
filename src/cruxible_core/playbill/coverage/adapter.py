"""The harness-facing half of coverage delivery: paths in, observations out.

§11.7 says adapters contain no semantic logic, and this module is where that
line is drawn rather than asserted. Everything here answers one of three
mechanical questions -- *which logical source is this working path?*, *what
bytes are currently there?*, and *which windows did the caller ask about?* --
and none of them can be answered by consulting accepted state. The resolver
never sees a filesystem path, and this module never sees a Claim.

Binding is declared, never inferred
-----------------------------------
A working path is bound to a `LogicalSourceIdentityV1` only by an explicit
declaration. Inferring one -- "the file is called `handbook.md` and the ledger
holds `documents/handbook.md`, so they must be the same source" -- is precisely
the mistake §11.6.1 exists to prevent: it would let identical bytes at a foreign
occurrence inherit governance by filename coincidence. An undeclared path is a
typed refusal, and two paths may never claim the same logical source, because a
working snapshot names each source at most once.

The five request forms
----------------------
§11.7 names five things one operation must resolve, and all five reduce to
`(logical source, optional byte window)` pairs before the resolver sees them:

* a file read with a line/range selection -- :func:`selection_for_lines`;
* a grep result batch -- :func:`observations_for_grep_hits`;
* a set of changed filesystem paths -- :func:`observe_working_paths`;
* an explicit source occurrence -- :func:`observe_working_source`;
* a working-set scope -- :func:`working_set_observations`.

Line numbers enter here and stop here. They are converted to byte windows
against the bytes the caller is actually looking at, and a byte window is a
presentation question about a region of a file, never part of an identity.

Bytes on the wire
-----------------
An observation carries its bytes base64-encoded because the two indexes cannot
be joined without them: an accepted commitment is a digest rather than a needle,
so finding cited content that moved requires hashing windows of the working
source, and only the side holding accepted state knows which lengths to look
for. The observation therefore re-verifies its own digest on construction --
a declared digest that does not reproduce from the declared bytes is refused
rather than believed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.canonical import Sha256Value
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    CoverageSelectionV1,
    CoverageSpanRequestV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import (
    CoverageScanBudgetV1,
    WorkingOccurrenceOverlayV1,
    WorkingSourceContent,
    build_working_occurrence_overlay,
)


class _StrictAdapterModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- declared path bindings ------------------------------------------------


class WorkingPathBindingV1(_StrictAdapterModel):
    """One declared `working path -> logical source` binding."""

    tag: Literal["playbill-coverage-path-binding-v1"] = "playbill-coverage-path-binding-v1"
    path: str
    source: LogicalSourceIdentityV1

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("a bound working path must be a non-empty trimmed value")
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("a bound working path must be relative and may not traverse upward")
        if candidate.as_posix() != value:
            raise ValueError("a bound working path must already be in POSIX form")
        return value


class WorkingPathBindingsV1(_StrictAdapterModel):
    """Every binding a harness declares for one working set.

    Both directions are unique. Two paths naming one logical source would make
    the snapshot name that source twice, and one path naming two sources would
    make the binding ambiguous; neither is a coverage answer, so both are
    refused before any bytes are read.
    """

    tag: Literal["playbill-coverage-path-bindings-v1"] = "playbill-coverage-path-bindings-v1"
    bindings: tuple[WorkingPathBindingV1, ...] = ()

    @field_validator("bindings")
    @classmethod
    def _bindings(cls, value: tuple[WorkingPathBindingV1, ...]) -> tuple[WorkingPathBindingV1, ...]:
        paths = tuple(item.path for item in value)
        if len(set(paths)) != len(paths):
            raise ValueError("a working path may declare at most one logical source")
        sources = tuple(item.source.sort_key for item in value)
        if len(set(sources)) != len(sources):
            raise ValueError("a logical source may be observed at most one working path")
        return value

    def source_for(self, path: str) -> LogicalSourceIdentityV1:
        """Return the declared logical source, refusing to invent one."""

        for item in self.bindings:
            if item.path == path:
                return item.source
        raise CoverageError(
            f"working path has no declared logical source binding: {path}. "
            "Coverage never infers a binding from a filename."
        )

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(item.path for item in self.bindings))


# -- observations ----------------------------------------------------------


class WorkingSourceObservationV1(_StrictAdapterModel):
    """One working source as a harness observed it, ready for the operation."""

    tag: Literal["playbill-coverage-working-source-observation-v1"] = (
        "playbill-coverage-working-source-observation-v1"
    )
    source: LogicalSourceIdentityV1
    content_base64: str
    content_digest: str
    byte_length: int = Field(ge=0)
    selections: tuple[CoverageSelectionV1, ...] = ()

    @field_validator("content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _observation_reproduces(self) -> "WorkingSourceObservationV1":
        content = self.content
        if len(content) != self.byte_length:
            raise ValueError("observed byte length does not match the observed content")
        if observed_commitment(content) != self.content_digest:
            raise ValueError("observed content digest does not reproduce from the observed bytes")
        windows = tuple((item.start_byte, item.end_byte) for item in self.selections)
        if windows != tuple(sorted(set(windows))):
            raise ValueError("observation selections must be sorted and unique")
        for item in self.selections:
            if item.end_byte > self.byte_length:
                raise ValueError("an observation selection may not run past the observed content")
        return self

    @property
    def content(self) -> bytes:
        try:
            return base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("observed content must be canonical base64") from exc

    @property
    def material(self) -> WorkingSourceContent:
        return WorkingSourceContent(source=self.source, content=self.content)


def observed_commitment(content: bytes) -> str:
    """Hash observed working bytes into the commitment the overlay is keyed on."""

    return Sha256Value(hashlib.sha256(content).hexdigest()).tagged


def observe_working_source(
    source: LogicalSourceIdentityV1,
    content: bytes,
    *,
    selections: Iterable[CoverageSelectionV1] = (),
) -> WorkingSourceObservationV1:
    """Observe one already-read working source; the explicit-occurrence form."""

    windows = tuple(sorted(set(selections), key=lambda item: (item.start_byte, item.end_byte)))
    return WorkingSourceObservationV1(
        source=source,
        content_base64=base64.b64encode(content).decode("ascii"),
        content_digest=observed_commitment(content),
        byte_length=len(content),
        selections=windows,
    )


def read_working_path(path: str, *, root: Path) -> bytes:
    """Read one working path under a declared root, refusing to leave it."""

    base = root.expanduser().resolve()
    target = (base / path).resolve()
    if target != base and base not in target.parents:
        raise CoverageError(f"working path escapes the declared working root: {path}")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise CoverageError(f"working path could not be read: {path}") from exc


def observe_working_path(
    path: str,
    *,
    bindings: WorkingPathBindingsV1,
    root: Path,
    selections: Iterable[CoverageSelectionV1] = (),
) -> WorkingSourceObservationV1:
    """Bind, read, and hash exactly one working path."""

    return observe_working_source(
        bindings.source_for(path),
        read_working_path(path, root=root),
        selections=selections,
    )


def observe_working_paths(
    paths: Iterable[str],
    *,
    bindings: WorkingPathBindingsV1,
    root: Path,
) -> tuple[WorkingSourceObservationV1, ...]:
    """The changed-filesystem-paths form: whole sources, no windows."""

    ordered = tuple(sorted(set(paths)))
    return tuple(observe_working_path(path, bindings=bindings, root=root) for path in ordered)


def working_set_observations(
    *,
    bindings: WorkingPathBindingsV1,
    root: Path,
) -> tuple[WorkingSourceObservationV1, ...]:
    """The working-set-scope form: every path the harness declared."""

    return observe_working_paths(bindings.paths, bindings=bindings, root=root)


# -- line and grep presentation --------------------------------------------


def _line_starts(content: bytes) -> Sequence[int]:
    starts = [0]
    offset = content.find(b"\n")
    while offset != -1:
        starts.append(offset + 1)
        offset = content.find(b"\n", offset + 1)
    return starts


def selection_for_lines(
    content: bytes,
    *,
    start_line: int,
    end_line: int,
) -> CoverageSelectionV1:
    """Convert a 1-based inclusive line range into the byte window it covers.

    Presentation in, presentation out. The window is computed against the bytes
    the caller is looking at, so it says where to look and nothing about what is
    there.
    """

    if start_line < 1 or end_line < start_line:
        raise CoverageError("a line selection must be a 1-based increasing range")
    starts = _line_starts(content)
    if start_line > len(starts):
        raise CoverageError("a line selection may not begin past the observed content")
    start_byte = starts[start_line - 1]
    end_byte = starts[end_line] if end_line < len(starts) else len(content)
    if end_byte <= start_byte:
        raise CoverageError("a line selection must cover at least one byte")
    return CoverageSelectionV1(start_byte=start_byte, end_byte=end_byte)


def observations_for_grep_hits(
    hits: Iterable[tuple[str, int]],
    *,
    bindings: WorkingPathBindingsV1,
    root: Path,
) -> tuple[WorkingSourceObservationV1, ...]:
    """The grep-result-batch form: one observation per file, windows per hit.

    A grep batch is many hits across few files, so it collapses to one
    observation per file carrying every hit line as a window. That is what makes
    the §11.6.4 "summarize ungoverned results once per operation" rule
    achievable: the batch is one operation, not one operation per line.
    """

    by_path: dict[str, list[int]] = {}
    for path, line in hits:
        if line < 1:
            raise CoverageError("a grep hit names a 1-based line number")
        by_path.setdefault(path, []).append(line)
    observations: list[WorkingSourceObservationV1] = []
    for path in sorted(by_path):
        content = read_working_path(path, root=root)
        windows = tuple(
            selection_for_lines(content, start_line=line, end_line=line)
            for line in sorted(set(by_path[path]))
        )
        observations.append(
            observe_working_source(bindings.source_for(path), content, selections=windows)
        )
    return tuple(observations)


def parse_grep_batch(text: str) -> tuple[tuple[str, int], ...]:
    """Read `path:line:...` grep output into `(path, line)` hits.

    This is the shape `grep -n` already emits, so a harness pipes its own tool
    output in without a translation layer of its own.
    """

    hits: list[tuple[str, int]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        path, separator, remainder = line.partition(":")
        number, _, _ = remainder.partition(":")
        if not separator or not path or not number.isdigit():
            raise CoverageError(f"grep batch line is not `path:line:...`: {raw}")
        hits.append((path, int(number)))
    return tuple(hits)


# -- overlay and spans -----------------------------------------------------


def coverage_span_requests(
    observations: Sequence[WorkingSourceObservationV1],
) -> tuple[CoverageSpanRequestV1, ...]:
    """Turn observations into the spans the operation resolves.

    A source observed without a window asks about the whole source; a source
    observed with windows asks one question per window, which is what keeps a
    grep batch's answer bounded to the lines the caller actually saw.
    """

    spans: list[CoverageSpanRequestV1] = []
    for observation in observations:
        if not observation.selections:
            spans.append(CoverageSpanRequestV1(source=observation.source))
            continue
        spans.extend(
            CoverageSpanRequestV1(source=observation.source, selection=selection)
            for selection in observation.selections
        )
    return tuple(spans)


def build_overlay(
    observations: Sequence[WorkingSourceObservationV1],
    *,
    wanted: Iterable[tuple[str, int]] = (),
    budget: CoverageScanBudgetV1 | None = None,
) -> WorkingOccurrenceOverlayV1:
    """Build the working-occurrence overlay from observed bytes.

    ``wanted`` comes from the evidence index and is the only reason this call
    cannot happen entirely inside a harness: the lengths worth hashing for are a
    fact about accepted state.
    """

    return build_working_occurrence_overlay(
        tuple(observation.material for observation in observations),
        wanted=wanted,
        budget=budget or CoverageScanBudgetV1(),
    )


__all__ = [
    "WorkingPathBindingV1",
    "WorkingPathBindingsV1",
    "WorkingSourceObservationV1",
    "build_overlay",
    "coverage_span_requests",
    "observations_for_grep_hits",
    "observe_working_path",
    "observe_working_paths",
    "observe_working_source",
    "observed_commitment",
    "parse_grep_batch",
    "read_working_path",
    "selection_for_lines",
    "working_set_observations",
]
