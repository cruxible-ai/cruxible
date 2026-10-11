"""A one-Claim read never grows with the change set that accepted the Claim.

``read_whole`` makes a ``get(ref, detail="evidence", at=...)`` read. Its
provenance needs the accepting generation's actor binding, instant and the
one member's law evidence, never the whole change-set record. The counted work
is record parses and whole-record copies, weighted by the members they carry:
a Claim accepted beside many others must cost exactly what one accepted beside
a few costs, on a warm instance and on the Claim's first read.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.history import history_index
from cruxible_core.proposals import settlement
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
_SMALL = 2
_LARGE = 12


def _accept(instance: PlaybillInstance, prefix: str, count: int) -> dict[str, list[str]]:
    """Accept ``count`` long titles and exact-content rulings in one change set."""

    request = WriteRequest.model_validate(
        {
            "because": f"Seed the {prefix} batch.",
            "changes": [
                {
                    "op": "set",
                    "subject": f"{KIND}/{prefix}-{index}",
                    "field": field,
                    "value": f"{prefix} {index} " + "long text " * 80 + "\n",
                }
                for field in ("title", "ruling")
                for index in range(count)
            ],
        }
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    claims = [str(change.claim) for change in outcome.changes]
    return {"title": claims[:count], "ruling": claims[count:]}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("accepting-sets"))
    small = _accept(instance, "small", _SMALL)
    large = _accept(instance, "large", _LARGE)
    return {"instance": instance, "small": small, "large": large}


class _Work:
    """Members parsed from record bytes, and members copied with a whole record."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.counts: Counter[str] = Counter()
        parse = history_index.parse_change_set_record

        def counted_parse(*args: Any, **kwargs: Any) -> Any:
            record = parse(*args, **kwargs)
            self.counts["records parsed"] += 1
            self.counts["members parsed"] += len(record.members)
            return record

        monkeypatch.setattr(history_index, "parse_change_set_record", counted_parse)
        for model in (
            settlement.ChangeSetRecord,
            settlement.ChangeSetRecordV2,
            settlement.ChangeSetRecordV3,
        ):
            original = model.model_copy

            def counted_copy(self_: Any, *args: Any, _original: Any = original, **kwargs: Any):
                if kwargs.get("deep") or (len(args) > 1 and args[1]):
                    self.counts["whole records copied"] += 1
                    self.counts["members copied with a record"] += len(self_.members)
                return _original(self_, *args, **kwargs)

            monkeypatch.setattr(model, "model_copy", counted_copy)

    def take(self) -> dict[str, int]:
        found = dict(self.counts)
        self.counts.clear()
        return found


def _evidence(instance: PlaybillInstance, claim: str) -> object:
    result = service_playbill_get(
        instance,
        request=GetRequest(
            ref=claim,
            detail="evidence",
            at=instance.accepted_coordinate().git_oid,
            surface="sdk",
        ),
        access=_ACCESS,
    )
    assert result.evidence is not None
    return result.evidence.value


def _cold(instance: PlaybillInstance) -> None:
    """Forget every derived memo a Claim read could reuse, as a fresh process would."""

    instance.verified_change_set_records.clear()
    instance.verified_record_facts.clear()
    instance._owner_facts.clear()  # noqa: SLF001
    instance.claim_law_memo.clear()


def test_a_claims_first_read_costs_the_same_whatever_its_change_set_size(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = world["instance"]
    work = _Work(monkeypatch)
    by_size = {}
    for label, field in (
        ("small", "title"),
        ("large", "title"),
        ("small ruling", "ruling"),
        ("large ruling", "ruling"),
    ):
        size = label.split()[0]
        first, second = world[size][field][0], world[size][field][1]
        # The instance's first sight of the change set parses its record once.
        _cold(instance)
        assert str(_evidence(instance, first)).startswith(size)
        cold = work.take()
        # Another Claim the same change set accepted is that Claim's first read:
        # its facts are compiled, but the record is neither parsed nor copied.
        assert str(_evidence(instance, second)).startswith(size)
        first_read = work.take()
        # And a repeated read does no record work at all.
        _evidence(instance, second)
        warm = work.take()
        by_size[label] = (cold, first_read, warm)

    for cold, first_read, warm in by_size.values():
        assert cold.get("whole records copied", 0) == 0
        assert first_read == {} and warm == {}
    # Each record a read needs is parsed once; the count does not depend on its size.
    for small, large in (("small", "large"), ("small ruling", "large ruling")):
        assert by_size[small][0]["records parsed"] == by_size[large][0]["records parsed"]


def test_a_batched_claim_read_does_no_record_work_once_its_records_are_verified(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claims/read-batch`` (prefetch, ``read_whole_many``) over a large accepting set."""

    from cruxible_client.contracts.claim_reads import ClaimReadBatchRequest
    from cruxible_core.service.claims.claim_reads import service_read_claim_batch

    instance = world["instance"]
    claims = world["large"]["title"] + world["large"]["ruling"]
    request = ClaimReadBatchRequest(at=instance.accepted_coordinate().git_oid, claim_ids=claims)
    work = _Work(monkeypatch)
    _cold(instance)
    first = service_read_claim_batch(instance, request=request)
    cold = work.take()
    assert cold.get("whole records copied", 0) == 0
    assert service_read_claim_batch(instance, request=request) == first
    assert work.take() == {}


def test_history_listings_read_each_revisions_member_not_its_whole_record(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.claims.claims import service_playbill_claim_history

    instance = world["instance"]
    work = _Work(monkeypatch)
    claim = world["large"]["title"][0]
    expected = service_playbill_claim_history(instance, identity=claim)
    work.take()
    assert service_playbill_claim_history(instance, identity=claim) == expected
    assert work.take() == {}
    (entry,) = expected.entries
    assert entry.change_set_path.startswith("changesets/cs-")
    assert entry.changeset_digest.startswith("sha256:")
