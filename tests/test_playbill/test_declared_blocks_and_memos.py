"""Held lists, watched queries, block registration, and the two read-path memos.

Four laws meet in this file, and each of them exists because a projection block
is agent-authored prose held to an explicit list rather than a rendering of one
governed artifact.

A block HOLDS a list and, at most, WATCHES one query. The held list is what the
block is accountable for; the query only surfaces candidates for it. Those are
two different questions, and collapsing them -- either by letting a block watch
two queries with no rule for reconciling their answers, or by reporting a moved
query result as a stale member of the list -- tells an author to repair
something that is not broken.

A block is REGISTERED by the instance that governs its page, on both declaration
roads. The question `next` and `workspace detach` ask of a marker is whether this
instance stands behind it, and that was once answered by whether the block id
happened to begin `pub-` -- a spelling only the retired publication road minted.
An id an author chooses cannot be interrogated that way, so the answer has to be
the record the instance keeps.

Finally, both `orient` and `next` are READS, and neither may pay for the same
derivation twice. The registration fold parses every durable intent event and the
resolution fold evaluates every live Claim's verdict; at a few hundred Claims the
second crossed the client's own default timeout, so the first thing an SDK
session does could fail against a healthy instance.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_BACKINGS_PER_BLOCK,
    MAX_PROJECTION_STAMP_BYTES,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionQueryBackingV1,
    parse_projection_blocks,
    projection_parameter_digest,
    render_projection_closing,
    render_projection_opening,
)
from cruxible_core.errors import ConfigError
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.runtime import host_api
from cruxible_core.service import playbill_next as next_service
from cruxible_core.service import playbill_search as search_service
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    resolve_playbill_claim_group,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextItemV1,
    PlaybillNextRequestV1,
    service_playbill_next,
)
from cruxible_core.service.playbill_publications import (
    registered_projection_blocks,
    reset_bound_publication_registration_memo,
    service_declare_playbill_block,
    service_depublish_playbill_block,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts
from cruxible_core.service.playbill_search import (
    claim_resolution_statuses,
    reset_claim_resolution_memo,
)
from cruxible_core.service.playbill_verdict_memo import verdict_input_fingerprint
from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
from tests.test_playbill._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    activate,
    authoring,
    seed_claims,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_projection_next import (
    NOW,
    _claim_backing,
    _query_backing,
    _request,
)
from tests.test_playbill.test_query_execution_service import _instance_with_query
from tests.test_playbill.test_read_latency_memos import _declared_projection_observation

DECLARED_AT = "2026-09-04T12:00:00+00:00"
STALE_DIGEST = "sha256:" + "f" * 64


# --------------------------------------------------------------------------
# Shared worlds and small adapters over the builders these suites already own.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedWorld:
    """One instance whose watched query gained a row after the block was stamped."""

    instance: PlaybillInstance
    held: tuple[ProjectionClaimBackingV1, ...]
    stamped_query: ProjectionQueryBackingV1
    entered: str


@pytest.fixture(scope="module")
def watched_world(tmp_path_factory: pytest.TempPathFactory) -> WatchedWorld:
    """Stamp a block over the whole query result, then let one more row in.

    The stamp is taken BEFORE the third Claim is accepted, so the recorded
    semantic result digest is the one a real block would carry: the answer the
    query gave on the day the author wrote the prose.
    """

    instance, owner = _instance_with_query(tmp_path_factory.mktemp("watched-world"))
    held = _claim_backings(instance)
    stamped_query = _query_backing(instance)
    before = {backing.identity.qualified for backing in held}
    activate(
        instance,
        owner,
        service_propose_playbill_claim(
            instance,
            authoring=authoring("wi-44", "ready", with_claim_type=False),
            actor_id="owner",
            proposal_name="candidate-entering-the-watched-query",
            timestamp=TIMESTAMP,
        ),
    )
    after = {backing.identity.qualified for backing in _claim_backings(instance)}
    (entered,) = after - before
    return WatchedWorld(
        instance=instance,
        held=held,
        stamped_query=stamped_query,
        entered=entered,
    )


@pytest.fixture(scope="module")
def registration_world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    """One instance with accepted Claims, used only to read `next`'s marker rows."""

    instance, _owner = seed_claims(tmp_path_factory.mktemp("registration-world"))
    return instance


@pytest.fixture(scope="module")
def resolution_world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    """One instance whose live Claims are the resolution fold's input."""

    instance, _owner = seed_claims(tmp_path_factory.mktemp("resolution-world"))
    return instance


@pytest.fixture(autouse=True)
def _cold_memos() -> Iterator[None]:
    """Every law here is about a cold read or a deliberate second one."""

    reset_claim_resolution_memo()
    reset_bound_publication_registration_memo()
    yield
    reset_claim_resolution_memo()
    reset_bound_publication_registration_memo()


def _claim_backings(instance: PlaybillInstance) -> tuple[ProjectionClaimBackingV1, ...]:
    """A held-list entry for every accepted Claim, in the stamp's own order."""

    facts = build_accepted_query_facts(instance, coordinate=instance.accepted_coordinate())
    return tuple(
        sorted(
            (
                ProjectionClaimBackingV1(
                    identity=row.accepted.claim.identity,
                    statement_digest=row.accepted.statement_digest,
                )
                for row in facts.claims
            ),
            key=lambda item: item.identity.qualified.encode("utf-8"),
        )
    )


def _rows(
    instance: PlaybillInstance,
    request: PlaybillNextRequestV1,
    *,
    reason: str,
) -> tuple[PlaybillNextItemV1, ...]:
    return tuple(
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == reason
    )


def _stamp_of(request: PlaybillNextRequestV1) -> ProjectionBlockStampV1:
    observation = request.workspace_observation
    assert observation is not None
    assert observation.source_observations is not None
    return observation.source_observations[0].marker_summaries[0].stamp


def _with_block_id(request: PlaybillNextRequestV1, block_id: str) -> PlaybillNextRequestV1:
    """Re-address the observed marker without rebuilding the observation."""

    observation = request.workspace_observation
    assert observation is not None
    assert observation.source_observations is not None
    source = observation.source_observations[0]
    marker = source.marker_summaries[0]
    return request.model_copy(
        update={
            "workspace_observation": observation.model_copy(
                update={
                    "source_observations": (
                        source.model_copy(
                            update={
                                "marker_summaries": (
                                    marker.model_copy(
                                        update={
                                            "stamp": marker.stamp.model_copy(
                                                update={"block_id": block_id}
                                            )
                                        }
                                    ),
                                )
                            }
                        ),
                    )
                }
            )
        }
    )


def _declare(instance: PlaybillInstance, stamp: ProjectionBlockStampV1) -> None:
    service_declare_playbill_block(
        instance,
        actor_id="owner",
        stamp=stamp,
        declared_at=DECLARED_AT,
    )


def _depublish(instance: PlaybillInstance, *, source_id: str, block_id: str) -> Any:
    return service_depublish_playbill_block(
        instance,
        coordinator=AuthoringIntentCoordinator.for_instance(instance),
        actor=AuthenticatedActor(actor_id="owner"),
        source_id=source_id,
        block_id=block_id,
    )


def _synthetic_claim_backings(count: int) -> tuple[ProjectionClaimBackingV1, ...]:
    """A held list of the declared size, with no ledger behind it.

    The ceiling is a property of the marker, not of accepted state: it decides
    how many rows one governed table may carry before the author has to cut it
    somewhere a reader cannot explain.
    """

    return tuple(
        ProjectionClaimBackingV1(
            identity=ArtifactIdentity(kind="Claim", name=f"CLM-{index:028d}"),
            statement_digest="sha256:" + f"{index:064x}",
        )
        for index in range(count)
    )


def _synthetic_query_backing(name: str) -> ProjectionQueryBackingV1:
    return ProjectionQueryBackingV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        definition_digest="sha256:" + "1" * 64,
        canonical_param_digest=projection_parameter_digest(()),
        declared_evaluation_time=datetime(2026, 9, 4, tzinfo=UTC),
        semantic_result_digest="sha256:" + "2" * 64,
    )


# --------------------------------------------------------------------------
# A block holds a list and watches at most one query.
# --------------------------------------------------------------------------


def test_a_block_holds_a_list_and_watches_one_query_through_a_render_and_a_read(
    watched_world: WatchedWorld,
) -> None:
    """A held list is the point of a projection block, so every member must survive.

    A block that holds three governed rows and watches the query they came from
    carries four backings in one marker, and both halves of the round trip have
    to keep all four: the base64 opening the author's page actually carries, and
    the read that decides whether the page is still true. A marker that renders
    but re-parses to something smaller would silently drop rows from the list the
    block is accountable for, and a read that walks only the first backing would
    let every later row go stale unreported -- which is the failure a per-block
    held list exists to make impossible.
    """

    instance = watched_world.instance
    held = _claim_backings(instance)
    assert len(held) == 3
    backing: tuple[ProjectionBackingV1, ...] = (*held, _query_backing(instance))

    request = _request(instance, backing=backing)
    stamp = _stamp_of(request)
    assert len(stamp.backing) == 4

    document = (
        render_projection_opening(stamp)
        + b"status: ready\n"
        + render_projection_closing(stamp.block_id)
    )
    (parsed,) = parse_projection_blocks(document, source_id=stamp.source_id)
    assert parsed.stamp == stamp

    # Nothing moved, so a walk of all four backings reports nothing.
    assert _rows(instance, request, reason="projection_backing_stale") == ()

    # Move the LAST member of the held list. Only a read that walks past the
    # first backing can see it, and it must name that member and no other.
    moved = held[-1].model_copy(update={"statement_digest": STALE_DIGEST})
    (row,) = _rows(
        instance,
        _request(instance, backing=(*held[:-1], moved, _query_backing(instance))),
        reason="projection_backing_stale",
    )
    assert row.related_identities == (moved.identity.qualified,)
    assert row.detail["stale_backings"] == [moved.identity.qualified]  # type: ignore[index]


def test_a_block_refuses_a_second_watched_query_at_the_model(
    watched_world: WatchedWorld,
) -> None:
    """Two watched queries are two answers to "what belongs here" with no tiebreak.

    A watched query says which governed rows are candidates for this block. One
    query is an opinion the author can act on; two are a contradiction with no
    rule for reconciling them, and every consumer downstream -- the candidate
    delta, the repin that answers it, the prose the author writes against it --
    would have to invent one. The refusal is at the model so no stamp carrying
    the contradiction can ever be written to a page.
    """

    coordinate = AcceptedCoordinate.from_internal(watched_world.instance.accepted_coordinate())
    one = _synthetic_query_backing("project.first_items")
    other = _synthetic_query_backing("project.second_items")

    with pytest.raises(ValidationError, match="watches at most one query"):
        ProjectionBlockStampV1(
            source_id="corpus.runbook",
            block_id="held-rows",
            declared_generation=0,
            declared_coordinate=coordinate,
            backing=(one, other),
            body_digest="sha256:" + "b" * 64,
        )

    # One query alongside a held list is the shape the block is FOR.
    assert (
        len(
            ProjectionBlockStampV1(
                source_id="corpus.runbook",
                block_id="held-rows",
                declared_generation=0,
                declared_coordinate=coordinate,
                backing=(*_synthetic_claim_backings(3), one),
                body_digest="sha256:" + "b" * 64,
            ).backing
        )
        == 4
    )


def test_a_held_list_is_sized_for_a_real_table_and_still_bounded(
    watched_world: WatchedWorld,
) -> None:
    """The ceiling must be reached by writing a page, not by counting Claims.

    The old pair -- sixty-four backings inside sixteen kibibytes -- made the
    limit a LAYOUT constraint: a governed table of sixty-six rows had to be split
    at a row number that means nothing to a reader, and an author discovered the
    ceiling by counting members rather than by writing prose. A block sized for a
    real table has to hold five hundred and twelve rows AND still fit the stamp
    ceiling, or the two limits would contradict each other and the larger one
    would be a promise the marker cannot keep. It stays a ceiling: one more row
    is refused, so no marker grows without bound.
    """

    coordinate = AcceptedCoordinate.from_internal(watched_world.instance.accepted_coordinate())

    def stamp(count: int) -> ProjectionBlockStampV1:
        return ProjectionBlockStampV1(
            source_id="corpus.runbook",
            block_id="held-rows",
            declared_generation=0,
            declared_coordinate=coordinate,
            backing=_synthetic_claim_backings(count),
            body_digest="sha256:" + "b" * 64,
        )

    full = stamp(MAX_PROJECTION_BACKINGS_PER_BLOCK)
    assert len(full.backing) == 512
    # The ceiling governs the stamp's own bytes, which is what the opening
    # carries base64-encoded; a full held list must fit inside it with room to
    # spare, or the two limits could not both be honoured.
    content = canonical_bytes(full.model_dump(mode="json"))
    assert len(content) < MAX_PROJECTION_STAMP_BYTES
    document = (
        render_projection_opening(full)
        + b"status: ready\n"
        + render_projection_closing(full.block_id)
    )
    (parsed,) = parse_projection_blocks(document, source_id=full.source_id)
    assert parsed.stamp == full

    with pytest.raises(ValidationError, match="at most 512 items"):
        stamp(MAX_PROJECTION_BACKINGS_PER_BLOCK + 1)


# --------------------------------------------------------------------------
# A moved watched query is a candidate delta, never a stale member.
# --------------------------------------------------------------------------


def test_a_row_entering_a_watched_query_is_reported_as_a_candidate_not_a_stale_backing(
    watched_world: WatchedWorld,
) -> None:
    """A query result that moved is news about the world, not damage to the block.

    A watched query is not a member of the held list: it surfaces candidates for
    it. Reporting its movement as a stale backing said the block had fallen out
    of date with something it holds, and named a repair -- re-read the prose
    against the new state and re-stamp -- for prose that may be exactly right.
    What actually happened is that a governed row the author has never ruled on
    now qualifies, so the row has to name that Claim and ask for a decision:
    hold it, or decline it on the record. The delta is computed against the list
    the block HOLDS, because that is the only thing an author can act on.
    """

    instance = watched_world.instance
    request = _request(
        instance,
        backing=(*watched_world.held, watched_world.stamped_query),
    )

    (row,) = _rows(instance, request, reason="projection_candidates_changed")
    assert row.severity == "warning"
    assert row.subject_identity == "corpus.runbook#status"
    assert row.related_identities == (watched_world.entered,)
    assert row.detail["watched_query"] == f"QueryDefinition:{QUERY_NAME}"  # type: ignore[index]
    assert row.detail["entered"] == [watched_world.entered]  # type: ignore[index]
    assert row.detail["left"] == []  # type: ignore[index]
    assert row.repair.operation == "playbill.block.repin"
    assert row.repair.required_change == "hold_or_decline_the_entered_candidates"
    assert row.repair.arguments == {
        "source_id": "corpus.runbook",
        "block_id": "status",
        "claim": [watched_world.entered],
    }

    # The query is not a member of the held list, so nothing about it is stale.
    assert _rows(instance, request, reason="projection_backing_stale") == ()


def test_restamping_the_watched_query_clears_the_row_without_holding_the_candidate(
    watched_world: WatchedWorld,
) -> None:
    """Declining a candidate is a decision the stamp records, not an item to ignore.

    The row is a function of the digest the stamp COMMITS, never of the list the
    block holds, and that is what makes "no" expressible. An author who reads the
    entered row and judges it out of scope re-stamps the block without holding
    it: the marker then carries the query's current answer, the row is gone, and
    the record says this author saw that candidate and declined it. Were the row
    instead a function of the held list, the only way to silence it would be to
    hold every row the query ever returns, and a considered "no" would be
    indistinguishable from an unread queue.
    """

    instance = watched_world.instance
    declined = _request(
        instance,
        # The same held list as before -- the entered candidate is NOT held --
        # with the watched query re-stamped at its current answer.
        backing=(*watched_world.held, _query_backing(instance)),
    )

    assert _rows(instance, declined, reason="projection_candidates_changed") == ()
    assert _rows(instance, declined, reason="projection_backing_stale") == ()


def test_a_held_backing_that_moved_is_still_a_stale_backing(
    watched_world: WatchedWorld,
) -> None:
    """The two reasons answer two different questions and must not collapse into one.

    A member of the held list whose statement moved IS damage to the block: the
    prose was written against a statement the world no longer makes, so it has to
    be re-read and re-stamped. That is a different fact from a candidate arriving
    at the door, and it earns a different repair. Had the candidate delta
    swallowed the stale-member row -- or the reverse -- an author would be told to
    rule on new rows when the rows already in the block had silently changed
    underneath the prose.
    """

    instance = watched_world.instance
    moved = _claim_backing(instance, stale=True)
    request = _request(instance, backing=(moved, _query_backing(instance)))

    (row,) = _rows(instance, request, reason="projection_backing_stale")
    assert row.severity == "repair"
    assert row.related_identities == (moved.identity.qualified,)
    assert row.repair.operation == "playbill.block.repin"
    assert row.repair.required_change == "review_block_supersede_prose_then_repin"
    assert _rows(instance, request, reason="projection_candidates_changed") == ()


# --------------------------------------------------------------------------
# Both declaration roads register a block, keyed on the pair the page names.
# --------------------------------------------------------------------------


def test_a_repin_declared_block_is_registered_under_the_authors_own_id(
    registration_world: PlaybillInstance,
) -> None:
    """An id an author chooses cannot be recognised by spelling, so it must be recorded.

    A block minted by the retired publication road carried `pub-` in its id and
    could be identified by the string alone. A block an agent declares picks its
    own id, so the only honest answer to "does this instance stand behind this
    marker?" is the record the instance keeps. Without one, every declared block
    read as an intruder in its own page: `next` reported an unregistered marker
    the author had just been told to write, and the fold that guards a workspace
    detachment could not see it at all.
    """

    instance = registration_world
    request = _with_block_id(
        _request(instance, backing=(_claim_backing(instance),)),
        "held-rows",
    )
    stamp = _stamp_of(request)
    assert not stamp.block_id.startswith("pub-")

    # Before the declaration the instance has never heard of the marker.
    assert len(_rows(instance, request, reason="unregistered_projection_block")) == 1

    _declare(instance, stamp)

    folded = registered_projection_blocks(instance)
    assert folded is not None
    registration = folded[("corpus.runbook", "held-rows")]
    assert registration.origin == "declaration"
    assert registration.declaration is not None
    assert registration.declaration.declared_by == "owner"
    assert registration.publication is None
    assert _rows(instance, request, reason="unregistered_projection_block") == ()


def test_an_unregistered_marker_is_reported_whatever_its_id_spells(
    registration_world: PlaybillInstance,
) -> None:
    """A marker no record sanctions is unregistered, and its spelling is not evidence.

    The question was asked only of ids beginning `pub-`, so a marker under any
    other id was never checked against anything: a page could carry a governed
    block no instance had ever registered and `next` would say nothing. The
    prefix was never a fact about sanction -- it was a fact about which road
    minted the block -- so both spellings are asked the same question here, and
    both answer it from the fold rather than from the string.
    """

    instance = registration_world
    base = _request(instance, backing=(_claim_backing(instance),))

    for block_id in ("unheld-rows", "pub-status"):
        request = _with_block_id(base, block_id)
        (row,) = _rows(instance, request, reason="unregistered_projection_block")
        assert row.severity == "warning"
        assert row.subject_identity == f"corpus.runbook#{block_id}"
        assert row.detail == {"source_id": "corpus.runbook", "block_id": block_id}
        assert row.repair.operation == "playbill.block.repin"
        assert row.repair.required_change == "remove_or_register_projection_block"


def test_depublishing_a_declared_block_releases_it_from_the_fold(tmp_path: Path) -> None:
    """A registration with no way out is a demand the author cannot satisfy.

    A registration outlived every ruling that could remove the block it names:
    `next` went on demanding the frame for a marker a later decision had told the
    author to delete, and the only repair it could name was to put it back. One
    verb answers both roads because the page names a source and a block and knows
    nothing about which road registered it -- and a declared block has no intent
    to abandon, so releasing it is forgetting the declaration.
    """

    instance, _owner = initialize_local(tmp_path)
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="held-rows",
        declared_generation=0,
        declared_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        backing=_synthetic_claim_backings(2),
        body_digest="sha256:" + "b" * 64,
    )
    _declare(instance, stamp)
    folded = registered_projection_blocks(instance)
    assert folded is not None and ("corpus.runbook", "held-rows") in folded

    result = _depublish(instance, source_id="corpus.runbook", block_id="held-rows")
    assert result.origin == "declaration"
    assert result.outcome == "depublished"
    # A declaration has no publishing Claim, no intent and no expectation, and
    # the result must not invent any of the three.
    assert result.intent_id is None
    assert result.expectation_id is None
    assert result.claim_identity is None

    released = registered_projection_blocks(instance)
    assert released == {}


def test_a_detach_refuses_while_a_repin_declared_block_is_registered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detaching under a live registration strands markers with no host to repair them.

    The instance is never told the workspace left, so a detachment that goes
    through under a live registration leaves a page carrying governed markers no
    host owns -- the one state with no repair available from inside the
    workspace. The guard keyed on the `pub-` spelling, so a block an agent had
    declared was invisible to it, even though such a block is stranded exactly as
    badly. The refusal names the pair and the verb that releases it, and it stops
    the moment the instance no longer stands behind the marker.
    """

    instance, _owner = initialize_local(tmp_path)
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="held-rows",
        declared_generation=0,
        declared_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        backing=_synthetic_claim_backings(1),
        body_digest="sha256:" + "b" * 64,
    )
    _declare(instance, stamp)

    class _Manager:
        def get(self, _instance_id: str) -> PlaybillInstance:
            return instance

    monkeypatch.setattr(host_api, "get_playbill_manager", lambda: _Manager())

    with pytest.raises(ConfigError) as refusal:
        host_api._refuse_detach_with_registered_blocks("inst_declared_blocks")
    message = str(refusal.value)
    assert "corpus.runbook#held-rows" in message
    assert "playbill block depublish" in message

    _depublish(instance, source_id="corpus.runbook", block_id="held-rows")

    # Released means released: the detachment now strands nothing.
    host_api._refuse_detach_with_registered_blocks("inst_declared_blocks")


# --------------------------------------------------------------------------
# One registration fold per `next`.
# --------------------------------------------------------------------------


def test_one_next_folds_the_durable_registration_stream_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that folds the whole durable stream per block does not scale with a page.

    The registration fold reads and parses every durable intent event, which on a
    worked instance is a large stream. One `next` used to reach it from three
    places and once more for every syncable block, so the cost of asking "which
    blocks does this instance register?" grew with the size of the page being
    asked about -- and the retirement release, which needs the same answer,
    folded it yet again. One fold answers all of them: the queue folds once and
    hands the result to every reader that needs it.
    """

    instance, _owner = seed_claims(tmp_path)
    (instance.root / instance.descriptor.storage.exhaust / "authoring-intents").mkdir(
        mode=0o700, parents=True, exist_ok=True
    )
    reset_bound_publication_registration_memo()

    observation = _declared_projection_observation(instance)
    assert observation.source_observations is not None
    source = observation.source_observations[0]
    first = source.marker_summaries[0]
    second = first.model_copy(
        update={
            "stamp": first.stamp.model_copy(update={"block_id": "second"}),
            "start_byte": 200,
            "end_byte": 300,
        }
    )
    observation = observation.model_copy(
        update={
            "source_observations": (
                source.model_copy(update={"marker_summaries": (second, first)}),
            )
        }
    )

    folds = 0
    original_events = AuthoringIntentStore.events

    def counting_events(self: AuthoringIntentStore) -> Any:
        nonlocal folds
        folds += 1
        return original_events(self)

    reads = 0
    original_fold = next_service._registered_publication_blocks

    def counting_fold(instance_: PlaybillInstance) -> Any:
        nonlocal reads
        reads += 1
        return original_fold(instance_)

    monkeypatch.setattr(AuthoringIntentStore, "events", counting_events)
    monkeypatch.setattr(next_service, "_registered_publication_blocks", counting_fold)

    service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=NOW,
            access_profile=_request(instance, backing=(_claim_backing(instance),)).access_profile,
            workspace_observation=observation,
        ),
    )

    # Two observed blocks, one fold: the answer does not get re-derived per
    # block, and the retirement release reads the folded result.
    assert reads == 1
    assert folds == 1


# --------------------------------------------------------------------------
# The per-process resolution memo.
# --------------------------------------------------------------------------


def _resolution_inputs(
    instance: PlaybillInstance,
    *,
    at: AcceptedCoordinate | None = None,
) -> tuple[Any, ...]:
    coordinate = at or AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    listed = service_list_playbill_claims(instance, at=coordinate, include_retired=True)
    return tuple(_claim_from_view(view) for view in listed.claims)


def _count_group_resolutions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the real per-slot verdict work the resolution fold does.

    The group resolution is where a Claim's verdict is actually derived, so it is
    the only honest measure of whether a second read paid again.
    """

    counted = [0]

    def counting(*args: Any, **values: Any) -> Any:
        counted[0] += 1
        return resolve_playbill_claim_group(*args, **values)

    monkeypatch.setattr(search_service, "resolve_playbill_claim_group", counting)
    return counted


def test_a_second_resolution_read_of_the_same_state_evaluates_no_verdicts(
    resolution_world: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `orient` is a read, and a read must not pay twice for the same derivation.

    Deriving every live Claim's verdict and resolution status is the first thing
    both `orient` and `next` do, and at a few hundred Claims it crossed the
    client's own default timeout -- so the opening move of an SDK session could
    fail against a perfectly healthy instance. The derivation is a pure function
    of things that cannot move between two reads at one coordinate, one instant
    and one Claim set, so the second read is entitled to evaluate nothing at all.
    It must still answer identically: a memo that changed the answer would be
    accepted state by the back door.
    """

    instance = resolution_world
    reset_claim_resolution_memo()
    claims = _resolution_inputs(instance)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    counted = _count_group_resolutions(monkeypatch)

    first = claim_resolution_statuses(
        instance,
        claims=claims,
        at=coordinate,
        evaluation_time=NOW,
    )
    assert counted[0] > 0
    evaluated = counted[0]

    verdicts: dict[str, Any] = {}
    second = claim_resolution_statuses(
        instance,
        claims=claims,
        at=coordinate,
        evaluation_time=NOW,
        verdicts_by_identity=verdicts,
    )

    assert counted[0] == evaluated
    assert second == first
    # The verdicts the first read derived come back with the statuses, so the
    # folds that share them are not silently handed an empty map.
    assert set(verdicts) == {f"Claim:{name}" for name in first}


def test_a_moved_verdict_input_re_evaluates_instead_of_serving_the_memo(
    resolution_world: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict reads more than the accepted tree, so the key must name all of it.

    Whether a Claim's capture can be replayed now depends on the content-address
    store, and whether a principal has attested to it depends on the attestation
    ledger. Neither is part of the accepted coordinate, so a memo keyed on the
    coordinate alone would keep serving a verdict derived before an object
    landed. The key carries a fingerprint of both stores; this proves the
    fingerprint actually moves when one of them does, and that the moved
    fingerprint is what sends the read back to the derivation.
    """

    instance = resolution_world
    reset_claim_resolution_memo()
    claims = _resolution_inputs(instance)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    counted = _count_group_resolutions(monkeypatch)

    before = verdict_input_fingerprint(instance)
    claim_resolution_statuses(instance, claims=claims, at=coordinate, evaluation_time=NOW)
    evaluated = counted[0]
    assert evaluated > 0
    claim_resolution_statuses(instance, claims=claims, at=coordinate, evaluation_time=NOW)
    assert counted[0] == evaluated

    instance.body_store().store(b"a body no verdict has seen yet\n")
    after = verdict_input_fingerprint(instance)
    assert after != before

    claim_resolution_statuses(instance, claims=claims, at=coordinate, evaluation_time=NOW)
    assert counted[0] > evaluated


def test_two_accepted_coordinates_do_not_share_a_resolution_entry(
    watched_world: WatchedWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolution is an answer AT a coordinate, so two coordinates are two answers.

    Which Claim holds a slot is decided by everything accepted at the coordinate
    being read: a generation that admits a rival, retires a contender or migrates
    a ClaimType can change the answer for Claims whose own artifacts never moved.
    A memo that keyed on the Claim set alone would serve one generation's verdict
    at another's coordinate, which is the single failure that would make this
    cache into a lie rather than an optimization.
    """

    instance = watched_world.instance
    history = instance.accepted_history()
    assert len(history) >= 2
    earlier = AcceptedCoordinate.from_internal(instance.coordinate_for_oid(history[-2].oid))
    latest = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    assert earlier != latest

    reset_claim_resolution_memo()
    # Only the Claims both generations hold, so the two reads differ in exactly
    # one input: the coordinate.
    claims = _resolution_inputs(instance, at=earlier)
    counted = _count_group_resolutions(monkeypatch)

    claim_resolution_statuses(instance, claims=claims, at=earlier, evaluation_time=NOW)
    evaluated = counted[0]
    assert evaluated > 0
    claim_resolution_statuses(instance, claims=claims, at=earlier, evaluation_time=NOW)
    assert counted[0] == evaluated

    claim_resolution_statuses(instance, claims=claims, at=latest, evaluation_time=NOW)
    assert counted[0] > evaluated
    latest_evaluated = counted[0]

    # And the earlier coordinate keeps its own entry rather than being displaced.
    claim_resolution_statuses(instance, claims=claims, at=earlier, evaluation_time=NOW)
    assert counted[0] == latest_evaluated
