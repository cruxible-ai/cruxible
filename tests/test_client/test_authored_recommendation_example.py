"""The example retains authored input and only stages explicitly revised fields."""

import base64
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client import ClaimRef, ClaimTypeRef, Playbill, SubjectRef
from cruxible_client.authoring.sdk import ClaimView
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from tests.test_client.test_playbill_sdk import _Client, _workspace

EXAMPLE = runpy.run_path(
    str(
        Path(__file__).resolve().parents[2]
        / "packages/cruxible-client/examples/authored_recommendation.py"
    )
)
Recommendation = EXAMPLE["Recommendation"]
stage = EXAMPLE["stage"]
render = EXAMPLE["render"]


def test_author_once_and_revise_only_changed_field(tmp_path):
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(client, instance_id="inst_test", workspace=tmp_path)
    subject = SubjectRef("program.recommendation/projection-refresh", pb.coordinate)
    fields = {
        name: ClaimTypeRef("program.recommendation." + name, pb.coordinate)
        for name in ("rule", "rationale")
    }
    record = Recommendation("Refresh with a receipt.", "Routine refresh should be cheap.")
    draft = stage(pb, subject, fields, record)._compiled()
    assert client.compiled is None  # No remote compile/submit; vocabulary reads may occur.
    assert len(draft.payload.members) == 2
    for member in draft.payload.members:
        authored = json.loads(base64.b64decode(member.source.content_base64))
        assert authored == {
            "kind": "authored-recommendation",
            "subject": subject.address,
            "rule": record.rule,
            "rationale": record.rationale,
        }
    updated = replace(record, rule="Refresh with a receipt; review when required.")
    prior_claim = ClaimRef("CLM-" + "a" * 32, pb.coordinate)
    revised = stage(
        pb, subject, fields, updated, previous=record, replacements={"rule": prior_claim}
    )._compiled()
    assert len(revised.payload.members) == 1
    member = revised.payload.members[0]
    assert member.revises == prior_claim.address
    assert member.statement.object.value == updated.rule
    assert member.existing_claim_dispositions[0].disposition == "contradict"
    for kwargs in ({"previous": record}, {"replacements": {"rule": prior_claim}}):
        with pytest.raises(ValueError, match="exact Claim replacements"):
            stage(pb, subject, fields, updated, **kwargs)
    with pytest.raises(ValueError, match="unchanged"):
        stage(pb, subject, fields, record, previous=record)


def rows():
    return [
        ClaimView(
            claim_id="CLM-" + digit * 32,
            revision=1,
            subject="subjects/program.recommendation/projection-refresh.json",
            predicate="program.recommendation." + name,
            qualifier=None,
            role="normative",
            object_kind="literal",
            value=value,
            lifecycle_state="live",
            verdict="supported",
            captures=(),
        )
        for digit, name, value in (
            ("a", "rule", "Refresh with a receipt."),
            ("b", "rationale", "Routine refresh should be cheap."),
        )
    ]


def test_render_follows_read_values_and_refuses_incomplete_or_competing_state():
    original = rows()
    fields = {name: "program.recommendation." + name for name in ("rule", "rationale")}

    def body(selected):
        return render(original[0].subject, fields, selected)

    assert "Refresh with a receipt." in body(original)
    assert "Updated rule." in body([replace(original[0], value="Updated rule."), original[1]])
    for invalid in (
        original[:1],
        [*original, original[0]],
        [replace(original[0], verdict="unresolved"), original[1]],
        [replace(original[0], lifecycle_state="retired"), original[1]],
    ):
        with pytest.raises(ValueError):
            body(invalid)


def test_recommendation_composes_with_subject_in_one_changeset(tmp_path):
    _workspace(tmp_path)
    pb = Playbill._from_client(_Client(), instance_id="inst_test", workspace=tmp_path)
    change = pb.changes(rationale="Bootstrap the recommendation in one proposal.")
    subject = change.subject(
        pb.subject(
            subject="program.recommendation/projection-refresh",
            pins=(),
            lifecycle=ArtifactLifecycle(),
        )
    )
    fields = {
        name: ClaimTypeRef("program.recommendation." + name, pb.coordinate)
        for name in ("rule", "rationale")
    }
    result = stage(change, subject, fields, Recommendation("Use receipts.", "Refresh cheaply."))
    assert result is change
    assert len(result._compiled().payload.members) == 3
