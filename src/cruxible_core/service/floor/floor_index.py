"""The floor index: the floor at one accepted generation, advanced from the ledger diff.

Every floor file is stamped with ``changed_at``, the latest accepted generation
that touched any of its inputs. For a Subject's ``current/`` file those are
the Subject itself, its Claims of any lifecycle, every Claim that ever pointed
at it (so a moved edge restamps its old target and its new one), and the
ClaimTypes (and the short-name shadows) of the fields it shows. The stamp is read from the
history index, so a full render and an incremental one give the same bytes,
and a file's bytes differ between two generations exactly when its stamp does.

The index is rebuildable and instance-internal. It holds the parsed inputs
(Subjects, Claims, ClaimTypes, the latest sequence of every ledger path), the
reverse indexes a change is traced through (the Claims that ever pointed at a
Subject, the Subjects of each kind, the sequences of the live Claims, and the
commits of each change's review notes),
the rendered files and each file's digest. ``advance_floor_index`` moves it
forward by reading only the paths the change records touched since, and
re-renders only the files those paths reach: a Subject is restamped only when
one of its inputs moved, a kind INDEX only when one of its Subjects did, a
change file only when it enters the floor or its review notes moved, and
``sources/LEDGER`` from the Claims and Documents that changed. A render at any
other generation is the same patch applied to a copy, in either direction, so
any generation of the accepted history can be rendered from it without
binding that generation's projection.

Each forward render records the files it wrote and removed, and the index
keeps the last few renders, so a delta from a recent base is the union of the
steps since it, against a base the index already rendered, with no file
hashed again: every file's digest is taken once, when it is rendered.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Mapping, Set
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
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
    floor_manifest_digest,
    floor_notes_digest,
)
from cruxible_client.contracts.subjects import SubjectShell, parse_subject
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.derived.persistent import EMPTY_PSET, PMap, PSet
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
    SourcesLedgerState,
    build_sources_ledger,
    update_sources_ledger,
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
# Prior heads the index keeps, so a delta from a recently delivered base reads
# that base from memory instead of patching the head back to it.
_RECENT_RENDERS = 4
# Forward steps each render carries, so a delta from a base that many
# advances back is the union of their changed files.
_STEPS_KEPT = 64
_README_BYTES = FLOOR_README.encode()


@dataclass(frozen=True)
class FloorInputs:
    """Everything the floor reads at one accepted generation, from the ledger alone.

    Every collection is persistent: a patched version shares all entries the
    change did not touch, so advancing costs the change, not the instance.
    """

    generation: int
    coordinate: AcceptedCoordinate
    # The latest accepted sequence that touched each ledger path, at this generation.
    latest: PMap[str, int]
    subjects: PMap[str, SubjectShell]
    # Every Claim in the tree, any lifecycle, by Claim path.
    claims: PMap[str, ClaimArtifactAny]
    # Every ClaimType in the tree, any lifecycle, by ClaimType path.
    claim_types: PMap[str, ClaimType]
    # Subject path -> the paths of its Claims, any lifecycle.
    by_subject: PMap[str, PSet[str]]
    # Subject-valued Claim path -> (sequence, object Subject path) of each of
    # its revisions to this generation: every Subject it ever pointed at.
    objects: PMap[str, tuple[tuple[int, str], ...]] = field(default_factory=PMap)
    # Accepted Document paths, and CaptureContracts by path.
    documents: PSet[str] = EMPTY_PSET
    capture_contracts: PMap[str, CaptureContract] = field(default_factory=PMap)
    # Derived from the fields above and patched with them, so a render traces a
    # change instead of scanning: Subject path -> every Claim that ever pointed
    # at it; predicate -> its Claims; Subject kind -> how many Subjects have
    # it; the ClaimTypes by predicate and the live predicates; the contracts
    # by digest (and each contract path's digest); and the latest sequence of
    # the source paths (claims/, documents/, capture-contracts/) as a count per
    # sequence, with its maximum.
    pointed: PMap[str, PSet[str]] = field(default_factory=PMap)
    by_predicate: PMap[str, PSet[str]] = field(default_factory=PMap)
    subject_kinds: PMap[str, int] = field(default_factory=PMap)
    types: PMap[str, ClaimType] = field(default_factory=PMap)
    live_predicates: PSet[str] = EMPTY_PSET
    contracts_by_digest: PMap[str, CaptureContract] = field(default_factory=PMap)
    contract_digests: PMap[str, str] = field(default_factory=PMap)
    source_latest: PMap[int, int] = field(default_factory=PMap)
    sources_changed: int = 0

    def pointed_at(self) -> Mapping[str, PSet[str]]:
        """Subject path -> every Claim that ever pointed at it, any lifecycle."""

        return self.pointed

    def predicates(self) -> tuple[Mapping[str, ClaimType], PSet[str]]:
        return self.types, self.live_predicates


@dataclass(frozen=True)
class _SubjectRender:
    changed_at: int
    # Excluding the edges pointing in: what the kind's INDEX row reads.
    own_changed_at: int
    part: SubjectPart
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class FloorStep:
    """What one forward render changed: the files it wrote and the paths it dropped."""

    base: int
    generation: int
    written: frozenset[str]
    removed: frozenset[str]


@dataclass(frozen=True)
class FloorRender:
    """The floor at one generation: every file with its bytes and ``changed_at``."""

    inputs: FloorInputs
    renderer: str
    subjects: PMap[str, _SubjectRender]
    indexes: PMap[str, tuple[int, bytes]]
    changes: PMap[int, bytes]
    files: PMap[str, tuple[bytes, int]] = field(default_factory=PMap)
    sources: tuple[int, bytes] | None = None
    # The one immutable review-notes commit every change file was read from
    # (None: no notes), what each change's notes said, and their identity.
    notes: str | None = None
    change_notes: PMap[int, ChangeNote] = field(default_factory=PMap)
    # Each file's (content digest, byte length), taken once when it was rendered.
    digests: PMap[str, tuple[str, int]] = field(default_factory=PMap)
    # Kind -> its Subjects; live Claim sequence -> how many live Claims it
    # stamps (the changes the floor shows); notes commit -> the changes read from it.
    kinds: PMap[str, PSet[str]] = field(default_factory=PMap)
    live_sequences: PMap[int, int] = field(default_factory=PMap)
    notes_by_commit: PMap[str, PSet[int]] = field(default_factory=PMap)
    source_state: SourcesLedgerState | None = None
    # The forward renders that led here, oldest first.
    steps: tuple[FloorStep, ...] = ()

    @property
    def generation(self) -> int:
        return self.inputs.generation

    @cached_property
    def notes_digest(self) -> str:
        return floor_notes_digest(
            {sequence: note.rationale for sequence, note in self.change_notes.items()}
        )

    @cached_property
    def _manifest(self) -> FloorManifest:
        # The v6 manifest digests the whole inventory: its build is the format's
        # O(files), once per render and only when a delta or export needs it.
        return build_floor_manifest(
            renderer=self.renderer,
            coordinate=self.inputs.coordinate,
            generation=self.inputs.generation,
            notes_digest=self.notes_digest,
            files={
                path: (*self.digests[path], changed)
                for path, (_content, changed) in self.files.items()
            },
        )

    def manifest(self) -> FloorManifest:
        """The floor's manifest, built once per render from the digests taken at render time."""

        return self._manifest

    @cached_property
    def manifest_digest(self) -> str:
        return floor_manifest_digest(self._manifest)

    def changed_paths_since(self, base: int) -> frozenset[str] | None:
        """Every file path this render's forward steps touched after ``base``.

        None when the kept steps do not reach back to ``base`` as one unbroken
        chain; the caller then compares the two floors whole.
        """

        start = next((index for index, step in enumerate(self.steps) if step.base == base), None)
        if start is None:
            return None
        found: set[str] = set()
        previous = base
        for step in self.steps[start:]:
            if step.base != previous:
                return None
            found.update(step.written)
            found.update(step.removed)
            previous = step.generation
        return frozenset(found) if previous == self.generation else None


# -- reading the inputs -----------------------------------------------------------


def _parse(path: str, content: bytes) -> tuple[str, Any]:
    """Which input map a member belongs in, and its parsed value."""

    if path.startswith(SUBJECTS_PREFIX):
        return "subject", parse_subject(content, path=path)
    if path.startswith(CLAIMS_PREFIX):
        return "claim", parse_claim(content, path=path)
    if path.startswith(CLAIM_TYPES_PREFIX):
        return "claim_type", parse_claim_type(content, path=path)
    if path.startswith(DOCUMENTS_PREFIX):
        return "document", True
    if path.startswith(CAPTURE_CONTRACTS_PREFIX):
        return "contract", parse_capture_contract(content, path=path)
    return "other", None


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
    known: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
    since: int | None = None,
) -> dict[str, tuple[tuple[int, str], ...]]:
    """Every Subject each Subject-valued Claim pointed at, revision by revision.

    A Claim touched once is its current object. One revised in place is read
    back at each generation that touched it; a predicate keeps its object kind,
    so only Subject-valued Claims are read.

    ``known`` holds each Claim's history at generation ``since``: it is
    extended by only the sequences in ``(since, at_most]`` (or cut back to
    ``at_most``), and a revision it names, or the one at ``at_most`` itself
    (the current Claim), is not read again. A Claim it does not hold is read
    whole.
    """

    wanted = [path for path in paths if _object(claims.get(path)) is not None]
    histories: dict[str, list[tuple[int, str]]] = {}
    revisited: dict[int, list[str]] = {}
    whole: list[str] = []
    extended: list[str] = []
    for path in wanted:
        prior = () if known is None or since is None else known.get(path, ())
        if not prior or (len(prior) == 1 and prior[0][0] == 0):
            whole.append(path)
            continue
        assert since is not None
        if at_most < since:
            kept = [item for item in prior if item[0] <= at_most]
            if kept:
                histories[path] = kept
            else:
                whole.append(path)
        else:
            extended.append(path)
    sequences: dict[str, tuple[int, ...]] = {}
    if whole:
        sequences.update(
            (path, tuple(item for item in found if item <= at_most))
            for path, found in history.member_sequences(whole).items()
        )
    newer: dict[str, tuple[int, ...]] = {}
    if extended:
        assert since is not None
        newer = history.member_sequences_between(extended, since, at_most)
    for path in whole:
        touched = sequences.get(path, ())
        current = _object(claims[path])
        assert current is not None
        if len(touched) <= 1:
            histories[path] = [(touched[0] if touched else 0, current)]
            continue
        histories[path] = []
        for sequence in touched:
            if sequence == at_most:
                histories[path].append((sequence, current))
            else:
                revisited.setdefault(sequence, []).append(path)
    for path in extended:
        assert known is not None
        current = _object(claims[path])
        assert current is not None
        histories[path] = list(known[path])
        for sequence in newer.get(path, ()):
            if sequence == at_most:
                histories[path].append((sequence, current))
            else:
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


def _predicate_of_type_path(path: str) -> str:
    namespace, _separator, name = path.removeprefix(CLAIM_TYPES_PREFIX).rpartition("/")
    return f"{namespace}.{name.removesuffix('.json')}"


def _max_count_key(counts: Mapping[int, int]) -> int:
    return max((key for key, count in counts.items() if count), default=0)


class _Moves:
    """Membership changes to a key -> members index, applied once per key."""

    def __init__(self) -> None:
        self._added: dict[Any, set[Any]] = {}
        self._removed: dict[Any, set[Any]] = {}

    def move(self, key: Any, member: Any, *, present: bool) -> None:
        (self._added if present else self._removed).setdefault(key, set()).add(member)
        (self._removed if present else self._added).get(key, set()).discard(member)

    def keys(self) -> set[Any]:
        return {*self._added, *self._removed}

    def apply(self, index: Any) -> None:
        """Apply to a ``PMapEvolver`` of key -> ``PSet``; each set copies only its path."""

        for key in self.keys():
            members = index.get(key, EMPTY_PSET).update(
                self._added.get(key, ()), self._removed.get(key, ())
            )
            if members:
                index[key] = members
            else:
                index.discard(key)


def _count(index: Any, key: Any, delta: int) -> None:
    """Add ``delta`` to one counter of a ``PMapEvolver``, dropping it at zero."""

    value = index.get(key, 0) + delta
    if value:
        index[key] = value
    else:
        index.discard(key)


def build_floor_inputs(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    history: HistoryReader,
    location: AcceptedGenerationLocation,
) -> FloorInputs:
    """Read the floor's inputs whole, from the accepted projection at ``coordinate``.

    The cold path: a fresh process, a new renderer or a rewound history.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        paths = tuple(
            row.path
            for kind in ("subject", "claim", "claim-type", "capture-contract")
            for row in projection.typed.envelopes(kind=kind)
        )
        document_paths = [row.path for row in projection.typed.envelopes(kind="document")]
        projection.typed.prefetch_members(paths)
        raw = {path: projection.typed.member_bytes(path) for path in paths}
    held: dict[str, dict[str, Any]] = {
        name: {} for name in ("subject", "claim", "claim_type", "document", "contract", "other")
    }
    for path in document_paths:
        held["document"][path] = True
    for path, content in raw.items():
        if content is not None:
            kind, value = _parse(path, content)
            held[kind][path] = value
    claims = held["claim"]
    latest = history.latest_sequences(at_most=location.sequence)
    objects = _object_histories(instance, history, claims, claims, at_most=location.sequence)
    by_subject: dict[str, set[str]] = {}
    by_predicate: dict[str, set[str]] = {}
    for path, claim in claims.items():
        by_subject.setdefault(claim.statement.subject.artifact_path, set()).add(path)
        by_predicate.setdefault(claim.statement.predicate, set()).add(path)
    pointed: dict[str, set[str]] = {}
    for claim_file, revisions in objects.items():
        for _sequence, target in revisions:
            pointed.setdefault(target, set()).add(claim_file)
    source_latest: dict[int, int] = {}
    for path, sequence in latest.items():
        if path.startswith(_SOURCE_PREFIXES):
            source_latest[sequence] = source_latest.get(sequence, 0) + 1
    subject_kinds: dict[str, int] = {}
    for shell in held["subject"].values():
        subject_kinds[shell.subject_kind] = subject_kinds.get(shell.subject_kind, 0) + 1
    types = {item.predicate: item for item in held["claim_type"].values()}
    contract_digests = {
        path: capture_contract_digest(contract).tagged
        for path, contract in held["contract"].items()
    }
    return FloorInputs(
        generation=location.sequence,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        latest=PMap(latest),
        subjects=PMap(held["subject"]),
        claims=PMap(claims),
        claim_types=PMap(held["claim_type"]),
        by_subject=PMap((key, PSet(found)) for key, found in by_subject.items()),
        objects=PMap(objects),
        documents=PSet(held["document"]),
        capture_contracts=PMap(held["contract"]),
        pointed=PMap((key, PSet(found)) for key, found in pointed.items()),
        by_predicate=PMap((key, PSet(found)) for key, found in by_predicate.items()),
        subject_kinds=PMap(subject_kinds),
        types=PMap(types),
        live_predicates=PSet(
            predicate for predicate, item in types.items() if item.lifecycle.state == "live"
        ),
        contracts_by_digest=PMap(
            (contract_digests[path], contract) for path, contract in held["contract"].items()
        ),
        contract_digests=PMap(contract_digests),
        source_latest=PMap(source_latest),
        sources_changed=_max_count_key(source_latest),
    )


def _patch_floor_inputs(
    instance: PlaybillInstance,
    history: HistoryReader,
    inputs: FloorInputs,
    target: AcceptedGenerationLocation,
) -> tuple[FloorInputs, frozenset[str]]:
    """The inputs at ``target`` and every ledger path touched between the two.

    Only the touched paths are read, and every map is updated in place of its
    touched entries: the work is the change between the two generations.
    """

    if target.sequence == inputs.generation:
        return inputs, frozenset()
    low, high = sorted((inputs.generation, target.sequence))
    touched = history.member_paths_between(low, high)
    changed = sorted(touched, key=lambda item: item.encode())
    at_target = history.latest_sequences(changed, at_most=target.sequence)
    latest = inputs.latest.evolver()
    source_latest = inputs.source_latest.evolver()
    sources_changed = inputs.sources_changed
    source_removed = False
    for path in changed:
        was = latest.get(path)
        now = at_target.get(path)
        if now is None:
            latest.discard(path)
        else:
            latest[path] = now
        if was == now or not path.startswith(_SOURCE_PREFIXES):
            continue
        if was is not None:
            _count(source_latest, was, -1)
            if was == sources_changed and was not in source_latest:
                source_removed = True
        if now is not None:
            _count(source_latest, now, 1)
    candidate = max(
        (sequence for path, sequence in at_target.items() if path.startswith(_SOURCE_PREFIXES)),
        default=None,
    )
    source_counts = source_latest.persistent()
    if candidate is not None and candidate >= sources_changed:
        sources_changed = candidate
    elif source_removed:
        # Only when the newest source path itself went away (a patch backwards).
        sources_changed = _max_count_key(source_counts)
    wanted = [path for path in changed if path.startswith(_INPUT_PREFIXES)]
    blobs = instance.blobs_at(target.git_oid, wanted) if wanted else {}
    subjects = inputs.subjects.evolver()
    claims = inputs.claims.evolver()
    claim_types = inputs.claim_types.evolver()
    documents_added: list[str] = []
    documents_removed: list[str] = []
    contracts = inputs.capture_contracts.evolver()
    types = inputs.types.evolver()
    contracts_by_digest = inputs.contracts_by_digest.evolver()
    contract_digests = inputs.contract_digests.evolver()
    subject_kinds = inputs.subject_kinds.evolver()
    live_added: list[str] = []
    live_removed: list[str] = []
    subject_moves = _Moves()
    predicate_moves = _Moves()
    for path in wanted:
        content = blobs.get(path)
        value: Any
        kind, value = ("other", None) if content is None else _parse(path, content)
        if path.startswith(SUBJECTS_PREFIX):
            old_shell = subjects.get(path)
            if old_shell is not None:
                _count(subject_kinds, old_shell.subject_kind, -1)
            if kind == "subject":
                subjects[path] = value
                _count(subject_kinds, value.subject_kind, 1)
            else:
                subjects.discard(path)
        elif path.startswith(CLAIMS_PREFIX):
            old = claims.get(path)
            new = value if kind == "claim" else None
            if new is None:
                claims.discard(path)
            else:
                claims[path] = new
            for item, present in ((old, False), (new, True)):
                if item is None:
                    continue
                subject_moves.move(item.statement.subject.artifact_path, path, present=present)
                predicate_moves.move(item.statement.predicate, path, present=present)
        elif path.startswith(CLAIM_TYPES_PREFIX):
            old_type = claim_types.get(path)
            if old_type is not None:
                if types.get(old_type.predicate) is old_type:
                    types.discard(old_type.predicate)
                live_removed.append(old_type.predicate)
            if kind == "claim_type":
                claim_types[path] = value
                types[value.predicate] = value
                if value.lifecycle.state == "live":
                    live_added.append(value.predicate)
            else:
                claim_types.discard(path)
        elif path.startswith(DOCUMENTS_PREFIX):
            (documents_added if kind == "document" else documents_removed).append(path)
        elif path.startswith(CAPTURE_CONTRACTS_PREFIX):
            old_digest = contract_digests.get(path)
            if old_digest is not None:
                contracts_by_digest.discard(old_digest)
                contract_digests.discard(path)
            if kind == "contract":
                digest = capture_contract_digest(value).tagged
                contracts[path] = value
                contracts_by_digest[digest] = value
                contract_digests[path] = digest
            else:
                contracts.discard(path)
    by_subject = inputs.by_subject.evolver()
    subject_moves.apply(by_subject)
    by_predicate = inputs.by_predicate.evolver()
    predicate_moves.apply(by_predicate)
    claims_now = claims.persistent()
    touched_claims = [path for path in wanted if path.startswith(CLAIMS_PREFIX)]
    objects = inputs.objects.evolver()
    before = {path: inputs.objects.get(path, ()) for path in touched_claims}
    histories = _object_histories(
        instance,
        history,
        claims_now,
        touched_claims,
        at_most=target.sequence,
        known=before,
        since=inputs.generation,
    )
    pointer_moves = _Moves()
    for path in touched_claims:
        found = histories.get(path)
        if found is None:
            objects.discard(path)
        else:
            objects[path] = found
        old_targets = {item for _sequence, item in before[path]}
        new_targets = {item for _sequence, item in found or ()}
        for item in old_targets - new_targets:
            pointer_moves.move(item, path, present=False)
        for item in new_targets - old_targets:
            pointer_moves.move(item, path, present=True)
    pointed = inputs.pointed.evolver()
    pointer_moves.apply(pointed)
    types_now = types.persistent()
    live = inputs.live_predicates
    if live_added or live_removed:
        # A removed predicate stays live while a live ClaimType still names it.
        live = live.update(
            live_added,
            [
                predicate
                for predicate in live_removed
                if predicate not in live_added
                or types_now.get(predicate) is None
                or types_now[predicate].lifecycle.state != "live"
            ],
        )
    coordinate = instance.coordinate_for_oid(target.git_oid)
    return (
        FloorInputs(
            generation=target.sequence,
            coordinate=AcceptedCoordinate.from_internal(coordinate),
            latest=latest.persistent(),
            subjects=subjects.persistent(),
            claims=claims_now,
            claim_types=claim_types.persistent(),
            by_subject=by_subject.persistent(),
            objects=objects.persistent(),
            documents=inputs.documents.update(documents_added, documents_removed),
            capture_contracts=contracts.persistent(),
            pointed=pointed.persistent(),
            by_predicate=by_predicate.persistent(),
            subject_kinds=subject_kinds.persistent(),
            types=types_now,
            live_predicates=live,
            contracts_by_digest=contracts_by_digest.persistent(),
            contract_digests=contract_digests.persistent(),
            source_latest=source_counts,
            sources_changed=sources_changed,
        ),
        touched,
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

    return _patch_floor_inputs(instance, history, inputs, target)[0]


# -- the stamps ---------------------------------------------------------------------


class _Stamps:
    """``changed_at`` for every file, from the inputs' latest sequences."""

    def __init__(self, inputs: FloorInputs, live_predicates: Set[str]) -> None:
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


# -- what a change reaches ------------------------------------------------------------


def _affected_subjects(old: FloorInputs, new: FloorInputs, changed: Collection[str]) -> set[str]:
    """Every Subject whose stamp could read a changed path, in either generation.

    A Subject's stamp reads its own path, its Claims, every Claim that ever
    pointed at it, the edges' source Subjects and the ClaimTypes (and
    short-name shadows) of the fields it and its edges show. A Subject outside
    this set reads none of the changed paths, so its stamp and bytes hold.
    """

    affected: set[str] = set()
    type_predicates: set[str] = set()
    sides = (old, new)
    for path in changed:
        if path.startswith(SUBJECTS_PREFIX):
            affected.add(path)
            for inputs in sides:
                for claim_file in inputs.by_subject.get(path, ()):
                    target = _object(inputs.claims.get(claim_file))
                    if target is not None:
                        affected.add(target)
        elif path.startswith(CLAIMS_PREFIX):
            for inputs in sides:
                claim = inputs.claims.get(path)
                if claim is not None:
                    affected.add(claim.statement.subject.artifact_path)
                affected.update(target for _sequence, target in inputs.objects.get(path, ()))
        elif path.startswith(CLAIM_TYPES_PREFIX):
            type_predicates.add(_predicate_of_type_path(path))
            for inputs in sides:
                claim_type = inputs.claim_types.get(path)
                if claim_type is not None:
                    type_predicates.add(claim_type.predicate)
    if type_predicates:
        # A field's stamp reads its ClaimType and its short-name shadow: the
        # predicate itself and ``<kind>.<predicate>`` for every Subject kind.
        for inputs in sides:
            reached = {
                shown
                for predicate in type_predicates
                for shown in (predicate, *(f"{kind}.{predicate}" for kind in inputs.subject_kinds))
            }
            for predicate in reached:
                for claim_file in inputs.by_predicate.get(predicate, ()):
                    claim = inputs.claims[claim_file]
                    affected.add(claim.statement.subject.artifact_path)
                    target = _object(claim)
                    if target is not None:
                        affected.add(target)
    return affected


def _live_sequence(inputs: FloorInputs, claim_file: str) -> int | None:
    """The sequence a live Claim of a floor Subject stamps the changes with."""

    claim = inputs.claims.get(claim_file)
    if (
        claim is None
        or claim.lifecycle.state != "live"
        or claim.statement.subject.artifact_path not in inputs.subjects
    ):
        return None
    found: int | None = inputs.latest.get(claim_file)
    return found


def _all_live_sequences(inputs: FloorInputs) -> dict[int, int]:
    counts: dict[int, int] = {}
    for path in inputs.subjects:
        for claim_file in inputs.by_subject.get(path, ()):
            sequence = _live_sequence(inputs, claim_file)
            if sequence is not None:
                counts[sequence] = counts.get(sequence, 0) + 1
    return counts


# -- rendering ------------------------------------------------------------------

# The notes default: the commit the review-notes ref names when the render starts.
LIVE = object()


class _Files:
    """The floor's files and digests being updated, and what this render changed."""

    def __init__(self, previous: FloorRender | None) -> None:
        self.files = (PMap() if previous is None else previous.files).evolver()
        self.digests = (PMap() if previous is None else previous.digests).evolver()
        self.written: set[str] = set()
        self.removed: set[str] = set()

    def fresh(self, path: str, content: bytes, changed: int) -> None:
        """Record one rendered file; its digest is taken here, once."""

        held = self.files.get(path)
        if held is not None and held[0] is content and held[1] == changed:
            return
        self.files[path] = (content, changed)
        self.digests[path] = (content_digest(content), len(content))
        self.written.add(path)

    def drop(self, path: str) -> None:
        if path in self.files:
            self.files.discard(path)
            self.digests.discard(path)
            self.removed.add(path)


def render_floor(
    instance: PlaybillInstance,
    history: HistoryReader,
    inputs: FloorInputs,
    *,
    previous: FloorRender | None = None,
    notes: str | None | object = LIVE,
    changed: Collection[str] | None = None,
    step: bool = False,
) -> FloorRender:
    """Render the floor at ``inputs``, reusing every file of ``previous`` whose stamp held.

    A file's bytes are a function of its inputs, and its stamp moves whenever
    any input does, so a kept render with the same stamp is the same bytes.
    With ``changed``, the ledger paths that differ between ``previous`` and
    ``inputs``, only the files those paths reach are restamped and every
    other entry is shared with ``previous``; without it every Subject's stamp
    is checked against ``previous``.

    ``notes`` is the one immutable review-notes commit every change rationale
    is read from: by default the commit the notes ref names now, resolved once.
    A kept change keeps its rationale only while its own notes are unchanged
    between the two snapshots, so a warm render equals a cold one.

    ``step`` records the files this render wrote and dropped as one forward
    step after ``previous``'s, for deltas from a recent base.
    """

    renderer = floor_renderer(inputs.coordinate.compiler_digest)
    if previous is not None and previous.renderer != renderer:
        previous = None
    old = None if previous is None else previous.inputs
    if old is None:
        changed = None
    types, live_predicates = inputs.types, inputs.live_predicates
    stamps = _Stamps(inputs, live_predicates)
    values = ValueRenderer(instance)
    pointed = inputs.pointed
    subjects = (PMap() if previous is None else previous.subjects).evolver()
    files = _Files(previous)
    kind_moves = _Moves()
    # Per kind: the own stamps of its restamped Subjects, and whether a
    # Subject that held the kind's INDEX stamp left it or went back.
    reached_owns: dict[str, list[int]] = {}
    held_index = {} if previous is None else previous.indexes
    lowered: set[str] = set()
    if changed is None or old is None:
        candidates: Collection[str] = {
            *inputs.subjects,
            *(() if previous is None else previous.subjects),
        }
    else:
        candidates = _affected_subjects(old, inputs, changed)
    for path in sorted(candidates, key=lambda item: item.encode()):
        kept = subjects.get(path)
        shell = inputs.subjects.get(path)
        if shell is None:
            if kept is None:
                continue
            subjects.discard(path)
            kind_moves.move(kept.part.kind, path, present=False)
            index = held_index.get(kept.part.kind)
            if index is not None and kept.own_changed_at >= index[0]:
                lowered.add(kept.part.kind)
            for file_path in kept.files:
                files.drop(file_path)
            continue
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
        if kept is not None and (kept.changed_at, kept.own_changed_at) == (changed_at, own):
            continue
        part = render_subject(
            path=path,
            shell=shell,
            claims=live,
            claim_types=types,
            accepted_predicates=live_predicates,  # type: ignore[arg-type]
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
        render = _SubjectRender(changed_at, own, part, stamped(part, changed_at))
        subjects[path] = render
        reached_owns.setdefault(part.kind, []).append(own)
        if kept is None or kept.part.kind != part.kind:
            kind_moves.move(part.kind, path, present=True)
        if kept is not None:
            index = held_index.get(kept.part.kind)
            if (
                index is not None
                and kept.own_changed_at >= index[0]
                and (kept.part.kind != part.kind or own < kept.own_changed_at)
            ):
                lowered.add(kept.part.kind)
            if kept.part.kind != part.kind:
                kind_moves.move(kept.part.kind, path, present=False)
            for file_path in kept.files:
                if file_path not in render.files:
                    files.drop(file_path)
        for file_path, content in render.files.items():
            files.fresh(file_path, content, changed_at)
    subjects_now = subjects.persistent()
    kinds = (PMap() if previous is None else previous.kinds).evolver()
    kind_moves.apply(kinds)
    kinds_now = kinds.persistent()
    indexes = (PMap() if previous is None else previous.indexes).evolver()
    for kind in sorted({*reached_owns, *kind_moves.keys()}):
        members = kinds_now.get(kind)
        if not members:
            if kind in indexes:
                indexes.discard(kind)
                files.drop(index_path(kind))
            continue
        kept_index = indexes.get(kind)
        if kept_index is None or kind in lowered:
            # The kind's stamp may have gone down: read it from its members.
            index_changed = max(subjects_now[member].own_changed_at for member in members)
        else:
            index_changed = max(kept_index[0], *reached_owns.get(kind, ()))
        if kept_index is not None and kept_index[0] == index_changed:
            continue
        # The INDEX file lists every Subject of its kind: its bytes are the
        # format's whole output for that kind.
        content = render_index(
            kind, [subjects_now[member].part for member in members], index_changed
        )
        indexes[kind] = (index_changed, content)
        files.fresh(index_path(kind), content, index_changed)
    # The changes that introduced each current Claim revision of a Subject.
    held_before: Mapping[int, int] = PMap() if previous is None else previous.live_sequences
    if changed is None or old is None:
        counts = _all_live_sequences(inputs)
        live_sequences = PMap(counts)
        moved_sequences: set[int] = {*counts, *held_before}
    else:
        evolving = previous.live_sequences.evolver() if previous is not None else PMap().evolver()
        moved_sequences = set()
        reached = {path for path in changed if path.startswith(CLAIMS_PREFIX)}
        for path in changed:
            if path.startswith(SUBJECTS_PREFIX):
                reached.update(old.by_subject.get(path, ()))
                reached.update(inputs.by_subject.get(path, ()))
        for claim_file in reached:
            before = _live_sequence(old, claim_file)
            after = _live_sequence(inputs, claim_file)
            if before == after:
                continue
            if before is not None:
                moved_sequences.add(before)
                _count(evolving, before, -1)
            if after is not None:
                moved_sequences.add(after)
                _count(evolving, after, 1)
        live_sequences = evolving.persistent()
    entered = {
        sequence
        for sequence in moved_sequences
        if sequence in live_sequences and sequence not in held_before
    }
    left = {
        sequence
        for sequence in moved_sequences
        if sequence in held_before and sequence not in live_sequences
    }
    snapshot = review_snapshot_oid(instance) if notes is LIVE else notes
    assert snapshot is None or isinstance(snapshot, str)
    previous_notes: PMap[int, ChangeNote] = PMap() if previous is None else previous.change_notes
    by_commit_before: PMap[str, PSet[int]] = (
        PMap() if previous is None else previous.notes_by_commit
    )
    noted = previous_notes.evolver()
    moved = None if previous is None else notes_changed_between(instance, previous.notes, snapshot)
    if moved is None:
        stale = set(live_sequences)
    else:
        stale = {
            sequence
            for commit in moved
            for sequence in by_commit_before.get(commit, ())
            if sequence in live_sequences
        }
    refetch = sorted(stale | entered)
    commit_moves = _Moves()
    for sequence in left | set(refetch):
        note = noted.pop(sequence, None)
        if note is not None:
            for commit in note.commits:
                commit_moves.move(commit, sequence, present=False)
    fetched = change_notes(
        instance, [history.generation(sequence) for sequence in refetch], snapshot
    )
    changes = (PMap() if previous is None else previous.changes).evolver()
    for sequence in left:
        if sequence in changes:
            changes.discard(sequence)
            files.drop(change_path(sequence))
    rerender: dict[int, tuple[str, ...]] = {}
    for sequence in refetch:
        note = fetched[sequence]
        noted[sequence] = note
        for commit in note.commits:
            commit_moves.move(commit, sequence, present=True)
        if sequence in changes and previous_notes.get(sequence) == note:
            continue
        rerender[sequence] = note.rationale
    by_commit = by_commit_before.evolver()
    commit_moves.apply(by_commit)
    for sequence, content in render_changes(instance, history, rerender).items():
        changes[sequence] = content
        files.fresh(change_path(sequence), content, sequence)
    if previous is None:
        files.fresh(README_PATH, _README_BYTES, 0)
    sources_changed = inputs.sources_changed
    sources = None if previous is None else previous.sources
    source_state = None if previous is None else previous.source_state
    if sources is None or source_state is None or sources[0] != sources_changed:
        if (
            changed is None
            or source_state is None
            or any(path.startswith(CAPTURE_CONTRACTS_PREFIX) for path in changed)
        ):
            # A contract change relabels every line: read every Claim again.
            source_state = build_sources_ledger(
                instance,
                claims=inputs.claims,
                latest=inputs.latest,
                contracts=inputs.contracts_by_digest,
                documents=inputs.documents,
            )
        else:
            source_state = update_sources_ledger(
                instance,
                source_state,
                claims=inputs.claims,
                latest=inputs.latest,
                contracts=inputs.contracts_by_digest,
                documents=inputs.documents,
                changed_claims=[path for path in changed if path.startswith(CLAIMS_PREFIX)],
                changed_documents=[path for path in changed if path.startswith(DOCUMENTS_PREFIX)],
            )
        # The whole file is the format's output; it moves only with a source path.
        sources = (sources_changed, source_state.render(sources_changed))
        files.fresh(SOURCES_LEDGER_PATH, sources[1], sources[0])
    steps: tuple[FloorStep, ...] = ()
    if step and previous is not None:
        steps = (
            *previous.steps[-(_STEPS_KEPT - 1) :],
            FloorStep(
                previous.generation,
                inputs.generation,
                frozenset(files.written),
                frozenset(files.removed),
            ),
        )
    return FloorRender(
        inputs,
        renderer,
        subjects_now,
        indexes.persistent(),
        changes.persistent(),
        files.files.persistent(),
        sources,
        notes=snapshot,
        change_notes=noted.persistent(),
        digests=files.digests.persistent(),
        kinds=kinds_now,
        live_sequences=live_sequences,
        notes_by_commit=by_commit.persistent(),
        source_state=source_state,
        steps=steps,
    )


# -- the index on the instance ------------------------------------------------------

_LOCKS: WeakKeyDictionary[PlaybillInstance, threading.Lock] = WeakKeyDictionary()
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class _FloorIndex:
    """The kept head render and the few heads before it."""

    head: FloorRender
    recent: tuple[FloorRender, ...] = ()


def _lock(instance: PlaybillInstance) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(instance)
        if lock is None:
            lock = _LOCKS[instance] = threading.Lock()
        return lock


def _kept_index(instance: PlaybillInstance) -> _FloorIndex | None:
    kept = memo_get(instance.floor_current_memo, _INDEX_KEY)
    return kept if isinstance(kept, _FloorIndex) else None


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
    inputs, changed = _patch_floor_inputs(instance, history, kept.inputs, location)
    return render_floor(
        instance,
        history,
        inputs,
        previous=kept,
        notes=notes,
        changed=changed,
        step=location.sequence >= kept.generation,
    )


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
            index = _kept_index(instance)
            kept = _usable(history, None if index is None else index.head)
            if kept is not None and kept.generation == location.sequence and kept.notes == notes:
                return kept
            render = _render_at(instance, history, location, kept, notes)
            if kept is None or render.generation >= kept.generation:
                recent: tuple[FloorRender, ...] = ()
                if kept is not None and index is not None:
                    recent = (kept, *index.recent)[:_RECENT_RENDERS]
                memo_put(
                    instance.floor_current_memo,
                    _INDEX_KEY,
                    _FloorIndex(render, recent),
                    capacity=1,
                )
            return render


def floor_render_at(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate | AcceptedCoordinate,
) -> FloorRender:
    """The floor at ``coordinate``, from the index without moving it backwards."""

    return advance_floor_index(instance, coordinate)


def recent_floor_render(
    instance: PlaybillInstance, head: FloorRender, generation: int
) -> FloorRender | None:
    """A floor the index already rendered at ``generation`` that equals ``head``'s base.

    Equal means: the same renderer and history, and no review note any of its
    changes read has moved between its notes snapshot and ``head``'s, so it is
    the floor ``floor_render_from(head, generation)`` would render, without
    patching back to it.
    """

    index = _kept_index(instance)
    if index is None:
        return None
    with instance.accepted_history_reader() as history:
        for candidate in (index.head, *index.recent):
            if (
                candidate.generation != generation
                or candidate.renderer != head.renderer
                or _usable(history, candidate) is None
            ):
                continue
            moved = notes_changed_between(instance, candidate.notes, head.notes)
            if moved is None or any(commit in candidate.notes_by_commit for commit in moved):
                continue
            return candidate
    return None


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
        inputs, changed = _patch_floor_inputs(instance, history, render.inputs, location)
        return render_floor(
            instance, history, inputs, previous=render, notes=render.notes, changed=changed
        )


__all__ = [
    "FloorInputs",
    "FloorRender",
    "FloorStep",
    "advance_floor_index",
    "build_floor_inputs",
    "floor_render_at",
    "floor_render_from",
    "patch_floor_inputs",
    "recent_floor_render",
    "render_floor",
]
