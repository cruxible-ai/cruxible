"""The execution ports read exactly what the executor read before them."""

from __future__ import annotations

from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionRule,
    resolve_acquisition_rules,
)
from cruxible_client.contracts.provider_execution import (
    ProviderExternalOccurrencePlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_operations import ProviderOperation
from cruxible_client.contracts.workspace_file import workspace_file_signature_digest
from cruxible_core.procedures.ports import (
    ResolvedAcquisitionRules,
    bound_operation,
)

_D = "sha256:" + "1" * 64


def _operation(
    *, signature: tuple[str, str] = ("security.fetch-request@1", "security.fetch-result@1")
) -> ProviderOperation:
    return ProviderOperation.model_validate(
        {
            "name": "security.fetch",
            "in": signature[0],
            "out": signature[1],
            "effect": "external_read",
            "entrypoint": "pkg.mod:fn",
        }
    )


def test_resolved_rules_answer_as_the_defaults_and_overrides_did() -> None:
    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_client.contracts.procedures.acquisition_rules import AcquisitionDefault

    defaults = {"feed": AcquisitionDefault(max_age=CanonicalDuration(microseconds=1_000_000))}
    overrides: dict[str, AcquisitionDefault] = {}
    rules = resolve_acquisition_rules(("feed", "other"), defaults=defaults, overrides=overrides)
    resolved = ResolvedAcquisitionRules(rules=rules)
    assert isinstance(resolved.rule_for("node-a", "feed"), AcquisitionRule)
    assert resolved.rule_for("node-a", "feed") == rules["feed"]
    assert resolved.rule_for("node-a", "other") == rules["other"]
    assert resolved.digest


def _occurrence(
    *, kind: str, operation: ProviderOperation, signature_digest: str
) -> ProviderExternalOccurrencePlan:
    local = VerifiedProviderBinding.model_construct(entrypoint="pkg.mod:fn")
    return ProviderExternalOccurrencePlan.model_construct(
        occurrence_path="fetch",
        occurrence_kind=kind,
        node_id="fetch",
        slot="fetch",
        provider_artifact_digest=_D,
        operation=operation,
        operation_signature_digest=signature_digest,
        implementation_digest=_D,
        effect_class="external_read",
        capture_contracts={"capture": "sha256:" + "2" * 64} if kind == "source" else {},
        local_execution=local,
    )


def test_bound_operation_names_the_workspace_builtin() -> None:
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_client.contracts.workspace_file import workspace_file_contracts

    _source, _content, request, result = workspace_file_contracts()
    operation = ProviderOperation.model_validate(
        {
            "name": "workspace.file",
            "in": {
                "name": "workspace.file.request",
                "version": 1,
                "digest": contract_digest(request).tagged,
            },
            "out": {
                "name": "workspace.file.result",
                "version": 1,
                "digest": contract_digest(result).tagged,
            },
            "effect": "pure",
            "entrypoint": "pkg.mod:fn",
        }
    )
    bound = bound_operation(
        _occurrence(
            kind="source",
            operation=operation,
            signature_digest=workspace_file_signature_digest(),
        )
    )
    assert bound.builtin == "workspace.file"
    assert bound.out_digest == contract_digest(result).tagged


def test_bound_operation_reads_a_call_occurrence() -> None:
    operation = _operation()
    bound = bound_operation(_occurrence(kind="call", operation=operation, signature_digest=_D))
    assert bound.builtin is None
    assert (bound.node_path, bound.build_digest, bound.operation) == (
        "fetch",
        _D,
        "security.fetch",
    )
    assert (bound.effect, bound.entrypoint) == ("external_read", "pkg.mod:fn")


# NOTE: the old "acquisition_policy xor acquisition_rules" ProcedureExecutor
# constructor check is gone -- ProcedureExecutor.__init__ now takes only
# `acquisition_rules`, so the ambiguity it guarded against is structurally
# impossible. That test (test_executor_takes_rules_or_the_policy_not_both)
# had no surviving behavior to migrate and is deleted.
