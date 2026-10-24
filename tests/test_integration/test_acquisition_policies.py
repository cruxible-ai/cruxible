"""Light source policies and exact frozen v1 reads."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.acquisition_policies import (
    AcceptedSourceAcquisitionPolicy,
    SourceAcquisitionPolicy,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
    acquisition_policy_path,
    acquisition_rule,
    evaluate_acquisition_policy_law,
    parse_acquisition_policy,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.captures import CanonicalDuration


def _policy(**kwargs: object) -> SourceAcquisitionPolicy:
    return SourceAcquisitionPolicy.model_validate(
        {
            "identity": ArtifactIdentity(kind="SourceAcquisitionPolicy", name="default"),
            **kwargs,
        }
    )


def test_default_and_override_change_only_one_alias_age() -> None:
    policy = _policy(max_age="P1D", overrides={"catalog": "PT1H", "archive": None})
    for alias, age in [("record", 86_400_000_000), ("catalog", 3_600_000_000), ("archive", None)]:
        rule = acquisition_rule(policy, alias)
        assert rule is not None
        assert rule.model_dump().keys() == {"input_name", "max_age"}
        assert (rule.max_age.microseconds if rule.max_age else None) == age
    wire = render_acquisition_policy(policy)
    assert parse_acquisition_policy(wire, path=acquisition_policy_path("default")) == policy
    assert set(json.loads(wire)) == {
        "artifact_format",
        "identity",
        "max_age",
        "overrides",
        "lifecycle",
    }


@pytest.mark.parametrize(
    "field",
    [
        "inputs",
        "coherence",
        "pins",
        "requirement",
        "permitted_replayability",
        "on_unavailable",
        "conservative_default",
    ],
)
def test_current_policy_rejects_removed_choices(field: str) -> None:
    with pytest.raises(ValidationError):
        _policy(**{field: None})


@pytest.mark.parametrize("size", [10, 100])
def test_resolving_one_alias_does_not_walk_overrides(size: int) -> None:
    class LookupOnly(dict[str, CanonicalDuration | None]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("an event must not scan the policy")

        def items(self):  # type: ignore[no-untyped-def]
            raise AssertionError("an event must not scan the policy")

    age = CanonicalDuration(microseconds=1)
    policy = _policy(max_age="P1D").model_copy(
        update={"overrides": LookupOnly({f"source{i}": age for i in range(size)})}
    )
    assert acquisition_rule(policy, "source0").max_age == age
    assert acquisition_rule(policy, "other").max_age.microseconds == 86_400_000_000


def test_frozen_v1_policy_bytes_digest_and_successor() -> None:
    golden = json.loads(
        (Path(__file__).parents[1] / "goldens/playbill/p2-b0-artifact-codec-v1.json").read_text()
    )

    # The codec corpus is authoritative historical bytes, never regenerated here.
    def find(value: object) -> dict:
        if isinstance(value, dict):
            if (
                isinstance(value.get("compact_wire"), str)
                and "playbill-source-acquisition-policy-v1" in value["compact_wire"]
            ):
                return value
            for child in value.values():
                found = find(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = find(child)
                if found:
                    return found
        return {}

    entry = find(golden)
    payload = json.loads(entry["compact_wire"])
    old = SourceAcquisitionPolicyV1.model_validate(payload)
    wire = render_acquisition_policy(old)
    parsed = parse_acquisition_policy(wire, path=acquisition_policy_path(old.identity.name))
    assert isinstance(parsed, SourceAcquisitionPolicyV1)
    assert parsed.model_dump(mode="json") == payload
    assert acquisition_policy_digest(parsed) == acquisition_policy_digest(old)
    successor = SourceAcquisitionPolicy(
        identity=old.identity,
        max_age=old.inputs[0].max_age,
        lifecycle=ArtifactLifecycle(predecessor_digest=acquisition_policy_digest(old).tagged),
    )
    assert (
        evaluate_acquisition_policy_law(
            successor,
            path=acquisition_policy_path(old.identity.name),
            predecessor=AcceptedSourceAcquisitionPolicy(
                path=acquisition_policy_path(old.identity.name),
                policy=old,
                artifact_digest=acquisition_policy_digest(old).tagged,
            ),
        ).verdict
        == "accepted"
    )
