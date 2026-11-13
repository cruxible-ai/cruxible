"""The execution ports read exactly what the executor read before them."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionRule,
    resolve_acquisition_rules,
)
from cruxible_client.contracts.provider_execution import (
    ProviderExternalOccurrencePlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.provider_registration import DeclaredOperation
from cruxible_core.procedures.ports import (
    ResolvedAcquisitionRules,
    bound_operation,
)
from tests.support.declared_operations import declared_operation

_D = "sha256:" + "1" * 64


def _operation(
    *, signature: tuple[str, str] = ("security.fetch-request@1", "security.fetch-result@1")
) -> DeclaredOperation:
    return declared_operation(
        {
            "name": "security.fetch",
            "in": signature[0],
            "out": signature[1],
            "effect": "external_read",
            "entrypoint": "pkg.mod:fn",
        }
    )


def test_resolved_rules_answer_as_the_line_and_the_core_default_did() -> None:
    from cruxible_client.contracts.captures import CanonicalDuration
    from cruxible_client.contracts.procedures.acquisition_rules import LineAcquisitionRule

    line_rules = {"feed": LineAcquisitionRule(max_age=CanonicalDuration(microseconds=1_000_000))}
    rules = resolve_acquisition_rules(("feed", "other"), line_rules=line_rules)
    assert rules["feed"].declared_by == "line"
    assert rules["other"].declared_by == "core_default"
    resolved = ResolvedAcquisitionRules(rules=rules)
    assert isinstance(resolved.rule_for("node-a", "feed"), AcquisitionRule)
    assert resolved.rule_for("node-a", "feed") == rules["feed"]
    assert resolved.rule_for("node-a", "other") == rules["other"]
    assert resolved.digest


def _occurrence(
    *, kind: str, operation: DeclaredOperation, signature_digest: str
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


@pytest.mark.parametrize("name", ["workspace.file", "workspace.files"])
def test_bound_operation_names_the_workspace_builtin(name: str) -> None:
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_client.contracts.workspace_file import (
        workspace_file_contracts,
        workspace_read_signature_digest,
    )

    handoff, file_result, files_result = workspace_file_contracts()
    result = file_result if name == "workspace.file" else files_result
    operation = declared_operation(
        {
            "name": name,
            "in": {
                "name": "workspace.file.handoff",
                "version": 1,
                "digest": contract_digest(handoff).tagged,  # type: ignore[arg-type]
            },
            "out": {
                "name": result.name,  # type: ignore[attr-defined]
                "version": 1,
                "digest": contract_digest(result).tagged,  # type: ignore[arg-type]
            },
            "effect": "pure",
            "entrypoint": "pkg.mod:fn",
        }
    )
    bound = bound_operation(
        _occurrence(
            kind="source",
            operation=operation,
            signature_digest=workspace_read_signature_digest(name),  # type: ignore[arg-type]
        )
    )
    # Each of the builtin's two reads is recognised by its own exact signature.
    assert bound.builtin == name
    assert bound.out_digest == contract_digest(result).tagged  # type: ignore[arg-type]


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
