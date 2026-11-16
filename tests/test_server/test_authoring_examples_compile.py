"""Every `authoring example` template compiles on a fresh instance.

`authoring example NAME` prints a payload to edit and send back. A template the
product itself refuses for its shape (a closed step, a retired field) is worse
than none, so each one is compiled here exactly as printed: the only refusals
allowed are the prerequisites a fresh instance does not hold yet, which the
template names (a ClaimType, a SubjectKind, the Procedure a Line runs). And the
Procedure, Line, Trigger and mandate templates, given their prerequisites, are
accepted together as one change set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_NAMES,
    authoring_example,
    line_example,
    procedure_example,
    procedure_mandate_example,
    subject_kind_example,
    trigger_example,
)
from cruxible_client.authoring.inputs import ChangeSetInput, ClaimTypeInput
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.errors import CoreError
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicy,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.shapes import StringType
from cruxible_client.transport.http import CruxibleClient

#: What each template refuses on a fresh instance: the prerequisites it names.
FRESH_PREREQUISITES: dict[str, frozenset[str]] = {
    "claim-existing-capture": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-self-source": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-subject-relation": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-revision": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-adjudicate-contradicting-evidence": frozenset(
        {"cruxible.authoring.claim_type_not_found"}
    ),
    "claim-cite-supporting-evidence": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-adjudicate-unreviewed-evidence": frozenset({"cruxible.authoring.claim_type_not_found"}),
    # The CaptureKind it reads: its layout declares the records the extract names.
    "procedure": frozenset({"cruxible.authoring.artifact_reference_unresolved"}),
    "query-claims-by-type": frozenset({"cruxible.authoring.claim_type_missing"}),
    "query-ontology": frozenset(),
    "query-procedures": frozenset(),
    "subject": frozenset({"cruxible.subject.kind_undeclared"}),
    "subject-kind": frozenset(),
    "approval-policy": frozenset(),
    "procedure-runtime-policy": frozenset(),
    "procedure-mandate": frozenset({"cruxible.authoring.procedure_mandate_line_missing"}),
    "line": frozenset({"cruxible.authoring.line_procedure_missing"}),
    "trigger": frozenset({"cruxible.authoring.trigger_line_missing"}),
    "change-set": frozenset({"cruxible.authoring.claim_type_not_found"}),
    "claim-type-succession": frozenset({"cruxible.authoring.claim_type_succession_invalid"}),
}
#: Observes local bytes, so it goes through `authoring bind`, never compile.
BIND_ONLY = {"claim-flow-a": "cruxible.authoring.working_selection_requires_bind"}


def _transport(http: TestClient) -> CruxibleClient:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    return transport


def _diagnostic_codes(value: Any) -> set[str]:
    """Every diagnostic code in a preflight result, wherever the result nests it."""

    found: set[str] = set()
    if isinstance(value, dict):
        if (
            isinstance(value.get("code"), str)
            and "message" in value
            and value.get("severity", "error") == "error"
        ):
            found.add(value["code"])
        for item in value.values():
            found |= _diagnostic_codes(item)
    elif isinstance(value, list):
        for item in value:
            found |= _diagnostic_codes(item)
    return found


def _first_diagnostic(value: Any, code: str) -> dict[str, Any]:
    """The first diagnostic with this code, wherever the result nests it."""

    if isinstance(value, dict):
        if value.get("code") == code and "message" in value:
            return value
        children: Any = value.values()
    elif isinstance(value, list):
        children = value
    else:
        children = ()
    for item in children:
        try:
            return _first_diagnostic(item, code)
        except LookupError:
            continue
    raise LookupError(code)


def _example(name: str) -> Any:
    if name.startswith("claim-adjudicate") or name == "claim-cite-supporting-evidence":
        return authoring_example(
            name,  # type: ignore[arg-type]
            claim_id="CLM-" + "0" * 32,
            capture_digest="sha256:" + "0" * 64,
        )
    return authoring_example(name)  # type: ignore[arg-type]


def test_every_example_compiles_on_a_fresh_instance(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _ = playbill_http
    transport = _transport(http)
    assert set(AUTHORING_EXAMPLE_NAMES) == set(FRESH_PREREQUISITES) | set(BIND_ONLY)
    for name in AUTHORING_EXAMPLE_NAMES:
        payload = _example(name).model_dump(mode="json")
        if name in BIND_ONLY:
            try:
                transport.compile_authoring_input(instance_id, input=payload)
            except CoreError as exc:
                assert exc.error_code == BIND_ONLY[name], (name, str(exc))
            else:  # pragma: no cover - decisive guard
                raise AssertionError(f"{name} compiled without authoring bind")
            continue
        result = transport.compile_authoring_input(instance_id, input=payload)
        codes = _diagnostic_codes(result.model_dump(mode="json"))
        assert codes == FRESH_PREREQUISITES[name], (name, codes)
        assert (result.verdict == "passed") == (not codes), (name, result.verdict)


def _status_claim_type() -> ClaimType:
    """The ClaimType the Procedure example states: Captures of its kind are evidence."""

    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name="project.work_item.status"),
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        value=StringType(max_len=64),
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="work-item-file",
                    claim_roles=("observation",),
                    capture_contracts=("project.work-items",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        admission_policy=ClaimAdmissionPolicy(),
        resolution_policy=ClaimResolutionPolicy(
            cardinality="one", eligible_verdicts=("supported",), selector="only_contender"
        ),
        default_role="observation",
        evidence_requirement="captured",
        revision_evidence="replace",
    )


def _landed(transport: CruxibleClient, instance_id: str, payload: dict[str, Any]) -> None:
    submitted = transport.submit_authoring_input(instance_id, input=payload)
    assert submitted.model_dump(mode="json")["outcome"]["status"] == "landed", submitted


def test_the_procedure_line_trigger_and_mandate_examples_are_accepted_together(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    http, instance_id, _ = playbill_http
    transport = _transport(http)
    _land_procedure_prerequisites(transport, instance_id)

    automation = ChangeSetInput(
        kind="change_set",
        members=(
            procedure_example(),
            line_example(),
            trigger_example(),
            procedure_mandate_example(),
        ),
        rationale="The example Procedure, its Line, Trigger and mandate.",
    )
    result = transport.compile_authoring_input(
        instance_id, input=automation.model_dump(mode="json")
    )
    assert _diagnostic_codes(result.model_dump(mode="json")) == set()
    assert result.verdict == "passed"


def test_revising_a_procedure_a_line_runs_names_the_line_to_include(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """coldkit-10: the refusal says, in words, to bring the Line's successor along."""

    http, instance_id, _ = playbill_http
    transport = _transport(http)
    _land_procedure_prerequisites(transport, instance_id)
    _landed(
        transport,
        instance_id,
        ChangeSetInput(
            kind="change_set",
            members=(procedure_example(), line_example()),
            rationale="The example Procedure and its Line.",
        ).model_dump(mode="json"),
    )
    revised = procedure_example().model_dump(mode="json")
    revised["spec"]["description"] = "Read the work-item file and state its statuses, again."

    alone = transport.compile_authoring_input(instance_id, input=revised)
    dumped = alone.model_dump(mode="json")
    assert _diagnostic_codes(dumped) == {"cruxible.change_set.incomplete_closure"}
    diagnostic = _first_diagnostic(dumped, "cruxible.change_set.incomplete_closure")
    message = diagnostic["message"]
    assert message.startswith("1 live definition(s) outside this change set pin")
    assert "Line replace-me pins Procedure replace-me" in message
    assert "include each dependent's successor in this same change set" in message
    assert "{" not in message  # no closure record JSON
    (repair,) = diagnostic["repairs"]
    assert repair["kind"] == "include_dependents"
    assert repair["replacement"] == {"dependents": ["lines/replace-me.json"]}

    together = transport.compile_authoring_input(
        instance_id,
        input=ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "rationale": "Revise the Procedure and carry its Line.",
                "members": [revised, line_example().model_dump(mode="json")],
            }
        ).model_dump(mode="json"),
    )
    assert _diagnostic_codes(together.model_dump(mode="json")) == set()


def _land_procedure_prerequisites(transport: CruxibleClient, instance_id: str) -> None:
    # The prerequisites the Procedure example names: how work items are named,
    # its row Contract, then the kind whose layout reads the file, then the
    # ClaimType whose evidence rule names that kind.
    _landed(
        transport,
        instance_id,
        ChangeSetInput.model_validate(
            {
                "kind": "change_set",
                "rationale": "Name work items and type a work-item file's rows.",
                "members": [
                    *(member.model_dump(mode="json") for member in subject_kind_example().members),
                    {
                        "kind": "contract",
                        "name": "project.work-item",
                        "version": 1,
                        "description": "One work item as the file lists it.",
                        "fields": {"status": "string{max_len: 64}"},
                    },
                ],
            }
        ).model_dump(mode="json"),
    )
    kind = transport.propose_capture_contract_input(
        instance_id,
        input={
            "name": "project.work-items",
            "shape": "http_response",
            "decoding": {
                "format": "json",
                "limits": {"decoded_bytes": 1048576, "depth": 8, "records": 1000},
            },
            "layout": {
                "records": {
                    "items": {
                        "at": "/items/*",
                        "key": {"slug": {"at": "slug", "type": "key_part<project.work_item:slug>"}},
                        "row": "project.work-item@1",
                        "fields": {"status": {"at": "status"}},
                    }
                }
            },
        },
        proposal_name="work-items-kind",
    )
    assert kind.outcome is not None, kind
    assert kind.outcome.model_dump(mode="json")["status"] == "landed", kind.outcome
    _landed(
        transport,
        instance_id,
        ChangeSetInput(
            kind="change_set",
            members=(ClaimTypeInput(kind="claim_type", claim_type=_status_claim_type()),),
            rationale="A work item's status, as its file states it.",
        ).model_dump(mode="json"),
    )
