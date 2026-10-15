"""One settle request does its shared work once: per-item work stays flat as the batch grows.

A fan-out settle of N Claims over one shared ClaimType, CaptureContract and
Provider is run at two batch sizes in one world. Each test counts one kind of
work the request used to repeat per item (or per pass) and requires the count
to stay fixed, or to grow at most linearly, as N grows; the settled outcome is
the same as before.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.indexes import evaluated_state
from tests.test_procedures import test_procedure_settlement_delivery as SD
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import item_template

FAN_OUT = {"items": f"$steps.{fixtures.SOURCE_ALIAS}.content.json.proposals"}
SMALL, LARGE = 2, 6


def _element(index: int, batch: int) -> dict[str, object]:
    statement = {
        **item_template()["statement"],  # type: ignore[dict-item]
        "qualifier": f"q{index:03d}",
        "object": {"kind": "literal", "value": ("high", "low", "medium")[(index + batch) % 3]},
    }
    return item_template(statement=statement, rationale=f"Severity item {index}.")


def _world(tmp_path: Path) -> tuple[Any, Path, Any]:
    return SD.settle_world(tmp_path, candidate_templates=FAN_OUT)  # type: ignore[no-any-return]


def _settle(instance: Any, root: Path, line: Any, *, items: range, batch: int) -> Any:
    proposals = [_element(index, batch) for index in items]
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({**fixtures.ADVISORY, "proposals": proposals})
    )
    state = SD.run_settle(
        instance, root, line, now=fixtures.NOW + timedelta(minutes=10 * (batch + 1))
    )
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    assert len(egress.children) == len(items)
    assert egress.accepted_git_oid == instance.accepted_coordinate().git_oid
    return egress


def _two_batches(
    tmp_path: Path, measure: Callable[[], Any], *, worlds: list[Any] | None = None
) -> tuple[Counter[str], Counter[str]]:
    """Settle SMALL then LARGE new Claims, each inside its own fresh ``measure()`` window."""

    instance, root, line = _world(tmp_path)
    if worlds is not None:
        worlds.append((instance, root, line))
    counts = []
    for batch, items in enumerate((range(SMALL), range(SMALL, SMALL + LARGE))):
        with measure() as counter:
            _settle(instance, root, line, items=items, batch=batch)
        counts.append(counter)
    return counts[0], counts[1]


@contextmanager
def _patched(owner: Any, name: str, wrap: Callable[[Any], Any]) -> Iterator[None]:
    original = getattr(owner, name)
    setattr(owner, name, wrap(original))
    try:
        yield
    finally:
        setattr(owner, name, original)


# --- L3: one evaluation resolves each shared dependency once -------------------------


def test_an_evaluation_reads_each_shared_dependency_once_however_many_items_cite_it(
    tmp_path: Path,
) -> None:
    """The shared ClaimType, CaptureContract and Provider rows are read once per selection.

    Before, every member's lookup re-ran the row SELECT, read the source bytes,
    parsed and built the accepted model again: about a dozen ClaimType reads per
    item per evaluation.
    """

    owner_tables = ("claim_types", "capture_contracts", "providers")

    @contextmanager
    def measure() -> Iterator[Counter[str]]:
        counter: Counter[str] = Counter()

        def wrap(original: Any) -> Any:
            def one(self: Any, sql: str, key: str) -> Any:
                for table in owner_tables:
                    if f"{table} WHERE" in sql:
                        counter[table] += 1
                return original(self, sql, key)

            return one

        with _patched(evaluated_state.EvaluationRows, "one", wrap):
            yield counter

    small, large = _two_batches(tmp_path, measure)
    assert small["claim_types"] > 0, small
    for table in owner_tables:
        # Fixed per evaluation: the larger batch reads each shared row no more often.
        assert large[table] <= small[table], (table, small, large)


# --- L5: one accepted slot read per item, staged edits indexed by slot ---------------


def test_slot_reads_and_projection_binds_stay_flat_and_staged_claims_are_indexed_by_slot(
    tmp_path: Path,
) -> None:
    """Delivery, its many-valued fill and lowering ask the same slot at one base once.

    Before, each asked again (three accepted reads per item), every reused
    projection bind re-verified its coordinate through a fresh history-index
    snapshot, and each slot lookup on the staged tree re-decided every edit
    staged so far (n^2/2 Claim facts at n items).
    """

    from cruxible_core.derived import derived_state
    from cruxible_core.runtime.instance import PlaybillInstance

    @contextmanager
    def measure() -> Iterator[Counter[str]]:
        counter: Counter[str] = Counter()

        def facts(original: Any) -> Any:
            def counted(path: str, content: bytes) -> Any:
                counter["claim_facts"] += 1
                return original(path, content)

            return counted

        def reads(original: Any) -> Any:
            def counted(self: Any, statement: Any) -> Any:
                counter["accepted_slot_reads"] += 1
                return original(self, statement)

            return counted

        def verified(original: Any) -> Any:
            def counted(self: Any, **kwargs: Any) -> Any:
                counter["coordinate_verifications"] += 1
                return original(self, **kwargs)

            return counted

        with (
            _patched(derived_state, "_claim_facts", facts),
            _patched(evaluated_state.EvaluationRows, "claim_items", reads),
            _patched(PlaybillInstance, "resolve_accepted_coordinate", verified),
        ):
            yield counter

    small, large = _two_batches(tmp_path, measure)
    assert small["accepted_slot_reads"] > 0, small
    # One accepted read per added item, not one per pass that asks.
    assert large["accepted_slot_reads"] - small["accepted_slot_reads"] <= LARGE - SMALL, (
        small,
        large,
    )
    # A reused bind re-verifies its coordinate once per epoch, not once per lookup.
    assert large["coordinate_verifications"] <= small["coordinate_verifications"], (small, large)
    # Each staged Claim is indexed a bounded number of times: linear, not n^2/2.
    assert small["claim_facts"] > 0, small
    assert large["claim_facts"] * SMALL <= small["claim_facts"] * LARGE, (small, large)


# --- L4: activation takes the submission in hand ---------------------------------------


def _activation_reads() -> Any:
    """Count candidate, inspect and proposal-tree read-backs activation makes before settling."""

    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.authoring import documents

    @contextmanager
    def measure() -> Iterator[Counter[str]]:
        counter: Counter[str] = Counter()
        # True from activation's start until it hands over to settlement.
        reading = [False]

        def activation(original: Any) -> Any:
            def counted(*args: Any, **kwargs: Any) -> Any:
                counter["activations"] += 1
                counter["handed_off"] += kwargs.get("handoff") is not None
                reading[0] = True
                try:
                    return original(*args, **kwargs)
                finally:
                    reading[0] = False

            return counted

        def settling(original: Any) -> Any:
            def counted(*args: Any, **kwargs: Any) -> Any:
                reading[0] = False
                return original(*args, **kwargs)

            return counted

        def read(name: str) -> Callable[[Any], Any]:
            def wrap(original: Any) -> Any:
                def counted(*args: Any, **kwargs: Any) -> Any:
                    if reading[0]:
                        counter[name] += 1
                    return original(*args, **kwargs)

                return counted

            return wrap

        with (
            _patched(documents, "service_activate_playbill_proposal", activation),
            _patched(ProposalEvidenceStore, "read_candidate", read("read_candidate")),
            _patched(documents, "service_inspect_playbill_proposal", read("inspect")),
            _patched(PlaybillInstance, "proposal_tree", read("proposal_tree")),
            _patched(PlaybillInstance, "settle_and_activate", settling),
        ):
            yield counter

    return measure


def test_a_settle_activates_from_its_own_submission_without_reading_it_back(
    tmp_path: Path,
) -> None:
    """No candidate re-read, inspect or proposal-tree read-back at any batch size.

    Before, activation re-read the candidate twice (status, inspect) and read
    the proposal tree back from Git, every item's candidate member re-validated.
    """

    small, large = _two_batches(tmp_path, _activation_reads())
    for counts in (small, large):
        assert counts["activations"] == 1 and counts["handed_off"] == 1, counts
        assert counts["read_candidate"] == 0, counts
        assert counts["inspect"] == 0, counts
        assert counts["proposal_tree"] == 0, counts


# --- L8: the settle condition is checked once per head ---------------------------------


def test_admission_takes_the_settle_condition_checked_just_before_it_at_the_same_head(
    tmp_path: Path,
) -> None:
    """The admission evaluation reuses the terminal's own check; other inputs re-check."""

    from cruxible_core.proposals import delegated_authority
    from cruxible_core.proposals.delegated_authority import DelegatedAuthorityVerdict
    from cruxible_core.proposals.proposals import ProposalService

    @contextmanager
    def measure() -> Iterator[Counter[str]]:
        counter: Counter[str] = Counter()
        submitting = [False]

        def submit(original: Any) -> Any:
            def counted(*args: Any, **kwargs: Any) -> Any:
                submitting[0] = True
                try:
                    return original(*args, **kwargs)
                finally:
                    submitting[0] = False

            return counted

        def issues(original: Any) -> Any:
            def counted(*args: Any, **kwargs: Any) -> Any:
                counter["in_admission" if submitting[0] else "elsewhere"] += 1
                return original(*args, **kwargs)

            return counted

        with (
            _patched(ProposalService, "submit", submit),
            _patched(delegated_authority, "delegated_authority_issues", issues),
        ):
            yield counter

    worlds: list[Any] = []
    small, large = _two_batches(tmp_path, measure, worlds=worlds)
    assert small["in_admission"] == large["in_admission"] == 0, (small, large)

    ((instance, _root, _line),) = worlds
    head = instance.accepted_coordinate()
    scope = ("claims/a.json", "claims/b.json")
    tree = {"claims/a.json": b"a", "claims/b.json": b"b", "subjects/x.json": b"x"}
    verdict = DelegatedAuthorityVerdict.of(
        mandate_digest="sha256:" + "1" * 64,
        current=head,
        timestamp="2026-09-12T12:00:00.000000Z",
        scope=scope,
        candidate_tree=tree,
        issues=(),
    )
    same = {
        "mandate_digest": verdict.mandate_digest,
        "current": head,
        "timestamp": verdict.timestamp,
        "scope": scope,
        "candidate_tree": {**tree, "subjects/x.json": b"unread"},
    }
    assert verdict.answers(**same)  # type: ignore[arg-type]
    for change in (
        {"mandate_digest": "sha256:" + "2" * 64},
        {"current": head.model_copy(update={"git_oid": "0" * len(head.git_oid)})},
        {"timestamp": "2026-09-12T12:00:01.000000Z"},
        {"scope": ("claims/a.json",)},
        {"candidate_tree": {**tree, "claims/b.json": b"changed"}},
    ):
        assert not verdict.answers(**{**same, **change}), change  # type: ignore[arg-type]


# --- L1: settlement reuses the same request's admission evaluation ---------------------


@contextmanager
def _settlement_evaluations(
    tamper: Callable[[Any, Any], Any] | None = None,
) -> Iterator[Counter[str]]:
    """Count full settlement evaluations; check each served bundle against a full one.

    Every settlement is prepared twice: first with no admission evaluation (the
    oracle: a full re-evaluation from the stored records), then as served. The
    two bundles must agree in every field that becomes the generation. Only
    the served preparation's evaluations are counted. ``tamper(instance,
    admitted)`` may replace the admitted evaluation the served one receives.
    Why a scope went ineligible is recorded, to name it if a test fails.
    """

    from cruxible_core.proposals import settlement
    from cruxible_core.proposals.prepared_evaluation import PreparedEvaluationScope
    from cruxible_core.runtime.instance import PlaybillInstance

    counter: Counter[str] = Counter()
    serving = [False]
    why: list[str] = []

    def evaluate(original: Any) -> Any:
        def counted(**kwargs: Any) -> Any:
            if serving[0]:
                counter["full_evaluations"] += 1
            return original(**kwargs)

        return counted

    def prepare(original: Any) -> Any:
        def both(self: Any, **kwargs: Any) -> Any:
            admitted = kwargs.get("admitted")
            counter["handed"] += admitted is not None
            oracle = original(self, **{**kwargs, "admitted": None})
            if admitted is not None and tamper is not None:
                kwargs = {**kwargs, "admitted": tamper(self, admitted)}
            serving[0] = True
            try:
                served = original(self, **kwargs)
            finally:
                serving[0] = False
            assert served.record == oracle.record
            assert served.tree == oracle.tree
            assert dict(served.members) == dict(oracle.members)
            assert (served.merkle is None) == (oracle.merkle is None)
            if served.merkle is not None and oracle.merkle is not None:
                assert served.merkle.root == oracle.merkle.root
            assert served.semantic_root == oracle.semantic_root
            assert served.generation_root == oracle.generation_root
            assert served.approvals == oracle.approvals
            return served

        return both

    original_setattr = PreparedEvaluationScope.__setattr__

    def observe_ineligible(self: Any, name: str, value: Any) -> None:
        if name == "ineligible" and value is True and not getattr(self, "ineligible", False):
            import traceback

            frames = traceback.extract_stack(limit=4)[:-1]
            why.append(" < ".join(f"{f.name}:{f.lineno}" for f in reversed(frames)))
        original_setattr(self, name, value)

    with (
        _patched(settlement, "evaluate_proposal_tree", evaluate),
        _patched(PlaybillInstance, "prepare_generation", prepare),
        _patched(PreparedEvaluationScope, "__setattr__", lambda _o: observe_ineligible),
    ):
        yield counter
    counter["ineligible_reasons"] = len(why)
    counter.update({f"ineligible: {reason}": 1 for reason in why})


def test_a_settle_reuses_its_admission_evaluation_and_settles_what_a_full_one_would(
    tmp_path: Path,
) -> None:
    """At both batch sizes settlement evaluates nothing, and its bundle equals a full one's."""

    small, large = _two_batches(tmp_path, _settlement_evaluations)
    for counts in (small, large):
        assert counts["handed"] == 1, counts
        assert counts["full_evaluations"] == 0, counts


@pytest.mark.parametrize(
    "violation",
    ["epoch", "base", "rebased", "candidate", "tree", "body_replay", "facts_replay", "stored"],
)
def test_each_unmet_reuse_condition_settles_by_a_full_re_evaluation(
    tmp_path: Path, violation: str
) -> None:
    """Break one reuse condition and settlement re-evaluates in full, to the same bundle."""

    from dataclasses import replace

    from cruxible_core.derived.derived_state import SnapshotTree
    from cruxible_core.proposals.prepared_evaluation import _FactsObservation
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
    from cruxible_core.service.authoring import documents

    def tamper(instance: Any, admitted: Any) -> Any:
        outcome = admitted.outcome
        if violation == "epoch":
            instance.prepared_evaluations.clear()
            return admitted
        if violation == "base":
            moved = admitted.current.model_copy(update={"semantic_root": "sha256:" + "0" * 64})
            return replace(admitted, current=moved)
        if violation == "rebased":
            return replace(admitted, outcome=replace(outcome, rebased=True))
        if violation == "candidate":
            other = outcome.candidate.model_copy(update={"candidate_digest": "sha256:" + "0" * 64})
            return replace(admitted, outcome=replace(outcome, candidate=other))
        if violation == "tree":
            copied = SnapshotTree(dict(outcome.tree))
            return replace(admitted, outcome=replace(outcome, tree=copied))
        if violation == "body_replay":
            assert admitted.observations, "the settle observed no body read to replay"
            first, *rest = admitted.observations
            result = (not first.result) if isinstance(first.result, bool) else b""
            return replace(admitted, observations=(replace(first, result=result), *rest))
        assert violation == "facts_replay"
        # A fact build whose answer is not what the provider answers now.
        unseen = _FactsObservation(admitted.current, ("never.read",), None, object())
        return replace(admitted, facts=(*admitted.facts, unseen))

    instance, root, line = _world(tmp_path)
    before = instance.prepared_evaluations.status()
    if violation == "stored":
        # The stored evaluation is not the one in hand: activation reads it all back.
        real_read = ProposalEvidenceStore.read_evaluation

        def check(original: Any) -> Any:
            def differing(*args: Any, **kwargs: Any) -> Any:
                def read_evaluation(self: Any, proposal_id: str) -> Any:
                    record = real_read(self, proposal_id)
                    return record.model_copy(update={"evaluated_at": "2026-01-01T00:00:00.000000Z"})

                ProposalEvidenceStore.read_evaluation = read_evaluation  # type: ignore[method-assign]
                try:
                    return original(*args, **kwargs)
                finally:
                    ProposalEvidenceStore.read_evaluation = real_read  # type: ignore[method-assign]

            return differing

        with (
            _patched(documents, "_submitted_for_activation", check),
            _settlement_evaluations() as counts,
        ):
            _settle(instance, root, line, items=range(SMALL), batch=0)
        assert counts["handed"] == 0 and counts["full_evaluations"] == 1, counts
        return
    with _settlement_evaluations(tamper) as counts:
        _settle(instance, root, line, items=range(SMALL), batch=0)
    after = instance.prepared_evaluations.status()
    assert counts["handed"] == 1, counts
    assert counts["full_evaluations"] == 1, counts
    assert after["settlement_reused"] == before["settlement_reused"], (before, after)


def test_a_separately_requested_activation_always_re_evaluates_in_full(tmp_path: Path) -> None:
    """A fallback proposal approved and activated by its own request is settled from storage."""

    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.test_ledger.test_activation import _sign

    instance, root, line = SD.settle_world(
        tmp_path, only_subject="someone-else", candidate_templates=FAN_OUT
    )
    proposals = [_element(index, 0) for index in range(SMALL)]
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({**fixtures.ADVISORY, "proposals": proposals})
    )
    state = SD.run_settle(instance, root, line)
    (egress,) = state.terminal_egress
    assert egress.settle_outcome == "proposed" and egress.proposal_id is not None
    approval = _sign(
        SD.OWNERS[instance.root],
        egress.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=egress.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    with _settlement_evaluations() as counts:
        receipt = service_activate_playbill_proposal(
            instance, proposal_id=egress.proposal_id, activated_by="owner"
        )
    assert receipt.status == "accepted"
    assert counts["handed"] == 0 and counts["full_evaluations"] == 1, counts


def test_a_committed_write_settles_from_its_preflight_evaluation(tmp_path: Path) -> None:
    """A write evaluates once: preflight, reused by admission and then by settlement."""

    from cruxible_client.contracts.write import WriteRequest
    from cruxible_core.proposals import proposals
    from cruxible_core.service.authoring.write_verbs import service_playbill_write
    from tests.core_support._write_support import KIND, caller, seed_write_surface

    instance = seed_write_surface(tmp_path)[0]
    request = WriteRequest.model_validate(
        {
            "because": "One evaluation per write.",
            "changes": [
                {"op": "set", "subject": f"{KIND}/reuse-{n}", "field": "title", "value": f"t{n}"}
                for n in range(3)
            ],
        }
    )
    admissions: list[int] = []
    before = instance.prepared_evaluations.status()
    with (
        _patched(
            proposals,
            "evaluate_proposal_tree",
            lambda original: lambda **kwargs: admissions.append(1) or original(**kwargs),
        ),
        _settlement_evaluations() as counts,
    ):
        outcome = service_playbill_write(instance, request=request, caller=caller())
    after = instance.prepared_evaluations.status()
    assert outcome.status == "accepted", outcome
    assert admissions == []  # admission reused the preflight evaluation
    assert counts["handed"] == 1 and counts["full_evaluations"] == 0, counts
    assert after["settlement_reused"] == before["settlement_reused"] + 1, (before, after)
