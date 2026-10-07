"""Guardrail: every kind ``get`` resolves, crossed with every detail it allows, answers.

Each (kind, detail) pair ``GET_DETAILS_BY_KIND`` admits must return a result
or a coded refusal (a ``CoreError``), never an uncaught exception. A run's
bare ``RUN-`` identity once reached ``_name`` under ``detail="proof"`` and
raised IndexError; this reads every pair over real worlds so a new kind or a
new detail cannot ship with the same hole.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.test_service.test_get_reads import world  # noqa: F401
from tests.test_service.test_operational_credentials import credential_world  # noqa: F401
from tests.test_service.test_operational_get import prediction_world  # noqa: F401
from tests.test_service.test_procedure_run_reads import run_world  # noqa: F401

from cruxible_client.contracts.get_reads import GET_DETAILS_BY_KIND, GetRequest
from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_core.errors import CoreError
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)
_TABLES = {
    "claim": ("claims", "Claim:"),
    "subject": ("subjects", "Subject:"),
    "claim_type": ("claim_types", ""),
    "document": ("documents", ""),
    "procedure": ("procedures", ""),
    "query": ("query_definitions", ""),
    "capture_contract": ("capture_contracts", ""),
    "trigger": ("triggers", ""),
    "line": ("lines", ""),
    "resolution_contract": ("resolution_contracts", ""),
    "mandate": ("procedure_mandates", ""),
    "provider_interface": ("provider_interfaces", ""),
    "source_acquisition_policy": ("source_acquisition_policies", ""),
}
_REF_FORMS = {
    "claim": lambda identity: identity.removeprefix("Claim:"),
    "subject": lambda identity: identity.removeprefix("Subject:"),
    "claim_type": lambda identity: identity,
    "document": lambda identity: "Document:" + identity.removeprefix("document:"),
    "procedure": lambda identity: identity,
    "query": lambda identity: "query:" + identity.removeprefix("QueryDefinition:"),
    "capture_contract": lambda identity: identity,
    "trigger": lambda identity: identity,
    "line": lambda identity: identity,
    "resolution_contract": lambda identity: identity,
    "mandate": lambda identity: "Mandate:" + identity.removeprefix("ProcedureMandate:"),
    "provider_interface": lambda identity: identity,
    "source_acquisition_policy": lambda identity: identity,
}


def _refs(instance: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        for kind, (table, _prefix) in _TABLES.items():
            row = connection.execute(f"SELECT identity FROM {table} ORDER BY identity").fetchone()
            if row is not None:
                found[kind] = _REF_FORMS[kind](str(row[0]))
        capture = connection.execute("SELECT capture_digest FROM captures").fetchone()
        if capture is not None:
            found["capture"] = f"Capture:{capture[0]}"
    principals = instance.accepted_history()[-1].principals.principals
    if principals:
        found["principal"] = f"Principal:{principals[0].principal_id}"
    found["approval_policy"] = "ApprovalPolicy:instance"
    found["procedure_runtime_policy"] = "ProcedureRuntimePolicy:instance"
    runs = service_playbill_orient(instance, section="runs").runs or ()
    if runs:
        found["procedure_run"] = f"ProcedureRun:{runs[0].run}"
    return found


@pytest.fixture(scope="module")
def refs(world, credential_world, prediction_world, run_world):  # type: ignore[no-untyped-def]  # noqa: F811
    by_kind: dict[str, tuple[Any, str]] = {}
    instances = [
        world["instance"],
        credential_world[0],
        prediction_world[0],
        run_world[0],
    ]
    for instance in instances:
        for kind, ref in _refs(instance).items():
            by_kind.setdefault(kind, (instance, ref))
    by_kind["proposal"] = (world["instance"], world["pending"])
    return by_kind


# Read over its own world below: accepting an interface needs a patched fixture.
_OWN_WORLD = frozenset({"provider_interface"})


def test_every_get_kind_has_a_reference_in_the_matrix(refs) -> None:  # type: ignore[no-untyped-def]
    assert set(refs) | _OWN_WORLD == set(GET_DETAILS_BY_KIND)


@pytest.mark.parametrize("historical", [False, True])
def test_every_provider_interface_detail_answers_or_refuses_coded(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch, historical
) -> None:
    from tests.test_service.test_playbill_orient import _accept_interfaces

    instance = _accept_interfaces(tmp_path, monkeypatch)
    history = instance.accepted_history()
    at = history[-2].oid if historical else None
    for detail in GET_DETAILS_BY_KIND["provider_interface"]:
        try:
            result = service_playbill_get(
                instance,
                request=GetRequest(ref="ProviderInterface:demo.interface", detail=detail, at=at),
                access=_ACCESS,
            )
        except (CoreError, ClientCoreError):
            continue
        assert (result.kind, result.detail) == ("provider_interface", detail)


@pytest.mark.parametrize(
    ("kind", "detail"),
    [(kind, detail) for kind, details in GET_DETAILS_BY_KIND.items() for detail in details],
)
@pytest.mark.parametrize("historical", [False, True])
def test_every_kind_and_detail_answers_or_refuses_coded(refs, kind, detail, historical) -> None:  # type: ignore[no-untyped-def]
    if kind in _OWN_WORLD:
        return
    instance, ref = refs[kind]
    history = instance.accepted_history()
    at = history[-2].oid if historical and len(history) > 1 else None
    fields: dict[str, Any] = {"ref": ref, "detail": detail, "at": at}
    if detail == "body":
        fields["range"] = {"start": 0, "end": 16}

    try:
        result = service_playbill_get(
            instance, request=GetRequest.model_validate(fields), access=_ACCESS
        )
    except (CoreError, ClientCoreError):
        return
    assert result.kind == kind
    assert result.detail == detail
