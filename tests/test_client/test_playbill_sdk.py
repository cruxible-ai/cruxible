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
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate

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

    def list_playbill_curation(
        self, _instance_id: str, **values: object
    ) -> api.PlaybillCurationListResult:
        self.curation_observation = values["workspace_observation"]
        return api.PlaybillCurationListResult(
            coordinate=_COORDINATE,
            generation=4,
            operational_head_digest="sha256:" + "6" * 64,
            items=[],
            observation_coverage={
                "tag": "playbill-curation-observation-coverage-v1",
                "source_count": 1,
                "observed_block_count": 0,
                "omitted_source_count": 0,
                "omissions": [],
            },
        )

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
