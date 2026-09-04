"""Client-owned block sync reports drift and preserves exact file boundaries.

A projection block is prose an agent wrote, held to an explicit list of accepted
Claims and artifacts. Nothing renders it, so this verb converges nothing: every
stamped block is reported `unchanged`, `stale` when a held member moved under
it, or `dirty` when the prose moved away from the stamp, and the one edit left
is `--detach`. These tests pin the reporting, the selection and the safety laws
that survived that change, and prove the page bytes for the cases that used to
be rewritten.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1,
    PlaybillBlockSyncReadResultV1,
    PlaybillBlockSyncSuccessorCandidateV1,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionArtifactBackingV1,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    frame_projection_block,
    parse_projection_blocks,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperationV1

INSTANCE_ID = "inst_block_sync"
OLD_BODY = b"status: old\n"
EDITED_BODY = b"status: hand edited\n"
MOVED_DIGEST = "sha256:" + "9" * 64
OLD_COORDINATE = AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
NEW_COORDINATE = AcceptedCoordinate(
    git_oid="5" * 64,
    semantic_root="sha256:" + "6" * 64,
    generation_root="sha256:" + "7" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _claim_backing(letter: str) -> ProjectionClaimBackingV1:
    return ProjectionClaimBackingV1(
        identity=ArtifactIdentity(kind="Claim", name="CLM-" + letter * 32),
        statement_digest="sha256:" + "8" * 64,
    )


def _stamp(*, body: bytes = OLD_BODY) -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="pub-example",
        declared_generation=1,
        declared_coordinate=OLD_COORDINATE,
        backing=(_claim_backing("a"),),
        body_digest=_digest(body),
    )


def _three_claim_stamp(*, body: bytes = OLD_BODY) -> ProjectionBlockStampV1:
    """A stamp holding three Claims, which the model has always permitted."""

    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="pub-example",
        declared_generation=1,
        declared_coordinate=OLD_COORDINATE,
        backing=(_claim_backing("a"), _claim_backing("b"), _claim_backing("c")),
        body_digest=_digest(body),
    )


def _artifact_stamp(*, body: bytes = OLD_BODY) -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="vocabulary",
        declared_generation=1,
        declared_coordinate=OLD_COORDINATE,
        backing=(
            ProjectionArtifactBackingV1(
                identity=ArtifactIdentity(kind="ClaimType", name="sec.vuln.severity"),
                artifact_digest="sha256:" + "7" * 64,
            ),
        ),
        body_digest=_digest(body),
    )


def _workspace(
    root: Path,
    *,
    instance_id: str = INSTANCE_ID,
    stamp: ProjectionBlockStampV1 | None = None,
) -> Path:
    playbill = root / ".playbill"
    playbill.mkdir()
    (playbill / "coverage.json").write_text(
        "{"
        '"tag":"playbill-coverage-workspace-config-v2",'
        '"server_url":"https://sync.example.test",'
        f'"instance_id":"{instance_id}"'
        "}",
        encoding="utf-8",
    )
    (playbill / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: corpus.runbook
    locator: corpus/runbook.md
    document_id: runbook
    document_kind: runbook
    title: Runbook
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:runbook]
""",
        encoding="utf-8",
    )
    source = root / "corpus" / "runbook.md"
    source.parent.mkdir()
    source.write_bytes(
        b"PREFIX\n"
        + frame_projection_block(stamp=_stamp() if stamp is None else stamp, body=OLD_BODY)
        + b"SUFFIX\n"
    )
    return source


def _moved(backing: ProjectionBackingV1) -> ProjectionBackingV1:
    """The current spelling of a held backing that moved under the stamp.

    A successor verdict names what the backing reads as NOW, not a body: the
    author's prose is never touched, so the only thing the daemon can hand back
    about a member that moved is its new identity-and-digest spelling.
    """

    if isinstance(backing, ProjectionClaimBackingV1):
        return backing.model_copy(update={"statement_digest": MOVED_DIGEST})
    assert isinstance(backing, ProjectionArtifactBackingV1)
    return backing.model_copy(update={"artifact_digest": MOVED_DIGEST})


class _SyncClient:
    """A daemon that answers the one question a block sync still asks.

    The read renders no body at all, so this stub carries none. It either
    agrees that every held backing still reads as the stamp declares
    (`current`), names the members that moved under it (`successor`), or
    refuses with a typed reason. `moved` says how many of the held backings a
    successor verdict reports as having moved, because a block holds a list and
    naming one of them would not tell an author what to repin.
    """

    def __init__(
        self,
        *,
        status: str = "current",
        refusal: str | None = None,
        moved: int = 1,
    ) -> None:
        self.requests: list[PlaybillBlockSyncReadRequestV1] = []
        self.declared: list[dict[str, object]] = []
        self.status = status
        self.refusal = refusal
        self.moved = moved

    def declare_playbill_block(
        self,
        _instance_id: str,
        stamp: dict[str, object],
    ) -> object:
        """Record the declaration a repin makes after it writes the marker."""

        self.declared.append(stamp)
        return SimpleNamespace(
            source_id=stamp["source_id"],
            block_id=stamp["block_id"],
            outcome="declared",
        )

    def read_playbill_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: PlaybillBlockSyncReadRequestV1,
    ) -> PlaybillBlockSyncReadResultV1:
        assert instance_id == INSTANCE_ID
        self.requests.append(request)
        if self.refusal is not None:
            candidates = (
                (
                    PlaybillBlockSyncSuccessorCandidateV1(
                        identity=request.stamp.backing[0].identity,
                        artifact_digest="sha256:" + "a" * 64,
                        coordinate=NEW_COORDINATE,
                        generation=2,
                    ),
                    PlaybillBlockSyncSuccessorCandidateV1(
                        identity=request.stamp.backing[0].identity,
                        artifact_digest="sha256:" + "b" * 64,
                        coordinate=NEW_COORDINATE,
                        generation=3,
                    ),
                )
                if self.refusal == "block_successor_ambiguous"
                else ()
            )
            return PlaybillBlockSyncReadResultV1(
                status="refused",
                original_artifact_digest=_digest(b"old-artifact"),
                reason=self.refusal,  # type: ignore[arg-type]
                detail="ruled refusal",
                successor_candidates=candidates,
            )
        held = request.stamp.backing
        # `backing` is the current spelling of THE held backing, so it is only
        # meaningful when the stamp holds exactly one; that single-backing case
        # is what `block repin --backing DIGEST` reads back.
        single = held[0] if len(held) == 1 else None
        if self.status == "current":
            return PlaybillBlockSyncReadResultV1(
                status="current",
                original_artifact_digest=_digest(b"old-artifact"),
                coordinate=NEW_COORDINATE,
                generation=2,
                backing=single,
            )
        return PlaybillBlockSyncReadResultV1(
            status="successor",
            original_artifact_digest=_digest(b"old-artifact"),
            artifact_digest=_digest(b"new-artifact"),
            coordinate=NEW_COORDINATE,
            generation=2,
            backing=None if single is None else _moved(single),
            moved_backings=tuple(_moved(item) for item in held[: self.moved]),
        )


def test_a_moved_backing_is_reported_stale_and_the_page_is_never_touched(
    tmp_path: Path,
) -> None:
    """Converted from the convergence law: this verb used to rewrite the body.

    A single-Claim block was rewritten to the accepted body it had been
    published from. Nothing renders a projection block now -- the prose is the
    agent's, held to a list -- so a member that moved is a finding about the
    page, not an edit to it: one `stale` row, one typed reason, one repin that
    re-declares the list the block still means. The page must come out of the
    call byte for byte as it went in, which is why the comparison is explicit,
    and reporting is idempotent by construction, so a second call says the same
    thing about the same unmodified bytes.
    """

    source = _workspace(tmp_path)
    source.chmod(0o640)
    before = source.read_bytes()
    client = _SyncClient(status="successor")

    result = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )

    (item,) = result.items
    assert item.outcome == "stale"
    assert item.reason == "block_backing_changed"
    assert item.repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={"source_id": "corpus.runbook", "block_id": "pub-example"},
    )
    assert item.detail["moved_backings"] == ["Claim:CLM-" + "a" * 32]
    assert item.detail["backing_count"] == 1
    # A stale block changes no file, and it is still a finding an activation's
    # closing sweep must not exit clean over.
    assert result.would_change is False
    assert result.changed_file_count == 0
    assert result.has_refusals is True
    assert source.read_bytes() == before
    assert source.stat().st_mode & 0o777 == 0o640

    second = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )
    assert [row.outcome for row in second.items] == ["stale"]
    assert source.read_bytes() == before


def test_a_moved_claim_type_backing_is_stale_like_any_other_held_member(
    tmp_path: Path,
) -> None:
    """Converted: a ClaimType card body used to be rendered into the block.

    The old law read the governed card and wrote its body between the markers.
    What survives is that a pinned vocabulary artifact is a held member exactly
    like a Claim: when the card moves the block is stale and the row names it,
    and the prose the agent wrote about that vocabulary stays as the agent
    wrote it, because no card body is an accepted body for this block.
    """

    source = _workspace(tmp_path, stamp=_artifact_stamp())
    before = source.read_bytes()

    result = sync_projection_blocks(
        _SyncClient(status="successor"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )

    (item,) = result.items
    assert item.outcome == "stale"
    assert item.reason == "block_backing_changed"
    assert item.detail["moved_backings"] == ["ClaimType:sec.vuln.severity"]
    assert item.repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={"source_id": "corpus.runbook", "block_id": "vocabulary"},
    )
    assert source.read_bytes() == before


def test_a_stamp_holding_three_claim_backings_reports_one_outcome_for_the_block(
    tmp_path: Path,
) -> None:
    """The one-backing gate is gone: a held list of any size is ordinary.

    A block used to be `unsyncable` unless it held exactly one Claim or
    artifact backing, because the sync had to choose a body to render and could
    not choose between three. Nothing renders, so a list of three is no harder
    to answer than a list of one: the block gets ONE row for the whole block,
    reporting what its declaration says, rather than a refusal for the shape of
    that declaration.
    """

    source = _workspace(tmp_path, stamp=_three_claim_stamp())
    before = source.read_bytes()
    client = _SyncClient()

    result = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )

    (item,) = result.items
    assert item.outcome == "unchanged"
    assert item.reason is None
    assert item.detail["backing_count"] == 3
    assert item.detail["coordinate_git_oid"] == NEW_COORDINATE.git_oid
    assert result.has_refusals is False
    assert result.would_change is False
    # One read for the block, not one per held member: the currency verdict is
    # taken over the whole list at one coordinate.
    assert len(client.requests) == 1
    assert len(client.requests[0].stamp.backing) == 3
    assert source.read_bytes() == before


def test_a_stale_row_names_every_held_member_that_moved(tmp_path: Path) -> None:
    """A block holds a list, so naming one moved member would not repair it.

    Two of three Claims moved under this stamp. The author has to re-declare
    what the block still means, and can only do that knowing which members
    drifted, so the row carries every one of them -- and still exactly one
    outcome and one repair for the block.
    """

    source = _workspace(tmp_path, stamp=_three_claim_stamp())
    before = source.read_bytes()

    result = sync_projection_blocks(
        _SyncClient(status="successor", moved=2),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("corpus/runbook.md",),
    )

    (item,) = result.items
    assert item.outcome == "stale"
    assert item.detail["moved_backings"] == [
        "Claim:CLM-" + "a" * 32,
        "Claim:CLM-" + "b" * 32,
    ]
    assert item.detail["backing_count"] == 3
    assert source.read_bytes() == before


def test_detach_is_atomic_per_file_and_preserves_outside_bytes_and_mode(
    tmp_path: Path,
) -> None:
    """The atomic-per-file write law, on the one write this verb still makes.

    Detaching strips a marker pair and keeps the prose between it. That is a
    whole-file replacement, so it must land as one atomic swap that preserves
    the file's mode and every byte outside the markers it removed -- the same
    guarantees the convergence write used to carry, now carried by the only
    write left. Detaching twice is a no-op because the page no longer declares
    a block at all.
    """

    source = _workspace(tmp_path)
    source.chmod(0o640)
    before = source.read_bytes()
    (block,) = parse_projection_blocks(before, source_id="corpus.runbook")
    body = before[block.body_start : block.body_end]
    client = _SyncClient(refusal="block_backing_retired")

    first = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )

    assert [item.outcome for item in first.items] == ["detached"]
    assert first.changed_file_count == 1
    assert (
        first.items[0].detail["outside_digest_before"]
        == first.items[0].detail["outside_digest_after"]
    )
    assert source.read_bytes() == b"PREFIX\n" + body + b"SUFFIX\n"
    assert source.stat().st_mode & 0o777 == 0o640

    second = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )
    assert second.items == ()
    assert second.changed_file_count == 0
    assert source.read_bytes() == b"PREFIX\n" + body + b"SUFFIX\n"


def test_check_is_the_default_and_changes_only_detach(tmp_path: Path) -> None:
    """Converted: `--check` used to separate reporting from writing.

    An ordinary sync writes nothing at all now, so the flag cannot change what
    it does: with and without it the rows and the page bytes are identical, and
    that identity is the whole claim. What `--check` still separates is
    `would_detach` from `detached`, because detaching is the one edit left.
    """

    source = _workspace(tmp_path)
    before = source.read_bytes()

    checked = sync_projection_blocks(
        _SyncClient(status="successor"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
        check=True,
    )
    unchecked = sync_projection_blocks(
        _SyncClient(status="successor"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert [item.outcome for item in checked.items] == ["stale"]
    assert checked.model_dump(mode="json") == unchecked.model_dump(mode="json")
    assert checked.would_change is False
    assert checked.changed_file_count == 0
    assert source.read_bytes() == before

    retired = _SyncClient(refusal="block_backing_retired")
    dry = sync_projection_blocks(
        retired,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
        check=True,
    )
    assert [item.outcome for item in dry.items] == ["would_detach"]
    assert dry.would_change is True
    assert dry.changed_file_count == 0
    assert source.read_bytes() == before

    applied = sync_projection_blocks(
        retired,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )
    assert [item.outcome for item in applied.items] == ["detached"]
    assert applied.changed_file_count == 1
    assert source.read_bytes() != before


@pytest.mark.parametrize("all_sources", [False, True])
def test_source_catalog_is_optional_for_stamped_marker_discovery(
    tmp_path: Path,
    all_sources: bool,
) -> None:
    source = _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()
    before = source.read_bytes()
    client = _SyncClient()

    result = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=() if all_sources else ("corpus/runbook.md",),
        all_sources=all_sources,
    )

    # The block was found and read either way; the stamp the read carries is
    # the proof that discovery reached this page without a catalog to name it.
    assert [item.outcome for item in result.items] == ["unchanged"]
    assert [request.stamp.block_id for request in client.requests] == ["pub-example"]
    assert source.read_bytes() == before


def test_a_local_edit_is_dirty_and_accept_local_restamps_the_block_on_it(
    tmp_path: Path,
) -> None:
    """Converted: `--discard-local` used to overwrite the hand-edited body.

    A body that no longer matches its stamp is decided from the page alone --
    the daemon is never asked, because no answer it could give would change the
    finding -- and it is reported `dirty`, with the repin that re-stamps what
    the block now says. There is no accepted body to put back, so the flag is
    renamed to what it now means: `--accept-local` says the local prose IS the
    block, and records that by moving the stamp onto it and declaring it. The
    prose is left exactly where the author left it.
    """

    source = _workspace(tmp_path)
    edited = source.read_bytes().replace(OLD_BODY, EDITED_BODY)
    source.write_bytes(edited)
    client = _SyncClient(status="successor")

    reported = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert client.requests == []
    (item,) = reported.items
    assert item.outcome == "dirty"
    assert item.reason == "block_locally_modified"
    assert item.repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={"source_id": "corpus.runbook", "block_id": "pub-example"},
    )
    assert item.detail["last_synced_body_digest"] == _digest(OLD_BODY)
    assert item.detail["observed_body_digest"] == _digest(EDITED_BODY)
    assert reported.has_refusals is True
    assert reported.would_change is False
    assert source.read_bytes() == edited

    accepted = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
        accept_local_paths=(source,),
    )

    (row,) = accepted.items
    assert row.outcome == "synced"
    assert row.reason is None
    assert row.detail["local_body_accepted"] is True
    assert row.detail["stamped_body_digest"] == _digest(OLD_BODY)
    assert row.detail["observed_body_digest"] == _digest(EDITED_BODY)
    assert accepted.has_refusals is False
    assert client.requests == []

    # The stamp moved onto the prose, the prose did not move, and the instance
    # holds the declaration that records the alignment.
    restamped = source.read_bytes()
    (block,) = parse_projection_blocks(restamped, source_id="corpus.runbook")
    assert block.stamp is not None
    assert block.stamp.body_digest == _digest(EDITED_BODY)
    assert restamped[block.body_start : block.body_end] == EDITED_BODY
    assert [item["body_digest"] for item in client.declared] == [_digest(EDITED_BODY)]

    # `--check` proves the same alignment without writing.
    source.write_bytes(edited)
    client.declared.clear()
    previewed = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
        accept_local_paths=(source,),
        check=True,
    )
    (preview,) = previewed.items
    assert preview.outcome == "would_sync"
    assert client.declared == []
    assert source.read_bytes() == edited


def test_retired_block_refuses_then_detaches_markers_without_changing_body(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    before = source.read_bytes()
    (block,) = parse_projection_blocks(before, source_id="corpus.runbook")
    body = before[block.body_start : block.body_end]
    client = _SyncClient(refusal="block_backing_retired")

    refused = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert refused.items[0].reason == "block_backing_retired"
    assert refused.items[0].repair == RepairOperationV1(
        operation="playbill.block.sync",
        arguments={"paths": [refused.items[0].path], "detach": True},
    )

    detached = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )
    assert detached.items[0].outcome == "detached"
    assert (
        detached.items[0].detail["outside_digest_before"]
        == (detached.items[0].detail["outside_digest_after"])
    )
    assert source.read_bytes() == b"PREFIX\n" + body + b"SUFFIX\n"


def test_a_foreign_instances_block_detaches_instead_of_naming_a_re_attach(
    tmp_path: Path,
) -> None:
    """Card 88: --detach is the case a foreign block IS, and it was gated shut.

    Moving a worktree from one governed host to another leaves the markers the
    old host published. Both the read and the escape hatch answered
    workspace_instance_mismatch, whose named repair re-attaches this worktree to
    the host that published them -- undoing the rebind the operator is doing.
    Stripping a marker pair and keeping the body is a local text edit that
    claims no authority over the foreign block at all.
    """

    source = _workspace(tmp_path)
    before = source.read_bytes()
    (block,) = parse_projection_blocks(before, source_id="corpus.runbook")
    body = before[block.body_start : block.body_end]
    client = _SyncClient(refusal="block_workspace_instance_mismatch")

    refused = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert refused.items[0].reason == "workspace_instance_mismatch"
    assert source.read_bytes() == before

    detached = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )

    assert detached.items[0].outcome == "detached"
    assert (
        detached.items[0].detail["foreign_declared_git_oid"]
        == block.stamp.declared_coordinate.git_oid
    )
    assert (
        detached.items[0].detail["outside_digest_before"]
        == detached.items[0].detail["outside_digest_after"]
    )
    assert source.read_bytes() == b"PREFIX\n" + body + b"SUFFIX\n"


def test_ambiguous_live_successors_emit_exact_repin_selections(tmp_path: Path) -> None:
    source = _workspace(tmp_path)

    result = sync_projection_blocks(
        _SyncClient(refusal="block_successor_ambiguous"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )

    assert result.items[0].reason == "block_successor_ambiguous"
    assert result.items[0].repair == RepairOperationV1(
        operation="playbill.block.repin",
        arguments={
            "source_id": "corpus.runbook",
            "block_id": "pub-example",
            "backing_candidates": ["sha256:" + "a" * 64, "sha256:" + "b" * 64],
        },
    )


def test_malformed_marker_and_foreign_workspace_are_typed_without_daemon_calls(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path, instance_id="inst_other")
    client = _SyncClient()
    mismatch = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert mismatch.items[0].reason == "workspace_instance_mismatch"
    assert client.requests == []

    coverage = tmp_path / ".playbill" / "coverage.json"
    coverage.write_text(
        coverage.read_text(encoding="utf-8").replace("inst_other", INSTANCE_ID),
        encoding="utf-8",
    )
    source.write_bytes(b"<!-- playbill:block:broken -->\nbody\n")
    malformed = sync_projection_blocks(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(source,),
    )
    assert malformed.items[0].reason == "block_marker_malformed"
    assert malformed.items[0].detail["target"] == "corpus/runbook.md:1"


def test_catalog_optional_explicit_path_cannot_escape_workspace(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_bytes(source.read_bytes())

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=(outside,),
    )

    assert result.items[0].reason == "source_path_invalid"
    assert result.items[0].path == outside.name


def test_nonportable_catalog_returns_a_typed_refusal(tmp_path: Path) -> None:
    source = _workspace(tmp_path)
    outside = tmp_path.parent / "outside-runbook.md"
    outside.write_bytes(source.read_bytes())
    catalog = tmp_path / ".playbill" / "sources.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        .replace("catalog_kind: portable", "catalog_kind: local")
        .replace("locator: corpus/runbook.md", f"locator: {outside}"),
        encoding="utf-8",
    )

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    assert result.items[0].outcome == "refused"
    assert result.items[0].reason == "workspace_source_catalog_invalid"
    assert outside.read_bytes() == source.read_bytes()


def test_catalog_symlink_escape_is_typed_and_does_not_abort_other_sources(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path)
    valid_source = tmp_path / "corpus" / "valid.md"
    valid_stamp = _stamp().model_copy(update={"source_id": "corpus.valid", "block_id": "pub-valid"})
    valid_source.write_bytes(frame_projection_block(stamp=valid_stamp, body=OLD_BODY))
    valid_before = valid_source.read_bytes()
    catalog = tmp_path / ".playbill" / "sources.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + """\
  - name: corpus.valid
    locator: corpus/valid.md
    document_id: valid
    document_kind: runbook
    title: Valid
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:valid]
""",
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside-runbook.md"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.source_id: item for item in result.items}
    assert rows["corpus.runbook"].outcome == "refused"
    assert rows["corpus.runbook"].reason == "source_path_invalid"
    assert "escapes the workspace" in str(rows["corpus.runbook"].detail["message"])
    # The lawful source in the same walk is still read and reported, and the
    # escaping symlink's target is never opened through it.
    assert rows["corpus.valid"].outcome == "unchanged"
    assert valid_source.read_bytes() == valid_before
    assert outside.read_bytes().count(OLD_BODY) == 1


def test_whole_file_cas_preserves_a_concurrent_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compare-and-swap now guards the one write left, which is `--detach`.

    A whole-file replacement that landed over somebody else's concurrent write
    would silently discard it, so the swap is conditional on the exact bytes
    that were read. This used to be proved through the convergence write; the
    detach write inherits it unchanged, and losing it would mean a marker strip
    could eat a concurrent edit to prose it never looked at.
    """

    source = _workspace(tmp_path)
    concurrent = source.read_bytes() + b"CONCURRENT\n"
    from cruxible_client.authoring import blocks as block_module

    original = block_module.replace_publication_file

    def race(path: Path, *, expected: bytes, replacement: bytes) -> None:
        path.write_bytes(concurrent)
        original(path, expected=expected, replacement=replacement)

    monkeypatch.setattr(block_module, "replace_publication_file", race)
    result = sync_projection_blocks(
        _SyncClient(refusal="block_backing_retired"),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        detach_paths=(source,),
    )

    assert result.items[0].outcome == "refused"
    assert result.items[0].reason == "block_concurrent_edit"
    assert source.read_bytes() == concurrent
    assert os.path.isfile(source)


_QUOTING_CAPTURE = (
    b"# Agent reports\n"
    b"\n"
    b"The proposed grammar was `<!-- playbill:block:draft-note -->`, quoted here\n"
    b"<!-- playbill:block:draft-note -->\n"
    b"so a reader can see the exact bytes under discussion.\n"
)


def _catalog_a_second_source(root: Path, *, name: str, locator: str) -> None:
    catalog = root / ".playbill" / "sources.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + f"""\
  - name: {name}
    locator: {locator}
    document_id: {name.split(".")[-1]}
    document_kind: report
    title: Reports
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:{name.split(".")[-1]}]
""",
        encoding="utf-8",
    )


def test_a_capture_that_quotes_marker_bytes_is_skipped_by_the_catalog_walk(
    tmp_path: Path,
) -> None:
    """Card 101: a source ABOUT markers is not a projection target.

    The whole point of the capture is that it is exact accepted bytes, so the
    `block_marker_malformed` repair -- hand-edit the file -- is the one repair
    unavailable here, and the refusal it produced turned every activation that
    runs this same walk into a non-zero exit.
    """

    source = _workspace(tmp_path)
    before = source.read_bytes()
    capture = tmp_path / "history" / "agent-reports.md"
    capture.parent.mkdir()
    capture.write_bytes(_QUOTING_CAPTURE)
    _catalog_a_second_source(tmp_path, name="repo.reports", locator="history/agent-reports.md")

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.source_id: item for item in result.items}
    assert rows["repo.reports"].outcome == "skipped"
    assert rows["repo.reports"].reason == "source_not_projection_target"
    assert rows["repo.reports"].path == "history/agent-reports.md"
    # The lawful sources in the same walk are still read and reported, and the
    # walk as a whole reports no refusal -- which is what an activation's exit
    # code reads.
    assert rows["corpus.runbook"].outcome == "unchanged"
    assert result.has_refusals is False
    assert source.read_bytes() == before
    assert capture.read_bytes() == _QUOTING_CAPTURE


def test_naming_the_quoting_path_explicitly_still_refuses(tmp_path: Path) -> None:
    """An explicit path asserts the file declares a block, so it is parsed."""

    _workspace(tmp_path)
    capture = tmp_path / "history" / "agent-reports.md"
    capture.parent.mkdir()
    capture.write_bytes(_QUOTING_CAPTURE)
    _catalog_a_second_source(tmp_path, name="repo.reports", locator="history/agent-reports.md")

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        paths=("history/agent-reports.md",),
    )

    assert result.items[0].outcome == "refused"
    assert result.items[0].reason == "block_marker_malformed"
    assert result.has_refusals is True


def test_the_catalog_free_workspace_walk_also_skips_a_quoting_capture(
    tmp_path: Path,
) -> None:
    """The inferred walk that has no catalog reaches the same conclusion."""

    source = _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()
    before = source.read_bytes()
    capture = tmp_path / "history" / "agent-reports.md"
    capture.parent.mkdir()
    capture.write_bytes(_QUOTING_CAPTURE)

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.path: item for item in result.items}
    assert rows["history/agent-reports.md"].outcome == "skipped"
    assert rows["history/agent-reports.md"].reason == "source_not_projection_target"
    assert rows["corpus/runbook.md"].outcome == "unchanged"
    assert result.has_refusals is False
    assert source.read_bytes() == before


def _broken_projection_page(root: Path, *, source_id: str, locator: str) -> Path:
    """A catalogued projection page, stamped, with its closing marker deleted."""

    stamp = ProjectionBlockStampV1(
        source_id=source_id,
        block_id="pub-broken",
        declared_generation=1,
        declared_coordinate=OLD_COORDINATE,
        backing=(_claim_backing("b"),),
        body_digest=_digest(OLD_BODY),
    )
    framed = frame_projection_block(stamp=stamp, body=OLD_BODY)
    page = root / locator
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_bytes(b"PREFIX\n" + framed.rsplit(b"<!-- /playbill:block:", 1)[0])
    return page


def test_a_registered_page_with_a_deleted_closing_marker_still_refuses_under_all(
    tmp_path: Path,
) -> None:
    """A real marker defect is not hidden behind "not a projection target".

    The skip exists for a capture that merely QUOTES the grammar. This page
    carries its own stamp -- bytes only a stamping write produces -- and is
    catalogued under the source id that stamp names, so it is a projection page
    by every available reading, with a defect. Classifying it by the selection
    mode alone made `block sync --all` skip it with `has_refusals` False, and
    the sync that `proposal activate` runs as its last step therefore exited
    zero over a broken page until somebody happened to name that path.
    """

    source = _workspace(tmp_path)
    before = source.read_bytes()
    _broken_projection_page(tmp_path, source_id="repo.notes", locator="notes/page.md")
    _catalog_a_second_source(tmp_path, name="repo.notes", locator="notes/page.md")

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.path: item for item in result.items}
    assert rows["notes/page.md"].outcome == "refused"
    assert rows["notes/page.md"].reason == "block_marker_malformed"
    assert result.has_refusals is True
    # The lawful source in the same walk is still read and reported: one broken
    # page is one refusal, not a stopped walk.
    assert rows["corpus/runbook.md"].outcome == "unchanged"
    assert source.read_bytes() == before


def test_a_registered_page_that_repeats_a_block_identity_still_refuses_under_all(
    tmp_path: Path,
) -> None:
    """The second marker defect review found hidden: a duplicated identity."""

    _workspace(tmp_path)
    page = _broken_projection_page(tmp_path, source_id="repo.notes", locator="notes/page.md")
    stamped = page.read_bytes().rstrip(b"\n").split(b"\n")[1] + b"\n"
    page.write_bytes(page.read_bytes() + b"<!-- /playbill:block:pub-broken -->\n" + stamped)
    _catalog_a_second_source(tmp_path, name="repo.notes", locator="notes/page.md")

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.path: item for item in result.items}
    assert rows["notes/page.md"].outcome == "refused"
    assert rows["notes/page.md"].reason == "block_marker_malformed"
    assert "repeats block identity" in str(rows["notes/page.md"].detail["message"])
    assert result.has_refusals is True


def test_the_catalog_free_walk_also_refuses_a_page_that_declares_a_block_badly(
    tmp_path: Path,
) -> None:
    """The inferred walk with no catalog draws the same line."""

    _workspace(tmp_path)
    (tmp_path / ".playbill" / "sources.yaml").unlink()
    _broken_projection_page(tmp_path, source_id="repo.notes", locator="notes/page.md")
    capture = tmp_path / "history" / "agent-reports.md"
    capture.parent.mkdir()
    capture.write_bytes(_QUOTING_CAPTURE)

    result = sync_projection_blocks(
        _SyncClient(),  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        all_sources=True,
    )

    rows = {item.path: item for item in result.items}
    assert rows["notes/page.md"].outcome == "refused"
    assert rows["notes/page.md"].reason == "block_marker_malformed"
    assert rows["history/agent-reports.md"].outcome == "skipped"
    assert rows["history/agent-reports.md"].reason == "source_not_projection_target"
    assert result.has_refusals is True


def test_repin_with_an_exact_backing_digest_reads_the_single_held_member(
    tmp_path: Path,
) -> None:
    """`--backing` selects one exact successor, and needs the read to name it.

    `block repin --backing DIGEST` is how an author resolves an ambiguous
    lineage: it hands the daemon one digest and re-stamps whatever that digest
    resolves to. The only thing in the result it reads is `backing`, so a read
    that names the moved members but forgets the CURRENT spelling of a block
    that has not moved leaves the flag refusing a live successor. Nothing else
    in the tree exercises that field, and the block-sync report does not need
    it, so it is pinned here.
    """

    source = _workspace(tmp_path)
    stamp = _stamp()
    client = _SyncClient(status="current")
    stamped = repin_projection_block(
        client,  # type: ignore[arg-type]
        INSTANCE_ID,
        workspace=tmp_path,
        source_id="corpus.runbook",
        block_id="pub-example",
        backing_digest="sha256:" + "b" * 64,
        evaluation_time=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )

    (request,) = client.requests
    assert request.preferred_successor_digest == "sha256:" + "b" * 64
    assert request.stamp == stamp
    assert stamped.backing == (_claim_backing("a"),)
    assert stamped.declared_coordinate == NEW_COORDINATE
    content = source.read_bytes()
    assert content.startswith(b"PREFIX\n") and content.endswith(b"SUFFIX\n")
    (block,) = parse_projection_blocks(
        content[len(b"PREFIX\n") : -len(b"SUFFIX\n")],
        source_id="corpus.runbook",
    )
    # Only the opening line moves: a repin re-stamps a declaration and never
    # touches the prose it is held against.
    assert block.stamp == stamped
    assert content[len(b"PREFIX\n") + block.body_start : len(b"PREFIX\n") + block.body_end] == (
        OLD_BODY
    )
