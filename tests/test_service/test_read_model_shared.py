"""The three read verbs share one answer for what they all show.

``orient``, ``query`` and ``get`` each show a ClaimType's accepted evidence as
CaptureContract names, and each shows verdict problems as flags. Both come from
one shared derivation, so on the same state the verbs agree.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
)

_WHEN = datetime.fromisoformat(EVALUATION_TIME)
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture
def instance(tmp_path: Path) -> Any:
    seeded, _owner = seed_claims(tmp_path)
    return seeded


def test_every_verb_names_the_same_capture_contracts(instance: Any) -> None:
    query_row = service_playbill_query(
        instance,
        request=PlaybillQueryRequestV1.model_validate(
            {"kind": "ClaimType", "where": [{"field": "namespace", "eq": SUBJECT_KIND}]}
        ),
    ).rows[0]
    orient_kind = next(
        item for item in service_playbill_orient(instance).kinds if item.kind == SUBJECT_KIND
    )
    orient_evidence = orient_kind.evidence or next(
        item.evidence for item in orient_kind.predicates if item.predicate == PREDICATE
    )
    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=f"ClaimType:{PREDICATE}", evaluation_time=_WHEN),
        access=_ACCESS,
    ).card
    assert card is not None

    assert query_row["evidence"]
    assert tuple(query_row["evidence"]) == orient_evidence
    assert card.model_dump()["evidence"] == tuple(
        f"CaptureContract:{name}" for name in orient_evidence
    )
    assert not any(name.startswith("sha256:") for name in orient_evidence)
