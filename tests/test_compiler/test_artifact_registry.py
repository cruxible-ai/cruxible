"""The current compiler's closed ledger path grammar and projection registry."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_core.compiler.compiler import ARTIFACT_KINDS, PROJECTION_REGISTRY
from cruxible_core.compiler.projection_artifacts import registered_path_kind


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("governance/approval-policy.json", "approval-policy"),
        ("governance/procedure-runtime-policy.json", "procedure-runtime-policy"),
        ("claim-types/project.work_item/status.json", "claim-type"),
        ("capture-contracts/erp-release.json", "capture-contract"),
        ("claims/12/CLM-12" + "ab" * 15 + ".json", "claim"),
        ("procedures/product-lot-release.json", "procedure"),
        ("lines/product-lot-release.json", "line"),
        ("query-definitions/project.active_work.json", "query-definition"),
        ("procedure-mandates/demo.json", "procedure-mandate"),
        ("providers/cruxible-builtin.json", "provider"),
        ("providers/demo/" + "ab" * 32 + ".json", "provider"),
        ("contracts/workspace.file.request/v1.json", "contract"),
        ("triggers/evidence-sweep.json", "trigger"),
        ("subject-kinds/project.work_item.json", "subject-kind"),
        ("compiler-upgrade.json", "compiler-upgrade"),
    ],
)
def test_the_current_grammar_resolves_each_kind(path: str, kind: str) -> None:
    assert registered_path_kind(path) == kind


@pytest.mark.parametrize(
    "path",
    [
        # Removed nouns (C12) have no path kind.
        "provider-interfaces/demo.interface.json",
        "blueprints/demo.json",
        "source-acquisition-policies/demo.json",
        # The compact codec's .yaml spelling is not a path of this compiler.
        "subjects/project.work_item/wi-1.yaml",
        "governance/approval-policy.yaml",
    ],
)
def test_the_current_grammar_refuses_retired_paths(path: str) -> None:
    with pytest.raises(ProjectionFormatError):
        ARTIFACT_KINDS.resolve_path(path)


def test_the_projection_registry_declares_no_removed_noun() -> None:
    """ProviderInterface and Blueprint are gone (C12): no kind and no fact schema remains."""

    kinds = {entry.kind for entry in ARTIFACT_KINDS.entries()}
    assert not kinds & {"provider-interface", "blueprint", "source-acquisition-policy"}
    for schema_id in (
        "cruxible.provider_interface.registration",
        "cruxible.provider_interface.vocabulary",
        "cruxible.provider_interface.classifier",
    ):
        assert not PROJECTION_REGISTRY.supports(schema_id, 1, classification="semantic")
    assert PROJECTION_REGISTRY.supports("cruxible.provider.runtime", 1, classification="semantic")


def test_calibration_readings_have_no_governed_path_kind() -> None:
    # Calibration readings are compute-produced, CAS-pinned artifacts. Registering a
    # governed tree path would collapse the ratified policy/readings/mandates split.
    assert "calibration-reading" not in {entry.kind for entry in ARTIFACT_KINDS.entries()}
