"""The execution ports read exactly what the executor read before them."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.acquisition_policies import (
    SourceAcquisitionPolicy,
    acquisition_policy_digest,
    acquisition_rule,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.provider_execution import (
    ProviderExternalOccurrencePlan,
    VerifiedProviderBinding,
)
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGESTS
from cruxible_core.procedures.ports import (
    PolicyAcquisitionRules,
    bound_operation,
    policy_acquisition_rules,
)

_D = "sha256:" + "1" * 64


def _policy() -> SourceAcquisitionPolicy:
    return SourceAcquisitionPolicy(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="reads"),
        max_age=CanonicalDuration(microseconds=60_000_000),
        overrides={"feed": CanonicalDuration(microseconds=1_000_000)},
    )


def test_policy_rules_answer_as_the_policy_did() -> None:
    policy = _policy()
    rules = policy_acquisition_rules(policy)
    assert isinstance(rules, PolicyAcquisitionRules)
    assert rules.digest == acquisition_policy_digest(policy).tagged
    for alias in ("feed", "other"):
        assert rules.rule_for("node-a", alias) == acquisition_rule(policy, alias)
    assert policy_acquisition_rules(None) is None


def _occurrence(*, kind: str, interface_digest: str) -> ProviderExternalOccurrencePlan:
    local = VerifiedProviderBinding.model_construct(entrypoint="pkg.mod:fn")
    return ProviderExternalOccurrencePlan.model_construct(
        occurrence_path="fetch",
        occurrence_kind=kind,
        node_id="fetch",
        provider_artifact_digest=_D,
        interface_id="security.fetch",
        interface_digest=interface_digest,
        implementation_digest=_D,
        effect_class="external_read",
        capture_contract_digest="sha256:" + "2" * 64 if kind == "source" else None,
        contract_input_digest=None if kind == "source" else "sha256:" + "3" * 64,
        contract_output_digest=None if kind == "source" else "sha256:" + "4" * 64,
        local_execution=local,
    )


@pytest.mark.parametrize("workspace_digest", sorted(WORKSPACE_FILE_INTERFACE_DIGESTS))
def test_bound_operation_names_the_workspace_builtin(workspace_digest: str) -> None:
    operation = bound_operation(_occurrence(kind="source", interface_digest=workspace_digest))
    assert operation.builtin == "workspace.file"
    assert operation.out_digest == "sha256:" + "2" * 64


def test_bound_operation_reads_a_call_occurrence() -> None:
    operation = bound_operation(_occurrence(kind="call", interface_digest=_D))
    assert operation.builtin is None
    assert (operation.node_path, operation.build_digest, operation.operation) == (
        "fetch",
        _D,
        "security.fetch",
    )
    assert (operation.in_digest, operation.out_digest) == (
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
    )
    assert (operation.effect, operation.entrypoint) == ("external_read", "pkg.mod:fn")


def test_executor_takes_rules_or_the_policy_not_both() -> None:
    from cruxible_core.procedures.execution import ProcedureExecutor

    with pytest.raises(ValueError, match="not both"):
        ProcedureExecutor(
            journal=None,  # type: ignore[arg-type]
            bodies=None,  # type: ignore[arg-type]
            run_index=None,  # type: ignore[arg-type]
            fencing_token="t",
            activation_authority=None,  # type: ignore[arg-type]
            contract_validator=None,  # type: ignore[arg-type]
            acquisition_policy=_policy(),
            acquisition_rules=policy_acquisition_rules(_policy()),
        )
