"""Genesis-rooted replay, half-publication repair, and serving admission recovery."""

from __future__ import annotations

import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.attestations import verify_candidate_approvals
from cruxible_core.playbill.bootstrap import VerifiedGenesis, generation_root
from cruxible_core.playbill.candidates import CandidateRecord, CandidateRecordV2
from cruxible_core.playbill.canonical import (
    GenerationRoot,
    SemanticRoot,
    canonical_bytes,
    manifest_root,
    semantic_diff,
    semantic_projection,
)
from cruxible_core.playbill.cas import BodyProjectionProtocol
from cruxible_core.playbill.errors import (
    PlaybillError,
    ProjectionIntegrityError,
    SettlementIntegrityError,
)
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.laws import PLAYBILL_ACCEPTANCE_LAWS, AcceptanceLawRegistry
from cruxible_core.playbill.principals import (
    PrincipalRegistrySnapshot,
    principal_registry_from_tree,
)
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
    ExhaustPromotionVerifierProtocol,
    claim_type_expansions_from_candidate,
    evaluate_proposal_tree,
)
from cruxible_core.playbill.serving import (
    SERVING_MANIFEST_FILE,
    bind_current_projection,
    load_serving_manifest,
    publish_serving_manifest,
    remove_exact_projection_build,
)
from cruxible_core.playbill.settlement import (
    ChangeSetRecord,
    ChangeSetRecordV2,
    compute_semantic_root,
    parse_change_set_record,
    render_generation_descriptor,
)
from cruxible_core.playbill.types import CompilerCoordinate, GenerationDescriptor, GitObjectFormat
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
    record: ChangeSetRecord | ChangeSetRecordV2 | None


@dataclass(frozen=True)
class _GenerationWindow:
    """One generation plus its tree, held only for the length of one replay step.

    Replay verifies each successor against exactly its predecessor, so the walk
    needs a two-generation sliding window and nothing more. This object is the
    whole parent context a verification step receives; keeping it cohesive lets
    later incremental (merkle) verification thread additional carried-forward
    state through the same seam without re-plumbing every call site.
    """

    generation: RecoveredGeneration
    tree: dict[str, bytes]


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
    record: ChangeSetRecord | ChangeSetRecordV2,
) -> CandidateRecord | CandidateRecordV2:
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
    tree = ledger.read_tree(oid)
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
        promotion_verifier=promotion_verifier,
    )
    if reevaluated.candidate != candidate or reevaluated.diagnostics:
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
        purpose="principal-lifecycle" if principal_lifecycle else "ordinary-artifact",
    )
    if principal_lifecycle and record.actor_binding.actor_id not in {
        approval.signer_id for approval in verified_approvals
    }:
        raise SettlementIntegrityError(
            "principal lifecycle actor did not cryptographically approve the transition"
        )
    semantic_tree = semantic_projection(tree)
    manifest = manifest_root(semantic_tree)
    if manifest.tagged != record.candidate.candidate_manifest_root:
        raise SettlementIntegrityError("generation manifest root differs from C_s")
    diff, scope = semantic_diff(parent_tree, tree)
    if diff.tagged != record.candidate.semantic_diff_digest or scope != record.candidate.scope:
        raise SettlementIntegrityError("generation semantic diff differs from C_s")
    approval_digests = tuple(sorted(item.digest.tagged for item in verified_approvals))
    semantic_root = compute_semantic_root(
        manifest_root_value=manifest.tagged,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approval_digests,
        parent_semantic_root=parent.semantic_root.tagged,
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
            # check and released with the window when the iteration ends.
            _verify_successor(
                ledger,
                oid,
                window=_GenerationWindow(generation=parent, tree=ledger.read_tree(parent.oid)),
                repository_path=repository_path,
                object_format=object_format,
                instance_id=instance_id,
                compiler=compiler,
                bodies=bodies,
                laws=laws,
                promotion_verifier=promotion_verifier,
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
) -> RecoveredInstanceState:
    """Replay accepted history and repair only deterministic post-CAS publication."""

    history_oids = ledger.main_history()
    if not history_oids or history_oids[0] != genesis.oid:
        raise SettlementIntegrityError("main history is not rooted at verified genesis")
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
    history: list[RecoveredGeneration] = [genesis_generation]
    repository_path = str(ledger.path.resolve(strict=True))
    # A two-generation sliding window: rebinding `window` releases the
    # predecessor's tree, so replay memory stays flat in the history length.
    window = _GenerationWindow(generation=genesis_generation, tree=genesis.tree)
    for oid in history_oids[1:]:
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
        )
        history.append(window.generation)
    # Release the head tree before projection assembly, the peak-memory phase.
    del window
    head = history[-1]
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
        for generation in recovered_history[1:]:
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
