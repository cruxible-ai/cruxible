"""Delegated settle authority: one verifier for settlement and historical replay.

A change set that names a settle ProcedureMandate is accepted without candidate
approvals only when that mandate, read from the parent state, covers every
changed member and its pinned condition query holds for every target at that
same state. Proposal evaluation calls this for publication and for replay alike,
so a recorded settlement reproduces from the parent state and the record alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from cruxible_client.contracts.claims import ClaimFormatError, SubjectClaimObject, parse_claim
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.procedure_mandates import (
    MANDATE_CHANGE_KIND_ORDER,
    MandateChangeKind,
    ProcedureMandate,
    ProcedureMandateError,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    procedure_mandate_path,
)
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinition,
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import parse_subject
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.query.backends import ClaimQueryFactsV1

MANDATE_PREFIX = "procedure-mandates/"
CLAIM_PREFIX = "claims/"
SUBJECT_PREFIX = "subjects/"


@dataclass(frozen=True)
class SettleTarget:
    """One changed Claim, the change it makes, and the Subject its condition binds."""

    path: str
    claim_type: str
    change_kind: MandateChangeKind
    subject: SemanticAddress
    subject_identity: str
    subject_id: str


def _issue(code: str, message: str) -> tuple[str, str]:
    return f"cruxible.settle.{code}", message


#: Accepted Claim facts for exactly what one condition query reads: its
#: referenced predicates, and the Subjects of its entry kinds (plus those its
#: Claims name), as a served query reads them.
QueryFactsForPredicates = Callable[[tuple[str, ...], tuple[str, ...]], ClaimQueryFactsV1]


def _mandate_paths_by_digest(tree: Mapping[str, bytes], digest: str) -> tuple[str, ...] | None:
    """Where a mandate with ``digest`` can be in ``tree``, from its accepted projection.

    An accepted tree carries a reader for the projection it stands on; that
    projection's ``procedure_mandates`` rows plus every mandate path edited on
    top of it are the only places the digest can be. ``None`` when the tree has
    no reader (cold source-only ingress): the caller scans the mandate prefix.
    """

    reader = getattr(tree, "_accepted_reader", None)
    if reader is None:
        return None
    with reader() as projection:
        paths = {
            str(row[0])
            for row in projection.typed.connection.execute(
                "SELECT path FROM procedure_mandates WHERE artifact_digest=?", (digest,)
            )
        }
    node: object | None = tree
    while node is not None:
        edits = getattr(node, "_edits", None) or {}
        paths.update(path for path in edits if path.startswith(MANDATE_PREFIX))
        node = getattr(node, "_parent", None)
    return tuple(sorted(paths))


def _mandate_by_digest(
    tree: Mapping[str, bytes], digest: str, *, name: str | None = None
) -> ProcedureMandate | None:
    """The mandate at ``tree`` with ``digest``, read from the paths it can be at.

    A digest fixes the mandate's bytes and so its path; ``name`` (from a copy
    of the same mandate) or the tree's accepted projection says where to look,
    and the bytes there must still carry the digest. Only a tree with neither
    scans the mandate prefix.
    """

    paths = (
        (procedure_mandate_path(name),)
        if name is not None
        else _mandate_paths_by_digest(tree, digest)
    )
    if paths is None:
        paths = tuple(path for path in tree if path.startswith(MANDATE_PREFIX))
    for path in paths:
        content = tree.get(path)
        if content is None:
            continue
        try:
            mandate = parse_procedure_mandate_any(content, path=path)
        except ProcedureMandateError:
            continue
        if procedure_mandate_digest(mandate).tagged == digest:
            return mandate if isinstance(mandate, ProcedureMandate) else None
    return None


def _in_namespace(path: str, namespace: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in namespace)


def _created_subjects(
    scope: tuple[str, ...],
    *,
    subject_creation: tuple[str, ...],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    """The new Subjects a settle grant's ``subject_creation`` covers, with their keys.

    Subject path -> the key predicates of its kind. Any other Subject the change
    writes -- an existing one, or a new one of a kind the grant does not name --
    is an issue: settle authority creates Subjects only of the named kinds.
    """

    from cruxible_client.contracts.subject_kinds import live_subject_kind

    created: dict[str, tuple[str, ...]] = {}
    issues: list[tuple[str, str]] = []
    for path in scope:
        if not path.startswith(SUBJECT_PREFIX):
            continue
        kind = path.removeprefix(SUBJECT_PREFIX).partition("/")[0]
        content = candidate_tree.get(path)
        live = False
        if content is not None:
            try:
                live = parse_subject(content, path=path).lifecycle.state == "live"
            except (CruxibleError, ValueError):
                live = False
        if path in current_tree or kind not in subject_creation or not live:
            issues.append(
                _issue(
                    "scope_uncovered",
                    "Settle authority creates only new Subjects of the kinds its "
                    f"subject_creation names; {path!r} is not one.",
                )
            )
            continue
        declared = live_subject_kind(candidate_tree, kind)
        created[path] = () if declared is None else declared.keys
    return created, issues


def settle_targets(
    *,
    scope: tuple[str, ...],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    binding_roles: Mapping[str, str],
    subject_creation: tuple[str, ...] = (),
) -> tuple[tuple[SettleTarget, ...], tuple[tuple[str, str], ...]]:
    """Derive each changed Claim's target from the change itself, never from a caller.

    A new Subject of a kind the grant's ``subject_creation`` names, and each key
    Claim its creation writes, are covered by the creation and bind no target.
    """

    targets: list[SettleTarget] = []
    created, issues = _created_subjects(
        scope,
        subject_creation=subject_creation,
        current_tree=current_tree,
        candidate_tree=candidate_tree,
    )
    for path in scope:
        if path.startswith(SUBJECT_PREFIX):
            continue  # judged above
        if not path.startswith(CLAIM_PREFIX) or path not in candidate_tree:
            issues.append(
                _issue(
                    "scope_uncovered",
                    f"Settle authority reaches only accepted Claims; {path!r} is not one.",
                )
            )
            continue
        try:
            claim = parse_claim(candidate_tree[path], path=path)
        except ClaimFormatError:
            issues.append(_issue("scope_uncovered", f"{path!r} is not a parseable Claim."))
            continue
        statement = claim.statement
        claim_type = statement.claim_type.qualified
        change_kind: MandateChangeKind = (
            "create"
            if path not in current_tree
            else "retire"
            if claim.lifecycle.state == "retired"
            else "revise"
        )
        if (
            change_kind == "create"
            and statement.qualifier is None
            and statement.predicate in created.get(statement.subject.artifact_path, ())
        ):
            continue  # a key Claim of a Subject the change creates
        role = binding_roles.get(claim_type, "subject")
        if role == "object":
            if not isinstance(statement.object, SubjectClaimObject):
                issues.append(
                    _issue("target_unbound", f"Claim {path!r} has no object Subject to bind.")
                )
                continue
            address = statement.object.address
        else:
            address = statement.subject
        subject_path = address.artifact_path
        content = candidate_tree.get(subject_path)
        if content is None:
            issues.append(_issue("target_unbound", f"Claim {path!r} binds no accepted Subject."))
            continue
        shell = parse_subject(content, path=subject_path)
        targets.append(
            SettleTarget(
                path=path,
                claim_type=claim_type,
                change_kind=change_kind,
                subject=address,
                subject_identity=shell.identity.qualified,
                subject_id=shell.subject_id,
            )
        )
    return tuple(targets), tuple(issues)


@dataclass(frozen=True)
class DelegatedAuthorityVerdict:
    """One settle condition check, carried to an evaluation that would repeat it.

    The check reads only the parent state (named by its coordinate), the
    evaluation timestamp, the change set's scope and the candidate bytes at
    those paths -- any other path the check reads is unchanged, so the parent
    holds it. An evaluation with exactly those inputs reuses the issues
    instead of evaluating the condition again; any other input re-checks.
    """

    mandate_digest: str
    current: AcceptedProjectionCoordinate
    timestamp: str
    scope: tuple[str, ...]
    members: tuple[bytes | None, ...]
    issues: tuple[tuple[str, str], ...]

    @classmethod
    def of(
        cls,
        *,
        mandate_digest: str,
        current: AcceptedProjectionCoordinate,
        timestamp: str,
        scope: tuple[str, ...],
        candidate_tree: Mapping[str, bytes],
        issues: tuple[tuple[str, str], ...],
    ) -> DelegatedAuthorityVerdict:
        return cls(
            mandate_digest=mandate_digest,
            current=current,
            timestamp=timestamp,
            scope=scope,
            members=tuple(candidate_tree.get(path) for path in scope),
            issues=issues,
        )

    def answers(
        self,
        *,
        mandate_digest: str,
        current: AcceptedProjectionCoordinate,
        timestamp: str,
        scope: tuple[str, ...],
        candidate_tree: Mapping[str, bytes],
    ) -> bool:
        """Whether this check was made on exactly these inputs."""
        return (
            mandate_digest == self.mandate_digest
            and timestamp == self.timestamp
            and scope == self.scope
            and current == self.current
            and all(
                candidate_tree.get(path) == content
                for path, content in zip(scope, self.members, strict=True)
            )
        )


def delegated_authority_issues(
    *,
    mandate_digest: str,
    scope: tuple[str, ...],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    timestamp: str,
    facts: QueryFactsForPredicates | None,
    mandate_name: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Every reason the named mandate does not authorize this change set; empty if it does.

    Everything is read from the parent state: a candidate can never make itself
    eligible. False, missing, conflicting, truncated or ambiguous facts refuse.
    ``facts`` builds the parent's Claim facts for the predicates the condition
    query reads; ``mandate_name`` reads the mandate from its one path.
    """

    mandate = _mandate_by_digest(current_tree, mandate_digest, name=mandate_name)
    if mandate is None:
        return (
            _issue(
                "mandate_unresolved",
                "No accepted ProcedureMandate v2 at the parent has the recorded digest.",
            ),
        )
    evaluated_at = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc
    )
    targets, found = mandate_coverage(
        mandate,
        scope=scope,
        current_tree=current_tree,
        candidate_tree=candidate_tree,
        evaluated_at=evaluated_at,
    )
    if found:
        return found
    return condition_issues(
        mandate,
        targets=targets,
        current_tree=current_tree,
        current=current,
        evaluated_at=evaluated_at,
        facts=facts,
    )


def mandate_coverage(
    mandate: ProcedureMandate,
    *,
    scope: tuple[str, ...],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    evaluated_at: datetime,
) -> tuple[tuple[SettleTarget, ...], tuple[tuple[str, str], ...]]:
    """Whether a live settle grant covers every changed member, and the targets it binds."""

    if mandate.grants != "settle" or mandate.condition is None:
        return (), (_issue("mandate_not_settle", "The mandate grants no settle authority."),)
    if mandate.lifecycle.state != "live":
        return (), (_issue("mandate_retired", "The settle mandate is retired."),)
    if mandate.suspended:
        return (), (_issue("mandate_suspended", "The settle mandate is suspended."),)
    if not (mandate.valid_from <= evaluated_at < mandate.expires_at):
        return (), (
            _issue("mandate_expired", "The settle mandate is outside its validity window."),
        )
    targets, issues = settle_targets(
        scope=scope,
        current_tree=current_tree,
        candidate_tree=candidate_tree,
        binding_roles={
            item.claim_type.target.qualified: item.binding_subject_role for item in mandate.scope
        },
        subject_creation=mandate.subject_creation,
    )
    found = [*issues, *(issue for _gap, issue, _target in _target_gaps(mandate, targets))]
    if not found and not targets and not any(path.startswith(SUBJECT_PREFIX) for path in scope):
        found.append(_issue("scope_uncovered", "The change set changes no Claim."))
    return targets, tuple(found)


_TargetGap = Literal["namespace", "claim_type", "change_kind", "subject"]


def _target_gaps(
    mandate: ProcedureMandate, targets: tuple[SettleTarget, ...]
) -> list[tuple[_TargetGap, tuple[str, str], SettleTarget]]:
    """Each target a settle grant's scope does not cover, with the term that left it out."""

    scope_by_type = {item.claim_type.target.qualified: item for item in mandate.scope}
    gaps: list[tuple[_TargetGap, tuple[str, str], SettleTarget]] = []
    for target in targets:
        item = scope_by_type.get(target.claim_type)
        if not _in_namespace(target.path, mandate.namespace):
            gaps.append(
                (
                    "namespace",
                    _issue("scope_uncovered", f"{target.path!r} is outside the namespace."),
                    target,
                )
            )
        elif item is None:
            gaps.append(
                (
                    "claim_type",
                    _issue("scope_uncovered", f"ClaimType {target.claim_type} is not in scope."),
                    target,
                )
            )
        elif target.change_kind not in item.change_kinds:
            allowed = ", ".join(k for k in MANDATE_CHANGE_KIND_ORDER if k in item.change_kinds)
            gaps.append(
                (
                    "change_kind",
                    _issue(
                        "scope_uncovered",
                        f"A {target.change_kind} of {target.claim_type} is not covered "
                        f"(covered: {allowed}).",
                    ),
                    target,
                )
            )
        elif mandate.subject_scope is not None and target.subject not in mandate.subject_scope:
            gaps.append(
                (
                    "subject",
                    _issue(
                        "scope_uncovered", f"Subject {target.subject_identity} is not in scope."
                    ),
                    target,
                )
            )
    return gaps


def claim_types_outside_scope(
    mandate: ProcedureMandate,
    *,
    scope: tuple[str, ...],
    current_tree: Mapping[str, bytes],
    candidate_tree: Mapping[str, bytes],
    evaluated_at: datetime,
) -> tuple[str, ...] | None:
    """The ClaimTypes a live settle grant leaves out, when they are its ONLY gap.

    ``None`` unless the grant is live and every changed Claim is a settle target
    inside its namespace, change kinds and Subject scope, apart from Claims whose
    ClaimType the scope does not name -- or retirements of a ClaimType whose
    scope does not list ``retire`` (a Procedure's retirement item settles only
    under a grant that explicitly lists it). Those ClaimTypes are returned,
    sorted; an empty result means the grant covers the change. A settle
    terminal proposes instead of settling a change carrying such a ClaimType
    (per-rule trust).
    """

    targets, issues = mandate_coverage(
        mandate,
        scope=scope,
        current_tree=current_tree,
        candidate_tree=candidate_tree,
        evaluated_at=evaluated_at,
    )
    if not issues:
        return ()
    if not targets:
        return None
    # Every issue is one target's gap only when no changed member failed to be a
    # target at all (not a Claim, unparseable, or binding no Subject).
    gaps = _target_gaps(mandate, targets)
    if len(issues) != len(gaps) or not all(
        gap == "claim_type" or (gap == "change_kind" and target.change_kind == "retire")
        for gap, _detail, target in gaps
    ):
        return None
    outside = {target.claim_type for _gap, _detail, target in gaps}
    return tuple(sorted(outside, key=lambda item: item.encode("utf-8")))


def condition_issues(
    mandate: ProcedureMandate,
    *,
    targets: tuple[SettleTarget, ...],
    current_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    evaluated_at: datetime,
    facts: QueryFactsForPredicates | None,
) -> tuple[tuple[str, str], ...]:
    """Evaluate the pinned condition query for every target at the parent state."""

    from cruxible_core.indexes.subject_aliases import tree_reference_resolver
    from cruxible_core.query.engine import evaluate_claim_query

    assert mandate.condition is not None  # mandate_coverage requires a settle grant
    found: list[tuple[str, str]] = []
    condition = mandate.condition
    query_path = query_definition_path(condition.query.target.name)
    content = current_tree.get(query_path)
    definition = None if content is None else parse_query_definition(content, path=query_path)
    if definition is None or query_definition_digest(definition).tagged != (
        condition.query.artifact_digest
    ):
        return (
            _issue(
                "condition_query_unresolved",
                "The pinned condition query is not accepted at the parent.",
            ),
        )
    query_facts = (
        None
        if facts is None
        else facts(definition.referenced_predicates, definition.entry.subject_kinds)
    )
    if query_facts is None or query_facts.coordinate != current:
        return (
            _issue(
                "condition_incomplete",
                "Accepted query facts for the parent are unavailable.",
            ),
        )
    accepted = AcceptedQueryDefinition(
        path=query_path, query=definition, artifact_digest=condition.query.artifact_digest
    )
    # A Subject reference the condition compares resolves at the parent exactly
    # as at every other entry point; the pinned definition is never rewritten.
    resolve_reference = tree_reference_resolver(current_tree)
    for target in targets:
        result = evaluate_claim_query(
            accepted,
            facts=query_facts,
            coordinate=current,
            evaluation_time=evaluated_at,
            resolve_reference=resolve_reference,
            parameters={
                **condition.fixed_parameters,
                condition.binding_parameter: target.subject_id,
            },
        )
        truncation = result.truncation
        if (
            result.verdict != "completed"
            or result.conflicts
            or truncation.clipped_budgets
            or truncation.truncated_includes
            or truncation.returned_result_count != truncation.candidate_result_count
        ):
            found.append(
                _issue(
                    "condition_incomplete",
                    f"The condition for {target.subject_identity} is refused, conflicted "
                    "or truncated.",
                )
            )
            continue
        if len(result.rows) != 1:
            found.append(
                _issue(
                    "condition_false" if not result.rows else "condition_incomplete",
                    f"The condition for {target.subject_identity} returned "
                    f"{len(result.rows)} rows; exactly one is required.",
                )
            )
            continue
        row = result.rows[0]
        if row.result_subject_identity != target.subject_identity:
            found.append(
                _issue(
                    "condition_false",
                    f"The condition row is not the changed target {target.subject_identity}.",
                )
            )
            continue
        fields = {field.name: field for field in row.fields}
        missing = [
            name
            for name in condition.required_fields
            if name not in fields or fields[name].state != "present" or fields[name].value is None
        ]
        if row.conflicts or missing:
            found.append(
                _issue(
                    "condition_incomplete",
                    f"The condition for {target.subject_identity} lacks "
                    f"{', '.join(missing) or 'a conflict-free row'}.",
                )
            )
    return tuple(found)


__all__ = [
    "SettleTarget",
    "condition_issues",
    "mandate_coverage",
    "delegated_authority_issues",
    "settle_targets",
]
