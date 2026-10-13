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
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
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
    # Derived from the fields above and patched with them, so a render traces a
    # change instead of scanning: Subject path -> every Claim that ever pointed
    # at it; the ClaimTypes by predicate and the live predicates; the
    # contracts by digest; and the
    # latest sequence of the source paths (claims/, documents/,
    # capture-contracts/) as a count per sequence, with its maximum.
    pointed: Mapping[str, frozenset[str]] = field(default_factory=dict)
    types: Mapping[str, ClaimType] = field(default_factory=dict)
    live_predicates: frozenset[str] = frozenset()
    contracts_by_digest: Mapping[str, CaptureContract] = field(default_factory=dict)
    source_latest: Mapping[int, int] = field(default_factory=dict)
    sources_changed: int = 0

    def pointed_at(self) -> Mapping[str, frozenset[str]]:
        """Subject path -> every Claim that ever pointed at it, any lifecycle."""

        return self.pointed

    def predicates(self) -> tuple[Mapping[str, ClaimType], frozenset[str]]:
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
    subjects: Mapping[str, _SubjectRender]
    indexes: Mapping[str, tuple[int, bytes]]
    changes: Mapping[int, bytes]
    files: Mapping[str, tuple[bytes, int]] = field(default_factory=dict)
    sources: tuple[int, bytes] | None = None
    # The one immutable review-notes commit every change file was read from
    # (None: no notes), what each change's notes said, and their identity.
    notes: str | None = None
    change_notes: Mapping[int, ChangeNote] = field(default_factory=dict)
    # Each file's (content digest, byte length), taken once when it was rendered.
    digests: Mapping[str, tuple[str, int]] = field(default_factory=dict)
    # Kind -> its Subjects; live Claim sequence -> how many live Claims it
    # stamps (the changes the floor shows); notes commit -> the changes read from it.
    kinds: Mapping[str, frozenset[str]] = field(default_factory=dict)
    live_sequences: Mapping[int, int] = field(default_factory=dict)
    notes_by_commit: Mapping[str, frozenset[int]] = field(default_factory=dict)
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
    known: Mapping[str, tuple[tuple[int, str], ...]] | None = None,
) -> dict[str, tuple[tuple[int, str], ...]]:
    """Every Subject each Subject-valued Claim pointed at, revision by revision.

    A Claim touched once is its current object. One revised in place is read
    back at each generation that touched it; a predicate keeps its object kind,
    so only Subject-valued Claims are read. ``known`` holds histories already
    read at another generation: a revision they name, or the one at
    ``at_most`` itself (the current Claim), is not read again.
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
        prior = dict(() if known is None else known.get(path, ()))
        histories[path] = []
        for sequence in touched:
            if sequence == at_most:
                histories[path].append((sequence, current))
            elif sequence in prior:
                histories[path].append((sequence, prior[sequence]))
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


def _types(
    claim_types: Mapping[str, ClaimType],
) -> tuple[dict[str, ClaimType], frozenset[str]]:
    types = {item.predicate: item for item in claim_types.values()}
    live = frozenset(
        predicate for predicate, item in types.items() if item.lifecycle.state == "live"
    )
    return types, live


def _contracts_by_digest(
    contracts: Mapping[str, CaptureContract],
) -> dict[str, CaptureContract]:
    return {capture_contract_digest(contract).tagged: contract for contract in contracts.values()}


def _max_count_key(counts: Mapping[int, int]) -> int:
    return max((key for key, count in counts.items() if count), default=0)


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
    latest = history.latest_sequences(at_most=location.sequence)
    objects = _object_histories(instance, history, claims, claims, at_most=location.sequence)
    pointed: dict[str, set[str]] = {}
    for claim_file, revisions in objects.items():
        for _sequence, target in revisions:
            pointed.setdefault(target, set()).add(claim_file)
    source_latest: dict[int, int] = {}
    for path, sequence in latest.items():
        if path.startswith(_SOURCE_PREFIXES):
            source_latest[sequence] = source_latest.get(sequence, 0) + 1
    types, live = _types(claim_types)
    return FloorInputs(
        generation=location.sequence,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        latest=latest,
        subjects=subjects,
        claims=claims,
        claim_types=claim_types,
        by_subject=_group_by_subject(claims),
        objects=objects,
        documents=frozenset(documents),
        capture_contracts=contracts,
        pointed={target: frozenset(found) for target, found in pointed.items()},
        types=types,
        live_predicates=live,
        contracts_by_digest=_contracts_by_digest(contracts),
        source_latest=source_latest,
        sources_changed=_max_count_key(source_latest),
    )


class _Moves:
    """Membership changes to a key -> members index, applied once per key."""

    def __init__(self) -> None:
        self._added: dict[str, set[str]] = {}
        self._removed: dict[str, set[str]] = {}

    def move(self, key: str, member: str, *, present: bool) -> None:
        (self._added if present else self._removed).setdefault(key, set()).add(member)
        (self._removed if present else self._added).get(key, set()).discard(member)

    def keys(self) -> set[str]:
        return {*self._added, *self._removed}

    def apply(self, index: dict[str, frozenset[str]]) -> None:
        for key in self.keys():
            members = (set(index.get(key, ())) - self._removed.get(key, set())) | self._added.get(
                key, set()
            )
            if members:
                index[key] = frozenset(members)
            else:
                index.pop(key, None)


def _patch_floor_inputs(
    instance: PlaybillInstance,
    history: HistoryReader,
    inputs: FloorInputs,
    target: AcceptedGenerationLocation,
) -> tuple[FloorInputs, frozenset[str]]:
    """The inputs at ``target`` and every ledger path touched between the two."""

    if target.sequence == inputs.generation:
        return inputs, frozenset()
    low, high = sorted((inputs.generation, target.sequence))
    touched = history.member_paths_between(low, high)
    changed = sorted(touched, key=lambda item: item.encode())
    latest = dict(inputs.latest)
    at_target = history.latest_sequences(changed, at_most=target.sequence)
    source_latest = dict(inputs.source_latest)
    sources_changed = inputs.sources_changed
    source_removed = False
    for path in changed:
        was = latest.get(path)
        now = at_target.get(path)
        if now is None:
            latest.pop(path, None)
        else:
            latest[path] = now
        if was == now or not path.startswith(_SOURCE_PREFIXES):
            continue
        if was is not None:
            source_latest[was] -= 1
            if not source_latest[was]:
                del source_latest[was]
                source_removed = source_removed or was == sources_changed
        if now is not None:
            source_latest[now] = source_latest.get(now, 0) + 1
    candidate = max(
        (sequence for path, sequence in at_target.items() if path.startswith(_SOURCE_PREFIXES)),
        default=None,
    )
    if candidate is not None and candidate >= sources_changed:
        sources_changed = candidate
    elif source_removed:
        sources_changed = _max_count_key(source_latest)
    wanted = [path for path in changed if path.startswith(_INPUT_PREFIXES)]
    blobs = instance.blobs_at(target.git_oid, wanted) if wanted else {}
    subjects = dict(inputs.subjects)
    claims = dict(inputs.claims)
    claim_types = dict(inputs.claim_types)
    documents = set(inputs.documents)
    contracts = dict(inputs.capture_contracts)
    by_subject = dict(inputs.by_subject)
    subject_moves = _Moves()
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
            subject_moves.move(
                subject,
                path,
                present=new is not None and new.statement.subject.artifact_path == subject,
            )
    subject_moves.apply(by_subject)
    objects = dict(inputs.objects)
    pointed = dict(inputs.pointed)
    touched_claims = [path for path in wanted if path.startswith(CLAIMS_PREFIX)]
    before = {path: objects.pop(path, ()) for path in touched_claims}
    objects.update(
        _object_histories(
            instance,
            history,
            claims,
            touched_claims,
            at_most=target.sequence,
            known=before,
        )
    )
    pointer_moves = _Moves()
    for path in touched_claims:
        old_targets = {item for _sequence, item in before[path]}
        new_targets = {item for _sequence, item in objects.get(path, ())}
        for item in old_targets - new_targets:
            pointer_moves.move(item, path, present=False)
        for item in new_targets - old_targets:
            pointer_moves.move(item, path, present=True)
    pointer_moves.apply(pointed)
    types, live = inputs.types, inputs.live_predicates
    if any(path.startswith(CLAIM_TYPES_PREFIX) for path in wanted):
        types, live = _types(claim_types)
    contracts_by_digest = inputs.contracts_by_digest
    if any(path.startswith(CAPTURE_CONTRACTS_PREFIX) for path in wanted):
        contracts_by_digest = _contracts_by_digest(contracts)
    coordinate = instance.coordinate_for_oid(target.git_oid)
    return (
        FloorInputs(
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
            pointed=pointed,
            types=types,
            live_predicates=live,
            contracts_by_digest=contracts_by_digest,
            source_latest=source_latest,
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
        # A ClaimType change is a schema change: it restamps every Subject
        # showing the field (or its short-name shadow), so its Claims are found
        # by one pass rather than kept in a per-predicate index every ordinary
        # generation would have to copy.
        suffixes = tuple(f".{predicate}" for predicate in type_predicates)
        for inputs in sides:
            for claim in inputs.claims.values():
                predicate = claim.statement.predicate
                if predicate not in type_predicates and not predicate.endswith(suffixes):
                    continue
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
    return inputs.latest.get(claim_file)


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


def _fresh(
    renders: dict[str, tuple[bytes, int]],
    digests: dict[str, tuple[str, int]],
    written: set[str],
    path: str,
    content: bytes,
    changed: int,
) -> None:
    """Record one rendered file; its digest is taken here, once."""

    held = renders.get(path)
    if held is not None and held[0] is content and held[1] == changed:
        return
    renders[path] = (content, changed)
    digests[path] = (content_digest(content), len(content))
    written.add(path)


def _drop(
    renders: dict[str, tuple[bytes, int]],
    digests: dict[str, tuple[str, int]],
    removed: set[str],
    path: str,
) -> None:
    if renders.pop(path, None) is not None:
        digests.pop(path, None)
        removed.add(path)


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
    ``inputs``, only the files those paths reach are restamped; without it
    every Subject's stamp is checked against ``previous``.

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
    subjects: dict[str, _SubjectRender] = {} if previous is None else dict(previous.subjects)
    renders: dict[str, tuple[bytes, int]] = {} if previous is None else dict(previous.files)
    digests: dict[str, tuple[str, int]] = {} if previous is None else dict(previous.digests)
    written: set[str] = set()
    removed: set[str] = set()
    kind_moves = _Moves()
    # Kinds whose INDEX must be checked: a member was restamped, added or dropped.
    reached_kinds: set[str] = set()
    if changed is None or old is None:
        candidates: Collection[str] = {*inputs.subjects, *subjects}
    else:
        candidates = _affected_subjects(old, inputs, changed)
    for path in sorted(candidates, key=lambda item: item.encode()):
        kept = subjects.get(path)
        shell = inputs.subjects.get(path)
        if shell is None:
            if kept is None:
                continue
            del subjects[path]
            kind_moves.move(kept.part.kind, path, present=False)
            for file_path in kept.files:
                _drop(renders, digests, removed, file_path)
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
        render = _SubjectRender(changed_at, own, part, stamped(part, changed_at))
        subjects[path] = render
        reached_kinds.add(part.kind)
        if kept is None or kept.part.kind != part.kind:
            kind_moves.move(part.kind, path, present=True)
        if kept is not None:
            if kept.part.kind != part.kind:
                kind_moves.move(kept.part.kind, path, present=False)
            for file_path in kept.files:
                if file_path not in render.files:
                    _drop(renders, digests, removed, file_path)
        for file_path, content in render.files.items():
            _fresh(renders, digests, written, file_path, content, changed_at)
    kinds: dict[str, frozenset[str]] = {} if previous is None else dict(previous.kinds)
    kind_moves.apply(kinds)
    indexes: dict[str, tuple[int, bytes]] = {} if previous is None else dict(previous.indexes)
    for kind in sorted(reached_kinds | kind_moves.keys()):
        members = kinds.get(kind)
        if not members:
            if indexes.pop(kind, None) is not None:
                _drop(renders, digests, removed, index_path(kind))
            continue
        index_changed = max(subjects[member].own_changed_at for member in members)
        kept_index = indexes.get(kind)
        if kept_index is not None and kept_index[0] == index_changed:
            continue
        content = render_index(kind, [subjects[member].part for member in members], index_changed)
        indexes[kind] = (index_changed, content)
        _fresh(renders, digests, written, index_path(kind), content, index_changed)
    # The changes that introduced each current Claim revision of a Subject.
    held_before: Mapping[int, int] = {} if previous is None else previous.live_sequences
    if changed is None or old is None:
        live_sequences = _all_live_sequences(inputs)
        moved_sequences: set[int] = {*live_sequences, *held_before}
    else:
        live_sequences = dict(held_before)
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
                live_sequences[before] -= 1
                if not live_sequences[before]:
                    del live_sequences[before]
            if after is not None:
                moved_sequences.add(after)
                live_sequences[after] = live_sequences.get(after, 0) + 1
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
    noted: dict[int, ChangeNote] = {} if previous is None else dict(previous.change_notes)
    by_commit: dict[str, frozenset[int]] = (
        {} if previous is None else dict(previous.notes_by_commit)
    )
    moved = None if previous is None else notes_changed_between(instance, previous.notes, snapshot)
    if moved is None:
        stale = set(live_sequences)
    else:
        stale = {
            sequence
            for commit in moved
            for sequence in by_commit.get(commit, ())
            if sequence in live_sequences
        }
    refetch = sorted(stale | entered)
    commit_moves = _SequenceMoves()
    for sequence in left | set(refetch):
        note = noted.pop(sequence, None)
        if note is not None:
            for commit in note.commits:
                commit_moves.move(commit, sequence, present=False)
    fetched = change_notes(
        instance, [history.generation(sequence) for sequence in refetch], snapshot
    )
    changes: dict[int, bytes] = {} if previous is None else dict(previous.changes)
    for sequence in left:
        if changes.pop(sequence, None) is not None:
            _drop(renders, digests, removed, change_path(sequence))
    rerender: dict[int, tuple[str, ...]] = {}
    for sequence in refetch:
        note = fetched[sequence]
        noted[sequence] = note
        for commit in note.commits:
            commit_moves.move(commit, sequence, present=True)
        if (
            sequence in changes
            and previous is not None
            and previous.change_notes.get(sequence) == note
        ):
            continue
        rerender[sequence] = note.rationale
    commit_moves.apply(by_commit)
    for sequence, content in render_changes(instance, history, rerender).items():
        changes[sequence] = content
        _fresh(renders, digests, written, change_path(sequence), content, sequence)
    if previous is None:
        _fresh(renders, digests, written, README_PATH, _README_BYTES, 0)
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
        sources = (sources_changed, source_state.render(sources_changed))
        _fresh(renders, digests, written, SOURCES_LEDGER_PATH, sources[1], sources[0])
    steps: tuple[FloorStep, ...] = ()
    if step and previous is not None:
        steps = (
            *previous.steps[-(_STEPS_KEPT - 1) :],
            FloorStep(
                previous.generation,
                inputs.generation,
                frozenset(written),
                frozenset(removed),
            ),
        )
    return FloorRender(
        inputs,
        renderer,
        subjects,
        indexes,
        changes,
        renders,
        sources,
        notes=snapshot,
        change_notes=noted,
        digests=digests,
        kinds=kinds,
        live_sequences=live_sequences,
        notes_by_commit=by_commit,
        source_state=source_state,
        steps=steps,
    )


class _SequenceMoves:
    """Membership changes to a commit -> sequences index, applied once per commit."""

    def __init__(self) -> None:
        self._added: dict[str, set[int]] = {}
        self._removed: dict[str, set[int]] = {}

    def move(self, key: str, sequence: int, *, present: bool) -> None:
        (self._added if present else self._removed).setdefault(key, set()).add(sequence)
        (self._removed if present else self._added).get(key, set()).discard(sequence)

    def apply(self, index: dict[str, frozenset[int]]) -> None:
        for key in {*self._added, *self._removed}:
            members = (set(index.get(key, ())) - self._removed.get(key, set())) | self._added.get(
                key, set()
            )
            if members:
                index[key] = frozenset(members)
            else:
                index.pop(key, None)


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
