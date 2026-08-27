from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import (
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    ClaimTypeRef,
    Disposition,
    Playbill,
    ReferentSensitivity,
    SubjectRef,
)
from cruxible_client import contracts as api
from cruxible_client.authoring.blocks import (
    ProjectionIndependentEvidenceForbidden,
    render_projection_opening,
)
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
)
from cruxible_client.contracts.authoring.models import ClaimAuthoringPayloadV2
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimAttestationConsequenceRuleV1,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.errors import CoreError

_DIGEST = "sha256:" + "1" * 64
_COORDINATE = api.PlaybillAcceptedCoordinate(
    git_oid="a" * 40,
    semantic_root=_DIGEST,
    generation_root="sha256:" + "2" * 64,
    compiler_digest="sha256:" + "3" * 64,
)


class _Client:
    def __init__(self) -> None:
        self.compiled: dict[str, Any] | None = None
        self.curation_observation: object | None = None
        self.coverage_observations: object | None = None
        self.curation_actions: list[tuple[str, dict[str, object]]] = []
        self.audit_request: dict[str, object] | None = None
        self.retirement_request: dict[str, object] | None = None

    def search_playbill(self, _instance_id: str, **values: object) -> api.PlaybillSearchResult:
        return api.PlaybillSearchResult(
            mode=values["mode"],
            coordinate=_COORDINATE,
            evaluation_time=str(values["evaluation_time"]),
            rows=[],
            orientation={"state": "empty"} if values["mode"] == "orient" else None,
            selection_basis_digest="sha256:" + "4" * 64,
            truncated=False,
            result_digest="sha256:" + "5" * 64,
        )

    def since_playbill(self, _instance_id: str, **values: object) -> api.PlaybillSinceResult:
        result_values: dict[str, object] = {
            "coordinate": _COORDINATE.model_dump(mode="json"),
            "generation": 4,
            "rows": [],
            "next_cursor": None,
            "truncated": False,
        }
        return api.PlaybillSinceResult.model_validate(
            {
                **result_values,
                "result_digest": api._since_digest(  # type: ignore[attr-defined]
                    "playbill-since-result-v1", result_values
                ),
            }
        )

    def resolve_playbill_coverage(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCoverageResult:
        self.coverage_observations = values["observations"]
        return api.PlaybillCoverageResult(
            coordinate=_COORDINATE,
            result={
                "at": _COORDINATE.model_dump(mode="json"),
                "access_profile": {
                    "tag": "playbill-coverage-access-profile-v1",
                    "profile_id": "sdk-default",
                    "permitted_access_classes": ["instance", "public"],
                    "disclose_restricted_existence": True,
                },
                "spans": [],
            },
        )

    def list_playbill_curation(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCurationListResult:
        self.curation_observation = values["workspace_observation"]
        return api.PlaybillCurationListResult(
            coordinate=_COORDINATE,
            generation=4,
            evaluation_time=str(values["evaluation_time"]),
            operational_head_digest="sha256:" + "6" * 64,
            items=[],
            detector_coverage=[],
            observation_coverage={
                "tag": "playbill-curation-observation-coverage-v1",
                "source_count": 1,
                "observed_block_count": 0,
                "omitted_source_count": 0,
                "omissions": [],
            },
            result_digest="sha256:" + "7" * 64,
        )

    def audit_playbill(self, _instance_id: str, **values: object) -> api.PlaybillAuditResult:
        self.audit_request = values
        return api.PlaybillAuditResult(
            coordinate=_COORDINATE,
            generation=4,
            evaluation_time=str(values["evaluation_time"]),
            operational_input_head_digest="sha256:" + "6" * 64,
            audited_through_generation=4,
            rows=[],
            coverage=api.PlaybillAuditCoverage(
                access_permitted=True,
                declared_scope=api.PlaybillAuditScope(
                    claim_type_identities=list(values["claim_type_identities"]),
                    subject_kinds=list(values["subject_kinds"]),
                ),
                covered_claims=[],
                candidate_claim_count=0,
                returned_claim_count=0,
                omitted_claim_count=0,
                omission_reasons=[],
            ),
            result_digest="sha256:" + "7" * 64,
        )

    def retire_playbill_claim(
        self,
        _instance_id: str,
        claim_id: str,
        *,
        request: dict[str, object],
    ) -> api.PlaybillClaimRetireResponse:
        self.retirement_request = {"claim_id": claim_id, **request}
        return api.PlaybillClaimRetirePreflight(
            operation_digest="sha256:" + "8" * 64,
            coordinate=_COORDINATE,
            root_identity={"kind": "Claim", "name": claim_id},
            root_predecessor_digest="sha256:" + "9" * 64,
            reason=request["reason"],  # type: ignore[arg-type]
            effective_until=request.get("effective_until"),  # type: ignore[arg-type]
            required_dependents=[],
            diagnostics=[],
            submit_ready=True,
        )

    def _curation_action(
        self, operation: str, values: dict[str, object]
    ) -> api.PlaybillCurationActionResult:
        self.curation_actions.append((operation, values))
        return api.PlaybillCurationActionResult(
            coordinate=_COORDINATE,
            generation=4,
            operational_head_digest="sha256:" + "6" * 64,
            item={"item_id": values["item_id"], "status": "resolved"},
        )

    def overrule_playbill_curation(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCurationActionResult:
        return self._curation_action("overrule", values)

    def accept_fixed_playbill_curation(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCurationActionResult:
        return self._curation_action("accept_fixed", values)

    def suppress_playbill_curation(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCurationActionResult:
        return self._curation_action("suppress", values)

    def compile_playbill_authoring(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillAuthoringPreflightResult:
        self.compiled = dict(values)
        return api.PlaybillAuthoringPreflightResult(
            verdict="passed",
            certificate={"intent_id": "AIT-" + "1" * 32},
            frontier={"diagnostics": []},
        )

    def get_playbill_authoring_intent(
        self, _instance_id: str, _intent_id: str
    ) -> api.PlaybillAuthoringIntentView:
        assert self.compiled is not None
        return api.PlaybillAuthoringIntentView(
            intent={
                "intent_id": "AIT-" + "1" * 32,
                "intent_revision": 1,
                "payload": self.compiled["payload"],
                "insertion_expectation": None,
            }
        )

    def close(self) -> None:
        return None


def _workspace(path: Path) -> None:
    (path / ".playbill").mkdir()
    (path / ".playbill" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    approval_roles: [owner]
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )
    (path / "corpus").mkdir()
    (path / "corpus" / "runbook.md").write_text(
        "Patch KEV systems within 48 hours.\n", encoding="utf-8"
    )


def test_sdk_since_uses_its_active_orientation(tmp_path: Path) -> None:
    _workspace(tmp_path)
    pb = Playbill._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    result = pb.since(2)

    assert result.generation == 4
    assert result.rows == []


def test_sdk_curation_list_uses_the_existing_explicit_workspace_scanner(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    result = pb.curation_list()

    assert result.generation == 4
    assert isinstance(client.curation_observation, dict)
    assert client.curation_observation["tag"] == "playbill-next-workspace-observation-v1"
    (source_row,) = client.curation_observation["source_observations"]
    assert source_row["tag"] == "playbill-next-source-observation-v4"
    assert source_row["source_id"] == "corpus.runbook"
    assert isinstance(client.coverage_observations, list)


def test_sdk_audit_uses_orientation_scope_and_explicit_time(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    result = pb.audit(
        claim_type_identities=("ClaimType:status",),
        subject_kinds=("work_item",),
        max_rows=9,
        max_bytes=4096,
    )

    assert result.audited_through_generation == 4
    assert client.audit_request is not None
    assert client.audit_request["at"] == _COORDINATE
    assert client.audit_request["claim_type_identities"] == ("ClaimType:status",)
    assert client.audit_request["subject_kinds"] == ("work_item",)


def test_sdk_curation_lifecycle_methods_are_thin_typed_delegates(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    common = {
        "item_id": "sha256:" + "1" * 64,
        "expected_latest_event_digest": "sha256:" + "2" * 64,
        "reason": "operator-reviewed mechanical facts",
    }

    pb.curation_overrule(**common)
    pb.curation_accept_fixed(
        **common,
        accepted_proposal_id="sha256:" + "3" * 64,
        accepted_changeset_digest="sha256:" + "4" * 64,
    )
    pb.curation_suppress(**common, scope="item", until_generation=8)

    assert [name for name, _values in client.curation_actions] == [
        "overrule",
        "accept_fixed",
        "suppress",
    ]
    assert client.curation_actions[2][1]["scope"] == "item"


@pytest.mark.parametrize("window", [False, True])
def test_sdk_declared_block_forbids_evidence_but_allows_explicit_copy(
    tmp_path: Path,
    window: bool,
) -> None:
    _workspace(tmp_path)
    source = tmp_path / "corpus" / "runbook.md"
    body = b"Patch KEV systems within 48 hours.\n"
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="policy",
        declared_generation=1,
        declared_coordinate=AcceptedCoordinate.model_validate(_COORDINATE.model_dump(mode="json")),
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-source"),
                statement_digest="sha256:" + "9" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    source.write_bytes(
        render_projection_opening(stamp) + body + b"<!-- /playbill:block:policy -->\n"
    )
    pb = Playbill._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    selector = pb.file("corpus/runbook.md")
    selection = (
        selector.anchor_window(text="within 48 hours", surrounding_lines=1)
        if window
        else selector.anchor("within 48 hours")
    )
    common: dict[str, Any] = {
        "subject": "secops.policy/patch-sla",
        "predicate": "secops.policy.patch_sla",
        "value": 48,
        "role": ClaimRole.NORMATIVE,
        "rationale": "Declared policy.",
        "self_source": None,
        "qualifier": None,
        "effective_period": None,
        "revises": None,
        "dispositions": {},
        "publish_to": None,
        "subject_definition": None,
        "claim_type_definition": None,
    }

    with pytest.raises(ProjectionIndependentEvidenceForbidden):
        pb.claim(supported_by=selection, copied_from=None, **common)

    copy = pb.claim(supported_by=None, copied_from=selection, **common)
    assert copy.payload.citation_role == "copy"
    # Raw-wire callers remain the explicitly accepted, documented residual.
    assert selection.observation().selected_content


def test_sdk_retirement_owns_claim_ref_and_coordinate_plumbing(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    result = pb.retire_claim(
        "Claim:CLM-0123456789abcdef0123456789abcdef",
        reason="was-wrong",
        mode="submit",
    )

    assert result.tag == "playbill-claim-retire-preflight-v1"
    assert client.retirement_request == {
        "claim_id": "CLM-0123456789abcdef0123456789abcdef",
        "tag": "playbill-claim-retire-request-v1",
        "mode": "submit",
        "claim_ref": "Claim:CLM-0123456789abcdef0123456789abcdef",
        "reason": "was-wrong",
        "effective_until": None,
        "expected_coordinate": AcceptedCoordinate.model_validate(
            _COORDINATE.model_dump(mode="json")
        ).model_dump(mode="json"),
        "dependents": [],
    }


def test_sdk_plain_retirement_replay_uses_accepted_operation_coordinate(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    operation_digest = "sha256:" + "d" * 64

    class ReplayClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[dict[str, object]] = []

        def playbill_claim_history(
            self, _instance_id: str, identity: str
        ) -> api.PlaybillClaimHistory:
            return api.PlaybillClaimHistory(
                identity=f"Claim:{identity}",
                entries=[
                    {
                        "sequence": 5,
                        "coordinate": _COORDINATE.model_dump(mode="json"),
                        "lifecycle_state": "retired",
                    }
                ],
            )

        def retire_playbill_claim(
            self,
            _instance_id: str,
            claim_id: str,
            *,
            request: dict[str, object],
        ) -> api.PlaybillClaimRetireResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return api.PlaybillClaimRetireResult(
                    outcome="proposed",
                    operation_digest=operation_digest,
                    coordinate=_COORDINATE,
                    retirements=[],
                )
            if len(self.requests) == 2:
                raise CoreError(
                    "playbill.claim.retire_closure_mismatch: expected accepted operation"
                )
            assert request["expected_coordinate"] == AcceptedCoordinate.model_validate(
                _COORDINATE.model_dump(mode="json")
            ).model_dump(mode="json")
            return api.PlaybillClaimRetireResult(
                outcome="already_retired",
                operation_digest=operation_digest,
                coordinate=_COORDINATE,
                retirements=[
                    {
                        "artifact_identity": {"kind": "Claim", "name": claim_id},
                        "predecessor_digest": "sha256:" + "e" * 64,
                        "reason": request["reason"],
                        "effective_until": request["effective_until"],
                        "successor_digest": "sha256:" + "f" * 64,
                    }
                ],
            )

    client = ReplayClient()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    proposed = pb.retire_claim(
        "Claim:CLM-0123456789abcdef0123456789abcdef",
        reason="was-wrong",
        mode="submit",
    )
    assert proposed.outcome == "proposed"
    pb._coordinate = AcceptedCoordinate(  # type: ignore[attr-defined]
        git_oid="b" * 40,
        semantic_root="sha256:" + "b" * 64,
        generation_root="sha256:" + "c" * 64,
        compiler_digest="sha256:" + "3" * 64,
    )
    result = pb.retire_claim(
        "Claim:CLM-0123456789abcdef0123456789abcdef",
        reason="was-wrong",
        mode="submit",
    )

    assert result.outcome == "already_retired"
    assert result.operation_digest == proposed.operation_digest
    assert len(client.requests) == 3


def test_sdk_plain_retirement_replay_never_masks_a_genuine_mismatch(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    original = CoreError(
        "playbill.claim.retire_closure_mismatch: accepted retirement attribution differs"
    )

    class MismatchClient(_Client):
        calls = 0

        def playbill_claim_history(
            self, _instance_id: str, identity: str
        ) -> api.PlaybillClaimHistory:
            return api.PlaybillClaimHistory(
                identity=f"Claim:{identity}",
                entries=[
                    {
                        "sequence": 5,
                        "coordinate": _COORDINATE.model_dump(mode="json"),
                        "lifecycle_state": "retired",
                    }
                ],
            )

        def retire_playbill_claim(
            self,
            _instance_id: str,
            _claim_id: str,
            *,
            request: dict[str, object],
        ) -> api.PlaybillClaimRetireResponse:
            self.calls += 1
            if self.calls == 1:
                return api.PlaybillClaimRetireResult(
                    outcome="proposed",
                    operation_digest="sha256:" + "d" * 64,
                    coordinate=_COORDINATE,
                    retirements=[],
                )
            if self.calls == 2:
                raise original
            raise CoreError(
                "playbill.claim.retire_closure_mismatch: different reason remains refused"
            )

    client = MismatchClient()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    pb.retire_claim(
        "Claim:CLM-0123456789abcdef0123456789abcdef",
        reason="was-wrong",
        mode="submit",
    )

    with pytest.raises(CoreError) as raised:
        pb.retire_claim(
            "Claim:CLM-0123456789abcdef0123456789abcdef",
            reason="was-rescinded",
            mode="submit",
        )

    assert raised.value is original
    assert client.calls == 2


def test_cold_claim_prepares_one_payload_with_dependencies_and_program_stamp(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    authority = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))
    subject = pb.subject(
        subject="secops.policy/patch-sla",
        authority=authority,
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )
    claim_type = pb.claim_type(
        predicate="secops.policy.patch_sla",
        subject_kinds=("secops.policy",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "object"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=("corpus.runbook",),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=authority,
        pins=(),
        evidence_freshness=None,
    )
    draft = pb.claim(
        subject=subject.address,
        predicate=claim_type.predicate,
        value={"kev_deadline_hours": 48},
        role=ClaimRole.NORMATIVE,
        rationale="The runbook fixes the KEV deadline.",
        supported_by=pb.file("corpus/runbook.md").anchor("within 48 hours"),
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=subject,
        claim_type_definition=claim_type,
    )

    intent = draft.prepare()

    assert not intent.refused
    assert client.compiled is not None
    payload = ClaimAuthoringPayloadV2.model_validate(client.compiled["payload"])
    assert payload.dependency_drafts.subject == subject.shell
    assert payload.dependency_drafts.claim_type == claim_type.definition
    assert client.compiled["reference_expectations"] == []
    stamp = client.compiled["program_stamp"]
    assert stamp["tag"] == "playbill-authoring-program-stamp-v1"


def test_claim_type_builder_selects_v4_for_attestation_consequences(tmp_path: Path) -> None:
    _workspace(tmp_path)
    pb = Playbill._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    authority = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))
    policy = ClaimAttestationConsequencePolicyV1(
        rules=(
            ClaimAttestationConsequenceRuleV1(
                rule_id="two-independent-unsure",
                stance="unsure",
                minimum_independent_control_components=2,
            ),
        )
    )

    draft = pb.claim_type(
        predicate="secops.policy.patch_sla",
        subject_kinds=("secops.policy",),
        object_kind=ClaimObjectKind.LITERAL,
        value_schema={"type": "object"},
        object_subject_kinds=(),
        cardinality=Cardinality.ONE,
        permitted_roles=(ClaimRole.NORMATIVE,),
        referent_sensitivity=ReferentSensitivity.IDENTITY,
        sources=("corpus.runbook",),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        authority=authority,
        pins=(),
        evidence_freshness=None,
        attestation_consequence_policy=policy,
    )

    assert draft.definition.artifact_format == "playbill-claim-type-v4"
    assert draft.definition.attestation_consequence_policy == policy


def test_claim_requires_exactly_one_explicit_source_role(tmp_path: Path) -> None:
    _workspace(tmp_path)
    pb = Playbill._from_client(  # type: ignore[arg-type]
        _Client(), instance_id="inst_test", workspace=tmp_path
    )
    try:
        pb.claim(
            subject="secops.policy/patch-sla",
            predicate="secops.policy.patch_sla",
            value=48,
            role=ClaimRole.NORMATIVE,
            rationale="rationale",
            supported_by=None,
            copied_from=None,
            self_source=None,
            qualifier=None,
            effective_period=None,
            revises=None,
            dispositions={"CLM-" + "1" * 32: Disposition.NOT_TESTED},
            publish_to=None,
            subject_definition=None,
            claim_type_definition=None,
        )
    except ValueError as exc:
        assert "exactly one" in str(exc)
    else:  # pragma: no cover - assertion form keeps the refusal readable
        raise AssertionError("claim unexpectedly accepted an omitted source role")


def test_typed_refs_emit_coordinate_assertions_without_entering_the_payload(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client, instance_id="inst_test", workspace=tmp_path
    )
    coordinate = AcceptedCoordinate.model_validate(_COORDINATE.model_dump(mode="json"))
    draft = pb.claim(
        subject=SubjectRef("secops.policy/patch-sla", coordinate),
        predicate=ClaimTypeRef("secops.policy.patch_sla", coordinate),
        value=48,
        role=ClaimRole.NORMATIVE,
        rationale="A self-authored test claim.",
        supported_by=None,
        copied_from=None,
        self_source="Patch within 48 hours.",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    draft.prepare()

    assert client.compiled is not None
    assert client.compiled["reference_expectations"] == [
        {
            "tag": "playbill-authoring-reference-expectation-v1",
            "payload_path": "statement.predicate",
            "artifact_kind": "ClaimType",
            "address": "secops.policy.patch_sla",
            "minted_coordinate": coordinate.model_dump(mode="json"),
        },
        {
            "tag": "playbill-authoring-reference-expectation-v1",
            "payload_path": "statement.subject",
            "artifact_kind": "Subject",
            "address": "secops.policy/patch-sla",
            "minted_coordinate": coordinate.model_dump(mode="json"),
        },
    ]
    payload = client.compiled["payload"]
    assert "reference_expectations" not in payload
    assert "program_stamp" not in payload


def test_plain_strings_never_forge_coordinate_assertions_or_change_yaml_shorthand(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(  # type: ignore[arg-type]
        client, instance_id="inst_test", workspace=tmp_path
    )
    claim_id = "CLM-" + "1" * 32
    common = {
        "subject": "secops.policy/patch-sla.yaml",
        "predicate": "secops.policy.patch_sla",
        "value": 48,
        "role": ClaimRole.NORMATIVE,
        "rationale": "String references resolve at the daemon's accepted coordinate.",
        "supported_by": None,
        "copied_from": None,
        "self_source": "Patch within 48 hours.",
        "qualifier": None,
        "effective_period": None,
        "publish_to": None,
        "subject_definition": None,
        "claim_type_definition": None,
    }

    fresh = pb.claim(**common, revises=None, dispositions={})  # type: ignore[arg-type]
    revision = pb.claim(  # type: ignore[arg-type]
        **common,
        revises=claim_id,
        dispositions={claim_id: Disposition.SUPPORT},
    )

    assert fresh.payload.statement.subject == revision.payload.statement.subject
    assert fresh.payload.statement.subject.artifact_path == (
        "subjects/secops.policy/patch-sla.yaml"
    )
    assert fresh.reference_expectations == ()
    assert revision.reference_expectations == ()


def test_connect_refuses_an_unknown_daemon_before_instance_io(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    context = tmp_path / "context.json"
    context.write_text(
        '{"server_url":"http://remembered","server_socket":"/tmp/remembered.sock",'
        '"instance_id":"inst_test"}\n',
        encoding="utf-8",
    )
    calls: list[str] = []
    connection: dict[str, object] = {}

    class _UnknownClient:
        def __init__(self, **values: object) -> None:
            calls.append("connect")
            connection.update(values)

        def version(self) -> str:
            calls.append("version")
            return "9.0.0"

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("cruxible_client.authoring.sdk.CruxibleClient", _UnknownClient)

    try:
        Playbill.connect(context=context, target="http://explicit", workspace=tmp_path)
    except IncompatibleDaemonVersion as exc:
        assert exc.daemon_version == "9.0.0"
    else:  # pragma: no cover - the handshake must fail closed
        raise AssertionError("unknown daemon version was accepted")
    assert calls == ["connect", "version", "close"]
    assert connection["base_url"] == "http://explicit"
    assert connection["socket_path"] is None


def test_refusal_diagnostic_maps_exact_payload_path_to_the_call_expression(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)

    class _RefusingClient(_Client):
        def compile_playbill_authoring(
            self, _instance_id: str, **values: object
        ) -> api.PlaybillAuthoringPreflightResult:
            self.compiled = dict(values)
            return api.PlaybillAuthoringPreflightResult(
                verdict="refused",
                certificate={"intent_id": "AIT-" + "1" * 32},
                frontier={
                    "diagnostics": [
                        {
                            "code": "playbill.test.role_refused",
                            "stage": "admission",
                            "offending_element": "statement.role",
                            "message": "role is not admitted",
                            "repairs": [],
                            "owner": "writer",
                            "disposition": "repairable",
                        }
                    ]
                },
            )

    pb = Playbill._from_client(  # type: ignore[arg-type]
        _RefusingClient(), instance_id="inst_test", workspace=tmp_path
    )
    intent = pb.claim(
        subject="secops.policy/patch-sla",
        predicate="secops.policy.patch_sla",
        value=48,
        role=ClaimRole.NORMATIVE,
        rationale="Map this refusal to its exact decision.",
        supported_by=None,
        copied_from=None,
        self_source="Patch within 48 hours.",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    ).prepare()

    diagnostic = intent.diagnostics[0]
    assert diagnostic.offending_element == "statement.role"
    assert diagnostic.call_site is not None
    assert diagnostic.call_site.expression == "ClaimRole.NORMATIVE"
