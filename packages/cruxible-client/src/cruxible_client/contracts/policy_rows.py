"""One live governed policy row: where it is declared and what it says.

``orient(section="policies")`` pages these rows. A policy is either a standalone
governed artifact (the ``ApprovalPolicy``, the ``ProcedureRuntimePolicy``, a
source acquisition policy) or a field embedded in the artifact that declares it
(a ClaimType's admission or resolution policy, a QueryDefinition's evaluation
policy, ...); ``field_path`` names that field and ``declaring_artifact_identity``
is the reference ``get`` reads the whole declaring artifact by.
"""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

PolicyKind: TypeAlias = Literal[
    "approval_policy",
    "procedure_runtime_policy",
    "source_acquisition_policy",
    "claim_evidence_admission_policy",
    "claim_admission_policy",
    "claim_resolution_policy",
    "claim_evidence_freshness_policy",
    "claim_attestation_consequence_policy",
    "capture_retention_erasure_policy",
    "query_evaluation_policy",
    "document_activation_policy",
    "procedure_activation_policy",
    "trigger_schedule",
]


class PolicyInForce(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-policy-in-force-v1"] = "playbill-policy-in-force-v1"
    placement: Literal["embedded", "standalone"]
    policy_kind: PolicyKind
    declaring_artifact_identity: str
    declaring_artifact_kind: str
    declaring_artifact_digest: str
    path: str
    field_path: str
    policy: dict[str, Any]


__all__ = ["PolicyInForce", "PolicyKind"]
