"""Accepting-record facts reuse only immutable complete inputs."""

from copy import deepcopy
from dataclasses import replace

from cruxible_client.contracts.content_memo import artifact_request
from cruxible_core.proposals.settlement import parse_change_set_record
from cruxible_core.query.explanation import _AcceptingCoordinate, _shared_explanation
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world


def test_carried_explanation_equals_cold_and_keys_every_coordinate_field(tmp_path):
    instance, _, _ = _accepted_claim_world(tmp_path)
    accepted = instance.accepted_coordinate()
    tree = instance.tree_at(accepted.git_oid)
    record_path, record = max(
        (
            (path, parse_change_set_record(body, path=path))
            for path, body in tree.items()
            if path.startswith("changesets/")
        ),
        key=lambda item: item[1].sequence,
    )
    coordinate = _AcceptingCoordinate(
        instance_id="carrier-test",
        git_object_format="sha256",
        git_oid=accepted.git_oid,
        semantic_root=accepted.semantic_root,
        generation_root=accepted.generation_root,
        compiler_digest=accepted.compiler_digest,
    )
    law = next(iter(record.law_digests))
    kwargs = dict(record_path=record_path, law_identifier=law, coordinate=coordinate)
    cold = _shared_explanation(deepcopy(record), **kwargs)
    with artifact_request():
        first = _shared_explanation(record, **kwargs)
        assert first == cold
        assert _shared_explanation(record, **kwargs) is first
        for field in (
            "instance_id",
            "git_object_format",
            "git_oid",
            "semantic_root",
            "generation_root",
            "compiler_digest",
        ):
            value = getattr(coordinate, field)
            replacement = (
                "sha256:" + ("e" if value != "sha256:" + "e" * 64 else "f") * 64
                if field in {"semantic_root", "generation_root", "compiler_digest"}
                else value + "-other"
            )
            other = replace(coordinate, **{field: replacement})
            assert _shared_explanation(record, **{**kwargs, "coordinate": other}) is not first
        changed = deepcopy(record)
        changed.law_digests[law] = "sha256:" + "f" * 64
        assert _shared_explanation(changed, **kwargs) != first
    assert _shared_explanation(record, **kwargs) is not first
