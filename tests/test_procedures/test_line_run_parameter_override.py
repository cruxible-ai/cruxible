"""A manual Line run may override the Line's parameters (D-R39).

The override is checked against the Procedure's params Contract and is the
run's own input, so it is in the run's identity. A run whose parameters differ
from the Line's runs WITHOUT the Line's mandate: it may propose, never settle.
A run that names the Line's own values is the Line's run and keeps its mandate.
Triggers always run on the Line's pinned parameters.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScope,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from tests.core_support._identity_support import accept_tree
from tests.support.mandates import procedure_mandate, scope_changes
from tests.support.operation_world import NOW, actor, author_line, author_procedure
from tests.support.procedures import procedure_limits, procedure_spec
from tests.test_procedures.test_claims_value_upsert import (
    AFFECTED,
    CAPTURE,
    COMPONENT,
    KIND,
    ROW,
    _accepted_affected,
    upsert_world,
)

LINE = "test.override-line"
FIRST = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}]
SECOND = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0002"}]


def override_spec() -> Any:
    """read the file the ``path`` parameter names -> extract -> claims."""

    return procedure_spec(
        "test.override",
        reference_text=True,
        input={},
        output={},
        params={"path": 'string{max_len: 64, pattern: "^data/[a-z]+[.]json$"}'},
        slots={
            "fetch": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "pure",
            }
        },
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=262144),
        steps=[
            {
                "call": "doc",
                "slot": "fetch",
                "captures": {"capture": CAPTURE},
                "input": {
                    "logical_source": "commerce.production.orders",
                    "relative_path": "$params.path",
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 7, "relation": "orders"},
                },
            },
            {
                "extract": "entries",
                "capture": "$steps.doc.capture",
                "records": "/entries/*",
                "row": ROW.ref.spelling,
                "key": {
                    "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                    "advisory": {"at": "cve", "type": f"key_part<{KIND}:cve_id>"},
                },
                "fields": {"cve": {"at": "cve"}, "since": {"at": "since", "parse": "datetime"}},
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": {
                    "kind": COMPONENT,
                    "key": {"purl": "$item.key.purl"},
                    "create": "never",
                },
                "claims": [
                    {
                        "type": AFFECTED,
                        "value": {
                            "kind": KIND,
                            "key": {"cve_id": "$item.fields.cve"},
                            "create": "if_absent",
                        },
                    }
                ],
            },
        ],
    )


def _settle_world(tmp_path: Path) -> tuple[Any, Path]:
    """The Line reads data/doc.json and holds a settle grant for its Claims and CVEs."""

    instance, root = upsert_world(tmp_path, FIRST)
    (root / "data" / "other.json").write_text(json.dumps({"entries": SECOND, "count": 1}))
    spec = override_spec()
    author_procedure(instance, spec)
    author_line(
        instance,
        name=LINE,
        procedure=spec.procedure,
        parameters={"path": "data/doc.json"},
        max_authority="settle",
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(LINE)], path=line_spec_path(LINE))
    affected = parse_claim_type(tree[claim_type_path(AFFECTED)], path=claim_type_path(AFFECTED))
    grant = procedure_mandate(
        "settle-grant",
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        grants="settle",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=affected.identity,
                    artifact_digest=claim_type_digest(affected).tagged,
                ),
                changes=scope_changes("create"),
                binding_subject_role="subject",
            ),
        ),
        subject_creation=(KIND,),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(grant.identity.name): render_procedure_mandate(grant)},
        name="grant",
    )
    return instance, root


def run(
    instance: Any,
    root: Path,
    *,
    parameters: dict[str, Any] | None = None,
    minute: int = 0,
    trigger_fire: Any = None,
) -> Any:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=LINE,
        request=LineRunRequest(line=LINE, parameters=parameters),
        actor_context=actor(),
        caller_rung=3,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=ProviderRuntimeOperator(root.parent / "state"),
        workspace_file_reader=WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
        trigger_fire=trigger_fire,
    )


def _admission(instance: Any, state: Any) -> Any:
    from tests.test_procedures.test_trigger_capture_inputs import admission

    return admission(instance, state.run_id).admission


def test_the_lines_own_run_settles_under_its_mandate(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    assert _accepted_affected(instance) == {"CVE-2026-0001"}


def test_an_overridden_run_never_settles_it_proposes(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    base = instance.accepted_coordinate()
    state = run(instance, root, parameters={"path": "data/other.json"})
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    # The settle terminal falls back to a proposal: the run's authority is capped.
    assert egress.settle_outcome == "proposed", egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None, egress
    # Nothing settled: the accepted head did not move; the Claim waits for review.
    assert instance.accepted_coordinate() == base
    assert _accepted_affected(instance) == set()
    # The override is the run's input: in its admission, and so in its identity.
    admitted = _admission(instance, state)
    assert admitted.parameters == {"path": "data/other.json"}


def test_naming_the_lines_own_values_is_the_lines_run(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    state = run(instance, root, parameters={"path": "data/doc.json"})
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "settled", egress


def test_an_override_the_params_contract_refuses_never_runs(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    base = instance.accepted_coordinate()
    state = run(instance, root, parameters={"path": "../etc/passwd"})
    assert state.status == "node_refused", state.status
    assert state.terminal.code == "contract_input_refused", state.terminal
    assert instance.accepted_coordinate() == base
    undeclared = run(instance, root, parameters={"unknown": 1}, minute=1)
    assert undeclared.status == "node_refused", undeclared.status
    assert undeclared.terminal.code == "contract_input_refused", undeclared.terminal


def test_two_overrides_are_two_runs(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    one = run(instance, root, parameters={"path": "data/other.json"})
    two = run(instance, root, parameters={"path": "data/doc.json"}, minute=1)
    assert one.run_id != two.run_id
    assert _admission(instance, one).semantic_replay_key_digest != (
        _admission(instance, two).semantic_replay_key_digest
    )


def test_a_triggers_occurrence_carries_no_override(tmp_path: Path) -> None:
    from cruxible_core.service.procedures.procedure_runs import TriggerFire

    instance, root = _settle_world(tmp_path)
    with pytest.raises(ExecutionError, match="Line's own parameters"):
        run(
            instance,
            root,
            parameters={"path": "data/other.json"},
            trigger_fire=TriggerFire(trigger="test.any-trigger"),
        )


def test_the_sdk_sends_the_override_as_the_line_run_requests_parameters() -> None:
    import httpx

    from cruxible_client.transport.http import CruxibleClient
    from cruxible_core.service.procedures.procedure_runs import LineRunRequest

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(500, json={"detail": "stop here"})

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[assignment]
        base_url="http://cruxible", transport=httpx.MockTransport(handler)
    )
    for parameters in ({"path": "data/other.json"}, None):
        with pytest.raises(Exception):  # noqa: B017 - the stub answers an error
            client.run_line("inst_a", LINE, occurrence_id=None, parameters=parameters)
    with_override, without = bodies
    assert LineRunRequest.model_validate(with_override).parameters == {"path": "data/other.json"}
    # A run that names no override sends none: the Line's own request bytes.
    assert "parameters" not in without
    assert LineRunRequest.model_validate(without).parameters is None
