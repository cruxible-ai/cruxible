"""Genesis-rooted replay, half-publication repair, and serving admission recovery."""

from __future__ import annotations

import json
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from cruxible_client.contracts.attestations import verify_candidate_approvals
from cruxible_client.contracts.candidates import (
    CandidateRecord,
    CandidateRecordAnyVersion,
    CandidateRecordV2,
    CandidateRecordV3,
)
from cruxible_client.contracts.canonical import (
    GenerationRoot,
    SemanticRoot,
    canonical_bytes,
)
from cruxible_client.contracts.captures import ProducerReceiptResolverProtocol
from cruxible_client.contracts.errors import (
    PlaybillError,
    PlaybillGitError,
    PlaybillInstanceIncompatiblePrereleaseContent,
    ProjectionIntegrityError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.laws import PLAYBILL_ACCEPTANCE_LAWS, AcceptanceLawRegistry
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    parse_principal_record,
    principal_registry_from_tree,
)
from cruxible_client.contracts.types import (
    CompilerCoordinate,
    GenerationDescriptor,
    GenesisCoordinate,
    GitObjectFormat,
)
from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.bootstrap import VerifiedGenesis, generation_root
from cruxible_core.playbill.cas import BodyProjectionProtocol
from cruxible_core.playbill.checkpoints import (
    ReplayCheckpointBodyV2,
    checkpoint_body,
    load_verified_checkpoint,
    write_checkpoint,
)
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.projection import (
    AcceptedCoordinate,
    AcceptedProjectionCoordinate,
    AssemblerResult,
    BuildInstrumentation,
    ProjectionManifest,
    projection_manifest_name,
    projection_piece_name,
)
from cruxible_core.playbill.proposals import (
    ClaimQueryFactsProvider,
    EvaluatedTreeState,
    ExhaustPromotionVerifierProtocol,
    build_tree_state,
    claim_admission_accounts_from_candidate,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1
from cruxible_core.playbill.serving import (
    SERVING_MANIFEST_FILE,
    bind_current_projection,
    load_serving_manifest,
    publish_serving_manifest,
    remove_exact_projection_build,
)
from cruxible_core.playbill.settlement import (
    ChangeSetRecordAnyVersion,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
    parse_change_set_record,
    render_generation_descriptor,
    semantic_root_for_record,
)
from cruxible_core.playbill.witness import WitnessRecord, WitnessSink
from cruxible_core.storage.playbill_projection import (
    bind_projection,
    detect_projection_orphans,
    load_projection_manifest,
)


@dataclass(frozen=True)
class RecoveredGeneration:
    """One replay-verified accepted generation, without its artifact payload.

    A generation never carries its tree. The ledger *is* the store: every
    accepted tree is already content-addressed and deduplicated in Git, so
    retaining one copy per generation would make recovery cost O(generations x
    artifacts) of process memory for bytes Git already holds. Consumers that
    need historical artifact bytes read them back through the ledger.
    """

    sequence: int
    oid: str
    semantic_root: SemanticRoot
    descriptor: GenerationDescriptor
    generation_root: GenerationRoot
    principals: PrincipalRegistrySnapshot
    record: ChangeSetRecordAnyVersion | None


@dataclass(frozen=True)
class _ReplayQueryFactsSource:
    """Present a replayed accepted prefix through the shared Claim-facts seam."""

    ledger: GitLedger
    history: tuple[RecoveredGeneration, ...]
    bodies: BodyProjectionProtocol

    def accepted_history(self) -> tuple[RecoveredGeneration, ...]:
        return self.history

    def tree_at(self, oid: str) -> dict[str, bytes]:
        if oid not in {generation.oid for generation in self.history}:
            raise SettlementIntegrityError(
                "query facts requested a tree outside the replayed accepted prefix"
            )
        return self.ledger.read_tree(oid)

    def body_store(self) -> BodyProjectionProtocol:
        return self.bodies


AcceptedQueryFactsBuilder = Callable[
    [object, AcceptedProjectionCoordinate],
    ClaimQueryFactsV1,
]


@dataclass(frozen=True)
class _GenerationWindow:
    """One generation plus its tree, held only for the length of one replay step.

    Replay verifies each successor against exactly its predecessor, so the walk
    needs a two-generation sliding window and nothing more. This object is the
    whole parent context a verification step receives; keeping it cohesive lets
    later incremental (merkle) verification thread additional carried-forward
    state through the same seam without re-plumbing every call site.

    `state` is the parent's derived state: its semantic member manifest, the
    merkle trie over that manifest, and its dependency index. All three are
    carried rather than recomputed, so a successor hashes only the members whose
    bytes actually changed and re-resolves only the dependency edges those
    members can move. Nothing in it is believed: every member digest it carries
    was computed in this process from bytes Git validated against their own
    content addresses, and the successor's manifest root, edge root, and law
    evidence are still checked against what the accepted receipt commits to. It
    must never be seeded from anything but a state this replay built: see
    `build_tree_state`, which is the only cold seed.
    """

    generation: RecoveredGeneration
    tree: dict[str, bytes]
    state: EvaluatedTreeState


def _refuse_removed_prerelease_content(
    ledger: GitLedger,
    *,
    history_oids: tuple[str, ...],
    genesis: VerifiedGenesis,
) -> None:
    """Reject authenticated retired prerelease content before any replay cache.

    The fixed-string search runs inside Git against exact content-addressed head
    blobs. Normal compatible instances never hydrate another complete tree or
    reverify a checkpointed prefix. Only a possible match pays for complete
    signature-chain authentication and exact JSON predicate confirmation.
    """

    head = history_oids[-1]
    paths = ledger.tree_paths_containing_literal(
        head,
        literal='"predicate":"knowledge.brief"',
        paths=("claim-types/knowledge/brief.json", "claims/"),
    )
    if not paths:
        return

    daemon = next(
        principal for principal in genesis.principals if principal.principal_id == "daemon"
    )
    previous_oid = history_oids[0]
    for oid in history_oids[1:]:
        if ledger.parent_of(oid) != previous_oid:
            raise SettlementIntegrityError("generation parent differs from accepted predecessor")
        if not ledger.verify_commit_with_public_key(
            oid,
            principal_id="daemon",
            public_key_hex=daemon.public_key,
        ):
            raise SettlementIntegrityError("generation daemon signature does not verify")
        principal_entry = next(
            (entry for entry in ledger.list_tree(oid) if entry.path == "principals/daemon.json"),
            None,
        )
        if principal_entry is None:
            raise SettlementIntegrityError("generation has no daemon principal registry entry")
        daemon = parse_principal_record(
            ledger.read_blob(principal_entry.oid),
            path=principal_entry.path,
        )
        if daemon.status != "active" or daemon.kind != "daemon":
            raise SettlementIntegrityError("generation daemon principal is not active")
        previous_oid = oid

    if "claim-types/knowledge/brief.json" in paths:
        raise PlaybillInstanceIncompatiblePrereleaseContent(artifact_class="knowledge.brief")

    candidates = {
        entry.path: entry.oid
        for entry in ledger.list_tree(head)
        if entry.path in paths and entry.path.startswith("claims/")
    }
    blobs = ledger.read_blobs(tuple(candidates.values()))
    for oid in candidates.values():
        try:
            payload = json.loads(blobs[oid])
        except (UnicodeDecodeError, ValueError):
            continue
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("statement"), dict)
            and payload["statement"].get("predicate") == "knowledge.brief"
        ):
            raise PlaybillInstanceIncompatiblePrereleaseContent(artifact_class="knowledge.brief")


def _materialize_successor_tree(
    ledger: GitLedger,
    *,
    parent_oid: str,
    oid: str,
    parent_tree: dict[str, bytes],
) -> dict[str, bytes]:
    """Build a successor's exact tree as its parent plus only the changed members.

    `ledger.read_tree(oid)` reads every member of every generation, which makes
    replay cost O(generations x members) of blob reads for bytes that mostly did
    not change. Git already knows which members changed, and knows it exactly:
    an entry Git omits from the diff has the same mode and the same
    content-addressed object ID in both trees, so its bytes are identical by
    construction rather than by assumption. Only the changed members are read.

    The result is byte-identical to `read_tree(oid)`, including iteration order:
    a recursive Git listing is ordered as if every directory entry carried a
    trailing separator, which for full paths is exactly byte order, so the
    rebuilt mapping is re-ordered the same way.

    The tamper posture is unchanged. Every carried member's bytes were read from
    Git in this same process (at the window's cold seed or at an earlier step of
    this walk), Git validates each object against its own content address on
    read, and the successor's manifest still verifies against the manifest root
    the accepted change-set record commits to.
    """

    changes = ledger.changed_entries(parent_oid, oid)
    blobs = ledger.read_blobs([change.oid for change in changes if change.oid is not None])
    materialized = dict(parent_tree)
    for change in changes:
        if change.oid is None:
            if change.path not in materialized:
                raise SettlementIntegrityError(
                    f"generation deletes a member absent from its predecessor: {change.path}"
                )
            del materialized[change.path]
            continue
        if change.mode != "100644":
            # `read_tree` refuses the same non-regular-file members before any
            # blob is read; a successor may not smuggle one past replay.
            raise PlaybillGitError(
                f"ledger tree contains unsupported {change.mode} member: {change.path}"
            )
        if (change.status == "A") != (change.path not in materialized):
            raise SettlementIntegrityError(
                f"generation change status differs from its predecessor tree: {change.path}"
            )
        materialized[change.path] = blobs[change.oid]
    return {
        path: materialized[path]
        for path in sorted(materialized, key=lambda item: item.encode("utf-8"))
    }


@dataclass(frozen=True)
class RecoveredInstanceState:
    genesis: VerifiedGenesis
    head: RecoveredGeneration
    history: tuple[RecoveredGeneration, ...]
    coordinate: AcceptedProjectionCoordinate
    projection: AssemblerResult | None


def _parse_note(content: bytes, *, oid: str) -> GenerationDescriptor:
    try:
        descriptor = GenerationDescriptor.model_validate_json(content)
    except (ValueError, ValidationError) as exc:
        raise SettlementIntegrityError(f"generation descriptor note is invalid: {oid}") from exc
    if canonical_bytes(descriptor.model_dump(mode="json")) + b"\n" != content:
        raise SettlementIntegrityError(f"generation descriptor note is not canonical: {oid}")
    return descriptor


def _candidate_from_record(
    record: ChangeSetRecordAnyVersion,
) -> CandidateRecordAnyVersion:
    """Recover the validated candidate one accepted receipt settled.

    The candidate version travels with the receipt version, so a receipt from
    either side of the succession boundary rebuilds exactly the candidate its own
    generation was judged against.
    """

    if isinstance(record, ChangeSetRecordV3):
        return CandidateRecordV3(
            candidate=record.candidate,
            candidate_digest=record.candidate_digest,
            required_tier=record.required_tier,
            approval_requirements=record.approval_requirements,
            activation_policy=record.activation_policy,
            closure_proof=record.closure_proof,
            members=record.members,
            law_evidence=record.law_evidence,
            law_digests=record.law_digests,
            compiler_digest=record.compiler_digest,
        )
    if isinstance(record, ChangeSetRecordV2):
        return CandidateRecordV2(
            candidate=record.candidate,
            candidate_digest=record.candidate_digest,
            required_tier=record.required_tier,
            approval_requirements=record.approval_requirements,
            activation_policy=record.activation_policy,
            closure_proof=record.closure_proof,
            members=record.members,
            law_evidence=record.law_evidence,
            law_digests=record.law_digests,
            compiler_digest=record.compiler_digest,
        )
    return CandidateRecord(
        candidate=record.candidate,
        candidate_digest=record.candidate_digest,
        required_tier=record.required_tier,
        approval_requirements=record.approval_requirements,
        activation_policy=record.activation_policy,
        closure_paths=record.closure_proof.paths,
        members=record.members,
        law_digests=record.law_digests,
        compiler_digest=record.compiler_digest,
    )


def _verify_successor(
    ledger: GitLedger,
    oid: str,
    *,
    window: _GenerationWindow,
    repository_path: str,
    object_format: GitObjectFormat,
    instance_id: str,
    compiler: CompilerCoordinate,
    bodies: BodyProjectionProtocol,
    laws: AcceptanceLawRegistry,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    query_facts_provider: ClaimQueryFactsProvider | None,
) -> _GenerationWindow:
    """Verify one successor against its parent window and return the next window."""

    parent = window.generation
    parent_tree = window.tree
    if ledger.parent_of(oid) != parent.oid:
        raise SettlementIntegrityError("generation parent differs from accepted predecessor")
    daemon = parent.principals.require_active("daemon")
    if not ledger.verify_commit_with_public_key(
        oid,
        principal_id="daemon",
        public_key_hex=daemon.public_key,
    ):
        raise SettlementIntegrityError("generation daemon signature does not verify")
    tree = _materialize_successor_tree(
        ledger,
        parent_oid=parent.oid,
        oid=oid,
        parent_tree=parent_tree,
    )
    parent_change_sets = {
        path: content for path, content in parent_tree.items() if path.startswith("changesets/")
    }
    current_change_sets = {
        path: content for path, content in tree.items() if path.startswith("changesets/")
    }
    if any(
        current_change_sets.get(path) != content for path, content in parent_change_sets.items()
    ):
        raise SettlementIntegrityError("generation modified a predecessor change-set record")
    added = sorted(set(current_change_sets) - set(parent_change_sets))
    if len(added) != 1:
        raise SettlementIntegrityError("generation must add exactly one change-set record")
    record_path = added[0]
    record = parse_change_set_record(current_change_sets[record_path], path=record_path)
    if record.sequence != parent.sequence + 1:
        raise SettlementIntegrityError("change-set sequence is not contiguous")
    expected_path = f"changesets/cs-{record.sequence:020d}.json"
    if record_path != expected_path:
        raise SettlementIntegrityError("change-set path differs from its sequence")

    candidate = _candidate_from_record(record)
    parent_coordinate = AcceptedProjectionCoordinate(
        instance_id=instance_id,
        repository_path=repository_path,
        git_object_format=object_format,
        git_oid=parent.oid,
        semantic_root=parent.semantic_root.tagged,
        generation_root=parent.generation_root.tagged,
        compiler=compiler,
    )
    # The parent's carried state enters here and the successor's comes back out,
    # so one traversal of the change set serves the law re-evaluation, the
    # manifest commitment, the semantic diff, and the dependency edge root.
    reevaluated = evaluate_proposal_tree(
        base_tree=parent_tree,
        current_tree=parent_tree,
        proposed_tree=tree,
        current=parent_coordinate,
        bodies=bodies,
        timestamp=record.candidate.timestamp,
        rebased=False,
        actor_id=record.actor_binding.actor_id,
        claim_type_expansions=claim_type_expansions_from_candidate(candidate),
        query_facts_provider=query_facts_provider,
        replay_claim_admission_accounts=claim_admission_accounts_from_candidate(candidate),
        promotion_verifier=promotion_verifier,
        producer_receipt_resolver=producer_receipt_resolver,
        parent_state=window.state,
        wire_version=candidate.tag,
        acceptance_laws=laws,
        historical_law_coordinates={
            member.path: (
                member.law_identifier,
                candidate.law_digests[member.law_identifier],
            )
            for member in candidate.members
        },
    )
    reproduced = reevaluated.candidate
    if reproduced is None or reevaluated.diagnostics or reevaluated.state is None:
        raise SettlementIntegrityError("generation candidate law/closure evidence diverged")
    if reevaluated.tree != tree:
        raise SettlementIntegrityError("generation derivative cards do not reproduce exactly")
    state = reevaluated.state
    # Named before the whole-object comparison so a tampered manifest or scope is
    # reported as what it is. Both values were recomputed from this generation's
    # own member bytes; the comparison is against what its receipt commits to.
    if reproduced.candidate.candidate_manifest_root != record.candidate.candidate_manifest_root:
        raise SettlementIntegrityError("generation manifest root differs from C_s")
    if (
        reproduced.candidate.semantic_diff_digest != record.candidate.semantic_diff_digest
        or reproduced.candidate.scope != record.candidate.scope
    ):
        raise SettlementIntegrityError("generation semantic diff differs from C_s")
    if reproduced != candidate:
        raise SettlementIntegrityError("generation candidate law/closure evidence diverged")
    for identifier, digest in record.law_digests.items():
        laws.require_historical(identifier=identifier, digest=digest)
    principal_lifecycle = all(
        member.artifact_kind == "principal-lifecycle" for member in candidate.members
    )
    verified_approvals = verify_candidate_approvals(
        candidate,
        record.approvals,
        principals=parent.principals,
        creator_principal_id=record.actor_binding.actor_id,
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
    if principal_lifecycle and record.actor_binding.actor_id not in {
        approval.signer_id for approval in verified_approvals
    }:
        raise SettlementIntegrityError(
            "principal lifecycle actor did not cryptographically approve the transition"
        )
    approval_digests = tuple(sorted(item.digest.tagged for item in verified_approvals))
    semantic_root = semantic_root_for_record(
        record,
        approval_digests=approval_digests,
        parent_semantic_root=parent.semantic_root.tagged,
        parent_record=parent.record,
    )
    descriptor = GenerationDescriptor(
        semantic_root=semantic_root.value,
        git_oid=oid,
        parent_generation_root=parent.generation_root.value,
    )
    computed_generation_root = generation_root(descriptor)
    note = ledger.read_generation_note(oid)
    if note is not None and _parse_note(note, oid=oid) != descriptor:
        raise SettlementIntegrityError("generation descriptor note differs from replay")
    principals = principal_registry_from_tree(tree, semantic_root=semantic_root.tagged)
    return _GenerationWindow(
        generation=RecoveredGeneration(
            sequence=record.sequence,
            oid=oid,
            semantic_root=semantic_root,
            descriptor=descriptor,
            generation_root=computed_generation_root,
            principals=principals,
            record=record,
        ),
        tree=tree,
        state=state,
    )


def _projection_for_head(
    ledger: GitLedger,
    *,
    coordinate: AcceptedProjectionCoordinate,
    history: tuple[RecoveredGeneration, ...],
    publication_directory: Path,
    bodies: BodyProjectionProtocol,
) -> AssemblerResult:
    assembler = ProjectionAssembler(
        ledger,
        accepted=coordinate,
        publication_directory=publication_directory,
        bodies=bodies,
        accepted_coordinates_by_sequence={
            generation.sequence: AcceptedCoordinate(
                git_oid=generation.oid,
                semantic_root=generation.semantic_root.tagged,
                generation_root=generation.generation_root.tagged,
                compiler_digest=coordinate.compiler.rule_digest,
            )
            for generation in history
        },
    )
    request = assembler.request(
        output_staging_directory=publication_directory / f".stage-{secrets.token_hex(12)}"
    )
    manifest_path = publication_directory / projection_manifest_name(request)
    if manifest_path.exists():
        try:
            manifest = load_projection_manifest(manifest_path)
            with bind_projection(manifest_path, expected=coordinate):
                pass
            return _result_for_manifest(manifest_path, manifest)
        except ProjectionIntegrityError:
            # The coordinate determines both exact v1 publication names. The
            # projection is disposable, so a corrupt/torn copy is rebuilt.
            manifest_path.unlink(missing_ok=True)
            (publication_directory / projection_piece_name(request)).unlink(missing_ok=True)
    return assembler.assemble(request)


def _result_for_manifest(path: Path, manifest: ProjectionManifest) -> AssemblerResult:
    return AssemblerResult(
        manifest_path=str(path),
        manifest=manifest,
        git_oid=manifest.git_oid,
        semantic_root=manifest.semantic_root,
        generation_root=manifest.generation_root,
        logical_digest=manifest.logical_digest,
        row_counts=manifest.row_counts,
        instrumentation=BuildInstrumentation(
            phase_nanoseconds={},
            high_water_memory_bytes=None,
        ),
    )


def _clean_unaccepted_publications(
    ledger: GitLedger,
    *,
    history: tuple[RecoveredGeneration, ...],
    instance_id: str,
    object_format: GitObjectFormat,
    compiler: CompilerCoordinate,
    publication_directory: Path,
) -> None:
    """Retire only projection builds proven outside accepted main history."""

    accepted_oids = {generation.oid for generation in history}
    manifests = sorted(
        publication_directory.glob("projection-*.json"),
        key=lambda path: path.name.encode("utf-8"),
    )
    for path in manifests:
        try:
            manifest = load_projection_manifest(path)
        except ProjectionIntegrityError:
            # A malformed immutable build is never authority and cannot serve.
            path.unlink(missing_ok=True)
            continue
        if manifest.git_oid in accepted_oids:
            continue
        expected = AcceptedProjectionCoordinate(
            instance_id=instance_id,
            repository_path=str(ledger.path.resolve(strict=True)),
            git_object_format=object_format,
            git_oid=manifest.git_oid,
            semantic_root=manifest.semantic_root,
            generation_root=manifest.generation_root,
            compiler=compiler,
        )
        with bind_projection(path, expected=expected):
            pass
        remove_exact_projection_build(
            _result_for_manifest(path, manifest),
            expected=manifest,
        )
        if ledger.object_exists(manifest.git_oid):
            ledger.collect_unreachable_generation(manifest.git_oid)


def _clean_unaccepted_generations(
    ledger: GitLedger,
    *,
    history: tuple[RecoveredGeneration, ...],
    repository_path: str,
    object_format: GitObjectFormat,
    instance_id: str,
    compiler: CompilerCoordinate,
    bodies: BodyProjectionProtocol,
    laws: AcceptanceLawRegistry,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    query_facts_builder: AcceptedQueryFactsBuilder | None,
) -> None:
    """Collect exact replay-valid generation commits that never settled on main."""

    by_oid = {generation.oid: generation for generation in history}
    for oid in ledger.unreachable_commits():
        try:
            parent_oid = ledger.parent_of(oid)
            parent = by_oid.get(parent_oid or "")
            if parent is None:
                continue
            # The parent tree is read back from the ledger for exactly this
            # check and released with the window when the iteration ends. Its
            # manifest is computed cold: this window is built from an arbitrary
            # accepted parent, so no replay-carried manifest applies to it.
            parent_tree = ledger.read_tree(parent.oid)
            query_source = _ReplayQueryFactsSource(
                ledger=ledger,
                history=history,
                bodies=bodies,
            )
            _verify_successor(
                ledger,
                oid,
                window=_GenerationWindow(
                    generation=parent,
                    tree=parent_tree,
                    state=build_tree_state(parent_tree),
                ),
                repository_path=repository_path,
                object_format=object_format,
                instance_id=instance_id,
                compiler=compiler,
                bodies=bodies,
                laws=laws,
                promotion_verifier=promotion_verifier,
                producer_receipt_resolver=producer_receipt_resolver,
                query_facts_provider=(
                    None
                    if query_facts_builder is None
                    else lambda coordinate: query_facts_builder(query_source, coordinate)
                ),
            )
            ledger.collect_unreachable_generation(oid)
        except PlaybillError:
            # Unaccepted Git garbage cannot affect service admission. Only a
            # complete replay-valid generation is eligible for targeted deletion.
            continue


def _clean_torn_projection_files(publication_directory: Path) -> None:
    """Remove detector-proven staging/unreferenced output after replay."""

    for orphan in detect_projection_orphans(publication_directory):
        path = Path(orphan.path)
        if orphan.kind == "staging-build":
            if path.is_symlink() or not path.is_dir() or path.parent != publication_directory:
                raise ProjectionIntegrityError("projection staging cleanup target is unsafe")
            shutil.rmtree(path)
        elif orphan.kind in {"unreferenced-piece", "malformed-manifest"}:
            if path.is_symlink() or not path.is_file() or path.parent != publication_directory:
                raise ProjectionIntegrityError("projection orphan cleanup target is unsafe")
            path.unlink()
        elif orphan.kind == "missing-piece":
            raise ProjectionIntegrityError("accepted projection manifest names a missing piece")
    for path in publication_directory.glob(".serving-*.tmp"):
        if path.is_symlink() or not path.is_file() or path.parent != publication_directory:
            raise ProjectionIntegrityError("serving temporary cleanup target is unsafe")
        path.unlink()


def _repair_serving(
    publication_directory: Path,
    *,
    coordinate: AcceptedProjectionCoordinate,
    projection: AssemblerResult,
) -> None:
    serving_path = publication_directory / SERVING_MANIFEST_FILE
    if serving_path.exists():
        try:
            serving = load_serving_manifest(publication_directory)
        except ProjectionIntegrityError:
            raise
        if (
            serving.git_oid == coordinate.git_oid
            and serving.semantic_root == coordinate.semantic_root
            and serving.generation_root == coordinate.generation_root
        ):
            with bind_current_projection(publication_directory, expected=coordinate):
                return
    publish_serving_manifest(publication_directory, projection)
    with bind_current_projection(publication_directory, expected=coordinate):
        pass


def _repair_witness(
    history: tuple[RecoveredGeneration, ...],
    *,
    instance_id: str,
    object_format: GitObjectFormat,
    witness: WitnessSink,
) -> None:
    expected = tuple(
        WitnessRecord(
            instance_id=instance_id,
            object_format=object_format,
            head_oid=generation.oid,
            semantic_root=generation.semantic_root.tagged,
            generation_root=generation.generation_root.tagged,
            sequence=generation.sequence,
        )
        for generation in history
        if generation.sequence > 0
    )
    latest = witness.latest(instance_id)
    start = 0
    if latest is not None:
        matches = [
            index for index, record in enumerate(expected) if record.sequence == latest.sequence
        ]
        if len(matches) != 1 or expected[matches[0]] != latest:
            raise SettlementIntegrityError("external witness state differs from replayed history")
        start = matches[0] + 1
    for record in expected[start:]:
        witness.publish(record)


def recover_instance(
    ledger: GitLedger,
    *,
    genesis: VerifiedGenesis,
    instance_id: str,
    object_format: GitObjectFormat,
    compiler: CompilerCoordinate,
    publication_directory: Path,
    bodies: BodyProjectionProtocol,
    witness: WitnessSink | None = None,
    laws: AcceptanceLawRegistry = PLAYBILL_ACCEPTANCE_LAWS,
    promotion_verifier: ExhaustPromotionVerifierProtocol | None = None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None = None,
    query_facts_builder: AcceptedQueryFactsBuilder | None = None,
    checkpoint_directory: Path | None = None,
) -> RecoveredInstanceState:
    """Replay accepted history and repair only deterministic post-CAS publication.

    When `checkpoint_directory` names a verifiable local checkpoint, the prefix
    it summarizes is re-derived from the ledger instead of re-verified, and only
    the suffix after it is replayed. An unusable checkpoint is discarded and the
    replay is genesis-rooted, so the answer never depends on the cache.
    """

    history_oids = ledger.main_history()
    if not history_oids or history_oids[0] != genesis.oid:
        raise SettlementIntegrityError("main history is not rooted at verified genesis")
    _refuse_removed_prerelease_content(ledger, history_oids=history_oids, genesis=genesis)
    genesis_coordinate = GenesisCoordinate(
        git_oid=genesis.oid,
        bootstrap_root=genesis.bootstrap_root.tagged,
        semantic_root=genesis.semantic_root.tagged,
        generation_root=genesis.generation_root.tagged,
    )
    genesis_principals = principal_registry_from_tree(
        genesis.tree,
        semantic_root=genesis.semantic_root.tagged,
    )
    genesis_generation = RecoveredGeneration(
        sequence=0,
        oid=genesis.oid,
        semantic_root=genesis.semantic_root,
        descriptor=genesis.descriptor,
        generation_root=genesis.generation_root,
        principals=genesis_principals,
        record=None,
    )
    repository_path = str(ledger.path.resolve(strict=True))
    seed = load_verified_checkpoint(
        ledger,
        checkpoint_directory,
        genesis=genesis,
        instance_id=instance_id,
        object_format=object_format,
        compiler=compiler,
        genesis_coordinate=genesis_coordinate,
    )
    if seed is None:
        history: list[RecoveredGeneration] = [genesis_generation]
        # A two-generation sliding window: rebinding `window` releases the
        # predecessor's tree, so replay memory stays flat in the history length.
        # Genesis is the one cold manifest of the walk; every later generation
        # carries its predecessor's digests forward for byte-identical members.
        window = _GenerationWindow(
            generation=genesis_generation,
            tree=genesis.tree,
            state=build_tree_state(genesis.tree),
        )
    else:
        history = [
            RecoveredGeneration(
                sequence=generation.sequence,
                oid=generation.oid,
                semantic_root=generation.semantic_root,
                descriptor=generation.descriptor,
                generation_root=generation.generation_root,
                principals=generation.principals,
                record=generation.record,
            )
            for generation in seed.prefix
        ]
        window = _GenerationWindow(
            generation=history[-1],
            tree=seed.tree,
            state=seed.state,
        )
    replayed_from = len(history)
    for oid in history_oids[replayed_from:]:
        query_source = _ReplayQueryFactsSource(
            ledger=ledger,
            history=tuple(history),
            bodies=bodies,
        )
        window = _verify_successor(
            ledger,
            oid,
            window=window,
            repository_path=repository_path,
            object_format=object_format,
            instance_id=instance_id,
            compiler=compiler,
            bodies=bodies,
            laws=laws,
            promotion_verifier=promotion_verifier,
            producer_receipt_resolver=producer_receipt_resolver,
            query_facts_provider=(
                None
                if query_facts_builder is None
                else lambda coordinate: query_facts_builder(query_source, coordinate)
            ),
        )
        history.append(window.generation)
    head = history[-1]
    checkpoint: ReplayCheckpointBodyV2 | None = None
    if checkpoint_directory is not None and head.sequence > 0:
        checkpoint = checkpoint_body(
            instance_id=instance_id,
            object_format=object_format,
            compiler=compiler,
            genesis=genesis_coordinate,
            sequence=head.sequence,
            git_oid=head.oid,
            semantic_root=head.semantic_root.tagged,
            generation_root=head.generation_root.tagged,
            parent_generation_root=history[-2].generation_root.tagged,
            tree=window.tree,
            members=window.state.members,
        )
    # Release the head tree before projection assembly, the peak-memory phase.
    del window
    recovered_history = tuple(history)
    _clean_unaccepted_generations(
        ledger,
        history=recovered_history,
        repository_path=repository_path,
        object_format=object_format,
        instance_id=instance_id,
        compiler=compiler,
        bodies=bodies,
        laws=laws,
        promotion_verifier=promotion_verifier,
        producer_receipt_resolver=producer_receipt_resolver,
        query_facts_builder=query_facts_builder,
    )
    _clean_unaccepted_publications(
        ledger,
        history=recovered_history,
        instance_id=instance_id,
        object_format=object_format,
        compiler=compiler,
        publication_directory=publication_directory,
    )
    _clean_torn_projection_files(publication_directory)
    coordinate = AcceptedProjectionCoordinate(
        instance_id=instance_id,
        repository_path=repository_path,
        git_object_format=object_format,
        git_oid=head.oid,
        semantic_root=head.semantic_root.tagged,
        generation_root=head.generation_root.tagged,
        compiler=compiler,
    )
    projection: AssemblerResult | None = None
    if head.sequence > 0:
        # Only the generations this process actually replayed can have a torn
        # note: a checkpointed prefix was noted when it was accepted, and its
        # notes are re-checked whenever a genesis-rooted replay runs.
        for generation in recovered_history[replayed_from:]:
            if ledger.read_generation_note(generation.oid) is None:
                ledger.write_recovered_generation_note(
                    generation.oid,
                    render_generation_descriptor(generation.descriptor),
                )
        projection = _projection_for_head(
            ledger,
            coordinate=coordinate,
            history=recovered_history,
            publication_directory=publication_directory,
            bodies=bodies,
        )
        _repair_serving(
            publication_directory,
            coordinate=coordinate,
            projection=projection,
        )
    if witness is not None:
        _repair_witness(
            recovered_history,
            instance_id=instance_id,
            object_format=object_format,
            witness=witness,
        )
    if checkpoint is not None and checkpoint_directory is not None:
        # Written last, after every repair has succeeded: a checkpoint may only
        # ever summarize a coordinate this process fully brought into service.
        write_checkpoint(checkpoint_directory, checkpoint)
    return RecoveredInstanceState(
        genesis=genesis,
        head=head,
        history=recovered_history,
        coordinate=coordinate,
        projection=projection,
    )


__all__ = [
    "RecoveredGeneration",
    "RecoveredInstanceState",
    "recover_instance",
]
