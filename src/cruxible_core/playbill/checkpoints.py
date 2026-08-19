"""Local verified-replay checkpoints: a disposable cache of verification work.

Why this exists
---------------
Reopening an instance replays accepted history from genesis, and every
generation costs a full law re-evaluation, an approval-signature verification,
and a body-availability check. That cost is linear in the length of history, so
at adoption scale a process start would replay thousands of generations before
it could serve anything. A checkpoint lets a reopen re-verify only a bounded
suffix.

What a checkpoint is, and is not
--------------------------------
It is **not** a second authority and **not** a wire record. It never enters the
ledger, the journals, or any frozen format; it is one local file under the
instance's own rebuildable cache root, exactly like a projection build.
Deleting it can only cost time. It can never change the accepted answer, and it
is never accepted as proof by anything outside this instance.

The security argument
---------------------
The requirement is precise: replaying only a suffix must not weaken tamper
detection *for that suffix*, and must not let a checkpoint manufacture an
accepted answer that a genesis-rooted replay would not have produced.

1. Nothing in the file is believed because it is written down. Every value the
   suffix consumes is re-derived on load from bytes the ledger holds, and the
   file's own copies are compared against those derivations. A mismatch in any
   one of them is a typed refusal and a fall back to genesis.

2. The prefix's coordinates are re-derived, not trusted. The commit chain comes
   from `main_history()`. Genesis is the trust-rooted coordinate the descriptor
   already proved. Every accepted change-set record for the whole prefix lives
   in the daemon-signed tree at the checkpoint coordinate -- `changesets/` is
   append-only and replay refuses any generation that modifies a predecessor's
   record -- so each generation's committed manifest root, change-set digest and
   recorded approvals are read out of one signed tree. That is enough to
   recompute the entire `playbill-sroot-v1` chain from the genesis semantic root
   forward, and the `playbill-gen-v1` chain with it. The checkpoint's claimed
   semantic and generation roots are then required to equal the recomputed head
   roots. A forged root does not survive the recomputation.

3. The checkpoint coordinate's own tree is read from the ledger, its semantic
   manifest is rebuilt by hashing those bytes, and that manifest's root is
   required to equal the manifest root the accepted change-set record at that
   sequence commits to. So the member digests the suffix carries forward are
   bound to a daemon-signed generation, not to this file.

4. The daemon signature on the checkpoint commit is verified against the
   principal registry reconstructed for its predecessor, so the key that admits
   the first suffix generation comes from replayed registry state.

5. The suffix is then replayed by exactly the code a genesis-rooted replay runs,
   against a window seeded with re-derived state. Suffix tamper detection is
   therefore identical, not merely similar.

What a checkpoint genuinely elides -- and it is only this -- is the *expensive*
per-generation verification of the prefix: the acceptance-law and closure
re-evaluation, the approval signature checks, and the body-availability checks.
Those are the operations a checkpoint exists to skip. An operator who wants them
back deletes the file.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from cruxible_core.playbill.attestations import approval_digest
from cruxible_core.playbill.bootstrap import VerifiedGenesis, generation_root
from cruxible_core.playbill.canonical import (
    GenerationRoot,
    Manifest,
    SemanticManifestRoot,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
    manifest_for_tree,
    manifest_root_from_members,
    semantic_projection,
    typed_digest,
)
from cruxible_core.playbill.errors import PlaybillError, ReplayCheckpointError
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
from cruxible_core.playbill.settlement import (
    ChangeSetRecord,
    ChangeSetRecordV2,
    compute_semantic_root,
    parse_producible_change_set_record,
)
from cruxible_core.playbill.types import (
    CompilerCoordinate,
    GenerationDescriptor,
    GenesisCoordinate,
    GitObjectFormat,
)
from cruxible_core.temporal import format_datetime, utc_now

CHECKPOINT_DIRECTORY: Final = "checkpoints"
CHECKPOINT_FILE: Final = "replay-checkpoint-v1.json"
CHECKPOINT_TAG: Final = "playbill-replay-checkpoint-v1"
DEFAULT_CHECKPOINT_INTERVAL: Final = 50

_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PRINCIPAL_PATH_PREFIX: Final = "principals/"


class ReplayCheckpointDigest(Sha256Value):
    """The self-digest of one local checkpoint file.

    Deliberately declared here rather than beside the wire digest kinds: this
    value commits to a local operational cache, never to accepted state, and no
    ledger record, journal record, or exported format may ever carry it.
    """

    kind = "replay checkpoint digest"


class _StrictCheckpointModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayCheckpointBodyV1(_StrictCheckpointModel):
    """Exactly the fields the checkpoint digest commits to.

    No wall-clock value appears here. The file records when it was written
    outside this preimage, so an identical accepted coordinate always produces
    an identical digest.
    """

    tag: Literal["playbill-replay-checkpoint-v1"] = CHECKPOINT_TAG
    format_version: Literal[1] = 1
    instance_id: str
    git_object_format: GitObjectFormat
    compiler: CompilerCoordinate
    genesis: GenesisCoordinate
    sequence: int
    git_oid: str
    semantic_root: str
    generation_root: str
    parent_generation_root: str
    manifest_root: str
    members: dict[str, str]
    principals: PrincipalRegistrySnapshot

    @field_validator("sequence")
    @classmethod
    def _sequence(cls, value: int) -> int:
        if value < 1:
            raise ValueError("a checkpoint summarizes at least one accepted generation")
        return value

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("checkpoint Git OID is malformed")
        return value

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("generation_root", "parent_generation_root")
    @classmethod
    def _generation_root(cls, value: str) -> str:
        GenerationRoot.from_tagged(value)
        return value

    @field_validator("manifest_root")
    @classmethod
    def _manifest_root(cls, value: str) -> str:
        SemanticManifestRoot.from_tagged(value)
        return value


class ReplayCheckpointFileV1(_StrictCheckpointModel):
    """The on-disk record: one digest-committed body plus non-committed metadata."""

    tag: Literal["playbill-replay-checkpoint-file-v1"] = "playbill-replay-checkpoint-file-v1"
    body: ReplayCheckpointBodyV1
    checkpoint_digest: str
    written_at: str

    @field_validator("checkpoint_digest")
    @classmethod
    def _checkpoint_digest(cls, value: str) -> str:
        ReplayCheckpointDigest.from_tagged(value)
        return value


@dataclass(frozen=True)
class CheckpointGeneration:
    """One prefix generation, re-derived rather than read from the checkpoint."""

    sequence: int
    oid: str
    semantic_root: SemanticRoot
    descriptor: GenerationDescriptor
    generation_root: GenerationRoot
    principals: PrincipalRegistrySnapshot
    record: ChangeSetRecord | ChangeSetRecordV2 | None


@dataclass(frozen=True)
class CheckpointSeed:
    """A verified prefix summary plus the exact window state the suffix resumes from."""

    prefix: tuple[CheckpointGeneration, ...]
    tree: dict[str, bytes]
    manifest: Manifest


def checkpoint_digest(body: ReplayCheckpointBodyV1) -> ReplayCheckpointDigest:
    return typed_digest(
        ReplayCheckpointDigest,
        CHECKPOINT_TAG,
        {key: value for key, value in body.model_dump(mode="json").items() if key not in {"tag"}},
    )


def render_checkpoint(body: ReplayCheckpointBodyV1, *, written_at: str) -> bytes:
    record = ReplayCheckpointFileV1(
        body=body,
        checkpoint_digest=checkpoint_digest(body).tagged,
        written_at=written_at,
    )
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def checkpoint_body(
    *,
    instance_id: str,
    object_format: GitObjectFormat,
    compiler: CompilerCoordinate,
    genesis: GenesisCoordinate,
    sequence: int,
    git_oid: str,
    semantic_root: str,
    generation_root: str,
    parent_generation_root: str,
    tree: Mapping[str, bytes],
    members: Manifest | None = None,
) -> ReplayCheckpointBodyV1:
    """Summarize one already-verified accepted coordinate.

    Both the member manifest and the principal registry are derived from the
    coordinate's own tree rather than accepted from the caller. A caller holding
    a settlement bundle has the *parent's* registry close at hand and the new
    generation's nowhere in sight, so accepting one would invite a summary that
    disagrees with its own coordinate and is discarded on the next reopen.
    `members` may be supplied only to reuse a manifest the caller already
    computed over this exact tree.
    """

    resolved = members if members is not None else members_for_tree(tree)
    principals = principal_registry_from_tree(tree, semantic_root=semantic_root)
    return ReplayCheckpointBodyV1(
        instance_id=instance_id,
        git_object_format=object_format,
        compiler=compiler,
        genesis=genesis,
        sequence=sequence,
        git_oid=git_oid,
        semantic_root=semantic_root,
        generation_root=generation_root,
        parent_generation_root=parent_generation_root,
        manifest_root=manifest_root_from_members(resolved).tagged,
        members=resolved,
        principals=principals,
    )


def members_for_tree(tree: Mapping[str, bytes]) -> Manifest:
    """Build the semantic member manifest a checkpoint commits to."""

    return manifest_for_tree(semantic_projection(tree))


def checkpoint_path(directory: Path) -> Path:
    return directory / CHECKPOINT_FILE


def write_checkpoint(
    directory: Path,
    body: ReplayCheckpointBodyV1,
    *,
    written_at: str | None = None,
) -> Path:
    """Publish one checkpoint atomically, replacing any earlier one in place.

    `written_at` is operator-facing metadata and sits outside the digest
    preimage, so supplying it never changes what the checkpoint commits to; it
    exists so a deterministic builder can produce byte-stable files.
    """

    written_at = written_at if written_at is not None else format_datetime(utc_now())
    if written_at is None:
        raise ReplayCheckpointError("failed to stamp a checkpoint write time")

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = checkpoint_path(directory)
    if target.is_symlink():
        raise ReplayCheckpointError("checkpoint path may not be a symlink")
    temporary = directory / f".checkpoint-{secrets.token_hex(12)}.tmp"
    content = render_checkpoint(body, written_at=written_at)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return target


def discard_checkpoint(directory: Path) -> None:
    """Remove a checkpoint that failed verification; recovery falls back to genesis."""

    target = checkpoint_path(directory)
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)


def load_checkpoint_file(directory: Path) -> ReplayCheckpointFileV1 | None:
    """Read and self-verify one checkpoint file, or report its absence."""

    target = checkpoint_path(directory)
    if target.is_symlink():
        raise ReplayCheckpointError("checkpoint file may not be a symlink")
    if not target.is_file():
        return None
    raw = target.read_bytes()
    try:
        record = ReplayCheckpointFileV1.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise ReplayCheckpointError("checkpoint file is malformed") from exc
    if canonical_bytes(record.model_dump(mode="json")) + b"\n" != raw:
        raise ReplayCheckpointError("checkpoint file is not canonical")
    if checkpoint_digest(record.body).tagged != record.checkpoint_digest:
        raise ReplayCheckpointError("checkpoint digest does not reproduce from its body")
    return record


def _prefix_records(
    tree: Mapping[str, bytes],
    *,
    sequence: int,
) -> tuple[ChangeSetRecord | ChangeSetRecordV2, ...]:
    """Read every accepted change-set record of the prefix from one signed tree.

    `changesets/` is append-only in accepted history -- replay refuses any
    generation that rewrites a predecessor's record -- so the tree at the
    checkpoint coordinate carries the exact record each earlier generation
    settled, under the same daemon signature.
    """

    records: list[ChangeSetRecord | ChangeSetRecordV2] = []
    for index in range(1, sequence + 1):
        path = f"changesets/cs-{index:020d}.json"
        content = tree.get(path)
        if content is None:
            raise ReplayCheckpointError(
                f"checkpoint coordinate is missing an accepted change-set record: {path}"
            )
        record = parse_producible_change_set_record(
            content,
            path=path,
            operation="checkpoint prefix replay",
        )
        if record.sequence != index:
            raise ReplayCheckpointError("accepted change-set record sequence differs from its path")
        records.append(record)
    present = {path for path in tree if path.startswith("changesets/")}
    if len(present) != sequence:
        raise ReplayCheckpointError(
            "checkpoint coordinate carries change-set records outside its own history"
        )
    return tuple(records)


def _principals_at(
    ledger: GitLedger,
    *,
    oid: str,
    semantic_root: str,
) -> PrincipalRegistrySnapshot:
    """Read only the principal members of one exact generation."""

    entries = tuple(
        entry for entry in ledger.list_tree(oid) if entry.path.startswith(_PRINCIPAL_PATH_PREFIX)
    )
    blobs = ledger.read_blobs(tuple(entry.oid for entry in entries))
    return principal_registry_from_tree(
        {entry.path: blobs[entry.oid] for entry in entries},
        semantic_root=semantic_root,
    )


def _rederive_prefix(
    ledger: GitLedger,
    *,
    genesis: VerifiedGenesis,
    history: tuple[str, ...],
    records: tuple[ChangeSetRecord | ChangeSetRecordV2, ...],
    head_tree: Mapping[str, bytes],
) -> tuple[CheckpointGeneration, ...]:
    """Rebuild the whole prefix coordinate chain from the verified genesis forward."""

    genesis_principals = principal_registry_from_tree(
        genesis.tree,
        semantic_root=genesis.semantic_root.tagged,
    )
    prefix: list[CheckpointGeneration] = [
        CheckpointGeneration(
            sequence=0,
            oid=genesis.oid,
            semantic_root=genesis.semantic_root,
            descriptor=genesis.descriptor,
            generation_root=genesis.generation_root,
            principals=genesis_principals,
            record=None,
        )
    ]
    principals = genesis_principals.principals
    for index, record in enumerate(records, start=1):
        parent = prefix[-1]
        oid = history[index]
        # Spelled as replay spells it, list and not set: a duplicated approval
        # digest must raise here exactly as it would there, never fold away.
        approvals = tuple(
            sorted(
                approval_digest(submission.attestation).tagged for submission in record.approvals
            )
        )
        semantic_root = compute_semantic_root(
            manifest_root_value=record.candidate.candidate_manifest_root,
            changeset_digest_value=record.changeset_digest,
            approval_digests=approvals,
            parent_semantic_root=parent.semantic_root.tagged,
        )
        descriptor = GenerationDescriptor(
            semantic_root=semantic_root.value,
            git_oid=oid,
            parent_generation_root=parent.generation_root.value,
        )
        if any(path.startswith(_PRINCIPAL_PATH_PREFIX) for path in record.candidate.scope):
            # Registry transitions are rare and each one is read from exactly the
            # generation that made it, so the timeline never drifts.
            snapshot = _principals_at(ledger, oid=oid, semantic_root=semantic_root.tagged)
            principals = snapshot.principals
        else:
            snapshot = PrincipalRegistrySnapshot(
                semantic_root=semantic_root.tagged,
                principals=principals,
            )
        prefix.append(
            CheckpointGeneration(
                sequence=index,
                oid=oid,
                semantic_root=semantic_root,
                descriptor=descriptor,
                generation_root=generation_root(descriptor),
                principals=snapshot,
                record=record,
            )
        )
    head = prefix[-1]
    head_registry = principal_registry_from_tree(
        head_tree,
        semantic_root=head.semantic_root.tagged,
    )
    if head_registry != head.principals:
        raise ReplayCheckpointError(
            "re-derived principal registry differs from the checkpoint coordinate tree"
        )
    return tuple(prefix)


def verify_checkpoint(
    ledger: GitLedger,
    record: ReplayCheckpointFileV1,
    *,
    genesis: VerifiedGenesis,
    instance_id: str,
    object_format: GitObjectFormat,
    compiler: CompilerCoordinate,
    genesis_coordinate: GenesisCoordinate,
) -> CheckpointSeed:
    """Re-derive the checkpointed prefix from the ledger, or refuse it."""

    body = record.body
    if body.instance_id != instance_id:
        raise ReplayCheckpointError("checkpoint instance identity differs from this instance")
    if body.git_object_format != object_format:
        raise ReplayCheckpointError("checkpoint Git object format differs from this ledger")
    if body.compiler != compiler:
        raise ReplayCheckpointError("checkpoint compiler coordinate differs from this instance")
    if body.genesis != genesis_coordinate:
        raise ReplayCheckpointError("checkpoint genesis coordinate differs from this instance")

    history = ledger.main_history()
    if not history or history[0] != genesis.oid:
        raise ReplayCheckpointError("main history is not rooted at verified genesis")
    if body.sequence >= len(history):
        raise ReplayCheckpointError("checkpoint sequence is beyond accepted main history")
    if history[body.sequence] != body.git_oid:
        raise ReplayCheckpointError(
            "checkpoint coordinate is not the accepted generation at its sequence"
        )

    tree = ledger.read_tree(body.git_oid)
    members = manifest_for_tree(semantic_projection(tree))
    manifest_root = manifest_root_from_members(members)
    records = _prefix_records(tree, sequence=body.sequence)
    if manifest_root.tagged != records[-1].candidate.candidate_manifest_root:
        raise ReplayCheckpointError(
            "checkpoint coordinate tree differs from the manifest root its change set accepted"
        )
    if manifest_root.tagged != body.manifest_root or members != body.members:
        raise ReplayCheckpointError("checkpoint member manifest differs from its coordinate tree")

    prefix = _rederive_prefix(
        ledger,
        genesis=genesis,
        history=history,
        records=records,
        head_tree=tree,
    )
    head = prefix[-1]
    if head.semantic_root.tagged != body.semantic_root:
        raise ReplayCheckpointError("re-derived semantic root differs from the checkpoint")
    if head.generation_root.tagged != body.generation_root:
        raise ReplayCheckpointError("re-derived generation root differs from the checkpoint")
    if prefix[-2].generation_root.tagged != body.parent_generation_root:
        raise ReplayCheckpointError("re-derived parent generation root differs from the checkpoint")
    if body.principals != head.principals:
        raise ReplayCheckpointError("checkpoint principal registry differs from its coordinate")

    daemon = prefix[-2].principals.require_active("daemon")
    if not ledger.verify_commit_with_public_key(
        head.oid,
        principal_id="daemon",
        public_key_hex=daemon.public_key,
    ):
        raise ReplayCheckpointError("checkpoint generation daemon signature does not verify")
    note = ledger.read_generation_note(head.oid)
    if note is not None:
        expected = canonical_bytes(head.descriptor.model_dump(mode="json")) + b"\n"
        if note != expected:
            raise ReplayCheckpointError(
                "ledger generation note differs from the re-derived checkpoint descriptor"
            )
    return CheckpointSeed(prefix=prefix, tree=tree, manifest=members)


def load_verified_checkpoint(
    ledger: GitLedger,
    directory: Path | None,
    *,
    genesis: VerifiedGenesis,
    instance_id: str,
    object_format: GitObjectFormat,
    compiler: CompilerCoordinate,
    genesis_coordinate: GenesisCoordinate,
) -> CheckpointSeed | None:
    """Return a verified seed, or `None` after discarding an unusable checkpoint.

    A checkpoint that fails any check is never partially believed: it is deleted
    and the caller performs an ordinary genesis-rooted replay. The refusal net is
    deliberately wide -- a corrupt cache must never be able to crash a reopen
    that would otherwise have succeeded from genesis.
    """

    if directory is None:
        return None
    try:
        record = load_checkpoint_file(directory)
        if record is None:
            return None
        return verify_checkpoint(
            ledger,
            record,
            genesis=genesis,
            instance_id=instance_id,
            object_format=object_format,
            compiler=compiler,
            genesis_coordinate=genesis_coordinate,
        )
    except (PlaybillError, OSError, ValueError):
        discard_checkpoint(directory)
        return None


__all__ = [
    "CHECKPOINT_DIRECTORY",
    "CHECKPOINT_FILE",
    "CHECKPOINT_TAG",
    "DEFAULT_CHECKPOINT_INTERVAL",
    "CheckpointGeneration",
    "CheckpointSeed",
    "ReplayCheckpointBodyV1",
    "ReplayCheckpointDigest",
    "ReplayCheckpointFileV1",
    "checkpoint_body",
    "checkpoint_digest",
    "checkpoint_path",
    "members_for_tree",
    "discard_checkpoint",
    "load_checkpoint_file",
    "load_verified_checkpoint",
    "render_checkpoint",
    "verify_checkpoint",
    "write_checkpoint",
]
