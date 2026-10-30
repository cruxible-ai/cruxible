"""Provider operations, slotted Procedures and Lines over them, for B2 tests.

A world holds two B1 Contracts (``test.value@1`` and ``test.refusal@1``), a
provider build serving ``compute: test.value@1 -> test.value@1 (pure)``, a
Procedure whose one Call names slot ``compute`` with that signature, and a
Line binding it. Every artifact is authored and accepted through the ordinary
lowering and proposal path; providers run through a fake operator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.inputs import (
    LineInput,
    ProcedureInput,
    lower_authoring_input,
)
from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservation,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_operations import (
    ProviderOperation,
    operation_signature_digest,
)
from cruxible_client.contracts.providers import (
    AcceptedProvider,
    NamedProviderBuild,
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderLocalDistributionPin,
    ProviderLocalEnvBackendPin,
    ProviderRuntimeManifest,
    provider_path,
    render_provider,
)
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from cruxible_core.providers.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
)
from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeResultEnvelope

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
PROCEDURE = "test.slotted"
LINE = "test.slotted-line"

VALUE = Contract.model_validate(
    {"name": "test.value", "version": 1, "fields": {"value": {"type": "int"}}}
)
REFUSAL = Contract.model_validate(
    {"name": "test.refusal", "version": 1, "fields": {"reason": {"type": "string"}}}
)


def contract_files(*contracts: Contract) -> dict[str, bytes]:
    return {
        contract_path(item.name, item.version): render_contract(item)
        for item in (contracts or (VALUE, REFUSAL))
    }


def pinned(spelling: str) -> str:
    """``name@N`` with the exact digest of the fixture Contract it names."""

    for contract in (VALUE, REFUSAL):
        if spelling == f"{contract.name}@{contract.version}":
            return spelling
    raise KeyError(spelling)


def operation(
    name: str = "compute",
    *,
    effect: str = "pure",
    refusals: Mapping[str, bool] | None = None,
    entrypoint: str = "demo.runtime:Provider",
    signature: tuple[str, str] = ("test.value@1", "test.value@1"),
) -> ProviderOperation:
    """One unpinned operation (``signature`` = in, out); ``refusals`` maps code -> absorbable."""

    return ProviderOperation.model_validate(
        {
            "name": name,
            "in": signature[0],
            "out": signature[1],
            "effect": effect,
            "refusals": {
                code: {"contract": "test.refusal@1", "absorbable": absorbable}
                for code, absorbable in (refusals or {}).items()
            },
            "entrypoint": entrypoint,
        }
    )


def _digests(ref: Any) -> str | None:
    from cruxible_client.contracts.workspace_file import workspace_file_contracts

    for contract in (VALUE, REFUSAL, *workspace_file_contracts()):
        if (ref.name, ref.version) == (contract.name, contract.version):
            return contract_digest(contract).tagged
    return None


def operation_build(
    *,
    provider_id: str = "demo-provider",
    build: str = "a",
    operations: tuple[ProviderOperation, ...] | None = None,
) -> NamedProviderBuild:
    """One provider build declaring ``operations`` (pinned), as an install lowers it.

    ``build`` varies the wheel digest, so each value is a distinct build of the
    same provider name.
    """

    served = operations or (operation(),)
    entrypoints = sorted({item.entrypoint for item in served})
    manifest = ProviderRuntimeManifest(
        provider_id=provider_id,
        distribution=ProviderDistributionRef(name=provider_id, version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=tuple(
            ProviderImplementationManifest(
                entrypoint=entry,
                backends=("local_env",),
                deterministic=True,
            )
            for entry in entrypoints
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=provider_id,
            version="1.0.0",
            filename=f"{provider_id.replace('-', '_')}-1.0.0-py3-none-any.whl",
            sha256="sha256:" + (build * 64)[:64],
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain=provider_id,
        operations=document.pinned_operations(_digests),
    )


def build_files(build: NamedProviderBuild) -> dict[str, bytes]:
    return {provider_path(build.identity.name): render_provider(build)}


def slotted_spec(
    *,
    name: str = PROCEDURE,
    effect: str = "pure",
    description: str | None = None,
) -> ProcedureSpec:
    """One Call through slot ``compute`` (``test.value@1 -> test.value@1``)."""

    return ProcedureSpec.model_validate(
        {
            "procedure": name,
            **({} if description is None else {"description": description}),
            "input": {"value": "integer"},
            "output": {"value": "integer"},
            "slots": {"compute": {"in": "test.value@1", "out": "test.value@1", "effect": effect}},
            "steps": [{"call": "result", "slot": "compute", "input": {"value": "$input.value"}}],
            "returns": "result",
            "budget": {"provider_calls": 1},
        }
    )


@dataclass
class Invoker:
    """A fake provider process: answers with ``output`` or a typed refusal."""

    output: Callable[[Any], dict[str, Any]] = lambda context: {"value": context.input["value"] + 1}
    refusal: tuple[str, dict[str, Any]] | None = None
    contexts: list[Any] = field(default_factory=list)

    def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
        return BoundLocalProviderV1(
            binding=occurrence.local_execution,
            interpreter_path=Path("/portable/provider"),
        )

    def invoke_provider(  # type: ignore[no-untyped-def]
        self, *, occurrence, context, invocation_id, bound, deadline
    ) -> ProviderDriverOutcomeV1:
        del occurrence, invocation_id, deadline
        self.contexts.append(context)
        if self.refusal is not None:
            code, detail = self.refusal
            envelope = ProviderRuntimeResultEnvelope.model_validate(
                {
                    "protocol_version": "2.0",
                    "run_id": context.run_id,
                    "status": "refused",
                    "refusal": {"code": code, "message": "refused", "detail": detail},
                }
            )
        else:
            envelope = ProviderRuntimeResultEnvelope(
                protocol_version="2.0",
                run_id=context.run_id,
                status="ok",
                output=self.output(context),
            )
        return ProviderDriverOutcomeV1(
            envelope=envelope,
            stderr="",
            duration_seconds=0.001,
            egress=ProviderEgressObservation(observer_backend="test", observer_grade="attribution"),
            verified_binding=bound.binding,
        )


class Operator:
    """A ProviderRuntimeOperator whose deployments are already materialized."""

    def __init__(self, invoker: Invoker | None = None) -> None:
        self.invoker = invoker or Invoker()

    def invoker_for(self, instance, *, accepted_oid, pins=()):  # type: ignore[no-untyped-def]
        del instance, accepted_oid, pins
        return self.invoker

    def admit_line_provider(  # type: ignore[no-untyped-def]
        self,
        accepted_provider: AcceptedProvider,
        bound: ProviderOperation,
        implementation_digest: str,
        *,
        eligible_environment_pin_keys,
    ) -> VerifiedProviderBinding:
        del eligible_environment_pin_keys
        implementation = next(
            item
            for item in accepted_provider.provider.implementations
            if item.implementation_digest == implementation_digest
        )
        return VerifiedProviderBinding(
            provider_artifact_digest=accepted_provider.artifact_digest,
            operation=bound.name,
            operation_signature_digest=operation_signature_digest(bound.signature()),
            implementation_digest=implementation_digest,
            deployment_digest="sha256:" + "a" * 64,
            materialization_digest=implementation.materialization_references[
                0
            ].materialization_digest,
            environment_manifest_digest="sha256:" + "b" * 64,
            entrypoint=bound.entrypoint,
        )


def actor() -> Any:
    from cruxible_core.governance.actor_context import GovernedActorContext

    return GovernedActorContext(
        actor_type="human_user",
        actor_id="owner",
        org_id="test",
        operation_id="b2-operations",
        timestamp=NOW,
    )


def author(instance: Any, payload: Any, *, name: str) -> dict[str, bytes]:
    """Lower one authoring input at the head and accept every path it changes."""

    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._identity_support import accept_tree

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(payload),
        # A distinct instant per call: the same payload authored again is a new intent.
        canonical_timestamp="2026-10-29T11:00:00.000000Z",
    ).intent
    lowered = lower_authoring(instance, intent=intent, actor_id="owner")
    changed = dict(lowered.changed_members)
    if changed:
        accept_tree(instance, changed, name=name)
    return changed


def author_procedure(instance: Any, spec: ProcedureSpec, *, name: str = "procedure") -> bytes:
    from cruxible_client.contracts.procedures.artifacts import procedure_path

    changed = author(instance, ProcedureInput(kind="procedure", spec=spec), name=name)
    return changed[procedure_path(spec.procedure)]


def author_line(
    instance: Any,
    *,
    name: str = LINE,
    procedure: str = PROCEDURE,
    bindings: Mapping[str, str] | None = None,
    parameters: Mapping[str, object] | None = None,
    max_authority: str | None = None,
    acquisition: Mapping[str, Mapping[str, object]] | None = None,
    occurrence_epoch: int = 1,
    label: str = "line",
) -> dict[str, bytes]:
    return author(
        instance,
        LineInput.model_validate(
            {
                "kind": "line",
                "name": name,
                "procedure_name": procedure,
                "bindings": dict(bindings or {}),
                "acquisition": dict(acquisition or {}),
                "parameters": {"value": 1} if parameters is None else dict(parameters),
                "max_authority": max_authority,
                "occurrence_epoch": occurrence_epoch,
            }
        ),
        name=label,
    )


def world(tmp_path: Path, *builds: NamedProviderBuild) -> Any:
    """An instance holding the fixture Contracts and ``builds``."""

    from tests.core_support._identity_support import accept_tree
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    files = contract_files()
    for build in builds or (operation_build(),):
        files.update(build_files(build))
    accept_tree(instance, files, name="operations")
    return instance


__all__ = [
    "LINE",
    "NOW",
    "PROCEDURE",
    "REFUSAL",
    "VALUE",
    "Invoker",
    "Operator",
    "actor",
    "author",
    "author_line",
    "author_procedure",
    "build_files",
    "contract_files",
    "operation",
    "operation_build",
    "pinned",
    "slotted_spec",
    "world",
]
