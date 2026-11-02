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


def test_sealed_subject_metadata_is_shared_but_changed_subject_values_are_read_fresh():
    from types import SimpleNamespace

    from cruxible_client.contracts.content_memo import seal

    shell = _subject()
    assert seal(shell)
    calls = []

    def read(path):
        calls.append(path)
        return SimpleNamespace(shell=shell)

    backend = SimpleNamespace(subject=read)
    path = "subjects/project.work_item/wi-42.json"
    first = _binding_row(backend, "work", path)
    assert _binding_row(backend, "work", path) is first
    assert len(calls) == 2
    shell = shell.model_copy(update={"subject_id": "changed"})
    assert seal(shell)
    changed = _binding_row(backend, "work", path)
    assert changed is not first and changed.subject_id == "changed"


def test_cached_query_accounting_changes_with_every_counter_and_clipping_witness():
    from cruxible_client.contracts.query.results import QueryTruncation
    from cruxible_core.query.engine import _query_truncation

    original = dict(
        clipped_budgets=(),
        truncated_includes=(),
        candidate_result_count=1,
        returned_result_count=1,
        evaluated_path_count=1,
        retained_path_count=1,
    )
    first = _query_truncation(**original)
    assert first == QueryTruncation(**original)
    assert _query_truncation(**original) is first
    for field in ("candidate_result_count", "evaluated_path_count", "retained_path_count"):
        changed = {**original, field: 2}
        if field == "candidate_result_count":
            changed["clipped_budgets"] = ("max_results",)
        result = _query_truncation(**changed)
        assert result is not first and result == QueryTruncation(**changed)
    clipped = {
        **original,
        "clipped_budgets": ("include_max_items",),
        "truncated_includes": ("children",),
    }
    assert _query_truncation(**clipped) == QueryTruncation(**clipped)
