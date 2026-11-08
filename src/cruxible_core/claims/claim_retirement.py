"""Attributed Claim retirement over the complete Claim dependency closure."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_evidence import AbsenceWitness
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimRetireDependent,
    ClaimRetirementAttribution,
    ClaimRetirementReason,
    ClaimStatement,
    claim_artifact_digest,
    claim_path,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.claims.closure import (
    ArtifactDependencyStateV1,
    ReversePinClosureItem,
    SelectedReverseNeighbors,
    _walk_reverse_pin_closure,
    reverse_pin_closure,
    selected_reverse_neighbors,
)
from cruxible_core.derived.derived_state import CandidateTree, fork_tree
from cruxible_core.runtime.instance import PlaybillInstance


class _StrictRetirementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimRetireInventoryItemV1(_StrictRetirementModel):
    artifact_identity: ArtifactIdentity
    predecessor_digest: str
    triggering_identity: ArtifactIdentity
    triggering_edge_roles: tuple[str, ...]


class ClaimRetirementResultItemV1(_StrictRetirementModel):
    artifact_identity: ArtifactIdentity
    predecessor_digest: str
    reason: ClaimRetirementReason
    effective_until: datetime | None
    successor_digest: str


class ClaimRetireError(FormatError):
    error_code = "cruxible.claim.retire_invalid"


class ClaimRetireClosureMismatch(ClaimRetireError):
    error_code = "cruxible.claim.retire_closure_mismatch"


class ClaimRetireDependentUnsupported(ClaimRetireError):
    error_code = "cruxible.claim.retire_dependent_unsupported"


class ClaimRetireStale(ClaimRetireError):
    error_code = "cruxible.claim.retire_stale"


def _inventory(
    closure: tuple[ReversePinClosureItem, ...],
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    return tuple(
        ClaimRetireInventoryItemV1(
            artifact_identity=item.state.identity,
            predecessor_digest=item.state.artifact_digest,
            triggering_identity=item.triggering_identity,
            triggering_edge_roles=item.dependency_edge_roles,
        )
        for item in closure
        if item.state.artifact_kind == "claim"
    )


def _retired_claim(
    claim: ClaimArtifact,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None,
    successor_digests: Mapping[str, str],
    absence: AbsenceWitness | None = None,
) -> ClaimArtifact:
    statement = claim.statement
    if effective_until is not None:
        try:
            statement = ClaimStatement.model_validate(
                {
                    **statement.model_dump(mode="python"),
                    "effective_until": effective_until,
                }
            )
        except ValidationError as exc:
            raise ClaimRetireError(
                f"{ClaimRetireError.error_code}: effective_until produces an invalid Claim "
                "effective interval"
            ) from exc
    pins = tuple(
        pin.model_copy(update={"artifact_digest": successor_digests[pin.target.qualified]})
        if pin.target.kind == "Claim" and pin.target.qualified in successor_digests
        else pin
        for pin in claim.pins
    )
    return ClaimArtifact(
        identity=claim.identity,
        statement=statement,
        backing=claim.backing,
        pins=pins,
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=claim_artifact_digest(claim).tagged,
        ),
        retirement=ClaimRetirementAttribution(reason=reason, absence=absence),
    )


def build_claim_retirement_candidate(
    tree: Mapping[str, bytes],
    *,
    root: ClaimRetireDependent,
    dependents: tuple[ClaimRetireDependent, ...],
) -> tuple[CandidateTree, tuple[ClaimRetirementResultItemV1, ...]]:
    requests = {item.artifact_identity.qualified: item for item in (root, *dependents)}
    claims = {
        identity: parse_claim(
            tree[claim_path(item.artifact_identity.name)],
            path=claim_path(item.artifact_identity.name),
        )
        for identity, item in requests.items()
    }
    for identity, item in requests.items():
        if claim_artifact_digest(claims[identity]).tagged != item.predecessor_digest:
            raise ClaimRetireStale(
                f"{ClaimRetireStale.error_code}: predecessor changed for {identity}"
            )

    candidate_tree = fork_tree(tree)
    successor_digests: dict[str, str] = {}
    successors: dict[str, ClaimArtifact] = {}
    pending = set(requests)
    while pending:
        progressed = False
        for identity in sorted(pending, key=lambda item: item.encode("utf-8")):
            claim = claims[identity]
            unresolved_targets = {
                pin.target.qualified
                for pin in claim.pins
                if pin.target.kind == "Claim" and pin.target.qualified in pending
            }
            if unresolved_targets:
                continue
            request = requests[identity]
            successor = _retired_claim(
                claim,
                reason=request.reason,
                absence=request.absence,
                effective_until=request.effective_until,
                successor_digests=successor_digests,
            )
            path = claim_path(claim.identity.name)
            candidate_tree[path] = render_claim(successor)
            successors[identity] = successor
            successor_digests[identity] = claim_artifact_digest(successor).tagged
            pending.remove(identity)
            progressed = True
            break
        if not progressed:
            raise ClaimRetireClosureMismatch(
                f"{ClaimRetireClosureMismatch.error_code}: Claim-target pin cycle"
            )
    results = tuple(
        ClaimRetirementResultItemV1(
            artifact_identity=requests[identity].artifact_identity,
            predecessor_digest=requests[identity].predecessor_digest,
            reason=requests[identity].reason,
            effective_until=successors[identity].statement.effective_until,
            successor_digest=successor_digests[identity],
        )
        for identity in sorted(successors, key=lambda item: item.encode("utf-8"))
    )
    return candidate_tree, results


def claim_retirement_inventory(
    instance: PlaybillInstance,
    *,
    tree: Mapping[str, bytes],
    coordinate: AcceptedCoordinate,
    claim: ClaimArtifact,
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    """Return the live Claim closure one retirement must carry, or refuse it.

    The change-set retirement member demands exactly this closure over exactly
    the tree the retirement is being written onto; the retire verb computes it
    for the writer from the same function.
    """

    from cruxible_core.derived.derived_state import snapshot_against

    tree = snapshot_against(tree, instance.immutable_tree_at(coordinate.git_oid))
    with instance.accepted_history_reader(at=coordinate) as history:

        def resolve(digest: str) -> ArtifactIdentity | None:
            try:
                location = history.artifact(digest)
            except FormatError as exc:
                raise ClaimRetireError(
                    "one accepted Claim digest names multiple identities"
                ) from exc
            if location is None:
                return None
            kind, name = location.identity.split(":", 1)
            if kind != "Claim":
                raise ClaimRetireError("accepted Claim input digest names another artifact kind")
            return ArtifactIdentity(kind="Claim", name=name)

        closure = reverse_pin_closure(
            tree,
            root=claim.identity,
            include=lambda state: state.lifecycle.state == "live",
            resolve_claim_digest=resolve,
        )
    return _supported_inventory(closure)


def _supported_inventory(
    closure: tuple[ReversePinClosureItem, ...],
) -> tuple[ClaimRetireInventoryItemV1, ...]:
    unsupported = tuple(item for item in closure if item.state.artifact_kind != "claim")
    if unsupported:
        names = tuple(item.state.identity.qualified for item in unsupported)
        raise ClaimRetireDependentUnsupported(
            f"{ClaimRetireDependentUnsupported.error_code}: {names!r}"
        )
    return _inventory(closure)


def _live(state: ArtifactDependencyStateV1) -> bool:
    return state.lifecycle.state == "live"


class RetirementClosureWalker:
    """Successive retirement closures over one fixed tree T0.

    Member k of a change set's retirement stage is judged today on T_k: T0 with
    members 0..k-1's retired successors written. Walking T0 instead, with every
    identity those members retired (R) excluded, reaches the same closure:

    - I1: a member writes only its results' Claim paths, each a non-live
      successor of the same identity whose pin targets are its predecessor's.
      Every other node has the same bytes, so the same state, in T0 and T_k.
    - I2: an excluded node is a traversal boundary: it is never a member and is
      never expanded. In T_k a node of R is excluded because it is not live; here
      because it is in R. Nodes outside R are included in both exactly when live.

    So the walk expands the same nodes in the same order (a node's reverse
    neighbors differ between the trees only in nodes of R, which both skip) and
    returns the same items. ``record`` must see every successful member, in
    staging order, before the next member walks.
    """

    def __init__(
        self,
        neighbors: Callable[[str], tuple[tuple[ArtifactDependencyStateV1, tuple[str, ...]], ...]],
    ) -> None:
        self._neighbors = neighbors
        self._retired: set[str] = set()

    def closure(self, root: ArtifactIdentity) -> tuple[ReversePinClosureItem, ...]:
        retired = self._retired
        return _walk_reverse_pin_closure(
            root,
            lambda state: _live(state) and state.identity.qualified not in retired,
            self._neighbors,
        )

    def record(self, identities: Iterable[str]) -> None:
        self._retired.update(identities)


class ChangeRetirementClosures:
    """Every retirement member's closure in one change set, over one overlay.

    ``claim_retirement_inventory`` builds an overlay of the whole staged change
    per call; a change retiring n Claims would pay n overlays of its edits. The
    retirement stage is the last staging pass (asserted by the lowering) and
    each member writes only its results (asserted by ``record``), so the
    overlay of the tree the first member sees (T0) answers every member
    (``RetirementClosureWalker``). A tree no instance binds (no accepted
    reader) keeps today's per-member inventory. Open lazily; close once.
    """

    def __init__(self, instance: PlaybillInstance, *, coordinate: AcceptedCoordinate) -> None:
        self._instance = instance
        self._coordinate = coordinate
        self._selected: SelectedReverseNeighbors | None = None
        self._walker: RetirementClosureWalker | None = None
        self._cold = False

    def inventory(
        self, *, tree: Mapping[str, bytes], claim: ClaimArtifact
    ) -> tuple[ClaimRetireInventoryItemV1, ...]:
        """The live Claim closure this member must carry, or refuse it (as the per-member law)."""

        if self._walker is None and not self._cold:
            from cruxible_core.derived.derived_state import snapshot_against

            snapshot = snapshot_against(
                tree, self._instance.immutable_tree_at(self._coordinate.git_oid)
            )
            self._selected = selected_reverse_neighbors(snapshot)
            if self._selected is None:
                self._cold = True
            else:
                self._walker = RetirementClosureWalker(self._selected)
        if self._walker is None:
            return claim_retirement_inventory(
                self._instance, tree=tree, coordinate=self._coordinate, claim=claim
            )
        return _supported_inventory(self._walker.closure(claim.identity))

    def record(
        self,
        *,
        before: Mapping[str, bytes],
        after: Mapping[str, bytes],
        results: tuple[ClaimRetirementResultItemV1, ...],
    ) -> None:
        """Add one successful member's retirements to R, asserting I1 of its writes."""

        if self._walker is None:
            return
        written = {claim_path(item.artifact_identity.name): item for item in results}
        for path, item in written.items():
            content = after[path]
            successor = parse_claim(content, path=path)
            if (
                content == before.get(path)
                or successor.identity != item.artifact_identity
                or successor.lifecycle.state == "live"
            ):
                raise AssertionError(
                    f"a retirement member's write at {path!r} is not a retired successor"
                )
        if not _wrote_exactly(before, after, written):
            raise AssertionError("a retirement member wrote outside its results' Claim paths")
        self._walker.record(item.artifact_identity.qualified for item in results)

    def close(self) -> None:
        if self._selected is not None:
            self._selected.close()
            self._selected = None
        self._walker = None


def _wrote_exactly(
    before: Mapping[str, bytes], after: Mapping[str, bytes], written: Mapping[str, object]
) -> bool:
    """Whether ``after`` differs from ``before`` at exactly ``written`` (each already differs)."""

    if (
        isinstance(before, CandidateTree)
        and isinstance(after, CandidateTree)
        and before._parent is after._parent
    ):
        # Both hold their complete edits against one root: the edit count moves
        # by the written paths not edited before, and by nothing else.
        new = sum(1 for path in written if path not in before._edits)
        return len(after._edits) == len(before._edits) + new
    from cruxible_core.derived.derived_state import changed_paths

    return set(changed_paths(before, after)) == set(written)
