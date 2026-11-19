"""A batched Claim read pinned by git oid names exactly one accepted generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.claim_reads import ClaimReadBatchRequest
from cruxible_client.contracts.errors import CruxibleError
from cruxible_core.service.claims.claim_reads import service_read_claim_batch
from tests.core_support._support import initialize_local

_SUBJECTS = ("subjects/project.work_item/wi-1.json",)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_a_git_oid_must_be_one_accepted_generations_whole_oid(
    tmp_path: Path, object_format: str
) -> None:
    instance, _owner = initialize_local(tmp_path, object_format=object_format)  # type: ignore[arg-type]
    head = instance.accepted_coordinate().git_oid
    assert len(head) == {"sha1": 40, "sha256": 64}[object_format]

    def read(at: str):  # type: ignore[no-untyped-def]
        return service_read_claim_batch(
            instance, request=ClaimReadBatchRequest(at=at, subject_paths=_SUBJECTS)
        )

    assert read(head).coordinate.git_oid == head
    unknown = ("0" if head[0] != "0" else "1") + head[1:]
    with pytest.raises(CruxibleError):
        read(unknown)
    if object_format == "sha256":
        # Forty characters are a whole SHA-1 oid but only a prefix here: refused.
        with pytest.raises(CruxibleError, match="not a prefix"):
            read(head[:40])
    else:
        with pytest.raises(CruxibleError, match="not a prefix"):
            read(head + "0" * 24)
