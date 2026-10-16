"""Opt-in managed Cruxible instance initialization, reopening, and inspection."""

from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import Context, ContextVar
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import ValidationError

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicy,
    parse_approval_policy,
)
from cruxible_client.contracts.attestations import ApprovalSubmission
from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.canonical import canonical_bytes, is_candidate_card_path
from cruxible_client.contracts.captures import (
    AcceptedCaptureContract,
    capture_contract_digest,
    parse_capture_contract,
)
from cruxible_client.contracts.claims import (
    ClaimLawEvidenceAny,
    claim_path,
    parse_claim_law_evidence,
)
from cruxible_client.contracts.errors import (
    BootstrapError,
    FormatError,
    InstanceDecommissioned,
    PrincipalIntegrityError,
    ProjectionIntegrityError,
    ProposalContentUnavailable,
    ProposalIntegrityError,
    SettlementIntegrityError,
    SigningKeyError,
)
from cruxible_client.contracts.ledger_mirror import validate_mirror_url
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    parse_principal_record,
)
from cruxible_client.contracts.temporal import format_datetime, utc_now
from cruxible_client.contracts.types import (
    Decommission,
    Descriptor,
    GenesisCoordinate,
    GitObjectFormat,
    Inspection,
    OperatingProfile,
    PrincipalInspection,
    PrincipalRecord,
    RecoveryPosture,
    StorageLayout,
    TrustRoot,
    initial_authority_matrix,
)
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    WorkspaceAdvertisement,
)
from cruxible_core.authoring.assignment_records import SubjectAssignmentRecords
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.compiler.compiler import (
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    SUPPORTED_COMPILERS,
    artifact_kinds_for_compiler,
    current_compiler_coordinate,
)
from cruxible_core.compiler.projection_artifacts import (
    ArtifactEnvelopeRow,
    registered_path_kind,
)
from cruxible_core.compiler.projection_tree import TreeReadLimits, read_registered_tree
from cruxible_core.curation.review_operational import ReviewOperationalStore
from cruxible_core.derived.derived_runtime import BoundedCache
from cruxible_core.derived.derived_state import (
    ClaimPredicateKey,
    DerivedState,
    IndexDefinition,
    SnapshotTree,
    advance_accepted_tree,
)
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.exhaust.producer_receipts import (
    LocalProducerReceiptResolver,
    local_producer_receipt_resolver,
    requested_verification_context,
    verification_request,
)
from cruxible_core.governance.keys import (
    ALLOWED_SIGNERS_FILE,
    DAEMON_PRIVATE_KEY_FILE,
    DAEMON_PUBLIC_KEY_FILE,
    generate_daemon_key,
    public_key_hex_from_private_file,
    raw_public_key_hex_from_openssh,
)
from cruxible_core.indexes.evaluated_state import derive_indexed_state
from cruxible_core.indexes.history.history_index import (
    AcceptedGenerationLocation,
    AcceptedHistoryIndex,
    GenerationRecordFacts,
    HistoryReader,
    PriorMemberSequences,
    RetainedRecordReader,
    VerifiedRecord,
)
from cruxible_core.indexes.projection import (
    AcceptedCoordinate,
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    projection_manifest_name,
)
from cruxible_core.indexes.projection_retention import (
    evict_or_wait,
    held_here,
    released_pieces,
    retention_lock,
)
from cruxible_core.indexes.proposals.proposal_note_projection import ProposalNoteIndex
from cruxible_core.indexes.proposals.proposal_note_reader import proposal_note_snapshot
from cruxible_core.indexes.serving import bind_current_projection
from cruxible_core.indexes.sqlite import (
    ProjectionEvicted,
    ProjectionHandle,
    ProjectionPool,
    bind_projection,
    load_projection_manifest,
)
from cruxible_core.indexes.typed_sqlite import parse_static_owners
from cruxible_core.ledger.activation import ActivationPublisher, ActivationResult
from cruxible_core.ledger.bootstrap import (
    VerifiedGenesis,
    current_genesis_seed_set,
    prepare_genesis,
    seeded_procedure_runtime_policy,
    verify_genesis,
)
from cruxible_core.ledger.checkpoints import (
    CHECKPOINT_DIRECTORY,
    DEFAULT_CHECKPOINT_INTERVAL,
    QuietCheckpointWriter,
)
from cruxible_core.ledger.git import GitLedger
from cruxible_core.ledger.ledger_mirror import (
    LedgerMirrorStateV1,
    clear_mirror_state,
    mirror_credential_environment,
    mirror_lock,
    read_mirror_state,
    write_mirror_state,
)
from cruxible_core.ledger.recovery import (
    RecoveredGeneration,
    RecoveredInstanceState,
    candidate_from_change_set_record,
    prepared_generation_for_handoff,
    recover_instance,
)
from cruxible_core.ledger.witness import WitnessSink
from cruxible_core.proposals.prepared_evaluation import (
    AdmittedEvaluation,
    PreparedEvaluationAdapter,
)
from cruxible_core.proposals.proposal_evidence import (
    ProposalEvidenceStore,
    SettledCandidate,
    SettledCandidateLocator,
)
from cruxible_core.proposals.proposals import (
    ExhaustPromotionVerifierProtocol,
    ProposalReceiveLimits,
    ProposalService,
)
from cruxible_core.proposals.settlement import (
    ChangeActorBinding,
    ChangeSetRecordAnyVersion,
    SemanticRootDerivation,
    VerifiedGenerationBundle,
    prepare_generation,
    record_semantic_root_derivation,
    render_generation_descriptor,
)
from cruxible_core.runtime.phase_timing import phase
from cruxible_core.runtime.retention import DEFAULT_RETENTION, RetentionSettings
from cruxible_core.storage.cas import (
    CasObjectMetadata,
    ContentAddressedBodyStore,
    DryRunBodyStore,
    dry_run_held_bodies,
)
from cruxible_core.storage.preview_fence import is_previewing, refuse_write_while_previewing

if TYPE_CHECKING:
    from cruxible_core.evidence.attestation_verification import CaptureContractVersions
    from cruxible_core.evidence.claim_attestation_store import ClaimAttestationEvidenceStore
    from cruxible_core.proposals.refusal_index import RefusalIndex
    from cruxible_core.query.backends import ClaimQueryFactsV1

DESCRIPTOR_FILE = "instance.json"
# Ledgers are SHA-1 unless a workspace or caller says otherwise: common Git
# viewers do not recognize a SHA-256 repository, and a ledger nobody can open is
# not evidence anyone can read (maintainer ruling, 2026-09-03). Descriptors
# written before this ruling keep their pinned format and reopen unchanged.
DEFAULT_GIT_OBJECT_FORMAT: GitObjectFormat = "sha1"


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)
    _fsync_directory(path.parent)


def _atomic_replace(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Replace one existing file's bytes without ever leaving it truncated."""

    temporary = path.with_name(f".{path.name}.replacing")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _validate_client_principals(
    records: Sequence[PrincipalRecord],
) -> tuple[tuple[PrincipalRecord, ...], RecoveryPosture]:
    ordered = tuple(sorted(records, key=lambda record: record.principal_id))
    identifiers = [record.principal_id for record in ordered]
    if identifiers != sorted(set(identifiers)):
        raise BootstrapError("bootstrap client principals must be unique")
    ordinary = [record for record in ordered if record.kind == "ordinary"]
    if len(ordinary) < 1:
        raise BootstrapError("bootstrap requires at least one ordinary principal")
    if any(record.principal_id == "daemon" for record in ordered):
        raise BootstrapError("the daemon principal is generated by the instance")
    if any(record.status != "active" for record in ordered):
        raise BootstrapError("bootstrap client principals must begin active")
    if any(record.kind == "daemon" for record in ordered):
        raise BootstrapError("client principals cannot have daemon kind")
    recovery_configured = any(record.kind == "recovery" for record in ordered)
    posture: RecoveryPosture = (
        "recovery-configured" if recovery_configured else "narrowed-no-recovery"
    )
    return ordered, posture


_VALIDATED_PATH_CAPACITY = 64
#: The request-scoped projection pool of this thread (see ``projection_scope``).
_PROJECTION_POOL: ContextVar[ProjectionPool | None] = ContextVar(
    "cruxible_projection_pool", default=None
)
_PathBinding = tuple[str, tuple[tuple[tuple[int, int, int], ...], ...]]
_VALIDATED_PATHS: OrderedDict[
    tuple[str, tuple[tuple[str, Any], ...]],
    tuple[_PathBinding, dict[str, Path]],
] = OrderedDict()


def _lstat_identity(path: Path) -> tuple[int, int, int]:
    status = path.lstat()
    return (status.st_dev, status.st_ino, status.st_mode)


def _path_binding(root: Path, entries: tuple[tuple[str, Any], ...]) -> _PathBinding:
    """Where the root resolves, plus every component between it and each storage path.

    Each component's lstat identity (device, inode, mode) is recorded, so a
    directory -- or any ancestor below the root -- moved away and replaced by a
    symlink or another inode changes the binding even when the leaf keeps its
    inode, and the root's own resolution covers the ancestors above it. This is
    one ``realpath`` plus a few ``lstat`` calls rather than a ``realpath`` of
    every storage path.
    """

    components = []
    for _name, relative in entries:
        current = root
        identities = []
        for part in Path(str(relative)).parts:
            current = current / part
            status = current.lstat()
            identities.append((status.st_dev, status.st_ino, status.st_mode))
        components.append(tuple(identities))
    return os.path.realpath(root), tuple(components)


# Live ledger compaction (repack, pack-refs, commit-graph) runs once per this
# many accepted generations, on the advisory thread after a write; the daemon
# also compacts and prunes each instance at start, before serving.
LEDGER_COMPACTION_STRIDE = 64
# At start, a ledger with fewer loose objects than this is left as it is.
LEDGER_START_COMPACTION_LOOSE_OBJECTS = 256

_log = structlog.get_logger("cruxible.ledger_upkeep")


def _generation_at(recovered: RecoveredInstanceState, sequence: int) -> RecoveredGeneration:
    """The replayed generation at ``sequence`` (history is held in sequence order)."""

    if 0 <= sequence < len(recovered.history):
        generation = recovered.history[sequence]
        if generation.sequence == sequence:
            return generation
    return next(item for item in recovered.history if item.sequence == sequence)


class PlaybillInstance:
    """A verified opt-in Cruxible substrate rooted outside agent workspaces."""

    def __init__(
        self,
        root: Path,
        descriptor: Descriptor,
        trust_root: TrustRoot,
        ledger: GitLedger,
        verified_genesis: VerifiedGenesis,
        recovered: RecoveredInstanceState,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    ) -> None:
        self.root = root
        self.descriptor = descriptor
        self.trust_root = trust_root
        self._ledger = ledger
        self._verified_genesis = verified_genesis
        self._recovered = recovered
        self._state_lock = threading.RLock()
        #: The daemon's live wall clock for evidence-plane recording (each
        #: re-observation's recording instant). Never an execution's frozen
        #: evaluation clock; every run, dispatch, nested level and recovery
        #: records through it. Tests replace it here.
        self.recording_clock: Callable[[], datetime] = utc_now
        self.derived = DerivedState()
        #: What rebuildable and raw operational state is kept (projection
        #: generations, raw-receipt days). Tests replace it here.
        self.retention: RetentionSettings = DEFAULT_RETENTION
        # Compiled semantic facts per owner version; see TypedStateReader.facts_for.
        self._owner_facts = self.derived.memo(
            "owner-facts", max_entries=65536, max_bytes=64 * 1024 * 1024
        )
        # Parsed Claim law evidence per (path, accepting sequence).
        self.claim_law_memo: BoundedCache[bytes] = self.derived.memo(
            "claim-law-evidence", max_entries=65536, max_bytes=64 * 1024 * 1024
        )
        self.prepared_evaluations = PreparedEvaluationAdapter(
            self.derived, fact_inputs=self.query_fact_inputs
        )
        for name, namespace, adapter, source in (
            ("accepted-artifacts", "accepted", self.derived, "verified-ledger-tree-v1"),
        ):
            self.derived.register(IndexDefinition(name, namespace, "1", source), adapter)
        self._accepted_history_index = AcceptedHistoryIndex(
            self._validated_paths(root, descriptor.storage)["projections"] / "history.sqlite3"
        )
        self.derived.register(
            IndexDefinition("accepted-history", "accepted", "1", "verified-ledger-history-v1"),
            self._accepted_history_index,
        )
        self._promotion_verifier = promotion_verifier
        self._claim_attestation_store: ClaimAttestationEvidenceStore | None = None
        self._workspace_advertiser: Callable[[], WorkspaceAdvertisement] | None = None
        self._workspace_path: Path | None = None
        # Advisory ref refreshes run after the write that asked for them. Requests
        # made while one runs fold into a single following run.
        self._advertisement_condition = threading.Condition()
        self._advertisement_thread: threading.Thread | None = None
        self._advertisement_requested = 0
        self._advertisement_completed = 0
        self._last_advertisement: WorkspaceAdvertisement | None = None
        self._receive_limits = ProposalReceiveLimits()
        self._mirror_condition = threading.Condition()
        self._mirror_thread: threading.Thread | None = None
        # The compaction stride (head sequence // stride) last compacted at;
        # None until the first advisory pass after open records it.
        self._compacted_stride: int | None = None
        # The accepted day consumption receipts were last folded on.
        self._consumption_folded_on: date | None = None
        # Projection eviction: the cutoff sequence already applied (None until
        # the first pass in this process). A piece at or before it that a
        # handle here held is tried again when the hold is released.
        self._eviction_lock = threading.Lock()
        self._evicted_through: int | None = None
        # Retired accepted candidates rebuilt from their records, by digest.
        self._settled_candidates: dict[str, SettledCandidate] = {}
        self._quiet_checkpoints: QuietCheckpointWriter | None = None
        # Read services keyed by accepted coordinate park their derived
        # history indexes here so activation drops them with one clear().
        # Immutable-coordinate exports survive head movement; keys include their
        # review-context snapshot and access profile. Bounded by the floor service.
        self.floor_structure_memo: OrderedDict[tuple[object, ...], object] = OrderedDict()
        # The last export's per-Subject current/ renders, so the next export at a
        # later coordinate re-renders only what the change records touched.
        self.floor_current_memo: OrderedDict[tuple[object, ...], object] = OrderedDict()
        self._body_store_cache: (
            tuple[tuple[Path, Path, tuple[int, int, int] | None], ContentAddressedBodyStore] | None
        ) = None
        # Verified retained change-set records, keyed by their full accepted
        # location. Their bytes are immutable, so they survive head movement.
        self.verified_change_set_records: OrderedDict[
            AcceptedGenerationLocation, VerifiedRecord
        ] = OrderedDict()
        # Their small derived facts (actor binding, instant, approvals), shared
        # without copies so a hit never copies a whole change-set record.
        self.verified_record_facts: OrderedDict[
            AcceptedGenerationLocation, GenerationRecordFacts
        ] = OrderedDict()

    @staticmethod
    def _accepted_query_facts(
        source: object,
        coordinate: AcceptedProjectionCoordinate,
        *,
        predicates: tuple[str, ...] | None = None,
        subject_kinds: tuple[str, ...] | None = None,
    ) -> "ClaimQueryFactsV1":
        """Build the one accepted-Claim facts projection for live and replay paths.

        Inside a request (``projection_scope``) a live instance hands the same
        facts to every evaluation at one coordinate that reads the same
        predicates and Subject kinds -- a settle's delivery check, admission and
        activation -- while nothing they were assembled from has moved.
        """

        from cruxible_core.service.discovery.query import build_accepted_query_facts
        from cruxible_core.service.evidence.evidence import ClaimReadSourceProtocol

        pool = _PROJECTION_POOL.get()
        if pool is not None and predicates is not None and isinstance(source, PlaybillInstance):
            return source._request_query_facts(
                pool, coordinate, predicates=predicates, subject_kinds=subject_kinds
            )
        return build_accepted_query_facts(
            cast(ClaimReadSourceProtocol, source),
            coordinate=coordinate,
            predicates=predicates,
            subject_kinds=subject_kinds,
        )

    def query_fact_inputs(self) -> str | None:
        """The writer-owned fingerprint of every mutable input accepted query facts read.

        At one accepted coordinate a facts build varies only with the body store
        and the re-observation journal; this names both (``verdict_input_fingerprint``).
        A build observed under one value is replayable only while it holds.
        """

        from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint

        return verdict_input_fingerprint(self)

    def _request_query_facts(
        self,
        pool: ProjectionPool,
        coordinate: AcceptedProjectionCoordinate,
        *,
        predicates: tuple[str, ...],
        subject_kinds: tuple[str, ...] | None,
    ) -> "ClaimQueryFactsV1":
        """Facts remembered for this request while their mutable inputs hold.

        At one accepted coordinate a fact row varies only with the body store
        and the re-observation journal. The verdict input fingerprint names
        both by writer-owned positions: the landing log's (every evidence body
        landing, erase and damage record) and the re-observation journal's.
        A remembered build is served while that fingerprint is unchanged, in
        bounded work: no consulted body is enumerated or stat'ed (presence
        comes from the landing index; out-of-band corruption is found by
        damage records and direct byte verification, never by per-read file
        checks). A build that consulted an external reader is never remembered.
        """

        from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint
        from cruxible_core.service.discovery.query import build_accepted_query_facts
        from cruxible_core.service.evidence.evidence import UNKNOWN_BODIES

        memo = pool.values()
        fingerprint = verdict_input_fingerprint(self)
        if memo is None or fingerprint is None:
            return build_accepted_query_facts(
                self, coordinate=coordinate, predicates=predicates, subject_kinds=subject_kinds
            )
        key = (
            "accepted-query-facts",
            id(self),
            coordinate.git_oid,
            coordinate.semantic_root,
            coordinate.generation_root,
            coordinate.compiler.rule_digest,
            predicates,
            subject_kinds,
        )
        held = memo.get(key)
        if held is not None:
            kept, seen = held
            if seen == fingerprint:
                return cast("ClaimQueryFactsV1", kept)
        consulted: dict[str, Any] = {}
        facts = build_accepted_query_facts(
            self,
            coordinate=coordinate,
            predicates=predicates,
            subject_kinds=subject_kinds,
            consulted_bodies=consulted,
        )
        if UNKNOWN_BODIES not in consulted and verdict_input_fingerprint(self) == fingerprint:
            memo[key] = (facts, fingerprint)
        else:
            memo.pop(key, None)
        return facts

    @classmethod
    def initialize(
        cls,
        root: Path,
        *,
        instance_id: str,
        client_principals: Sequence[PrincipalRecord],
        workspace_roots: Sequence[Path],
        git_object_format: GitObjectFormat = DEFAULT_GIT_OBJECT_FORMAT,
        operating_profile: OperatingProfile = "local",
        require_independent_approval: bool = False,
        timestamp: str | None = None,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    ) -> "PlaybillInstance":
        """Create and reopen one managed instance from explicit bootstrap inputs."""

        if not root.is_absolute():
            raise BootstrapError("managed Cruxible root must be absolute")
        managed_root = _resolved(root)
        for workspace in workspace_roots:
            if _is_within(managed_root, _resolved(workspace)):
                raise BootstrapError(
                    "managed Cruxible root must be outside every declared agent workspace"
                )
        if managed_root.exists():
            raise BootstrapError(f"managed Cruxible root already exists: {managed_root}")

        clients, recovery_posture = _validate_client_principals(client_principals)
        if (
            require_independent_approval
            and sum(record.kind == "ordinary" and record.status == "active" for record in clients)
            < 2
        ):
            raise BootstrapError(
                "independent approval requires at least two active ordinary principals"
            )
        layout = StorageLayout()
        managed_root.parent.mkdir(parents=True, exist_ok=True)
        managed_root.mkdir(mode=0o700)
        os.chmod(managed_root, 0o700)
        try:
            for relative in (
                layout.projections,
                layout.cas,
                layout.exhaust,
                layout.leases,
            ):
                directory = managed_root / relative
                directory.mkdir(mode=0o700)
                os.chmod(directory, 0o700)

            credentials = managed_root / layout.credentials
            daemon_material = generate_daemon_key(credentials)
            principals = tuple(
                sorted((daemon_material.principal, *clients), key=lambda item: item.principal_id)
            )
            trust_root = TrustRoot(
                instance_id=instance_id,
                daemon_public_key=daemon_material.principal.public_key,
                principals=principals,
            )

            ledger = GitLedger.initialize(
                managed_root / layout.ledger,
                object_format=git_object_format,
                signing_key_path=daemon_material.private_key_path,
                allowed_signers_path=credentials / ALLOWED_SIGNERS_FILE,
            )
            commit_timestamp = timestamp or format_datetime(utc_now())
            if commit_timestamp is None:
                raise BootstrapError("failed to produce a canonical bootstrap timestamp")
            verified = prepare_genesis(
                ledger,
                trust_root=trust_root,
                approval_policy=ApprovalPolicy(
                    mode=(
                        "independent_approval_required"
                        if require_independent_approval
                        else "self_approval_allowed"
                    )
                ),
                procedure_runtime_policy=seeded_procedure_runtime_policy(),
                # The instance starts with the newest historical seed set its
                # compiler admits; the seeded artifacts are governed from here on.
                seed_set=current_genesis_seed_set(
                    entry.kind
                    for entry in artifact_kinds_for_compiler(
                        current_compiler_coordinate()
                    ).entries()
                ),
                timestamp=commit_timestamp,
            )
            descriptor = Descriptor(
                instance_id=instance_id,
                git_object_format=git_object_format,
                daemon_public_key=trust_root.daemon_public_key,
                compiler=current_compiler_coordinate(),
                authority=initial_authority_matrix(),
                operating_profile=operating_profile,
                recovery_posture=recovery_posture,
                storage=layout,
                genesis=GenesisCoordinate(
                    git_oid=verified.oid,
                    bootstrap_root=verified.bootstrap_root.tagged,
                    semantic_root=verified.semantic_root.tagged,
                    generation_root=verified.generation_root.tagged,
                ),
            )
            _exclusive_write(
                managed_root / DESCRIPTOR_FILE,
                canonical_bytes(descriptor.model_dump(mode="json")) + b"\n",
            )
            _fsync_directory(managed_root)
            return cls.open(
                managed_root,
                trust_root=trust_root,
                promotion_verifier=promotion_verifier,
            )
        except BaseException:
            # `managed_root` was proven absent and created by this invocation.
            # Removing this exact incomplete target cannot affect prior user data.
            shutil.rmtree(managed_root)
            raise

    @classmethod
    def open(
        cls,
        root: Path,
        *,
        trust_root: TrustRoot,
        witness: WitnessSink | None = None,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    ) -> "PlaybillInstance":
        """Reopen only after replaying descriptor, key, signature, and both roots."""

        managed_root = _resolved(root)
        descriptor_path = managed_root / DESCRIPTOR_FILE
        if descriptor_path.is_symlink():
            raise FormatError("Cruxible descriptor may not be a symlink")
        try:
            raw_descriptor = descriptor_path.read_bytes()
            payload = json.loads(raw_descriptor)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FormatError("Cruxible descriptor is missing or malformed") from exc
        if not isinstance(payload, dict):
            raise FormatError("Cruxible descriptor must be a JSON object")
        if payload.get("format_version") != 1 or payload.get("tag") != "playbill-instance-v1":
            raise FormatError(
                "unsupported Cruxible descriptor version; upgrade or use an explicit fork/import"
            )
        if payload.get("git_object_format") not in {"sha1", "sha256"}:
            raise FormatError("unsupported Cruxible Git object format; use an explicit fork/import")
        try:
            descriptor = Descriptor.model_validate(payload)
        except ValidationError as exc:
            raise FormatError("Cruxible descriptor failed strict validation") from exc
        if canonical_bytes(descriptor.model_dump(mode="json")) + b"\n" != raw_descriptor:
            raise FormatError("Cruxible descriptor is not canonical")
        if descriptor.instance_id != trust_root.instance_id:
            raise BootstrapError("descriptor instance ID differs from trust root")
        if descriptor.daemon_public_key != trust_root.daemon_public_key:
            raise BootstrapError("descriptor daemon key differs from trust root")
        if descriptor.compiler not in SUPPORTED_COMPILERS:
            raise FormatError("descriptor compiler coordinate is unsupported")
        if descriptor.authority != initial_authority_matrix():
            raise BootstrapError(
                "PB-A descriptor authority matrix differs from the initial migration state"
            )
        if descriptor.storage != StorageLayout():
            raise FormatError(
                "descriptor storage layout is unsupported; use an explicit fork/import"
            )
        recovery_configured = any(
            principal.kind == "recovery" for principal in trust_root.principals
        )
        expected_recovery: RecoveryPosture = (
            "recovery-configured" if recovery_configured else "narrowed-no-recovery"
        )
        if descriptor.recovery_posture != expected_recovery:
            raise BootstrapError("descriptor recovery posture differs from bootstrap principals")

        paths = cls._validated_paths(managed_root, descriptor.storage)
        private_key_path = paths["credentials"] / DAEMON_PRIVATE_KEY_FILE
        public_key_path = paths["credentials"] / DAEMON_PUBLIC_KEY_FILE
        allowed_signers_path = paths["credentials"] / ALLOWED_SIGNERS_FILE
        for name, path in (
            ("daemon private key", private_key_path),
            ("daemon public key", public_key_path),
            ("allowed signers", allowed_signers_path),
        ):
            if path.is_symlink() or not path.is_file():
                raise SigningKeyError(f"managed {name} must be a regular file")
        if public_key_hex_from_private_file(private_key_path) != trust_root.daemon_public_key:
            raise SigningKeyError("daemon private key does not match the trust root")
        if (
            raw_public_key_hex_from_openssh(public_key_path.read_bytes())
            != trust_root.daemon_public_key
        ):
            raise SigningKeyError("daemon public key does not match the trust root")
        private_mode = stat.S_IMODE(private_key_path.stat().st_mode)
        if private_mode & 0o077:
            raise SigningKeyError("daemon private key permissions expose group/world access")

        ledger = GitLedger(
            paths["ledger"],
            signing_key_path=private_key_path,
            allowed_signers_path=allowed_signers_path,
        )
        if ledger.object_format() != descriptor.git_object_format:
            raise FormatError(
                "ledger object format differs from descriptor; object-format changes require "
                "an explicit fork/import"
            )
        verified = verify_genesis(
            ledger,
            descriptor.genesis.git_oid,
            trust_root=trust_root,
        )
        expected = GenesisCoordinate(
            git_oid=verified.oid,
            bootstrap_root=verified.bootstrap_root.tagged,
            semantic_root=verified.semantic_root.tagged,
            generation_root=verified.generation_root.tagged,
        )
        if expected != descriptor.genesis:
            raise BootstrapError("recorded genesis coordinates do not reproduce")
        bodies = ContentAddressedBodyStore(paths["cas"])
        recovered = recover_instance(
            ledger,
            genesis=verified,
            instance_id=descriptor.instance_id,
            object_format=descriptor.git_object_format,
            compiler=descriptor.compiler,
            publication_directory=paths["projections"],
            bodies=bodies,
            witness=witness,
            promotion_verifier=promotion_verifier,
            producer_receipt_resolver=local_producer_receipt_resolver(
                exhaust_root=paths["exhaust"],
                instance_id=descriptor.instance_id,
                bodies=bodies,
                full_journal=True,
            ),
            query_facts_builder=cls._accepted_query_facts,
            checkpoint_directory=cls._checkpoint_directory(managed_root),
        )
        instance = cls(
            managed_root,
            descriptor,
            trust_root,
            ledger,
            verified,
            recovered,
            promotion_verifier,
        )
        if not is_previewing():
            instance._housekeeping("projection_eviction", instance.evict_projection_pieces)
        if (
            descriptor.mirror_url is not None
            and descriptor.decommissioned is None
            and not is_previewing()
        ):
            instance.request_ledger_mirror()
        return instance

    @staticmethod
    def _checkpoint_directory(root: Path) -> Path:
        """Locate the rebuildable checkpoint cache beside the managed storage roots.

        Deliberately not a `StorageLayout` field: the layout is part of the
        frozen `playbill-instance-v1` descriptor preimage, and a disposable local
        cache may never widen an accepted format. The directory is created on
        first write, may be deleted at any time, and `inspect()` never reports it
        as semantic storage.
        """

        return root / CHECKPOINT_DIRECTORY

    @staticmethod
    def _validated_paths(root: Path, layout: StorageLayout) -> dict[str, Path]:
        """Resolve and confine every managed storage directory.

        A full validation is remembered with the path binding that reached it
        (see ``_path_binding``). Later calls reuse the result only while that
        binding is unchanged; a directory or any ancestor replaced by a symlink or
        another inode takes the full validation again.
        """
        entries = tuple(layout.model_dump().items())
        key = (str(root), entries)
        remembered = memo_get(_VALIDATED_PATHS, key)
        if remembered is not None:
            binding, cached = remembered
            try:
                current: _PathBinding | None = _path_binding(root, entries)
            except OSError:
                current = None
            if current == binding:
                return dict(cached)
        paths = PlaybillInstance._validate_paths(root, entries)
        try:
            binding = _path_binding(root, entries)
        except OSError:
            return paths
        memo_put(_VALIDATED_PATHS, key, (binding, dict(paths)), capacity=_VALIDATED_PATH_CAPACITY)
        return paths

    @staticmethod
    def _validate_paths(root: Path, entries: tuple[tuple[str, Any], ...]) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for name, relative in entries:
            path = root / relative
            if path.is_symlink():
                raise FormatError(f"managed storage path may not be a symlink: {name}")
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise FormatError(f"managed storage path is missing: {name}") from exc
            if not _is_within(resolved, root) or not resolved.is_dir():
                raise FormatError(f"managed storage path escapes instance root: {name}")
            paths[name] = resolved
        return paths

    def inspect(self) -> Inspection:
        """Return public coordinates and custody posture without key locations."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        principals = tuple(
            PrincipalInspection(
                principal_id=record.principal_id,
                algorithm=record.algorithm,
                kind=record.kind,
                status=record.status,
                public_key_digest=record.public_key_digest,
            )
            for record in self._recovered.head.principals.principals
        )
        storage_directories = {
            name: str(path) for name, path in paths.items() if name != "credentials"
        }
        policy_bytes = self.blob_at(self._recovered.head.oid, APPROVAL_POLICY_PATH)
        if policy_bytes is None:
            raise FormatError("accepted approval policy is absent")
        approval_policy = parse_approval_policy(
            policy_bytes,
            path=APPROVAL_POLICY_PATH,
        )
        return Inspection(
            descriptor_tag=self.descriptor.tag,
            format_version=self.descriptor.format_version,
            instance_id=self.descriptor.instance_id,
            git_object_format=self.descriptor.git_object_format,
            head_oid=self._recovered.head.oid,
            bootstrap_root=self._verified_genesis.bootstrap_root.tagged,
            semantic_root=self._recovered.head.semantic_root.tagged,
            generation_root=self._recovered.head.generation_root.tagged,
            compiler=self.accepted_coordinate().compiler,
            authority=self.descriptor.authority,
            operating_profile=self.descriptor.operating_profile,
            recovery_posture=self.descriptor.recovery_posture,
            approval_policy_mode=approval_policy.mode,
            principals=principals,
            managed_root=str(self.root),
            storage_directories=storage_directories,
            daemon_private_key_present=(paths["credentials"] / DAEMON_PRIVATE_KEY_FILE).is_file(),
        )

    @contextmanager
    def accepted_head_held(self) -> Iterator[str]:
        """Hold accepted main where it is (the activation lock) and yield its oid.

        A change pinned to a preview's coordinate (R12 ``at``) confirms the
        head inside this and writes before leaving it, so no acceptance can
        land between the check and the write. Nothing inside may take the
        activation lock again.
        """

        with self._ledger.activation_lock():
            yield self._ledger.read_main()

    def accepted_coordinate(self) -> AcceptedProjectionCoordinate:
        """Return the verified accepted coordinate without consulting proposal refs."""

        return self._recovered.coordinate

    def _accepted_coordinates_by_sequence(self) -> dict[int, AcceptedCoordinate]:
        """Return replay-proven historical coordinates for stable derived activations."""

        return {
            generation.sequence: AcceptedCoordinate(
                git_oid=generation.oid,
                semantic_root=generation.semantic_root.tagged,
                generation_root=generation.generation_root.tagged,
                compiler_digest=generation.compiler.rule_digest,
            )
            for generation in self._recovered.history
        }

    def projection_assembler(self) -> ProjectionAssembler:
        """Bind PB-B's internal assembler to this already-verified generation."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ProjectionAssembler(
            self._ledger,
            accepted=self.accepted_coordinate(),
            publication_directory=paths["projections"],
            bodies=ContentAddressedBodyStore(paths["cas"]),
            accepted_coordinates_by_sequence=self._accepted_coordinates_by_sequence(),
            resolve_claim_digest=self._claim_identities_for_digest,
        )

    def _claim_identities_for_digest(self, digest: str) -> tuple[str, ...]:
        with self.accepted_history_reader() as history:
            return history.identities_for_digest(digest)

    def body_store(self) -> ContentAddressedBodyStore:
        """Return PB-C's inert, access-controlled content-addressed body store.

        Inside ``dry_run_bodies()`` the store holds what it is asked to store in
        memory instead, so a dry run takes the write's own path and writes nothing.
        """

        store = self._disk_body_store()
        held = dry_run_held_bodies()
        if held is None:
            return store
        return cast(ContentAddressedBodyStore, DryRunBodyStore(store, held))

    def _disk_body_store(self) -> ContentAddressedBodyStore:
        paths = self._validated_paths(self.root, self.descriptor.storage)
        try:
            algorithm = _lstat_identity(paths["cas"] / "sha256")
        except OSError:
            algorithm = None
        key = (paths["cas"], paths["leases"], algorithm)
        cached = self._body_store_cache
        if algorithm is not None and cached is not None and cached[0] == key:
            return cached[1]
        store = ContentAddressedBodyStore(
            paths["cas"],
            reservation_root=paths["leases"] / "procedure-material",
        )
        try:
            key = (paths["cas"], paths["leases"], _lstat_identity(paths["cas"] / "sha256"))
        except OSError:
            return store
        # The store holds only confined, resolved paths; reuse it while they and
        # its algorithm directory keep the same identity.
        self._body_store_cache = (key, store)
        return store

    @property
    def is_decommissioned(self) -> bool:
        """Return whether this instance has reached its terminal lifecycle state."""

        return self.descriptor.decommissioned is not None

    def require_writable(self) -> None:
        """Refuse typed when this instance no longer accepts governed writes."""

        terminal = self.descriptor.decommissioned
        if terminal is None:
            return
        raise InstanceDecommissioned(
            instance_id=self.descriptor.instance_id,
            reason=terminal.reason,
            decommissioned_at=terminal.decommissioned_at,
        )

    def _persisted_descriptor(self) -> Descriptor:
        """Read the descriptor from disk rather than from this handle.

        Two handles can be open over one directory, and the two operational
        fields a descriptor carries -- the terminal decommission record and the
        mirror URL -- are written by different verbs. Merging an update into
        THIS handle's in-memory copy would silently drop whatever the other
        handle wrote, so every write starts from the bytes on disk.
        """

        try:
            payload = json.loads((self.root / DESCRIPTOR_FILE).read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FormatError("Cruxible descriptor is missing or malformed") from exc
        try:
            return Descriptor.model_validate(payload)
        except ValidationError as exc:
            raise FormatError("Cruxible descriptor failed strict validation") from exc

    def _rewrite_descriptor(self, **updates: object) -> Descriptor:
        """Persist one operational descriptor change over the bytes on disk."""

        refuse_write_while_previewing("instance descriptor")
        updated = self._persisted_descriptor().model_copy(update=updates)
        _atomic_replace(
            self.root / DESCRIPTOR_FILE,
            canonical_bytes(updated.model_dump(mode="json")) + b"\n",
        )
        self.descriptor = updated
        return updated

    def _persisted_decommission(self) -> Decommission | None:
        """Read the terminal record from disk rather than from this handle."""

        return self._persisted_descriptor().decommissioned

    def decommission(
        self,
        *,
        reason: str,
        decommissioned_by: str,
        confirm_head: Callable[[str], None] | None = None,
    ) -> Decommission:
        """Stamp the terminal lifecycle state on the descriptor, deleting nothing.

        The record lands in the descriptor, which every reopen replays and
        re-renders canonically, so the state survives a daemon restart without a
        separate operational store that could disagree with it. Repeating the
        call is refused rather than silently restamping: a second reason would
        overwrite the first without a record.

        ``confirm_head`` is called with the accepted head under the activation
        lock, immediately before the stamp, so a head pinned by a preview
        cannot move between the check and the write (R12).
        """

        self.require_writable()
        # Two handles can be open over one directory. This handle's in-memory
        # descriptor is not authority for a terminal state another handle may
        # already have stamped, so the persisted record decides.
        persisted = self._persisted_decommission()
        if persisted is not None:
            self.descriptor = self.descriptor.model_copy(update={"decommissioned": persisted})
            self.require_writable()
        record = Decommission(
            reason=reason,
            decommissioned_at=format_datetime(utc_now()) or "",
            decommissioned_by=decommissioned_by,
        )
        if is_previewing():
            # R12: the record the commit would stamp, and nothing stamped.
            return record
        with self._ledger.activation_lock():
            if confirm_head is not None:
                confirm_head(self._ledger.read_main())
            self._rewrite_descriptor(decommissioned=record)
        return record

    def ledger_mirror_url(self) -> str | None:
        """Return where this ledger publishes itself, or None if it publishes nowhere."""

        return self.descriptor.mirror_url

    def set_ledger_mirror(
        self, url: str, *, confirm_head: Callable[[str], None] | None = None
    ) -> LedgerMirrorStateV1 | None:
        """Bind a remote and publish to it at once, so a bad one is found now.

        Setting a mirror without pushing to it would leave the operator holding
        a URL whose credential, reachability and permissions are all unverified
        until the next governed write happens to exercise them. Publishing here
        makes the answer immediate, and it is still only a publication: a push
        that fails records the reason and leaves the mirror bound, because a
        remote that is temporarily unreachable is not a wrong remote.
        """

        self.require_writable()
        validated = validate_mirror_url(url)
        if is_previewing():
            # R12: the URL is checked and nothing is bound or sent.
            return None
        # A pinned binding is confirmed against the accepted head under the
        # activation lock, so the head the preview saw is the head bound at.
        with self._ledger.activation_lock():
            if confirm_head is not None:
                confirm_head(self._ledger.read_main())
            self._rewrite_descriptor(mirror_url=validated)
        return self.publish_ledger_mirror()

    def clear_ledger_mirror(
        self, *, confirm_head: Callable[[str], None] | None = None
    ) -> str | None:
        """Unbind the mirror so nothing more is published, returning the URL it had.

        What was already sent stays on the remote: clearing stops future
        publication and forgets the last attempt, it calls nothing back. Clearing
        an instance with no mirror is a no-op that returns None.
        """

        self.require_writable()
        previous = self._persisted_descriptor().mirror_url
        if previous is None or is_previewing():
            return previous
        with self._ledger.activation_lock():
            if confirm_head is not None:
                confirm_head(self._ledger.read_main())
            # The state lock orders this against a publisher deciding whether
            # the descriptor still names the remote it is about to push to.
            with mirror_lock(self.root):
                self._rewrite_descriptor(mirror_url=None)
                clear_mirror_state(self.root)
        with self._mirror_condition:
            self._mirror_condition.notify_all()
        return previous

    def ledger_mirror_state(self) -> LedgerMirrorStateV1 | None:
        """Report acknowledgement and local ref lag, including note-only changes."""

        state = read_mirror_state(self.root)
        if state is not None and state.status == "current":
            try:
                if state.published_refs != self._ledger.mirror_refs():
                    return state.model_copy(
                        update={
                            "status": "pending",
                            "detail": "local review refs await publication",
                        }
                    )
            except Exception:  # noqa: BLE001 - publication status cannot refuse a read
                return state.model_copy(
                    update={"status": "behind", "detail": "cannot inspect local publication refs"}
                )
        return state

    def request_ledger_mirror(self) -> LedgerMirrorStateV1 | None:
        """Mark publication pending and wake a worker without waiting for Git/network.

        This is called only after durable local work. Queue failure cannot undo
        that work; reopening reconciles from the ledger and evidence stores.
        """

        url = self.ledger_mirror_url()
        try:
            url = self._persisted_descriptor().mirror_url
            if url is None:
                return None
            with mirror_lock(self.root):
                url = Descriptor.model_validate_json(
                    (self.root / DESCRIPTOR_FILE).read_bytes()
                ).mirror_url
                if url is None:
                    return None
                previous = read_mirror_state(self.root)
                state = (
                    previous
                    if previous is not None and previous.url == url
                    else LedgerMirrorStateV1(
                        url=url, status="pending", attempted_at=format_datetime(utc_now()) or ""
                    )
                )
                state = state.model_copy(
                    update={
                        "requested_sequence": state.requested_sequence + 1,
                        "status": "pending",
                        "detail": None,
                        "wait_sequence": None,
                    }
                )
                write_mirror_state(self.root, state)
            with self._mirror_condition:
                if self._mirror_thread is None or not self._mirror_thread.is_alive():
                    self._mirror_thread = threading.Thread(
                        target=Context().run,
                        args=(self._run_ledger_publisher,),
                        name=f"ledger-publisher-{self.descriptor.instance_id}",
                        daemon=True,
                    )
                    self._mirror_thread.start()
                self._mirror_condition.notify_all()
            return state
        except Exception as exc:  # noqa: BLE001 - an accepted write must still succeed
            with self._mirror_condition:
                if self._mirror_thread is not None and not self._mirror_thread.is_alive():
                    self._mirror_thread = None
            if url is None:
                return None
            return LedgerMirrorStateV1(
                url=url,
                status="behind",
                attempted_at=format_datetime(utc_now()) or "",
                detail=f"publication scheduling failed: {type(exc).__name__}"[:500],
            )

    def publish_ledger_mirror(self, *, timeout: float = 60.0) -> LedgerMirrorStateV1 | None:
        """Wait at most timeout for this request's publication, returning its watermark.

        A newer request may remain pending after this barrier is satisfied.
        Compare published_sequence with wait_sequence for this call's result.
        """

        if not 0 <= timeout <= 60:
            raise ValueError("publication timeout must be between 0 and 60 seconds")
        deadline = time.monotonic() + timeout
        requested = self.request_ledger_mirror()
        if requested is None:
            return None
        sequence = requested.requested_sequence
        if requested.status == "behind":
            return requested.model_copy(update={"wait_sequence": None})
        with self._mirror_condition:
            while True:
                state = read_mirror_state(self.root) or requested
                remaining = deadline - time.monotonic()
                if state.url != requested.url:
                    return requested.model_copy(
                        update={
                            "status": "behind",
                            "wait_sequence": sequence,
                            "detail": "publication wait interrupted: mirror destination changed",
                        }
                    )
                if (
                    state.published_sequence >= sequence
                    or (state.attempted_sequence >= sequence and state.status == "behind")
                    or remaining <= 0
                ):
                    return state.model_copy(update={"wait_sequence": sequence})
                # Another process's publisher cannot signal our condition.
                self._mirror_condition.wait(min(remaining, 0.1))

    def _run_ledger_publisher(self) -> None:
        failures = 0
        previous_sequence = -1
        observed: tuple[str, int] | None = None
        try:
            while True:
                before = read_mirror_state(self.root)
                observed = None if before is None else (before.url, before.requested_sequence)
                state = self._publish_ledger_mirror_once()
                with self._mirror_condition:
                    self._mirror_condition.notify_all()
                    current = read_mirror_state(self.root)
                    if current is None or state is None:
                        return
                    if current.requested_sequence > state.attempted_sequence:
                        failures = 0
                        continue
                    if state.status == "behind":
                        failures = (
                            failures + 1 if previous_sequence == state.attempted_sequence else 1
                        )
                        previous_sequence = state.attempted_sequence
                        if failures < 3:
                            self._mirror_condition.wait(0.25 if failures == 1 else 1.0)
                            continue
                    return
        except Exception as exc:  # noqa: BLE001 - contain operational storage failures
            try:
                with mirror_lock(self.root):
                    current = read_mirror_state(self.root)
                    if current is not None:
                        write_mirror_state(
                            self.root,
                            current.model_copy(
                                update={
                                    "status": "behind",
                                    "attempted_sequence": current.requested_sequence,
                                    "detail": f"publication worker failed: {type(exc).__name__}",
                                }
                            ),
                        )
            except Exception:  # noqa: BLE001 - no writable status store remains
                pass
        finally:
            with self._mirror_condition:
                self._mirror_thread = None
                current = read_mirror_state(self.root)
                # A request can arrive between our last decision to stop and
                # this finalization. Start its worker under the same condition
                # used by request_ledger_mirror so that wakeup cannot be lost.
                if current is not None and (
                    observed is None
                    or current.url != observed[0]
                    or current.requested_sequence > observed[1]
                ):
                    self._mirror_thread = threading.Thread(
                        target=Context().run,
                        args=(self._run_ledger_publisher,),
                        name=f"ledger-publisher-{self.descriptor.instance_id}",
                        daemon=True,
                    )
                    try:
                        self._mirror_thread.start()
                    except RuntimeError:
                        # Resource exhaustion is operational. Leave the request
                        # pending and allow the next explicit request/reopen to
                        # start a worker, instead of retaining a dead handle.
                        self._mirror_thread = None
                self._mirror_condition.notify_all()

    def _retired_mirror_proposal(self, ref: str, oid: str) -> bool:
        """Bind a remote-only alias to its indexed closed proposal; Git proves retention."""
        evidence = self.proposal_evidence()
        pid = "sha256:" + ref.removeprefix("refs/heads/proposals/")
        coordinate = AcceptedCoordinate.from_internal(self.accepted_coordinate())
        with self.accepted_history_reader(at=coordinate) as history:
            assert evidence.index is not None
            with evidence.index.read(evidence, review_context=True) as connection:
                row = connection.execute(
                    "SELECT p.*, EXISTS(SELECT 1 FROM accepted_generations g "
                    "WHERE g.candidate_digest=p.candidate_digest "
                    "AND g.sequence<=?) AS accepted "
                    "FROM proposals p WHERE proposal_id=?",
                    (history.sequence, pid),
                ).fetchone()
            if row is None or row["review_commit_oid"] != oid:
                return False
            if row["candidate_parent_semantic_root"] is None:
                return False
            return bool(
                row["accepted"]
                or row["withdrawal_path"] is not None
                or row["candidate_parent_semantic_root"] != coordinate.semantic_root
            )

    def _publish_ledger_mirror_once(self) -> LedgerMirrorStateV1 | None:
        """Serialize publication across handles/processes and acknowledge exact refs."""

        with mirror_lock(self.root, publication=True):
            with mirror_lock(self.root):
                state = read_mirror_state(self.root)
                if state is None:
                    return None
                # Use current durable configuration, not a stale instance handle.
                descriptor = Descriptor.model_validate_json(
                    (self.root / DESCRIPTOR_FILE).read_bytes()
                )
                if descriptor.mirror_url != state.url:
                    return None
                if (
                    state.published_sequence >= state.requested_sequence
                    and state.status == "current"
                ):
                    return state
                sequence = state.requested_sequence
                state = state.model_copy(update={"status": "publishing"})
                write_mirror_state(self.root, state)
            snapshot: dict[str, str] = {}
            try:
                self._reconcile_proposal_review_refs()
                snapshot = self._ledger.mirror_refs()
                with mirror_lock(self.root):
                    current = read_mirror_state(self.root)
                    if current is None or current.url != state.url:
                        return current
                    write_mirror_state(
                        self.root, current.model_copy(update={"attempted_refs": snapshot})
                    )
                detail = self._ledger.push_mirror(
                    state.url,
                    environment=mirror_credential_environment(state.url),
                    snapshot=snapshot,
                    expected_remote=state.published_refs,
                    previous_attempt=state.attempted_refs,
                    retired_proposal=self._retired_mirror_proposal,
                )
            except Exception as exc:  # noqa: BLE001 - remote failure never refuses local work
                detail = f"publication failed: {type(exc).__name__}"[:500]
            with mirror_lock(self.root):
                current = read_mirror_state(self.root)
                if current is None or current.url != state.url:
                    return current
                update: dict[str, object] = {
                    "attempted_at": format_datetime(utc_now()) or "",
                    "attempted_sequence": sequence,
                    "detail": detail,
                    "status": "behind"
                    if detail is not None
                    else ("current" if current.requested_sequence == sequence else "pending"),
                }
                if detail is None:
                    update.update(
                        published_sequence=sequence,
                        published_refs=snapshot,
                        published_main_oid=snapshot.get("refs/heads/main"),
                    )
                result = current.model_copy(update=update)
                write_mirror_state(self.root, result)
                return result

    def store_document_body(self, content: bytes) -> CasObjectMetadata:
        """Persist inert bytes without proposing or changing accepted state."""

        self.require_writable()
        return self.body_store().store(content)

    def accepted_principal_registry(
        self, coordinate: AcceptedProjectionCoordinate
    ) -> PrincipalRegistrySnapshot:
        """Read the public-key registry at an exact verified accepted snapshot."""
        if coordinate.git_oid == self._verified_genesis.oid:
            # Bootstrap has no served SQLite publication yet. Its signed registry
            # is already authenticated independently of all derived storage.
            verified = self.resolve_accepted_coordinate(
                git_oid=coordinate.git_oid,
                semantic_root=coordinate.semantic_root,
                generation_root=coordinate.generation_root,
                compiler_digest=coordinate.compiler.rule_digest,
            )
            if verified != coordinate:
                raise FormatError("principal registry coordinate differs from genesis")
            return PrincipalRegistrySnapshot(
                semantic_root=coordinate.semantic_root, principals=self._verified_genesis.principals
            )
        with self.bind_accepted_projection(coordinate) as projection:
            registry = projection.typed.principal_registry()
            generation = self._generation_for_oid(coordinate.git_oid)
            if generation is None or registry != generation.principals:
                raise PrincipalIntegrityError(
                    "indexed principals differ from the replay-verified accepted registry"
                )
            return registry

    def require_accepted_principal(
        self, coordinate: AcceptedProjectionCoordinate, principal_id: str
    ) -> None:
        """Check an admission actor with a single indexed principal lookup."""
        self.accepted_principal(AcceptedCoordinate.from_internal(coordinate), principal_id)

    def accepted_referent_coordinates(
        self, coordinate: AcceptedProjectionCoordinate
    ) -> frozenset[AcceptedCoordinate]:
        with self.accepted_history_reader(
            at=AcceptedCoordinate.from_internal(coordinate)
        ) as history:
            return history.accepted_coordinates()

    def accepted_principal(self, at: AcceptedCoordinate, principal_id: str) -> PrincipalRecord:
        """Read and authenticate one active principal at an exact accepted coordinate."""
        coordinate = self.resolve_accepted_coordinate(
            git_oid=at.git_oid,
            semantic_root=at.semantic_root,
            generation_root=at.generation_root,
            compiler_digest=at.compiler_digest,
        )
        if coordinate.git_oid == self._verified_genesis.oid:
            return self.accepted_principal_registry(coordinate).require_active(principal_id)
        with self.bind_accepted_projection(coordinate) as projection:
            principal = projection.typed.principal(principal_id, active=True)
            path = f"principals/{principal_id}.json"
            raw = projection.typed.member_bytes(path)
            if principal is None or principal != parse_principal_record(raw, path=path):
                raise PrincipalIntegrityError(
                    "indexed attestation principal differs from accepted member"
                )
            return principal

    def accepted_claim_law_evidence(
        self, coordinate: AcceptedCoordinate, identity: str, artifact_digest: str
    ) -> ClaimLawEvidenceAny | None:
        """Resolve one exact Claim admission through the shared history index."""
        path = claim_path(identity.removeprefix("Claim:"))
        with self.accepted_history_reader(at=coordinate) as history:
            evidence = history.read_claim_law_evidence(
                identity,
                artifact_digest=artifact_digest,
                path=path,
                load_record=self.retained_record_reader(),
            )
        if evidence is None:
            return None
        raw = evidence.result.get("claim_evidence")
        if raw is None:
            raise ProposalIntegrityError("Claim admission has no retained law evidence")
        return parse_claim_law_evidence(raw)

    def accepted_artifact_version(
        self, coordinate: AcceptedCoordinate, artifact_digest: str, *, family: str
    ) -> tuple[str, bytes] | None:
        """Exact bytes of one accepted artifact version within a prefix, even if superseded.

        A missing version, or one outside ``family``, is None; nothing ever
        substitutes today's version for a historical one.
        """

        with self.accepted_history_reader(at=coordinate) as history:
            location = history.artifact(artifact_digest)
            if location is None or not location.path.startswith(family):
                return None
            generation = history.generation(location.occurrence_sequence)
        raw = self.blob_at(generation.git_oid, location.path)
        if raw is None:
            raise ProposalIntegrityError("an accepted artifact version's bytes are unavailable")
        return location.path, raw

    def accepted_capture_contract_version(
        self, coordinate: AcceptedCoordinate, contract_digest: str
    ) -> AcceptedCaptureContract | None:
        """One exact CaptureContract version accepted within a prefix, even if superseded."""

        found = self.accepted_artifact_version(
            coordinate, contract_digest, family="capture-contracts/"
        )
        if found is None:
            return None
        path, raw = found
        contract = parse_capture_contract(raw, path=path)
        if capture_contract_digest(contract).tagged != contract_digest:
            return None
        return AcceptedCaptureContract(
            path=path, contract=contract, artifact_digest=contract_digest
        )

    def accepted_capture_contract_versions(
        self, coordinate: AcceptedCoordinate
    ) -> "CaptureContractVersions":
        """``accepted_capture_contract_version`` at one referent, with its authority.

        Authoritative when this instance's history index holds every accepted
        artifact version, genesis included: its compiler is one that indexes
        genesis (``_history_reader_for_epoch``).
        """

        from cruxible_core.evidence.attestation_verification import CaptureContractVersions

        return CaptureContractVersions(
            resolve=lambda digest: self.accepted_capture_contract_version(coordinate, digest),
            authoritative=self._recovered.coordinate.compiler in PC_HR_ARTIFACT_CODEC_COMPILERS,
        )

    def capture_verification_context(self) -> LocalProducerReceiptResolver:
        """The Capture verification context of the current request.

        Inside ``capture_verification_request`` every stage of the request --
        lowering, preflight, admission, settlement, capture reads -- gets the
        one context the request owns: it reads each cited run's journal records
        once and verifies each Capture once. Outside one, a fresh context.
        """

        active = requested_verification_context(self._verification_key())
        return active if active is not None else self._open_verification_context()

    @contextmanager
    def capture_verification_request(self) -> Iterator[LocalProducerReceiptResolver]:
        """Own one Capture verification context for a whole request."""

        with verification_request(
            self._verification_key(), self._open_verification_context
        ) as context:
            yield context

    def _verification_key(self) -> tuple[str, str]:
        return (str(self.root), self.descriptor.instance_id)

    def _open_verification_context(self) -> LocalProducerReceiptResolver:
        return local_producer_receipt_resolver(
            exhaust_root=self._validated_paths(self.root, self.descriptor.storage)["exhaust"],
            instance_id=self.descriptor.instance_id,
            bodies=self.body_store(),
        )

    def subject_assignment_authority(self) -> SubjectAssignmentRecords:
        """The core records a new Subject's recorded assignment must name (review F-001)."""

        return SubjectAssignmentRecords(self.root / self.descriptor.storage.exhaust)

    def proposal_service(self) -> ProposalService:
        """Bind PB-C proposal evaluation to authenticated main and inert storage."""

        # Through `body_store()`, so a preview evaluates against the bodies it holds
        # in memory exactly as a submission does against the ones it stored.
        bodies = self.body_store()
        return ProposalService(
            self._ledger,
            accepted=self.accepted_coordinate(),
            bodies=bodies,
            evidence=self.proposal_evidence(),
            review_projection_lock=self.review_projection_lock,
            note_index_provider=self.proposal_note_index,
            accepted_tree_provider=self.immutable_tree_at,
            claim_law_provider=self.accepted_claim_law_evidence,
            attestation_principal_provider=self.accepted_principal,
            accepted_referents_provider=self.accepted_referent_coordinates,
            historical_artifact_provider=self.accepted_artifact_version,
            subject_assignment_authority=self.subject_assignment_authority(),
            prepared_evaluations=self.prepared_evaluations,
            principal_registry_provider=self.accepted_principal_registry,
            active_principal_provider=self.require_accepted_principal,
            current_coordinate=self.accepted_coordinate,
            promotion_verifier=self._promotion_verifier,
            producer_receipt_resolver=self.capture_verification_context(),
            query_facts_provider=lambda coordinate, *, predicates=None, subject_kinds=None: (
                self._accepted_query_facts(
                    self, coordinate, predicates=predicates, subject_kinds=subject_kinds
                )
            ),
            workspace_advertiser=self.advertise_workspace,
            receive_limits=self._receive_limits,
            require_writable=self.require_writable,
            ledger_publisher=self.request_ledger_mirror,
            tree_state_provider=derive_indexed_state,
        )

    def bind_receive_limits(self, limits: ProposalReceiveLimits) -> None:
        """Bind this daemon's operational receive ceiling for the running process.

        The changed-member ceiling is an operator admission knob, so it reaches
        the instance from the daemon's own state root rather than from anything
        an author can send.
        """

        self._receive_limits = limits

    def bind_workspace_advertiser(
        self,
        advertiser: Callable[[], WorkspaceAdvertisement],
        *,
        workspace_path: Path | None,
    ) -> None:
        """Bind the manager-owned advisory Git hook for this process."""

        self._workspace_advertiser = advertiser
        self._workspace_path = workspace_path

    def advertise_workspace(self) -> WorkspaceAdvertisement:
        """Queue the advisory ref refresh and return without waiting for Git.

        Review refs, their notes and the workspace fetch are advisory: nothing a
        write returns depends on them, and mirror publication reconciles them
        itself before it snapshots refs. The refresh runs after the response.
        """

        with self._advertisement_condition:
            self._advertisement_requested += 1
            self._start_advertiser_locked()
        if self._workspace_advertiser is None or self._workspace_path is None:
            return NOT_ATTACHED_ADVERTISEMENT
        return WorkspaceAdvertisement(status="scheduled", workspace_path=str(self._workspace_path))

    def settled_workspace_advertisement(self) -> WorkspaceAdvertisement:
        """Wait for every refresh requested so far and report the latest outcome."""

        with self._advertisement_condition:
            if self._last_advertisement is None:
                self._advertisement_requested += 1
            target = self._advertisement_requested
            self._start_advertiser_locked()
            while self._advertisement_completed < target:
                if self._advertisement_thread is None:
                    # The worker could not start; refresh on this thread instead.
                    break
                self._advertisement_condition.wait()
            else:
                assert self._last_advertisement is not None
                return self._last_advertisement
        result = self._advertise_workspace_now()
        with self._advertisement_condition:
            if self._advertisement_completed < target:
                self._advertisement_completed = target
                self._last_advertisement = result
            self._advertisement_condition.notify_all()
        return result

    def _start_advertiser_locked(self) -> None:
        if self._advertisement_thread is not None:
            self._advertisement_condition.notify_all()
            return
        # Not a daemon thread: a short-lived process finishes the refresh it
        # queued before the interpreter exits.
        thread = threading.Thread(
            target=Context().run,
            args=(self._run_workspace_advertiser,),
            name=f"workspace-advertiser-{self.descriptor.instance_id}",
        )
        try:
            thread.start()
        except RuntimeError:
            # Resource exhaustion is operational; the next request or settle
            # retries, and a settle refreshes on its own thread.
            return
        self._advertisement_thread = thread

    def _run_workspace_advertiser(self) -> None:
        while True:
            with self._advertisement_condition:
                if self._advertisement_completed >= self._advertisement_requested:
                    self._advertisement_thread = None
                    self._advertisement_condition.notify_all()
                    return
                target = self._advertisement_requested
            result = self._advertise_workspace_now()
            with self._advertisement_condition:
                if self._advertisement_completed < target:
                    self._advertisement_completed = target
                    self._last_advertisement = result
                self._advertisement_condition.notify_all()

    def _advertise_workspace_now(self) -> WorkspaceAdvertisement:
        """Refresh advisory refs, or report that this instance has no attachment."""

        try:
            self._reconcile_proposal_review_refs()
            self._compact_ledger_on_stride()
            if self._workspace_advertiser is None:
                return NOT_ATTACHED_ADVERTISEMENT
            return self._workspace_advertiser()
        except BaseException:
            return WorkspaceAdvertisement(
                status="failed",
                workspace_path=None,
                failure_code="unexpected_failure",
            )

    def _compact_ledger_on_stride(self) -> None:
        """Compact the ledger once each time the head crosses a stride, off the acceptance path."""

        with self._state_lock:
            stride = self._recovered.head.sequence // LEDGER_COMPACTION_STRIDE
        if self._compacted_stride is None:
            self._compacted_stride = stride
            return
        if stride <= self._compacted_stride or is_previewing():
            return
        self._compacted_stride = stride
        try:
            self.compact_ledger()
        except Exception as exc:
            # Compaction is housekeeping: the next stride retries.
            _log.warning(
                "ledger_compaction_failed",
                instance_id=self.descriptor.instance_id,
                reason=f"{type(exc).__name__}: {exc}",
            )

    def compact_ledger(self, *, prune: bool = False) -> bool:
        """Pack objects and refs and write the commit-graph; prune only when asked.

        Packing and the commit-graph are safe beside live writers; refs are
        packed while the review projection and activation locks exclude every
        ref writer. ``prune`` deletes unreachable loose objects and is for the
        daemon's start, before it serves: it also waits for no generation in
        flight, and is skipped (returning False) if one is.
        """

        refuse_write_while_previewing("ledger compaction")
        self._ledger.compact_objects()
        pruned = False
        with self.review_projection_lock(), self._ledger.activation_lock():
            self._ledger.pack_refs()
            if prune:
                pruned = self._ledger.prune_unreachable()
        return pruned

    def compact_ledger_at_start(self) -> bool:
        """The daemon's start pass: retention, then compact and prune a loose ledger.

        Retention folds due receipts and retires every accepted candidate a
        record already carries (the backlog), before anything is served.
        """

        self._housekeeping("consumption_fold", self.fold_consumption_if_due)
        self._housekeeping("candidate_retirement", self.retire_settled_candidates)
        if self._ledger.loose_object_count() < LEDGER_START_COMPACTION_LOOSE_OBJECTS:
            return False
        self.compact_ledger(prune=True)
        return True

    @contextmanager
    def review_projection_lock(self) -> Iterator[None]:
        """Keep evidence groups and their note aliases coherent across local writers."""
        with mirror_lock(self.root, review=True):
            yield

    def _reconcile_proposal_review_refs(self) -> None:
        """Serialize all local review projection writers, independently of network I/O."""

        with self.review_projection_lock():
            if self._ledger.read_main() != self.accepted_coordinate().git_oid:
                self.refresh()
            self._reconcile_proposal_review_refs_locked()

    def _reconcile_proposal_review_refs_locked(self) -> None:
        """Publish active review aliases without revisiting settled proposal history."""
        coordinate = AcceptedCoordinate.from_internal(self.accepted_coordinate())
        evidence = self.proposal_evidence()
        with self.accepted_history_reader(at=coordinate):
            index = proposal_note_snapshot(evidence, open_at=coordinate)
        active_ids = index.active_proposal_ids or frozenset()
        dependencies: dict[str, str] = {}
        for proposal_id in active_ids:
            evaluation = index.evaluations[proposal_id]
            assert evaluation.evaluated_tree_oid is not None
            for oid, kind in (
                (evaluation.evaluated_tree_oid, "tree"),
                (evaluation.evaluated_base_oid, "commit"),
            ):
                if dependencies.get(oid, kind) != kind:
                    raise ProposalIntegrityError("review dependency has conflicting object types")
                dependencies[oid] = kind
        object_presence, stored_notes = self._ledger.read_review_projection(
            tuple(index.proposal_ids_by_oid), dependencies=dependencies
        )
        refs: dict[str, str] = {}
        published: dict[str, tuple[str, str | None]] = {}
        for oid, proposal_ids in index.proposal_ids_by_oid.items():
            if not proposal_ids & active_ids:
                continue
            first = min(proposal_ids)
            published[oid] = (first, index.evaluations[first].candidate_digest)
        for proposal_id in sorted(active_ids):
            admission = index.admissions[proposal_id]
            evaluation = index.evaluations[proposal_id]
            candidate_digest = evaluation.candidate_digest
            if candidate_digest is None or evaluation.evaluated_tree_oid is None:
                continue
            candidate = index.candidates[candidate_digest]
            review_oid = index.review_oids[admission.proposal_id]
            if not object_presence[review_oid]:
                review_oid = self._ledger.proposal_review_commit(
                    tree_oid=evaluation.evaluated_tree_oid,
                    base_oid=evaluation.evaluated_base_oid,
                    actor_id=admission.actor_id,
                    timestamp=admission.admitted_at,
                    message=candidate.message(rationale=admission.rationale),
                )
            if review_oid != index.review_oids[admission.proposal_id]:
                raise ProposalIntegrityError("materialized review alias differs from its index")
            object_presence[review_oid] = True
            refs[admission.proposal_id.removeprefix("sha256:")] = review_oid
        # Author slots are retry anchors only until their admission completes.
        # Open candidates have independent review refs; unrecorded interrupted
        # slots remain intact for the delivery recovery path.
        retired_targets = {}
        assert evidence.index is not None
        slots = self._ledger.proposal_refs()
        # One snapshot of the index answers every slot.
        slot_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        if slots:
            with evidence.index.read(evidence) as connection:
                for target, oid in slots.items():
                    slot_rows[(target, oid)] = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT * FROM proposals WHERE target_ref=? AND candidate_commit_oid=? "
                            "ORDER BY proposal_id",
                            (target, oid),
                        )
                    ]
        for target, oid in slots.items():
            rows = slot_rows[(target, oid)]
            if not rows or any(row["proposal_id"] in active_ids for row in rows):
                continue
            complete = [
                row
                for row in rows
                if row["admission_path"] is not None
                and row["evaluation_status"] != "missing"
                and (
                    row["evaluation_status"] == "refused"
                    or row["candidate_parent_semantic_root"] is not None
                )
            ]
            if len(complete) != len(rows):
                continue
            retired_targets[target] = oid
        self._ledger.replace_proposal_review_refs(refs, retired_targets=retired_targets)
        # After the refs, so every annotated commit is already reachable from
        # one: a note on an unreferenced object is a note a `gc` may collect.
        for review_oid, (proposal_id, candidate_digest) in published.items():
            self._publish_review_commit_notes(
                evidence,
                review_oid=review_oid,
                proposal_id=proposal_id,
                candidate_digest=candidate_digest,
                index=index,
                object_presence=object_presence,
                stored_notes=stored_notes,
            )

    def _publish_review_commit_notes(
        self,
        evidence: ProposalEvidenceStore,
        *,
        review_oid: str,
        proposal_id: str,
        candidate_digest: str | None,
        index: ProposalNoteIndex | None = None,
        object_presence: Mapping[str, bool] | None = None,
        stored_notes: Mapping[tuple[str, str], bytes | None] | None = None,
    ) -> None:
        """Restate one proposal's evidence onto the commit a reviewer receives."""

        grouped = index or self.proposal_note_index(evidence=evidence, oids=(review_oid,))
        if proposal_id not in grouped.proposal_ids_by_oid.get(review_oid, ()):
            raise ProposalIntegrityError("review note target does not belong to the proposal")
        with ExitStack() as locks:
            for digest in grouped.candidate_digests(review_oid):
                locks.enter_context(self.approval_note_lock(digest))
            grouped.publish(
                self._ledger,
                (review_oid,),
                object_presence=object_presence,
                stored_notes=stored_notes,
            )

    def proposal_note_index(
        self,
        *,
        evidence: ProposalEvidenceStore | None = None,
        oids: Iterable[str] | None = None,
        candidate_digests: Iterable[str] = (),
    ) -> ProposalNoteIndex:
        """Materialize this request's review groups from one verified snapshot."""
        return proposal_note_snapshot(
            evidence or self.proposal_evidence(), oids=oids, candidate_digests=candidate_digests
        )

    def proposal_evidence(self) -> ProposalEvidenceStore:
        """Return the immutable non-authoritative proposal/approval evidence store."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ProposalEvidenceStore(
            paths["exhaust"],
            index=self._accepted_history_index.proposals,
            transport=self._ledger,
            settled=self._settled_candidate,
        )

    def _settled_candidate(self, candidate_digest: str) -> SettledCandidate | None:
        """A retired accepted candidate, rebuilt from the change-set record its locator names."""

        known = self._settled_candidates.get(candidate_digest)
        if known is not None:
            return known
        paths = self._validated_paths(self.root, self.descriptor.storage)
        locator = ProposalEvidenceStore(paths["exhaust"]).read_settled_locator(candidate_digest)
        if locator is None:
            return None
        with self._state_lock:
            history = self._recovered.history
        generation = history[locator.sequence] if locator.sequence < len(history) else None
        record = None if generation is None else generation.record
        if (
            generation is None
            or record is None
            or generation.oid != locator.git_oid
            or record.changeset_digest != locator.changeset_digest
            or record.candidate_digest != candidate_digest
        ):
            raise ProposalIntegrityError(
                "a retired candidate's locator names no accepted change-set record of it"
            )
        settled = SettledCandidate(
            candidate=candidate_from_change_set_record(record), approvals=record.approvals
        )
        self._settled_candidates[candidate_digest] = settled
        return settled

    def retire_settled_candidates(self, sequences: Iterable[int] | None = None) -> int:
        """Delete accepted candidates' files (and the approvals their records hold).

        ``sequences`` names the accepted generations to retire (default: the
        backlog sweep at daemon start, every accepted candidate whose file is
        still there, found from the candidate files and the history index).
        Each accepted proposal's change-set record carries its candidate and
        the approvals settlement verified, so every reader then answers from
        the record. Withdrawn, rejected and refused proposals' evidence is
        never touched. Returns how many candidate files were removed.
        """

        if is_previewing():
            return 0
        with self._state_lock:
            history = self._recovered.history
        evidence = self.proposal_evidence()
        if sequences is None:
            present = [
                "sha256:" + path.name.removesuffix(".json")
                for path in evidence.candidates.glob("*.json")
            ]
            with self.accepted_history_reader() as reader:
                located = (reader.generation_for_candidate(digest) for digest in present)
                selected: Iterable[int] = sorted(
                    location.sequence for location in located if location is not None
                )
        else:
            selected = sequences
        removed = 0
        for sequence in selected:
            generation = history[sequence]
            record = generation.record
            if record is None or record.candidate_digest is None:
                continue
            digest = record.candidate_digest
            name = digest.removeprefix("sha256:")
            if (
                not (evidence.candidates / f"{name}.json").exists()
                and not (evidence.approvals / name).exists()
            ):
                continue
            settled = SettledCandidate(
                candidate=candidate_from_change_set_record(record), approvals=record.approvals
            )
            locator = SettledCandidateLocator(
                candidate_digest=digest,
                sequence=sequence,
                git_oid=generation.oid,
                changeset_digest=record.changeset_digest,
            )
            # The approval writer's locks: no approval is written or its note
            # rendered while this candidate's files go.
            with self.review_projection_lock(), self.approval_note_lock(digest):
                candidate_removed, _approvals_removed = evidence.retire_settled_candidate(
                    locator, settled
                )
            removed += int(candidate_removed)
        return removed

    def refusal_index(self) -> RefusalIndex:
        """The derived index of earlier refusals (retired Claims, rejected proposals).

        One per instance, in the projections directory: rebuilt from accepted
        history and rejection records whenever it is missing.
        """

        index = self.__dict__.get("_refusal_index")
        if index is None:
            from cruxible_core.proposals.refusal_index import RefusalIndex

            paths = self._validated_paths(self.root, self.descriptor.storage)
            with self._state_lock:
                index = self.__dict__.get("_refusal_index")
                if index is None:
                    index = RefusalIndex(paths["projections"] / "refusals.sqlite3")
                    self.__dict__["_refusal_index"] = index
        return index

    def proposal_ref_target(self, target_ref: str) -> str | None:
        """Read one proposal transport ref without exposing ledger mutation."""

        return self._ledger.read_proposal_ref(target_ref)

    @contextmanager
    def approval_note_lock(self, candidate_digest: str) -> Iterator[None]:
        """Hold one candidate's approval read-modify-write, store through Git."""

        with self._ledger.approval_note_lock(candidate_digest):
            yield

    def write_proposal_note(self, kind: str, oid: str, content: bytes) -> None:
        """Project one proposal record onto its own candidate commit."""

        self._ledger.write_proposal_note(kind, oid, content)

    def read_proposal_note(self, kind: str, oid: str) -> bytes | None:
        """Read one candidate commit's projected proposal note, if it carries one."""

        return self._ledger.read_proposal_note(kind, oid)

    def read_proposal_notes(
        self, pairs: Sequence[tuple[str, str]], *, notes_commit: str | None = None
    ) -> dict[tuple[str, str], bytes | None]:
        """Read several projected proposal notes in one fresh ledger read.

        ``notes_commit`` pins one immutable notes commit instead of the moving ref.
        """

        return self._ledger.read_proposal_notes(pairs, notes_commit=notes_commit)

    def review_operational_store(self) -> ReviewOperationalStore:
        """Return the local append-only review observation store.

        This accessor does not initialize the store.  The first successful
        operational append binds its initialization coordinate and generation.
        """

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ReviewOperationalStore(
            paths["exhaust"],
            instance_id=self.descriptor.instance_id,
        )

    def claim_attestation_evidence_store(self) -> ClaimAttestationEvidenceStore:
        """Return the principal-authored evidence ledger without initializing it."""

        from cruxible_core.evidence.claim_attestation_store import (
            ClaimAttestationEvidenceStore,
        )

        if self._claim_attestation_store is None:
            paths = self._validated_paths(self.root, self.descriptor.storage)
            self._claim_attestation_store = ClaimAttestationEvidenceStore(
                paths["exhaust"],
                instance_id=self.descriptor.instance_id,
            )
        return self._claim_attestation_store

    def accepted_history(self) -> tuple[RecoveredGeneration, ...]:
        """Return genesis-rooted history verified by acceptance or replay.

        Internal callers borrow these records and must treat nested values as
        read-only. Public transports serialize them rather than exposing this
        process-owned state.
        """

        return self._recovered.history

    @contextmanager
    def accepted_history_reader(
        self, *, at: AcceptedCoordinate | None = None
    ) -> Iterator[HistoryReader]:
        """Acquire one verified epoch and a cutoff-bound shared history reader."""
        with self._state_lock:
            recovered = self._recovered
        with self._history_reader_for_epoch(recovered, at=at) as reader:
            yield reader

    @contextmanager
    def _history_reader_for_epoch(
        self, recovered: RecoveredInstanceState, *, at: AcceptedCoordinate | None = None
    ) -> Iterator[HistoryReader]:
        paths = self._validated_paths(self.root, self.descriptor.storage)
        if self._accepted_history_index.path.parent != paths["projections"]:
            raise ProjectionIntegrityError("history index storage binding changed")

        def envelopes(
            sequence: int, prior_members: PriorMemberSequences
        ) -> tuple[ArtifactEnvelopeRow, ...]:
            if (
                sequence == 0
                and recovered.coordinate.compiler not in PC_HR_ARTIFACT_CODEC_COMPILERS
            ):
                # Frozen bootstrap was verified separately, before typed owner
                # publication existed. Record its generation binding without
                # interpreting those bytes under a different artifact codec.
                return ()
            generation = recovered.history[sequence]
            coordinate = recovered.coordinate.model_copy(
                update={
                    "git_oid": generation.oid,
                    "semantic_root": generation.semantic_root.tagged,
                    "generation_root": generation.generation_root.tagged,
                    "compiler": generation.compiler,
                }
            )
            assembler = ProjectionAssembler(
                self._ledger,
                accepted=coordinate,
                publication_directory=paths["projections"],
                bodies=ContentAddressedBodyStore(paths["cas"]),
            )
            if sequence == 0:
                request = assembler.request(
                    output_staging_directory=paths["projections"]
                    / f".stage-history-{secrets.token_hex(12)}"
                )
                manifest = paths["projections"] / projection_manifest_name(request)
                if manifest.exists():
                    with bind_projection(manifest, expected=coordinate) as projection:
                        projection.require_source_authentication(repository=self._ledger)
                        return projection.artifact_envelopes(paths=None)
                # Missing publications derive membership from retained contracts.
                # Document bodies and promoted output availability are
                # independent of accepted membership.
                sources = {
                    blob.path: blob.content
                    for blob in read_registered_tree(
                        self._ledger,
                        generation.oid,
                        limits=TreeReadLimits(),
                        artifact_kinds=assembler.artifact_kinds,
                    )
                }
                return parse_static_owners(sources, accepted=coordinate).envelopes
            # A successor needs envelopes only for its changed members, published
            # or not. Their bytes come from this generation; the replay-verified
            # record prefix supplies change-set context, so no whole historical
            # tree is read, authenticated or re-parsed once per generation.
            changed = self._ledger.changed_tree_paths(
                recovered.history[sequence - 1].oid, generation.oid
            )
            member_paths = tuple(
                path
                for path in changed
                if is_candidate_card_path(path)
                or registered_path_kind(path, artifact_kinds=assembler.artifact_kinds)
                != "changeset"
            )
            # Only the records that touched these members bear on their
            # revisions and proofs: earlier ones from the rows this sync already
            # wrote, plus this generation's own. Each is read back on demand.
            sequences = {
                prior for touched in prior_members(member_paths).values() for prior in touched
            } | {sequence}
            history = tuple(
                (f"changesets/cs-{item.sequence:020d}.json", item.record)
                for item in (recovered.history[prior] for prior in sorted(sequences))
                if item.record is not None
            )
            parsed = parse_static_owners(
                self._ledger.blobs_at(generation.oid, member_paths),
                accepted=coordinate,
                selected_member_history=history,
            )
            selected = frozenset(changed)
            return tuple(row for row in parsed.envelopes if row.path in selected)

        with self._accepted_history_index.read(recovered, envelopes, at=at) as reader:
            yield reader

    def _generation_for_oid(self, oid: str) -> RecoveredGeneration | None:
        """Locate membership through C, then bind it to the captured replay proof."""
        recovered = self._recovered
        with self._history_reader_for_epoch(recovered) as reader:
            location = reader.generation_for_oid(oid)
        return self._replayed_generation(recovered, location)

    def _replayed_generation(
        self, recovered: RecoveredInstanceState, location: AcceptedGenerationLocation | None
    ) -> RecoveredGeneration | None:
        if location is None:
            return None
        if not 0 <= location.sequence < len(recovered.history):
            raise ProjectionIntegrityError("indexed generation lies outside captured replay")
        generation = recovered.history[location.sequence]
        if (
            generation.sequence != location.sequence
            or generation.oid != location.git_oid
            or generation.semantic_root.tagged != location.semantic_root
            or generation.generation_root.tagged != location.generation_root
            or generation.compiler.rule_digest != location.compiler_digest
        ):
            raise ProjectionIntegrityError("indexed generation differs from captured replay")
        return generation

    def projected_member_revision(self, path: str, artifact_digest: str) -> int | None:
        """``projected_revision`` for one member from the index, without reading records.

        Every accepted occurrence of the path is one earlier revision, and an
        occurrence already carrying this exact digest means the member is
        unchanged. None when an occurrence stored no digest (an early record
        format): the caller then derives it from the records themselves.
        """

        with self._history_reader_for_epoch(self._recovered) as history:
            locations = history.member_history(path)
        if any(location.artifact_digest is None for location in locations):
            return None
        if any(location.artifact_digest == artifact_digest for location in locations):
            return len(locations)
        return len(locations) + 1

    def member_record_history(
        self, paths: Sequence[str]
    ) -> tuple[tuple[str, ChangeSetRecordAnyVersion], ...]:
        """The verified records that touched these paths, found through the index."""

        recovered = self._recovered
        with self._history_reader_for_epoch(recovered) as history:
            sequences = sorted(
                {location.sequence for path in paths for location in history.member_history(path)}
            )
        records = []
        for sequence in sequences:
            record = recovered.history[sequence].record
            if record is not None:
                records.append((f"changesets/cs-{sequence:020d}.json", record))
        return tuple(records)

    def accepted_evaluation_time(self, oid: str) -> datetime:
        """Resolve the immutable acceptance instant for one replayed generation."""

        generation = self._generation_for_oid(oid)
        if generation is None:
            raise FormatError("evaluation coordinate is outside accepted history")
        if generation.record is not None:
            try:
                return datetime.strptime(
                    generation.record.candidate.timestamp,
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise FormatError("accepted candidate timestamp is malformed") from exc
        author_time, committer_time = self._ledger.commit_timestamps(oid)
        if author_time != committer_time:
            raise FormatError("genesis author and committer timestamps disagree")
        return author_time

    def accepted_candidate_digest(self, oid: str) -> str | None:
        """The candidate digest one accepted generation's change-set record settled.

        ``None`` for genesis or an OID outside accepted history.
        """

        generation = self._generation_for_oid(oid)
        if generation is None or generation.record is None:
            return None
        return generation.record.candidate_digest

    def coordinate_for_oid(self, oid: str) -> AcceptedProjectionCoordinate:
        """Resolve one accepted historical OID to its complete verified coordinate."""

        generation = self._generation_for_oid(oid)
        if generation is None:
            raise FormatError("Git OID is not one accepted generation of this instance")
        return AcceptedProjectionCoordinate(
            instance_id=self.descriptor.instance_id,
            repository_path=str(self._ledger.path.resolve(strict=True)),
            git_object_format=self.descriptor.git_object_format,
            git_oid=generation.oid,
            semantic_root=generation.semantic_root.tagged,
            generation_root=generation.generation_root.tagged,
            compiler=generation.compiler,
        )

    def generation_for_semantic_root(self, semantic_root: str) -> RecoveredGeneration:
        """Resolve one historical signing root without consulting mutable proposal state."""

        recovered = self._recovered
        with self._history_reader_for_epoch(recovered) as reader:
            location = reader.generation_for_semantic_root(semantic_root)
        generation = self._replayed_generation(recovered, location)
        if generation is None:
            raise FormatError("semantic root is not one accepted generation of this instance")
        return generation

    def immutable_tree_at(self, oid: str) -> SnapshotTree:
        """Return an owned immutable root after verifying the full accepted binding."""
        coordinate = self.coordinate_for_oid(oid)
        binding = canonical_bytes(
            {
                "coordinate": coordinate.model_dump(mode="json", exclude={"repository_path"}),
                "genesis": self._verified_genesis.generation_root.tagged,
            }
        )

        def advance(previous_binding: bytes, previous: SnapshotTree) -> SnapshotTree:
            import json

            previous_oid = json.loads(previous_binding)["coordinate"]["git_oid"]
            self.coordinate_for_oid(previous_oid)
            return advance_accepted_tree(previous, self._ledger.blob_ref_changes(previous_oid, oid))

        def load() -> SnapshotTree:
            self.coordinate_for_oid(oid)
            return SnapshotTree(self._ledger.blob_refs_at(oid))

        tree = self.derived.accepted_tree(binding, load, advance)
        # Loaded or advanced from exactly this commit's tree, so its rows are that tree.
        tree._commit_oid = oid
        if coordinate.git_oid == self._verified_genesis.oid:
            tree._accepted_reader = None
            tree._accepted_claim_items = None
        else:
            tree._accepted_reader = lambda: self.bind_accepted_projection(coordinate)
            tree._accepted_claim_items = lambda statement: self._accepted_claim_items(
                coordinate, statement
            )
        return tree

    def _accepted_claim_items(
        self, coordinate: AcceptedProjectionCoordinate, statement: ClaimPredicateKey
    ) -> tuple[tuple[str, bytes], ...]:
        """The live Claims of one statement at an accepted coordinate, once per request.

        Accepted state at a coordinate is immutable, so inside ``projection_scope``
        the answer is remembered for the rest of the request: a settle asks for
        each item's slot in delivery, in its many-valued fill and in lowering.
        """

        from cruxible_core.indexes.evaluated_state import EvaluationRows

        pool = _PROJECTION_POOL.get()
        memo = None if pool is None else pool.values()
        subject = statement.subject
        key = (
            "accepted-claim-items",
            id(self),
            coordinate.git_oid,
            coordinate.semantic_root,
            coordinate.generation_root,
            coordinate.compiler.rule_digest,
            subject.artifact_path,
            subject.selector.scheme,
            subject.selector.value,
            statement.predicate,
        )
        if memo is not None:
            held = memo.get(key)
            if held is not None:
                return cast("tuple[tuple[str, bytes], ...]", held)
        rows = EvaluationRows(self.bind_accepted_projection(coordinate))
        try:
            answer = rows.claim_items(statement)
        finally:
            rows.close()
        if memo is not None:
            memo[key] = answer
        return answer

    def tree_at(self, oid: str) -> dict[str, bytes]:
        """Explicitly materialize an owned tree after proving accepted membership."""
        self.coordinate_for_oid(oid)
        return self._ledger.read_tree(oid)

    def paths_at(self, oid: str) -> tuple[str, ...]:
        """List accepted paths without reading a single blob payload.

        The listing carries the same whole-generation blob proof ``tree_at``
        applies, so a generation a read would refuse is refused here too and a
        warm memo and a cold listing answer identically.
        """

        self.coordinate_for_oid(oid)
        return self._ledger.paths_at(oid)

    def retained_record_reader(self) -> RetainedRecordReader:
        """A record reader sharing this instance's verified change-set records."""

        return RetainedRecordReader(
            self.blob_at,
            verified=self.verified_change_set_records,
            facts=self.verified_record_facts,
        )

    def blob_at(self, oid: str, path: str) -> bytes | None:
        """Read one accepted path without materializing its whole generation."""

        return self.blobs_at(oid, (path,)).get(path)

    def blobs_at(self, oid: str, paths: Sequence[str]) -> dict[str, bytes]:
        """Read an exact set of accepted paths under the same acceptance proof."""

        self.coordinate_for_oid(oid)
        return self._ledger.blobs_at(oid, paths)

    def _require_proposal_object(self, oid: str, proposal_id: str | None) -> None:
        if self._ledger.object_exists(oid):
            return
        if proposal_id is not None:
            coordinate = AcceptedCoordinate.from_internal(self.accepted_coordinate())
            evidence = self.proposal_evidence()
            with self.accepted_history_reader(at=coordinate) as history:
                assert evidence.index is not None
                with evidence.index.read(evidence) as connection:
                    row = connection.execute(
                        "SELECT p.*, EXISTS(SELECT 1 FROM accepted_generations g "
                        "WHERE g.candidate_digest=p.candidate_digest "
                        "AND g.sequence<=?) AS accepted "
                        "FROM proposals p WHERE proposal_id=?",
                        (history.sequence, proposal_id),
                    ).fetchone()
            if (
                row is not None
                and row["evaluation_status"] != "missing"
                and (
                    row["withdrawal_path"] is not None
                    or row["accepted"]
                    or row["evaluation_status"] == "refused"
                    or (
                        row["candidate_parent_semantic_root"] is not None
                        and row["candidate_parent_semantic_root"] != coordinate.semantic_root
                    )
                )
            ):
                raise ProposalContentUnavailable()
        raise ProposalIntegrityError("active proposal content is missing or unavailable")

    def proposal_tree(
        self, oid: str, *, base_oid: str | None = None, proposal_id: str | None = None
    ) -> Mapping[str, bytes]:
        """Read an exact proposal tree, optionally carrying a proven accepted base."""

        self._require_proposal_object(oid, proposal_id)
        if base_oid is None:
            return self._ledger.read_tree(oid)
        parent = self.immutable_tree_at(base_oid)
        return self._ledger.read_tree_delta(base_oid, oid, parent_tree=parent)

    def proposal_blobs(
        self, oid: str, paths: Sequence[str], *, proposal_id: str | None = None
    ) -> dict[str, bytes]:
        """Read selected retained proposal members without asserting acceptance."""
        self._require_proposal_object(oid, proposal_id)
        return self._ledger.blobs_at(oid, paths)

    def resolve_accepted_coordinate(
        self,
        *,
        git_oid: str,
        semantic_root: str,
        generation_root: str,
        compiler_digest: str | None = None,
    ) -> AcceptedProjectionCoordinate:
        """Verify exact triple/compiler correspondence against replayed history."""

        coordinate = self.coordinate_for_oid(git_oid)
        if (
            coordinate.semantic_root != semantic_root
            or coordinate.generation_root != generation_root
        ):
            raise FormatError("accepted coordinate triple has mixed members")
        if compiler_digest is not None and coordinate.compiler.rule_digest != compiler_digest:
            raise FormatError("accepted coordinate compiler digest is unsupported")
        return coordinate

    def _projection_sources(self, handle: ProjectionHandle) -> ProjectionHandle:
        coordinate = handle.accepted
        at = AcceptedCoordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )
        return handle.attach_sources(
            self._ledger,
            bodies=self.body_store(),
            history=lambda: self.accepted_history_reader(at=at),
            records=RetainedRecordReader(
                self._ledger.blob_at,
                verified=self.verified_change_set_records,
                facts=self.verified_record_facts,
            ),
            fact_memo=self._owner_facts,
        )

    def bind_accepted_projection(
        self,
        coordinate: AcceptedProjectionCoordinate,
    ) -> ProjectionHandle:
        """Bind a current serving or retained historical immutable projection.

        Inside ``projection_scope`` a handle of the same coordinate that its
        last holder closed cleanly is handed out again instead of rebinding.
        """

        pool = _PROJECTION_POOL.get()
        if pool is None:
            return self._bind_accepted_projection(coordinate)
        key = (
            id(self),
            coordinate.git_oid,
            coordinate.semantic_root,
            coordinate.generation_root,
            coordinate.compiler.rule_digest,
        )
        reused = pool.take(key)
        if reused is not None:
            # Still one accepted generation: refused exactly as a fresh bind is.
            # Within one recovered epoch the answer cannot change, so a request
            # re-verifies a reused coordinate once per epoch, not once per lookup.
            memo = pool.values()
            verified = ("verified-coordinate", *key)
            recovered = self._recovered
            if memo is None or memo.get(verified) is not recovered:
                try:
                    self.resolve_accepted_coordinate(
                        git_oid=coordinate.git_oid,
                        semantic_root=coordinate.semantic_root,
                        generation_root=coordinate.generation_root,
                        compiler_digest=coordinate.compiler.rule_digest,
                    )
                except BaseException:
                    reused.close()
                    raise
                if memo is not None:
                    memo[verified] = recovered
            return reused
        return pool.adopt(key, self._bind_accepted_projection(coordinate), owner=self)

    @contextmanager
    def projection_scope(self) -> Iterator[None]:
        """One request on this thread: reuse bound projections instead of rebinding per lookup.

        A write or a Line run reads the same accepted coordinate hundreds of
        times; each fresh bind re-reads the manifests, re-opens and re-checks the
        piece. Nested scopes join the outermost one; handles close at its exit.
        """

        if _PROJECTION_POOL.get() is not None:
            yield
            return
        pool = ProjectionPool()
        token = _PROJECTION_POOL.set(pool)
        try:
            with self._ledger.request_pack():
                yield
        finally:
            _PROJECTION_POOL.reset(token)
            pool.close()

    def _bind_accepted_projection(
        self,
        coordinate: AcceptedProjectionCoordinate,
    ) -> ProjectionHandle:
        verified = self.resolve_accepted_coordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )
        paths = self._validated_paths(self.root, self.descriptor.storage)
        directory = paths["projections"]
        if verified == self.accepted_coordinate():
            # The serving head is never evicted; its handle holds its piece too.
            return self._projection_sources(bind_current_projection(directory, expected=verified))
        # Naming the manifest needs only this coordinate. The replay-proven
        # coordinates of every generation are built only for a rebuild, so a
        # bind at a base behind the head costs the same at any history length.
        assembler = ProjectionAssembler(
            self._ledger,
            accepted=verified,
            publication_directory=directory,
            bodies=ContentAddressedBodyStore(paths["cas"]),
        )
        request = assembler.request(
            output_staging_directory=directory / f".stage-historical-bind-{secrets.token_hex(12)}"
        )
        manifest_path = directory / projection_manifest_name(request)
        for _attempt in range(3):
            if manifest_path.exists():
                try:
                    # The handle holds its piece from eviction; one evicted
                    # between the look and the hold is bound again, rebuilt.
                    return self._projection_sources(
                        bind_projection(manifest_path, expected=verified)
                    )
                except ProjectionEvicted:
                    continue
            # Only replay-proven coordinates reach this point. Rebuild a missing
            # historical projection (never built, or evicted past retention)
            # under its own compiler, without changing serving or rewriting the
            # accepted record/receipt. Eviction waits on the same lock, and the
            # handle's hold is taken before it is released.
            rebuilt = False
            with retention_lock(directory):
                if not manifest_path.exists():
                    assembler.accepted_coordinates_by_sequence = (
                        self._accepted_coordinates_by_sequence()
                    )
                    assembler.assemble(request)
                    rebuilt = True
                handle = bind_projection(manifest_path, expected=verified)
            if rebuilt:
                self._rebuilt_on_miss(verified, manifest_path)
            return self._projection_sources(handle)
        raise ProjectionEvicted("projection was evicted on every bind attempt")

    def _rebuilt_on_miss(
        self, coordinate: AcceptedProjectionCoordinate, manifest_path: Path
    ) -> None:
        """A piece rebuilt on a miss is held by its new handle: evict it once that is released."""

        with self.accepted_history_reader() as history:
            location = history.generation_for_oid(coordinate.git_oid)
        if location is not None:
            with held_here(manifest_path, location.sequence):
                pass

    def _manifest_name_for(self, generation: RecoveredGeneration, directory: Path) -> str:
        return projection_manifest_name(
            AssemblerRequest(
                instance_id=self.descriptor.instance_id,
                repository_path=str(self._ledger.path.resolve(strict=True)),
                git_object_format=self.descriptor.git_object_format,
                git_oid=generation.oid,
                semantic_root=generation.semantic_root.tagged,
                generation_root=generation.generation_root.tagged,
                compiler_digest=generation.compiler.rule_digest,
                schema_version=generation.compiler.schema_version,
                output_staging_directory=str(directory),
            )
        )

    def evict_projection_pieces(self) -> tuple[str, ...]:
        """Evict accepted projection pieces past retention; returns the evicted manifest names.

        Kept: every generation after the cutoff (the head's sequence less
        ``retention.generations``, taken once from one snapshot, so a
        generation accepted meanwhile is always after it), which includes the
        serving head and every open proposal's base (an open proposal is
        evaluated at the head; a stale one is rebuilt if it is read), and
        every piece a reader holds in any process. An evicted coordinate is
        rebuilt when it is next bound.

        Each pass after the first tries only what changed since the last: the
        generations that crossed the cutoff, and the pieces at or before it
        whose hold in this process was released since (skipped while held,
        or rebuilt on a miss and so held by the rebuilding handle). A piece
        still held is never tried again until its release; one another
        process held at its turn is left for the next open. The first pass in
        a process (at open) reads the directory once. Runs after each
        activation and at open.
        """

        with self._state_lock:
            recovered = self._recovered
        cutoff = recovered.head.sequence - self.retention.generations
        directory = self._validated_paths(self.root, self.descriptor.storage)["projections"]
        evicted: list[str] = []
        with self._eviction_lock, retention_lock(directory):
            if self._evicted_through is None:
                candidates = self._listed_beyond(directory, recovered, cutoff)
            else:
                candidates = {
                    self._manifest_name_for(_generation_at(recovered, sequence), directory): (
                        sequence
                    )
                    for sequence in range(max(self._evicted_through + 1, 0), cutoff + 1)
                }
            for name, sequence in released_pieces(directory).items():
                candidates.setdefault(name, sequence)
            for name, sequence in candidates.items():
                if sequence > cutoff:
                    continue  # inside the window: the cutoff reaches it later
                # "waiting": its release here makes it a candidate again;
                # "held_elsewhere": the next open's directory pass retries it.
                if evict_or_wait(directory, name, sequence) == "evicted":
                    evicted.append(name)
            if self._evicted_through is None or cutoff > self._evicted_through:
                self._evicted_through = cutoff
        return tuple(evicted)

    def _evict_after_request(self) -> None:
        """Evict past retention once this thread's request releases its projection holds.

        A request's pooled handles hold their pieces while it can still read
        them (its base, and the base's parent an advanced tree reads). Evicting
        inside the request would find those generations held here, wait on
        them, and try them again after the scope's release: two tries per
        acceptance. Deferred to scope exit, one pass sees them released and
        tries each generation that crossed the cutoff once.
        """

        def run() -> None:
            self._housekeeping("projection_eviction", self.evict_projection_pieces)

        pool = _PROJECTION_POOL.get()
        if pool is None or not pool.after_close(("projection-eviction", id(self)), run):
            run()

    def _listed_beyond(
        self, directory: Path, recovered: RecoveredInstanceState, cutoff: int
    ) -> dict[str, int]:
        """The first pass in a process: every accepted manifest at or before the cutoff."""

        by_oid = {generation.oid: generation.sequence for generation in recovered.history}
        found: dict[str, int] = {}
        for name in os.listdir(directory):
            if not (name.startswith("projection-") and name.endswith(".json")):
                continue
            try:
                manifest = load_projection_manifest(directory / name)
            except ProjectionIntegrityError:
                continue  # recovery sweeps malformed and old-schema manifests
            sequence = by_oid.get(manifest.git_oid)
            if sequence is not None and sequence <= cutoff:
                found[name] = sequence
        return found

    def _housekeeping(self, step: str, run: Callable[[], object]) -> None:
        """Run one retention step; a failure is logged, never the caller's failure.

        Each step is retried by the next activation or open, and none deletes
        anything before what keeps it readable is durable.
        """

        try:
            run()
        except Exception as exc:
            _log.warning(
                "retention_step_failed",
                step=step,
                instance_id=self.descriptor.instance_id,
                reason=f"{type(exc).__name__}: {exc}",
            )

    def fold_consumption_if_due(self) -> None:
        """Fold raw consumption receipts past ``retention.days``, once per accepted day.

        The clock is the accepted head's commit time, so the cutoff follows the
        ledger, not the wall clock. Housekeeping: a failure is logged and the
        next accepted day retries.
        """

        if is_previewing():
            return
        with self._state_lock:
            oid = self._recovered.coordinate.git_oid
        at = self._ledger.commit_timestamps(oid)[1]
        if self._consumption_folded_on == at.date():
            return
        from cruxible_core.exhaust.consumption import fold_consumption_receipts

        fold_consumption_receipts(self, now=at, days=self.retention.days)
        # Marked only once the fold completed, so a failed day is retried.
        self._consumption_folded_on = at.date()

    @contextmanager
    def holding_accepted_head(self) -> Iterator[str]:
        """Hold accepted main still and yield its commit, for one short critical section.

        Takes the locks every acceptance takes, in the same order (this
        instance's state lock, then the ledger activation lock), so no
        activation in this or any other process can move main until exit and
        the hold cannot deadlock against one. The body must not accept,
        activate or refresh, and must not wait on a thread that does.
        """

        with self._state_lock, self._ledger.activation_lock():
            yield self._ledger.read_main()

    def refresh(self, *, witness: WitnessSink | None = None) -> AcceptedProjectionCoordinate:
        """Replay accepted state and repair publication, excluding concurrent writers."""
        with self._state_lock, self._ledger.activation_lock():
            return self._refresh_locked(witness=witness)

    def _refresh_locked(
        self, *, witness: WitnessSink | None = None
    ) -> AcceptedProjectionCoordinate:
        """Recover with both the instance and ledger activation locks held."""
        paths = self._validated_paths(self.root, self.descriptor.storage)
        bodies = ContentAddressedBodyStore(paths["cas"])
        self.derived.clear()
        self._recovered = recover_instance(
            self._ledger,
            genesis=self._verified_genesis,
            instance_id=self.descriptor.instance_id,
            object_format=self.descriptor.git_object_format,
            compiler=self.descriptor.compiler,
            publication_directory=paths["projections"],
            bodies=bodies,
            witness=witness,
            promotion_verifier=self._promotion_verifier,
            producer_receipt_resolver=local_producer_receipt_resolver(
                exhaust_root=paths["exhaust"],
                instance_id=self.descriptor.instance_id,
                bodies=bodies,
                full_journal=True,
            ),
            query_facts_builder=self._accepted_query_facts,
            checkpoint_directory=self._checkpoint_directory(self.root),
        )
        return self.accepted_coordinate()

    def activation_publisher(
        self,
        *,
        witness: WitnessSink | None = None,
    ) -> ActivationPublisher:
        """Bind PB-D activation to this instance's projection and body stores."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ActivationPublisher(
            self._ledger,
            publication_directory=paths["projections"],
            bodies=ContentAddressedBodyStore(paths["cas"]),
            witness=witness,
            accepted_coordinates_by_sequence=self._accepted_coordinates_by_sequence(),
            checkpoint_directory=self._checkpoint_directory(self.root),
            checkpoint_interval=DEFAULT_CHECKPOINT_INTERVAL,
            genesis=self.descriptor.genesis,
            member_history=self.member_record_history,
            resolve_claim_digest=self._claim_identities_for_digest,
            defer_checkpoint=(
                None if self._quiet_checkpoints is None else self._quiet_checkpoints.defer
            ),
        )

    def defer_replay_checkpoints(self, *, quiet_seconds: float) -> None:
        """Also summarize an off-stride head once acceptances pause for `quiet_seconds`."""

        if self._quiet_checkpoints is None:
            self._quiet_checkpoints = QuietCheckpointWriter(
                quiet_seconds=quiet_seconds,
                name=f"replay-checkpoint-{self.descriptor.instance_id}",
            )

    def flush_replay_checkpoint(self) -> None:
        if self._quiet_checkpoints is not None:
            self._quiet_checkpoints.flush()

    def prepare_generation(
        self,
        *,
        base: AcceptedProjectionCoordinate,
        candidate_tree: Mapping[str, bytes],
        candidate: CandidateRecordAnyVersion,
        approvals: tuple[ApprovalSubmission, ...],
        actor_binding: ChangeActorBinding,
        proposal_actor_id: str,
        sequence: int,
        candidate_tree_oid: str | None = None,
        mandate_digest: str | None = None,
        admitted: AdmittedEvaluation | None = None,
    ) -> VerifiedGenerationBundle:
        """Construct one verified generation without exposing the Git ledger to surfaces.

        ``mandate_digest`` names the settle ProcedureMandate whose delegated
        authority replaces candidate approval; only the settle terminal passes it.
        ``admitted`` is the same request's own admission evaluation (see
        ``settlement.prepare_generation``); only an activation handed its
        submission passes it.
        """

        self.require_writable()
        if any(member.artifact_kind == "compiler-upgrade" for member in candidate.members):
            from cruxible_core.runtime.permissions import check_permission

            check_permission("cruxible_compiler_upgrade", instance_id=self.descriptor.instance_id)
        return prepare_generation(
            self._ledger,
            base=base,
            candidate_tree=candidate_tree,
            candidate_tree_oid=candidate_tree_oid,
            candidate=candidate,
            approval_submissions=approvals,
            bodies=self.body_store(),
            actor_binding=actor_binding,
            proposal_actor_id=proposal_actor_id,
            sequence=sequence,
            mandate_digest=mandate_digest,
            promotion_verifier=self._promotion_verifier,
            producer_receipt_resolver=self.capture_verification_context(),
            query_facts_provider=lambda coordinate, *, predicates=None, subject_kinds=None: (
                self._accepted_query_facts(
                    self, coordinate, predicates=predicates, subject_kinds=subject_kinds
                )
            ),
            tree_state_provider=derive_indexed_state,
            accepted_tree_provider=self.immutable_tree_at,
            claim_law_provider=self.accepted_claim_law_evidence,
            attestation_principal_provider=self.accepted_principal,
            accepted_referents_provider=self.accepted_referent_coordinates,
            historical_artifact_provider=self.accepted_artifact_version,
            principal_registry_provider=self.accepted_principal_registry,
            subject_assignment_authority=self.subject_assignment_authority(),
            admitted=admitted,
            parent_derivation=self._parent_derivation(base, sequence=sequence),
        )

    def _parent_derivation(
        self, base: AcceptedProjectionCoordinate, *, sequence: int
    ) -> SemanticRootDerivation | None:
        """The base generation's semantic-root derivation, when this instance holds it.

        The verified head keeps its own receipt, so a settle on the head reads
        the one fact its successor's root needs from it instead of re-reading
        and re-parsing the parent's whole change-set record. Any other base
        (``None``) is read from its tree as before.
        """

        if sequence <= 1:
            return record_semantic_root_derivation(None)
        with self._state_lock:
            head = self._recovered.head
        if (
            head.oid != base.git_oid
            or head.sequence != sequence - 1
            or head.retained_record is None
            or head.retained_record.sequence != head.sequence
        ):
            return None
        return record_semantic_root_derivation(head.retained_record)

    def settle_and_activate(
        self,
        *,
        base: AcceptedProjectionCoordinate,
        candidate_tree: Mapping[str, bytes],
        candidate: CandidateRecordAnyVersion,
        approvals: tuple[ApprovalSubmission, ...],
        actor_binding: ChangeActorBinding,
        proposal_actor_id: str,
        candidate_tree_oid: str | None = None,
        mandate_digest: str | None = None,
        admitted: AdmittedEvaluation | None = None,
    ) -> ActivationResult:
        """Publish one owned preparation and install its verified successor state.

        Ordinary success retains the verified prefix and the exact successor;
        lost CAS, stale epochs and publication failures use recovery. Locks are
        released before callers reconcile advisory review/workspace surfaces.
        """
        with self._state_lock, self._ledger.generation_attempt():
            previous = self._recovered
            bundle = self.prepare_generation(
                base=base,
                candidate_tree=candidate_tree,
                candidate=candidate,
                approvals=approvals,
                actor_binding=actor_binding,
                proposal_actor_id=proposal_actor_id,
                sequence=previous.head.sequence + 1,
                candidate_tree_oid=candidate_tree_oid,
                mandate_digest=mandate_digest,
                admitted=admitted,
            )
            successor = (
                prepared_generation_for_handoff(
                    self._ledger, parent=previous.head, bundle=bundle, candidate=candidate
                )
                if previous.coordinate == base
                else None
            )
            publisher = self.activation_publisher()

            def install(result: ActivationResult) -> None:
                # The publisher holds the cross-process lock until this returns.
                # Never install a stale epoch or manufacture an accepted result
                # when the CAS lost. Full recovery supplies the winning history.
                if (
                    result.status != "accepted"
                    or successor is None
                    or self._recovered is not previous
                    or result.accepted is None
                    or result.projection is None
                    or self._ledger.read_main() != bundle.oid
                ):
                    self._refresh_locked()
                    return
                expected = AcceptedProjectionCoordinate(
                    instance_id=base.instance_id,
                    repository_path=base.repository_path,
                    git_object_format=base.git_object_format,
                    git_oid=bundle.oid,
                    semantic_root=bundle.semantic_root.tagged,
                    generation_root=bundle.generation_root.tagged,
                    compiler=successor.compiler,
                )
                if result.accepted != expected:
                    raise SettlementIntegrityError(
                        "activation handoff coordinate differs from bundle"
                    )
                if self._ledger.read_generation_note(bundle.oid) != render_generation_descriptor(
                    successor.descriptor
                ):
                    self._refresh_locked()
                    return
                try:
                    with bind_current_projection(
                        publisher.publication_directory, expected=expected
                    ):
                        pass
                except (OSError, ProjectionIntegrityError):
                    self._refresh_locked()
                    return
                advanced = RecoveredInstanceState(
                    genesis=previous.genesis,
                    head=successor,
                    # The previous head's record now lives only in the ledger.
                    history=(
                        *previous.history[:-1],
                        previous.history[-1].released(self._ledger.record_at),
                        successor,
                    ),
                    coordinate=copy.deepcopy(result.accepted),
                    projection=copy.deepcopy(result.projection),
                )
                self._recovered = advanced

            try:
                projection = publisher.prebuild(bundle, base=base)
                activated = publisher.activate(bundle, projection, base=base, on_completed=install)
            except Exception:
                # Main may have moved before a post-CAS publication failure.
                # Repair from authority, never install a partially published
                # outcome. Preserve the caller's failure even if repair succeeds.
                self.refresh()
                raise
        if activated.status == "accepted":
            # Outside the activation locks: eviction is unlinks, and a bind
            # that wants an evicted coordinate rebuilds it under the same lock.
            with phase("post_accept"):
                with self._state_lock:
                    head = self._recovered.head.sequence
                self._evict_after_request()
                self._housekeeping("consumption_fold", self.fold_consumption_if_due)
                self._housekeeping(
                    "candidate_retirement", lambda: self.retire_settled_candidates((head,))
                )
        return activated


__all__ = ["DESCRIPTOR_FILE", "PlaybillInstance"]
