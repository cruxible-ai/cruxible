from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import (
    CaptureRef,
    Cardinality,
    ClaimObjectKind,
    ClaimRole,
    ClaimTypeRef,
    Cruxible,
    Disposition,
    ExactContent,
    ExactContentTypeError,
    ReferentSensitivity,
    SubjectRef,
)
from cruxible_client import __version__ as CLIENT_VERSION
from cruxible_client import contracts as api
from cruxible_client.authoring.blocks import (
    ProjectionIndependentEvidenceForbidden,
    render_projection_opening,
)
from cruxible_client.authoring.sdk import SDK_CONTRACT_SNAPSHOT_DIGEST, ClaimView
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
)
from cruxible_client.contracts.authoring.models import (
    AuthoringExactContentObject,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV2,
    WorkingSelectionObservation,
)
from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimAttestationConsequenceRule,
    ClaimType,
)
from cruxible_client.contracts.claims import LiteralClaimObject, SubjectClaimObject
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBacking,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core import __version__ as DAEMON_VERSION
from tests.support.preflight_results import stub_diagnostic, stub_preflight_result
from tests.test_client._read_fakes import ClaimTypeRead

_DIGEST = "sha256:" + "1" * 64
_COORDINATE = api.AcceptedCoordinate(
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
        self.claim_type_object_kinds: dict[str, str] = {"sec.vuln.affects_package": "subject"}
        self.claim_type_reads = 0

    def head(self, instance_id: str, *, at: object = None) -> api.Head:
        return api.Head(
            instance=instance_id,
            coordinate=_COORDINATE.model_dump(mode="json"),  # type: ignore[arg-type]
            generation=4,
        )

    def get(self, instance_id: str, *, request: Any) -> Any:
        """``get(detail="proof")`` over this fake's ClaimType and Claim views."""

        from cruxible_client.contracts.get_reads import (
            GetCoordinate,
            GetResult,
        )

        assert request.detail == "proof"
        if request.ref.startswith("ClaimType:"):
            kind = "claim_type"
            view: Any = self._claim_type_view(
                instance_id, request.ref.removeprefix("ClaimType:"), at=request.at
            )
        else:
            kind = "claim"
            view = self._claim_view(instance_id, request.ref, at=request.at)
        return GetResult(
            ref=request.ref,
            kind=kind,  # type: ignore[arg-type]
            detail="proof",
            proof=view.model_dump(mode="json"),
            coordinate=GetCoordinate(git_oid=view.coordinate.git_oid[:12], generation=4),
            accepted_coordinate=view.coordinate,
            evaluation_time=request.evaluation_time or datetime(2026, 9, 1, tzinfo=UTC),
        )

    def _claim_view(self, _instance_id: str, _identity: str, **_values: Any) -> Any:
        raise AssertionError("this fake reads no Claim")

    def _claim_type_view(
        self,
        _instance_id: str,
        predicate: str,
        *,
        at: api.AcceptedCoordinate,
    ) -> ClaimTypeRead:
        self.claim_type_reads += 1
        return ClaimTypeRead(
            coordinate=at,
            path=f"claim-types/{predicate}.json",
            predicate=predicate,
            identity=f"ClaimType:{predicate}",
            artifact_digest=_DIGEST,
            envelope={"object_kind": self.claim_type_object_kinds.get(predicate, "literal")},
        )

    def whoami(self, _instance_id: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(coordinate=_COORDINATE)

    def since(self, _instance_id: str, **values: object) -> api.SinceResult:
        result_values: dict[str, object] = {
            "coordinate": _COORDINATE.model_dump(mode="json"),
            "generation": 4,
            "rows": [],
            "next_cursor": None,
            "truncated": False,
        }
        return api.SinceResult.model_validate(
            {
                **result_values,
                "result_digest": api._since_digest(  # type: ignore[attr-defined]
                    "playbill-since-result-v1", result_values
                ),
            }
        )

    def resolve_coverage(self, _instance_id: str, **values: object) -> api.CoverageResult:
        self.coverage_observations = values["observations"]
        return api.CoverageResult(
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

    def list_curation(self, _instance_id: str, **values: object) -> api.CurationListResult:
        self.curation_observation = values["workspace_observation"]
        return api.CurationListResult(
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

    def audit(self, _instance_id: str, **values: object) -> api.AuditResult:
        self.audit_request = values
        return api.AuditResult(
            coordinate=_COORDINATE,
            generation=4,
            evaluation_time=str(values["evaluation_time"]),
            operational_input_head_digest="sha256:" + "6" * 64,
            audited_through_generation=4,
            rows=[],
            coverage=api.AuditCoverage(
                access_permitted=True,
                declared_scope=api.AuditScope(
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

    def _curation_action(
        self, operation: str, values: dict[str, object]
    ) -> api.CurationActionResult:
        self.curation_actions.append((operation, values))
        return api.CurationActionResult(
            coordinate=_COORDINATE,
            generation=4,
            operational_head_digest="sha256:" + "6" * 64,
            item={"item_id": values["item_id"], "status": "resolved"},
        )

    def overrule_curation(self, _instance_id: str, **values: object) -> api.CurationActionResult:
        return self._curation_action("overrule", values)

    def accept_fixed_curation(
        self, _instance_id: str, **values: object
    ) -> api.CurationActionResult:
        return self._curation_action("accept_fixed", values)

    def suppress_curation(self, _instance_id: str, **values: object) -> api.CurationActionResult:
        return self._curation_action("suppress", values)

    def compile_authoring(
        self, _instance_id: str, **values: object
    ) -> api.AuthoringPreflightResult:
        self.compiled = dict(values)
        return stub_preflight_result(intent_id="AIT-" + "1" * 32)

    def get_authoring_intent(
        self, _instance_id: str, _intent_id: str
    ) -> api.AuthoringIntentViewRecord:
        assert self.compiled is not None
        return api.AuthoringIntentViewRecord(
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
    (path / ".cruxible").mkdir()
    (path / ".cruxible" / "sources.yaml").write_text(
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
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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


def test_sdk_audit_uses_current_head_and_explicit_time(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
    assert client.audit_request["at"] is None
    assert client.audit_request["claim_type_identities"] == ("ClaimType:status",)
    assert client.audit_request["subject_kinds"] == ("work_item",)


def test_sdk_curation_lifecycle_methods_are_thin_typed_delegates(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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


def test_sdk_declared_block_refuses_every_citation_role_inside_it(
    tmp_path: Path,
) -> None:
    """A copy of projection bytes attests them into concrete as evidence would.

    `copied_from` used to walk past the guard by its role alone. The daemon now
    refuses every role at lowering and at the citation gate; the SDK guard is
    the fast path, and a selection of prose OUTSIDE the block hands the daemon
    the page so it can prove the span independent itself.
    """

    _workspace(tmp_path)
    source = tmp_path / "corpus" / "runbook.md"
    body = b"Patch KEV systems within 48 hours.\n"
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="policy",
        declared_generation=1,
        declared_coordinate=AcceptedCoordinate.model_validate(_COORDINATE.model_dump(mode="json")),
        backing=(
            ProjectionClaimBacking(
                identity=ArtifactIdentity(kind="Claim", name="CLM-source"),
                statement_digest="sha256:" + "9" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    page = (
        b"Preamble the author wrote.\n"
        b"A second preamble line.\n"
        + render_projection_opening(stamp)
        + body
        + b"<!-- /cruxible:block:policy -->\n"
    )
    source.write_bytes(page)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    selector = pb.file("corpus/runbook.md")
    selection = selector.anchor("within 48 hours")
    outside = selector.anchor("Preamble the author wrote")
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
        "subject_definition": None,
        "claim_type_definition": None,
    }

    with pytest.raises(ProjectionIndependentEvidenceForbidden) as evidence_refusal:
        pb.claim(supported_by=selection, copied_from=None, **common)
    assert evidence_refusal.value.code == "cruxible.projection.evidence_from_projection"
    with pytest.raises(ProjectionIndependentEvidenceForbidden):
        pb.claim(supported_by=None, copied_from=selection, **common)

    copy = pb.claim(supported_by=None, copied_from=outside, **common)
    assert copy.payload.citation_role == "copy"
    assert isinstance(copy.payload.source, WorkingSelectionObservation)
    assert copy.payload.source.source_content == page


def test_sdk_procedure_run_binds_its_typed_input_contract_coordinate(tmp_path: Path) -> None:
    _workspace(tmp_path)

    from cruxible_client.contracts.procedures.artifacts import procedure_artifact_digest
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from cruxible_client.contracts.procedures.models import ProcedureDefinition, ProjectNode
    from tests.test_procedures.test_procedure_execution import (
        _budget,
        _hard_caps,
        _owned_accepted,
        _owned_contract,
        _owned_pin,
    )

    ci = _owned_contract("input", {"account": PropertySchema(type="string")})
    co = _owned_contract("output", {"ok": PropertySchema(type="bool")})
    pi, po = _owned_pin("contract-in", ci), _owned_pin("contract-out", co)
    artifact = _owned_accepted(
        ProcedureDefinition(
            name="daily-summary",
            contract_in=pi,
            contract_out=po,
            nodes=(
                ProjectNode(node_id="result", fields={"ok": True}, contract_out=po, as_="result"),
            ),
            returns="result",
            budget=_budget(),
            hard_caps=_hard_caps(),
            terminal_capability=1,
        ),
        contracts=(ci, co),
        pins=(pi, po),
    ).procedure

    class ProcedureClient(_Client):
        def procedure_readiness(self, *args, **kwargs):
            return api.ProcedureReadiness.model_construct(
                coordinate=_COORDINATE,
                artifact=artifact,
                procedure_artifact_digest=procedure_artifact_digest(artifact).tagged,
            )

        def __init__(self) -> None:
            super().__init__()
            self.runs: list[dict[str, object]] = []

        def run_procedure(
            self,
            _instance_id: str,
            name: str,
            **values: object,
        ) -> api.ProcedureRunState:
            self.runs.append(values)
            lane = "replay" if values["at"] is not None else "current"
            return api.ProcedureRunState(
                run_id="RUN-" + "a" * 64,
                procedure_identity={"kind": "Procedure", "name": name},
                procedure_artifact_digest=_DIGEST,
                bound_coordinate=_COORDINATE,
                head_at_admission=_COORDINATE,
                lane=lane,
                evaluation_time=str(values["evaluation_time"]),
                status="succeeded",
                pending_inputs=[],
                outcomes=[],
                next_operation={"kind": "done"},
                result={"ok": True},
            )

    client = ProcedureClient()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    procedure = pb.accepted_procedure("daily-summary")

    assert procedure.run(input=procedure.input(account="one")).status == "succeeded"
    assert (
        procedure.run(at=pb.coordinate, input=procedure.input(account="one")).status == "succeeded"
    )
    assert client.runs[0]["at"] == _COORDINATE
    assert client.runs[0]["input"] == {"account": "one"}
    assert client.runs[1]["at"] == _COORDINATE
    run = procedure.run(input=procedure.input(account="one"))
    assert run.succeeded and run.result.ok is True
    assert run.outcome.status == "succeeded"
    with pytest.raises(TypeError, match="record"):
        procedure.run(input={"account": "one"})
    run._raw = run._raw.model_copy(update={"status": "halted", "result": None})
    assert not run.succeeded
    with pytest.raises(ValueError, match="no successful output"):
        _ = run.result


def test_sdk_line_run_carries_the_asserted_identity_occurrence_and_event(tmp_path: Path) -> None:
    _workspace(tmp_path)

    class LineClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.line_request: dict[str, object] = {}

        def run_line(
            self,
            _instance_id: str,
            line: str,
            **values: object,
        ) -> api.ProcedureRunState:
            self.line_request = {"line": line, **values}
            return api.ProcedureRunState(
                run_id="RUN-" + "b" * 64,
                procedure_identity={"kind": "Procedure", "name": "daily-summary"},
                procedure_artifact_digest=_DIGEST,
                bound_coordinate=_COORDINATE,
                head_at_admission=_COORDINATE,
                lane="current",
                evaluation_time=str(values["evaluation_time"]),
                status="succeeded",
                pending_inputs=[],
                outcomes=[],
                next_operation={"kind": "done"},
                result={"ok": True},
            )

    client = LineClient()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )

    line = pb.line("daily-line")
    assert line.run(occurrence_id="sha256:" + "c" * 64).status == "succeeded"
    assert client.line_request == {
        "resolution_contract": None,
        "event": None,
        "repeat": False,
        "line": "daily-line",
        "occurrence_id": "sha256:" + "c" * 64,
        "evaluation_time": "2026-08-24T12:00:00+00:00",
    }

    contract = api.ResolutionContractReference(
        identity={"kind": "ResolutionContract", "name": "test"},
        artifact_digest=_DIGEST,
        coordinate=_COORDINATE.model_dump(),
    )
    event = api.TriggerEventReference(
        run_id="RUN-anchor",
        partition_id="direct:anchor",
        sequence=1,
        record_digest=_DIGEST,
    )
    line.run(resolution_contract=contract, event=event, repeat=True)
    assert client.line_request["resolution_contract"] == contract
    assert client.line_request["event"] == event
    assert client.line_request["repeat"] is True


def test_procedure_run_track_record_reads_the_procedure_card_from_get(tmp_path: Path) -> None:
    """The track record comes from `get` on the run's Procedure, not a search row.

    Search rows never carried ``track_record``, so the old read always answered
    None. The Procedure card does, one entry per accepted promotion.
    """

    _workspace(tmp_path)
    from cruxible_client.contracts.get_reads import (
        GetCoordinate,
        GetProcedureCard,
        GetProcedureTrackRecord,
        GetResult,
    )

    entry = GetProcedureTrackRecord(
        promotion="daily-summary-runs",
        first_sequence=1,
        last_sequence=4,
        output={"succeeded": 3, "halted": 1},
        output_digest="sha256:" + "6" * 64,
        promotion_digest="sha256:" + "7" * 64,
    )

    class TrackRecordClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.gets: list[Any] = []

        def get(self, _instance_id: str, *, request: Any) -> GetResult:
            self.gets.append(request)
            return GetResult(
                ref=request.ref,
                kind="procedure",
                detail=request.detail,
                card=GetProcedureCard(
                    procedure="daily-summary",
                    inputs={"input": "daily-summary-input"},
                    runnable="direct",
                    track_record=(entry,),
                ),
                coordinate=GetCoordinate(git_oid="a" * 12, generation=3),
                accepted_coordinate=_COORDINATE,
                evaluation_time=datetime(2026, 8, 24, 12, tzinfo=UTC),
            )

    client = TrackRecordClient()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    from cruxible_client.authoring.sdk import ProcedureRun

    run = ProcedureRun(
        pb,
        api.ProcedureRunState(
            run_id="RUN-" + "a" * 64,
            procedure_identity={"kind": "Procedure", "name": "daily-summary"},
            procedure_artifact_digest=_DIGEST,
            bound_coordinate=_COORDINATE,
            head_at_admission=_COORDINATE,
            lane="current",
            evaluation_time="2026-08-24T12:00:00+00:00",
            status="succeeded",
            pending_inputs=[],
            outcomes=[],
            next_operation={"kind": "done"},
            result={"ok": True},
        ),
    )

    assert run.track_record == (entry,)
    (request,) = client.gets
    assert request.ref == "Procedure:daily-summary"
    assert request.detail == "summary"


def test_subject_draft_prepares_through_the_authoring_coordinator(tmp_path: Path) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    subject = pb.subject(
        subject="secops.policy/patch-sla",
        pins=(),
        lifecycle=ArtifactLifecycle(),
    )

    prepared = subject.prepare()

    assert prepared.intent_id == "AIT-" + "1" * 32
    assert client.compiled is not None
    assert client.compiled["payload"]["tag"] == "playbill-subject-authoring-payload-v1"
    assert client.compiled["payload"]["subject"]["identity"] == {
        "kind": "Subject",
        "name": "secops.policy/patch-sla",
    }


def test_cold_claim_prepares_one_payload_with_dependencies_and_program_stamp(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    subject = pb.subject(
        subject="secops.policy/patch-sla",
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
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
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


def test_subject_values_build_typed_objects_and_only_refs_pin_a_coordinate(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
    )
    common: dict[str, object] = {
        "subject": "sec.vuln/cve-2026-0001",
        "predicate": "sec.vuln.affects_package",
        "role": ClaimRole.OBSERVATION,
        "rationale": "The vulnerability affects this accepted package.",
        "supported_by": None,
        "copied_from": None,
        "self_source": "affected package",
        "qualifier": None,
        "effective_period": None,
        "revises": None,
        "dispositions": {},
        "subject_definition": None,
        "claim_type_definition": None,
    }
    subject_ref = SubjectRef("sec.package/demo", pb.coordinate)

    by_ref = pb.claim(value=subject_ref, **common)  # type: ignore[arg-type]
    by_address = pb.claim(value="sec.package/demo", **common)  # type: ignore[arg-type]

    assert isinstance(by_ref.payload.statement.object, SubjectClaimObject)
    assert by_ref.payload.statement.object.address.artifact_path == (
        "subjects/sec.package/demo.json"
    )
    assert by_address.payload.statement.object == by_ref.payload.statement.object
    assert [item.payload_path for item in by_ref.reference_expectations] == [
        "statement.object.address"
    ]
    assert by_address.reference_expectations == ()


def test_address_shaped_strings_follow_the_accepted_claim_type_object_kind(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    client.claim_type_object_kinds.update(
        {
            "docs.reference": "literal",
            "sec.vuln.affects_package": "subject",
        }
    )
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )
    common: dict[str, object] = {
        "subject": "sec.vuln/cve-2026-0001",
        "role": ClaimRole.OBSERVATION,
        "rationale": "Preserve the ClaimType's governed object kind.",
        "supported_by": None,
        "copied_from": None,
        "self_source": "object kind matrix",
        "qualifier": None,
        "effective_period": None,
        "revises": None,
        "dispositions": {},
        "subject_definition": None,
        "claim_type_definition": None,
    }

    literal = pb.claim(predicate="docs.reference", value="docs/readme", **common)  # type: ignore[arg-type]
    subject = pb.claim(
        predicate="sec.vuln.affects_package",
        value="sec.package/demo",
        **common,  # type: ignore[arg-type]
    )

    assert literal.payload.statement.object == LiteralClaimObject(value="docs/readme")
    assert subject.payload.statement.object == SubjectClaimObject(
        address=SemanticAddress.whole_artifact("subjects/sec.package/demo.json")
    )


# Every keyword `Cruxible.claim` requires but the object-kind oracles do not vary.
_OBJECT_KIND_CLAIM_DEFAULTS: dict[str, Any] = {
    "subject": "sec.vuln/cve-2026-0001",
    "role": ClaimRole.OBSERVATION,
    "supported_by": None,
    "copied_from": None,
    "qualifier": None,
    "effective_period": None,
    "revises": None,
    "dispositions": {},
    "subject_definition": None,
    "claim_type_definition": None,
}


def test_address_shaped_string_on_an_exact_content_type_defers_to_the_daemon(
    tmp_path: Path,
) -> None:
    """An exact_content predicate must not raise a bare, repair-less ValueError.

    The SDK cannot author an ExactContentClaimObject, so it builds the literal
    shape and lets the daemon answer with the typed
    `cruxible.claim.object_kind_mismatch` refusal
    (tests/test_authoring/test_authoring_preflight.py::
    test_claim_object_kind_mismatch_is_a_typed_preflight_refusal).
    """

    _workspace(tmp_path)
    client = _Client()
    client.claim_type_object_kinds.update({"docs.exact": "exact_content"})
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )

    draft = pb.claim(
        predicate="docs.exact",
        value="docs/readme",
        rationale="An exact-content predicate keeps the daemon as the authority.",
        self_source="exact content object kind",
        **_OBJECT_KIND_CLAIM_DEFAULTS,  # type: ignore[arg-type]
    )

    assert draft.payload.statement.object == LiteralClaimObject(value="docs/readme")
    assert draft.reference_expectations == ()


def test_an_unknown_claim_type_object_kind_falls_back_to_the_literal_shape(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    client = _Client()
    client.claim_type_object_kinds.update({"docs.future": "kind_from_a_newer_daemon"})
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )

    draft = pb.claim(
        predicate="docs.future",
        value="docs/readme",
        rationale="Client/daemon skew stays a typed daemon refusal.",
        self_source="unknown object kind",
        **_OBJECT_KIND_CLAIM_DEFAULTS,  # type: ignore[arg-type]
    )

    assert draft.payload.statement.object == LiteralClaimObject(value="docs/readme")


def test_claim_type_builder_writes_v7_with_identity_rules(tmp_path: Path) -> None:
    _workspace(tmp_path)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: datetime(2026, 8, 24, 12, tzinfo=UTC),
    )
    policy = ClaimAttestationConsequencePolicy(
        rules=(
            ClaimAttestationConsequenceRule(
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
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
        pins=(),
        evidence_freshness=None,
        attestation_consequence_policy=policy,
    )

    assert draft.definition.artifact_format == "playbill-claim-type-v7"
    assert draft.definition.attestation_consequence_policy == policy
    assert draft.definition.evidence_requirement == "self"
    assert draft.definition.revision_evidence == "replace"
    (rule,) = draft.definition.evidence_admission_policy.rules
    assert [item.target.qualified for item in rule.capture_contracts] == [
        foreign_source_capture_contract("corpus.runbook").identity.qualified
    ]


def test_claim_requires_exactly_one_explicit_source_role(tmp_path: Path) -> None:
    _workspace(tmp_path)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
        "subjects/secops.policy/patch-sla.json"
    )
    assert fresh.reference_expectations == ()
    assert revision.reference_expectations == ()


def test_capture_ref_builds_v3_authoring_and_owns_the_contract_expectation(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
    )
    capture = CaptureRef(
        capture_digest="sha256:" + "7" * 64,
        contract_address="capture-contracts/repo.work-items.json",
        coordinate=pb.coordinate,
        citation_role="evidence",
    )

    draft = pb.claim(
        subject="secops.policy/patch-sla",
        predicate="secops.policy.patch_sla",
        value=48,
        role=ClaimRole.NORMATIVE,
        rationale="Reuse the already accepted observation.",
        supported_by=capture,
        copied_from=None,
        self_source=None,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )

    assert isinstance(draft.payload, ClaimAuthoringPayload)
    assert draft.payload.source.capture_digest == capture.capture_digest
    assert [item.payload_path for item in draft.reference_expectations] == ["source"]
    assert draft.reference_expectations[0].address == capture.contract_address


def test_claim_view_mints_capture_refs_from_typed_admission_accounts(tmp_path: Path) -> None:
    _workspace(tmp_path)

    class ClaimClient(_Client):
        def _claim_view(
            self,
            _instance_id: str,
            _identity: str,
            **_values: Any,
        ) -> api.ClaimViewRecord:
            return api.ClaimViewRecord(
                tag="playbill-claim-read-v2",
                coordinate_kind="canonical",
                coordinate=_COORDINATE,
                envelope={"identity": "Claim:CLM-typed", "revision": 2},
                facts=[
                    {
                        "schema_id": "cruxible.claim.statement",
                        "value": {
                            "subject": {"artifact_path": "subjects/secops.policy/a.json"},
                            "predicate": "secops.policy.patch_sla",
                            "qualifier": None,
                            "role": "normative",
                            "object": {"kind": "literal", "value": 48},
                        },
                    },
                    {
                        "schema_id": "cruxible.claim.lifecycle",
                        "value": {"lifecycle": {"state": "live"}},
                    },
                    {
                        "schema_id": "cruxible.claim.current_verdict",
                        "value": {"verdict": "supported"},
                    },
                ],
                admission_evaluation_time="2026-08-28T12:00:00Z",
                statement=api.ClaimStatementCard(
                    subject={
                        "artifact_path": "subjects/secops.policy/a.json",
                        "selector": {"scheme": "artifact-v1", "value": ""},
                    },
                    predicate="secops.policy.patch_sla",
                    object={"kind": "literal", "value": 48},
                    role="normative",
                    qualifier=None,
                    lifecycle="live",
                ),
                admission_accounts=[
                    api.CaptureAdmissionAccount(
                        tag="playbill-capture-admission-account-v1",
                        citation_id="sha256:" + "6" * 64,
                        capture_digest="sha256:" + "7" * 64,
                        citation_role="evidence",
                        citation_origin="independent",
                        capture_contract_identity="CaptureContract:repo.work-items",
                        capture_contract_digest="sha256:" + "8" * 64,
                        status="admitted",
                        decisions=[],
                    )
                ],
            )

    pb = Cruxible._from_client(  # type: ignore[arg-type]
        ClaimClient(),
        instance_id="inst_test",
        workspace=tmp_path,
    )

    view = pb.get("Claim:CLM-typed").value
    assert isinstance(view, ClaimView)
    (capture,) = view.captures

    assert capture.capture_digest == "sha256:" + "7" * 64
    assert capture.contract_address == "capture-contracts/repo.work-items.json"
    assert capture.citation_role == "evidence"


def test_capture_ref_from_a_copy_cannot_be_promoted_to_independent_evidence(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        _Client(),
        instance_id="inst_test",
        workspace=tmp_path,
    )
    capture = CaptureRef(
        capture_digest="sha256:" + "7" * 64,
        contract_address="capture-contracts/repo.work-items.json",
        coordinate=pb.coordinate,
        citation_role="copy",
    )

    with pytest.raises(ValueError, match="cannot be promoted to independent evidence"):
        pb.claim(
            subject="secops.policy/patch-sla",
            predicate="secops.policy.patch_sla",
            value=48,
            role=ClaimRole.NORMATIVE,
            rationale="A projection copy is not independent support.",
            supported_by=capture,
            copied_from=None,
            self_source=None,
            qualifier=None,
            effective_period=None,
            revises=None,
            dispositions={},
            subject_definition=None,
            claim_type_definition=None,
        )
    assert capture.coordinate == pb.coordinate


def test_connect_accepts_current_daemon_package_with_current_served_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    _workspace(tmp_path)
    calls: list[str] = []

    class _CurrentClient(_Client):
        def __init__(self, **_values: object) -> None:
            super().__init__()
            calls.append("connect")

        def _version_info(self) -> tuple[str, str]:
            calls.append("version_info")
            return DAEMON_VERSION, SDK_CONTRACT_SNAPSHOT_DIGEST

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("cruxible_client.authoring.sdk.CruxibleClient", _CurrentClient)

    assert CLIENT_VERSION == DAEMON_VERSION
    playbill = Cruxible.connect(
        target="http://explicit",
        instance="inst_test",
        workspace=tmp_path,
    )
    try:
        assert playbill.coordinate.git_oid == _COORDINATE.git_oid
        assert playbill.coordinate.generation_root == _COORDINATE.generation_root
        assert calls == ["connect", "version_info"]
    finally:
        playbill.close()
    assert calls == ["connect", "version_info", "close"]


def test_connect_refuses_mismatched_served_snapshot_before_instance_io(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    context = tmp_path / "context.json"
    context.write_text(
        '{"server_url":"http://remembered","server_socket":"/'
        'tmp/remembered.sock",'
        '"instance_id":"inst_test"}\n',
        encoding="utf-8",
    )
    calls: list[str] = []
    connection: dict[str, object] = {}

    daemon_snapshot_digest = "sha256:" + "9" * 64

    class _MismatchedClient:
        def __init__(self, **values: object) -> None:
            calls.append("connect")
            connection.update(values)

        def _version_info(self) -> tuple[str, str]:
            calls.append("version_info")
            return DAEMON_VERSION, daemon_snapshot_digest

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("cruxible_client.authoring.sdk.CruxibleClient", _MismatchedClient)

    try:
        Cruxible.connect(
            context=context,
            target="http://explicit",
            instance="inst_test",
            workspace=tmp_path,
        )
    except IncompatibleDaemonVersion as exc:
        assert exc.client_version == CLIENT_VERSION
        assert exc.daemon_version == DAEMON_VERSION
        assert exc.client_snapshot_digest == SDK_CONTRACT_SNAPSHOT_DIGEST
        assert exc.daemon_snapshot_digest == daemon_snapshot_digest
        message = str(exc)
        assert f"client_version={CLIENT_VERSION}" in message
        assert f"daemon_version={DAEMON_VERSION}" in message
        assert f"client_snapshot_digest={SDK_CONTRACT_SNAPSHOT_DIGEST}" in message
        assert f"daemon_snapshot_digest={daemon_snapshot_digest}" in message
        assert "upgrade the client or daemon" in message
    else:  # pragma: no cover - the handshake must fail closed
        raise AssertionError("mismatched daemon snapshot was accepted")
    assert calls == ["connect", "version_info", "close"]
    assert connection["base_url"] == "http://explicit"
    assert connection["socket_path"] is None


def test_refusal_diagnostic_maps_exact_payload_path_to_the_call_expression(
    tmp_path: Path,
) -> None:
    _workspace(tmp_path)

    class _RefusingClient(_Client):
        def compile_authoring(
            self, _instance_id: str, **values: object
        ) -> api.AuthoringPreflightResult:
            self.compiled = dict(values)
            return stub_preflight_result(
                verdict="refused",
                intent_id="AIT-" + "1" * 32,
                diagnostics=(
                    stub_diagnostic(
                        "cruxible.test.role_refused",
                        "role is not admitted",
                        stage="admission",
                        offending_element="statement.role",
                    ),
                ),
            )

    pb = Cruxible._from_client(  # type: ignore[arg-type]
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
        subject_definition=None,
        claim_type_definition=None,
    ).prepare()

    diagnostic = intent.diagnostics[0]
    assert diagnostic.offending_element == "statement.role"
    assert diagnostic.call_site is not None
    assert diagnostic.call_site.expression == "ClaimRole.NORMATIVE"


def _staged_claim_type(predicate: str, *, object_kind: str) -> ClaimType:
    """One whole ClaimType a change set can define, minimal but real."""

    return ClaimType(
        artifact_format="playbill-claim-type-v1",
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=("sec.vuln",),
        allowed_object_subject_kinds=("sec.package",) if object_kind == "subject" else (),
        object_kind=object_kind,  # type: ignore[arg-type]
        literal_schema={"type": "string"} if object_kind == "literal" else None,
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(rules=()),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def test_exact_content_authors_the_object_the_wire_has_always_carried(
    tmp_path: Path,
) -> None:
    """Card 84: rulings and method laws are exact-content Claims, and the SDK can say so."""

    _workspace(tmp_path)
    client = _Client()
    client.claim_type_object_kinds.update({"docs.exact": "exact_content"})
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )

    draft = pb.claim(
        predicate="docs.exact",
        value=ExactContent("the ruling exactly as it was written\n"),
        rationale="The statement is the wording, so the object is the wording.",
        self_source="the ruling exactly as it was written\n",
        **_OBJECT_KIND_CLAIM_DEFAULTS,  # type: ignore[arg-type]
    )

    assert draft.payload.statement.object == AuthoringExactContentObject(
        content_base64=base64.b64encode(b"the ruling exactly as it was written\n").decode("ascii")
    )
    assert draft.payload.statement.object.content == b"the ruling exactly as it was written\n"


def test_exact_content_keeps_bytes_that_are_not_text(tmp_path: Path) -> None:
    """A body whose exact bytes matter more than its reading is still one object."""

    _workspace(tmp_path)
    client = _Client()
    client.claim_type_object_kinds.update({"docs.exact": "exact_content"})
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )

    draft = pb.claim(
        predicate="docs.exact",
        value=ExactContent(b"\xff\xfe not utf-8"),
        rationale="The bytes are the object.",
        self_source="binary body",
        **_OBJECT_KIND_CLAIM_DEFAULTS,  # type: ignore[arg-type]
    )

    assert draft.payload.statement.object.content == b"\xff\xfe not utf-8"


def test_exact_content_on_a_literal_predicate_refuses_before_the_wire(
    tmp_path: Path,
) -> None:
    """The preflight kind check: exact bytes name their own kind, so the only
    question left is whether the predicate states one."""

    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )

    with pytest.raises(ExactContentTypeError) as refused:
        pb.claim(
            predicate="docs.plain",
            value=ExactContent("a body a literal predicate cannot hold"),
            rationale="The predicate states a literal.",
            self_source="literal predicate",
            **_OBJECT_KIND_CLAIM_DEFAULTS,  # type: ignore[arg-type]
        )

    assert refused.value.code == "cruxible.sdk.exact_content_claim_type_mismatch"
    assert "docs.plain" in str(refused.value)
    assert "literal" in str(refused.value)


def test_a_string_value_reads_the_object_kind_from_the_sets_own_definition(
    tmp_path: Path,
) -> None:
    """Card 85: the same set already answered, so the accepted coordinate is not asked.

    The typed ref path never needed a lookup. The string convenience path did,
    and it went straight past the definition sitting in this very set to read
    the accepted ClaimType -- which in a first generation is a coordinate that
    does not exist yet.
    """

    _workspace(tmp_path)
    client = _Client()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
    )
    reads_before = client.claim_type_reads

    draft = pb.changes(rationale="Define the predicate and state one Claim under it.")
    draft.claim_type(_staged_claim_type("sec.vuln.affects", object_kind="subject"))
    draft.claim(
        subject="sec.vuln/cve-2026-0001",
        predicate="sec.vuln.affects",
        value="sec.package/click",
        role=ClaimRole.OBSERVATION,
        rationale="The advisory names this package.",
        supported_by=None,
        copied_from=None,
        self_source="affects: click\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )

    compiled = draft._compiled()
    claim = next(
        member
        for member in compiled.payload.members
        if member.model_dump(mode="json")["tag"].startswith("playbill-claim-authoring-payload-")
    )

    assert isinstance(claim.statement.object, SubjectClaimObject)
    assert claim.statement.object.address.artifact_path == "subjects/sec.package/click.json"
    assert client.claim_type_reads == reads_before, "the set's own definition answered"


def test_sdk_measure_and_readings_carry_the_run_and_observation_basis(tmp_path: Path) -> None:
    _workspace(tmp_path)
    observation = datetime(2026, 8, 24, 12, tzinfo=UTC)
    coordinate = AcceptedCoordinate.model_validate(_COORDINATE.model_dump(mode="json"))

    class MeasureClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.measure_requests: list[api.ProcedureMeasureRequest] = []
            self.readings_requests: list[api.ProcedureReadingsRequest] = []

        def measure_procedure(
            self,
            _instance_id: str,
            name: str,
            *,
            request: api.ProcedureMeasureRequest,
        ) -> api.ProcedureMeasureResult:
            self.measure_requests.append(request)
            return api.ProcedureMeasureResult(
                procedure_identity={"kind": "Procedure", "name": name},
                procedure_artifact_digest=_DIGEST,
                activation_coordinate=coordinate,
                observation_coordinate=coordinate,
                observation_time=observation,
                run_id=request.run_id,
                rows=(
                    api.ProcedureMeasurementRow(
                        measurement_name="rows-present",
                        measurement_kind="accepted_query",
                        contract_id="RSC-" + "a" * 32,
                        activation_id="RSA-" + "a" * 32,
                        subject_grain="procedure_unit",
                        subject=SemanticAddress.procedure_unit("procedures/daily-summary.json"),
                        status="pending",
                        eligibility=api.ProcedureMeasurementEligibility(
                            activation_coordinate=coordinate,
                            activated_at=observation,
                            check_at=observation,
                            expires_at=observation.replace(hour=13),
                            observation_coordinate=coordinate,
                            observation_time=observation,
                            window="before_check",
                        ),
                        reading_status="no_resolution",
                        detail="the measurement window has not opened",
                    ),
                ),
            )

        def list_procedure_readings(
            self,
            _instance_id: str,
            name: str,
            *,
            request: api.ProcedureReadingsRequest,
        ) -> api.ProcedureReadingsResult:
            self.readings_requests.append(request)
            return api.ProcedureReadingsResult(
                procedure_identity={"kind": "Procedure", "name": name},
                procedure_artifact_digest=_DIGEST,
                activation_coordinate=coordinate,
                observation_coordinate=coordinate,
                observation_time=observation,
                contracts=(),
                readings=(),
            )

    client = MeasureClient()
    pb = Cruxible._from_client(  # type: ignore[arg-type]
        client,
        instance_id="inst_test",
        workspace=tmp_path,
        clock=lambda: observation,
    )
    procedure = pb.accepted_procedure("daily-summary")

    batch = procedure.measure(run="RUN-" + "a" * 64, measurements=("rows-present",))
    assert batch.run_id == "RUN-" + "a" * 64
    assert batch["rows-present"].status == "pending"
    assert batch["rows-present"].reading_status == "no_resolution"
    with pytest.raises(KeyError):
        batch["absent"]
    sent = client.measure_requests[0]
    assert sent.run_id == "RUN-" + "a" * 64
    assert sent.measurement_names == ("rows-present",)
    assert sent.evaluation_time == observation

    page = procedure.readings(measurements=("rows-present",), limit=10)
    assert page.readings == ()
    listed = client.readings_requests[0]
    assert listed.limit == 10 and listed.measurement_names == ("rows-present",)
    assert listed.evaluation_time == observation
