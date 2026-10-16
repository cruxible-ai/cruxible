"""Semantic ResolutionContract activation, replay, and authority laws."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    typed_digest,
)
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_node_digests
from cruxible_client.contracts.procedures.measurements import (
    AcceptedQueryProcedureMeasurement,
    ProcedureMeasurementDeclaration,
    ProcedureMeasurementExpectation,
)
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    GuardPredicate,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    StateTapNode,
)
from cruxible_core.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    append_procedure_resolution,
    append_resolution_disposition,
    build_procedure_resolution,
    build_resolution_disposition,
    derive_resolution_activations,
    evaluate_procedure_resolution,
    resolution_contract_partition_id,
)
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from tests.core_support._subject_kind_support import declare_created_kinds, world_history
from tests.core_support._support import client_material, initialize_local
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK, accepted_procedure
from tests.test_ledger.test_activation import _sign

NOW = datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-resolution-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _duration(value: int) -> CanonicalDuration:
    return CanonicalDuration(microseconds=value)


def _coordinate(label: str = "accepted") -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot, "playbill-resolution-semantic-v1", {"label": label}
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot, "playbill-resolution-generation-v1", {"label": label}
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor(actor_id: str = "operator") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="instance-a",
        operation_id=f"operation-{actor_id}",
        timestamp=NOW,
    )


def _measurement(
    name: str,
    grain: str,
    *,
    node_id: str | None = None,
    from_node_id: str | None = None,
    arm_label: str | None = None,
) -> ProcedureMeasurementDeclaration:
    return ProcedureMeasurementDeclaration(
        name=name,
        subject_grain=grain,  # type: ignore[arg-type]
        node_id=node_id,
        from_node_id=from_node_id,
        arm_label=arm_label,  # type: ignore[arg-type]
        measurement=AcceptedQueryProcedureMeasurement(
            query=_pin("query", "QueryDefinition", f"measure-{name}"),
            expect=ProcedureMeasurementExpectation(min_count=1),
        ),
        check_after=_duration(1_000_000),
        expires_after=_duration(10_000_000),
    )


def _accepted() -> AcceptedProcedure:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    definition = ProcedureDefinition(
        name="measured-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNode(
                node_id="read",
                query=_pin("query", "QueryDefinition", "state"),
                as_="rows",
            ),
            GuardNode(
                node_id="gate",
                predicate=GuardPredicate(
                    left=PredicateOperand(kind="count", alias="rows"),
                    operator="gt",
                    right=PredicateOperand(kind="literal", value=0),
                ),
                on_true="hot",
                on_false="cold",
                refusal_code="empty",
                message="No rows.",
            ),
            ProjectNode(
                node_id="hot",
                fields={"arm": "hot"},
                contract_out=contract_out,
                as_="hot_result",
                next="finish",
            ),
            ProjectNode(
                node_id="cold",
                fields={"arm": "cold"},
                contract_out=contract_out,
                as_="cold_result",
                next="finish",
            ),
            ProjectNode(
                node_id="finish",
                fields={"status": "done"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        measurements=(
            _measurement(
                "arm-health", "arm", node_id="hot", from_node_id="gate", arm_label="on_true"
            ),
            _measurement("node-health", "node", node_id="hot"),
            _measurement("unit-health", "procedure_unit"),
        ),
        budget=ProcedureBudget(
            wall_clock=RUN_WALL_CLOCK,
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )
    return accepted_procedure(definition, activation_policy="snapshot")


def test_accepted_measurements_derive_exact_semantic_grains_and_windows() -> None:
    accepted = _accepted()
    activations = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )

    arm, node, unit = activations
    assert arm.subject.address.selector.scheme == "procedure-arm-v1"
    assert arm.subject.address.selector.value == "gate:on_true:hot"
    assert arm.arm_subtree_digest == arm.subject.content_digest
    assert node.subject.address.selector.scheme == "procedure-node-v1"
    assert node.node_local_digest == node.subject.content_digest
    assert unit.subject.address.selector.scheme == "procedure-unit-v1"
    assert unit.subject.content_digest == accepted.procedure.definition_digest
    assert all(item.check_at == NOW + timedelta(seconds=1) for item in activations)
    assert all(item.expires_at == NOW + timedelta(seconds=10) for item in activations)
    assert (
        derive_resolution_activations(
            accepted,
            accepted_coordinate=_coordinate(),
            activated_at=NOW,
        )
        == activations
    )


def test_measurements_use_node_and_subtree_digests() -> None:
    accepted = _accepted()
    activations = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )
    expected = compute_procedure_node_digests(accepted.procedure.definition)

    arm, node, unit = activations
    assert arm.from_node_local_digest == expected["gate"].local_digest
    assert arm.node_local_digest == expected["hot"].local_digest
    assert node.node_local_digest == expected["hot"].local_digest
    assert unit.subject.content_digest == accepted.procedure.definition_digest


def test_resolution_and_latest_disposition_replay_from_exhaust(tmp_path) -> None:
    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query-receipt")),)
    resolution = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 2},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )
    disposition = build_resolution_disposition(
        resolution,
        sequence=1,
        verdict="overturned",
        reviewer_actor_context=_actor("reviewer"),
        recorded_at=NOW + timedelta(seconds=4),
        note="Query result was invalidated.",
    )
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(cas_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    partition_id = resolution_contract_partition_id(activation)
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer",
        expected_head=journal.read_head(stream, partition_id),
    )
    writer = ProcedureExhaustWriter(
        journal=journal,
        bodies=bodies,
        fencing_token="writer",
    )
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=resolution,
        stream=stream,
    )
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    assert book.latest_non_overturned(activation.contract_id) == resolution

    append_resolution_disposition(
        writer,
        activation=activation,
        resolution=resolution,
        disposition=disposition,
        stream=stream,
    )
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    assert book.latest_non_overturned(activation.contract_id) is None


def test_resolution_law_refuses_goodhart_clock_and_expectation_mismatches() -> None:
    activation = derive_resolution_activations(
        _accepted(),
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query")),)
    early = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
    )
    assert evaluate_procedure_resolution(activation, early).refusal_code == (
        "resolution.before_check_at"
    )

    false_success = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 0},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=2),
        actor_context=_actor(),
    )
    assert evaluate_procedure_resolution(activation, false_success).refusal_code == (
        "resolution.expectation_not_satisfied"
    )


def test_resolution_contract_reopens_only_after_latest_answer_is_overturned(tmp_path) -> None:
    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted,
        accepted_coordinate=_coordinate(),
        activated_at=NOW,
    )[2]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("query")),)
    first = build_procedure_resolution(
        activation,
        sequence=1,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=2),
        recorded_at=NOW + timedelta(seconds=2),
        actor_context=_actor(),
    )
    journal_root = tmp_path / "journal"
    journal_root.mkdir()
    cas_root = tmp_path / "cas"
    cas_root.mkdir()
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(cas_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    partition_id = resolution_contract_partition_id(activation)
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer",
        expected_head=journal.read_head(stream, partition_id),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    append_procedure_resolution(
        writer,
        activation=activation,
        resolution=first,
        stream=stream,
    )
    second = build_procedure_resolution(
        activation,
        sequence=2,
        verdict="satisfied",
        value={"count": 1},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=3),
        recorded_at=NOW + timedelta(seconds=3),
        actor_context=_actor(),
    )
    with pytest.raises(ExecutionError, match="closed until"):
        append_procedure_resolution(
            writer,
            activation=activation,
            resolution=second,
            stream=stream,
        )


@world_history
def _accept_tree(instance, owner, tree, *, timestamp: str, proposal_name: str) -> None:
    base = instance.accepted_coordinate()
    declare_created_kinds(instance.tree_at(base.git_oid), tree, instance=instance)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{proposal_name}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )
    assert proposed.candidate is not None, proposed.evaluation.diagnostics
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                proposed.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_derived_activation_remains_bound_to_its_accepting_generation(tmp_path) -> None:
    instance, owner = initialize_local(tmp_path)
    accepted = _accepted()
    procedure = accepted.procedure.model_copy(update={})
    procedure_path_value = procedure_path(procedure.identity.name)
    first_tree = {
        **instance.tree_at(instance.accepted_coordinate().git_oid),
        procedure_path_value: render_procedure(procedure),
    }
    _accept_tree(
        instance,
        owner,
        first_tree,
        timestamp="2026-08-17T15:00:00.000000Z",
        proposal_name="measured-procedure",
    )
    accepting_coordinate = instance.accepted_coordinate()

    body = instance.store_document_body(b"unrelated accepted state\n")
    document = DocumentShell(
        identity="document:unrelated",
        document_kind="note",
        title="Unrelated",
        media_type="text/plain",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:test",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    second_tree = {
        **instance.tree_at(accepting_coordinate.git_oid),
        "documents/unrelated.json": render_document(document),
    }
    _accept_tree(
        instance,
        owner,
        second_tree,
        timestamp="2026-08-17T16:00:00.000000Z",
        proposal_name="unrelated-document",
    )
    assert instance.accepted_coordinate().git_oid != accepting_coordinate.git_oid

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as handle:
        rows = handle.typed.facts(
            "cruxible.procedure.resolution_activation",
            identity=procedure.identity.qualified,
        )
    assert len(rows) == 3
    activation_coordinate = rows[0].value["subject"]["accepted_coordinate"]
    assert activation_coordinate["git_oid"] == accepting_coordinate.git_oid
    assert activation_coordinate["semantic_root"] == accepting_coordinate.semantic_root
    assert activation_coordinate["generation_root"] == accepting_coordinate.generation_root


def _resolution_partition(tmp_path, name: str = "b9"):  # type: ignore[no-untyped-def]
    """A journal partition of one derived resolution contract, with its writer."""

    accepted = _accepted()
    activation = derive_resolution_activations(
        accepted, accepted_coordinate=_coordinate(), activated_at=NOW
    )[2]
    root = tmp_path / name
    (root / "journal").mkdir(parents=True)
    (root / "cas").mkdir()
    journal = LocalJournalBackend(root / "journal")
    bodies = ContentAddressedBodyStore(root / "cas")
    stream = JournalStreamIdentityV1(
        instance_id=f"instance-{name}",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    partition_id = resolution_contract_partition_id(activation)
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token="writer",
        expected_head=journal.read_head(stream, partition_id),
    )
    writer = ProcedureExhaustWriter(journal=journal, bodies=bodies, fencing_token="writer")
    return activation, journal, bodies, stream, partition_id, writer


def _answer(activation, writer, stream, sequence: int, *, overturn: bool):  # type: ignore[no-untyped-def]
    evidence = (ProcedureProofReferenceV1(kind="query_receipt", digest=_digest("q")),)
    resolution = build_procedure_resolution(
        activation,
        sequence=sequence,
        verdict="satisfied",
        value={"count": sequence},
        evidence_refs=evidence,
        observed_at=NOW + timedelta(seconds=10 * sequence),
        recorded_at=NOW + timedelta(seconds=10 * sequence),
        actor_context=_actor(),
    )
    append_procedure_resolution(writer, activation=activation, resolution=resolution, stream=stream)
    if overturn:
        append_resolution_disposition(
            writer,
            activation=activation,
            resolution=resolution,
            disposition=build_resolution_disposition(
                resolution,
                sequence=1,
                verdict="overturned",
                reviewer_actor_context=_actor("reviewer"),
                recorded_at=NOW + timedelta(seconds=10 * sequence + 1),
                note=f"Overturned {sequence}.",
            ),
            stream=stream,
        )
    return resolution


def _replayed(activation, journal, bodies, stream, partition_id):  # type: ignore[no-untyped-def]
    book = ProcedureResolutionBook((activation,))
    book.replay(journal.all_records(stream, partition_id), bodies=bodies)
    return book


def _same_answers(view, book, activation) -> None:  # type: ignore[no-untyped-def]
    contract = activation.contract_id
    resolutions = book.resolutions.get(contract, [])
    assert view.resolution_count(contract) == len(resolutions)
    assert view.latest_resolution(contract) == (resolutions[-1] if resolutions else None)
    assert view.latest_non_overturned(contract) == book.latest_non_overturned(contract)
    for item in resolutions:
        assert view.resolution_by_id(item.resolution_id) == book.resolution_by_id(
            item.resolution_id
        )
        dispositions = book.dispositions.get(item.resolution_id, [])
        assert view.last_disposition(item.resolution_id) == (
            dispositions[-1] if dispositions else None
        )
        assert view.disposition_count(item.resolution_id) == len(dispositions)
        assert view.last_disposition_verdict(item.resolution_id) == (
            dispositions[-1].verdict if dispositions else None
        )

    all_reviews = [
        review for item in resolutions for review in book.dispositions.get(item.resolution_id, [])
    ]
    assert view.review_count(contract) == len(all_reviews)
    assert view.review_history(contract, limit=3) == tuple(reversed(all_reviews[-3:]))


def test_a_resolution_book_extends_from_its_last_head_and_equals_a_full_replay(
    tmp_path, monkeypatch
) -> None:
    """Each append folds only the records after the last folded head (perf B9)."""

    from cruxible_core.procedures import resolution as module
    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition_id, writer = _resolution_partition(tmp_path)
    folded: list[int] = []
    extend = module._ResolutionFold.extend

    def counted(self, records, **kwargs):  # type: ignore[no-untyped-def]
        folded.append(len(records))
        return extend(self, records, **kwargs)

    monkeypatch.setattr(module._ResolutionFold, "extend", counted)
    for sequence in range(1, 5):
        _answer(activation, writer, stream, sequence, overturn=True)
        view, head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
        assert head == journal.read_head(stream, partition_id)
        replayed = _replayed(activation, journal, bodies, stream, partition_id)
        _same_answers(view, replayed, activation)
        latest = view.latest_resolution(activation.contract_id)
        assert latest is not None
        record = view.resolution_record(latest.resolution_id)
        assert record is not None and record.record.event_kind == "resolution"
    _answer(activation, writer, stream, 5, overturn=False)
    view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    replayed = _replayed(activation, journal, bodies, stream, partition_id)
    _same_answers(view, replayed, activation)
    # After the first fold, each append and read folds only what it has not seen.
    assert max(folded[1:]) <= 2, folded


def test_a_returned_resolution_never_reaches_the_kept_fold(tmp_path) -> None:
    """Review F-004: values handed out are detached copies of the immutable fold."""

    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition_id, writer = _resolution_partition(tmp_path)
    _answer(activation, writer, stream, 1, overturn=False)
    view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    poisoned = view.latest_non_overturned(activation.contract_id)
    assert poisoned is not None
    poisoned.value["count"] = 999
    record = view.resolution_record(poisoned.resolution_id)
    assert record is not None
    again, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    kept = again.latest_non_overturned(activation.contract_id)
    assert kept is not None and kept.value == {"count": 1}
    replayed = _replayed(activation, journal, bodies, stream, partition_id)
    assert kept == replayed.latest_non_overturned(activation.contract_id)


def _assert_unavailable_as_a_replay(  # type: ignore[no-untyped-def]
    activation, journal, bodies, stream, partition_id, writer, digest
) -> None:
    """Damaged exhaust reads unavailable, never absent: the kept fold answers as a
    full replay does, and no append proceeds on it (durability-policy-1015)."""

    from cruxible_core.exhaust.payloads import UnavailablePayload
    from cruxible_core.procedures.resolution import resolution_book_at_head

    replayed = _replayed(activation, journal, bodies, stream, partition_id)
    view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    assert replayed.unavailable == (UnavailablePayload(digest),)
    assert view.unavailable == replayed.unavailable
    _same_answers(view, replayed, activation)
    with pytest.raises(ExecutionError, match="unavailable"):
        _answer(activation, writer, stream, 2, overturn=False)


@pytest.mark.parametrize("damage", ["erase", "corrupt", "same_bytes_new_file"])
def test_a_kept_fold_refuses_exactly_as_a_replay_after_payload_damage(tmp_path, damage) -> None:
    """Sequenced erase/damage changes invalidate only their subscribed books."""

    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition_id, writer = _resolution_partition(tmp_path)
    first = _answer(activation, writer, stream, 1, overturn=True)
    view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    assert view.resolution_count(activation.contract_id) == 1
    digest = next(
        stored.record.payload_digest
        for stored in journal.all_records(stream, partition_id)
        if stored.record.event_kind == "resolution"
    )
    path = bodies._path(digest)
    original = path.read_bytes()
    if damage == "erase":
        assert bodies.erase(digest)
    else:
        path.unlink()
        path.write_bytes(b"{}" if damage == "corrupt" else original)
        # The evidence sweep/direct verification publishes out-of-band damage.
        bodies.availability(digest)
    if damage == "same_bytes_new_file":
        view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
        replayed = _replayed(activation, journal, bodies, stream, partition_id)
        _same_answers(view, replayed, activation)
        _answer(activation, writer, stream, 2, overturn=False)
        view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
        replayed = _replayed(activation, journal, bodies, stream, partition_id)
        _same_answers(view, replayed, activation)
        return
    _assert_unavailable_as_a_replay(
        activation, journal, bodies, stream, partition_id, writer, digest
    )
    assert first.sequence == 1


def test_a_fixed_append_and_lookup_cost_the_same_at_two_history_sizes(
    tmp_path, monkeypatch
) -> None:
    """All work for a fixed append and lookup is independent of retained history."""

    from cruxible_core.procedures import resolution as module
    from cruxible_core.procedures.resolution import resolution_book_at_head

    def work(history: int, name: str) -> tuple[int, int, int]:
        activation, journal, bodies, stream, partition_id, writer = _resolution_partition(
            tmp_path, name
        )
        for sequence in range(1, history + 1):
            _answer(activation, writer, stream, sequence, overturn=True)
        resolution_book_at_head(journal, stream, activation, bodies=bodies)
        counts = {"read": 0, "loads": 0, "stat": 0}
        read, identity, loads = bodies.read, bodies.file_identity, module.pickle.loads

        def counted_read(*args, **kwargs):  # type: ignore[no-untyped-def]
            counts["read"] += 1
            return read(*args, **kwargs)

        def counted_identity(*args, **kwargs):  # type: ignore[no-untyped-def]
            counts["stat"] += 1
            return identity(*args, **kwargs)

        def counted_loads(*args, **kwargs):  # type: ignore[no-untyped-def]
            counts["loads"] += 1
            return loads(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(bodies, "read", counted_read)
            patch.setattr(bodies, "file_identity", counted_identity)
            patch.setattr(module.pickle, "loads", counted_loads)
            _answer(activation, writer, stream, history + 1, overturn=False)
            view, _head = resolution_book_at_head(journal, stream, activation, bodies=bodies)
            latest = view.latest_non_overturned(activation.contract_id)
            assert latest is not None
            assert view.last_disposition(latest.resolution_id) is None
            assert len(view.review_history(activation.contract_id, limit=1)) == 1
        return counts["read"], counts["loads"], counts["stat"]

    small, large = work(2, "small"), work(6, "large")
    assert small == large, (small, large)
    assert small[2] == 0


def test_damage_landing_after_verified_read_invalidates_the_next_book(
    tmp_path, monkeypatch
) -> None:
    """A sweep finding damage after a verified read is consumed on the next call."""

    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition_id, writer = _resolution_partition(tmp_path)
    _answer(activation, writer, stream, 1, overturn=True)
    resolution_book_at_head(journal, stream, activation, bodies=bodies)
    digest = next(
        stored.record.payload_digest
        for stored in journal.all_records(stream, partition_id)
        if stored.record.event_kind == "resolution"
    )
    path = bodies._path(digest)
    original = path.read_bytes()
    assert bodies.erase(digest)
    bodies.store(original)  # Sequenced restoration forces changed-input verification.
    read = bodies.read

    def replaced_after_verified_read(asked, *, access):  # type: ignore[no-untyped-def]
        content = read(asked, access=access)
        if asked == digest:
            path.unlink()
            path.write_bytes(b"{}")  # Damage after the verified read.
            assert bodies.availability(digest) == "corrupt"
        return content

    with monkeypatch.context() as patch:
        patch.setattr(bodies, "read", replaced_after_verified_read)
        resolution_book_at_head(journal, stream, activation, bodies=bodies)
    _assert_unavailable_as_a_replay(
        activation, journal, bodies, stream, partition_id, writer, digest
    )


def test_cas_changes_only_visit_affected_books_and_repair_without_a_history_scan(
    tmp_path, monkeypatch
):
    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition, writer = _resolution_partition(tmp_path)
    for sequence in range(1, 7):
        _answer(activation, writer, stream, sequence, overturn=True)
    second = accepted_procedure(
        _accepted().procedure.definition.model_copy(update={"name": "other-measured"}),
        activation_policy="snapshot",
    )
    other = derive_resolution_activations(
        second, accepted_coordinate=_coordinate(), activated_at=NOW
    )[2]
    other_partition = resolution_contract_partition_id(other)
    journal.activate_writer(
        stream,
        other_partition,
        fencing_token="writer",
        expected_head=journal.read_head(stream, other_partition),
    )
    _answer(other, writer, stream, 1, overturn=False)
    view, _ = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    resolution_book_at_head(journal, stream, other, bodies=bodies)
    digest = view.resolution_record(
        view.latest_resolution(activation.contract_id).resolution_id
    ).record.payload_digest
    original = bodies.read(
        digest, access=BodyAccessContext(principal_id="test", can_read_body=True)
    )
    assert bodies.erase(digest)
    reads = []
    read = bodies.read

    def counted_read(asked, *, access):
        reads.append(asked)
        return read(asked, access=access)

    def no_rebuild(*args, **kwargs):
        pytest.fail("A warm fold walked journal history.")

    with monkeypatch.context() as patch:
        patch.setattr(bodies, "read", counted_read)
        patch.setattr(journal, "all_records", no_rebuild)
        unaffected, _ = resolution_book_at_head(journal, stream, other, bodies=bodies)
        assert not unaffected.unavailable and reads == []
        damaged, _ = resolution_book_at_head(journal, stream, activation, bodies=bodies)
        assert damaged.unavailable and reads == [digest]
        # The unavailable book is maintained too; no repeated payload checks.
        resolution_book_at_head(journal, stream, activation, bodies=bodies)
        assert reads == [digest]
        bodies.store(original)
        restored, _ = resolution_book_at_head(journal, stream, activation, bodies=bodies)
        assert not restored.unavailable and reads == [digest, digest]
        assert restored.resolution_count(activation.contract_id) == 6
    _same_answers(restored, _replayed(activation, journal, bodies, stream, partition), activation)


def test_stale_disposition_head_is_a_conflict_but_bad_sequence_is_integrity_failure(tmp_path):
    from cruxible_client.contracts.errors import JournalConflictError
    from cruxible_core.procedures.resolution import resolution_book_at_head

    activation, journal, bodies, stream, partition, writer = _resolution_partition(tmp_path)
    first = _answer(activation, writer, stream, 1, overturn=False)
    _, before = resolution_book_at_head(journal, stream, activation, bodies=bodies)
    disposition = build_resolution_disposition(
        first,
        sequence=1,
        verdict="upheld",
        reviewer_actor_context=_actor("reviewer"),
        recorded_at=NOW + timedelta(seconds=11),
        note="Checked.",
    )
    append_resolution_disposition(
        writer,
        activation=activation,
        resolution=first,
        disposition=disposition,
        stream=stream,
        expected_head=before,
    )
    with pytest.raises(JournalConflictError):
        append_resolution_disposition(
            writer,
            activation=activation,
            resolution=first,
            disposition=disposition,
            stream=stream,
            expected_head=before,
        )
    with pytest.raises(ExecutionError, match="sequence is discontinuous"):
        append_resolution_disposition(
            writer,
            activation=activation,
            resolution=first,
            disposition=disposition.model_copy(update={"sequence": 4}),
            stream=stream,
            expected_head=journal.read_head(stream, partition),
        )
