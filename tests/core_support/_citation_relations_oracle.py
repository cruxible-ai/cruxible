"""Complete source reconstruction for citation SQL parity tests only."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from cruxible_client.contracts.captures import CaptureEnvelopeAny, parse_capture_envelope
from cruxible_client.contracts.cas_contracts import BodyAccessContext, BodyProjectionProtocol
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_citation_references,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillError, ProjectionFormatError
from cruxible_client.contracts.projection_extensions import ProjectionFact
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_core.evidence.citation_relations import (
    RELATION_RETIRED_CONFLICT_SCHEMA,
    _conflict_group_facts,
    _digest_identity,
    _fact_key,
    _same_version_span_key,
    external_source_relation_subject,
)

RELATION_USE_SCHEMA = "playbill.citation_relation.use"
RELATION_SOURCE_USE_SCHEMA = "playbill.citation_relation.source_use"
RELATION_EXTERNAL_USE_SCHEMA = "playbill.citation_relation.external_use"
_ACCESS = BodyAccessContext(principal_id="citation-parity-test", can_read_body=True)


def capture_relation_subject(capture_digest: str) -> str:
    return _digest_identity("capture", capture_digest)


def logical_source_relation_subject(source_id: str) -> str:
    return _digest_identity("logical-source", source_id)


def _claim_uses(
    tree: Mapping[str, bytes], *, bodies: BodyProjectionProtocol
) -> list[dict[str, object]]:
    uses: list[dict[str, object]] = []
    envelopes_by_digest: dict[str, CaptureEnvelopeAny] = {}
    for path in sorted(tree, key=lambda value: value.encode("utf-8")):
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        claim_digest = claim_artifact_digest(claim).tagged
        for citation in claim_citation_references(claim):
            envelope = envelopes_by_digest.get(citation.capture_digest)
            if envelope is None:
                try:
                    envelope = parse_capture_envelope(
                        bodies.read(citation.capture_digest, access=_ACCESS)
                    )
                except PlaybillError as exc:  # pragma: no cover - accepted-tree invariant
                    raise ProjectionFormatError(
                        "citation relation could not read an accepted Capture"
                    ) from exc
                envelopes_by_digest[citation.capture_digest] = envelope
            use: dict[str, object] = {
                "capture_contract_digest": {"$digest": envelope.capture_contract_digest},
                "capture_digest": {"$digest": citation.capture_digest},
                "citation_id": citation.citation_id,
                "claim_artifact_digest": {"$digest": claim_digest},
                "claim_identity": claim.identity.qualified,
                "claim_lifecycle": claim.lifecycle.state,
                "claim_path": path,
                "commitment": envelope.commitment.model_dump(mode="json"),
                "origin": getattr(citation, "origin", "legacy"),
                "role": getattr(citation, "role", "legacy"),
                "source": envelope.source.model_dump(mode="json"),
            }
            uses.append(use)

    return uses


def _use_facts(uses: list[dict[str, object]]) -> list[ProjectionFact]:
    facts: list[ProjectionFact] = []
    for use in uses:
        capture = use.get("capture_digest")
        citation_id = use.get("citation_id")
        claim_identity = use.get("claim_identity")
        if (
            not isinstance(capture, dict)
            or not isinstance(capture.get("$digest"), str)
            or not isinstance(citation_id, str)
            or not isinstance(claim_identity, str)
        ):
            raise ProjectionFormatError("citation relation use is malformed")
        facts.append(
            ProjectionFact(
                schema_id=RELATION_USE_SCHEMA,
                schema_version=1,
                subject_identity=capture_relation_subject(capture["$digest"]),
                fact_key=_fact_key("use", citation_id, claim_identity),
                value=use,
            )
        )
        source = use.get("source")
        if isinstance(source, dict) and source.get("kind") == "external":
            try:
                external = ExternalSourceReferenceV1.model_validate(source)
            except ValueError as exc:
                raise ProjectionFormatError(
                    "citation relation external source is malformed"
                ) from exc
            source_fact = ProjectionFact(
                schema_id=RELATION_SOURCE_USE_SCHEMA,
                schema_version=1,
                subject_identity=logical_source_relation_subject(external.source_identity),
                fact_key=_fact_key("use", citation_id, claim_identity),
                value=use,
            )
            facts.append(source_fact)
            facts.append(
                source_fact.model_copy(
                    update={
                        "schema_id": RELATION_EXTERNAL_USE_SCHEMA,
                        "subject_identity": external_source_relation_subject(external),
                    }
                )
            )

    return facts


def _conflict_facts(
    uses: list[dict[str, object]],
) -> list[ProjectionFact]:
    """Retain raw conflicts; capture precedence is applied at the Claim boundary."""
    capture_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    external_groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for use in uses:
        capture = use["capture_digest"]
        assert isinstance(capture, dict)
        capture_groups[str(capture["$digest"])].append(use)
        source = use["source"]
        if isinstance(source, dict) and source.get("kind") == "external":
            parsed = ExternalSourceReferenceV1.model_validate(source)
            external_groups[external_source_relation_subject(parsed)].append(use)

    version_groups: dict[str, list[tuple[int, int, dict[str, object]]]] = defaultdict(list)
    for use in uses:
        span = _same_version_span_key(use)
        if span is not None:
            version_groups[span[0]].append((span[1], span[2], use))
    return _conflict_group_facts(
        capture_groups,
        external_groups,
        version_groups,
    )


def build_citation_relation_facts(
    tree: Mapping[str, bytes], *, bodies: BodyProjectionProtocol
) -> tuple[ProjectionFact, ...]:
    """Reconstruct all uses and conflicts; no incremental cache or publication path."""
    uses = _claim_uses(tree, bodies=bodies)
    facts = [*_use_facts(uses), *_conflict_facts(uses)]
    capture_subjects = {
        str(fact.value["live_claim_identity"])
        for fact in facts
        if fact.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA
        and isinstance(fact.value, dict)
        and fact.value["relation_kind"] == "capture"
    }
    return tuple(
        fact
        for fact in facts
        if not (
            fact.schema_id == RELATION_RETIRED_CONFLICT_SCHEMA
            and isinstance(fact.value, dict)
            and fact.value["relation_kind"] != "capture"
            and fact.value["live_claim_identity"] in capture_subjects
        )
    )
