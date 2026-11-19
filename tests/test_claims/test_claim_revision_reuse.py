"""Revision reuse keeps the original comparison and evidence validation laws."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_verdicts import CaptureVerdictEvidence
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    _is_claim_type_rederivation,
    _request_verdict_capture,
    _statement_with_claim_type_digest,
)
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_core.authoring.lowering import _claim_pin
from tests.test_claims.test_claims import _claim


def _predecessor():
    return _claim(
        claim_id="CLM-" + "1" * 32,
        capture_digest="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        source_length=12,
    )


@pytest.mark.parametrize("successor", (False, True))
@pytest.mark.parametrize("changed", (None, "statement", "backing", "pin", "lifecycle"))
def test_rederivation_matches_the_original_copy_comparison(successor, changed):
    previous = _predecessor()
    digest = "sha256:" + "4" * 64 if successor else previous.statement.claim_type_digest
    statement = previous.statement.model_copy(update={"claim_type_digest": digest})
    pins = tuple(
        pin.model_copy(update={"artifact_digest": digest})
        if pin.role == "claim-type" and pin.target == previous.statement.claim_type
        else pin
        for pin in previous.pins
    )
    updates = {"statement": statement, "pins": pins}
    if changed == "statement":
        updates["statement"] = statement.model_copy(
            update={"object": LiteralClaimObject(value="done")}
        )
    elif changed == "backing":
        updates["backing"] = previous.backing.model_copy(update={"reducer_digest": digest})
    elif changed == "pin":
        updates["pins"] = (pins[0].model_copy(update={"artifact_digest": digest}), *pins[1:])
    elif changed == "lifecycle":
        updates["lifecycle"] = previous.lifecycle.model_copy(update={"state": "retired"})
    current = previous.model_copy(update=updates)
    expected = (
        current.artifact_format == previous.artifact_format
        and current.statement == statement
        and current.backing == previous.backing
        and current.pins == pins
        and current.lifecycle.state == previous.lifecycle.state
        and current.retirement == previous.retirement
    )
    assert (
        _is_claim_type_rederivation(
            current,
            predecessor=previous,
            claim_type_digest=digest,
            claim_type_identity=previous.statement.claim_type,
        )
        == expected
    )
    adjusted = _statement_with_claim_type_digest(previous.statement, digest)
    assert adjusted == statement
    assert (adjusted is previous.statement) == (not successor)


@pytest.mark.parametrize("changed", (None, "role", "target", "artifact_digest"))
def test_carried_pin_is_reused_only_when_every_field_matches(changed):
    previous = _predecessor().pins[-1]
    fields = {name: getattr(previous, name) for name in type(previous).model_fields}
    if changed == "role":
        fields[changed] = "other"
    elif changed == "target":
        fields[changed] = ArtifactIdentity(kind="Subject", name="project.work_item/other")
    elif changed == "artifact_digest":
        fields[changed] = "sha256:" + "4" * 64
    value = _claim_pin(**fields, previous=(previous,))
    assert value == type(previous)(**fields)
    assert (value is previous) == (changed is None)


VERDICT_FIELDS = {
    "capture_digest": "sha256:" + "5" * 64,
    "admission": "direct",
    "basis_kind": "replay_verified",
    "producer": ArtifactIdentity(kind="Provider", name="example"),
    "control_domain": "example",
    "upstream_provenance": (),
    "epistemic_grade": "observed",
    "provenance_grade": "daemon-fetched",
    "observed_at": datetime(2026, 8, 16, tzinfo=timezone.utc),
    "source_effective_until": None,
    "current_replay_available": True,
}
VERDICT_CHANGES = {
    "capture_digest": "sha256:" + "6" * 64,
    "admission": "origin_only",
    "basis_kind": "origin_only",
    "producer": ArtifactIdentity(kind="Provider", name="other"),
    "control_domain": "other",
    "upstream_provenance": (ArtifactIdentity(kind="Provider", name="upstream"),),
    "epistemic_grade": "derived",
    "provenance_grade": "self-asserted",
    "observed_at": VERDICT_FIELDS["observed_at"] + timedelta(days=1),
    "source_effective_until": VERDICT_FIELDS["observed_at"] + timedelta(days=2),
    "current_replay_available": False,
}


@pytest.mark.parametrize("field", tuple(VERDICT_CHANGES))
def test_shared_verdict_inputs_match_fresh_models_and_every_changed_field_misses(field):
    assert set(VERDICT_CHANGES) == set(CaptureVerdictEvidence.model_fields) - {"tag"}
    with artifact_request():
        first = _request_verdict_capture(**VERDICT_FIELDS)
        assert _request_verdict_capture(**VERDICT_FIELDS) is first
        fields = {**VERDICT_FIELDS, field: VERDICT_CHANGES[field]}
        changed = _request_verdict_capture(**fields)
        assert changed == CaptureVerdictEvidence(**fields)
        assert changed is not first
        with pytest.raises(ValidationError):
            _request_verdict_capture(**{**fields, "capture_digest": "invalid"})
    with artifact_request():
        assert _request_verdict_capture(**VERDICT_FIELDS) is not first


def test_unhashable_verdict_inputs_keep_constructor_validation_and_refusals():
    with artifact_request():
        fields = {
            **VERDICT_FIELDS,
            "upstream_provenance": [ArtifactIdentity(kind="Provider", name="upstream")],
        }
        assert _request_verdict_capture(**fields) == CaptureVerdictEvidence(**fields)
        fields["observed_at"] = datetime(2026, 8, 16)
        with pytest.raises(ValidationError, match="timezone-aware"):
            _request_verdict_capture(**fields)
