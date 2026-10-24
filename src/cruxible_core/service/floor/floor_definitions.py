"""Every accepted revision of each definition path the floor reads, as a persistent chain.

A floor file that shows a definition is stamped from that definition's
history, not from the instance. The stamp a ``current/`` field reads from its
ClaimType (and its short-name shadow) is the last sequence at which that
ClaimType changed what a Subject file shows of it: its cardinality and whether
it is live. A revision that changes neither restamps no Subject through it.
(A revision of a ClaimType that has live Claims also revises each of them --
they pin it exactly -- and those Subjects restamp through their own Claims.)

One chain per definition path holds its revisions to a generation, newest
first, each over the chain before it, which it shares: extending a path's
history by one revision is one node, and cutting it back drops only the newer
nodes. The families: ClaimTypes, SubjectKinds, CaptureContracts, Procedure
Mandates, Lines, Triggers, QueryDefinitions and kit receipts (the Documents
``documents/kit-<id>.json``). Each node carries the parsed value, the targets
that revision reached (``floor_ontology.ontology_reach``), and for each
declared view (a projection of the value some floor file shows) the sequence
at which that view last changed, so reading when a view last moved is O(1)
and a pure function of the generation. A cold build reads each definition
path's revisions once (``member_sequences`` plus one blob read per touched
sequence): O(definition revisions), never O(Claims). An advance reads only
the revisions the change added.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from cruxible_client.contracts.builtin_claim_types import concrete_subject_kinds
from cruxible_client.contracts.captures import parse_capture_contract
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import (
    ClaimType,
    effective_evidence_requirement,
    parse_claim_type,
)
from cruxible_client.contracts.documents import parse_document
from cruxible_client.contracts.errors import CruxibleError, ProjectionIntegrityError
from cruxible_client.contracts.kits import KIT_RECEIPT_DOCUMENT_KIND, KitReceipt
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionIdentityRule
from cruxible_client.contracts.procedure_mandates import parse_procedure_mandate_any
from cruxible_client.contracts.procedures.line_specs import parse_line_spec
from cruxible_client.contracts.query.definitions import parse_query_definition
from cruxible_client.contracts.subject_kinds import SUBJECT_KIND_PREFIX, parse_subject_kind
from cruxible_client.contracts.triggers import parse_trigger
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.derived.persistent import PMap
from cruxible_core.indexes.history.history_index import HistoryReader
from cruxible_core.runtime.instance import PlaybillInstance

CLAIM_TYPES_PREFIX = "claim-types/"
SUBJECT_KINDS_PREFIX = SUBJECT_KIND_PREFIX
CAPTURE_CONTRACTS_PREFIX = "capture-contracts/"
MANDATES_PREFIX = "procedure-mandates/"
LINES_PREFIX = "lines/"
TRIGGERS_PREFIX = "triggers/"
QUERIES_PREFIX = "query-definitions/"
#: Kit receipts are Documents named ``kit-<kit_id>``.
RECEIPTS_PREFIX = "documents/kit-"

#: The definition families whose histories the floor keeps.
TRACKED_PREFIXES: tuple[str, ...] = (
    CLAIM_TYPES_PREFIX,
    SUBJECT_KINDS_PREFIX,
    CAPTURE_CONTRACTS_PREFIX,
    MANDATES_PREFIX,
    LINES_PREFIX,
    TRIGGERS_PREFIX,
    QUERIES_PREFIX,
    RECEIPTS_PREFIX,
)

#: View indexes of a ClaimType node. ``CURRENT_VIEW``: what ``current/``
#: files show of it (cardinality, liveness). ``KINDS_VIEW``: the kinds it
#: names while live (``ontology/INDEX``). ``CONTRACTS_VIEW``: the contracts it
#: admits while live, and whether it requires a Capture (``ontology/contracts/``).
CURRENT_VIEW = 0
KINDS_VIEW = 1
CONTRACTS_VIEW = 2

_PARSERS: dict[str, Callable[..., Any]] = {
    CLAIM_TYPES_PREFIX: parse_claim_type,
    SUBJECT_KINDS_PREFIX: parse_subject_kind,
    CAPTURE_CONTRACTS_PREFIX: parse_capture_contract,
    MANDATES_PREFIX: parse_procedure_mandate_any,
    LINES_PREFIX: parse_line_spec,
    TRIGGERS_PREFIX: parse_trigger,
    QUERIES_PREFIX: parse_query_definition,
}
_RECEIPT_ACCESS = BodyAccessContext(principal_id="kit-receipt", can_read_body=True)
#: Receipts by body digest: immutable bytes, so read and parsed once per process.
_RECEIPTS: OrderedDict[str, KitReceipt] = OrderedDict()
_RECEIPTS_CAPACITY = 1024


def is_tracked(path: str) -> bool:
    """Whether ``path`` is a definition path the floor keeps a history of."""

    return path.endswith(".json") and path.startswith(TRACKED_PREFIXES)


def definition_live(value: Any) -> bool:
    """Whether a definition is live: live lifecycle, or a receipt that owns definitions."""

    if isinstance(value, KitReceipt):
        return bool(value.artifacts)
    lifecycle = getattr(value, "lifecycle", None)
    return getattr(lifecycle, "state", None) == "live"


def _receipt(instance: PlaybillInstance, path: str, content: bytes) -> KitReceipt | None:
    """The kit receipt a ``documents/kit-*`` path holds, or None for an ordinary Document.

    Accepted Document bodies are retained (no code path erases a body store
    object), so a superseded receipt's body is read back like the current
    one's; one that is nevertheless lost refuses the render as an integrity
    failure, never a floor published differently for the same history.
    """

    try:
        shell = parse_document(content, path=path)
    except CruxibleError:
        return None
    if shell.document_kind != KIT_RECEIPT_DOCUMENT_KIND:
        return None
    found = memo_get(_RECEIPTS, shell.body_digest)
    if isinstance(found, KitReceipt):
        return found
    try:
        body = instance.body_store().read(shell.body_digest, access=_RECEIPT_ACCESS)
        receipt = KitReceipt.model_validate_json(body)
    except (CruxibleError, OSError, ValueError) as exc:
        raise ProjectionIntegrityError(
            f"floor cannot render {path}: its accepted kit receipt body "
            f"{shell.body_digest} is not retained or does not parse"
        ) from exc
    memo_put(_RECEIPTS, shell.body_digest, receipt, capacity=_RECEIPTS_CAPACITY)
    return receipt


def parse_definition(instance: PlaybillInstance, path: str, content: bytes | None) -> Any:
    """The parsed definition at ``path``, or None when absent or not that family's artifact."""

    if content is None:
        return None
    if path.startswith(RECEIPTS_PREFIX):
        return _receipt(instance, path, content)
    for prefix, parse in _PARSERS.items():
        if path.startswith(prefix):
            try:
                return parse(content, path=path)
            except CruxibleError:
                return None
    return None


def _contract_names(claim_type: ClaimType) -> tuple[str, ...]:
    """What a ClaimType's evidence rules name: contract identities, or version digests."""

    names: set[str] = set()
    for rule in claim_type.evidence_admission_policy.rules:
        if isinstance(rule, ClaimEvidenceAdmissionIdentityRule):
            names.update(item.target.name for item in rule.capture_contracts)
        else:
            names.update(rule.capture_contract_digests)
    return tuple(sorted(names))


def _claim_type_views(value: Any) -> tuple[Any, ...]:
    claim_type = value if isinstance(value, ClaimType) else None
    if claim_type is None:
        return (None, None, None)
    live = claim_type.lifecycle.state == "live"
    return (
        (claim_type.cardinality, live),
        concrete_subject_kinds(claim_type.allowed_subject_kinds) if live else None,
        (
            (_contract_names(claim_type), effective_evidence_requirement(claim_type) == "captured")
            if live
            else None
        ),
    )


def definition_views(path: str, value: Any) -> tuple[Any, ...]:
    """The views a definition's revision shows, in view-index order."""

    if path.startswith(CLAIM_TYPES_PREFIX):
        return _claim_type_views(value)
    return ()


Reach = Callable[[str, Any], frozenset[str]]


def _no_reach(_path: str, _value: Any) -> frozenset[str]:
    return frozenset()


class DefinitionRevision:
    """One definition path's revisions to a generation, newest first, persistent.

    A node is one revision (its sequence, its bytes and parsed value, None when
    the path is absent there) over the chain before it, which it shares.
    ``reach`` counts, over the whole chain, the targets each revision reached
    (floor files, or other definition paths), so every target the path ever
    reached is read without walking its history. ``view_at`` holds, per view,
    the sequence at which that view last changed.
    """

    __slots__ = (
        "first",
        "length",
        "older",
        "raw",
        "reach",
        "sequence",
        "targets",
        "value",
        "view_at",
        "views",
    )

    sequence: int
    raw: bytes | None
    value: Any
    older: DefinitionRevision | None
    length: int
    first: int
    targets: frozenset[str]
    reach: PMap[str, int]
    views: tuple[Any, ...]
    view_at: tuple[int, ...]

    def __init__(
        self,
        sequence: int,
        raw: bytes | None,
        value: Any,
        older: DefinitionRevision | None,
        *,
        reach: frozenset[str],
        views: tuple[Any, ...],
    ) -> None:
        self.sequence = sequence
        self.raw = raw
        self.value = value
        self.older = older
        self.length = 1 if older is None else older.length + 1
        self.first = sequence if older is None else older.first
        self.targets = reach
        counts: PMap[str, int] = PMap() if older is None else older.reach
        for target in reach:
            counts = counts.set(target, counts.get(target, 0) + 1)
        self.reach = counts
        self.views = views
        if older is None or len(older.views) != len(views):
            self.view_at = tuple(sequence for _view in views)
        else:
            self.view_at = tuple(
                sequence if view != before else at
                for view, before, at in zip(views, older.views, older.view_at, strict=True)
            )

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[tuple[int, Any]]:
        """Oldest first, as ``(sequence, value)``: the whole history (tests only)."""

        newest_first: list[tuple[int, Any]] = []
        node: DefinitionRevision | None = self
        while node is not None:
            newest_first.append((node.sequence, node.value))
            node = node.older
        return reversed(newest_first)

    @property
    def present(self) -> bool:
        return self.value is not None

    def at_most(self, sequence: int) -> DefinitionRevision | None:
        """The shared chain of the revisions at or before ``sequence``: drops only newer nodes."""

        node: DefinitionRevision | None = self
        while node is not None and node.sequence > sequence:
            node = node.older
        return node


def view_changed_at(chain: DefinitionRevision | None, view: int) -> int:
    """When ``view`` of a definition last changed at the chain's generation; 0 if never."""

    return 0 if chain is None else chain.view_at[view]


def reach_moves(
    before: DefinitionRevision | None, after: DefinitionRevision | None
) -> tuple[set[str], set[str]]:
    """The targets a path newly ever reaches, and those it no longer ever reaches.

    Walks only the nodes the two chains do not share.
    """

    if before is None or after is None:
        return set(() if after is None else after.reach), set(
            () if before is None else before.reach
        )
    added: set[str] = set()
    removed: set[str] = set()
    old: DefinitionRevision | None = before
    new: DefinitionRevision | None = after
    while old is not new:
        if new is not None and (old is None or new.length >= old.length):
            node = new
            new = new.older
            for target in node.targets:
                if target not in before.reach:
                    added.add(target)
        else:
            assert old is not None
            node = old
            old = old.older
            for target in node.targets:
                if target not in after.reach:
                    removed.add(target)
    return added, removed


def definition_histories(
    instance: PlaybillInstance,
    history: HistoryReader,
    paths: Sequence[str],
    *,
    at_most: int,
    target_blobs: Mapping[str, bytes],
    reach: Reach = _no_reach,
    known: Mapping[str, DefinitionRevision] | None = None,
    since: int | None = None,
) -> dict[str, DefinitionRevision | None]:
    """Each definition path's history at ``at_most``.

    ``target_blobs`` holds every path's bytes at ``at_most`` (a path it lacks
    is absent there). ``known`` holds each path's chain at generation
    ``since``: it is extended by only the sequences in ``(since, at_most]``
    (or cut back to ``at_most``), sharing the known chain below them. A path
    it does not hold is read whole, from genesis on.
    """

    result: dict[str, DefinitionRevision | None] = {}
    whole: list[str] = []
    extended: list[str] = []
    for path in paths:
        prior = None if known is None or since is None else known.get(path)
        if prior is None:
            whole.append(path)
        elif since is not None and at_most < since:
            result[path] = prior.at_most(at_most)
        else:
            extended.append(path)
    sequences: dict[str, tuple[int, ...]] = {}
    if whole:
        sequences.update(
            (path, tuple(item for item in found if 0 < item <= at_most))
            for path, found in history.member_sequences(whole).items()
        )
    if extended:
        assert since is not None
        sequences.update(history.member_sequences_between(extended, since, at_most))
    # Every revision to read, by the sequence it is read at; genesis carries no
    # change record, so a whole path's genesis bytes are read there too.
    wanted: dict[int, list[str]] = {}
    for path in whole:
        wanted.setdefault(0, []).append(path)
    for path, found in sequences.items():
        for sequence in found:
            if sequence != at_most:
                wanted.setdefault(sequence, []).append(path)
    contents: dict[tuple[str, int], bytes | None] = {}
    for sequence in sorted(wanted):
        if sequence == at_most:
            continue
        blobs = instance.blobs_at(history.generation(sequence).git_oid, wanted[sequence])
        for path in wanted[sequence]:
            contents[(path, sequence)] = blobs.get(path)

    def node(
        older: DefinitionRevision | None, path: str, sequence: int, raw: bytes | None
    ) -> DefinitionRevision:
        value = parse_definition(instance, path, raw)
        return DefinitionRevision(
            sequence,
            raw,
            value,
            older,
            reach=reach(path, value) if value is not None else frozenset(),
            views=definition_views(path, value),
        )

    for path in whole:
        chain: DefinitionRevision | None = None
        touched = sequences.get(path, ())
        genesis = target_blobs.get(path) if at_most == 0 else contents.get((path, 0))
        if genesis is not None:
            chain = node(None, path, 0, genesis)
        for sequence in touched:
            raw = target_blobs.get(path) if sequence == at_most else contents.get((path, sequence))
            chain = node(chain, path, sequence, raw)
        result[path] = chain
    for path in extended:
        assert known is not None
        chain = known[path]
        for sequence in sequences.get(path, ()):
            raw = target_blobs.get(path) if sequence == at_most else contents.get((path, sequence))
            chain = node(chain, path, sequence, raw)
        result[path] = chain
    return result


__all__ = [
    "CAPTURE_CONTRACTS_PREFIX",
    "CLAIM_TYPES_PREFIX",
    "CONTRACTS_VIEW",
    "CURRENT_VIEW",
    "KINDS_VIEW",
    "LINES_PREFIX",
    "MANDATES_PREFIX",
    "QUERIES_PREFIX",
    "RECEIPTS_PREFIX",
    "SUBJECT_KINDS_PREFIX",
    "TRACKED_PREFIXES",
    "TRIGGERS_PREFIX",
    "definition_live",
    "DefinitionRevision",
    "definition_histories",
    "definition_views",
    "is_tracked",
    "parse_definition",
    "reach_moves",
    "view_changed_at",
]
