"""Regression (review F-001): a nested run answers for the arm that admitted its root.

A child run inherits its armed parent's Line and actor, but Line dispatch
records only the root run it admitted. Looking the arm up by the child's own
run ID found nothing, so run status, the run card and the proof showed the
child's actor -- the arming credential's principal -- to every reader. The arm
is now resolved through the child's verified ``parent_binding`` to its root.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import PlaybillGetRequest
from cruxible_client.contracts.line_dispatch import LineArmPrincipal
from cruxible_client.contracts.operational_reads import PlaybillGetProcedureRunCard
from cruxible_client.contracts.procedures.results import (
    ProcedureRunAttribution,
    ProcedureRunAttributionWithheld,
    ProcedureRunReceiptWithheld,
)
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.operational_viewer import OperationalViewer
from cruxible_core.service.discovery.runs import procedure_run_status
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)
_ARMED_BY = LineArmPrincipal(
    kind="runtime_credential", credential_id="cred-arm", label="line-operator"
)


def _resolver(credential_id: str) -> str | None:
    return {"cred-arm": "owner", "cred-rotated": "owner", "cred-unbound": None}.get(credential_id)


@pytest.fixture(scope="module")
def nested_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """A Line run whose Procedure invokes a child twice, admitted by a credential arm."""

    from cruxible_client.contracts.acquisition_policies import (
        acquisition_policy_path,
        render_acquisition_policy,
    )
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore
    from cruxible_core.service.procedures.procedure_runs import _accepted_procedure
    from tests.core_support._support import initialize_local
    from tests.test_procedures.test_nested_source_runs import (
        accept_blueprint,
        blueprints,
        payloads,
    )
    from tests.test_procedures.test_procedure_source_runs import (
        _accept_more,
        _line_mandate,
        _policy,
        _run_line,
        _served_line,
    )

    tmp_path = tmp_path_factory.mktemp("nested-arming")
    instance, owner = initialize_local(tmp_path)
    child, parent = blueprints()
    accept_blueprint(instance, owner, child)
    accept_blueprint(instance, owner, parent, child="child")
    accepted = _accepted_procedure(
        instance, name="parent", coordinate=instance.accepted_coordinate()
    )
    policy = _policy()
    line = _served_line(accepted.procedure, policy).model_copy(
        update={"parameters": {"positive": True}}
    )
    mandate = _line_mandate(accepted.procedure)
    _accept_more(
        instance,
        owner,
        {
            acquisition_policy_path(policy.identity.name): render_acquisition_policy(policy),
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="nested-line",
    )
    result, _ = _run_line(instance, tmp_path / "workspace", line)
    assert result.status == "succeeded", result.model_dump_json()
    children = [
        value["receipt"]["run_id"]
        for kind, value in payloads(instance, result.run_id)
        if kind == "child_invocation" and value["verdict"] == "completed"
    ]
    assert len(children) == 2
    # Record the root run exactly as dispatch records what an arm admitted:
    # one pending row naming the ROOT run, under the arming session.
    with LineDispatchStore(instance).locked() as conn:
        conn.execute(
            "INSERT INTO sessions(session_id,line_id,epoch,active,payload) VALUES (?,?,?,?,?)",
            (
                "session-1",
                line.identity.qualified,
                1,
                1,
                json.dumps({"arm_id": "arm-1", "armed_by": _ARMED_BY.model_dump(mode="json")}),
            ),
        )
        conn.execute(
            "INSERT INTO pending(line_id,epoch,occurrence_id,disposition,eligible_at,payload,"
            "run_id,session_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                line.identity.qualified,
                1,
                "sha256:" + "0" * 64,
                "admitted",
                "2026-08-21T12:00:00Z",
                "{}",
                result.run_id,
                "session-1",
            ),
        )
        conn.commit()
    return instance, result.run_id, tuple(children)


_HIDDEN = [
    pytest.param(None, id="no_viewer"),
    pytest.param(
        OperationalViewer(
            credential_id="cred-reviewer",
            admin=False,
            principal_id="reviewer",
            credential_principal=_resolver,
        ),
        id="other_principal",
    ),
    pytest.param(
        OperationalViewer(
            credential_id="cred-unbound", admin=False, credential_principal=_resolver
        ),
        id="unbound_credential",
    ),
]
_SHOWN = [
    pytest.param(OperationalViewer(credential_id="cred-arm", admin=False), id="arming"),
    pytest.param(OperationalViewer(credential_id=None, admin=True), id="admin"),
    pytest.param(
        OperationalViewer(
            credential_id="cred-rotated",
            admin=False,
            principal_id="owner",
            credential_principal=_resolver,
        ),
        id="same_principal",
    ),
]


def _reads(instance: Any, run_id: str, viewer: OperationalViewer | None):  # type: ignore[no-untyped-def]
    state = procedure_run_status(instance, run_id, viewer=viewer)
    card = service_playbill_get(
        instance,
        request=PlaybillGetRequest(ref=f"ProcedureRun:{run_id}"),
        access=_ACCESS,
        viewer=viewer,
    ).card
    proof = service_playbill_get(
        instance,
        request=PlaybillGetRequest(ref=f"ProcedureRun:{run_id}", detail="proof"),
        access=_ACCESS,
        viewer=viewer,
    ).proof
    assert isinstance(card, PlaybillGetProcedureRunCard) and proof is not None
    return state, card, proof


@pytest.mark.parametrize("viewer", _HIDDEN)
def test_every_run_of_an_armed_tree_withholds_the_arming_actor(
    nested_world,  # type: ignore[no-untyped-def]
    viewer: OperationalViewer | None,
) -> None:
    instance, root, children = nested_world

    for run_id in (root, *children):
        state, card, proof = _reads(instance, run_id, viewer)
        assert isinstance(state.attribution, ProcedureRunAttributionWithheld), run_id
        assert isinstance(state.receipt, ProcedureRunReceiptWithheld), run_id
        assert card.actor is None and card.triggered_by is not None
        assert card.triggered_by.armed_by_withheld and card.triggered_by.armed_by is None
        assert proof["attribution"]["tag"] == "playbill-procedure-run-attribution-withheld-v1"
        assert "actor_id" not in proof["attribution"]
        assert proof["receipt"]["tag"] == "playbill-procedure-run-receipt-withheld-v1"
        assert "line-operator" not in str(proof) and "cred-arm" not in str(proof)


@pytest.mark.parametrize("viewer", _SHOWN)
def test_whoever_may_see_the_arm_sees_every_run_of_the_tree(
    nested_world,  # type: ignore[no-untyped-def]
    viewer: OperationalViewer,
) -> None:
    instance, root, children = nested_world

    for run_id in (root, *children):
        state, card, proof = _reads(instance, run_id, viewer)
        assert isinstance(state.attribution, ProcedureRunAttribution), run_id
        assert card.triggered_by is not None
        assert card.triggered_by.armed_by == "line-operator"
        assert card.actor == state.attribution.actor_id
        assert proof["attribution"]["actor_id"] == state.attribution.actor_id


def test_a_child_whose_parent_chain_does_not_verify_names_no_one_but_to_an_admin(
    nested_world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a parent binding that does not reproduce is no arm's run."""

    from cruxible_core.service.discovery import runs

    instance, root, children = nested_world
    original = runs._admission

    def without_parent(target: Any, run_id: str) -> object | None:
        return None if run_id == root else original(target, run_id)

    monkeypatch.setattr(runs, "_admission", without_parent)
    arming = OperationalViewer(credential_id="cred-arm", admin=False)
    for run_id in children:
        state = procedure_run_status(instance, run_id, viewer=arming)
        assert isinstance(state.attribution, ProcedureRunAttributionWithheld)
        shown = procedure_run_status(
            instance, run_id, viewer=OperationalViewer(credential_id=None, admin=True)
        )
        assert isinstance(shown.attribution, ProcedureRunAttribution)
