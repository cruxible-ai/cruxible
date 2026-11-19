"""Who may read a Capture's bytes: its exact CaptureKind version's ``body_access`` (C14).

Authorization reads maintained metadata only, never an envelope or a body:

1. Capture digest -> the exact kind version it was taken under: the envelope
   index beside the body store, recorded from the object's own leading bytes
   when it was stored (one batched query per request);
2. kind version -> access: the accepted ``capture_contracts`` row of a current
   version (one batched query), else the exact superseded version from the
   accepted history index (once per version per process: a version's access
   never changes).

A derived Capture (D-R37 d6) inherits the most restrictive access of the
Captures it was computed from, transitively: the envelope index records each
derived envelope's inputs when it is stored, so this too is maintained metadata.
An open derived kind whose derivation is unrecorded is restricted (fail closed).

Every surface asks here, per Capture, before it reads an envelope or a body for
its caller. A Capture with no indexed kind (absent, unreadable, not an
envelope) or whose kind no accepted version resolves is **restricted**: the
check fails closed. A caller holding ``cruxible_body_read_restricted``
(``BodyAccessContext.can_read_restricted``) reads every kind.
"""

from __future__ import annotations

import dataclasses
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.captures import (
    CaptureBodyAccess,
    capture_body_access,
    capture_contract_is_self_asserted,
    provenance_rule_is_self_asserted,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.indexes.evidence.citation_coverage import CitationAccess
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.sql_batch import select_in
from cruxible_core.runtime.instance import PlaybillInstance

if TYPE_CHECKING:
    from cruxible_core.indexes.sqlite import ProjectionHandle


@dataclass(frozen=True)
class CaptureKindVersion:
    """One accepted CaptureKind version, by its exact digest."""

    identity: str
    path: str
    access: CaptureBodyAccess
    epistemic_grade: Literal["observed", "derived", "predicted"]
    #: Whether its provenance rule is proposer-supplied (coverage's trust grade).
    self_asserted: bool


@dataclass(frozen=True)
class CaptureKindAccess:
    """One Capture's kind version and access, resolved without reading it.

    ``contract_digest`` is None when the envelope index names no kind for the
    digest; ``kind`` is None when no accepted version resolves that digest.
    Either way the Capture is restricted.
    """

    capture_digest: str
    contract_digest: str | None
    kind: CaptureKindVersion | None
    #: A derived Capture computed from a restricted one, or whose derivation is
    #: unrecorded: restricted whatever its own kind declares (D-R37 d6).
    inherited_restricted: bool = False

    @property
    def restricted(self) -> bool:
        return self.kind is None or self.kind.access != "open" or self.inherited_restricted

    @property
    def kind_identity(self) -> str | None:
        return None if self.kind is None else self.kind.identity

    def withheld_from(self, access: BodyAccessContext) -> bool:
        return self.restricted and not access.can_read_restricted


#: Superseded kind versions resolved from accepted history, by (store, digest).
_HISTORICAL: dict[tuple[str, str], CaptureKindVersion] = {}
_HISTORICAL_LOCK = threading.Lock()
_HISTORICAL_MAX = 4096


@dataclass
class CaptureAccessResolver:
    """Per-request memo of Capture kinds at one accepted coordinate.

    ``projection`` is a handle the caller already holds bound at ``coordinate``;
    lookups then read through it instead of binding their own.
    """

    instance: PlaybillInstance
    coordinate: AcceptedProjectionCoordinate
    projection: ProjectionHandle | None = None
    _versions: dict[str, CaptureKindVersion | None] = field(default_factory=dict)
    _captures: dict[str, CaptureKindAccess] = field(default_factory=dict)

    def capture(self, digest: str) -> CaptureKindAccess:
        known = self._captures.get(digest)
        return known if known is not None else self.captures((digest,))[digest]

    def captures(self, digests: Iterable[str]) -> dict[str, CaptureKindAccess]:
        wanted = tuple(dict.fromkeys(digests))
        missing = [digest for digest in wanted if digest not in self._captures]
        if missing:
            unsettled = set(missing)
            try:
                self._resolve_own_kind(missing)
                self._inherit(missing, unsettled=unsettled)
            except BaseException:
                # Never leave a half-settled entry to be read as a final decision.
                for digest in unsettled:
                    self._captures.pop(digest, None)
                raise
        return {digest: self._captures[digest] for digest in wanted}

    def _resolve_own_kind(self, digests: Sequence[str]) -> None:
        """Record each digest's own kind access, never its inheritance.

        Never recurses into ``_inherit``: it only ever fills in base facts
        (``self._captures`` entries with ``inherited_restricted`` at its
        default), so a caller still assembling a reachable derivation graph
        can resolve an ancestor's own kind without publishing its restriction
        decision before the whole graph is in hand.
        """

        named = self.instance.body_store().capture_envelopes.contracts_of(digests)
        self.versions(set(named.values()))
        for digest in digests:
            contract = named.get(digest)
            self._captures[digest] = CaptureKindAccess(
                capture_digest=digest,
                contract_digest=contract,
                kind=None if contract is None else self._versions[contract],
            )

    def _inherit(self, digests: Iterable[str], *, unsettled: set[str]) -> None:
        """Restrict each open derived Capture whose inputs, transitively, are restricted.

        Resolves the COMPLETE reachable derivation graph for ``digests``
        before publishing any decision (D-R37 d6), then propagates restriction
        over it once:

        1. **Discover**, breadth-first by derivation depth: one batched
           ``derivation_inputs`` read and one batched own-kind read
           (``contracts_of``) per frontier, never per node. ``unsettled``
           holds the digests whose own kind this request resolved; only those
           are walked. An entry an earlier request settled is final.
        2. **Propagate** through a reverse-dependency work queue: every
           restricted node (own kind, unrecorded derivation, or settled
           earlier) restricts its dependents, each of which joins the queue
           once. Every edge is processed once; no node is revisited.

        Missing derivation metadata for any node fails closed, and that
        failure propagates to everything depending on it.
        """

        def open_derived(digest: str) -> bool:
            item = self._captures[digest]
            return (
                item.kind is not None
                and item.kind.epistemic_grade == "derived"
                and not item.restricted
            )

        frontier = [digest for digest in dict.fromkeys(digests) if open_derived(digest)]
        if not frontier:
            return
        envelopes = self.instance.body_store().capture_envelopes
        graph: dict[str, tuple[str, ...] | None] = {}
        queued = set(frontier)
        while frontier:
            recorded = envelopes.derivation_inputs(frontier)
            unknown: dict[str, None] = {}
            for digest in frontier:
                sources = recorded.get(digest)
                graph[digest] = sources
                for source in sources or ():
                    if source not in self._captures:
                        unknown[source] = None
            if unknown:
                # Registered before resolving: an interruption partway through
                # the batch must not leave an inserted ancestor cached as open.
                unsettled.update(unknown)
                self._resolve_own_kind(tuple(unknown))
            next_frontier: list[str] = []
            for digest in frontier:
                for source in graph[digest] or ():
                    if source not in queued and source in unsettled and open_derived(source):
                        queued.add(source)
                        next_frontier.append(source)
            frontier = next_frontier

        dependents: dict[str, list[str]] = {}
        restricted: list[str] = []
        for digest, sources in graph.items():
            if sources is None:
                self._restrict(digest)
                restricted.append(digest)
                continue
            for source in sources:
                dependents.setdefault(source, []).append(digest)
        restricted.extend(
            source
            for source in dependents
            if source not in graph and self._captures[source].restricted
        )
        while restricted:
            for digest in dependents.get(restricted.pop(), ()):
                if not self._captures[digest].inherited_restricted:
                    self._restrict(digest)
                    restricted.append(digest)

    def _restrict(self, digest: str) -> None:
        self._captures[digest] = dataclasses.replace(
            self._captures[digest], inherited_restricted=True
        )

    def versions(self, contract_digests: Iterable[str]) -> Mapping[str, CaptureKindVersion | None]:
        """Each exact kind version's access: current rows batched, then accepted history."""

        wanted = set(contract_digests)
        missing = sorted(wanted - set(self._versions))
        if missing:
            sql = (
                "SELECT artifact_digest,identity,path,body_access,epistemic_grade,"
                "provenance_rule_digest FROM capture_contracts WHERE artifact_digest IN ({})"
            )
            if self.projection is not None:
                rows = select_in(self.projection.typed.connection, sql, missing)
            else:
                with self.instance.bind_accepted_projection(self.coordinate) as projection:
                    rows = select_in(projection.typed.connection, sql, missing)
            for digest, identity, path, access, grade, provenance in rows:
                self._versions[str(digest)] = CaptureKindVersion(
                    identity=str(identity),
                    path=str(path),
                    access="restricted" if access == "restricted" else "open",
                    epistemic_grade=cast(Literal["observed", "derived", "predicted"], str(grade)),
                    self_asserted=provenance_rule_is_self_asserted(str(provenance)),
                )
            for digest in missing:
                if digest not in self._versions:
                    self._versions[digest] = self._historical(digest)
        return {digest: self._versions[digest] for digest in wanted}

    def _historical(self, digest: str) -> CaptureKindVersion | None:
        key = (str(self.instance.root), digest)
        with _HISTORICAL_LOCK:
            known = _HISTORICAL.get(key)
        if known is not None:
            return known
        found = self.instance.accepted_capture_contract_version(
            AcceptedCoordinate.from_internal(self.coordinate), digest
        )
        if found is None:
            # Unknown here may be accepted later: never memoized.
            return None
        version = CaptureKindVersion(
            identity=found.contract.identity.qualified,
            path=found.path,
            access=capture_body_access(found.contract),
            epistemic_grade=found.contract.epistemic_grade,
            self_asserted=capture_contract_is_self_asserted(found.contract),
        )
        with _HISTORICAL_LOCK:
            if len(_HISTORICAL) >= _HISTORICAL_MAX:
                _HISTORICAL.clear()
            _HISTORICAL[key] = version
        return version

    def withheld(
        self, digests: Iterable[str], access: BodyAccessContext
    ) -> tuple[dict[str, CaptureKindAccess], BodyRestricted | None]:
        """The Captures among ``digests`` withheld from ``access``, and their notice."""

        listed = tuple(digests)
        if access.can_read_restricted or not listed:
            return {}, None
        withheld = {
            digest: item
            for digest, item in self.captures(listed).items()
            if item.withheld_from(access)
        }
        return withheld, body_restricted_notice(withheld.values())


def body_restricted_notice(items: Iterable[CaptureKindAccess]) -> BodyRestricted | None:
    """One typed notice naming every withheld Capture and its kind, or None."""

    listed = tuple(items)
    if not listed:
        return None
    return BodyRestricted(
        capture_digests=tuple(sorted({item.capture_digest for item in listed})),
        capture_kinds=tuple(
            sorted({item.kind_identity for item in listed if item.kind_identity is not None})
        ),
    )


def capture_access_resolver(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate | None = None,
    *,
    projection: ProjectionHandle | None = None,
) -> CaptureAccessResolver:
    return CaptureAccessResolver(
        instance,
        instance.accepted_coordinate() if coordinate is None else coordinate,
        projection,
    )


def citation_access(resolver: CaptureAccessResolver, access: BodyAccessContext) -> CitationAccess:
    """The citation index's per-read classification for this reader (C14)."""

    def restricted(digests: Sequence[str]) -> set[str]:
        return {d for d, item in resolver.captures(digests).items() if item.restricted}

    def self_asserted(contract_digest: str) -> bool | None:
        version = resolver.versions((contract_digest,))[contract_digest]
        return None if version is None else version.self_asserted

    return CitationAccess(
        restricted=restricted,
        self_asserted=self_asserted,
        withhold=not access.can_read_restricted,
    )


#: How an in-process caller that passes no access reads: bodies, no restricted key.
NO_RESTRICTED_ACCESS = BodyAccessContext(principal_id="playbill-service", can_read_body=True)


#: The marker a withheld piece of evidence carries in a served Claim view.
WITHHELD_MARK = "body_restricted"


def withheld_commitments(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    captures: Iterable[str],
    *,
    projection: ProjectionHandle | None = None,
) -> tuple[set[str], bool]:
    """The evidence commitments of withheld Captures, from the citation index.

    The second value is False when some Capture has no index row: its spans
    cannot be told apart, so every span is withheld (fail closed).
    """

    wanted = sorted(set(captures))
    if not wanted:
        return set(), True
    sql = (
        "SELECT capture_digest,evidence_commitment_digest FROM captures "
        "WHERE capture_digest IN ({})"
    )
    if projection is not None:
        rows = select_in(projection.typed.connection, sql, wanted)
    else:
        with instance.bind_accepted_projection(coordinate) as bound:
            rows = select_in(bound.typed.connection, sql, wanted)
    found = {str(capture): str(commitment) for capture, commitment in rows}
    return set(found.values()), len(found) == len(wanted)


#: What an absence witness keeps when its Capture is withheld: which run judged
#: which Capture, never the selector or counts it read from the body.
_WITNESS_KEPT = ("run_id", "node_id", "procedure_digest", "capture", "logical_source")


def redact_claim_evidence(
    value: object,
    *,
    captures: set[str],
    commitments: set[str],
    every_span: bool = False,
) -> object:
    """A served payload without what is read from withheld Captures (C14).

    Accepted statement values stay. Removed, in every nested copy (facts, law
    evidence, history, review members, proposal records):

    - exact spans over a withheld Capture's commitment (``every_span``: all);
    - an evidence basis naming a withheld Capture (extracted, capture,
      measured) keeps only its basis, Capture digest and the ``withheld`` mark:
      record locators, field paths, record keys, extract specs, declared fields
      and transform lineage go;
    - a derived basis drawing on one keeps its Claim inputs and its redacted
      extractions, and loses its transform lineage;
    - an absence witness over one keeps its run, node, Procedure, Capture and
      logical source; its ``complete_when`` selector and counts, extract spec,
      extractor, enumerated key digest and expect coordinate go.
    """

    def walk(item: object) -> object:
        return redact_claim_evidence(
            item, captures=captures, commitments=commitments, every_span=every_span
        )

    if isinstance(value, list):
        return [walk(item) for item in value]
    if not isinstance(value, dict):
        return value
    capture = value.get("capture_digest")
    if "basis" in value and isinstance(capture, str) and capture in captures:
        return {"basis": value["basis"], "capture_digest": capture, "withheld": WITHHELD_MARK}
    witness = value.get("capture")
    if isinstance(witness, str) and witness in captures and "basis" not in value:
        return {
            **{key: value[key] for key in _WITNESS_KEPT if key in value},
            "withheld": WITHHELD_MARK,
        }
    if value.get("basis") == "derived":
        extractions = value.get("extractions")
        if isinstance(extractions, list) and any(
            isinstance(item, dict) and item.get("capture_digest") in captures
            for item in extractions
        ):
            return {
                "basis": "derived",
                "inputs": value.get("inputs", []),
                "extractions": walk(extractions),
                "withheld": WITHHELD_MARK,
            }
    result: dict[str, object] = {}
    for key, item in value.items():
        if key == "spans" and isinstance(item, list):
            kept = [
                span
                for span in item
                if not (
                    isinstance(span, dict)
                    and (every_span or span.get("content_digest") in commitments)
                )
            ]
            result[key] = kept
            if len(kept) != len(item):
                result["withheld"] = WITHHELD_MARK
            continue
        result[key] = walk(item)
    return result


_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def named_captures(value: object) -> tuple[str, ...]:
    """Every Capture digest a served payload names under a Capture field."""

    found: set[str] = set()

    def is_digest(item: object) -> bool:
        return isinstance(item, str) and _DIGEST_RE.fullmatch(item) is not None

    def walk(item: object) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in ("capture_digest", "capture") and is_digest(child):
                    found.add(str(child))
                elif key == "capture_digests" and isinstance(child, list):
                    found.update(str(digest) for digest in child if is_digest(digest))
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return tuple(sorted(found))


def withhold_evidence_payload(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    payload: dict[str, object],
    access: BodyAccessContext | None,
    *,
    resolver: CaptureAccessResolver | None = None,
) -> tuple[dict[str, object], BodyRestricted | None]:
    """A served payload with every Capture it names checked, and the notice (C14).

    The Captures are found in the payload itself; access is decided from
    maintained metadata before any of them is read; a withheld one's spans,
    locators and witness values go from every copy (``redact_claim_evidence``).
    Values, digests and commitments stay. A caller reading many payloads at
    ``coordinate`` passes one ``resolver`` for all of them.
    """

    resolver = resolver or capture_access_resolver(instance, coordinate)
    withheld, notice = resolver.withheld(named_captures(payload), access or NO_RESTRICTED_ACCESS)
    if notice is None:
        return payload, None
    commitments, complete = withheld_commitments(
        instance, coordinate, withheld, projection=resolver.projection
    )
    redacted = redact_claim_evidence(
        payload, captures=set(withheld), commitments=commitments, every_span=not complete
    )
    assert isinstance(redacted, dict)
    return redacted, notice


__all__ = [
    "NO_RESTRICTED_ACCESS",
    "CaptureAccessResolver",
    "CaptureKindAccess",
    "CaptureKindVersion",
    "WITHHELD_MARK",
    "body_restricted_notice",
    "capture_access_resolver",
    "citation_access",
    "named_captures",
    "redact_claim_evidence",
    "withhold_evidence_payload",
    "withheld_commitments",
]
