"""Core's local driver against a real provider runtime protocol 2.0 child.

The child here is ``cruxible_provider_runtime.child`` itself (from the checkout
``CRUXIBLE_PROVIDERS_CHECKOUT`` names, ``tests/support/provider_checkout.py``),
spawned by core's one spawner, dispatching to a test provider whose one
entrypoint serves two operations. Each outcome is then translated by core's own
map, the way an executor does: a declared code keeps its spelling, an
undeclared one is ``undeclared_refusal``, and a runtime code provider code
returns or raises reaches core as the child's ``provider_protocol_violation``.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.provider_execution import (
    ProviderSecretResolutionPlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_core.providers.provider_local_runtime import (
    BoundLocalProviderV1,
    LocalProviderExecutionDriver,
    ProviderSecretResolverRegistry,
)
from cruxible_core.providers.provider_outcomes import map_provider_envelope
from cruxible_core.providers.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgets,
    ProviderRuntimeRunContext,
)
from tests.support.invocation_ids import unique_invocation_id
from tests.support.short_temporary_root import short_temporary_directory

ENTRYPOINT = "pair_provider:Pair"

PROVIDER = '''
from cruxible_provider_runtime.errors import Refusal, RefusalCode, decline, refuse
from cruxible_provider_runtime.provider_api import ProviderResult


class Pair:
    """One entrypoint, two operations."""

    operations = ("demo.double", "demo.increment")

    def __call__(self, context):
        mode = context.input.get("mode")
        if mode == "declared":
            return ProviderResult.refused("too_long", "too long", limit=3)
        if mode == "raised_declared":
            raise decline("too_long", "too long", limit=3)
        if mode == "undeclared":
            return ProviderResult.refused("never_declared", "not this operation's")
        if mode == "returned_runtime":
            # Past the API's own check, as a hostile provider would build it.
            return ProviderResult(
                status="refused", refusal=Refusal(code="budget_wall_clock", message="invented")
            )
        if mode == "raised_runtime":
            raise refuse(RefusalCode.UNRESOLVED_SECRET_REF, "invented")
        n = context.input["n"]
        doubled = context.operation == "demo.double"
        return ProviderResult.ok(
            {"n": n * 2 if doubled else n + 1, "build": context.build_digest}
        )
'''


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _operation(name: str) -> ProviderOperation:
    pin = {"name": "demo.value", "version": 1, "digest": _digest("contract")}
    return ProviderOperation.model_validate(
        {
            "name": name,
            "in": pin,
            "out": pin,
            "effect": "pure",
            "refusals": {
                "too_long": {
                    "contract": {"name": "demo.too_long", "version": 1, "digest": _digest("r")}
                }
            },
            "entrypoint": ENTRYPOINT,
        }
    )


@pytest.fixture
def child(
    provider_runtime: Path, tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[Callable[..., Any]]:
    """Run one operation in a real runtime child; returns (envelope, outcome)."""

    del provider_runtime
    source = tmp_path / "provider"
    source.mkdir()
    (source / "pair_provider.py").write_text(PROVIDER, encoding="utf-8")
    # The spawner clears the environment; this interpreter puts the test
    # provider beside the runtime this test session has installed.
    interpreter = tmp_path / "python"
    interpreter.write_text(
        f'#!/bin/sh\nexec /usr/bin/env PYTHONPATH={source} {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    control = short_temporary_directory("real-child-control-")
    request.addfinalizer(lambda: shutil.rmtree(control, ignore_errors=True))
    leases = ProviderProcessLeaseStore(tmp_path / "leases", control_root=control)

    def run(operation: str, payload: dict[str, Any], /, **context_update: Any) -> Any:
        binding = BoundLocalProviderV1(
            binding=VerifiedProviderBinding(
                provider_artifact_digest=_digest("build"),
                operation=operation,
                operation_signature_digest=_digest("signature"),
                implementation_digest=_digest("implementation"),
                deployment_digest=_digest("deployment"),
                materialization_digest=_digest("materialization"),
                environment_manifest_digest=_digest("environment"),
                entrypoint=ENTRYPOINT,
            ),
            interpreter_path=interpreter,
        )
        context = ProviderRuntimeRunContext(
            protocol_version="2.0",
            run_id="RUN-real-child",
            operation=operation,
            operation_signature_digest=_digest("signature"),
            build_digest=_digest("build"),
            implementation_digest=_digest("implementation"),
            entrypoint=ENTRYPOINT,
            input=payload,
            budgets=ProviderRuntimeBudgets(wall_clock_seconds=30, output_bytes=65_536),
        ).model_copy(update=context_update)
        driven = LocalProviderExecutionDriver().invoke(
            binding,
            context,
            secret_plan=ProviderSecretResolutionPlan(),
            secret_resolvers=ProviderSecretResolverRegistry(()),
            invocation_id=unique_invocation_id(),
            process_leases=leases,
        )
        outcome = map_provider_envelope(
            driven.envelope,
            operation=_operation(operation),
            check_detail=lambda code, detail: None if detail == {"limit": 3} else ("bad",),
        )
        return driven.envelope, outcome

    try:
        yield run
    finally:
        leases.close()


def test_one_entrypoint_serves_each_operation_it_declares(child: Callable[..., Any]) -> None:
    doubled, outcome = child("demo.double", {"n": 2})
    assert doubled.status == "ok" and outcome.status == "ok"
    assert doubled.output == {"n": 4, "build": _digest("build")}
    incremented, _ = child("demo.increment", {"n": 2})
    assert incremented.output == {"n": 3, "build": _digest("build")}


def test_an_operation_the_entrypoint_does_not_declare_is_the_harness_refusal(
    child: Callable[..., Any],
) -> None:
    envelope, outcome = child("demo.triple", {"n": 2})
    assert envelope.refusal is not None and envelope.refusal.code == "undeclared_operation"
    assert (outcome.outcome_class, outcome.code) == ("internal", "undeclared_operation")


@pytest.mark.parametrize("mode", ["declared", "raised_declared"])
def test_a_declared_code_returned_or_raised_keeps_its_own_spelling(
    child: Callable[..., Any], mode: str
) -> None:
    envelope, outcome = child("demo.double", {"n": 2, "mode": mode})
    assert envelope.refusal is not None and envelope.refusal.code == "too_long"
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "node_refusal",
        "operation_declaration",
        "too_long",
    )
    assert outcome.detail == {"limit": 3}


def test_an_arbitrary_code_the_operation_does_not_declare_is_undeclared_refusal(
    child: Callable[..., Any],
) -> None:
    envelope, outcome = child("demo.double", {"n": 2, "mode": "undeclared"})
    # The child passes it through: the executor holds the operation.
    assert envelope.refusal is not None and envelope.refusal.code == "never_declared"
    assert (outcome.outcome_class, outcome.code) == ("internal", "undeclared_refusal")
    assert outcome.detail == {"operation": "demo.double", "refused_code": "never_declared"}


@pytest.mark.parametrize(
    ("mode", "asserted"),
    [("returned_runtime", "budget_wall_clock"), ("raised_runtime", "unresolved_secret_ref")],
)
def test_a_runtime_code_from_provider_code_is_never_believed(
    child: Callable[..., Any], mode: str, asserted: str
) -> None:
    envelope, outcome = child("demo.double", {"n": 2, "mode": mode})
    assert envelope.refusal is not None
    assert envelope.refusal.code == "provider_protocol_violation"
    assert asserted in envelope.refusal.message
    assert (outcome.outcome_class, outcome.attribution, outcome.code) == (
        "internal",
        "implementation",
        "provider_protocol_violation",
    )


@pytest.mark.parametrize(
    "update",
    [
        {"build_digest": _digest("another build")},
        {"operation_signature_digest": _digest("another signature")},
        {"operation": "demo.increment"},
    ],
)
def test_a_context_off_the_admitted_binding_never_reaches_the_child(
    child: Callable[..., Any], update: dict[str, Any]
) -> None:
    with pytest.raises(ProviderLocalRuntimeRefused) as refused:
        child("demo.double", {"n": 2}, **update)
    assert refused.value.code == "acceptance_divergence"
