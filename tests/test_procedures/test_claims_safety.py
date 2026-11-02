"""Incomplete inputs never become authoritative negatives at either sink."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.claims import claim_artifact_digest
from cruxible_client.contracts.procedures.claims import ClaimTemplate
from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
from cruxible_core.procedures.claims_terminal import bind_claim_input
from cruxible_core.procedures.execution import run_value_digest
from cruxible_core.procedures.terminal_dependencies import AliasProvenanceV1, accepted_state_token
from tests.core_support._identity_support import accept_tree
from tests.test_procedures.test_claims_terminal import (
    KIND,
    PREDICATE,
    activate,
    candidate_claims,
    claim_type,
    claims_spec,
    claims_world,
    install,
    run,
)


def _incomplete_spec(*, inbox: bool = False, unknown: bool = True, value: str = "closed") -> Any:
    steps = claims_spec().model_dump(mode="json", by_alias=True)["steps"]
    steps[-1] = {
        "id": "write",
        "for_each": "$steps.entries.records",
        "subject": {"kind": KIND, "key": {"cve_id": "$item.key.cve_id"}},
        "claims": [{"type": PREDICATE, "value": value}],
    }
    # D3: a slot explicitly warns that the operation can truncate.
    if inbox:
        steps[-1] = {
            "id": "write",
            "inbox": "test.requires_complete_input@1",
            "for_each": "$steps.entries.records",
            "fields": {"status": value},
        }
    slots = {
        "fetch": {
            "in": "workspace.file.request@1",
            "out": "workspace.file.result@1",
            "effect": "pure",
            "may_truncate": True,
        }
    }
    if inbox:
        steps.insert(-1, {"project": "out", "fields": {}})
        return claims_spec(steps=steps, slots=slots, returns="out")
    return claims_spec(steps=steps, slots=slots)


@pytest.mark.parametrize("unknown", [True, False])
def test_incomplete_negative_is_downgraded_or_skipped_at_claims(
    tmp_path: Path, unknown: bool
) -> None:
    ct = claim_type(
        requires_complete_input=("closed",), incomplete_fallback="unknown" if unknown else None
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(ct,)
    )
    install(instance, _incomplete_spec(unknown=unknown))
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    egress = state.terminal_egress[0]
    assert egress.claims_diagnostics[0].kind == "incomplete_source"
    assert egress.claims_diagnostics[0].input == "entries"
    if unknown:
        (claim,) = candidate_claims(instance, state)
        assert claim.statement.object.value == "unknown"
    else:
        assert egress.proposal_id is None and not egress.children


@pytest.mark.parametrize("unknown", [True, False])
def test_incomplete_negative_is_downgraded_or_skipped_at_inbox(
    tmp_path: Path, unknown: bool
) -> None:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    contract = Contract.model_validate(
        {
            "name": "test.requires_complete_input",
            "version": 1,
            "fields": {
                "status": {
                    "type": "enum[open, closed, unknown]",
                    "requires_complete_input": ["closed"],
                    **({"incomplete_fallback": "unknown"} if unknown else {}),
                }
            },
        }
    )
    accept_tree(
        instance,
        {contract_path(contract.name, contract.version): render_contract(contract)},
        name="inbox-negative",
    )
    install(instance, _incomplete_spec(inbox=True), authority="observe")
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    from cruxible_core.exhaust.line_inbox import LineInboxStore

    store = LineInboxStore(instance)
    page, _ = store.page(line=None, status=None, limit=20, cursor=None)
    payloads = [store.get(item.envelope.item_id).payload for item in page]
    # With no unknown member, the negative item is not posted at all, and
    # its skipped write still names the incomplete input in the run report.
    assert state.terminal_egress[0].claims_diagnostics[0].kind == "incomplete_source"
    assert state.terminal_egress[0].claims_diagnostics[0].input == "entries"
    if unknown:
        assert {"status": "unknown"} in payloads
    else:
        assert {"status": "closed"} not in payloads


@pytest.fixture
def read_claim(tmp_path: Path) -> dict[str, Any]:
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}
    )
    install(instance, claims_spec())
    state = run(instance, root)
    (claim,) = candidate_claims(instance, state)
    from cruxible_client.contracts.claims import claim_path

    activate(instance, state)
    return {
        "claim_id": claim.identity.name,
        "path": claim_path(claim.identity.name),
        "artifact_digest": claim_artifact_digest(claim).tagged,
        "captures": list(claim.backing.capture_digests),
    }


def test_derived_input_binds_the_exact_version_read(read_claim: dict[str, Any]) -> None:
    token = accepted_state_token("sha256:" + "a" * 64)
    pin, captures = bind_claim_input(
        read_claim,
        outputs={"read": read_claim},
        provenance={"read": AliasProvenanceV1(whole=frozenset({token}))},
        read_aliases=frozenset({"read"}),
        own_tokens=frozenset({token}),
        read_digests={"read": run_value_digest("state-result", read_claim)},
    )
    assert pin.artifact_digest == read_claim["artifact_digest"]
    assert captures


def test_derived_input_refuses_an_input_that_is_not_the_exact_read(
    read_claim: dict[str, Any],
) -> None:
    supplied = {**read_claim, "path": "claims/forged.json"}
    token = accepted_state_token("sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="claims_inputs_invalid"):
        bind_claim_input(
            supplied,
            outputs={"read": read_claim},
            provenance={"read": AliasProvenanceV1(whole=frozenset({token}))},
            read_aliases=frozenset({"read"}),
            own_tokens=frozenset({token}),
            read_digests={"read": run_value_digest("state-result", read_claim)},
        )


def test_derived_input_refuses_a_forged_version_even_on_an_equal_read(
    read_claim: dict[str, Any],
) -> None:
    supplied = {**read_claim, "artifact_digest": "sha256:" + "f" * 64}
    token = accepted_state_token("sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="forged Claim version"):
        bind_claim_input(
            supplied,
            outputs={"read": supplied},
            provenance={"read": AliasProvenanceV1(whole=frozenset({token}))},
            read_aliases=frozenset({"read"}),
            own_tokens=frozenset({token}),
            read_digests={"read": run_value_digest("state-result", read_claim)},
        )


def test_derived_input_refuses_a_claim_read_in_another_rows_closure(
    read_claim: dict[str, Any],
) -> None:
    own = accepted_state_token("sha256:" + "a" * 64)
    sibling = accepted_state_token("sha256:" + "b" * 64)
    with pytest.raises(ValueError, match="row's closure"):
        bind_claim_input(
            read_claim,
            outputs={"sibling": read_claim},
            provenance={"sibling": AliasProvenanceV1(whole=frozenset({sibling}))},
            read_aliases=frozenset({"sibling"}),
            own_tokens=frozenset({own}),
            read_digests={"read": run_value_digest("state-result", read_claim)},
        )


def test_inputs_on_a_non_derivation_role_are_refused() -> None:
    with pytest.raises(ValidationError, match="inputs require role: derivation"):
        ClaimTemplate(
            type=ArtifactNameRef(target=ArtifactIdentity(kind="ClaimType", name=PREDICATE)),
            value="closed",
            role="observation",
            inputs=("$steps.read",),
        )


@pytest.mark.parametrize("inbox", [False, True])
@pytest.mark.parametrize("value", ["affected", "fixed", "not_affected"])
def test_incomplete_input_gates_only_declared_members(
    tmp_path: Path, inbox: bool, value: str
) -> None:
    vocabulary = "enum[affected, fixed, not_affected, unknown]"
    ct = claim_type(
        value=vocabulary,
        requires_complete_input=("fixed", "not_affected"),
        incomplete_fallback="unknown",
    )
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(ct,)
    )
    if inbox:
        contract = Contract.model_validate(
            {
                "name": "test.requires_complete_input",
                "version": 1,
                "fields": {
                    "status": {
                        "type": vocabulary,
                        "requires_complete_input": ["fixed", "not_affected"],
                        "incomplete_fallback": "unknown",
                    }
                },
            }
        )
        accept_tree(
            instance,
            {contract_path(contract.name, contract.version): render_contract(contract)},
            name="member-policy",
        )
    install(
        instance,
        _incomplete_spec(inbox=inbox, value=value),
        authority="observe" if inbox else "propose",
    )
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    expected = "affected" if value == "affected" else "unknown"
    if inbox:
        from cruxible_core.exhaust.line_inbox import LineInboxStore

        store = LineInboxStore(instance)
        page, _ = store.page(line=None, status=None, limit=20, cursor=None)
        assert {"status": expected} in [store.get(item.envelope.item_id).payload for item in page]
    else:
        assert candidate_claims(instance, state)[0].statement.object.value == expected


@pytest.mark.parametrize("type_name", ["date", "number", "int", "string"])
def test_completeness_policy_refuses_non_member_types(type_name: str) -> None:
    from cruxible_client.contracts.shapes import ContractField

    with pytest.raises(ValidationError, match="enum or bool member type"):
        ContractField.model_validate({"type": type_name, "requires_complete_input": ["fixed"]})


def test_boolean_completeness_policy_names_false() -> None:
    from cruxible_client.contracts.shapes import ContractField, requires_complete_input

    field = ContractField.model_validate({"type": "bool", "requires_complete_input": [False]})
    assert requires_complete_input(False, field.requires_complete_input)
    assert not requires_complete_input(True, field.requires_complete_input)
    with pytest.raises(ValidationError, match="only false"):
        ContractField.model_validate({"type": "bool", "requires_complete_input": [True]})
