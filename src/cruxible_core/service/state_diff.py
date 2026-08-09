"""Coordinate resolution, artifact assembly, and receipting for ``state diff``.

The graph-shape half lives in ``graph/diff.py``. This module owns the parts a
comparator cannot: which two coordinates are being compared, what each one is
licensed to claim, how the complete logical diff becomes a content-addressed
canonical artifact, and how the bounded view that comes back over the wire
accounts for everything it dropped.

Two separations carry the whole design:

* **``logical_diff`` vs ``returned_view``.** Only the complete, unelided
  logical body is the plan artifact, and only it feeds ``diff_digest``. The
  bounded view has its own digest and its own accounting, and is never
  mistakable for the plan.
* **Omitted is not empty.** A section is compared only where it is
  ``available`` on BOTH coordinates; otherwise it is dropped from the body and
  named in ``omitted_sections`` with a reason. An empty section means
  "compared, no differences".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from cruxible_core import __version__
from cruxible_core.config.provenance import compute_file_digest
from cruxible_core.errors import ConcurrentStateDriftError, ConfigError
from cruxible_core.graph.diff import (
    DIFF_BUCKET_NAMES,
    GraphDiffSelector,
    GraphDiffSide,
    OwnershipBasis,
    SectionDiff,
    apply_selector_to_section,
    diff_edges,
    diff_entities,
    normalize_json_value,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.legacy_identity import (
    legacy_identity_map_digest,
    load_legacy_identity_map,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateMember
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.types import ProcedureRecord
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.property_diffs import property_delta, property_value_changes
from cruxible_core.service.types import (
    StateDiffArtifactRef,
    StateDiffArtifactResult,
    StateDiffResult,
)
from cruxible_core.snapshot.types import UpstreamMetadata
from cruxible_core.snapshot.upstream_verification import (
    UpstreamMember,
    sha256_file,
    verify_tracked_upstream,
)
from cruxible_core.storage.sqlite import LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY
from cruxible_core.temporal import ensure_utc, format_datetime
from cruxible_core.workflow.compiler import LOCK_FILE_NAME, compute_lock_config_digest

ARTIFACT_SCHEMA_VERSION = 1
"""Governs the digest PREIMAGE shape, and is inside the preimage.

``diff_engine_version`` records the implementation and is deliberately OUTSIDE
it: a pure implementation fix must not change every digest in the world.
"""

DIFF_ENGINE_VERSION = __version__

DEFAULT_BUCKET_CAP = 500
VALUE_ELISION_BYTES = 2048
DIFF_ARTIFACT_DIRNAME = "diffs"

StateCoordinateKind = Literal["current", "snapshot", "upstream", "origin"]
SectionName = Literal["entities", "edges", "procedures"]
SectionStatus = Literal[
    "available",
    "unavailable_by_format",
    "unavailable_missing_artifact",
    "not_applicable",
]

ALL_SECTIONS: tuple[SectionName, ...] = ("entities", "edges", "procedures")

CURRENT_COORDINATE = "current"
UPSTREAM_COORDINATE = "upstream"
ORIGIN_COORDINATE = "origin"
RESERVED_COORDINATES: frozenset[str] = frozenset(
    {CURRENT_COORDINATE, UPSTREAM_COORDINATE, ORIGIN_COORDINATE}
)


def compare_pending_relationships(
    graph: EntityGraph,
    relationship_type: str,
    members: list[CandidateMember],
) -> dict[str, Any]:
    """Preview candidate relationships through the shared edge comparator."""
    proposed = EntityGraph.from_dict(graph.to_dict())
    for index, member in enumerate(members):
        relationship = member.as_relationship()
        identity = {
            **relationship.identity_payload(),
            "properties": relationship.properties,
            "index": index,
        }
        claim_digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
        proposed.add_relationship(
            RelationshipInstance(
                **relationship.model_dump(mode="python", exclude={"claim_id", "edge_key"}),
                claim_id=f"preview-{claim_digest[:24]}",
            )
        )
    basis = OwnershipBasis.unknown_basis("pending relationship preview has no ownership pin")
    section = diff_edges(
        GraphDiffSide(graph=graph, ownership=basis),
        GraphDiffSide(graph=proposed, ownership=basis),
        GraphDiffSelector(relationship_types=frozenset({relationship_type})),
    )
    return {"basis": "pending_group", "sections": {"edges": section.payload()}}


_SNAPSHOT_ID_PATTERN = re.compile(r"^snap_[0-9a-f]{16}$")
"""Snapshot ids are ``snap_`` + 16 hex chars, minted by ``new_id("snap", 16, "_")``.

The safety property is DISJOINTNESS, not prefix disambiguation: no snapshot id
can ever equal ``current``/``upstream``/``origin`` because every id carries the
prefix and 16 hex characters and none of the literals do. Ambiguity is
impossible by construction.
"""

_GRAPH_ARTIFACT = "graph.json"
_PROCEDURES_ARTIFACT = "procedures.json"
_UPSTREAM_ARTIFACT = "upstream.json"

_COORDINATE_GRAMMAR = (
    "Coordinates are: 'current', 'upstream', 'origin', or a snapshot id "
    "(snap_ followed by 16 hex characters, exactly as `cruxible snapshot list` "
    "prints it). A release you have not pulled is NOT a coordinate -- diffing "
    "against un-materialized foreign bytes is pull-preview's job, because it "
    "owns transport and verification; materialize it with `cruxible state "
    "pull-apply` and it becomes 'upstream' here."
)


# ---------------------------------------------------------------------------
# Resolved coordinate
# ---------------------------------------------------------------------------


@dataclass
class ResolvedStateCoordinate:
    """Everything one side of the diff is LICENSED to claim.

    The three coordinate kinds do not carry a common state scope, so this
    records per-section availability rather than pretending they do.
    """

    kind: StateCoordinateKind
    spec: str
    identity: dict[str, Any]
    sections: dict[str, SectionStatus]
    digests: dict[str, dict[str, Any]]
    ownership: OwnershipBasis
    claim_identity_map_digest: str | None = None
    claim_identity_coverage: dict[str, int] | None = None
    verification: Literal["not_applicable", "verified", "unverified_legacy"] = "not_applicable"
    members: dict[str, str] = field(default_factory=dict)
    normalizations: tuple[str, ...] = ()
    default_basis: str | None = None
    graph: EntityGraph = field(default_factory=EntityGraph)
    procedures_source: bytes | None = None
    procedures_records: list[ProcedureRecord] | None = None

    def payload(self, *, ownership: OwnershipBasis) -> dict[str, Any]:
        """Digest-preimage payload. ``ownership`` is the RECONCILED basis (D2)."""
        body: dict[str, Any] = {
            "kind": self.kind,
            "spec": self.spec,
            "identity": normalize_json_value(self.identity),
            "sections": dict(sorted(self.sections.items())),
            "digests": {
                name: dict(sorted(value.items())) for name, value in sorted(self.digests.items())
            },
            "ownership": ownership.payload(),
            "verification": self.verification,
            "members": dict(sorted(self.members.items())),
        }
        if self.default_basis is not None:
            body["default_basis"] = self.default_basis
        if self.claim_identity_coverage is not None or self.claim_identity_map_digest is not None:
            body["claim_identity"] = {
                "map_digest": self.claim_identity_map_digest,
                "coverage": self.claim_identity_coverage,
            }
        return body

    def load_procedures(self) -> list[ProcedureRecord]:
        """Parse the procedures artifact ON DEMAND.

        Lazy on purpose: an unselected section is never read, so corruption in
        one section's artifact cannot block a diff of another.
        """
        if self.procedures_records is not None:
            return self.procedures_records
        if self.procedures_source is None:
            return []
        snapshot_id = self.identity.get("snapshot_id")
        try:
            payload = json.loads(self.procedures_source.decode("utf-8"))
            raw = payload["procedures"] if isinstance(payload, dict) else None
            if not isinstance(raw, list):
                raise ValueError("procedures.json must contain a procedures list")
            records = [ProcedureRecord.model_validate(item) for item in raw]
        except (UnicodeDecodeError, ValueError, KeyError, ValidationError) as exc:
            raise ConfigError(
                f"Snapshot '{snapshot_id}' has an invalid '{_PROCEDURES_ARTIFACT}' artifact "
                f"and the procedures section was selected: {exc}"
            ) from exc
        self.procedures_records = records
        return records


# ---------------------------------------------------------------------------
# Coordinate parsing and resolution
# ---------------------------------------------------------------------------


def parse_state_coordinate(spec: str) -> StateCoordinateKind:
    """Classify one coordinate token, or refuse with the grammar."""
    if spec == CURRENT_COORDINATE:
        return "current"
    if spec == UPSTREAM_COORDINATE:
        return "upstream"
    if spec == ORIGIN_COORDINATE:
        return "origin"
    if _SNAPSHOT_ID_PATTERN.match(spec):
        return "snapshot"
    raise ConfigError(f"Unrecognized state coordinate '{spec}'. {_COORDINATE_GRAMMAR}")


def resolve_default_coordinates(
    instance: InstanceProtocol,
    from_spec: str | None,
    to_spec: str | None,
) -> tuple[str, str, str | None]:
    """Fill the bare/one-sided command in, and say which rule fired.

    Bare ``cruxible state diff`` is ``parent(head) -> current``, not
    ``head -> current``: ``commit_graph_snapshot`` persists a snapshot AND
    atomically advances live state in one boundary, so on the common governed
    path head's graph IS the current graph and ``head -> current`` is the empty
    diff by construction -- a useless default on exactly the instances someone
    would run it on.
    """
    if to_spec is None:
        to_spec = CURRENT_COORDINATE
    if from_spec is not None:
        return from_spec, to_spec, None

    head = instance.get_head_snapshot_id()
    if head is None:
        raise ConfigError(
            "This instance has no snapshot to diff against, so there is no default "
            "'from' coordinate. Name one explicitly (`cruxible state diff <from> "
            "[to]`), list what exists with `cruxible snapshot list`, or use the "
            "'origin' coordinate if this instance was cloned."
        )
    snapshot = instance.get_snapshot(head)
    parent = snapshot.parent_snapshot_id if snapshot is not None else None
    if parent is None:
        return head, to_spec, "head"
    return parent, to_spec, "parent_of_head"


def resolve_state_coordinate(
    instance: InstanceProtocol,
    spec: str,
    *,
    sections: frozenset[str],
    default_basis: str | None = None,
) -> ResolvedStateCoordinate:
    """Resolve one coordinate to a typed, self-describing side of the diff."""
    kind = parse_state_coordinate(spec)
    if kind == "current":
        return _resolve_current(
            instance,
            spec,
            sections=sections,
            default_basis=default_basis,
        )
    if kind == "upstream":
        return _resolve_upstream(instance, spec, default_basis=default_basis)
    if kind == "origin":
        origin_id = instance.get_origin_snapshot_id()
        if origin_id is None:
            raise ConfigError(
                "This instance has no 'origin' snapshot. Origin is CLONE provenance -- "
                "it is stamped only by `cruxible snapshot clone` -- and is absent "
                "forever on an init-created instance. Name a snapshot id instead."
            )
        return _resolve_snapshot(
            instance,
            spec,
            snapshot_id=origin_id,
            kind="origin",
            sections=sections,
            default_basis=default_basis,
        )
    return _resolve_snapshot(
        instance,
        spec,
        snapshot_id=spec,
        kind="snapshot",
        sections=sections,
        default_basis=default_basis,
    )


def _resolve_current(
    instance: InstanceProtocol,
    spec: str,
    *,
    sections: frozenset[str],
    default_basis: str | None,
) -> ResolvedStateCoordinate:
    """Capture ``current`` under the invalidate + revision-sandwich + one retry.

    ``load_graph`` serves a per-process cache and head/revision are separate,
    unsynchronized reads, so without this the stamped coordinate could describe
    a graph it does not match. The invalidate is REQUIRED: another process's
    write does not clear this process's cache.

    EVERY section this coordinate will contribute is read INSIDE the sandwich.
    The procedure table is a separate store from the graph, so deferring its
    read until section assembly -- after the closing revision was taken -- let a
    concurrent procedure mutation produce an artifact whose graph is revision N
    and whose procedures are revision N+1, stamped as a single coordinate. The
    sandwich is a claim about the whole coordinate, not about the graph alone,
    so a bump between any two of its reads must retry or refuse.

    It stays lazy where it can: the store is not touched at all unless the
    procedures section was selected, so an edges-only diff still pays nothing
    for an unbounded full-table fetch.
    """
    wants_procedures = "procedures" in sections
    opening = 0
    closing = 0
    graph: EntityGraph | None = None
    head: str | None = None
    procedures: list[ProcedureRecord] | None = None
    for _attempt in range(2):
        instance.invalidate_graph_cache()
        opening = instance.get_read_revision()
        graph = instance.load_graph()
        procedures = _load_current_procedures(instance) if wants_procedures else None
        closing = instance.get_read_revision()
        head = instance.get_head_snapshot_id()
        if closing == opening:
            break
    else:
        raise ConcurrentStateDriftError(opening, closing)
    assert graph is not None

    upstream = instance.get_upstream_metadata()
    ownership = OwnershipBasis.pinned(
        owned_entity_types=upstream.owned_entity_types if upstream is not None else (),
        owned_relationship_types=upstream.owned_relationship_types if upstream is not None else (),
    )
    identity_map = load_legacy_identity_map(
        instance.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY)
    )
    return ResolvedStateCoordinate(
        kind="current",
        spec=spec,
        identity={"head_snapshot_id": head, "read_revision": opening},
        sections={"entities": "available", "edges": "available", "procedures": "available"},
        digests=_current_digest_domains(instance),
        ownership=ownership,
        claim_identity_map_digest=legacy_identity_map_digest(identity_map),
        claim_identity_coverage=None,
        verification="not_applicable",
        default_basis=default_basis,
        graph=graph,
        procedures_records=procedures,
    )


def _load_current_procedures(instance: InstanceProtocol) -> list[ProcedureRecord]:
    store = instance.get_procedure_store()
    try:
        total = store.count_procedures()
        return store.list_procedures(limit=max(total, 1), offset=0)
    finally:
        store.close()


def _current_digest_domains(instance: InstanceProtocol) -> dict[str, dict[str, Any]]:
    domains: dict[str, dict[str, Any]] = {
        "semantic_config_digest": {
            "value": compute_lock_config_digest(instance.load_config()),
            "scope": "semantic",
        }
    }
    provenance = instance.get_config_provenance()
    if provenance is not None:
        domains["materialized_config_digest"] = {
            "value": provenance.materialized_digest,
            "scope": "bytes",
        }
    lock_path = instance.get_instance_dir() / LOCK_FILE_NAME
    if lock_path.exists():
        domains["lock_file_digest"] = {"value": compute_file_digest(lock_path), "scope": "bytes"}
    return domains


def _resolve_snapshot(
    instance: InstanceProtocol,
    spec: str,
    *,
    snapshot_id: str,
    kind: StateCoordinateKind,
    sections: frozenset[str],
    default_basis: str | None,
) -> ResolvedStateCoordinate:
    snapshot = instance.get_snapshot(snapshot_id)
    if snapshot is None:
        raise ConfigError(f"Snapshot '{snapshot_id}' not found")
    graph_bytes = instance.get_snapshot_artifact(snapshot_id, _GRAPH_ARTIFACT)
    if graph_bytes is None:
        raise ConfigError(
            f"Snapshot '{snapshot_id}' is missing its '{_GRAPH_ARTIFACT}' artifact, so "
            "there is no graph to compare. Nothing partial is built from a snapshot "
            "whose graph member is absent."
        )
    try:
        graph_data = json.loads(graph_bytes.decode("utf-8"))
        if not isinstance(graph_data, dict):
            raise ValueError("graph.json must contain a node-link object")
        # MINTING AND MAP APPLICATION ARE BOTH FORBIDDEN on a snapshot.
        # ``from_dict`` never mints; the reconcile map is keyed by THIS
        # instance's tracked-upstream tuples, and a pre-identity snapshot of
        # this instance is not upstream-origin, so applying it would fabricate
        # identity out of an unrelated namespace.
        graph = EntityGraph.from_dict(graph_data)
    # KeyError and TypeError are in the net because networkx's node-link
    # deserializer raises them, not ValueError, for a dict that is well-formed
    # JSON but not node-link data -- ``{}`` raises KeyError('nodes'). Without
    # them a corrupt member escaped the named D12 refusal as an unhandled
    # server error.
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(
            f"Snapshot '{snapshot_id}' has a '{_GRAPH_ARTIFACT}' artifact that is not "
            f"valid node-link graph data: {exc}"
        ) from exc

    procedures_bytes = instance.get_snapshot_artifact(snapshot_id, _PROCEDURES_ARTIFACT)
    procedures_status: SectionStatus = (
        "available" if procedures_bytes is not None else "unavailable_missing_artifact"
    )
    ownership = _snapshot_ownership_basis(instance, snapshot_id)
    return ResolvedStateCoordinate(
        kind=kind,
        spec=spec,
        identity={"snapshot_id": snapshot_id, "created_at": format_datetime(snapshot.created_at)},
        sections={
            "entities": "available",
            "edges": "available",
            "procedures": procedures_status,
        },
        digests={
            "semantic_config_digest": {"value": snapshot.config_digest, "scope": "semantic"},
            **(
                {"lock_file_digest": {"value": snapshot.lock_digest, "scope": "bytes"}}
                if snapshot.lock_digest is not None
                else {}
            ),
            "graph_artifact_digest": {"value": snapshot.graph_digest, "scope": "bytes"},
        },
        ownership=ownership,
        # Snapshots record no reconcile-map digest, so condition 1 of the
        # reidentification rule is unavailable and reidentification is NEVER
        # claimed for a snapshot pair. That is correct, and it is the concrete
        # reason ``upstream.json`` is worth adding going forward.
        claim_identity_map_digest=None,
        verification="not_applicable",
        default_basis=default_basis,
        graph=graph,
        procedures_source=procedures_bytes if "procedures" in sections else None,
    )


def _snapshot_ownership_basis(instance: InstanceProtocol, snapshot_id: str) -> OwnershipBasis:
    """Pinned basis from ``upstream.json``, else ``unknown`` -- never recomputed.

    A corrupt member DEGRADES rather than refuses: ownership is an annotation,
    and losing it must not make an otherwise-readable snapshot undiffable. But
    the degrade is NAMED, because an unexplained ``unknown`` -- and the
    ``stub_detection: disabled`` it forces -- is indistinguishable from a bug
    in the diff.
    """
    raw = instance.get_snapshot_artifact(snapshot_id, _UPSTREAM_ARTIFACT)
    if raw is None:
        return OwnershipBasis.unknown_basis(
            f"'{_UPSTREAM_ARTIFACT}' absent: this snapshot predates the pinned ownership "
            "basis, which is never recomputed from current instance metadata"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return OwnershipBasis.unknown_basis(
            f"'{_UPSTREAM_ARTIFACT}' unreadable ({exc.__class__.__name__}): the pinned "
            "ownership basis cannot be recovered from this snapshot"
        )
    if payload is None:
        # The snapshot was written on an instance tracking no upstream. That is
        # a PINNED empty boundary -- "nothing was upstream-owned then" -- not an
        # unknown one.
        return OwnershipBasis.pinned()
    if not isinstance(payload, dict):
        return OwnershipBasis.unknown_basis(
            f"'{_UPSTREAM_ARTIFACT}' is neither an object nor null: the pinned ownership "
            "basis cannot be recovered from this snapshot"
        )
    try:
        # VALIDATED, not duck-typed. ``_write_snapshot`` writes exactly an
        # ``UpstreamMetadata`` dump or ``null``, so anything else is a
        # hand-edited or truncated member. Reading the fields off an arbitrary
        # dict accepted syntactically-valid garbage: ``{}`` became a pinned
        # EMPTY boundary that silently enabled stub detection, and a string
        # where a list belongs became a pinned boundary over its CHARACTERS.
        metadata = UpstreamMetadata.model_validate(payload)
    except ValidationError as exc:
        return OwnershipBasis.unknown_basis(
            f"'{_UPSTREAM_ARTIFACT}' is not a valid upstream record "
            f"({exc.error_count()} validation error(s)): the pinned ownership basis "
            "cannot be recovered from this snapshot"
        )
    return OwnershipBasis.pinned(
        owned_entity_types=metadata.owned_entity_types,
        owned_relationship_types=metadata.owned_relationship_types,
    )


_REPORTED_UPSTREAM_MEMBERS: tuple[tuple[UpstreamMember, str, str | None, str], ...] = (
    ("graph.json", "graph_digest", "graph_artifact_digest", "graph_path"),
    (
        "config.yaml",
        "upstream_config_digest",
        "materialized_config_digest",
        "upstream_config_path",
    ),
    ("cruxible.lock.yaml", "upstream_lock_digest", "lock_file_digest", "lock_path"),
)


def _resolve_upstream(
    instance: InstanceProtocol,
    spec: str,
    *,
    default_basis: str | None,
) -> ResolvedStateCoordinate:
    upstream = instance.get_upstream_metadata()
    if upstream is None:
        raise ConfigError(
            "This instance is not a pullable overlay: it tracks no upstream state "
            "release, so there is no 'upstream' coordinate to diff against."
        )
    root = instance.get_root_path()
    members, verification, digests = _verify_reported_upstream_members(root, upstream)

    graph_path = root / upstream.graph_path
    try:
        graph = EntityGraph.from_dict(json.loads(graph_path.read_text()))
    # Same net as the snapshot path: networkx raises KeyError/TypeError for a
    # JSON object that is not node-link data.
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(
            f"Materialized upstream '{_GRAPH_ARTIFACT}' at {upstream.graph_path} could not "
            f"be read as node-link graph data: {exc}"
        ) from exc
    # COMPARISON-TIME NORMALIZATION, disclosed in the header. Every
    # materialization site relabels receipts before use; the bytes on disk keep
    # the PUBLISHER's raw ids, and the loader does not relabel. Without this,
    # every upstream-owned edge of every fresh pull reads as "provenance
    # changed" against the live side that was already relabeled.
    graph.relabel_clone_receipts()
    # READ-ONLY reuse of the persisted reconcile map. Never mints, never
    # writes: edges the map cannot explain stay id-less and fall through to
    # tuple identity, and the coverage is reported so partial resolution is
    # visible rather than silent.
    identity_map = load_legacy_identity_map(
        instance.get_instance_state(LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY)
    )
    resolved, unresolved = graph.annotate_claim_ids_from_map(identity_map)

    return ResolvedStateCoordinate(
        kind="upstream",
        spec=spec,
        identity={
            "state_id": upstream.state_id,
            "release_id": upstream.release_id,
            "snapshot_id": upstream.snapshot_id,
        },
        sections={
            "entities": "available",
            "edges": "available",
            # Release bundles carry no procedures member, and the tracked
            # upstream member set has none either. Permanently unavailable by
            # FORMAT until the bundle format is versioned to carry it.
            "procedures": "unavailable_by_format",
        },
        digests=digests,
        ownership=OwnershipBasis.pinned(
            owned_entity_types=upstream.owned_entity_types,
            owned_relationship_types=upstream.owned_relationship_types,
        ),
        claim_identity_map_digest=upstream.identity_map_digest,
        claim_identity_coverage={"resolved": resolved, "unresolved": unresolved},
        verification=verification,
        members=members,
        normalizations=("upstream_clone_relabel",),
        default_basis=default_basis,
        graph=graph,
    )


def _verify_reported_upstream_members(
    root: Path,
    upstream: UpstreamMetadata,
) -> tuple[dict[str, str], Literal["verified", "unverified_legacy"], dict[str, dict[str, Any]]]:
    """Verify exactly the members whose values the header reports.

    Per-member and TRI-STATE. A member that was pinned and no longer matches
    (or is missing) still refuses loudly through ``verify_tracked_upstream``.
    A member whose digest was NEVER recorded -- overlays created before that
    member was pinned -- is ``unpinned_legacy`` rather than a refusal: refusing
    would break overlays that were never tampered with, and a diff is a read,
    not a trust escalation. The coordinate then reads ``unverified_legacy`` and
    the artifact header carries ``artifact_trust: "unverified_upstream"``
    INSIDE the digest preimage, so a downstream pin over an unverified upstream
    cannot be laundered into looking verified.
    """
    members: dict[str, str] = {}
    digests: dict[str, dict[str, Any]] = {}
    any_unpinned = False
    for member, digest_field, domain, path_field in _REPORTED_UPSTREAM_MEMBERS:
        expected = getattr(upstream, digest_field)
        # ONE hash per member. The value the header reports and the value the
        # pinned-digest comparison needs are the same bytes; re-reading a large
        # graph.json to produce each of them separately is cost paid for
        # nothing.
        actual = sha256_file(root / getattr(upstream, path_field))
        if expected is None:
            members[member] = "unpinned_legacy"
            any_unpinned = True
        elif actual == expected:
            members[member] = "verified"
        else:
            # Delegated rather than paraphrased, so the refusal is the
            # verifier's own message with its own repair guidance.
            verify_tracked_upstream(root, upstream, members=(member,))
            raise AssertionError(  # pragma: no cover - the verifier must refuse
                f"Upstream member '{member}' mismatched but verification passed"
            )
        if domain is not None and actual is not None:
            digests[domain] = {"value": actual, "scope": "bytes"}
    return members, ("unverified_legacy" if any_unpinned else "verified"), digests


# ---------------------------------------------------------------------------
# Procedures section
# ---------------------------------------------------------------------------


def _procedure_state(record: ProcedureRecord) -> dict[str, Any]:
    """Normalize procedure timestamps AT THE DIFF SEAM.

    Procedure models carry no UTC validator, so two records describing the same
    instant can serialize differently. Normalizing here is a diff-layer
    concern, not a procedure-model change. Domain PROPERTY values are never
    guessed at this way: a property that looks like a timestamp is compared as
    the value it is.
    """
    payload = record.model_dump(mode="json", by_alias=True, exclude_none=True)
    for name in ("proposed_at", "resolved_at", "retired_at"):
        value = getattr(record, name, None)
        if value is not None:
            payload[name] = format_datetime(ensure_utc(value))
    return dict(normalize_json_value(payload))


def diff_procedures(
    from_records: list[ProcedureRecord],
    to_records: list[ProcedureRecord],
    selector: GraphDiffSelector,
) -> SectionDiff:
    """Procedure definitions compared by ``procedure_id``.

    Bucket selection applies here exactly as it does to the graph sections:
    the selector is part of the diff's DEFINITION and rides inside the digest
    preimage, so a section that quietly ignored it would make two differently
    filtered artifacts carry different digests over identical bodies.
    """
    result = SectionDiff(
        counts={
            "from_total": len(from_records),
            "to_total": len(to_records),
            "unchanged": 0,
            "added": 0,
            "removed": 0,
            "changed": 0,
            "annotation_only": 0,
            "ambiguous_from": 0,
            "ambiguous_to": 0,
            "identity_conflict_from": 0,
            "identity_conflict_to": 0,
            "excluded_from": 0,
            "excluded_to": 0,
        }
    )
    from_states = {record.procedure_id: _procedure_state(record) for record in from_records}
    to_states = {record.procedure_id: _procedure_state(record) for record in to_records}
    for procedure_id in sorted(set(from_states) | set(to_states)):
        before = from_states.get(procedure_id)
        after = to_states.get(procedure_id)
        if before is None and after is not None:
            result.counts["added"] += 1
            result.added.append({"procedure_id": procedure_id, "state": after})
            continue
        if after is None and before is not None:
            result.counts["removed"] += 1
            result.removed.append({"procedure_id": procedure_id, "state": before})
            continue
        assert before is not None and after is not None
        if before == after:
            result.counts["unchanged"] += 1
            continue
        delta = property_delta(after, before)
        result.counts["changed"] += 1
        result.changed.append(
            {
                "procedure_id": procedure_id,
                "identity_basis": "procedure_id",
                "subtype": None,
                "channels": ["properties"],
                "annotation_only": False,
                "properties": {
                    "delta": {
                        "added": list(delta.added),
                        "removed": list(delta.removed),
                        "changed": list(delta.changed),
                    },
                    "changes": [
                        {
                            "property": change.property,
                            "from_value": normalize_json_value(change.from_value),
                            "to_value": normalize_json_value(change.to_value),
                        }
                        for change in property_value_changes(
                            after, before, include_added=True, include_removed=True
                        )
                    ],
                },
                "review_transition": None,
                "review_changes": [],
                "lifecycle_transition": None,
                "lifecycle_changes": [],
                "annotations": None,
            }
        )
    result.diagnostics = {"stub_detection": "not_applicable"}
    result.assert_partition()
    apply_selector_to_section(result, selector)
    return result


# ---------------------------------------------------------------------------
# Selector
# ---------------------------------------------------------------------------


def _normalized_selector(
    *,
    sections: tuple[str, ...] | None,
    entity_types: tuple[str, ...] | None,
    relationship_types: tuple[str, ...] | None,
    buckets: tuple[str, ...] | None,
    changed_only: bool,
) -> dict[str, Any]:
    if sections is not None:
        unknown = sorted(set(sections) - set(ALL_SECTIONS))
        if unknown:
            raise ConfigError(
                f"Unknown diff section(s): {', '.join(unknown)}. "
                f"Valid sections: {', '.join(ALL_SECTIONS)}."
            )
    if buckets is not None:
        unknown = sorted(set(buckets) - set(DIFF_BUCKET_NAMES))
        if unknown:
            raise ConfigError(
                f"Unknown diff bucket(s): {', '.join(unknown)}. "
                f"Valid buckets: {', '.join(DIFF_BUCKET_NAMES)}."
            )
    return {
        "sections": sorted(set(sections)) if sections is not None else None,
        "entity_types": sorted(set(entity_types)) if entity_types is not None else None,
        "relationship_types": (
            sorted(set(relationship_types)) if relationship_types is not None else None
        ),
        "buckets": sorted(set(buckets)) if buckets is not None else None,
        "changed_only": bool(changed_only),
    }


def _reconciled_basis(
    own: OwnershipBasis,
    other: ResolvedStateCoordinate,
    *,
    known: bool,
) -> OwnershipBasis:
    """Apply the both-sides-unknown rule while PRESERVING why it is unknown.

    A side that is unknown in its own right keeps its own reason; a pinned side
    dragged to unknown by the other coordinate says so, and names which one --
    otherwise the two situations, which have different fixes, are
    indistinguishable in the artifact.
    """
    if known:
        return own
    if not own.is_known:
        return own
    return OwnershipBasis.unknown_basis(
        f"propagated: the '{other.spec}' coordinate's ownership basis is unknown "
        f"({other.ownership.reason or 'reason unrecorded'}), and mixing a pinned "
        "basis with an unpinned one produces annotations that are individually "
        "plausible and jointly meaningless"
    )


def _validate_type_filters(
    selector: dict[str, Any],
    from_coordinate: ResolvedStateCoordinate,
    to_coordinate: ResolvedStateCoordinate,
) -> None:
    """Refuse a type filter no coordinate can satisfy, listing what exists.

    Validated against the UNION of the two resolved graphs rather than the
    current config: a snapshot legitimately carries types the config has since
    dropped, and refusing those would make history unreadable.
    """
    for key, valid in (
        (
            "entity_types",
            set(from_coordinate.graph.list_entity_types())
            | set(to_coordinate.graph.list_entity_types()),
        ),
        (
            "relationship_types",
            set(from_coordinate.graph.list_relationship_types())
            | set(to_coordinate.graph.list_relationship_types()),
        ),
    ):
        requested = selector[key]
        if requested is None:
            continue
        unknown = sorted(set(requested) - valid)
        if unknown:
            raise ConfigError(
                f"Unknown {key.replace('_', ' ')} in the diff selector: "
                f"{', '.join(unknown)}. Present at these coordinates: "
                f"{', '.join(sorted(valid)) or '(none)'}."
            )


# ---------------------------------------------------------------------------
# Artifact assembly
# ---------------------------------------------------------------------------


def _digest_of(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()}"


def _digest_domain_context(
    from_coordinate: ResolvedStateCoordinate,
    to_coordinate: ResolvedStateCoordinate,
) -> dict[str, Any]:
    """Equality flags ONLY between identically-named domains present on both sides.

    ``config_digest``/``lock_digest``/``graph_digest`` denote different
    algorithms on different sides; a semantic hash is never compared to a byte
    hash. ``graph_artifact_digest`` equality is sufficient-but-not-necessary
    for graph identity, so inequality is inequality of BYTES -- the diff body
    is the semantic oracle.
    """
    domains: dict[str, Any] = {}
    for name in sorted(set(from_coordinate.digests) | set(to_coordinate.digests)):
        left = from_coordinate.digests.get(name)
        right = to_coordinate.digests.get(name)
        comparable = left is not None and right is not None
        domains[name] = {
            "scope": (left or right or {}).get("scope"),
            "from": (left or {}).get("value"),
            "to": (right or {}).get("value"),
            "comparable": comparable,
            "equal": (left or {}).get("value") == (right or {}).get("value")
            if comparable
            else None,
        }
    return {"digest_domains": domains}


def _section_summary(sections: dict[str, Any]) -> dict[str, Any]:
    totals = {
        "added": 0,
        "removed": 0,
        "changed": 0,
        "annotation_only": 0,
        "ambiguous_from": 0,
        "ambiguous_to": 0,
        "identity_conflict_from": 0,
        "identity_conflict_to": 0,
        "unchanged": 0,
    }
    for body in sections.values():
        counts = body["counts"]
        for name in totals:
            totals[name] += int(counts.get(name, 0))
    return totals


def _build_logical_body(
    *,
    from_coordinate: ResolvedStateCoordinate,
    to_coordinate: ResolvedStateCoordinate,
    from_ownership: OwnershipBasis,
    to_ownership: OwnershipBasis,
    selector: dict[str, Any],
    sections: dict[str, SectionDiff],
    omitted: list[dict[str, Any]],
    artifact_trust: str,
    normalizations: list[str],
) -> dict[str, Any]:
    section_payloads = {name: sections[name].payload() for name in sorted(sections)}
    return {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "artifact_trust": artifact_trust,
        "normalizations": sorted(set(normalizations)),
        # Live-view membership is NEVER computed: it would make the digest
        # clock-dependent, so two runs over byte-identical stored state would
        # disagree and the artifact would be worthless.
        "liveness": "not_evaluated",
        "selector": selector,
        "from": from_coordinate.payload(ownership=from_ownership),
        "to": to_coordinate.payload(ownership=to_ownership),
        "omitted_sections": sorted(omitted, key=lambda item: str(item["section"])),
        "context": _digest_domain_context(from_coordinate, to_coordinate),
        "sections": section_payloads,
        "summary": _section_summary(section_payloads),
    }


# ---------------------------------------------------------------------------
# Bounded view
# ---------------------------------------------------------------------------


_ITEM_DIAGNOSTIC_KEY = "diagnostic"
_NESTED_ITEM_LIST_KEYS = ("from_items", "to_items")
_RECORD_SHAPED_BUCKETS = frozenset({"ambiguous", "identity_conflict"})
"""Buckets whose entries are per-side RECORDS, not single items.

Both decline to guess at a granularity coarser than one item -- a bucket of
residuals, a colliding id -- so one entry covers several rows on each side. The
shape is deliberate and shared; the counts that go with it are per side.
"""


def _without_item_diagnostics(body: dict[str, Any]) -> dict[str, Any]:
    """Project the body without per-item ``diagnostic`` blocks, BY PATH.

    Strictly path-scoped, never a recursive key sweep. Domain state is
    caller-authored: an entity or edge may legitimately carry a property
    literally named ``diagnostic``, and a blanket recursive strip would delete
    it from the persisted plan while ``artifact_complete`` still said the body
    was whole -- silent data loss inside the one artifact whose entire job is
    to be complete.

    Only two shapes carry the ephemeral block: a bucket item's own top-level
    ``diagnostic``, and the same key on each entry of an ``ambiguous`` /
    ``identity_conflict`` record's per-side item lists. Nothing below those is
    touched, so ``state``, ``properties``, and every property value survive
    verbatim.

    The plural section-level ``diagnostics`` is NOT stripped: stub-exclusion
    accounting and ``stub_detection`` are semantic claims about what the diff
    did and did not compare.
    """
    projected = dict(body)
    projected["sections"] = {
        name: _section_without_item_diagnostics(section)
        for name, section in body.get("sections", {}).items()
    }
    return projected


def _section_without_item_diagnostics(section: dict[str, Any]) -> dict[str, Any]:
    projected = dict(section)
    for bucket in DIFF_BUCKET_NAMES:
        if bucket not in section:
            continue
        projected[bucket] = [_item_without_diagnostics(item) for item in section[bucket]]
    return projected


def _item_without_diagnostics(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    projected = {key: value for key, value in item.items() if key != _ITEM_DIAGNOSTIC_KEY}
    for list_key in _NESTED_ITEM_LIST_KEYS:
        nested = projected.get(list_key)
        if isinstance(nested, list):
            projected[list_key] = [
                {key: value for key, value in entry.items() if key != _ITEM_DIAGNOSTIC_KEY}
                if isinstance(entry, dict)
                else entry
                for entry in nested
            ]
    return projected


@dataclass
class _ViewBudget:
    cap: int
    elided: int = 0


def _elide_value(value: Any, budget: _ViewBudget) -> Any:
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) <= VALUE_ELISION_BYTES:
        return value
    budget.elided += 1
    return {
        "elided": True,
        "value_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "byte_count": len(encoded),
    }


_ELIDABLE_KEYS = frozenset({"from_value", "to_value", "state"})


def _elide_item(item: Any, budget: _ViewBudget) -> Any:
    """Replace oversized VALUES with ``{elided, value_digest, byte_count}``.

    Scoped to value-bearing keys (a property change's two sides, and a present
    item's whole state) so structural channel bodies stay walkable no matter
    how large one property got.
    """
    if isinstance(item, dict):
        result: dict[str, Any] = {}
        for key, value in item.items():
            if key in _ELIDABLE_KEYS:
                result[key] = _elide_value(value, budget)
            else:
                result[key] = _elide_item(value, budget)
        return result
    if isinstance(item, list):
        return [_elide_item(entry, budget) for entry in item]
    return item


def _build_view(body: dict[str, Any], *, cap: int) -> tuple[dict[str, Any], bool]:
    """Bound the wire body, and REPORT everything it dropped.

    Silent truncation is a correctness bug for agent consumers, so every cap
    reports what it left out and every elided value carries its digest and byte
    count.
    """
    budget = _ViewBudget(cap=cap)
    view = json.loads(json.dumps(body))
    truncated_any = False
    for section in view["sections"].values():
        accounting: dict[str, Any] = {}
        for name in DIFF_BUCKET_NAMES:
            items = section.get(name, [])
            total = len(items)
            kept = items[:cap]
            section[name] = [_elide_item(item, budget) for item in kept]
            accounting[name] = {
                "total": total,
                "returned": len(kept),
                "truncated": total > len(kept),
                # ``ambiguous`` and ``identity_conflict`` emit one RECORD per
                # bucket / per colliding id, each carrying per-side item lists,
                # while ``counts`` tallies the individual ROWS those records
                # cover. Naming the unit here is what stops a reader treating
                # "1 record" against "2 rows" as an accounting bug.
                "unit": "records" if name in _RECORD_SHAPED_BUCKETS else "items",
            }
            truncated_any = truncated_any or total > len(kept)
        section["view"] = accounting
    view["view"] = {
        "max_items_per_bucket": cap,
        "value_elision_bytes": VALUE_ELISION_BYTES,
        "elided_value_count": budget.elided,
        "truncated": truncated_any,
    }
    return view, (not truncated_any and budget.elided == 0)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _artifact_dir(instance: InstanceProtocol) -> Path:
    return instance.get_instance_dir() / DIFF_ARTIFACT_DIRNAME


def _artifact_path(instance: InstanceProtocol, diff_digest: str) -> Path:
    # The digest's HEX is the filename; the ``sha256:`` prefix stays on the
    # value. Content-addressed either way -- re-hashing the file's bytes
    # reproduces the digest whose hex names it -- and path-safe on every
    # platform, which a literal ``sha256:...`` filename is not.
    return _artifact_dir(instance) / f"{_digest_hex(diff_digest)}.json"


def _digest_hex(diff_digest: str) -> str:
    hex_part = diff_digest.split(":", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{64}", hex_part):
        raise ConfigError(
            f"'{diff_digest}' is not a diff digest. Diff digests are 'sha256:' followed "
            "by 64 hex characters, exactly as `cruxible state diff` reports them."
        )
    return hex_part


def _persist_artifact(
    instance: InstanceProtocol,
    *,
    diff_digest: str,
    payload: bytes,
) -> StateDiffArtifactRef:
    """Persist EVERY diff, content-addressed, or fail the read.

    The plan-artifact role means "the plan I reviewed" must be re-obtainable to
    be checked against a pin later. If only oversized diffs were persisted, a
    small diff pinned by an outcome contract would have no retrievable
    preimage, and the pin would be an assertion about bytes nobody can produce.
    Content addressing makes repeats free and makes tampering self-evident.
    Retention is deliberately NOT garbage-collected in v1.

    Written through a sibling temp file and ``os.replace`` (the precedent is
    ``service_backup_instance``): the final name is content-addressed, so a
    reader is entitled to assume the bytes under it hash to the name it asked
    for. A direct ``write_bytes`` can leave a truncated file under exactly that
    name after a crash or a full disk -- a partial artifact masquerading as a
    complete plan. ``os.replace`` is atomic within a filesystem, so the final
    name only ever names complete bytes.
    """
    path = _artifact_path(instance, diff_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(payload)
    os.replace(temp_path, path)
    return StateDiffArtifactRef(
        path=str(path),
        diff_digest=diff_digest,
        byte_count=len(payload),
    )


def _record_receipt(
    instance: InstanceProtocol,
    *,
    from_payload: dict[str, Any],
    to_payload: dict[str, Any],
    selector: dict[str, Any],
    diff_digest: str,
    artifact_ref: StateDiffArtifactRef,
) -> str:
    """Persist the diff receipt, or FAIL THE READ.

    ``parameters`` is a first-class receipt column that ``list_receipts``
    returns decoded and ``operation_type`` is indexed and filterable, so
    putting both coordinates and the digest there makes "every diff taken
    against release R" listable with zero schema change.
    """
    builder = ReceiptBuilder(
        query_name="state_diff",
        operation_type="state_diff",
        parameters={
            "from": from_payload,
            "to": to_payload,
            "selector": selector,
            "diff_digest": diff_digest,
        },
    )
    builder.stamp_state_coordinates(
        head_snapshot_id=instance.get_head_snapshot_id(),
        read_revision=instance.get_read_revision(),
    )
    builder.mark_committed()
    receipt = builder.build(
        results=[
            {
                "diff_digest": diff_digest,
                "artifact_path": artifact_ref.path,
                "artifact_byte_count": artifact_ref.byte_count,
            }
        ]
    )
    with instance.write_transaction() as uow:
        uow.receipts.save_receipt(receipt)
    return receipt.receipt_id


# ---------------------------------------------------------------------------
# Service entry points
# ---------------------------------------------------------------------------


def service_state_diff(
    instance: InstanceProtocol,
    *,
    from_coordinate: str | None = None,
    to_coordinate: str | None = None,
    sections: tuple[str, ...] | None = None,
    entity_types: tuple[str, ...] | None = None,
    relationship_types: tuple[str, ...] | None = None,
    buckets: tuple[str, ...] | None = None,
    changed_only: bool = False,
    max_items_per_bucket: int = DEFAULT_BUCKET_CAP,
) -> StateDiffResult:
    """Compare two state coordinates and persist the canonical plan artifact."""
    if max_items_per_bucket < 1:
        # A cap of 0 returns every bucket empty while the counts still say what
        # was found -- a view that reports nothing and looks complete enough to
        # skim. Refuse rather than emit it; the honest way to ask for counts
        # only is to read `summary`.
        raise ConfigError(
            f"max_items_per_bucket must be at least 1 (got {max_items_per_bucket}). "
            "A zero cap returns an empty view of a non-empty diff; read `summary` "
            "for counts without items."
        )
    selector = _normalized_selector(
        sections=sections,
        entity_types=entity_types,
        relationship_types=relationship_types,
        buckets=buckets,
        changed_only=changed_only,
    )
    from_spec, to_spec, default_basis = resolve_default_coordinates(
        instance, from_coordinate, to_coordinate
    )
    selected_sections = frozenset(selector["sections"] or ALL_SECTIONS)

    resolved_from = resolve_state_coordinate(
        instance,
        from_spec,
        sections=selected_sections,
        default_basis=default_basis,
    )
    # ``current -> current`` resolves ONCE and reuses the resolution for both
    # sides, so a self-diff cannot manufacture drift against itself.
    resolved_to = (
        resolved_from
        if to_spec == from_spec
        else resolve_state_coordinate(instance, to_spec, sections=selected_sections)
    )

    # If EITHER side's basis is unknown, ownership is unknown on BOTH sides for
    # the whole diff: mixing a pinned basis with a recomputed one produces
    # annotations that are individually plausible and jointly meaningless.
    ownership_known = resolved_from.ownership.is_known and resolved_to.ownership.is_known
    from_basis = _reconciled_basis(resolved_from.ownership, resolved_to, known=ownership_known)
    to_basis = _reconciled_basis(resolved_to.ownership, resolved_from, known=ownership_known)
    from_side = GraphDiffSide(
        graph=resolved_from.graph,
        ownership=from_basis,
        claim_identity_map_digest=resolved_from.claim_identity_map_digest,
    )
    to_side = GraphDiffSide(
        graph=resolved_to.graph,
        ownership=to_basis,
        claim_identity_map_digest=resolved_to.claim_identity_map_digest,
    )

    _validate_type_filters(selector, resolved_from, resolved_to)
    graph_selector = GraphDiffSelector(
        entity_types=(
            frozenset(selector["entity_types"]) if selector["entity_types"] is not None else None
        ),
        relationship_types=(
            frozenset(selector["relationship_types"])
            if selector["relationship_types"] is not None
            else None
        ),
        buckets=frozenset(selector["buckets"]) if selector["buckets"] is not None else None,
        changed_only=selector["changed_only"],
    )

    built: dict[str, SectionDiff] = {}
    omitted: list[dict[str, Any]] = []
    for name in ALL_SECTIONS:
        if name not in selected_sections:
            continue
        from_status = resolved_from.sections[name]
        to_status = resolved_to.sections[name]
        if from_status != "available" or to_status != "available":
            omitted.append(
                {
                    "section": name,
                    "from_status": from_status,
                    "to_status": to_status,
                    "side": (
                        "both"
                        if from_status != "available" and to_status != "available"
                        else ("from" if from_status != "available" else "to")
                    ),
                }
            )
            continue
        if name == "entities":
            built[name] = diff_entities(from_side, to_side, graph_selector)
        elif name == "edges":
            built[name] = diff_edges(from_side, to_side, graph_selector)
        else:
            built[name] = diff_procedures(
                resolved_from.load_procedures(),
                resolved_to.load_procedures(),
                graph_selector,
            )

    normalizations = [*resolved_from.normalizations, *resolved_to.normalizations]
    artifact_trust = (
        "unverified_upstream"
        if "unverified_legacy" in {resolved_from.verification, resolved_to.verification}
        else "verified"
    )
    body = _build_logical_body(
        from_coordinate=resolved_from,
        to_coordinate=resolved_to,
        from_ownership=from_basis,
        to_ownership=to_basis,
        selector=selector,
        sections=built,
        omitted=omitted,
        artifact_trust=artifact_trust,
        normalizations=normalizations,
    )
    # ONE representation for the digest AND the persisted bytes: the artifact
    # body is the preimage. ``edge_key`` is DURABLE but semantically
    # meaningless across re-serialization, so a diagnostics-carrying preimage
    # would make two artifacts over identical semantic state carry different
    # digests -- the plan-artifact role's whole point, inverted. Diagnostics
    # survive on the returned view, where they are for a human's eyes and no
    # digest depends on them.
    preimage = _without_item_diagnostics(body)
    payload = canonical_json(preimage).encode("utf-8")
    diff_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    artifact_ref = _persist_artifact(instance, diff_digest=diff_digest, payload=payload)
    view, artifact_complete = _build_view(body, cap=max_items_per_bucket)
    receipt_id = _record_receipt(
        instance,
        from_payload=body["from"],
        to_payload=body["to"],
        selector=selector,
        diff_digest=diff_digest,
        artifact_ref=artifact_ref,
    )
    return StateDiffResult(
        diff_digest=diff_digest,
        view_digest=_digest_of(view),
        artifact_complete=artifact_complete,
        artifact_ref=artifact_ref,
        diff_engine_version=DIFF_ENGINE_VERSION,
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        artifact_trust=body["artifact_trust"],
        normalizations=body["normalizations"],
        liveness=body["liveness"],
        default_basis=default_basis,
        selector=selector,
        from_coordinate=body["from"],
        to_coordinate=body["to"],
        omitted_sections=body["omitted_sections"],
        context=body["context"],
        sections=view["sections"],
        summary=body["summary"],
        view=view["view"],
        receipt_id=receipt_id,
    )


def service_state_diff_artifact(
    instance: InstanceProtocol,
    diff_digest: str,
) -> StateDiffArtifactResult:
    """Return one persisted diff artifact's exact bytes, content-addressed.

    The read RE-DIGESTS. Content addressing only makes tampering self-evident
    if somebody actually checks, and the only party positioned to check before
    the bytes are used is the reader that just loaded them. A file whose
    contents no longer hash to the name it sits under is not a stale artifact,
    it is a corrupted or substituted one, and returning it would launder an
    edited plan into a verified-looking answer.
    """
    path = _artifact_path(instance, diff_digest)
    if not path.exists():
        raise ConfigError(
            f"No persisted diff artifact for digest '{diff_digest}'. Diff artifacts are "
            "never garbage-collected, but one may predate this instance's current "
            f"'{DIFF_ARTIFACT_DIRNAME}' directory (a restore, relocate, or clone does not "
            "carry them). Re-run the diff to reproduce it."
        )
    payload = path.read_bytes()
    actual = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual != diff_digest:
        raise ConfigError(
            f"Persisted diff artifact at {path} does not match the digest it is stored "
            f"under: requested {diff_digest}, its bytes hash to {actual}. Diff artifacts "
            "are content-addressed and immutable, so this file was edited or replaced "
            "after it was written. It is refused rather than returned; re-run the diff "
            "to reproduce the artifact from state."
        )
    return StateDiffArtifactResult(
        diff_digest=diff_digest,
        path=str(path),
        byte_count=len(payload),
        content_bytes=payload.decode("utf-8"),
        content=json.loads(payload.decode("utf-8")),
    )


__all__ = [
    "ALL_SECTIONS",
    "ARTIFACT_SCHEMA_VERSION",
    "DEFAULT_BUCKET_CAP",
    "DIFF_ENGINE_VERSION",
    "RESERVED_COORDINATES",
    "VALUE_ELISION_BYTES",
    "ResolvedStateCoordinate",
    "parse_state_coordinate",
    "resolve_default_coordinates",
    "resolve_state_coordinate",
    "service_state_diff",
    "service_state_diff_artifact",
]
