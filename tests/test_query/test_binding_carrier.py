"""The result and its truncation witness carry the same immutable binding."""

from cruxible_client.contracts.content_memo import artifact_request
from cruxible_client.contracts.query.results import QueryRowBinding
from cruxible_core.query.engine import _binding_row
from tests.test_claims.test_claims import _subject


def test_binding_carrier_matches_fresh_validation_and_never_keys_a_mutable_backend():
    from types import SimpleNamespace

    shell = _subject()
    backend = SimpleNamespace(subject=lambda _: SimpleNamespace(shell=shell))
    path = "subjects/project.work_item/wi-42.json"
    with artifact_request():
        first = _binding_row(backend, "work", path)
        assert first == QueryRowBinding(
            binding="work",
            subject_identity=shell.identity.qualified,
            subject_kind=shell.subject_kind,
            subject_id=shell.subject_id,
            subject_path=path,
        )
        assert _binding_row(backend, "work", path) is first
        assert _binding_row(backend, "other", path) is not first
        shell = shell.model_copy(update={"subject_id": "changed"})
        changed = _binding_row(backend, "work", path)
        assert changed is not first
        assert changed.subject_id == "changed"
    assert _binding_row(backend, "work", path) is not changed
