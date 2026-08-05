"""Procedure blueprints: the portable, digest-addressed procedure-library format.

A blueprint is a declarative document -- metadata, contracts, dependencies,
slots, and procedures -- that a publisher can ship and an instance can install.
No code ever travels in a blueprint.

Phase 1 (wi-038) ships the *artifact*: the schema, canonicalization and digest,
and lowering into the config-overlay fragment and ``ProcedureDefinition`` list
an installer submits. There is no installer here, no trigger runtime, and no
binding registry; ``invocation: manual`` procedures are the executable slice.

    from cruxible_core.blueprint import load_blueprint, lower_blueprint

    loaded = load_blueprint("kev-triage.blueprint.yaml")
    lowered = lower_blueprint(
        loaded.blueprint,
        bindings={"exposure_assessment": "kev_exposure_scorer"},
        digest=loaded.digest,
    )
"""

from cruxible_core.blueprint.errors import (
    BlueprintBindingError,
    BlueprintDigestError,
    BlueprintError,
    BlueprintIssue,
    BlueprintSlotCandidate,
    BlueprintUnsupportedError,
    BlueprintValidationError,
)
from cruxible_core.blueprint.loader import (
    BlueprintAttachment,
    LoadedBlueprint,
    build_attachment_manifest,
    canonical_bytes,
    canonical_document,
    canonical_yaml,
    compute_blueprint_digest,
    load_blueprint,
    load_blueprint_text,
    parse_blueprint,
)
from cruxible_core.blueprint.lowering import (
    ConfigOverlayFragment,
    LoweredBlueprint,
    ProviderCandidate,
    ResolvedSlotBinding,
    lower_blueprint,
)
from cruxible_core.blueprint.schema import (
    BLUEPRINT_FORMAT_VERSION,
    Blueprint,
    BlueprintDependencies,
    BlueprintMetadata,
    BlueprintPipeline,
    BlueprintProcedure,
    BlueprintProvenance,
    ComputeSlot,
    OutcomeMetricHook,
    QuerySlot,
    TriggerSchema,
    cross_reference_issues,
)

__all__ = [
    "BLUEPRINT_FORMAT_VERSION",
    "Blueprint",
    "BlueprintAttachment",
    "BlueprintBindingError",
    "BlueprintDependencies",
    "BlueprintDigestError",
    "BlueprintError",
    "BlueprintIssue",
    "BlueprintMetadata",
    "BlueprintPipeline",
    "BlueprintProcedure",
    "BlueprintProvenance",
    "BlueprintSlotCandidate",
    "BlueprintUnsupportedError",
    "BlueprintValidationError",
    "ComputeSlot",
    "ConfigOverlayFragment",
    "LoadedBlueprint",
    "LoweredBlueprint",
    "OutcomeMetricHook",
    "ProviderCandidate",
    "QuerySlot",
    "ResolvedSlotBinding",
    "TriggerSchema",
    "build_attachment_manifest",
    "canonical_bytes",
    "canonical_document",
    "canonical_yaml",
    "compute_blueprint_digest",
    "cross_reference_issues",
    "load_blueprint",
    "load_blueprint_text",
    "lower_blueprint",
    "parse_blueprint",
]
