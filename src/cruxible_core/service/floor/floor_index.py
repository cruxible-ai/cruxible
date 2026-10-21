"""The floor index: the floor at one accepted generation, advanced from the ledger diff.

Every floor file is stamped with ``changed_at``, the latest accepted generation
that touched any of its inputs. For a Subject's ``current/`` file those are
the Subject itself, its Claims of any lifecycle, every Claim that ever pointed
at it (so a moved edge restamps its old target and its new one), and the
ClaimTypes (and the short-name shadows) of the fields it shows, for what a
Subject file shows of them: a ClaimType's cardinality and liveness, read as
the sequence that last changed them (``floor_definitions``), so a description
edit restamps no Subject. The stamp is read from the history index, so a
full render and an incremental one give the same bytes, and a file's bytes
differ between two generations exactly when its stamp does.

The index is rebuildable and instance-internal. It holds the parsed inputs
(Subjects, Claims, ClaimTypes, the latest sequence of every ledger path), the
reverse indexes a change is traced through (each Subject's live Claims and
live incoming edges, the live Claims of each predicate and short name, the
Subjects of each kind, the sequences of the live Claims, and each shown
change's rationale), the maxima the stamps read over Claims of every
lifecycle (each Subject's Claims, and every Claim that ever pointed at it),
the rendered files and each file's digest. ``advance_floor_index`` moves it
forward by reading only the paths the change records touched since, and
re-renders only the files those paths reach: a Subject is restamped only when
one of its inputs moved, a kind INDEX only when one of its Subjects did, a
change file only when it enters the floor (its rationale is the accepting
admission's, immutable, read once when it enters), and ``sources/LEDGER``
from the Claims and Documents that changed. A render at any other generation
is the same patch applied to a copy, in either direction, so any generation
of the accepted history can be rendered from it without binding that
generation's projection.

Each forward render records the files it wrote and removed, and the index
keeps the last few renders, so a delta from a recent base is the union of the
steps since it, against a base the index already rendered, with no file
hashed again: every file's digest is taken once, when it is rendered.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Collection, Iterable, Iterator, Mapping, Set
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
from cruxible_core.derived.persistent import EMPTY_PCOUNTS, EMPTY_PSET, PCounts, PMap, PSet
from cruxible_core.indexes.history.history_index import AcceptedGenerationLocation, HistoryReader
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.field_names import short_field_name
from cruxible_core.service.floor.floor_content import (
    FLOOR_README,
    change_path,
    change_rationales,
    render_changes,
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
from cruxible_core.service.floor.floor_definitions import (
    CURRENT_VIEW,
    DefinitionRevision,
    definition_histories,
    is_tracked,
    view_changed_at,
)
from cruxible_core.service.floor.floor_ontology import (
    ONTOLOGY_PREFIX,
    ContractNamer,
    OntologyIndex,
    OntologyState,
    all_files,
    build_ontology_index,
    ontology_reach,
    patch_ontology_index,
    reached_files,
    render_ontology,
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
    # Subject-valued Claim path -> its revisions to this generation, as a
    # shared chain: every Subject it ever pointed at, and at which sequence.
    objects: PMap[str, _Revisions] = field(default_factory=PMap)
    # Accepted Document paths, and CaptureContracts by path.
    documents: PSet[str] = EMPTY_PSET
    capture_contracts: PMap[str, CaptureContract] = field(default_factory=PMap)
    # Derived from the fields above and patched with them, so a render traces a
    # change instead of scanning. What a render shows comes from live Claims
    # only, so the indexes a change is traced through hold live Claims only:
    # Subject path -> its live Claims; Subject path -> the live Claims whose
    # object is it now; predicate -> its live Claims; short name -> the live
    # Claims whose field shows it (``<kind>.<short>`` on a Subject of
    # ``<kind>``). What a stamp reads of every lifecycle is kept as counts of
    # latest sequences, read for their maximum: Subject path -> its Claims',
    # and Subject path -> those of every Claim that ever pointed at it.
    # Then the ClaimTypes by predicate and the live predicates; the contracts
    # by digest (and each contract path's digest); and the latest sequence of
    # each source path (claims/, documents/, capture-contracts/), whose
    # maximum stamps sources/LEDGER.
    live_by_subject: PMap[str, PSet[str]] = field(default_factory=PMap)
    live_incoming: PMap[str, PSet[str]] = field(default_factory=PMap)
    by_predicate: PMap[str, PSet[str]] = field(default_factory=PMap)
    by_shadow: PMap[str, PSet[str]] = field(default_factory=PMap)
    claims_latest: PMap[str, PCounts] = field(default_factory=PMap)
    pointed_latest: PMap[str, PCounts] = field(default_factory=PMap)
    types: PMap[str, ClaimType] = field(default_factory=PMap)
    live_predicates: PSet[str] = EMPTY_PSET
    contracts_by_digest: PMap[str, CaptureContract] = field(default_factory=PMap)
    contract_digests: PMap[str, str] = field(default_factory=PMap)
    source_latest: PCounts = EMPTY_PCOUNTS
    # Definition path -> its revisions to this generation (``floor_definitions``):
    # what each revision showed, and when each view of it last changed.
    definitions: PMap[str, DefinitionRevision] = field(default_factory=PMap)
    # The reverse indexes the ontology files are traced through (``floor_ontology``).
    ontology: OntologyIndex = field(default_factory=OntologyIndex)

    @property
    def sources_changed(self) -> int:
        """The latest sequence of any source path: when sources/LEDGER last changed."""

        return self.source_latest.max(0)


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
    # Each shown change's rationale, read once when it entered the floor.
    rationales: PMap[int, tuple[str, ...]] = field(default_factory=PMap)
    # Each file's (content digest, byte length), taken once when it was rendered.
    digests: PMap[str, tuple[str, int]] = field(default_factory=PMap)
    # Kind -> its Subjects; live Claim sequence -> how many live Claims it
    # stamps (the changes the floor shows).
    kinds: PMap[str, PSet[str]] = field(default_factory=PMap)
    live_sequences: PMap[int, int] = field(default_factory=PMap)
    source_state: SourcesLedgerState | None = None
    # The forward renders that led here, oldest first.
    steps: tuple[FloorStep, ...] = ()

    @property
    def generation(self) -> int:
        return self.inputs.generation

    @cached_property
    def notes_digest(self) -> str:
        return floor_notes_digest(dict(self.rationales.items()))

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


class _Revisions:
    """One Subject-valued Claim's revisions to a generation, newest first, persistent.

    A node is one revision (its sequence and the Subject it pointed at) over
    the chain before it, which it shares: extending a Claim's history by one
    revision is one node, and cutting it back drops only the newer nodes.
    ``targets`` counts the revisions per Subject over the whole chain, kept
    with the same sharing, so every Subject the Claim ever pointed at is read
    without walking its history.
    """

    __slots__ = ("length", "older", "sequence", "target", "targets")

    sequence: int
    target: str
    older: _Revisions | None
    length: int
    targets: PMap[str, int]

    def __init__(self, sequence: int, target: str, older: _Revisions | None) -> None:
        self.sequence = sequence
        self.target = target
        self.older = older
        self.length = 1 if older is None else older.length + 1
        counts: PMap[str, int] = PMap() if older is None else older.targets
        self.targets = counts.set(target, counts.get(target, 0) + 1)

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[tuple[int, str]]:
        """Oldest first, as ``(sequence, target)``: the whole history (cold and tests only)."""

        newest_first: list[tuple[int, str]] = []
        node: _Revisions | None = self
        while node is not None:
            newest_first.append((node.sequence, node.target))
            node = node.older
        return reversed(newest_first)

    def extended(self, revisions: Iterable[tuple[int, str]]) -> _Revisions:
        """This chain with ``revisions`` (ascending, all newer) on top: one node each."""

        node = self
        for sequence, target in revisions:
            node = _Revisions(sequence, target, node)
        return node

    def at_most(self, sequence: int) -> _Revisions | None:
        """The shared chain of the revisions at or before ``sequence``: drops only newer nodes."""

        node: _Revisions | None = self
        while node is not None and node.sequence > sequence:
            node = node.older
        return node


def _revisions(revisions: Iterable[tuple[int, str]]) -> _Revisions | None:
    """A chain from ``revisions`` in ascending sequence order."""

    node: _Revisions | None = None
    for sequence, target in revisions:
        node = _Revisions(sequence, target, node)
    return node


def _target_moves(before: _Revisions | None, after: _Revisions | None) -> tuple[set[str], set[str]]:
    """The Subjects a Claim newly points at (ever), and those it no longer ever points at.

    Walks only the nodes the two chains do not share: the revisions added or
    dropped between them, never the history below their common node.
    """

    if before is None or after is None:
        # A Claim new or gone: every Subject it points at, read off the counts.
        return set(() if after is None else after.targets), set(
            () if before is None else before.targets
        )
    added: set[str] = set()
    removed: set[str] = set()
    old: _Revisions | None = before
    new: _Revisions | None = after
    while old is not new:
        if new is not None and (old is None or new.length >= old.length):
            if new.target not in before.targets:
                added.add(new.target)
            new = new.older
        else:
            assert old is not None
            if old.target not in after.targets:
                removed.add(old.target)
            old = old.older
    return added, removed


def _object_histories(
    instance: PlaybillInstance,
    history: HistoryReader,
    claims: Mapping[str, ClaimArtifactAny],
    paths: Iterable[str],
    *,
    at_most: int,
    known: Mapping[str, _Revisions] | None = None,
    since: int | None = None,
) -> dict[str, _Revisions]:
    """Every Subject each Subject-valued Claim pointed at, revision by revision.

    A Claim touched once is its current object. One revised in place is read
    back at each generation that touched it; a predicate keeps its object kind,
    so only Subject-valued Claims are read.

    ``known`` holds each Claim's history at generation ``since``: it is
    extended by only the sequences in ``(since, at_most]`` (or cut back to
    ``at_most``), sharing the known chain below them, and a revision it names,
    or the one at ``at_most`` itself (the current Claim), is not read again.
    A Claim it does not hold is read whole.
    """

    wanted = [path for path in paths if _object(claims.get(path)) is not None]
    result: dict[str, _Revisions] = {}
    # The revisions to put on top of each Claim's base chain, by sequence.
    added: dict[str, dict[int, str]] = {}
    base: dict[str, _Revisions | None] = {}
    revisited: dict[int, list[str]] = {}
    whole: list[str] = []
    extended: list[str] = []
    for path in wanted:
        prior = None if known is None or since is None else known.get(path)
        if prior is None or (prior.length == 1 and prior.sequence == 0):
            whole.append(path)
            continue
        assert since is not None
        if at_most < since:
            kept = prior.at_most(at_most)
            if kept is not None:
                result[path] = kept
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
        base[path] = None
        if len(touched) <= 1:
            added[path] = {touched[0] if touched else 0: current}
            continue
        added[path] = {}
        for sequence in touched:
            if sequence == at_most:
                added[path][sequence] = current
            else:
                revisited.setdefault(sequence, []).append(path)
    for path in extended:
        assert known is not None
        current = _object(claims[path])
        assert current is not None
        base[path] = known[path]
        added[path] = {}
        for sequence in newer.get(path, ()):
            if sequence == at_most:
                added[path][sequence] = current
            else:
                revisited.setdefault(sequence, []).append(path)
    for sequence in sorted(revisited):
        oid = history.generation(sequence).git_oid
        blobs = instance.blobs_at(oid, revisited[sequence])
        for path in revisited[sequence]:
            content = blobs.get(path)
            target = None if content is None else _object(parse_claim(content, path=path))
            if target is not None:
                added[path][sequence] = target
    for path, revisions in added.items():
        prior_chain = base[path]
        top = sorted(revisions.items())
        chain = _revisions(top) if prior_chain is None else prior_chain.extended(top)
        if chain is not None:
            result[path] = chain
    return result


def _predicate_of_type_path(path: str) -> str:
    namespace, _separator, name = path.removeprefix(CLAIM_TYPES_PREFIX).rpartition("/")
    return f"{namespace}.{name.removesuffix('.json')}"


def _shadow(claim: ClaimArtifactAny) -> str | None:
    """The short name a Claim's field shows besides its predicate, if any.

    A field ``<kind>.<short>`` of a Subject of ``<kind>`` (the kind is the
    Subject path's first segment) reads the ClaimType of ``<short>`` too.
    """

    kind = claim.statement.subject.artifact_path.removeprefix(SUBJECTS_PREFIX).partition("/")[0]
    predicate = claim.statement.predicate
    prefix = f"{kind}."
    if predicate.startswith(prefix) and len(predicate) > len(prefix):
        return predicate[len(prefix) :]
    return None


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


class _CountMoves:
    """Values to add to and remove from a key -> ``PCounts`` index, applied once per key."""

    def __init__(self) -> None:
        self._added: dict[str, list[int]] = {}
        self._removed: dict[str, list[int]] = {}

    def move(self, key: str, value: int, *, present: bool) -> None:
        (self._added if present else self._removed).setdefault(key, []).append(value)

    def apply(self, index: Any) -> None:
        """Apply to a ``PMapEvolver`` of key -> ``PCounts``: one path per value."""

        for key in {*self._added, *self._removed}:
            counts = index.get(key, EMPTY_PCOUNTS)
            for value in self._removed.get(key, ()):
                counts = counts.remove(value)
            for value in self._added.get(key, ()):
                counts = counts.add(value)
            if counts:
                index[key] = counts
            else:
                index.discard(key)


def _count(index: Any, key: Any, delta: int) -> None:
    """Add ``delta`` to one counter of a ``PMapEvolver``, dropping it at zero."""

    value = index.get(key, 0) + delta
    if value:
        index[key] = value
    else:
        index.discard(key)


# The definition kinds whose accepted bytes a cold build reads (receipts are Documents).
_DEFINITION_KINDS = (
    "claim-type",
    "capture-contract",
    "subject-kind",
    "procedure-mandate",
    "line",
    "trigger",
    "query-definition",
)
# CaptureContract digest -> the identity it is a version of: fixed once accepted.
_CONTRACT_NAMES: OrderedDict[str, str] = OrderedDict()
_CONTRACT_NAMES_CAPACITY = 4096


def _contract_namer(
    instance: PlaybillInstance,
    coordinate: AcceptedCoordinate,
    by_digest: Mapping[str, CaptureContract],
) -> ContractNamer:
    """Resolve a contract version digest to its identity: held versions first, then history."""

    def name(digest: str) -> str | None:
        held = by_digest.get(digest)
        if held is not None:
            return held.identity.name
        known = memo_get(_CONTRACT_NAMES, digest)
        if known is not None:
            return known
        try:
            found = instance.accepted_capture_contract_version(coordinate, digest)
        except CruxibleError:
            return None
        if found is None:
            return None
        memo_put(
            _CONTRACT_NAMES,
            digest,
            found.contract.identity.name,
            capacity=_CONTRACT_NAMES_CAPACITY,
        )
        return found.contract.identity.name

    return name


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
        document_paths = [row.path for row in projection.typed.envelopes(kind="document")]
        paths = (
            *(
                row.path
                for kind in ("subject", "claim", *_DEFINITION_KINDS)
                for row in projection.typed.envelopes(kind=kind)
            ),
            *(path for path in document_paths if is_tracked(path)),
        )
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
    live_by_subject: dict[str, set[str]] = {}
    live_incoming: dict[str, set[str]] = {}
    by_predicate: dict[str, set[str]] = {}
    by_shadow: dict[str, set[str]] = {}
    claims_latest: dict[str, list[int]] = {}
    for path, claim in claims.items():
        subject = claim.statement.subject.artifact_path
        claims_latest.setdefault(subject, []).append(latest.get(path, 0))
        if claim.lifecycle.state != "live":
            continue
        live_by_subject.setdefault(subject, set()).add(path)
        by_predicate.setdefault(claim.statement.predicate, set()).add(path)
        short = _shadow(claim)
        if short is not None:
            by_shadow.setdefault(short, set()).add(path)
        target = _object(claim)
        if target is not None:
            live_incoming.setdefault(target, set()).add(path)
    pointed_latest: dict[str, list[int]] = {}
    for claim_file, revisions in objects.items():
        for target in revisions.targets:
            pointed_latest.setdefault(target, []).append(latest.get(claim_file, 0))
    source_latest = PCounts(
        sequence for path, sequence in latest.items() if path.startswith(_SOURCE_PREFIXES)
    )
    types = {item.predicate: item for item in held["claim_type"].values()}
    contract_digests = {
        path: capture_contract_digest(contract).tagged
        for path, contract in held["contract"].items()
    }
    contracts_by_digest = PMap(
        (contract_digests[path], contract) for path, contract in held["contract"].items()
    )
    served = AcceptedCoordinate.from_internal(coordinate)
    tracked = sorted(
        {path for path in (*raw, *latest) if is_tracked(path)}, key=lambda item: item.encode()
    )
    held_blobs = {path: blob for path in tracked if (blob := raw.get(path)) is not None}
    histories = definition_histories(
        instance,
        history,
        tracked,
        at_most=location.sequence,
        target_blobs=held_blobs,
        reach=ontology_reach(_contract_namer(instance, served, contracts_by_digest)),
    )
    definitions = {path: chain for path, chain in histories.items() if chain is not None}
    return FloorInputs(
        generation=location.sequence,
        coordinate=served,
        latest=PMap(latest),
        subjects=PMap(held["subject"]),
        claims=PMap(claims),
        claim_types=PMap(held["claim_type"]),
        objects=PMap(objects),
        documents=PSet(held["document"]),
        capture_contracts=PMap(held["contract"]),
        live_by_subject=PMap((key, PSet(found)) for key, found in live_by_subject.items()),
        live_incoming=PMap((key, PSet(found)) for key, found in live_incoming.items()),
        by_predicate=PMap((key, PSet(found)) for key, found in by_predicate.items()),
        by_shadow=PMap((key, PSet(found)) for key, found in by_shadow.items()),
        claims_latest=PMap((key, PCounts(found)) for key, found in claims_latest.items()),
        pointed_latest=PMap((key, PCounts(found)) for key, found in pointed_latest.items()),
        types=PMap(types),
        live_predicates=PSet(
            predicate for predicate, item in types.items() if item.lifecycle.state == "live"
        ),
        contracts_by_digest=contracts_by_digest,
        contract_digests=PMap(contract_digests),
        source_latest=source_latest,
        definitions=PMap(definitions),
        ontology=build_ontology_index(definitions),
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
    source_latest = inputs.source_latest
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
            source_latest = source_latest.remove(was)
        if now is not None:
            source_latest = source_latest.add(now)
    latest_now = latest.persistent()
    wanted = [path for path in changed if path.startswith(_INPUT_PREFIXES) or is_tracked(path)]
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
    live_added: list[str] = []
    live_removed: list[str] = []
    subject_moves = _Moves()
    incoming_moves = _Moves()
    predicate_moves = _Moves()
    shadow_moves = _Moves()
    claims_latest_moves = _CountMoves()
    for path in wanted:
        content = blobs.get(path)
        value: Any
        kind, value = ("other", None) if content is None else _parse(path, content)
        if path.startswith(SUBJECTS_PREFIX):
            if kind == "subject":
                subjects[path] = value
            else:
                subjects.discard(path)
        elif path.startswith(CLAIMS_PREFIX):
            old = claims.get(path)
            new = value if kind == "claim" else None
            if new is None:
                claims.discard(path)
            else:
                claims[path] = new
            for item, present, at in (
                (old, False, inputs.latest.get(path, 0)),
                (new, True, latest_now.get(path, 0)),
            ):
                if item is None:
                    continue
                subject = item.statement.subject.artifact_path
                claims_latest_moves.move(subject, at, present=present)
                if item.lifecycle.state != "live":
                    continue
                subject_moves.move(subject, path, present=present)
                predicate_moves.move(item.statement.predicate, path, present=present)
                short = _shadow(item)
                if short is not None:
                    shadow_moves.move(short, path, present=present)
                edge_target = _object(item)
                if edge_target is not None:
                    incoming_moves.move(edge_target, path, present=present)
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
    live_by_subject = inputs.live_by_subject.evolver()
    subject_moves.apply(live_by_subject)
    live_incoming = inputs.live_incoming.evolver()
    incoming_moves.apply(live_incoming)
    claims_latest = inputs.claims_latest.evolver()
    claims_latest_moves.apply(claims_latest)
    by_predicate = inputs.by_predicate.evolver()
    predicate_moves.apply(by_predicate)
    by_shadow = inputs.by_shadow.evolver()
    shadow_moves.apply(by_shadow)
    claims_now = claims.persistent()
    touched_claims = [path for path in wanted if path.startswith(CLAIMS_PREFIX)]
    objects = inputs.objects.evolver()
    before = {path: inputs.objects.get(path) for path in touched_claims}
    histories = _object_histories(
        instance,
        history,
        claims_now,
        touched_claims,
        at_most=target.sequence,
        known=before,
        since=inputs.generation,
    )
    pointer_moves = _CountMoves()
    for path in touched_claims:
        found = histories.get(path)
        if found is None:
            objects.discard(path)
        else:
            objects[path] = found
        was, now = inputs.latest.get(path, 0), latest_now.get(path, 0)
        if was == now:
            gained, lost = _target_moves(before[path], found)
        else:
            # Its latest sequence moved, so every Subject it ever pointed at
            # restamps with it: those are the affected output.
            lost = set(() if before[path] is None else before[path].targets)
            gained = set(() if found is None else found.targets)
        for item in lost:
            pointer_moves.move(item, was, present=False)
        for item in gained:
            pointer_moves.move(item, now, present=True)
    pointed_latest = inputs.pointed_latest.evolver()
    pointer_moves.apply(pointed_latest)
    coordinate = instance.coordinate_for_oid(target.git_oid)
    served = AcceptedCoordinate.from_internal(coordinate)
    contracts_now = contracts_by_digest.persistent()
    tracked = [path for path in changed if is_tracked(path)]
    definitions = inputs.definitions.evolver()
    moves = []
    for path, chain in definition_histories(
        instance,
        history,
        tracked,
        at_most=target.sequence,
        target_blobs=blobs,
        reach=ontology_reach(_contract_namer(instance, served, contracts_now)),
        known=inputs.definitions,
        since=inputs.generation,
    ).items():
        moves.append((path, inputs.definitions.get(path), chain))
        if chain is None:
            definitions.discard(path)
        else:
            definitions[path] = chain
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
    return (
        FloorInputs(
            generation=target.sequence,
            coordinate=served,
            latest=latest_now,
            subjects=subjects.persistent(),
            claims=claims_now,
            claim_types=claim_types.persistent(),
            objects=objects.persistent(),
            documents=inputs.documents.update(documents_added, documents_removed),
            capture_contracts=contracts.persistent(),
            live_by_subject=live_by_subject.persistent(),
            live_incoming=live_incoming.persistent(),
            by_predicate=by_predicate.persistent(),
            by_shadow=by_shadow.persistent(),
            claims_latest=claims_latest.persistent(),
            pointed_latest=pointed_latest.persistent(),
            types=types_now,
            live_predicates=live,
            contracts_by_digest=contracts_now,
            contract_digests=contract_digests.persistent(),
            source_latest=source_latest,
            definitions=definitions.persistent(),
            ontology=patch_ontology_index(inputs.ontology, moves) if moves else inputs.ontology,
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

    def _shown(self, type_path: str | None) -> int:
        """When a ClaimType last changed what a Subject file shows of it."""

        if type_path is None:
            return 0
        return view_changed_at(self._inputs.definitions.get(type_path), CURRENT_VIEW)

    def name(self, predicate: str, kind: str) -> int:
        """When the field ``predicate`` of ``kind`` last changed its shown type or short name.

        A field reads its ClaimType's cardinality and its short name, which
        drops the ``kind.`` prefix unless the short string is itself a live
        predicate, so it moves with that one ClaimType's liveness too. A
        revision that changes neither (a description edit) moves no field.
        """

        key = (predicate, kind)
        known = self._names.get(key)
        if known is not None:
            return known
        best = self._shown(_type_path(predicate))
        prefix = f"{kind}."
        if predicate.startswith(prefix):
            best = max(best, self._shown(_type_path(predicate[len(prefix) :])))
        self._names[key] = best
        return best

    def incoming(self, path: str, edges: Iterable[ClaimArtifactAny]) -> int:
        """When the edges pointing at a Subject last changed.

        Every Claim that ever pointed at it counts, read as the maximum of
        their maintained latest sequences, so an edge moved away still
        restamps its old target; ``edges`` are the live ones shown.
        """

        counts = self._inputs.pointed_latest.get(path)
        best = 0 if counts is None else counts.max(0)
        for claim in edges:
            source = self._inputs.subjects.get(claim.statement.subject.artifact_path)
            if source is not None:
                best = max(best, self.name(claim.statement.predicate, source.subject_kind))
        return best

    def subject(self, path: str, live: Iterable[ClaimArtifactAny]) -> int:
        """Its own path, its Claims of any lifecycle (one maintained maximum), its fields' names."""

        shell = self._inputs.subjects[path]
        best = self._latest(path)
        counts = self._inputs.claims_latest.get(path)
        if counts is not None:
            best = max(best, counts.max(0))
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
    pointed at it, the live edges' source Subjects and the ClaimTypes (and
    short-name shadows) of the fields it and its live edges show. A Subject
    outside this set reads none of the changed paths, so its stamp and bytes
    hold. A changed Subject reaches the targets of its live edges only (a
    retired Claim shows no edge), and a changed ClaimType the live Claims of
    its predicate and shadow only, and only when it changed what they show
    (its cardinality or liveness): the work is what the change can render.
    """

    affected: set[str] = set()
    type_predicates: set[str] = set()
    sides = (old, new)
    for path in changed:
        if path.startswith(SUBJECTS_PREFIX):
            affected.add(path)
            for inputs in sides:
                for claim_file in inputs.live_by_subject.get(path, ()):
                    target = _object(inputs.claims.get(claim_file))
                    if target is not None:
                        affected.add(target)
        elif path.startswith(CLAIMS_PREFIX):
            for inputs in sides:
                claim = inputs.claims.get(path)
                if claim is not None:
                    affected.add(claim.statement.subject.artifact_path)
                revisions = inputs.objects.get(path)
                if revisions is not None:
                    # Every Subject it ever pointed at stamps with it: affected output.
                    affected.update(revisions.targets)
        elif path.startswith(CLAIM_TYPES_PREFIX):
            if view_changed_at(old.definitions.get(path), CURRENT_VIEW) == view_changed_at(
                new.definitions.get(path), CURRENT_VIEW
            ):
                # The revisions between show a Subject nothing new (a description
                # edit): no field's stamp or bytes read them.
                continue
            type_predicates.add(_predicate_of_type_path(path))
            for inputs in sides:
                claim_type = inputs.claim_types.get(path)
                if claim_type is not None:
                    type_predicates.add(claim_type.predicate)
    if type_predicates:
        # A field's stamp reads its ClaimType and its short-name shadow: the
        # Claims of the predicate itself, and those whose field shows it as
        # its short name, each from its own index.
        for inputs in sides:
            for predicate in type_predicates:
                for index in (inputs.by_predicate, inputs.by_shadow):
                    for claim_file in index.get(predicate, ()):
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
        for claim_file in inputs.live_by_subject.get(path, ()):
            sequence = _live_sequence(inputs, claim_file)
            if sequence is not None:
                counts[sequence] = counts.get(sequence, 0) + 1
    return counts


# -- rendering ------------------------------------------------------------------


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

    A change's rationale is its accepting admission's (``change_rationales``),
    immutable, so it is read once, when the change enters the floor; a kept
    change keeps it, and a warm render equals a cold one.

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
            (inputs.claims[claim_file] for claim_file in inputs.live_by_subject.get(path, ())),
            key=lambda item: item.identity.name.encode(),
        )
        edges = [
            edge
            for claim_file in inputs.live_incoming.get(path, ())
            if (edge := inputs.claims[claim_file]).statement.subject.artifact_path
            in inputs.subjects
        ]
        own = stamps.subject(path, live)
        changed_at = max(own, stamps.incoming(path, edges))
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
    # The ontology files: only those a changed definition (or its annotators) reached.
    ontology = OntologyState(
        inputs.definitions,
        inputs.ontology,
        _contract_namer(instance, inputs.coordinate, inputs.contracts_by_digest),
    )
    if changed is None or old is None:
        ontology_files: Collection[str] = {
            *all_files(ontology),
            *(
                path
                for path in (() if previous is None else previous.files)
                if path.startswith(ONTOLOGY_PREFIX)
            ),
        }
    else:
        touched_definitions = [path for path in changed if is_tracked(path)]
        ontology_files = (
            reached_files(
                OntologyState(
                    old.definitions,
                    old.ontology,
                    _contract_namer(instance, old.coordinate, old.contracts_by_digest),
                ),
                ontology,
                touched_definitions,
            )
            if touched_definitions
            else ()
        )
    if ontology_files:
        render_ontology(
            ontology, files.files.get, ontology_files, fresh=files.fresh, drop=files.drop
        )
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
                # Only a live Claim has a live sequence: a retired one is not reached.
                reached.update(old.live_by_subject.get(path, ()))
                reached.update(inputs.live_by_subject.get(path, ()))
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
    kept_rationales: PMap[int, tuple[str, ...]] = (
        PMap() if previous is None else previous.rationales
    )
    rationales = kept_rationales.evolver()
    changes = (PMap() if previous is None else previous.changes).evolver()
    for sequence in left:
        rationales.discard(sequence)
        if sequence in changes:
            changes.discard(sequence)
            files.drop(change_path(sequence))
    rerender: dict[int, tuple[str, ...]] = {}
    fetched = change_rationales(
        instance, [history.generation(sequence) for sequence in sorted(entered)]
    )
    for sequence, rationale in fetched.items():
        rationales[sequence] = rationale
        if sequence in changes and kept_rationales.get(sequence) == rationale:
            continue
        rerender[sequence] = rationale
    for sequence, content in render_changes(instance, history, rerender).items():
        changes[sequence] = content
        files.fresh(change_path(sequence), content, sequence)
    if previous is None:
        files.fresh(README_PATH, _README_BYTES, 0)
    sources_changed = inputs.sources_changed
    sources = None if previous is None else previous.sources
    source_state = None if previous is None else previous.source_state
    if sources is None or source_state is None or sources[0] != sources_changed:
        if changed is None or source_state is None or old is None:
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
                # A contract change relabels only the Claims whose Captures
                # were taken under its digest, before or after.
                changed_contracts=[
                    digest
                    for path in changed
                    if path.startswith(CAPTURE_CONTRACTS_PREFIX)
                    for digest in (
                        old.contract_digests.get(path),
                        inputs.contract_digests.get(path),
                    )
                    if digest is not None
                ],
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
        rationales=rationales.persistent(),
        digests=files.digests.persistent(),
        kinds=kinds_now,
        live_sequences=live_sequences,
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
) -> FloorRender:
    coordinate = instance.coordinate_for_oid(location.git_oid)
    renderer = floor_renderer(coordinate.compiler.rule_digest)
    if kept is None or kept.renderer != renderer:
        inputs = build_floor_inputs(instance, coordinate, history, location)
        return render_floor(instance, history, inputs)
    inputs, changed = _patch_floor_inputs(instance, history, kept.inputs, location)
    return render_floor(
        instance,
        history,
        inputs,
        previous=kept,
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
        with instance.accepted_history_reader() as history:
            location = _location(history, target)
            index = _kept_index(instance)
            kept = _usable(history, None if index is None else index.head)
            if kept is not None and kept.generation == location.sequence:
                return kept
            render = _render_at(instance, history, location, kept)
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

    Equal means: the same renderer and history (every change's rationale is
    fixed once accepted), so it is the floor ``floor_render_from(head,
    generation)`` would render, without patching back to it.
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
            return candidate
    return None


def floor_render_from(
    instance: PlaybillInstance,
    render: FloorRender,
    generation: int,
) -> FloorRender:
    """The floor at another generation of the same history, patched from ``render``.

    The two floors differ only by what accepted history changed between them.
    """

    with instance.accepted_history_reader() as history:
        location = history.generation(generation)
        coordinate = instance.coordinate_for_oid(location.git_oid)
        if floor_renderer(coordinate.compiler.rule_digest) != render.renderer:
            raise ProjectionIntegrityError("floor renderer differs between the two generations")
        inputs, changed = _patch_floor_inputs(instance, history, render.inputs, location)
        return render_floor(instance, history, inputs, previous=render, changed=changed)


__all__ = [
    "CLAIM_TYPES_PREFIX",
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
