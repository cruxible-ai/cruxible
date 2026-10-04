"""The floor index: the floor at one accepted generation, advanced from the ledger diff.

Every floor file is stamped with ``changed_at``, the latest accepted generation
that touched any of its inputs. For a Subject's ``current/`` file those are
the Subject itself, its Claims of any lifecycle, every Claim that ever pointed
at it (so a moved edge restamps its old target and its new one), and the
ClaimTypes (and the short-name shadows) of the fields it shows. The stamp is read from the
history index, so a full render and an incremental one give the same bytes,
and a file's bytes differ between two generations exactly when its stamp does.

The index is rebuildable and instance-internal. It holds the parsed inputs
(Subjects, Claims, ClaimTypes, the latest sequence of every ledger path) and
the rendered files. ``advance_floor_index`` moves it forward by reading only
the paths the change records touched since; a render at any other generation
is the same patch applied to a copy, in either direction, so any generation of
the accepted history can be rendered from it without binding that
generation's projection.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from cruxible_client.contracts.captures import (
    CaptureContract,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import ClaimArtifactAny, SubjectClaimObject, parse_claim
from cruxible_client.contracts.errors import CruxibleError, ProjectionIntegrityError
from cruxible_client.contracts.floor import (
    FloorManifest,
    build_floor_manifest,
    content_digest,
    floor_notes_digest,
)
from cruxible_client.contracts.subjects import SubjectShell, parse_subject
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation, HistoryReader
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.floor.floor_content import (
    FLOOR_README,
    ChangeNote,
    change_notes,
    change_path,
    notes_changed_between,
    render_changes,
    review_snapshot_oid,
)
from cruxible_core.service.floor.floor_current import (
    IncomingEdge,
    SubjectPart,
    ValueRenderer,
    index_path,
    render_index,
    render_subject,
    stamped,
    subject_ref,
)
from cruxible_core.service.floor.floor_sources import (
    DOCUMENTS_PREFIX,
    SOURCES_LEDGER_PATH,
    render_sources_ledger,
)
from cruxible_core.service.floor.renderer import floor_renderer

SUBJECTS_PREFIX = "subjects/"
CLAIMS_PREFIX = "claims/"
CLAIM_TYPES_PREFIX = "claim-types/"
CAPTURE_CONTRACTS_PREFIX = "capture-contracts/"
README_PATH = "README.md"
_INPUT_PREFIXES = (
    SUBJECTS_PREFIX,
    CLAIMS_PREFIX,
    CLAIM_TYPES_PREFIX,
    DOCUMENTS_PREFIX,
    CAPTURE_CONTRACTS_PREFIX,
)
# What sources/LEDGER reads: every Claim's backing, the Documents and the contracts.
_SOURCE_PREFIXES = (CLAIMS_PREFIX, DOCUMENTS_PREFIX, CAPTURE_CONTRACTS_PREFIX)
_INDEX_KEY = ("floor-index",)


@dataclass(frozen=True)
class FloorInputs:
    """Everything the floor reads at one accepted generation, from the ledger alone."""

    generation: int
    coordinate: AcceptedCoordinate
    # The latest accepted sequence that touched each ledger path, at this generation.
    latest: Mapping[str, int]
    subjects: Mapping[str, SubjectShell]
    # Every Claim in the tree, any lifecycle, by Claim path.
    claims: Mapping[str, ClaimArtifactAny]
    # Every ClaimType in the tree, any lifecycle, by ClaimType path.
    claim_types: Mapping[str, ClaimType]
    # Subject path -> the paths of its Claims, any lifecycle.
    by_subject: Mapping[str, frozenset[str]]
    # Subject-valued Claim path -> (sequence, object Subject path) of each of
    # its revisions to this generation: every Subject it ever pointed at.
    objects: Mapping[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    # Accepted Document paths, and CaptureContracts by path.
    documents: frozenset[str] = frozenset()
    capture_contracts: Mapping[str, CaptureContract] = field(default_factory=dict)

    def pointed_at(self) -> dict[str, frozenset[str]]:
        """Subject path -> every Claim that ever pointed at it, any lifecycle."""

        found: dict[str, set[str]] = {}
        for claim_file, revisions in self.objects.items():
            for _sequence, target in revisions:
                found.setdefault(target, set()).add(claim_file)
        return {target: frozenset(claims) for target, claims in found.items()}

    def predicates(self) -> tuple[dict[str, ClaimType], frozenset[str]]:
        types = {item.predicate: item for item in self.claim_types.values()}
        live = frozenset(
            predicate for predicate, item in types.items() if item.lifecycle.state == "live"
        )
        return types, live


@dataclass(frozen=True)
class _SubjectRender:
    changed_at: int
    # Excluding the edges pointing in: what the kind's INDEX row reads.
    own_changed_at: int
    part: SubjectPart
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class FloorRender:
    """The floor at one generation: every file with its bytes and ``changed_at``."""

    inputs: FloorInputs
    renderer: str
    subjects: Mapping[str, _SubjectRender]
    indexes: Mapping[str, tuple[int, bytes]]
    changes: Mapping[int, bytes]
    files: Mapping[str, tuple[bytes, int]] = field(default_factory=dict)
    sources: tuple[int, bytes] | None = None
    # The one immutable review-notes commit every change file was read from
    # (None: no notes), what each change's notes said, and their identity.
    notes: str | None = None
    change_notes: Mapping[int, ChangeNote] = field(default_factory=dict)
    notes_digest: str = ""

    @property
    def generation(self) -> int:
        return self.inputs.generation

    def manifest(self) -> FloorManifest:
        return build_floor_manifest(
            renderer=self.renderer,
            coordinate=self.inputs.coordinate,
            generation=self.inputs.generation,
            notes_digest=self.notes_digest,
            files={
                path: (content_digest(content), len(content), changed)
                for path, (content, changed) in self.files.items()
            },
        )


# -- reading the inputs -----------------------------------------------------------


def _parse_into(
    path: str,
    content: bytes,
    *,
    subjects: dict[str, SubjectShell],
    claims: dict[str, ClaimArtifactAny],
    claim_types: dict[str, ClaimType],
    documents: set[str],
    contracts: dict[str, CaptureContract],
) -> None:
    if path.startswith(SUBJECTS_PREFIX):
        subjects[path] = parse_subject(content, path=path)
    elif path.startswith(CLAIMS_PREFIX):
        claims[path] = parse_claim(content, path=path)
    elif path.startswith(CLAIM_TYPES_PREFIX):
        claim_types[path] = parse_claim_type(content, path=path)
    elif path.startswith(DOCUMENTS_PREFIX):
        documents.add(path)
    elif path.startswith(CAPTURE_CONTRACTS_PREFIX):
        contracts[path] = parse_capture_contract(content, path=path)


def _group_by_subject(claims: Mapping[str, ClaimArtifactAny]) -> dict[str, frozenset[str]]:
    grouped: dict[str, set[str]] = {}
    for path, claim in claims.items():
        grouped.setdefault(claim.statement.subject.artifact_path, set()).add(path)
    return {subject: frozenset(paths) for subject, paths in grouped.items()}


def _object(claim: ClaimArtifactAny | None) -> str | None:
    obj = None if claim is None else claim.statement.object
    return obj.address.artifact_path if isinstance(obj, SubjectClaimObject) else None


def _object_histories(
    instance: PlaybillInstance,
    history: HistoryReader,
    claims: Mapping[str, ClaimArtifactAny],
    paths: Iterable[str],
    *,
    at_most: int,
) -> dict[str, tuple[tuple[int, str], ...]]:
    """Every Subject each Subject-valued Claim pointed at, revision by revision.

    A Claim touched once is its current object. One revised in place is read
    back at each generation that touched it; a predicate keeps its object kind,
    so only Subject-valued Claims are read.
    """

    wanted = [path for path in paths if _object(claims.get(path)) is not None]
    sequences = {
        path: tuple(item for item in found if item <= at_most)
        for path, found in history.member_sequences(wanted).items()
    }
    histories: dict[str, list[tuple[int, str]]] = {}
    revisited: dict[int, list[str]] = {}
    for path in wanted:
        touched = sequences.get(path, ())
        current = _object(claims[path])
        assert current is not None
        if len(touched) <= 1:
            histories[path] = [(touched[0] if touched else 0, current)]
            continue
        histories[path] = []
        for sequence in touched:
            revisited.setdefault(sequence, []).append(path)
    for sequence in sorted(revisited):
        oid = history.generation(sequence).git_oid
        blobs = instance.blobs_at(oid, revisited[sequence])
        for path in revisited[sequence]:
            content = blobs.get(path)
            target = None if content is None else _object(parse_claim(content, path=path))
            if target is not None:
                histories[path].append((sequence, target))
    return {
        path: tuple(sorted(set(revisions))) for path, revisions in histories.items() if revisions
    }


def build_floor_inputs(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    history: HistoryReader,
    location: AcceptedGenerationLocation,
) -> FloorInputs:
    """Read the floor's inputs whole, from the accepted projection at ``coordinate``."""

    with instance.bind_accepted_projection(coordinate) as projection:
        paths = tuple(
            row.path
            for kind in ("subject", "claim", "claim-type", "capture-contract")
            for row in projection.typed.envelopes(kind=kind)
        )
        documents = {row.path for row in projection.typed.envelopes(kind="document")}
        projection.typed.prefetch_members(paths)
        raw = {path: projection.typed.member_bytes(path) for path in paths}
    subjects: dict[str, SubjectShell] = {}
    claims: dict[str, ClaimArtifactAny] = {}
    claim_types: dict[str, ClaimType] = {}
    contracts: dict[str, CaptureContract] = {}
    for path, content in raw.items():
        if content is not None:
            _parse_into(
                path,
                content,
                subjects=subjects,
                claims=claims,
                claim_types=claim_types,
                documents=documents,
                contracts=contracts,
            )
    return FloorInputs(
        generation=location.sequence,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        latest=history.latest_sequences(at_most=location.sequence),
        subjects=subjects,
        claims=claims,
        claim_types=claim_types,
        by_subject=_group_by_subject(claims),
        objects=_object_histories(instance, history, claims, claims, at_most=location.sequence),
        documents=frozenset(documents),
        capture_contracts=contracts,
    )


def patch_floor_inputs(
    instance: PlaybillInstance,
    history: HistoryReader,
    inputs: FloorInputs,
    target: AcceptedGenerationLocation,
) -> FloorInputs:
    """The inputs at ``target``: ``inputs`` with every path touched in between re-read.

    Works in either direction. The touched paths are the history index's
    member paths between the two generations; each is read at ``target`` (or
    is absent there) and its latest sequence is taken at ``target``.
    """

    if target.sequence == inputs.generation:
        return inputs
    low, high = sorted((inputs.generation, target.sequence))
    changed = sorted(history.member_paths_between(low, high), key=lambda item: item.encode())
    latest = dict(inputs.latest)
    at_target = history.latest_sequences(changed, at_most=target.sequence)
    for path in changed:
        if path in at_target:
            latest[path] = at_target[path]
        else:
            latest.pop(path, None)
    wanted = [path for path in changed if path.startswith(_INPUT_PREFIXES)]
    blobs = instance.blobs_at(target.git_oid, wanted) if wanted else {}
    subjects = dict(inputs.subjects)
    claims = dict(inputs.claims)
    claim_types = dict(inputs.claim_types)
    documents = set(inputs.documents)
    contracts = dict(inputs.capture_contracts)
    by_subject = dict(inputs.by_subject)
    for path in wanted:
        old = claims.get(path)
        for held in (subjects, claims, claim_types, contracts):
            held.pop(path, None)
        documents.discard(path)
        content = blobs.get(path)
        if content is not None:
            _parse_into(
                path,
                content,
                subjects=subjects,
                claims=claims,
                claim_types=claim_types,
                documents=documents,
                contracts=contracts,
            )
        new = claims.get(path)
        for subject in {
            item.statement.subject.artifact_path for item in (old, new) if item is not None
        }:
            members = set(by_subject.get(subject, ()))
            members.discard(path)
            if new is not None and new.statement.subject.artifact_path == subject:
                members.add(path)
            if members:
                by_subject[subject] = frozenset(members)
            else:
                by_subject.pop(subject, None)
    objects = dict(inputs.objects)
    touched_claims = [path for path in wanted if path.startswith(CLAIMS_PREFIX)]
    for path in touched_claims:
        objects.pop(path, None)
    objects.update(
        _object_histories(instance, history, claims, touched_claims, at_most=target.sequence)
    )
    coordinate = instance.coordinate_for_oid(target.git_oid)
    return FloorInputs(
        generation=target.sequence,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        latest=latest,
        subjects=subjects,
        claims=claims,
        claim_types=claim_types,
        by_subject=by_subject,
        objects=objects,
        documents=frozenset(documents),
        capture_contracts=contracts,
    )


# -- the stamps ---------------------------------------------------------------------


class _Stamps:
    """``changed_at`` for every file, from the inputs' latest sequences."""

    def __init__(self, inputs: FloorInputs, live_predicates: frozenset[str]) -> None:
        self._inputs = inputs
        self._live = live_predicates
        self._names: dict[tuple[str, str], int] = {}

    def _latest(self, path: str | None) -> int:
        return 0 if path is None else self._inputs.latest.get(path, 0)

    def name(self, predicate: str, kind: str) -> int:
        """When the field ``predicate`` of ``kind`` last changed its type or short name.

        The short name drops the ``kind.`` prefix unless the short string is
        itself an accepted predicate, so it moves with that one ClaimType too.
        """

        key = (predicate, kind)
        known = self._names.get(key)
        if known is not None:
            return known
        best = self._latest(_type_path(predicate))
        prefix = f"{kind}."
        if predicate.startswith(prefix):
            best = max(best, self._latest(_type_path(predicate[len(prefix) :])))
        self._names[key] = best
        return best

    def incoming(self, pointed: Iterable[str], edges: Iterable[ClaimArtifactAny]) -> int:
        """When the edges pointing at a Subject last changed.

        ``pointed`` is every Claim that ever pointed at it, so an edge moved
        away still restamps its old target; ``edges`` are the live ones shown.
        """

        best = 0
        for claim_file in pointed:
            best = max(best, self._latest(claim_file))
        for claim in edges:
            source = self._inputs.subjects.get(claim.statement.subject.artifact_path)
            if source is not None:
                best = max(best, self.name(claim.statement.predicate, source.subject_kind))
        return best

    def subject(self, path: str, live: Iterable[ClaimArtifactAny]) -> int:
        shell = self._inputs.subjects[path]
        best = self._latest(path)
        for claim_file in self._inputs.by_subject.get(path, ()):
            best = max(best, self._latest(claim_file))
        for claim in live:
            best = max(best, self.name(claim.statement.predicate, shell.subject_kind))
        return best


def _type_path(predicate: str) -> str | None:
    try:
        return claim_type_path(predicate)
    except CruxibleError:
        return None


# -- rendering ------------------------------------------------------------------

# The notes default: the commit the review-notes ref names when the render starts.
LIVE = object()


def render_floor(
    instance: PlaybillInstance,
    history: HistoryReader,
    inputs: FloorInputs,
    *,
    previous: FloorRender | None = None,
    notes: str | None | object = LIVE,
) -> FloorRender:
    """Render the floor at ``inputs``, reusing every file of ``previous`` whose stamp held.

    A file's bytes are a function of its inputs, and its stamp moves whenever
    any input does, so a kept render with the same stamp is the same bytes.

    ``notes`` is the one immutable review-notes commit every change rationale
    is read from: by default the commit the notes ref names now, resolved once.
    A kept change keeps its rationale only while its own notes are unchanged
    between the two snapshots, so a warm render equals a cold one.
    """

    renderer = floor_renderer(inputs.coordinate.compiler_digest)
    if previous is not None and previous.renderer != renderer:
        previous = None
    types, live_predicates = inputs.predicates()
    stamps = _Stamps(inputs, live_predicates)
    values = ValueRenderer(instance)
    pointed = inputs.pointed_at()
    subjects: dict[str, _SubjectRender] = {}
    for path in sorted(inputs.subjects, key=lambda item: item.encode()):
        shell = inputs.subjects[path]
        live = sorted(
            (
                claim
                for claim_file in inputs.by_subject.get(path, ())
                if (claim := inputs.claims[claim_file]).lifecycle.state == "live"
            ),
            key=lambda item: item.identity.name.encode(),
        )
        edges = [
            edge
            for claim_file in pointed.get(path, ())
            if (edge := inputs.claims.get(claim_file)) is not None
            and edge.lifecycle.state == "live"
            and _object(edge) == path
            and edge.statement.subject.artifact_path in inputs.subjects
        ]
        own = stamps.subject(path, live)
        changed_at = max(own, stamps.incoming(pointed.get(path, ()), edges))
        kept = None if previous is None else previous.subjects.get(path)
        if kept is not None and (kept.changed_at, kept.own_changed_at) == (changed_at, own):
            subjects[path] = kept
            continue
        part = render_subject(
            path=path,
            shell=shell,
            claims=live,
            claim_types=types,
            accepted_predicates=live_predicates,
            values=values,
            incoming=[
                IncomingEdge(
                    field=short_field_name(
                        claim.statement.predicate,
                        inputs.subjects[claim.statement.subject.artifact_path].subject_kind,
                        live_predicates,
                    ),
                    source=subject_ref(claim.statement.subject.artifact_path),
                    claim=claim.identity.name,
                )
                for claim in edges
            ],
        )
        subjects[path] = _SubjectRender(changed_at, own, part, stamped(part, changed_at))
    by_kind: dict[str, list[_SubjectRender]] = {}
    for render in subjects.values():
        by_kind.setdefault(render.part.kind, []).append(render)
    indexes: dict[str, tuple[int, bytes]] = {}
    for kind, members in by_kind.items():
        changed_at = max(item.own_changed_at for item in members)
        kept_index = None if previous is None else previous.indexes.get(kind)
        indexes[kind] = (
            kept_index
            if kept_index is not None and kept_index[0] == changed_at
            else (changed_at, render_index(kind, [item.part for item in members], changed_at))
        )
    # The changes that introduced each current Claim revision of a Subject.
    wanted = sorted(
        {
            sequence
            for path in inputs.subjects
            for claim_file in inputs.by_subject.get(path, ())
            if inputs.claims[claim_file].lifecycle.state == "live"
            and (sequence := inputs.latest.get(claim_file)) is not None
        }
    )
    snapshot = review_snapshot_oid(instance) if notes is LIVE else notes
    assert snapshot is None or isinstance(snapshot, str)
    kept_notes: Mapping[int, ChangeNote] = {}
    if previous is not None:
        moved = notes_changed_between(instance, previous.notes, snapshot)
        if moved is not None:
            kept_notes = {
                sequence: note
                for sequence, note in previous.change_notes.items()
                if not moved.intersection(note.commits)
            }
    noted = {sequence: kept_notes[sequence] for sequence in wanted if sequence in kept_notes}
    noted.update(
        change_notes(
            instance,
            [history.generation(sequence) for sequence in wanted if sequence not in noted],
            snapshot,
        )
    )
    kept_changes = {} if previous is None else previous.changes
    changes = {
        sequence: kept_changes[sequence]
        for sequence in wanted
        if sequence in kept_changes
        and previous is not None
        and previous.change_notes.get(sequence) == noted[sequence]
    }
    changes.update(
        render_changes(
            instance,
            history,
            {sequence: noted[sequence].rationale for sequence in wanted if sequence not in changes},
        )
    )
    sources_changed = max(
        (sequence for path, sequence in inputs.latest.items() if path.startswith(_SOURCE_PREFIXES)),
        default=0,
    )
    kept_sources = None if previous is None else previous.sources
    sources = (
        kept_sources
        if kept_sources is not None and kept_sources[0] == sources_changed
        else (
            sources_changed,
            render_sources_ledger(
                instance,
                claims=inputs.claims.values(),
                claim_latest={
                    claim.identity.name: inputs.latest.get(path, 0)
                    for path, claim in inputs.claims.items()
                },
                contracts={
                    capture_contract_digest(contract).tagged: contract
                    for contract in inputs.capture_contracts.values()
                },
                documents={path: inputs.latest.get(path, 0) for path in inputs.documents},
                changed_at=sources_changed,
            ),
        )
    )
    files: dict[str, tuple[bytes, int]] = {
        README_PATH: (FLOOR_README.encode(), 0),
        SOURCES_LEDGER_PATH: (sources[1], sources[0]),
    }
    for render in subjects.values():
        for path, content in render.files.items():
            files[path] = (content, render.changed_at)
    for kind, (changed_at, content) in indexes.items():
        files[index_path(kind)] = (content, changed_at)
    for sequence, content in changes.items():
        files[change_path(sequence)] = (content, sequence)
    ordered = {path: files[path] for path in sorted(files, key=lambda item: item.encode())}
    return FloorRender(
        inputs,
        renderer,
        subjects,
        indexes,
        changes,
        ordered,
        sources,
        notes=snapshot,
        change_notes={sequence: noted[sequence] for sequence in wanted},
        notes_digest=floor_notes_digest(
            {sequence: noted[sequence].rationale for sequence in wanted}
        ),
    )


# -- the index on the instance ------------------------------------------------------

_LOCKS: WeakKeyDictionary[PlaybillInstance, threading.Lock] = WeakKeyDictionary()
_LOCKS_GUARD = threading.Lock()


def _lock(instance: PlaybillInstance) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(instance)
        if lock is None:
            lock = _LOCKS[instance] = threading.Lock()
        return lock


def _kept(instance: PlaybillInstance) -> FloorRender | None:
    kept = memo_get(instance.floor_current_memo, _INDEX_KEY)
    return kept if isinstance(kept, FloorRender) else None


def _usable(history: HistoryReader, render: FloorRender | None) -> FloorRender | None:
    """The kept render, while its generation is still this accepted history's."""

    if render is None or render.generation > history.sequence:
        return None
    if history.generation(render.generation).git_oid != render.inputs.coordinate.git_oid:
        return None
    return render


def _render_at(
    instance: PlaybillInstance,
    history: HistoryReader,
    location: AcceptedGenerationLocation,
    kept: FloorRender | None,
    notes: str | None,
) -> FloorRender:
    coordinate = instance.coordinate_for_oid(location.git_oid)
    renderer = floor_renderer(coordinate.compiler.rule_digest)
    if kept is None or kept.renderer != renderer:
        inputs = build_floor_inputs(instance, coordinate, history, location)
        return render_floor(instance, history, inputs, notes=notes)
    inputs = patch_floor_inputs(instance, history, kept.inputs, location)
    return render_floor(instance, history, inputs, previous=kept, notes=notes)


def _location(
    history: HistoryReader, coordinate: AcceptedProjectionCoordinate | AcceptedCoordinate
) -> AcceptedGenerationLocation:
    location = history.generation_for_oid(coordinate.git_oid)
    if location is None:
        raise ProjectionIntegrityError("floor coordinate is outside accepted history")
    return location


def advance_floor_index(
    instance: PlaybillInstance,
    head: AcceptedProjectionCoordinate | AcceptedCoordinate | None = None,
) -> FloorRender:
    """Bring the instance's floor index to ``head`` (default: the accepted head).

    Forward from the kept index reads only what changed since; with none kept
    (a fresh process, a new renderer, a rewound history) it renders whole. An
    index already past ``head`` stays where it is and ``head`` is rendered
    beside it.
    """

    target = instance.accepted_coordinate() if head is None else head
    with _lock(instance):
        # One review-notes snapshot for the whole render, resolved once.
        notes = review_snapshot_oid(instance)
        with instance.accepted_history_reader() as history:
            location = _location(history, target)
            kept = _usable(history, _kept(instance))
            if kept is not None and kept.generation == location.sequence and kept.notes == notes:
                return kept
            render = _render_at(instance, history, location, kept, notes)
            if kept is None or render.generation >= kept.generation:
                memo_put(instance.floor_current_memo, _INDEX_KEY, render, capacity=1)
            return render


def floor_render_at(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate | AcceptedCoordinate,
) -> FloorRender:
    """The floor at ``coordinate``, from the index without moving it backwards."""

    return advance_floor_index(instance, coordinate)


def floor_render_from(
    instance: PlaybillInstance,
    render: FloorRender,
    generation: int,
) -> FloorRender:
    """The floor at another generation of the same history, patched from ``render``.

    It reads the same review-notes snapshot ``render`` did, so the two floors
    differ only by what accepted history changed between them.
    """

    with instance.accepted_history_reader() as history:
        location = history.generation(generation)
        coordinate = instance.coordinate_for_oid(location.git_oid)
        if floor_renderer(coordinate.compiler.rule_digest) != render.renderer:
            raise ProjectionIntegrityError("floor renderer differs between the two generations")
        inputs = patch_floor_inputs(instance, history, render.inputs, location)
        return render_floor(instance, history, inputs, previous=render, notes=render.notes)


def floor_render_with_notes(
    instance: PlaybillInstance, render: FloorRender, notes: str | None
) -> FloorRender:
    """``render`` with its change rationale read from another notes snapshot."""

    if notes == render.notes:
        return render
    with instance.accepted_history_reader() as history:
        return render_floor(instance, history, render.inputs, previous=render, notes=notes)


__all__ = [
    "FloorInputs",
    "FloorRender",
    "advance_floor_index",
    "build_floor_inputs",
    "floor_render_at",
    "floor_render_from",
    "floor_render_with_notes",
    "patch_floor_inputs",
    "render_floor",
]
