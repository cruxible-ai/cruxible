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

from cruxible_client.contracts.claims import claim_path
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
_SMALL = 10
_LARGE = 300
_RULINGS = 4


def _accept(instance: PlaybillInstance, prefix: str, count: int) -> dict[str, list[str]]:
    """Accept ``count`` long titles and a few exact-content rulings in one change set."""

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
                for field, number in (("title", count), ("ruling", _RULINGS))
                for index in range(number)
            ],
        }
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    claims = [str(change.claim) for change in outcome.changes]
    return {"title": claims[:count], "ruling": claims[count:], "size": [str(count + _RULINGS)]}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("accepting-sets"))
    small = _accept(instance, "small", _SMALL)
    large = _accept(instance, "large", _LARGE)
    return {"instance": instance, "small": small, "large": large}


class _Work:
    """Record work a read does: parses, index builds, whole-record copies, entry copies.

    Entry copies are every member and law evaluation copied out of a record and
    every other structure deep-copied out of one; together with the index the
    record carries, they are the member and law visits a read makes.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cruxible_client.contracts.candidates import (
            CandidateMemberEvidence,
            CandidateMemberLawEvidence,
            MemberLawEvaluation,
        )

        self.counts: Counter[str] = Counter()
        parse = history_index.parse_change_set_record

        def counted_parse(*args: Any, **kwargs: Any) -> Any:
            record = parse(*args, **kwargs)
            self.counts["records parsed"] += 1
            self.counts["members parsed"] += len(record.members)
            return record

        monkeypatch.setattr(history_index, "parse_change_set_record", counted_parse)
        index = history_index.VerifiedRecord.of.__func__  # type: ignore[attr-defined]

        def counted_index(cls: Any, record: Any) -> Any:
            self.counts["records indexed"] += 1
            return index(cls, record)

        monkeypatch.setattr(history_index.VerifiedRecord, "of", classmethod(counted_index))
        for name, model in (
            ("members copied", CandidateMemberEvidence),
            ("members copied", CandidateMemberLawEvidence),
            ("law evaluations copied", MemberLawEvaluation),
        ):
            original_entry = model.model_copy

            def counted_entry(
                self_: Any,
                *args: Any,
                _original: Any = original_entry,
                _name: str = name,
                **kw: Any,
            ) -> Any:
                self.counts[_name] += 1
                return _original(self_, *args, **kw)

            monkeypatch.setattr(model, "model_copy", counted_entry)
        deepcopy = history_index.copy.deepcopy

        def counted_deepcopy(value: Any, *args: Any) -> Any:
            self.counts["metadata deep-copied"] += 1
            return deepcopy(value, *args)

        monkeypatch.setattr(history_index.copy, "deepcopy", counted_deepcopy)
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
        for work in (first_read, warm):
            assert not {"records parsed", "records indexed", "whole records copied"} & set(work)
    # A record is parsed and indexed once (the unavoidable first verification);
    # every later read of a Claim it accepted does the same work at 14 or 304
    # members: its own member and law entries, through the index.
    for small, large in (("small", "large"), ("small ruling", "large ruling")):
        assert by_size[small][0]["records parsed"] == by_size[large][0]["records parsed"]
        assert by_size[small][1] == by_size[large][1]
        assert by_size[small][2] == by_size[large][2]


class _Visited(tuple):  # type: ignore[type-arg]
    """A resident record's entries, counting each one a read visits."""

    visits: Counter[str]
    label: str

    def __iter__(self):  # type: ignore[no-untyped-def]
        for item in super().__iter__():
            self.visits[self.label] += 1
            yield item

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        self.visits[self.label] += 1
        return super().__getitem__(key)


def _count_visits(instance: PlaybillInstance, visits: Counter[str]) -> None:
    """Make every resident verified record count its member and law entry visits."""

    for verified in instance.verified_change_set_records.values():
        for field, label in (("members", "member visits"), ("law_evidence", "law visits")):
            entries = _Visited(getattr(verified.record, field, ()))
            entries.visits, entries.label = visits, label
            object.__setattr__(verified.record, field, entries)


def test_a_fresh_claim_read_against_a_resident_record_visits_only_its_own_entries(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the record verified, a never-read Claim's compiled facts are bounded."""

    instance = world["instance"]
    work = _Work(monkeypatch)
    fresh: dict[str, dict[str, int]] = {}
    for size in ("small", "large"):
        _cold(instance)
        _evidence(instance, world[size]["title"][2])  # verifies and indexes the record
        work.take()
        # Forget everything derived for Claims, but keep the verified record.
        instance._owner_facts.clear()  # noqa: SLF001
        instance.claim_law_memo.clear()
        instance.verified_record_facts.clear()
        visits: Counter[str] = Counter()
        _count_visits(instance, visits)
        _evidence(instance, world[size]["title"][3])
        fresh[size] = {**work.take(), **visits}
        assert 0 < visits["law visits"] <= 2 and visits["member visits"] <= 4, visits
    assert fresh["small"] == fresh["large"]
    assert not {"records parsed", "records indexed", "whole records copied"} & set(fresh["large"])
    # The compiler's selection of one path carries that path's entries only.
    with instance.accepted_history_reader() as history:
        (location,) = history.member_history(claim_path(world["large"]["title"][3]))
        selection = history.read_member_selection(
            location, instance.retained_record_reader(), frozenset({location.member_path})
        )
    assert [member.path for member in selection.members] == [location.member_path]
    assert selection.candidate.scope == selection.closure_proof.paths == (location.member_path,)


def test_a_batched_claim_read_does_no_record_work_once_its_records_are_verified(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claims/read-batch`` (prefetch, ``read_whole_many``) over a large accepting set."""

    from cruxible_client.contracts.claim_reads import ClaimReadBatchRequest
    from cruxible_core.service.claims.claim_reads import service_read_claim_batch

    instance = world["instance"]
    claims = world["large"]["title"][:250] + world["large"]["ruling"]
    request = ClaimReadBatchRequest(at=instance.accepted_coordinate().git_oid, claim_ids=claims)
    work = _Work(monkeypatch)
    _cold(instance)
    first = service_read_claim_batch(instance, request=request)
    cold = work.take()
    assert cold.get("whole records copied", 0) == 0
    assert service_read_claim_batch(instance, request=request) == first
    assert not {"records parsed", "records indexed", "whole records copied"} & set(work.take())


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
    # One revision: its one member, copied out of the resident record.
    assert work.take() == {"members copied": 1}
    (entry,) = expected.entries
    assert entry.change_set_path.startswith("changesets/cs-")
    assert entry.changeset_digest.startswith("sha256:")
