"""A kit never carries an evidence rule naming one workspace file's contract (coldkit-16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    capture_contract_path,
    foreign_source_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.kits import KitBuildRequest
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_core.errors import RequestRefusedError
from cruxible_core.service.kits import KIT_PER_FILE_EVIDENCE_RULE, build_kit
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type as build_claim_type


def _tree_with(tmp_path: Path, rule: ClaimEvidenceAdmissionRule) -> dict[str, bytes]:
    root = tmp_path / "root"
    root.mkdir()
    instance, _ = initialize_local(root)
    tree = dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))
    page = foreign_source_capture_contract("doc-docs-incidents.md")
    tree[capture_contract_path(page.identity.name)] = render_capture_contract(page)
    claim_type = build_claim_type(
        predicate="acme.incident.severity",
        allowed_subject_kinds=("acme.incident",),
        value_type={"enum": ["sev1", "sev2"], "type": "string"},
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=(rule,)),
    )
    tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    return tree


def test_kit_build_refuses_a_rule_naming_a_per_file_contract(tmp_path: Path) -> None:
    page = foreign_source_capture_contract("doc-docs-incidents.md")
    tree = _tree_with(
        tmp_path,
        ClaimEvidenceAdmissionRule(
            rule_id="incident-page",
            claim_roles=("observation",),
            capture_contracts=(page.identity.name,),
            admission="direct",
            subject_binding="exact_claim_subject",
        ),
    )
    with pytest.raises(RequestRefusedError) as caught:
        build_kit(tree, KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)))
    assert caught.value.error_code == KIT_PER_FILE_EVIDENCE_RULE
    message = str(caught.value)
    assert "this contract stands for one workspace file" in message
    assert "evidence: [<contract name>, ...]" in message
    assert "cruxible.foreign-source." in message


def test_kit_build_carries_a_family_rule(tmp_path: Path) -> None:
    tree = _tree_with(
        tmp_path,
        ClaimEvidenceAdmissionRule(
            rule_id="workspace",
            claim_roles=("observation",),
            capture_contracts=(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name,),
            capture_contract_families=("workspace_file",),
            admission="direct",
            subject_binding="exact_claim_subject",
        ),
    )
    tree[capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name)] = (
        render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
    )
    bundle = build_kit(tree, KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)))
    assert any(item.path.startswith("claim-types/") for item in bundle.manifest.artifacts)
