"""Per-terminal-item dependency closure derived from exact tokens."""

from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    AcquisitionInputOutcomeV1,
    DependencyEvidenceFactsV1,
    accepted_state_token,
    admitted_capture_token,
    build_terminal_item_manifest,
    derive_terminal_item_facts,
    policy_token,
    terminal_item_manifest_digest,
)


def _digest(byte: str) -> str:
    return f"sha256:{byte * 64}"


def test_manifest_and_facts_fold_only_the_item_dependency_closure() -> None:
    accepted = _digest("a")
    capture = _digest("b")
    policy = _digest("c")
    tokens = frozenset(
        {
            policy_token(policy),
            admitted_capture_token(capture),
            accepted_state_token(accepted),
        }
    )
    manifest = build_terminal_item_manifest(
        tokens,
        run_id="run-1",
        terminal_node_id="egress",
        item_key="00000000.item",
    )

    assert manifest.accepted_state_input_digests == (accepted,)
    assert manifest.admitted_capture_digests == (capture,)
    assert manifest.policy_and_law_digests == (policy,)
    assert terminal_item_manifest_digest(manifest).startswith("sha256:")

    derived = derive_terminal_item_facts(
        tokens,
        manifest=manifest,
        child_index=0,
        facts={
            accepted: DependencyEvidenceFactsV1(taint_labels=(TAINT_ACCEPTED_STATE,)),
            capture: DependencyEvidenceFactsV1(
                epistemic_grade="predicted",
                provenance_grade="self-asserted",
                selector_privacy="pseudonymous_required",
                acquisition_input_name="orders",
            ),
        },
        outcomes=(
            AcquisitionInputOutcomeV1(input_name="returns", disposition="omitted"),
            AcquisitionInputOutcomeV1(
                input_name="orders",
                disposition="acquired",
                capture_digests=(capture,),
            ),
        ),
    )

    assert derived.manifest_digest == terminal_item_manifest_digest(manifest)
    assert derived.epistemic_grade == "predicted"
    assert derived.provenance_grade == "self-asserted"
    assert derived.selector_privacy == "pseudonymous_required"
    assert derived.taint_labels == (TAINT_ACCEPTED_STATE,)
    assert tuple((row.input_name, row.disposition) for row in derived.source_coverage) == (
        ("orders", "consumed"),
        ("returns", "absent"),
    )


def test_unconsumed_evidence_cannot_taint_or_weaken_an_item() -> None:
    accepted = _digest("d")
    unconsumed = _digest("e")
    tokens = frozenset({accepted_state_token(accepted)})
    manifest = build_terminal_item_manifest(
        tokens,
        run_id="run-2",
        terminal_node_id="egress",
        item_key="00000000.item",
    )

    derived = derive_terminal_item_facts(
        tokens,
        manifest=manifest,
        child_index=0,
        facts={
            unconsumed: DependencyEvidenceFactsV1(
                epistemic_grade="predicted",
                provenance_grade="self-asserted",
                taint_labels=("caller-supplied",),
            )
        },
    )

    assert derived.epistemic_grade == "observed"
    assert derived.provenance_grade == "witnessed"
    assert derived.taint_labels == ()
