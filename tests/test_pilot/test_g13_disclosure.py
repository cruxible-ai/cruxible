"""C14 disclosure, adversarially: authorization before reads, complete responses, provenance.

Every served surface decides a Capture's access from maintained metadata (the
envelope index and the exact accepted kind version) before anything of it is
read for the caller; restricted, unknown and unresolvable kinds fail closed.
Responses are checked whole: a READ_ONLY reader's serialized answer carries no
span, locator, record key, lexeme or candidate value drawn from a restricted
body, and a key holder's carries them.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.capture_reads import CaptureReadRequest
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.runs import procedure_run_card, procedure_run_status
from cruxible_core.service.evidence.capture_reads import service_read_playbill_capture
from cruxible_core.storage.cas import ContentAddressedBodyStore
from tests.support.procedures import procedure_spec, refs
from tests.test_pilot.support import kev_spec, pilot_world
from tests.test_procedures.test_claims_terminal import activate, candidate_claims, run

READER = BodyAccessContext(principal_id="reader", can_read_body=True)
KEY = BodyAccessContext(principal_id="reviewer", can_read_body=True, can_read_restricted=True)
SECRET = "SECRET-LEXEME-7f3a"
#: Functions that may read a restricted Capture's bytes for a READ_ONLY reader:
#: accepted-state law evaluation (admission, verdict), whose output is a verdict.
LAW_FRAMES = frozenset(
    {
        "_claim_admission_accounts",
        "service_evaluate_playbill_claim_verdict",
        "_backfill",
    }
)


def _restricted(tmp_path: Path, fixture: str = "kev.json", spec: Any = None) -> Any:
    instance, root = pilot_world(tmp_path, fixture, capture_body_access="restricted", spec=spec)
    state = run(instance, root)
    (observation,) = [o for o in state.source_observations if o.capture_digest]
    return instance, root, state, observation.capture_digest


@contextmanager
def read_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, frozenset[str]]]]:
    """Every body-store read and head peek, with the functions on the stack."""

    from cruxible_core.storage import capture_metadata

    capture_metadata._ENVELOPES.clear()
    seen: list[tuple[str, frozenset[str]]] = []
    read, peek = ContentAddressedBodyStore.read, ContentAddressedBodyStore.peek

    def frames() -> frozenset[str]:
        frame, names = sys._getframe(2), set()
        while frame is not None:
            names.add(frame.f_code.co_name)
            frame = frame.f_back  # type: ignore[assignment]
        return frozenset(names)

    def spy_read(self: Any, digest: str, *, access: Any) -> bytes:
        seen.append((digest, frames()))
        return read(self, digest, access=access)

    def spy_peek(self: Any, digest: str, length: int) -> bytes:
        seen.append((digest, frames()))
        return peek(self, digest, length)

    with monkeypatch.context() as patch:
        patch.setattr(ContentAddressedBodyStore, "read", spy_read)
        patch.setattr(ContentAddressedBodyStore, "peek", spy_peek)
        yield seen


def _commitment(instance: Any, capture: str) -> str:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (value,) = projection.typed.connection.execute(
            "SELECT evidence_commitment_digest FROM captures WHERE capture_digest=?", (capture,)
        ).fetchone()
    return str(value)


def _assert_unread(seen: list[tuple[str, frozenset[str]]], *digests: str) -> None:
    leaked = [(d, sorted(f & LAW_FRAMES)) for d, f in seen if d in digests and not f & LAW_FRAMES]
    assert not leaked, leaked


def _get(instance: Any, ref: str, access: BodyAccessContext, detail: str = "summary") -> str:
    request = GetRequest(ref=ref) if detail == "summary" else GetRequest(ref=ref, detail=detail)
    return json.dumps(
        service_playbill_get(instance, request=request, access=access).model_dump(mode="json")
    )


def test_every_capture_surface_authorizes_before_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _root, state, digest = _restricted(tmp_path)
    claims = candidate_claims(instance, state)
    activate(instance, state)
    body = _commitment(instance, digest)
    claim = claims[0].identity.qualified
    handle = "CAP-" + digest.removeprefix("sha256:")[:16]
    with read_spy(monkeypatch) as seen:
        full = service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest=digest), access=READER
        )
        prefixed = service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest=handle), access=READER
        )
        card = _get(instance, "Capture:" + digest, READER)
        proof = _get(instance, handle, READER, "proof")
        evidence = _get(instance, claim, READER, "evidence")
        why = _get(instance, claim, READER, "why")
        claim_proof = _get(instance, claim, READER, "proof")
        from cruxible_core.service.authoring.documents import AcceptedCoordinate
        from cruxible_core.service.discovery.coverage import build_accepted_evidence_index_v2

        coverage = build_accepted_evidence_index_v2(
            instance,
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            access=READER,
        )
    _assert_unread(seen, digest, body)
    assert full.status == prefixed.status == "restricted"
    assert '"withheld"' in card and '"access": "restricted"' in card
    assert '"envelope": null' in proof and "body_restricted" in proof
    for text in (evidence, why, claim_proof):
        assert "record_locator" not in text and "field_path" not in text
        assert "/vulnerabilities/" not in text and body not in text
    assert "body_restricted" in why and "body_restricted" in claim_proof
    (row,) = [c for c in coverage.citations if digest in c.capture_digests]
    assert row.access_class == "restricted"
    # The key holder reads all of it.
    opened = _get(instance, claim, KEY, "proof")
    assert "record_locator" in opened and "/vulnerabilities/" in opened
    assert _get(instance, claim, KEY, "why").count(body) >= 1


def test_an_unresolvable_or_unindexed_capture_fails_closed(tmp_path: Path) -> None:
    from cruxible_core.service.evidence.body_access import capture_access_resolver
    from cruxible_core.service.procedures.line_inbox import _withheld

    instance, _root = pilot_world(tmp_path, "kev.json")
    unknown_kind = "sha256:" + "e" * 64
    stored = instance.body_store().store(
        b'{"capture_contract_digest":"' + unknown_kind.encode() + b'","x":1}'
    )
    resolver = capture_access_resolver(instance)
    assert resolver.capture(stored.digest).restricted
    assert resolver.capture("sha256:" + "0" * 64).restricted  # absent: no indexed kind
    read = service_read_playbill_capture(
        instance, request=CaptureReadRequest(capture_digest=stored.digest), access=READER
    )
    assert read.status == "restricted" and read.envelope is None
    assert read.notice is not None and read.notice.capture_kinds == ()
    # Derived payloads standing on a missing Capture stay withheld.
    assert resolver.withheld(["sha256:" + "0" * 64], READER)[1] is not None
    assert _withheld(instance, (), READER) == {}


def test_a_restricted_header_lexeme_never_reaches_a_read_only_run_reader(
    tmp_path: Path,
) -> None:
    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["steps"][2]["header"] = {
        "catalog_version": {"at": "/catalogVersion", "parse": "date", "presence": "required"}
    }
    instance, root = pilot_world(
        tmp_path, "kev.json", capture_body_access="restricted", spec=procedure_spec(base=base)
    )
    data = json.loads((root / "data" / "kev.json").read_bytes())
    data["catalogVersion"] = SECRET
    (root / "data" / "kev.json").write_text(json.dumps(data))
    state = run(instance, root)
    assert state.status == "node_refused", state.status
    assert SECRET in json.dumps(state.model_dump(mode="json"))
    assert state.run_id is not None
    ref = "ProcedureRun:" + state.run_id
    for access, visible in ((READER, False), (KEY, True)):
        status = procedure_run_status(instance, state.run_id, viewer=None, access=access)
        texts = [
            json.dumps(status.model_dump(mode="json")),
            _get(instance, ref, access),
            _get(instance, ref, access, "proof"),
        ]
        for text in texts:
            assert (SECRET in text) is visible, text[:400]
        if not visible:
            assert status.terminal is not None and status.terminal.code == state.terminal.code
            assert status.withheld is not None


def test_a_running_run_withholds_summaries_after_a_guard_over_restricted_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    from cruxible_core.service.discovery import runs

    base = kev_spec().model_dump(mode="python", by_alias=True)
    guard = refs(
        {
            "guard": {"op": "gt", "left": "$steps.entries.count", "right": {"literal": 0}},
            "else": {"halt": "nothing_listed"},
        }
    )
    base["steps"] = [*base["steps"][:-1], guard, base["steps"][-1]]
    instance, _root, state, digest = _restricted(tmp_path, spec=procedure_spec(base=base))
    assert state.run_id is not None
    journal = runs.procedure_run_journal(instance)
    assert journal is not None

    class Paused:
        """The run's journal as it stood before its final record: it is running."""

        def __init__(self, index: Any) -> None:
            self._index = index

        def run_locators(self, *args: Any, **kwargs: Any) -> Any:
            found, *rest = self._index.run_locators(*args, **kwargs)
            hidden = tuple(dataclasses.replace(item, final_payload_digest=None) for item in found)
            return (hidden, *rest)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._index, name)

    paused = type("PausedJournal", (), {})()
    paused.index = Paused(journal.index)  # type: ignore[attr-defined]
    monkeypatch.setattr(runs, "procedure_run_journal", lambda _instance: paused)
    at = datetime(2026, 11, 2, tzinfo=UTC)
    reader = procedure_run_card(
        instance, state.run_id, evaluation_time=at, render=lambda t, d: t, access=READER
    )
    assert reader.status == "running"
    assert reader.withheld is not None and reader.withheld.capture_digests == (digest,)
    assert all(node.summary is None for node in reader.nodes)
    holder = procedure_run_card(
        instance, state.run_id, evaluation_time=at, render=lambda t, d: t, access=KEY
    )
    assert any(node.kind == "guard" and node.summary is not None for node in holder.nodes)


def test_trigger_fed_runs_inherit_their_admitted_captures_restriction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta
    from functools import partial

    from cruxible_client.contracts.line_inbox import InboxItemEnvelope, InboxSighting
    from cruxible_core.service.procedures.line_inbox import _withheld
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        TriggerFire,
        service_run_playbill_line,
    )
    from tests.core_support._light_capture_support import record_contract
    from tests.test_procedures import test_procedure_source_runs as fixtures
    from tests.test_procedures import test_trigger_fed_proposals as fed

    sealed = partial(record_contract, body_access="restricted")
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, "capture_contract", sealed)
        patch.setattr(fed, "capture_contract", sealed)
        instance, _owner, root, line = fed._trigger_fed_world(tmp_path, pytest.MonkeyPatch())
        capture_digest, event = fed._event(instance, root)
        state = service_run_playbill_line(
            instance,
            path_identity_digest=line.identity.name,
            request=LineRunRequest(line=line.identity.name),
            actor_context=fixtures._actor(instance),
            caller_rung=2,
            daemon_clock=fixtures._TestClock(fixtures.NOW + timedelta(seconds=2)),
            trigger_fire=TriggerFire(trigger=fed.TRIGGER, event=event),
        )
    assert state.status == "succeeded", state.terminal
    assert state.source_observations == ()  # the trigger-bound Capture is its only input
    assert state.run_id is not None
    ref = "ProcedureRun:" + state.run_id
    status = procedure_run_status(instance, state.run_id, viewer=None, access=READER)
    assert status.withheld is not None and status.withheld.capture_digests == (capture_digest,)
    assert "body_restricted" in _get(instance, ref, READER)
    assert "body_restricted" in _get(instance, ref, READER, "proof")
    assert procedure_run_status(instance, state.run_id, viewer=None, access=KEY).withheld is None
    # An inbox item a trigger-fed run posts inherits the same provenance.
    sighting = InboxSighting(run_id=state.run_id, posted_at=fixtures.NOW)
    envelope = InboxItemEnvelope(
        item_id="INB-" + "a" * 64,
        status="unread",
        line=line.identity.qualified,
        procedure="Procedure:p",
        procedure_artifact_digest="sha256:" + "1" * 64,
        node_id="post",
        occurrences=1,
        first_sighting=sighting,
        recent_sightings=(sighting,),
        dedupe_key="sha256:" + "2" * 64,
        payload_digest="sha256:" + "3" * 64,
        payload_bytes=2,
    )
    assert envelope.item_id in _withheld(instance, (envelope,), READER)
    assert _withheld(instance, (envelope,), KEY) == {}


@pytest.mark.parametrize("size", [4, 40])
def test_authorization_work_is_constant_in_the_captures_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        capture_contract_path,
        render_capture_contract,
    )
    from cruxible_core.service.evidence import body_access
    from cruxible_core.storage.capture_envelope_index import CaptureEnvelopeIndex
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import record_contract

    instance, _root = pilot_world(tmp_path, "kev.json")
    sealed = record_contract(name="test.sealed-v1", body_access="restricted")
    accept_tree(
        instance,
        {capture_contract_path("test.sealed-v1"): render_capture_contract(sealed)},
        name="sealed-kind",
    )
    kinds = [
        capture_contract_digest(record_contract()).tagged,
        capture_contract_digest(sealed).tagged,
    ]
    digests = [
        instance.body_store()
        .store(b'{"capture_contract_digest":"' + kinds[n % 2].encode() + b'","n":%d}' % n)
        .digest
        for n in range(size)
    ]
    calls = {"index": 0, "kinds": 0, "history": 0}
    contracts_of, select_in = CaptureEnvelopeIndex.contracts_of, body_access.select_in

    def count_index(self: Any, wanted: Any) -> Any:
        calls["index"] += 1
        return contracts_of(self, wanted)

    def count_kinds(*args: Any, **kwargs: Any) -> Any:
        calls["kinds"] += 1
        return select_in(*args, **kwargs)

    # The index's one-time cold migration (head reads) happens before the read.
    body_access.capture_access_resolver(instance).captures(digests[:1])
    monkeypatch.setattr(CaptureEnvelopeIndex, "contracts_of", count_index)
    monkeypatch.setattr(body_access, "select_in", count_kinds)
    with read_spy(monkeypatch) as seen:
        found = body_access.capture_access_resolver(instance).captures(digests)
    assert not seen  # no envelope, body or head read
    assert calls == {"index": 1, "kinds": 1, "history": 0}
    assert sum(item.restricted for item in found.values()) == size // 2


def _candidate_dates(instance: Any, state: Any) -> set[str]:
    values = {str(c.statement.object.value) for c in candidate_claims(instance, state)}
    return {value for value in values if value.startswith("20")}


def test_proposal_review_and_approval_withhold_restricted_candidate_values(
    tmp_path: Path,
) -> None:
    from cruxible_core.service.proposals.review import (
        service_review_playbill_proposal,
        withhold_review_values,
    )

    instance, _root, state, digest = _restricted(tmp_path)
    dates = _candidate_dates(instance, state)
    assert dates
    proposal = state.terminal_egress[0].proposal_id
    for access, visible in ((READER, False), (KEY, True)):
        summary = service_review_playbill_proposal(instance, proposal_id=proposal, access=access)
        text = json.dumps(summary.model_dump(mode="json"))
        assert any(date in text for date in dates) is visible
        assert (summary.withheld is None) is visible  # type: ignore[union-attr]
        detail = service_review_playbill_proposal(
            instance, proposal_id=proposal, access=access, detail=True
        )
        payload = detail.model_dump(mode="json")
        served = withhold_review_values(instance, payload, access)
        text = json.dumps(served)
        assert any(date in text for date in dates) is visible, text[:400]
        assert ("record_locator" in text) is visible
        # The commitment a signer signs is unchanged.
        assert served["candidate_digest"] == payload["candidate_digest"]
        assert served["parent_semantic_root"] == payload["parent_semantic_root"]
        if not visible:
            assert served["withheld"]["capture_digests"] == [digest]
    from cruxible_client import contracts

    challenge_review = contracts.ProposalReview.model_validate(
        withhold_review_values(
            instance,
            service_review_playbill_proposal(
                instance, proposal_id=proposal, access=READER, detail=True
            ).model_dump(mode="json"),
            READER,
        )
    )
    assert challenge_review.withheld is not None


def test_a_capture_first_cited_after_its_kind_succeeds_keeps_its_versions_access(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )
    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        capture_contract_path,
        parse_capture_contract,
        render_capture_contract,
    )
    from cruxible_core.service.authoring.documents import AcceptedCoordinate
    from cruxible_core.service.discovery.coverage import build_accepted_evidence_index_v2
    from tests.core_support._identity_support import accept_tree

    instance, _root, state, digest = _restricted(tmp_path)
    path = capture_contract_path("test.orders-v1")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    current = parse_capture_contract(tree[path], path=path)
    successor = lower_capture_contract_input(
        CaptureContractInputRecord(name="test.orders-v1", max_bytes=8192),
        predecessor=current,
        named=True,
    )
    accept_tree(instance, {path: render_capture_contract(successor)}, name="kind-successor")
    assert capture_contract_digest(successor) != capture_contract_digest(current)
    activate(instance, state)  # the first citation of a Capture under the superseded version
    at = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    for access in (READER, KEY):
        card = json.loads(_get(instance, "Capture:" + digest, access))["card"]
        assert card["access"] == "restricted"
        assert ("withheld" in card) is (access is READER)
        index = build_accepted_evidence_index_v2(instance, at=at, access=access)
        (row,) = [c for c in index.citations if digest in c.capture_digests]
        assert row.access_class == "restricted"
    read = service_read_playbill_capture(
        instance, request=CaptureReadRequest(capture_digest=digest), access=READER
    )
    assert read.status == "restricted"


def test_restricted_kinds_compile_under_the_body_access_coordinate(tmp_path: Path) -> None:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )
    from cruxible_client.contracts.captures import (
        capture_body_access,
        capture_contract_path,
        parse_capture_contract,
        render_capture_contract,
    )
    from cruxible_core.compiler.compiler import CURRENT_COMPILER, CURRENT_COMPILER_REVISION
    from tests.core_support._identity_support import accept_tree

    instance, _root = pilot_world(tmp_path, "kev.json", capture_body_access="restricted")
    coordinate = instance.accepted_coordinate()
    assert CURRENT_COMPILER_REVISION == "derive-template-v1"
    from cruxible_core.service.authoring.documents import AcceptedCoordinate

    assert AcceptedCoordinate.from_internal(coordinate).compiler_digest == (
        CURRENT_COMPILER.rule_digest
    )
    path = capture_contract_path("test.orders-v1")
    accepted = parse_capture_contract(instance.tree_at(coordinate.git_oid)[path], path=path)
    assert capture_body_access(accepted) == "restricted"
    # A successor that opens the kind is refused at acceptance, live or retired.
    for lifecycle in ("live", "retired"):
        opened = lower_capture_contract_input(
            CaptureContractInputRecord(name="test.orders-v1", max_bytes=8192, lifecycle=lifecycle),
            predecessor=accepted,
            named=True,
        ).model_copy(update={"body_access": "open"})
        with pytest.raises(AssertionError):
            accept_tree(instance, {path: render_capture_contract(opened)}, name=f"open-{lifecycle}")
    assert instance.accepted_coordinate() == coordinate


# --- second review round (R1-R6) ------------------------------------------------------


@contextmanager
def served(instance: Any, monkeypatch: pytest.MonkeyPatch, mode: str) -> Iterator[Any]:
    """The real served facade over this instance, at one permission tier."""

    from types import SimpleNamespace

    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope

    with monkeypatch.context() as patch:
        patch.setattr(
            playbill_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _id: instance)
        )
        with request_permission_scope(PermissionMode[mode]):
            yield playbill_api


def test_r1_the_public_batch_read_filters_restricted_evidence_for_both_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.claim_reads import ClaimReadBatchRequest

    instance, _root, state, digest = _restricted(tmp_path)
    claims = candidate_claims(instance, state)
    activate(instance, state)
    ids = tuple(c.identity.qualified for c in claims[:3])
    for mode, visible in (("READ_ONLY", False), ("GOVERNED_WRITE", True)):
        with served(instance, monkeypatch, mode) as api:
            result = api.playbill_read_claim_batch(
                instance.descriptor.instance_id, request=ClaimReadBatchRequest(claim_ids=ids)
            )
        text = json.dumps(result.model_dump(mode="json"))
        assert len(result.claims) == len(ids)
        assert ("record_locator" in text) is visible and ("/vulnerabilities/" in text) is visible
        assert (result.withheld is None) is visible
        if not visible:
            assert result.withheld is not None and result.withheld.capture_digests == (digest,)
        # Accepted values stay for both readers.
        assert all(
            '"proposition"' in json.dumps(view.model_dump(mode="json")) for view in result.claims
        )


def test_r2_absence_witnesses_lose_their_selector_and_counts_in_every_copy(
    tmp_path: Path,
) -> None:
    from cruxible_core.service.proposals.review import (
        service_review_playbill_proposal,
        withhold_review_values,
    )
    from tests.test_pilot.test_gate import _delist, kev_mirror_spec

    instance, root = pilot_world(
        tmp_path, "kev.json", capture_body_access="restricted", spec=kev_mirror_spec()
    )
    first = run(instance, root)
    activate(instance, first)
    _delist(root)
    second = run(instance, root, minute=1)
    retired = candidate_claims(instance, second)
    assert retired and all(c.retirement.absence is not None for c in retired)
    proposal = second.terminal_egress[0].proposal_id
    for access, visible in ((READER, False), (KEY, True)):
        detail = service_review_playbill_proposal(
            instance, proposal_id=proposal, access=access, detail=True
        )
        text = json.dumps(withhold_review_values(instance, detail.model_dump(mode="json"), access))
        for leak in ('"count_at"', '"enumerated_key_digest"', '"extract_spec"', '"/count"'):
            assert (leak in text) is visible, leak
    activate(instance, second)
    claim = retired[0].identity.qualified
    for access, visible in ((READER, False), (KEY, True)):
        for detail in ("proof", "why"):
            text = _get(instance, claim, access, detail)
            assert ('"count_at"' in text) is visible and ('"extract_spec"' in text) is visible


def _two_kind_world(tmp_path: Path) -> Any:
    """KEV claims admissible under a restricted and an open kind, one Line each."""

    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from tests.core_support._light_capture_support import record_contract
    from tests.test_pilot import support
    from tests.test_procedures import test_claims_terminal as terminal

    policy = ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="fixture",
                claim_roles=("derivation", "normative", "observation"),
                capture_contracts=("test.open-v1", "test.orders-v1"),
                admission="direct",
                subject_binding="exact_claim_subject",
            ),
        )
    )
    original = terminal.claim_type
    accept = terminal.accept_tree
    open_kind = record_contract(name="test.open-v1")

    def with_open_kind(instance: Any, updates: dict[str, bytes], *, name: str) -> None:
        if name == "claims-vocabulary":
            path = capture_contract_path(open_kind.identity.name)
            updates = {**updates, path: render_capture_contract(open_kind)}
        accept(instance, updates, name=name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(terminal, "accept_tree", with_open_kind)
        patch.setattr(
            support,
            "claim_type",
            lambda name, value="enum[open, closed, unknown]", **kw: original(
                name, value, evidence_admission_policy=policy, **kw
            ),
        )
        instance, root = pilot_world(tmp_path, "kev.json", capture_body_access="restricted")
    base = kev_spec().model_dump(mode="python", by_alias=True)
    base["procedure"] = "security.kev-open"
    base["steps"][0]["captures"] = {"capture": "test.open-v1"}
    terminal.install(
        instance,
        procedure_spec(base=base),
        line_name="test.open-line",
        parameters={"logical_source": "cisa.kev.catalog", "relative_path": "data/kev.json"},
    )
    return instance, root


def test_r3_a_restricted_to_open_replacement_withholds_the_delta_in_review_and_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.proposals.review import (
        service_review_playbill_proposal,
        withhold_review_values,
    )

    instance, root = _two_kind_world(tmp_path)
    first = run(instance, root)
    sealed = first.source_observations[0].capture_digest
    activate(instance, first)
    data = json.loads((root / "data" / "kev.json").read_bytes())
    data["vulnerabilities"][0]["dueDate"] = "2026-12-31"
    (root / "data" / "kev.json").write_text(json.dumps(data))
    second = run(instance, root, minute=1, line="test.open-line")
    (revised,) = [
        c for c in candidate_claims(instance, second) if c.statement.predicate.endswith("due_date")
    ]
    assert sealed not in revised.backing.capture_digests  # the candidate rests on the open kind
    proposal = str(second.terminal_egress[0].proposal_id)
    payload = service_review_playbill_proposal(
        instance, proposal_id=proposal, access=READER, detail=True
    ).model_dump(mode="json")
    served_review = withhold_review_values(instance, payload, READER)
    member = next(
        m for m in served_review["members"] if m["path"].endswith(revised.identity.name + ".json")
    )
    assert member["semantic_delta"] == []
    # The base rests on the restricted Capture: its evidence and spans are gone.
    assert "record_locator" not in json.dumps(member["base_semantic_artifact"])
    assert "/vulnerabilities/" not in json.dumps(member["base_semantic_artifact"])
    assert served_review["candidate_digest"] == payload["candidate_digest"]
    # The real approval route, both tiers; the signed statement never changes.
    statements = []
    for mode, visible in (("READ_ONLY", False), ("GOVERNED_WRITE", True)):
        with served(instance, monkeypatch, mode) as api:
            challenge = api.playbill_prepare_approval(
                instance.descriptor.instance_id, proposal, signer_id="owner"
            )
        statements.append(challenge.statement)
        assert (challenge.review.withheld is None) is visible
        (served_member,) = [
            m for m in challenge.review.members if m.path.endswith(revised.identity.name + ".json")
        ]
        base_text = json.dumps(served_member.base_semantic_artifact)
        assert ("record_locator" in base_text) is visible
        assert bool(served_member.semantic_delta) is visible
    assert statements[0] == statements[1]


def test_r4_proposal_proof_records_are_filtered_with_commitments_kept(
    tmp_path: Path,
) -> None:
    instance, _root, state, digest = _restricted(tmp_path)
    proposal = "Proposal:" + str(state.terminal_egress[0].proposal_id)
    reader = json.loads(_get(instance, proposal, READER, "proof"))
    holder = json.loads(_get(instance, proposal, KEY, "proof"))
    assert "record_locator" not in json.dumps(reader) and "record_locator" in json.dumps(holder)
    assert reader["proof"]["withheld"]["capture_digests"] == [digest]
    holder_digests = {k: v for k, v in holder["proof"].items() if k != "withheld"}
    assert json.dumps(reader["proof"]).count("candidate_digest") == json.dumps(
        holder_digests
    ).count("candidate_digest")


def _legacy_shaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Journal payloads as a pre-C14 run wrote them: no versioned consumption."""

    from cruxible_core.service.discovery import runs

    original = runs.run_payload

    def legacy(instance: Any, digest: str) -> object:
        payload = original(instance, digest)
        if isinstance(payload, dict):
            return {key: value for key, value in payload.items() if key != "consumption"}
        return payload

    monkeypatch.setattr(runs, "run_payload", legacy)


def _assert_legacy_withheld(world: Any, run_id: str | None) -> None:
    from cruxible_client.contracts.line_inbox import InboxItemEnvelope, InboxSighting
    from cruxible_core.service.procedures.line_inbox import _withheld

    assert run_id is not None
    status = procedure_run_status(world, run_id, viewer=None, access=READER)
    assert status.withheld is not None and status.withheld.provenance_unavailable
    assert status.result is None
    assert procedure_run_status(world, run_id, viewer=None, access=KEY).withheld is None
    sighting = InboxSighting(run_id=run_id, posted_at=datetime(2026, 10, 29, tzinfo=UTC))
    envelope = InboxItemEnvelope(
        item_id="INB-" + "b" * 64,
        status="unread",
        line="Line:legacy",
        procedure="Procedure:p",
        procedure_artifact_digest="sha256:" + "1" * 64,
        node_id="post",
        occurrences=1,
        first_sighting=sighting,
        recent_sightings=(sighting,),
        dedupe_key="sha256:" + "2" * 64,
        payload_digest="sha256:" + "3" * 64,
        payload_bytes=2,
    )
    assert _withheld(world, (envelope,), READER)[envelope.item_id].provenance_unavailable
    assert _withheld(world, (envelope,), KEY) == {}


def test_r5_a_trigger_fed_run_without_versioned_consumption_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        TriggerFire,
        service_run_playbill_line,
    )
    from tests.test_procedures import test_procedure_source_runs as fixtures
    from tests.test_procedures import test_trigger_fed_proposals as fed

    # Over an OPEN kind: only the provenance record decides.
    instance, _owner, root, line = fed._trigger_fed_world(tmp_path, pytest.MonkeyPatch())
    _capture, event = fed._event(instance, root)
    state = service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequest(line=line.identity.name),
        actor_context=fixtures._actor(instance),
        caller_rung=2,
        daemon_clock=fixtures._TestClock(fixtures.NOW + timedelta(seconds=2)),
        trigger_fire=TriggerFire(trigger=fed.TRIGGER, event=event),
    )
    assert state.run_id is not None
    assert procedure_run_status(instance, state.run_id, viewer=None, access=READER).withheld is None
    _legacy_shaped(monkeypatch)
    _assert_legacy_withheld(instance, state.run_id)


def test_r5_a_parent_run_with_children_without_versioned_consumption_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.governance.actor_context import GovernedActorContext
    from cruxible_core.service.procedures.procedure_runs import (
        ProcedureRunRequest,
        service_run_playbill_procedure,
    )
    from tests.core_support._support import initialize_local
    from tests.test_procedures import test_nested_source_runs as nested

    instance, owner = initialize_local(tmp_path)
    child, parent = nested.blueprints()
    nested.accept_blueprint(instance, owner, child)
    nested.accept_blueprint(instance, owner, parent)
    state = service_run_playbill_procedure(
        instance,
        name="parent",
        request=ProcedureRunRequest(input={}, parameters={"positive": True}),
        actor_context=GovernedActorContext(
            actor_id="owner",
            actor_type="human_user",
            org_id=instance.descriptor.instance_id,
            operation_id="test-nested",
            timestamp=datetime(2026, 8, 21, 12, 3, tzinfo=UTC),
        ),
    )
    assert state.status == "succeeded" and state.run_id is not None
    # Proven empty (a pure parent and its children): nothing withheld.
    assert procedure_run_status(instance, state.run_id, viewer=None, access=READER).withheld is None
    _legacy_shaped(monkeypatch)
    _assert_legacy_withheld(instance, state.run_id)


def test_r6_an_uncited_restricted_prefix_is_never_read_for_a_read_only_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.backlinks import BacklinksRequest
    from cruxible_client.contracts.errors import ReadRefusalError
    from cruxible_core.service.discovery.backlinks import service_playbill_backlinks

    instance, _root, _state, digest = _restricted(tmp_path)  # proposed, never accepted: uncited
    body = None
    handle = "CAP-" + digest.removeprefix("sha256:")[:16]
    instance.body_store().capture_envelopes.contracts_of((digest,))  # one-time index migration
    with read_spy(monkeypatch) as seen:
        read = service_read_playbill_capture(
            instance, request=CaptureReadRequest(capture_digest=handle), access=READER
        )
        card = _get(instance, handle, READER)
        with pytest.raises(ReadRefusalError) as refused:
            service_playbill_backlinks(
                instance, request=BacklinksRequest(ref=handle), access=READER
            )
    assert refused.value.error_code == "cruxible.backlinks.target_unsupported"
    assert read.status == "restricted" and read.capture_digest == digest
    assert '"withheld"' in card
    _assert_unread(seen, digest)
    assert body is None


def test_r6_a_real_restricted_note_keeps_its_section_from_a_read_only_reader(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.backlinks import BacklinksRequest
    from cruxible_client.contracts.captures import (
        capture_contract_path,
        foreign_source_capture_contract,
        render_capture_contract,
    )
    from cruxible_core.service.discovery.backlinks import service_playbill_backlinks
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._write_support import KIND, seed_write_surface
    from tests.test_service.test_backlinks import cite, write
    from tests.test_service.test_notes import SOURCE, about

    instance, _ = seed_write_surface(tmp_path)
    # The workspace source's kind is authored restricted before any section of it lands.
    sealed = foreign_source_capture_contract(SOURCE).model_copy(
        update={"body_access": "restricted"}
    )
    accept_tree(
        instance,
        {capture_contract_path(sealed.identity.name): render_capture_contract(sealed)},
        name="sealed-notes",
    )
    write(instance, about(f"{KIND}/wi-1"), cite("wi-1", "ready"))
    request = BacklinksRequest(ref=f"{KIND}/wi-1")
    reader = service_playbill_backlinks(instance, request=request, access=READER)
    holder = service_playbill_backlinks(instance, request=request, access=KEY)
    assert [link.relation for link in reader.links] == ["about", "cites"]
    assert reader.withheld is not None
    assert reader.withheld.capture_kinds == (sealed.identity.qualified,)
    assert all(link.file is None and link.section is None for link in reader.links)
    assert holder.withheld is None and {link.file for link in holder.links} == {SOURCE}
