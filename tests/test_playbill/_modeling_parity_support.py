"""Shared fixture builders for the PC-F semantic modeling-parity suite.

These helpers build accepted Subject/Claim facts and governed QueryDefinitions
for the three donor domains PC-F draws its parity fixtures from: project-domain,
agent-operation, and one business domain (supply-chain blast radius).

Nothing here imports a donor module. The suite that consumes these builders is
the surviving half of the parity oracle: it keeps asserting Claim-native meaning
against the pinned donor expectations after the donor island is purged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import GenerationRoot, SemanticRoot
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claim_verdicts import (
    ClaimAdjudicationRuleV1,
    claim_adjudication_rule,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifactV2,
    ClaimBackingV2,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.query.definitions import (
    AcceptedQueryDefinitionV1,
    QueryDefinitionV1,
    query_definition_digest,
    query_definition_path,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import (
    AcceptedSubject,
    SubjectShell,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.compiler import PC_E1_COMPILER
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import ClaimFactRowV1, ClaimQueryFactsV1
from cruxible_core.playbill.query.engine import ClaimQueryResultV1

PARITY_AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))
AUTHORITY_BASIS = ("authority:owner",)
OBSERVED_AT = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
EVALUATION_TIME = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def instant(day: int, hour: int) -> str:
    """Return one canonical UTC timestamp literal for a Claim object value."""

    return datetime(2026, 8, day, hour, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def subject(kind: str, identifier: str) -> AcceptedSubject:
    """Return one accepted Subject shell for a domain kind and identifier."""

    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{identifier}"),
        subject_kind=kind,
        subject_id=identifier,
        authority=PARITY_AUTHORITY,
    )
    return AcceptedSubject(
        path=subject_path(kind, identifier),
        shell=shell,
        artifact_digest=subject_digest(shell).tagged,
    )


def claim_type(
    predicate: str,
    *,
    subject_kinds: tuple[str, ...],
    object_subject_kinds: tuple[str, ...] = (),
) -> ClaimType:
    """Return the ClaimType one parity predicate is read under.

    A literal predicate is one-cardinality (a work item has ONE status), which
    is what makes competing accepted Claims a typed conflict rather than a
    silent overwrite. A Subject-valued predicate is many-cardinality: a note can
    be about several work items.
    """

    relation = bool(object_subject_kinds)
    cardinality = "many" if relation else "one"
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=byte_sorted(subject_kinds),
        object_kind="subject" if relation else "literal",
        literal_schema=None if relation else {"type": "string"},
        allowed_object_subject_kinds=byte_sorted(object_subject_kinds),
        cardinality=cardinality,
        permitted_roles=("normative",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality=cardinality,
            eligible_verdicts=("supported",),
            selector="all" if relation else "only_contender",
        ),
        authority=PARITY_AUTHORITY,
    )


def claim_type_pin(
    predicate: str,
    *,
    subject_kinds: tuple[str, ...],
    object_subject_kinds: tuple[str, ...] = (),
) -> ArtifactPin:
    """Return the digest pin a QueryDefinition carries for one referenced predicate."""

    contract = claim_type(
        predicate,
        subject_kinds=subject_kinds,
        object_subject_kinds=object_subject_kinds,
    )
    return ArtifactPin(
        role="claim-type",
        target=contract.identity,
        artifact_digest=claim_type_digest(contract).tagged,
    )


def adjudication_rule(contract: ClaimType) -> ClaimAdjudicationRuleV1:
    """Return the replayable verdict rule for one ClaimType."""

    return claim_adjudication_rule(
        contract,
        claim_type_digest=claim_type_digest(contract).tagged,
    )


def claim_fact(
    index: int,
    *,
    subject_row: AcceptedSubject,
    predicate: str,
    value: str | AcceptedSubject,
    object_subject_kinds: tuple[str, ...] = (),
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> ClaimFactRowV1:
    """Return one accepted Claim fact row carrying its own verdict inputs."""

    relation = isinstance(value, AcceptedSubject)
    obj: LiteralClaimObject | SubjectClaimObject = (
        SubjectClaimObject(address=SemanticAddress.whole_artifact(value.path))
        if isinstance(value, AcceptedSubject)
        else LiteralClaimObject(value=value)
    )
    contract = claim_type(
        predicate,
        subject_kinds=(subject_row.shell.subject_kind,),
        object_subject_kinds=object_subject_kinds if relation else (),
    )
    digest = claim_type_digest(contract).tagged
    claim_id = f"CLM-{index:032x}"
    artifact = ClaimArtifactV2(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_row.path),
            claim_type=contract.identity,
            claim_type_digest=digest,
            predicate=predicate,
            object=obj,
            role="normative",
            effective_from=effective_from,
            effective_until=effective_until,
        ),
        backing=ClaimBackingV2(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_row.artifact_digest,
                observed_at=OBSERVED_AT,
            ),
        ),
        authority=PARITY_AUTHORITY,
        pins=(ArtifactPin(role="claim-type", target=contract.identity, artifact_digest=digest),),
    )
    return ClaimFactRowV1(
        accepted=AcceptedClaim(
            path=claim_path(claim_id),
            claim=artifact,
            statement_digest=claim_statement_digest(artifact.statement).tagged,
            artifact_digest=claim_artifact_digest(artifact).tagged,
        ),
        rule=adjudication_rule(contract),
        resolved_authority_basis=AUTHORITY_BASIS,
    )


def coordinate(domain: str) -> AcceptedProjectionCoordinate:
    """Return the fixed accepted coordinate one parity domain is read at."""

    return AcceptedProjectionCoordinate(
        instance_id=f"inst_parity_{domain}",
        repository_path=f"/tmp/parity/{domain}",
        git_object_format="sha1",
        git_oid="44" * 20,
        semantic_root=SemanticRoot("55" * 32).tagged,
        generation_root=GenerationRoot("33" * 32).tagged,
        compiler=PC_E1_COMPILER,
    )


def facts(
    domain: str,
    subjects: tuple[AcceptedSubject, ...],
    claims: tuple[ClaimFactRowV1, ...],
) -> ClaimQueryFactsV1:
    """Return the canonically ordered accepted facts one parity query may read."""

    return ClaimQueryFactsV1(
        coordinate=coordinate(domain),
        subjects=tuple(sorted(subjects, key=lambda item: item.path.encode("utf-8"))),
        claims=tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8"))),
    )


def accepted(query: QueryDefinitionV1) -> AcceptedQueryDefinitionV1:
    """Return the accepted, digest-reproducing form of a QueryDefinition."""

    return AcceptedQueryDefinitionV1(
        path=query_definition_path(query.identity.name),
        query=query,
        artifact_digest=query_definition_digest(query).tagged,
    )


def projected_rows(result: ClaimQueryResultV1) -> list[dict[str, object]]:
    """Return one comparable row list from a completed Claim-query result.

    Absent and conflicted projected values are reported as their typed state
    rather than as a value, so a comparison against a donor row can never quietly
    read a refusal as data.
    """

    rows: list[dict[str, object]] = []
    for row in result.rows:
        values: dict[str, object] = {}
        for field in row.fields:
            values[field.name] = field.value if field.state == "present" else f"<{field.state}>"
        rows.append(values)
    return rows
