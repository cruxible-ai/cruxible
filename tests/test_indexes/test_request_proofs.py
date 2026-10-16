"""Pure carried edge and binding contracts equal direct validation."""

import pytest

from cruxible_client.contracts.candidates import DependencyProofReference
from cruxible_client.contracts.content_memo import artifact_request
from cruxible_core.claims import closure


def test_edge_carrier_matches_direct_validation_and_keys_every_input(monkeypatch):
    values = dict(
        source_path="claims/aa/one.json",
        source_artifact_digest="sha256:" + "a" * 64,
        target_path="claim-types/example.json",
        target_artifact_digest="sha256:" + "b" * 64,
        pin_role="claim-type",
    )
    cold = DependencyProofReference(**values)
    with artifact_request():
        first = closure.dependency_proof(**values)
        assert first == cold
        assert closure.dependency_proof(**values) is first
        for field, other in (
            ("source_path", "claims/bb/two.json"),
            ("source_artifact_digest", "sha256:" + "c" * 64),
            ("target_path", "claim-types/other.json"),
            ("target_artifact_digest", "sha256:" + "d" * 64),
            ("pin_role", "other"),
        ):
            changed = {**values, field: other}
            result = closure.dependency_proof(**changed)
            assert result == DependencyProofReference(**changed)
            assert result is not first
        calls = []

        def refused(**kwargs):
            calls.append(1)
            return DependencyProofReference(**kwargs)

        monkeypatch.setattr(closure, "DependencyProofReference", refused)
        for _ in range(2):
            with pytest.raises(ValueError):
                closure.dependency_proof(**{**values, "pin_role": "Invalid Role"})
        assert len(calls) == 2
    assert closure.dependency_proof(**values) is not first
