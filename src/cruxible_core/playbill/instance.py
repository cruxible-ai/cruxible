"""Opt-in managed Playbill instance initialization, reopening, and inspection."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicyV1,
    parse_approval_policy,
)
from cruxible_client.contracts.attestations import ApprovalSubmission
from cruxible_client.contracts.candidates import CandidateRecordAnyVersion
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import (
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillInstanceDecommissioned,
    PlaybillKeyError,
    ProposalIntegrityError,
)
from cruxible_client.contracts.ledger_mirror import validate_mirror_url
from cruxible_client.contracts.temporal import format_datetime, utc_now
from cruxible_client.contracts.types import (
    GenesisCoordinate,
    GitObjectFormat,
    OperatingProfile,
    PlaybillDecommissionV1,
    PlaybillDescriptor,
    PlaybillInspection,
    PlaybillTrustRoot,
    PrincipalInspection,
    PrincipalRecord,
    RecoveryPosture,
    StorageLayout,
    initial_authority_matrix,
)
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)
from cruxible_core.playbill.activation import ActivationPublisher
from cruxible_core.playbill.assembler import ProjectionAssembler, ProjectionCrashHook
from cruxible_core.playbill.bootstrap import (
    VerifiedGenesis,
    prepare_genesis,
    seeded_procedure_runtime_policy,
    verify_genesis,
)
from cruxible_core.playbill.cas import CasObjectMetadata, ContentAddressedBodyStore
from cruxible_core.playbill.checkpoints import (
    CHECKPOINT_DIRECTORY,
    DEFAULT_CHECKPOINT_INTERVAL,
)
from cruxible_core.playbill.compiler import (
    SUPPORTED_COMPILERS,
    current_compiler_coordinate,
)
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.keys import (
    ALLOWED_SIGNERS_FILE,
    DAEMON_PRIVATE_KEY_FILE,
    DAEMON_PUBLIC_KEY_FILE,
    generate_daemon_key,
    public_key_hex_from_private_file,
    raw_public_key_hex_from_openssh,
)
from cruxible_core.playbill.ledger_mirror import (
    LedgerMirrorStateV1,
    mirror_credential_environment,
    mirror_lock,
    read_mirror_state,
    write_mirror_state,
)
from cruxible_core.playbill.memo import memo_get, memo_put
from cruxible_core.playbill.producer_receipts import local_producer_receipt_resolver
from cruxible_core.playbill.projection import (
    AcceptedCoordinate,
    AcceptedProjectionCoordinate,
    AssemblerResult,
    projection_manifest_name,
)
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposal_message import proposal_commit_message
from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex
from cruxible_core.playbill.proposals import (
    ExhaustPromotionVerifierProtocol,
    ProposalReceiveLimits,
    ProposalService,
)
from cruxible_core.playbill.recovery import (
    RecoveredGeneration,
    RecoveredInstanceState,
    recover_instance,
)
from cruxible_core.playbill.review_operational import ReviewOperationalStore
from cruxible_core.playbill.serving import bind_current_projection
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    VerifiedGenerationBundle,
    prepare_generation,
)
from cruxible_core.playbill.witness import WitnessSink
from cruxible_core.storage.playbill_projection import ProjectionHandle, bind_projection

if TYPE_CHECKING:
    from cruxible_core.playbill.claim_attestation_store import ClaimAttestationEvidenceStore
    from cruxible_core.playbill.query.backends import ClaimQueryFactsV1

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
        raise PlaybillBootstrapError("bootstrap client principals must be unique")
    ordinary = [record for record in ordered if record.kind == "ordinary"]
    if len(ordinary) < 1:
        raise PlaybillBootstrapError("bootstrap requires at least one ordinary principal")
    if any(record.principal_id == "daemon" for record in ordered):
        raise PlaybillBootstrapError("the daemon principal is generated by the instance")
    if any(record.status != "active" for record in ordered):
        raise PlaybillBootstrapError("bootstrap client principals must begin active")
    if any(record.kind == "daemon" for record in ordered):
        raise PlaybillBootstrapError("client principals cannot have daemon kind")
    recovery_configured = any(record.kind == "recovery" for record in ordered)
    posture: RecoveryPosture = (
        "recovery-configured" if recovery_configured else "narrowed-no-recovery"
    )
    return ordered, posture


# An accepted tree is immutable, so the only cost of a stale entry is memory.
# Four generations cover every read path that walks a bounded lineage while
# keeping the resident set to a handful of trees per served instance.
_TREE_MEMO_GENERATIONS = 4
# One index over metadata already owned by replay, with a hard entry ceiling.
# Larger histories retain the original scan semantics instead of truncating.
_HISTORY_LOOKUP_MAX_GENERATIONS = 65_536


class PlaybillInstance:
    """A verified opt-in Playbill substrate rooted outside agent workspaces."""

    def __init__(
        self,
        root: Path,
        descriptor: PlaybillDescriptor,
        trust_root: PlaybillTrustRoot,
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
        self._history_lookup: (
            tuple[RecoveredInstanceState, dict[str, RecoveredGeneration | None] | None] | None
        ) = None
        self._promotion_verifier = promotion_verifier
        self._claim_attestation_store: ClaimAttestationEvidenceStore | None = None
        self._workspace_advertiser: Callable[[], PlaybillWorkspaceAdvertisement] | None = None
        self._receive_limits = ProposalReceiveLimits()
        self._mirror_condition = threading.Condition()
        self._mirror_thread: threading.Thread | None = None
        self._tree_memo: OrderedDict[str, dict[str, bytes]] = OrderedDict()
        # Read services keyed by accepted coordinate park their derived
        # history indexes here so activation drops them with one clear().
        self.claim_read_history_memo: OrderedDict[str, object] = OrderedDict()

    @staticmethod
    def _accepted_query_facts(
        source: object,
        coordinate: AcceptedProjectionCoordinate,
    ) -> "ClaimQueryFactsV1":
        """Build the one accepted-Claim facts projection for live and replay paths."""

        from cruxible_core.service.playbill_evidence import ClaimReadSourceProtocol
        from cruxible_core.service.playbill_query import build_accepted_query_facts

        return build_accepted_query_facts(
            cast(ClaimReadSourceProtocol, source),
            coordinate=coordinate,
        )

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
            raise PlaybillBootstrapError("managed Playbill root must be absolute")
        managed_root = _resolved(root)
        for workspace in workspace_roots:
            if _is_within(managed_root, _resolved(workspace)):
                raise PlaybillBootstrapError(
                    "managed Playbill root must be outside every declared agent workspace"
                )
        if managed_root.exists():
            raise PlaybillBootstrapError(f"managed Playbill root already exists: {managed_root}")

        clients, recovery_posture = _validate_client_principals(client_principals)
        if (
            require_independent_approval
            and sum(record.kind == "ordinary" and record.status == "active" for record in clients)
            < 2
        ):
            raise PlaybillBootstrapError(
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
            trust_root = PlaybillTrustRoot(
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
                raise PlaybillBootstrapError("failed to produce a canonical bootstrap timestamp")
            verified = prepare_genesis(
                ledger,
                trust_root=trust_root,
                approval_policy=ApprovalPolicyV1(
                    mode=(
                        "independent_approval_required"
                        if require_independent_approval
                        else "self_approval_allowed"
                    )
                ),
                procedure_runtime_policy=seeded_procedure_runtime_policy(),
                timestamp=commit_timestamp,
            )
            descriptor = PlaybillDescriptor(
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
        trust_root: PlaybillTrustRoot,
        witness: WitnessSink | None = None,
        promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    ) -> "PlaybillInstance":
        """Reopen only after replaying descriptor, key, signature, and both roots."""

        managed_root = _resolved(root)
        descriptor_path = managed_root / DESCRIPTOR_FILE
        if descriptor_path.is_symlink():
            raise PlaybillFormatError("Playbill descriptor may not be a symlink")
        try:
            raw_descriptor = descriptor_path.read_bytes()
            payload = json.loads(raw_descriptor)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlaybillFormatError("Playbill descriptor is missing or malformed") from exc
        if not isinstance(payload, dict):
            raise PlaybillFormatError("Playbill descriptor must be a JSON object")
        if payload.get("format_version") != 1 or payload.get("tag") != "playbill-instance-v1":
            raise PlaybillFormatError(
                "unsupported Playbill descriptor version; upgrade or use an explicit fork/import"
            )
        if payload.get("git_object_format") not in {"sha1", "sha256"}:
            raise PlaybillFormatError(
                "unsupported Playbill Git object format; use an explicit fork/import"
            )
        try:
            descriptor = PlaybillDescriptor.model_validate(payload)
        except ValidationError as exc:
            raise PlaybillFormatError("Playbill descriptor failed strict validation") from exc
        if canonical_bytes(descriptor.model_dump(mode="json")) + b"\n" != raw_descriptor:
            raise PlaybillFormatError("Playbill descriptor is not canonical")
        if descriptor.instance_id != trust_root.instance_id:
            raise PlaybillBootstrapError("descriptor instance ID differs from trust root")
        if descriptor.daemon_public_key != trust_root.daemon_public_key:
            raise PlaybillBootstrapError("descriptor daemon key differs from trust root")
        if descriptor.compiler not in SUPPORTED_COMPILERS:
            raise PlaybillFormatError("descriptor compiler coordinate is unsupported")
        if descriptor.authority != initial_authority_matrix():
            raise PlaybillBootstrapError(
                "PB-A descriptor authority matrix differs from the initial migration state"
            )
        if descriptor.storage != StorageLayout():
            raise PlaybillFormatError(
                "descriptor storage layout is unsupported; use an explicit fork/import"
            )
        recovery_configured = any(
            principal.kind == "recovery" for principal in trust_root.principals
        )
        expected_recovery: RecoveryPosture = (
            "recovery-configured" if recovery_configured else "narrowed-no-recovery"
        )
        if descriptor.recovery_posture != expected_recovery:
            raise PlaybillBootstrapError(
                "descriptor recovery posture differs from bootstrap principals"
            )

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
                raise PlaybillKeyError(f"managed {name} must be a regular file")
        if public_key_hex_from_private_file(private_key_path) != trust_root.daemon_public_key:
            raise PlaybillKeyError("daemon private key does not match the trust root")
        if (
            raw_public_key_hex_from_openssh(public_key_path.read_bytes())
            != trust_root.daemon_public_key
        ):
            raise PlaybillKeyError("daemon public key does not match the trust root")
        private_mode = stat.S_IMODE(private_key_path.stat().st_mode)
        if private_mode & 0o077:
            raise PlaybillKeyError("daemon private key permissions expose group/world access")

        ledger = GitLedger(
            paths["ledger"],
            signing_key_path=private_key_path,
            allowed_signers_path=allowed_signers_path,
        )
        if ledger.object_format() != descriptor.git_object_format:
            raise PlaybillFormatError(
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
            raise PlaybillBootstrapError("recorded genesis coordinates do not reproduce")
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
        if descriptor.mirror_url is not None and descriptor.decommissioned is None:
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
        paths: dict[str, Path] = {}
        for name, relative in layout.model_dump().items():
            path = root / relative
            if path.is_symlink():
                raise PlaybillFormatError(f"managed storage path may not be a symlink: {name}")
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                raise PlaybillFormatError(f"managed storage path is missing: {name}") from exc
            if not _is_within(resolved, root) or not resolved.is_dir():
                raise PlaybillFormatError(f"managed storage path escapes instance root: {name}")
            paths[name] = resolved
        return paths

    def inspect(self) -> PlaybillInspection:
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
        accepted_tree = self.tree_at(self._recovered.head.oid)
        approval_policy = parse_approval_policy(
            accepted_tree[APPROVAL_POLICY_PATH],
            path=APPROVAL_POLICY_PATH,
        )
        return PlaybillInspection(
            descriptor_tag=self.descriptor.tag,
            format_version=self.descriptor.format_version,
            instance_id=self.descriptor.instance_id,
            git_object_format=self.descriptor.git_object_format,
            head_oid=self._recovered.head.oid,
            bootstrap_root=self._verified_genesis.bootstrap_root.tagged,
            semantic_root=self._recovered.head.semantic_root.tagged,
            generation_root=self._recovered.head.generation_root.tagged,
            compiler=self.descriptor.compiler,
            authority=self.descriptor.authority,
            operating_profile=self.descriptor.operating_profile,
            recovery_posture=self.descriptor.recovery_posture,
            approval_policy_mode=approval_policy.mode,
            principals=principals,
            managed_root=str(self.root),
            storage_directories=storage_directories,
            daemon_private_key_present=(paths["credentials"] / DAEMON_PRIVATE_KEY_FILE).is_file(),
        )

    def accepted_coordinate(self) -> AcceptedProjectionCoordinate:
        """Return the verified accepted coordinate without consulting proposal refs."""

        return self._recovered.coordinate

    def _accepted_coordinates_by_sequence(self) -> dict[int, AcceptedCoordinate]:
        """Return replay-proven historical coordinates for stable derived activations."""

        compiler_digest = self.descriptor.compiler.rule_digest
        return {
            generation.sequence: AcceptedCoordinate(
                git_oid=generation.oid,
                semantic_root=generation.semantic_root.tagged,
                generation_root=generation.generation_root.tagged,
                compiler_digest=compiler_digest,
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
        )

    def body_store(self) -> ContentAddressedBodyStore:
        """Return PB-C's inert, access-controlled content-addressed body store."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ContentAddressedBodyStore(
            paths["cas"],
            reservation_root=paths["leases"] / "procedure-material",
        )

    @property
    def is_decommissioned(self) -> bool:
        """Return whether this instance has reached its terminal lifecycle state."""

        return self.descriptor.decommissioned is not None

    def require_writable(self) -> None:
        """Refuse typed when this instance no longer accepts governed writes."""

        terminal = self.descriptor.decommissioned
        if terminal is None:
            return
        raise PlaybillInstanceDecommissioned(
            instance_id=self.descriptor.instance_id,
            reason=terminal.reason,
            decommissioned_at=terminal.decommissioned_at,
        )

    def _persisted_descriptor(self) -> PlaybillDescriptor:
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
            raise PlaybillFormatError("Playbill descriptor is missing or malformed") from exc
        try:
            return PlaybillDescriptor.model_validate(payload)
        except ValidationError as exc:
            raise PlaybillFormatError("Playbill descriptor failed strict validation") from exc

    def _rewrite_descriptor(self, **updates: object) -> PlaybillDescriptor:
        """Persist one operational descriptor change over the bytes on disk."""

        updated = self._persisted_descriptor().model_copy(update=updates)
        _atomic_replace(
            self.root / DESCRIPTOR_FILE,
            canonical_bytes(updated.model_dump(mode="json")) + b"\n",
        )
        self.descriptor = updated
        return updated

    def _persisted_decommission(self) -> PlaybillDecommissionV1 | None:
        """Read the terminal record from disk rather than from this handle."""

        return self._persisted_descriptor().decommissioned

    def decommission(self, *, reason: str, decommissioned_by: str) -> PlaybillDecommissionV1:
        """Stamp the terminal lifecycle state on the descriptor, deleting nothing.

        The record lands in the descriptor, which every reopen replays and
        re-renders canonically, so the state survives a daemon restart without a
        separate operational store that could disagree with it. Repeating the
        call is refused rather than silently restamping: a second reason would
        overwrite the first without a record.
        """

        self.require_writable()
        # Two handles can be open over one directory. This handle's in-memory
        # descriptor is not authority for a terminal state another handle may
        # already have stamped, so the persisted record decides.
        persisted = self._persisted_decommission()
        if persisted is not None:
            self.descriptor = self.descriptor.model_copy(update={"decommissioned": persisted})
            self.require_writable()
        record = PlaybillDecommissionV1(
            reason=reason,
            decommissioned_at=format_datetime(utc_now()) or "",
            decommissioned_by=decommissioned_by,
        )
        self._rewrite_descriptor(decommissioned=record)
        return record

    def ledger_mirror_url(self) -> str | None:
        """Return where this ledger publishes itself, or None if it publishes nowhere."""

        return self.descriptor.mirror_url

    def set_ledger_mirror(self, url: str) -> LedgerMirrorStateV1 | None:
        """Bind a remote and publish to it at once, so a bad one is found now.

        Setting a mirror without pushing to it would leave the operator holding
        a URL whose credential, reachability and permissions are all unverified
        until the next governed write happens to exercise them. Publishing here
        makes the answer immediate, and it is still only a publication: a push
        that fails records the reason and leaves the mirror bound, because a
        remote that is temporarily unreachable is not a wrong remote.
        """

        self.require_writable()
        self._rewrite_descriptor(mirror_url=validate_mirror_url(url))
        return self.publish_ledger_mirror()

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
                url = PlaybillDescriptor.model_validate_json(
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
                        target=self._run_ledger_publisher,
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
                        target=self._run_ledger_publisher,
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

    def _publish_ledger_mirror_once(self) -> LedgerMirrorStateV1 | None:
        """Serialize publication across handles/processes and acknowledge exact refs."""

        with mirror_lock(self.root, publication=True):
            with mirror_lock(self.root):
                state = read_mirror_state(self.root)
                if state is None:
                    return None
                # Use current durable configuration, not a stale instance handle.
                descriptor = PlaybillDescriptor.model_validate_json(
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

    def proposal_service(self) -> ProposalService:
        """Bind PB-C proposal evaluation to authenticated main and inert storage."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        bodies = ContentAddressedBodyStore(paths["cas"])
        return ProposalService(
            self._ledger,
            accepted=self.accepted_coordinate(),
            bodies=bodies,
            evidence=ProposalEvidenceStore(paths["exhaust"]),
            review_projection_lock=self.review_projection_lock,
            current_coordinate=self.accepted_coordinate,
            promotion_verifier=self._promotion_verifier,
            producer_receipt_resolver=local_producer_receipt_resolver(
                exhaust_root=paths["exhaust"],
                instance_id=self.descriptor.instance_id,
                bodies=bodies,
            ),
            query_facts_provider=lambda coordinate: self._accepted_query_facts(self, coordinate),
            workspace_advertiser=self.advertise_workspace,
            receive_limits=self._receive_limits,
            require_writable=self.require_writable,
            ledger_publisher=self.request_ledger_mirror,
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
        advertiser: Callable[[], PlaybillWorkspaceAdvertisement],
    ) -> None:
        """Bind the manager-owned advisory Git hook for this process."""

        self._workspace_advertiser = advertiser

    def advertise_workspace(self) -> PlaybillWorkspaceAdvertisement:
        """Refresh advisory refs, or report that this instance has no attachment."""

        if self._workspace_advertiser is None:
            return NOT_ATTACHED_ADVERTISEMENT
        try:
            self._reconcile_proposal_review_refs()
            return self._workspace_advertiser()
        except BaseException:
            return PlaybillWorkspaceAdvertisement(
                status="failed",
                workspace_path=None,
                failure_code="unexpected_failure",
            )

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
        """Project exactly the open proposal trees into standard ledger branch refs.

        The notes travel with the branch. They were attached only to
        `admission.candidate_commit_oid` -- the tip of `refs/proposals/<actor>/<name>`,
        which is neither mirrored nor fetched into a workspace -- while the
        commit a reviewer actually receives is the one rebuilt here. The two are
        different objects whenever evaluation derives cards or rebases, which is
        the ordinary case, so `git notes --ref=refs/notes/playbill-eval show
        <commit>` (the command `proposal review` prints and both docs pages
        promise) found nothing for anyone but the daemon. Attaching the same
        bytes to the projected commit is what makes the published note readable
        by the reviewer it is published for.

        Completed retained proposals rebuild both open and settled refs. One
        evaluation index groups their original and advisory commit aliases;
        existing notes are compared before a write. Approval rendering shares
        the approval door's per-candidate lock from evidence read
        through Git write. A delayed renderer cannot overwrite a newer approval
        projection. All reconciliation callers also hold the review projection
        lock, so an older workspace writer cannot resurrect settled branches.
        """

        recovered = self._recovered
        coordinate = recovered.coordinate
        accepted_candidates = {
            generation.record.candidate_digest
            for generation in recovered.history
            if generation.record is not None
        }
        evidence = self.proposal_evidence()
        index = ProposalNoteIndex.build(evidence, self._ledger)
        refs: dict[str, str] = {}
        settled_refs: dict[str, str] = {}
        published: dict[str, tuple[str, str | None]] = {}
        for oid, proposal_ids in index.proposal_ids_by_oid.items():
            first = min(proposal_ids)
            published[oid] = (first, index.evaluations[first].candidate_digest)
        for admission in index.admissions.values():
            evaluation = index.evaluations[admission.proposal_id]
            candidate_digest = evaluation.candidate_digest
            if candidate_digest is None or evaluation.evaluated_tree_oid is None:
                continue
            candidate = index.candidates[candidate_digest]
            is_settled = (
                candidate_digest in accepted_candidates
                or evidence.read_withdrawal(admission.proposal_id) is not None
                or candidate.candidate.parent_semantic_root != coordinate.semantic_root
            )
            # A coalesced publisher may never observe this candidate while open.
            # Rebuild its archive from evidence, not from the disposable branch
            # inventory left by a previous advertisement.
            review_oid = self._ledger.proposal_review_commit(
                tree_oid=evaluation.evaluated_tree_oid,
                base_oid=evaluation.evaluated_base_oid,
                actor_id=admission.actor_id,
                timestamp=admission.admitted_at,
                message=proposal_commit_message(candidate.members, rationale=admission.rationale),
            )
            if review_oid != index.review_oids[admission.proposal_id]:
                raise ProposalIntegrityError("materialized review alias differs from its index")
            destination = settled_refs if is_settled else refs
            destination[admission.proposal_id.removeprefix("sha256:")] = review_oid
        self._ledger.replace_proposal_review_refs(refs, settled_refs=settled_refs)
        # After the refs, so every annotated commit is already reachable from
        # one: a note on an unreferenced object is a note a `gc` may collect.
        for review_oid, (proposal_id, candidate_digest) in published.items():
            self._publish_review_commit_notes(
                evidence,
                review_oid=review_oid,
                proposal_id=proposal_id,
                candidate_digest=candidate_digest,
                index=index,
            )

    def _publish_review_commit_notes(
        self,
        evidence: ProposalEvidenceStore,
        *,
        review_oid: str,
        proposal_id: str,
        candidate_digest: str | None,
        index: ProposalNoteIndex | None = None,
    ) -> None:
        """Restate one proposal's evidence onto the commit a reviewer receives."""

        grouped = index or ProposalNoteIndex.build(evidence, self._ledger)
        if proposal_id not in grouped.proposal_ids_by_oid.get(review_oid, ()):
            raise ProposalIntegrityError("review note target does not belong to the proposal")
        with ExitStack() as locks:
            for digest in grouped.candidate_digests(review_oid):
                locks.enter_context(self.approval_note_lock(digest))
            grouped.publish(self._ledger, (review_oid,))

    def proposal_evidence(self) -> ProposalEvidenceStore:
        """Return the immutable non-authoritative proposal/approval evidence store."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        return ProposalEvidenceStore(paths["exhaust"])

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

        from cruxible_core.playbill.claim_attestation_store import (
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
        """Return the genesis-rooted, replay-verified accepted history."""

        return self._recovered.history

    def _generation_for_oid(self, oid: str) -> RecoveredGeneration | None:
        """Resolve unique membership only in this captured replay-verified epoch.

        An index hit replaces a history scan, never recovery or a blob proof.
        The atomic epoch/index pair prevents concurrent refresh from applying
        another history's membership. Duplicate identities remain ambiguous.
        """
        recovered = self._recovered
        cached = self._history_lookup
        if cached is None or cached[0] is not recovered:
            index: dict[str, RecoveredGeneration | None] | None = None
            if len(recovered.history) <= _HISTORY_LOOKUP_MAX_GENERATIONS:
                index = {}
                for generation in recovered.history:
                    index[generation.oid] = None if generation.oid in index else generation
            cached = (recovered, index)
            self._history_lookup = cached
        if cached[1] is not None:
            return cached[1].get(oid)
        matches = tuple(generation for generation in recovered.history if generation.oid == oid)
        return matches[0] if len(matches) == 1 else None

    def accepted_evaluation_time(self, oid: str) -> datetime:
        """Resolve the immutable acceptance instant for one replayed generation."""

        generation = self._generation_for_oid(oid)
        if generation is None:
            raise PlaybillFormatError("evaluation coordinate is outside accepted history")
        if generation.record is not None:
            try:
                return datetime.strptime(
                    generation.record.candidate.timestamp,
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                ).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise PlaybillFormatError("accepted candidate timestamp is malformed") from exc
        author_time, committer_time = self._ledger.commit_timestamps(oid)
        if author_time != committer_time:
            raise PlaybillFormatError("genesis author and committer timestamps disagree")
        return author_time

    def coordinate_for_oid(self, oid: str) -> AcceptedProjectionCoordinate:
        """Resolve one accepted historical OID to its complete verified coordinate."""

        generation = self._generation_for_oid(oid)
        if generation is None:
            raise PlaybillFormatError("Git OID is not one accepted generation of this instance")
        return AcceptedProjectionCoordinate(
            instance_id=self.descriptor.instance_id,
            repository_path=str(self._ledger.path.resolve(strict=True)),
            git_object_format=self.descriptor.git_object_format,
            git_oid=generation.oid,
            semantic_root=generation.semantic_root.tagged,
            generation_root=generation.generation_root.tagged,
            compiler=self.descriptor.compiler,
        )

    def generation_for_semantic_root(self, semantic_root: str) -> RecoveredGeneration:
        """Resolve one historical signing root without consulting mutable proposal state."""

        matches = tuple(
            generation
            for generation in self._recovered.history
            if generation.semantic_root.tagged == semantic_root
        )
        if len(matches) != 1:
            raise PlaybillFormatError(
                "semantic root is not one accepted generation of this instance"
            )
        return matches[0]

    def tree_at(self, oid: str) -> dict[str, bytes]:
        """Read an exact Git tree only after proving the OID is accepted history.

        An accepted generation's tree is immutable by construction, so the read
        is memoized per OID behind the same acceptance proof: the proof runs on
        every call and only the Git subprocess pair is elided. Callers keep the
        mutable-dict contract they had before, so each hit returns a fresh
        shallow copy (a pointer copy per path, not a byte copy).
        """

        self.coordinate_for_oid(oid)
        cached = memo_get(self._tree_memo, oid)
        if cached is None:
            cached = self._ledger.read_tree(oid)
            memo_put(self._tree_memo, oid, cached, capacity=_TREE_MEMO_GENERATIONS)
        return dict(cached)

    def paths_at(self, oid: str) -> tuple[str, ...]:
        """List accepted paths without reading a single blob payload.

        The listing carries the same whole-generation blob proof ``tree_at``
        applies, so a generation a read would refuse is refused here too and a
        warm memo and a cold listing answer identically.
        """

        self.coordinate_for_oid(oid)
        cached = memo_get(self._tree_memo, oid)
        if cached is not None:
            return tuple(cached)
        return self._ledger.paths_at(oid)

    def blob_at(self, oid: str, path: str) -> bytes | None:
        """Read one accepted path without materializing its whole generation."""

        return self.blobs_at(oid, (path,)).get(path)

    def blobs_at(self, oid: str, paths: Sequence[str]) -> dict[str, bytes]:
        """Read an exact set of accepted paths under the same acceptance proof."""

        self.coordinate_for_oid(oid)
        cached = memo_get(self._tree_memo, oid)
        if cached is not None:
            return {path: cached[path] for path in dict.fromkeys(paths) if path in cached}
        return self._ledger.blobs_at(oid, paths)

    def proposal_tree(self, oid: str) -> dict[str, bytes]:
        """Read one proposal commit tree for evidence-bound settlement."""

        return self._ledger.read_tree(oid)

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
            raise PlaybillFormatError("accepted coordinate triple has mixed members")
        if compiler_digest is not None and coordinate.compiler.rule_digest != compiler_digest:
            raise PlaybillFormatError("accepted coordinate compiler digest is unsupported")
        return coordinate

    def bind_accepted_projection(
        self,
        coordinate: AcceptedProjectionCoordinate,
    ) -> ProjectionHandle:
        """Bind a current serving or retained historical immutable projection."""

        verified = self.resolve_accepted_coordinate(
            git_oid=coordinate.git_oid,
            semantic_root=coordinate.semantic_root,
            generation_root=coordinate.generation_root,
            compiler_digest=coordinate.compiler.rule_digest,
        )
        paths = self._validated_paths(self.root, self.descriptor.storage)
        if verified == self.accepted_coordinate():
            return bind_current_projection(paths["projections"], expected=verified)
        assembler = ProjectionAssembler(
            self._ledger,
            accepted=verified,
            publication_directory=paths["projections"],
            bodies=ContentAddressedBodyStore(paths["cas"]),
            accepted_coordinates_by_sequence=self._accepted_coordinates_by_sequence(),
        )
        request = assembler.request(
            output_staging_directory=paths["projections"] / ".historical-bind"
        )
        manifest_path = paths["projections"] / projection_manifest_name(request)
        return bind_projection(manifest_path, expected=verified)

    def refresh(self, *, witness: WitnessSink | None = None) -> AcceptedProjectionCoordinate:
        """Replay accepted state after activation and repair its serving boundary."""

        paths = self._validated_paths(self.root, self.descriptor.storage)
        bodies = ContentAddressedBodyStore(paths["cas"])
        self._tree_memo.clear()
        self.claim_read_history_memo.clear()
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
            ),
            query_facts_builder=self._accepted_query_facts,
            checkpoint_directory=self._checkpoint_directory(self.root),
        )
        self._history_lookup = None
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
        )

    def prepare_generation(
        self,
        *,
        base: AcceptedProjectionCoordinate,
        candidate_tree: dict[str, bytes],
        candidate: CandidateRecordAnyVersion,
        approvals: tuple[ApprovalSubmission, ...],
        actor_binding: ChangeActorBinding,
        proposal_actor_id: str,
        sequence: int,
    ) -> VerifiedGenerationBundle:
        """Construct one verified generation without exposing the Git ledger to surfaces."""

        self.require_writable()
        return prepare_generation(
            self._ledger,
            base=base,
            candidate_tree=candidate_tree,
            candidate=candidate,
            approval_submissions=approvals,
            bodies=self.body_store(),
            actor_binding=actor_binding,
            proposal_actor_id=proposal_actor_id,
            sequence=sequence,
            promotion_verifier=self._promotion_verifier,
            producer_receipt_resolver=local_producer_receipt_resolver(
                exhaust_root=self._validated_paths(self.root, self.descriptor.storage)["exhaust"],
                instance_id=self.descriptor.instance_id,
                bodies=self.body_store(),
            ),
            query_facts_provider=lambda coordinate: self._accepted_query_facts(self, coordinate),
        )

    def assemble_projection(
        self,
        *,
        crash_hook: ProjectionCrashHook | None = None,
    ) -> AssemblerResult:
        """Build and publish the current verified generation for internal serving."""

        assembler = self.projection_assembler()
        staging = assembler.publication_directory / f".stage-{secrets.token_hex(12)}"
        return assembler.assemble(
            assembler.request(output_staging_directory=staging),
            crash_hook=crash_hook,
        )


__all__ = ["DESCRIPTOR_FILE", "PlaybillInstance"]
