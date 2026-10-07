"""Cruxible Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, cast, get_args

import click
import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from cruxible_client import (
    CruxibleClient,
    activate_with_workspace_refresh,
    contracts,
    observe_next_workspace,
)
from cruxible_client._error_base import CoreError, printable
from cruxible_client.artifacts import (
    RegistryClient,
    pack_artifact,
    unpack_artifact,
    write_layout,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
    local_attestation_signer_from_environment,
    principal_records,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
from cruxible_client.authoring.compact_query import (
    WHERE_SYNTAX,
    parse_where,
    render_query_table,
)
from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_FACTORIES,
    AUTHORING_EXAMPLE_NAMES,
    AuthoringExampleName,
    authoring_example,
    authoring_example_note,
    document_example,
)
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.authoring.signing import sign_runtime_credential_mint
from cruxible_client.authoring.sources import (
    compile_client_source_context,
    load_source_catalog,
    root_aliases,
)
from cruxible_client.authoring.workspace import (
    WorkspaceAttachmentError,
    daemon_floor_delivery,
    floor_export_parts,
    observe_next_workspace_with_coverage,
    observe_projection_coverage,
    validate_workspace_config_write,
    workspace_floor_freshness,
    write_workspace_config,
    write_workspace_floor,
    write_workspace_floor_delta,
)
from cruxible_client.authoring.world_stub import render_world_stub_for
from cruxible_client.authoring.write_evidence import observe_changes, observe_evidence
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_attestations import (
    ClaimStance,
    PreparedClaimAttestationRequest,
)
from cruxible_client.contracts.claim_type_upgrade import ClaimTypeUpgradeRequest
from cruxible_client.contracts.codes import normalize_code
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    SigningKeyError,
    SinceRequestInvalid,
)
from cruxible_client.contracts.evidence_rule_upgrade import EvidenceRuleUpgradeRequest
from cruxible_client.contracts.get_display import (
    GET_CLI_HISTORY_VALUE_WIDTH,
    GET_CLI_VALUE_WIDTH,
    get_value_display,
)
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitChangeResult,
    KitRemoveRequest,
)
from cruxible_client.contracts.procedures.results import ProcedureHaltTerminal
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_client.contracts.provider_installation import ProviderInstallRequest
from cruxible_client.contracts.repairs import RepairOperation, render_served_repair
from cruxible_client.contracts.resolution_contracts import ResolutionContractReference
from cruxible_client.contracts.source_catalog import SourceCatalog, SourceCompilationBundle
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_client.contracts.types import PrincipalKind, PrincipalRecord
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_client.contracts.write import (
    Change,
    FileEvidence,
    RetireRequest,
    SetRequest,
    SubjectRef,
    WriteOutcome,
    WriteRequest,
)
from cruxible_client.errors import DataValidationError
from cruxible_client.kits import (
    KIT_ARTIFACT,
    fetch_kit_image,
    kit_reference,
    push_kit,
    resolve_kit,
    write_kit_directory,
)
from cruxible_client.provider_installation import install_provider_package
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord, claim_type_input_template
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _echo_active_write_target,
    _echo_write_target,
    _emit_brief,
    _emit_json,
    _require_instance_id,
    _root_ctx_obj,
    _transport_target,
    and_activate_option,
    brief_option,
    change_control_options,
    echo_preview_next,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.cli.principal_settings import (
    PRINCIPAL_KEY_ENV,
    PRINCIPAL_SETTINGS_FILE,
    write_principal_settings,
)
from cruxible_core.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingSourceObservation,
)
from cruxible_core.coverage.claude_code import (
    PostToolUseResponseError,
    annotated_tool_output,
    post_tool_use_response,
    read_post_tool_use_event,
)
from cruxible_core.coverage.contracts import CoverageAccessProfile, CoverageResultV3
from cruxible_core.coverage.indexes import CoverageScanBudget
from cruxible_core.coverage.middleware import (
    CoverageRuleTagError,
    CoverageWorkspaceConfig,
    FloorGenerationPairV1,
    ResolveCoverage,
    ResolveFloorGenerations,
    coverage_middleware,
    load_coverage_config,
)
from cruxible_core.coverage.render import (
    render_coverage_manifest,
    render_coverage_result,
)
from cruxible_core.coverage.workspace import bindings_from_mapping, observe_workspace
from cruxible_core.curation.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root
from cruxible_core.governance.keys import (
    ClientPrincipalKeyTarget,
    GeneratedKeyMaterial,
    adopt_client_principal_key,
    generate_client_principal_key,
    preview_client_principal,
    validate_client_principal_key_target,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.server.config import get_runtime_bearer_token
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureBindRequest,
)
from cruxible_core.service.proposals.review import (
    ProposalReview,
    render_playbill_proposal_review,
    render_playbill_proposal_review_pointer,
)

ResultT = TypeVar("ResultT")
_ISO8601_DURATION = re.compile(
    r"P(?:(?P<weeks>[0-9]+)W|(?:(?P<days>[0-9]+)D)?"
    r"(?:T(?:(?P<hours>[0-9]+)H)?(?:(?P<minutes>[0-9]+)M)?"
    r"(?:(?P<seconds>[0-9]+)(?:\.(?P<fraction>[0-9]{1,6}))?S)?)?)"
)


def _parse_expiring_duration(
    _context: click.Context,
    _parameter: click.Parameter,
    value: str,
) -> int:
    match = _ISO8601_DURATION.fullmatch(value)
    if match is None or not any(
        match.group(key) is not None
        for key in (
            "weeks",
            "days",
            "hours",
            "minutes",
            "seconds",
        )
    ):
        raise click.BadParameter(
            "use a nonnegative ISO-8601 duration with days, weeks, hours, minutes, or seconds "
            "(for example P7D or PT12H)"
        )
    if "T" in value and not any(
        match.group(key) is not None
        for key in (
            "hours",
            "minutes",
            "seconds",
        )
    ):
        raise click.BadParameter("the ISO-8601 time section must contain a duration component")
    fraction = match.group("fraction") or ""
    return (
        int(match.group("weeks") or 0) * 604_800_000_000
        + int(match.group("days") or 0) * 86_400_000_000
        + int(match.group("hours") or 0) * 3_600_000_000
        + int(match.group("minutes") or 0) * 60_000_000
        + int(match.group("seconds") or 0) * 1_000_000
        + int(fraction.ljust(6, "0") or 0)
    )


def _server_call(
    operation: Callable[[CruxibleClient, str], ResultT],
    *,
    command_name: str,
) -> ResultT:
    instance_id = _require_instance_id()
    result = _dispatch_cli(
        lambda client: operation(client, instance_id),
        lambda: None,
        allow_local=False,
        command_name=command_name,
    )
    return cast(ResultT, result)


def _model_field_errors(exc: ValidationError) -> list[str]:
    """Render one pydantic failure per line as ``field.path: message``."""
    rendered: list[str] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "invalid"))
        rendered.append(f"{location}: {message}" if location else message)
    return rendered


def _read_model(path: str, model: type[ResultT]) -> ResultT:
    source = Path(path).expanduser()
    try:
        payload = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{source} must contain one mapping")
    validator = getattr(model, "model_validate")
    try:
        return cast(ResultT, validator(payload))
    except ValidationError as exc:
        # A malformed request file is the caller's mistake, not a crash: without
        # this the raw pydantic ValidationError escapes `handle_errors` (which
        # catches only the client CoreError family) and prints a Python
        # traceback, unlike every other refusal on this CLI. Carry the field
        # paths so the caller can repair the file from the message alone.
        # DataValidationError renders `summary: <errors>` itself, so the summary
        # must not repeat the field list.
        raise DataValidationError(
            f"{source} is not a valid {model.__name__}",
            errors=_model_field_errors(exc),
        ) from exc


def _read_since_access_profile(path: str) -> dict[str, Any]:
    """Read a CoverageAccessProfile file for since, filling model defaults.

    A file that is not a valid profile surfaces the same typed since refusal
    the daemon would give, before any request is built.
    """
    payload = _read_mapping(path)
    try:
        return CoverageAccessProfile.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise SinceRequestInvalid.from_validation_errors(
            [
                {**err, "loc": ("access_profile", *err.get("loc", ()))}
                for err in exc.errors(include_url=False)
            ]
        ) from exc


def _read_mapping(path: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{source} must contain one mapping")
    return cast(dict[str, Any], payload)


_AUTHORING_INPUT_ADAPTER: TypeAdapter[AuthoringInput] = TypeAdapter(AuthoringInput)
_CLAIM_TYPE_MIGRATION_ADAPTER: TypeAdapter[ClaimTypeMigrationRequestAny] = TypeAdapter(
    ClaimTypeMigrationRequestAny
)


def _authoring_examples_for(payload: Mapping[str, Any]) -> tuple[str, ...]:
    kind = payload.get("kind")
    if kind == "procedure":
        return ("procedure",)
    if kind != "claim":
        return tuple(AUTHORING_EXAMPLE_FACTORIES)
    source = payload.get("source")
    if isinstance(source, Mapping):
        if source.get("kind") == "working_selection":
            return ("claim-flow-a",)
        if source.get("kind") == "self_source":
            return ("claim-self-source",)
    return ("claim-flow-a", "claim-self-source")


def _validation_path(location: tuple[object, ...]) -> str:
    rendered = "$"
    for item in location:
        if isinstance(item, int):
            rendered += f"[{item}]"
        elif isinstance(item, str) and not item.startswith("playbill-"):
            rendered += f".{item}"
    return rendered


def _read_authoring_input(path: str) -> AuthoringInput:
    payload = _read_mapping(path)
    try:
        return _AUTHORING_INPUT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        examples = ", ".join(
            f"cruxible authoring create --example {name}"
            for name in _authoring_examples_for(payload)
        )
        errors = "; ".join(
            f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
            for item in exc.errors(include_url=False)
        )
        raise click.ClickException(
            f"Invalid authoring input: {errors}. Matching example: {examples}"
        ) from exc


@dataclass(frozen=True)
class _GitWorkspaceEnvironmentNote:
    """Advisory that ambient Git repository selection lost to the process CWD."""

    code: Literal["inherited_git_workspace_ignored"]
    cwd_workspace_root: Path
    inherited_workspace_root: Path


@dataclass(frozen=True)
class _LocalGitWorkspaceResult:
    """The CWD-selected worktree and any ignored ambient-repository advisory."""

    workspace_root: Path | None
    note: _GitWorkspaceEnvironmentNote | None


def _inherited_git_workspace_root() -> Path | None:
    """Resolve only the advisory root selected by ambient Git variables."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            cwd=Path.cwd(),
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        return Path(completed.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _explicit_git_workspace_root(value: str) -> Path:
    try:
        selected = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise click.UsageError(f"--workspace is not an accessible directory: {value}") from exc
    workspace_root = containing_git_workspace_root(selected)
    if workspace_root is None:
        raise click.UsageError("--workspace must select a path inside one Git worktree")
    return workspace_root


def _workspace_config_transport() -> dict[str, str]:
    obj = _root_ctx_obj()
    if obj.get("server_socket"):
        return {"server_socket": str(obj["server_socket"])}
    if obj.get("server_url"):
        return {"server_url": str(obj["server_url"])}
    raise click.UsageError("Server mode is required to attach a workspace")


def _local_git_workspace_root() -> _LocalGitWorkspaceResult:
    """Resolve the CWD worktree without trusting an inherited repository selector."""

    repository_selectors = ("GIT_DIR", "GIT_WORK_TREE")
    inherited_selector = any(name in os.environ for name in repository_selectors)
    workspace_root = containing_git_workspace_root(Path.cwd())
    inherited_root = _inherited_git_workspace_root() if inherited_selector else workspace_root
    note = None
    if (
        workspace_root is not None
        and inherited_root is not None
        and inherited_root != workspace_root
    ):
        note = _GitWorkspaceEnvironmentNote(
            code="inherited_git_workspace_ignored",
            cwd_workspace_root=workspace_root,
            inherited_workspace_root=inherited_root,
        )
    return _LocalGitWorkspaceResult(workspace_root=workspace_root, note=note)


def _emit_git_workspace_note(resolution: _LocalGitWorkspaceResult) -> None:
    _root_ctx_obj()["git_workspace_note"] = (
        None
        if resolution.note is None
        else contracts.GitWorkspaceNote(
            code=resolution.note.code,
            cwd_workspace_root=str(resolution.note.cwd_workspace_root),
            inherited_workspace_root=str(resolution.note.inherited_workspace_root),
        )
    )
    if resolution.note is None:
        return
    note = resolution.note
    click.echo(
        f"Note [{note.code}]: CWD worktree {str(note.cwd_workspace_root)!r} takes "
        f"precedence over inherited Git worktree {str(note.inherited_workspace_root)!r}.",
        err=True,
    )


def _custody_workspace_root() -> Path | None:
    """Resolve the one workspace boundary shared by every custody operation."""

    resolution = _local_git_workspace_root()
    _emit_git_workspace_note(resolution)
    return resolution.workspace_root


def _with_git_workspace_note(result: Any) -> Any:
    note = _root_ctx_obj().get("git_workspace_note")
    if note is None:
        return result
    return result.model_copy(update={"git_workspace_note": note})


def _json_receipt(result: Any) -> dict[str, Any]:
    """Elide only an absent client advisory while preserving nested null fields."""

    payload = result.model_dump(mode="json")
    if payload.get("git_workspace_note") is None:
        payload.pop("git_workspace_note", None)
    return cast(dict[str, Any], payload)


def _forbidden_roots_for(workspace_root: Path | None) -> tuple[Path, ...]:
    return () if workspace_root is None else (workspace_root,)


def _custody_forbidden_roots() -> tuple[Path, ...]:
    return _forbidden_roots_for(_custody_workspace_root())


def _refuse_tcp_workspace_operation(git_workspace: Path | None) -> None:
    if git_workspace is not None and _root_ctx_obj().get("server_url"):
        raise click.UsageError(
            f"This command is running in Git worktree {str(git_workspace)!r}, but TCP cannot "
            "attach a daemon-local workspace. Use --server-socket for local attachment, or "
            "run from outside the worktree for an intentionally unattached remote host."
        )


def _init_resume_marker(target: ClientPrincipalKeyTarget) -> Path:
    return target.directory / f".cruxible-init-resume-{target.principal.principal_id}.json"


def _init_resume_payload(
    target: ClientPrincipalKeyTarget,
    *,
    transport: str,
    instance_id: str,
    public_key: str,
) -> dict[str, str]:
    return {
        "tag": "playbill-init-key-resume-v1",
        "transport": transport,
        "instance_id": instance_id,
        "principal_id": target.principal.principal_id,
        "kind": target.principal.kind,
        "public_key": public_key,
    }


def _read_init_resume_marker(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SigningKeyError(
            f"Cruxible init retry marker is missing or malformed: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SigningKeyError(f"Cruxible init retry marker is missing or malformed: {path}")
    return payload


def _write_init_resume_marker(path: Path, payload: Mapping[str, str]) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(canonical_bytes(dict(payload)) + b"\n")
        path.chmod(0o600)
    except FileExistsError as exc:
        raise SigningKeyError(f"refusing to replace Cruxible init retry marker: {path}") from exc


def _adopt_init_retry_key(
    target: ClientPrincipalKeyTarget,
    *,
    workspace: Path | None,
    transport: str,
    instance_id: str,
) -> GeneratedKeyMaterial:
    marker = _init_resume_marker(target)
    if not marker.is_file() or marker.is_symlink():
        raise SigningKeyError(
            "Cruxible is already initialized for this custody, or the existing key material "
            "was provisioned independently; refusing to reuse it without this init's retry "
            f"marker: {marker}"
        )
    material = adopt_client_principal_key(
        target.directory,
        principal_id=target.principal.principal_id,
        kind=target.principal.kind,
        forbidden_roots=_forbidden_roots_for(workspace),
    )
    expected = _init_resume_payload(
        target,
        transport=transport,
        instance_id=instance_id,
        public_key=material.principal.public_key,
    )
    if _read_init_resume_marker(marker) != expected:
        raise SigningKeyError(
            f"existing key material belongs to a different Cruxible init target: {marker}"
        )
    return material


def _prepare_init_custody(
    *,
    workspace: Path | None,
    transport: str,
    instance_id: str,
    specifications: tuple[tuple[Path, str, PrincipalKind], ...],
) -> tuple[tuple[GeneratedKeyMaterial, ...], tuple[Path, ...]]:
    targets = tuple(
        validate_client_principal_key_target(
            directory,
            principal_id=principal_id,
            kind=kind,
            forbidden_roots=_forbidden_roots_for(workspace),
        )
        for directory, principal_id, kind in specifications
    )
    if len({target.principal.principal_id for target in targets}) != len(targets):
        raise SigningKeyError("Cruxible init principal IDs must be distinct")
    key_paths = tuple(
        path for target in targets for path in (target.private_key_path, target.public_key_path)
    )
    if len(set(key_paths)) != len(key_paths):
        raise SigningKeyError("Cruxible init custody key paths must be distinct")

    prepared: list[GeneratedKeyMaterial | None] = []
    for target in targets:
        private_exists = target.private_key_path.exists()
        public_exists = target.public_key_path.exists()
        marker = _init_resume_marker(target)
        if private_exists != public_exists:
            raise SigningKeyError(
                f"Cruxible init retry requires a complete key pair for "
                f"{target.principal.principal_id}"
            )
        if private_exists:
            prepared.append(
                _adopt_init_retry_key(
                    target,
                    workspace=workspace,
                    transport=transport,
                    instance_id=instance_id,
                )
            )
        else:
            if marker.exists():
                raise SigningKeyError(
                    f"Cruxible init retry marker has no complete key pair: {marker}"
                )
            prepared.append(None)

    materials: list[GeneratedKeyMaterial] = []
    markers: list[Path] = []
    for target, existing in zip(targets, prepared, strict=True):
        material = existing
        marker = _init_resume_marker(target)
        if material is None:
            material = generate_client_principal_key(
                target.directory,
                principal_id=target.principal.principal_id,
                kind=target.principal.kind,
                forbidden_roots=_forbidden_roots_for(workspace),
            )
            _write_init_resume_marker(
                marker,
                _init_resume_payload(
                    target,
                    transport=transport,
                    instance_id=instance_id,
                    public_key=material.principal.public_key,
                ),
            )
        materials.append(material)
        markers.append(marker)
    return tuple(materials), tuple(markers)


def _write_bundle(path: str, bundle: SourceCompilationBundle) -> None:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as handle:
            handle.write(canonical_bytes(bundle.model_dump(mode="json")) + b"\n")
    except FileExistsError as exc:
        raise click.ClickException(f"Refusing to overwrite existing bundle: {output}") from exc


def _root_aliases(values: tuple[str, ...]) -> dict[str, Path]:
    return root_aliases(values)


def _catalog(portable_path: str, local_path: str | None) -> SourceCatalog:
    return load_source_catalog(
        Path(portable_path).expanduser(),
        None if local_path is None else Path(local_path).expanduser(),
    )


def _compile_remote_context(
    client: CruxibleClient,
    instance_id: str,
    *,
    catalog: SourceCatalog,
    repository_root: Path,
    aliases: dict[str, Path],
) -> SourceCompilationBundle:
    return compile_client_source_context(
        client,
        instance_id,
        catalog=catalog,
        repository_root=repository_root,
        aliases=aliases,
    )


@click.group("playbill")
def playbill_group() -> None:
    """Govern Documents through Cruxible's proposal and acceptance ledger."""


@playbill_group.group("host")
def host_group() -> None:
    """Allocate daemon-owned hosts without adopting config or semantic state."""


@playbill_group.group("workspace")
def workspace_group() -> None:
    """Bind local client configuration to an existing registered host."""


@workspace_group.command("attach")
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@change_control_options
@click.option("--no-floor-delivery", is_flag=True, help="Opt out of daemon floor delivery.")
@json_option
@handle_errors
def attach_workspace(
    instance_id: str | None,
    no_floor_delivery: bool,
    replace: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Attach this Git worktree to a daemon host, initialized or not.

    A host with no worktree registers this one (an initialized host included,
    as long as the worktree is in its ledger's Git object format); a host
    registered to this worktree just gets the client config.
    """

    resolution = _local_git_workspace_root()
    _emit_git_workspace_note(resolution)
    workspace = resolution.workspace_root
    if workspace is None:
        raise click.UsageError("cruxible workspace attach must run inside one Git worktree")
    if not _root_ctx_obj().get("server_socket"):
        raise click.UsageError(
            "cruxible workspace attach requires a local --server-socket so the daemon can "
            "prove its registered workspace path"
        )
    selected = instance_id or _require_instance_id()
    registration = _dispatch_cli(
        lambda client: client.host_workspace_registration(selected),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace attach",
    )
    assert isinstance(registration, contracts.HostWorkspaceRegistration)
    registered = registration.workspace_path
    if registration.status != "registered":
        attached = _dispatch_cli(
            lambda client: client.host_workspace_attach(
                selected, workspace_root=str(workspace), dry_run=dry_run, at=at
            ),
            lambda: None,
            allow_local=False,
            command_name="cruxible workspace attach",
        )
        assert isinstance(attached, contracts.HostWorkspaceAttachResult)
        if attached.status == "would_attach":
            if output_json:
                _emit_json(attached.model_dump(mode="json"))
            else:
                click.echo(
                    f"Would attach {workspace} to Cruxible host {selected}; nothing was "
                    "registered or written"
                )
                echo_preview_next(attached.status, attached.coordinate)
            return
        registered = attached.workspace_root
    if registered is None or Path(registered).resolve(strict=False) != workspace:
        raise WorkspaceAttachmentError(
            instance_id=selected,
            requested_workspace=str(workspace),
            registered_workspace=registered,
        )
    if dry_run:
        click.echo(f"{workspace} is already attached to Cruxible host {selected}; nothing changes")
        return
    transport_values = _workspace_config_transport()
    transport = str(next(iter(transport_values.values())))
    _echo_active_write_target(
        instance_id=selected,
        instance_source="explicit" if instance_id is not None else None,
    )
    config_path = write_workspace_config(
        workspace,
        instance_id=selected,
        replace=replace,
        **transport_values,
    )
    _dispatch_cli(
        lambda client: client.set_floor_delivery(selected, enabled=not no_floor_delivery),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace attach",
    )
    result = contracts.WorkspaceAttachResult(
        instance_id=selected,
        workspace_root=str(workspace),
        config_path=str(config_path),
        transport=transport,
    )
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    click.echo(f"Attached workspace {workspace} to Cruxible host {selected}")
    click.echo(f"Config: {config_path}")


@workspace_group.command("floor-delivery")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@json_option
@handle_errors
def workspace_floor_delivery(state: str, instance_id: str | None, output_json: bool) -> None:
    """Choose whether the local daemon is the workspace floor's writer."""

    if not _root_ctx_obj().get("server_socket"):
        raise click.UsageError("workspace floor-delivery requires a local --server-socket")
    selected = instance_id or _require_instance_id()
    result = _dispatch_cli(
        lambda client: client.set_floor_delivery(selected, enabled=state == "on"),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace floor-delivery",
    )
    assert result is not None
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"Floor delivery {state} for {selected}")


@workspace_group.command("detach")
@click.option("--instance-id", default=None, help="Existing registered daemon host ID.")
@change_control_options
@json_option
@handle_errors
def detach_workspace(
    instance_id: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Release a daemon host from the Git worktree it is registered against.

    The registry allows one host per worktree, so re-binding a worktree to a
    second host needs the first one released. Nothing governed changes: the host
    keeps its ledger and every read it has ever served, and stops being the host
    of this directory. Attach the worktree to the new host afterwards.
    """

    selected = instance_id or _require_instance_id()
    _echo_active_write_target(
        instance_id=selected,
        instance_source="explicit" if instance_id is not None else None,
    )
    result = _dispatch_cli(
        lambda client: client.host_workspace_detach(selected, dry_run=dry_run, at=at),
        lambda: None,
        allow_local=False,
        command_name="cruxible workspace detach",
    )
    assert isinstance(result, contracts.WorkspaceDetachResult)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    if result.status == "not_registered":
        click.echo(f"Cruxible host {selected} registers no workspace")
        return
    if result.status == "would_detach":
        click.echo(f"Would detach {result.workspace_root} from Cruxible host {selected}")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Detached {result.workspace_root} from Cruxible host {selected}")


def _active_server_transport() -> str:
    obj = _root_ctx_obj()
    if obj.get("server_url"):
        return str(obj["server_url"])
    if obj.get("server_socket"):
        return f"unix socket {obj['server_socket']}"
    return "configured Cruxible server"


@host_group.command("show")
@click.argument("instance_id")
@json_option
@handle_errors
def show_host(instance_id: str, output_json: bool) -> None:
    """Show one existing daemon host and its write compatibility."""

    result = _dispatch_cli(
        lambda client: client.show_host(instance_id),
        lambda: None,
        allow_local=False,
        command_name="cruxible host show",
    )
    assert isinstance(result, contracts.HostInspection)
    transport = _active_server_transport()
    if output_json:
        payload = result.model_dump(mode="json")
        payload["transport"] = transport
        _emit_json(payload)
        return
    click.echo(f"Cruxible host: {result.instance_id}")
    click.echo(f"Transport: {transport}")
    click.echo(f"Managed root: {result.managed_root or '-'}")
    click.echo(f"Workspace root: {result.workspace_root or '-'}")
    click.echo(f"Floor delivery: {'on (default)' if result.floor_delivery else 'off (opted out)'}")
    click.echo(f"Compiler coordinate: {result.compiler_coordinate or '-'}")
    click.echo(f"Compiler revision: {result.compiler_revision or '-'}")
    click.echo(f"Compatibility: {result.compatibility}")
    if result.reason is not None:
        click.echo(f"Reason: {result.reason.code}: {result.reason.detail}")
        for repair in result.reason.repair_commands:
            click.echo(f"Repair: {repair}")


@host_group.command("create")
@click.option("--instance-id", default=None, help="Optional caller-selected opaque ID.")
@click.option(
    "--workspace",
    "workspace_path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Explicit Git workspace to configure; remote paths stay client-local.",
)
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@change_control_options
@json_option
@handle_errors
def create_host(
    instance_id: str | None,
    workspace_path: str | None,
    replace: bool,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Allocate an empty host and remember it as the active instance."""

    git_workspace: Path | None
    if workspace_path is not None:
        git_workspace = _explicit_git_workspace_root(workspace_path)
    else:
        workspace_resolution = _local_git_workspace_root()
        _emit_git_workspace_note(workspace_resolution)
        git_workspace = workspace_resolution.workspace_root
        _refuse_tcp_workspace_operation(git_workspace)
    workspace_root = (
        str(git_workspace)
        if git_workspace is not None and _root_ctx_obj().get("server_socket")
        else None
    )
    config_transport = _workspace_config_transport() if git_workspace is not None else {}
    if git_workspace is not None:
        validate_workspace_config_write(
            git_workspace,
            instance_id=instance_id,
            replace=replace,
            **config_transport,
        )
    result = _dispatch_cli(
        lambda client: client.create_host(
            instance_id=instance_id, workspace_root=workspace_root, dry_run=dry_run, at=at
        ),
        lambda: None,
        allow_local=False,
        command_name="cruxible host create",
    )
    assert isinstance(result, contracts.HostResult)
    result = _with_git_workspace_note(result)
    if dry_run:
        if output_json:
            _emit_json(_json_receipt(result))
        else:
            click.echo(f"Cruxible host: {result.instance_id} ({result.status}); nothing written")
            echo_preview_next(result.status, result.coordinate)
        return
    if git_workspace is not None:
        write_workspace_config(
            git_workspace,
            instance_id=result.instance_id,
            replace=replace,
            **config_transport,
        )
    _activate_server_instance(result.instance_id)
    if output_json:
        _emit_json(_json_receipt(result))
        return
    click.echo(f"Cruxible host: {result.instance_id} ({result.status})")


@playbill_group.command("init")
@click.option("--key-dir", required=True, help="Client custody directory outside the workspace.")
@click.option(
    "--principal-id",
    default=None,
    help=(
        "Owner principal ID this init makes you (default: CRUXIBLE_PRINCIPAL_ID or the "
        "global --principal-id)."
    ),
)
@click.option(
    "--reviewer-key-dir",
    default=None,
    help="Optional second ordinary-principal custody directory outside the workspace.",
)
@click.option(
    "--require-independent-approval",
    is_flag=True,
    help="Require one non-creator ordinary approval for governed changes.",
)
@click.option("--recovery-key-dir", default=None, help="Optional offline recovery custody dir.")
@click.option("--recovery-principal-id", default="recovery", show_default=True)
@click.option("--profile", type=click.Choice(["local", "cloud"]), default="local")
@click.option(
    "--workspace",
    "workspace_path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Explicit Git workspace to configure; remote paths stay client-local.",
)
@click.option("--replace", is_flag=True, help="Replace a differing workspace config.")
@click.option(
    "--object-format",
    "object_format",
    type=click.Choice(["sha1", "sha256"]),
    default=None,
    help=(
        "Ledger Git object format. Default: inherit an attached workspace, else sha1. "
        "A value that contradicts the attached workspace is refused."
    ),
)
@click.option(
    "--mirror-url",
    "mirror_url",
    default=None,
    help=(
        "Remote this ledger publishes to after every write. Never a URL carrying a "
        "credential; bind one later with 'cruxible ledger set-mirror'."
    ),
)
@json_option
@handle_errors
def init_playbill(
    key_dir: str,
    principal_id: str | None,
    reviewer_key_dir: str | None,
    require_independent_approval: bool,
    recovery_key_dir: str | None,
    recovery_principal_id: str,
    profile: str,
    workspace_path: str | None,
    replace: bool,
    object_format: str | None,
    mirror_url: str | None,
    output_json: bool,
) -> None:
    """Make you the owner: create client custody and bootstrap the approval policy.

    With daemon auth off, the owner principal ID is the identity this process
    claims for the init request; no bootstrap secret is needed.
    """

    git_workspace = (
        _explicit_git_workspace_root(workspace_path)
        if workspace_path is not None
        else _custody_workspace_root()
    )
    if require_independent_approval and reviewer_key_dir is None:
        raise click.UsageError("--require-independent-approval requires --reviewer-key-dir")
    if workspace_path is None:
        _refuse_tcp_workspace_operation(git_workspace)
    if not (_root_ctx_obj().get("server_url") or _root_ctx_obj().get("server_socket")):
        raise click.UsageError("Local execution disabled for cruxible init; use server mode.")
    selected = _require_instance_id()
    transport = _transport_target(_root_ctx_obj())
    if transport is None:  # pragma: no cover - guarded by _get_client above
        raise click.UsageError("Server mode is required for cruxible init")
    config_transport = _workspace_config_transport() if git_workspace is not None else {}
    if git_workspace is not None:
        validate_workspace_config_write(
            git_workspace,
            instance_id=selected,
            replace=replace,
            **config_transport,
        )
    configured = _root_ctx_obj().get("principal_id")
    if principal_id is None:
        principal_id = configured
    if principal_id is None:
        raise click.UsageError(
            "cruxible init needs the owner principal ID; repair: "
            "`cruxible init --principal-id ID --key-dir DIR`"
        )
    if configured is not None and configured != principal_id:
        raise click.UsageError(
            f"--principal-id {principal_id} disagrees with the configured principal "
            f"{configured} (CRUXIBLE_PRINCIPAL_ID or the global --principal-id); repair: "
            "pass one principal ID"
        )
    if get_runtime_bearer_token() is None:
        # Auth off: the init request claims the owner it creates. With a bearer
        # credential the credential decides who acts, so no claim is sent.
        _root_ctx_obj()["principal_id"] = principal_id
    workspace = git_workspace
    specifications: list[tuple[Path, str, PrincipalKind]] = [
        (Path(key_dir).expanduser(), principal_id, "ordinary")
    ]
    if reviewer_key_dir is not None:
        specifications.append((Path(reviewer_key_dir).expanduser(), "reviewer", "ordinary"))
    if recovery_key_dir is not None:
        specifications.append(
            (Path(recovery_key_dir).expanduser(), recovery_principal_id, "recovery")
        )
    materials, markers = _prepare_init_custody(
        workspace=workspace,
        transport=transport,
        instance_id=selected,
        specifications=tuple(specifications),
    )
    owner = materials[0]
    reviewer = materials[1] if reviewer_key_dir is not None else None
    result = _server_call(
        lambda client, active: client.init(
            active,
            principals=[item.principal.model_dump(mode="json") for item in materials],
            operating_profile=cast(Any, profile),
            require_independent_approval=require_independent_approval,
            git_object_format=cast(Any, object_format),
            mirror_url=mirror_url,
            **(
                {"workspace_root": str(git_workspace)}
                if git_workspace is not None and _root_ctx_obj().get("server_socket")
                else {}
            ),
        ),
        command_name="cruxible init",
    )
    result = _with_git_workspace_note(result)
    if git_workspace is not None:
        write_workspace_config(
            git_workspace,
            instance_id=result.instance_id,
            replace=replace,
            **config_transport,
        )
    for marker in markers:
        marker.unlink()
    _activate_server_instance(result.instance_id)
    owner_token = _mint_owner_credential(owner, principal_id=principal_id)
    settings = write_principal_settings(
        Path(key_dir),
        ctx_obj=_root_ctx_obj(),
        instance_id=result.instance_id,
        principal_id=principal_id,
        private_key_path=owner.private_key_path,
        token=owner_token,
        written_by="cruxible init",
    )
    if output_json:
        _emit_json({**_json_receipt(result), "owner_settings_path": str(settings)})
        return
    click.echo(f"Cruxible initialized at {result.coordinate.git_oid}")
    click.echo(f"Approval policy: {result.approval_policy_mode}")
    click.echo(f"Workspace refs: {result.workspace_advertisement.status}")
    if result.workspace_advertisement.failure_code is not None:
        click.echo(f"Workspace ref failure: {result.workspace_advertisement.failure_code}")
    click.echo(f"Owner public key: {owner.principal.public_key}")
    click.echo(f"Owner private key retained locally at: {owner.private_key_path}")
    click.echo(f"Owner principal: {principal_id}")
    click.echo(f"Owner settings: {settings}")
    click.echo(
        f"Next: set -a; . {settings}; set +a -- later commands then act as {principal_id} "
        "(CRUXIBLE_PRINCIPAL_ID)"
        + (
            "."
            if owner_token is not None
            else "; with daemon auth off that is a claim of identity, not authentication: "
            "every process of this OS user is equally trusted."
        )
    )
    if reviewer is not None:
        click.echo(f"Reviewer public key: {reviewer.principal.public_key}")
        click.echo(f"Reviewer private key retained locally at: {reviewer.private_key_path}")


def _mint_owner_credential(owner: GeneratedKeyMaterial, *, principal_id: str) -> str | None:
    """With daemon auth on, mint the new owner's own admin credential; return its token.

    The operator credential that ran init acts as no principal, so the owner
    needs one that acts as it. The owner's fresh key signs its consent. With auth
    off nothing is minted: the configured principal ID is the identity.
    """

    if get_runtime_bearer_token() is None:
        # An auth-on daemon answers nothing without a bearer credential.
        return None
    identity = _server_call(
        lambda client, instance_id: client.whoami(instance_id),
        command_name="cruxible init",
    )
    if not identity.authenticated or identity.actor_id == principal_id:
        return None
    minted = _server_call(
        lambda client, instance_id: client.create_runtime_credential(
            instance_id,
            principal_id=principal_id,
            permission_mode="admin",
            principal_proof=sign_runtime_credential_mint(
                instance_id=instance_id,
                principal_id=principal_id,
                permission_mode="admin",
                label=principal_id,
                private_key_path=owner.private_key_path,
                forbidden_roots=_custody_forbidden_roots(),
            ),
        ),
        command_name="cruxible init",
    )
    return minted.token


@playbill_group.group("body")
def body_group() -> None:
    """Store inert Document body bytes."""


@body_group.command("store")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def store_body(path: str, output_json: bool) -> None:
    content = Path(path).read_bytes()
    result = _server_call(
        lambda client, instance_id: client.store_body(instance_id, content),
        command_name="cruxible body store",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(result.digest)


@playbill_group.group("instance")
def instance_group() -> None:
    """Read and end the lifecycle of one governed instance."""


@instance_group.command("decommission")
@click.option("--reason", required=True, help="Why this instance stops accepting writes.")
@change_control_options
@json_option
@handle_errors
def decommission_instance(
    reason: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """End this instance's governed writes without deleting anything.

    Reads keep serving at the accepted coordinate and every byte stays on disk.
    Archiving or erasing the directory afterwards is your own step; no verb here
    performs it, and the state cannot be reversed. So it previews first; the
    confirmation is that preview's coordinate: ``--commit --at OID``.
    """

    result = _server_call(
        lambda client, instance_id: client.decommission_instance(
            instance_id, reason=reason, dry_run=dry_run, at=at
        ),
        command_name="cruxible instance decommission",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_decommission":
        click.echo(f"Would decommission instance {result.instance_id}; nothing changed.")
        click.echo(f"Reason: {printable(result.reason)}")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Instance {result.instance_id} decommissioned at {result.decommissioned_at}.")
    # Operator prose reaches the terminal here; escape it so a control
    # character in the reason cannot forge a line of daemon output.
    click.echo(f"Reason: {printable(result.reason)}")
    click.echo(f"By: {printable(result.decommissioned_by)}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")
    click.echo("Reads keep serving; nothing was deleted. Archive the directory yourself.")


@playbill_group.group("ledger")
def ledger_group() -> None:
    """Publish this instance's ledger, and read where it publishes to."""


@ledger_group.command("set-mirror")
@click.argument("url")
@change_control_options
@json_option
@handle_errors
def set_ledger_mirror(url: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Bind the remote and wait boundedly for its initial publication attempt.

    The URL must carry no credential: the daemon reads its token from its own
    environment, and this string is printed back by `ledger clone-url` to
    anyone who may read the instance at all. Every accepted byte is sent there at
    once and cannot be called back, so it previews first; commit that preview
    with ``--commit --at OID``.
    """

    result = _server_call(
        lambda client, instance_id: client.set_ledger_mirror(
            instance_id, url=url, dry_run=dry_run, at=at
        ),
        command_name="cruxible ledger set-mirror",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Ledger mirror: {result.mirror_url}")
    click.echo(f"Publication: {result.status}")
    if result.detail is not None:
        click.echo(f"Detail: {printable(result.detail)}")
    echo_preview_next(result.status, result.coordinate)


@ledger_group.command("clone-url")
@json_option
@handle_errors
def ledger_clone_url(output_json: bool) -> None:
    """Print the ledger mirror a reviewer clones to read this instance's proposals."""

    result = _server_call(
        lambda client, instance_id: client.get_ledger_mirror(instance_id),
        command_name="cruxible ledger clone-url",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(result.mirror_url)
    click.echo(
        f"Publication: {result.status}; acknowledged request {result.published_sequence}, "
        f"latest requested {result.requested_sequence}",
        err=True,
    )


@ledger_group.command("publish")
@click.option("--timeout", type=click.FloatRange(0, 60), default=60.0, show_default=True)
@change_control_options
@json_option
@handle_errors
def ledger_publish(timeout: float, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Wait for publication to the configured mirror; timeout 0 only requests it."""

    result = _server_call(
        lambda client, instance_id: client.publish_ledger(
            instance_id, timeout=timeout, dry_run=dry_run, at=at
        ),
        command_name="cruxible ledger publish",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Publication: {result.status}")
    if result.wait_sequence is not None:
        acknowledged = result.published_sequence >= result.wait_sequence
        click.echo(
            f"Request {result.wait_sequence}: "
            f"{'acknowledged' if acknowledged else 'awaiting acknowledgment'}"
        )
    if result.detail is not None:
        click.echo(f"Detail: {printable(result.detail)}")
    echo_preview_next(result.status, result.coordinate)


@playbill_group.group("provider")
def provider_group() -> None:
    """Manage governed Provider artifacts."""


@provider_group.command("list")
@json_option
@handle_errors
def list_provider_packages(output_json: bool) -> None:
    """List packages from the daemon's configured provider repository."""
    result = _server_call(
        lambda client, instance_id: client.list_provider_packages(instance_id),
        command_name="cruxible provider list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        for package in result.packages:
            click.echo(f"{package.name} {package.version}: {', '.join(package.interfaces)}")
        if result.detail:
            click.echo(result.detail)


@provider_group.command("install")
@click.argument("package_or_wheel")
@click.option("--lock", "lock_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dependency",
    "dependencies",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--extra", "extras", multiple=True)
@click.option("--reverify", is_flag=True, help="Recheck a retained installation explicitly.")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Resolve the package and, if it is already prepared here, evaluate the registration "
        "it would propose; fetch, build, register and propose nothing. By name only."
    ),
)
@json_option
@handle_errors
def install_provider(
    package_or_wheel: str,
    lock_path: Path | None,
    dependencies: tuple[Path, ...],
    extras: tuple[str, ...],
    reverify: bool,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Install a package by name (NAME or NAME==VERSION) or transfer a local wheel.

    By name, the package comes from the configured provider repository, or else
    from the provider index (PyPI unless the operator configured indexes).
    """
    if package_or_wheel.endswith(".whl"):
        if lock_path is None:
            raise click.UsageError("a local wheel requires --lock")
        if dry_run:
            raise click.UsageError(
                "--dry-run previews an install by name; a local wheel is transferred to the "
                "daemon before anything can be evaluated"
            )
        result = _server_call(
            lambda client, instance_id: install_provider_package(
                client,
                instance_id,
                wheel=Path(package_or_wheel),
                lock=lock_path,
                dependency_wheels=dependencies,
                extras=extras,
                reverify=reverify,
            ),
            command_name="cruxible provider install",
        )
    else:
        if lock_path is not None or dependencies:
            raise click.UsageError("--lock and --dependency apply to a local wheel")
        package, pinned, version = package_or_wheel.partition("==")
        request = ProviderInstallRequest(
            package=package,
            version=version if pinned else None,
            extras=tuple(sorted(set(extras))),
            reverify=reverify,
            dry_run=dry_run or None,
        )
        result = _server_call(
            lambda client, instance_id: client.install_provider(instance_id, request),
            command_name="cruxible provider install",
        )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"{result.provider_id}: {result.status}")
        if result.detail:
            click.echo(result.detail)
        if result.preview_scope == "validation_only":
            click.echo(
                "Validation-only preview; it did not run: "
                + ", ".join(step.replace("_", " ") for step in result.not_run)
            )
            echo_preview_next(result.status, result.coordinate)
        if result.proposal_id:
            click.echo(f"Proposal: {result.proposal_id}")
        for operation in result.operations:
            if operation.missing_requirements:
                click.echo(f"{operation.interface_id}: {', '.join(operation.missing_requirements)}")


@playbill_group.group("kit")
def kit_group() -> None:
    """Export and import definition kits."""


def _echo_kit_change(result: KitChangeResult) -> None:
    version = "" if result.version is None else f" {result.version}"
    click.echo(f"{result.kit_id}{version}: {result.status}")
    for item in result.plan:
        if item.action != "unchanged":
            detail = "" if item.detail is None else f" ({printable(item.detail)})"
            click.echo(f"  {item.action}: {item.path}{detail}")
    if result.detail:
        click.echo(printable(result.detail))
    if result.proposal_id:
        click.echo(f"Proposal: {result.proposal_id}")
        if result.approval_required:
            click.echo(
                f"Next: cruxible proposal approve {result.proposal_id} "
                "--signer-id ID --key FILE, then activate it."
            )
        else:
            click.echo(f"Next: cruxible proposal activate {result.proposal_id}")
    echo_preview_next(result.status, result.coordinate)


@kit_group.command("build")
@click.option("--id", "kit_id", required=True, help="Kit id, lowercase with hyphens.")
@click.option("--version", "version", required=True, help="Release version, MAJOR.MINOR.PATCH.")
@click.option(
    "--owns",
    "owns",
    multiple=True,
    required=True,
    help="Identity prefix the kit defines, ending in '.' (repeatable).",
)
@click.option(
    "--out", "out", required=True, type=click.Path(path_type=Path), help="New kit directory."
)
@json_option
@handle_errors
def build_kit(
    kit_id: str,
    version: str,
    owns: tuple[str, ...],
    out: Path,
    output_json: bool,
) -> None:
    """Export this instance's owned definitions as one self-contained kit release."""
    if out.exists():
        raise click.UsageError(f"{out} already exists")
    request = KitBuildRequest(
        kit_id=kit_id,
        version=version,
        owns=tuple(sorted(set(owns))),
    )
    bundle = _server_call(
        lambda client, instance_id: client.build_kit(instance_id, request),
        command_name="cruxible kit build",
    ).bundle
    write_kit_directory(bundle, out)
    if output_json:
        _emit_json(bundle.manifest.model_dump(mode="json"))
    else:
        click.echo(
            f"{kit_id} {version}: {len(bundle.artifacts)} artifacts, "
            f"{bundle.manifest.content_digest}"
        )


@kit_group.command("add")
@click.argument("kit")
@click.option(
    "--source", "source", default=None, help="Recorded origin; defaults to where KIT came from."
)
@change_control_options
@json_option
@handle_errors
def add_kit(
    kit: str, source: str | None, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Propose installing or upgrading KIT as one change set.

    KIT is a kit directory, an OCI image layout, or a registry reference such as
    ``project-state:1.0.0`` or ``ghcr.io/acme/kits/foo@sha256:...``. It previews
    by default; commit the preview with ``--commit --at OID``.
    """
    bundle, origin = resolve_kit(kit)
    request = KitAddRequest(bundle=bundle, source=source or origin, dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.add_kit(instance_id, request),
        command_name="cruxible kit add",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(result)


@kit_group.command("push")
@click.argument("kit")
@click.argument("reference")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Pack KIT and print the reference it would publish; contact no registry.",
)
@json_option
@handle_errors
def push_kit_cmd(kit: str, reference: str, dry_run: bool, output_json: bool) -> None:
    """Publish KIT (a directory or OCI layout) to a registry REFERENCE.

    Credentials come from CRUXIBLE_REGISTRY_USERNAME and CRUXIBLE_REGISTRY_PASSWORD,
    for the one registry host named in CRUXIBLE_REGISTRY. A push leaves this
    machine for good, so `--dry-run` packs the exact artifact and prints the
    digest-pinned reference it would publish without contacting the registry.
    """
    bundle, _origin = resolve_kit(kit)
    ref = kit_reference(reference)
    if dry_run:
        digest = pack_artifact(KIT_ARTIFACT, bundle).digest
        pinned = str(ref.pinned(digest))
        if output_json:
            _emit_json({"status": "would_push", "reference": pinned, "digest": digest})
        else:
            click.echo(f"Would push {pinned}; nothing was sent.")
        return
    with RegistryClient() as registry:
        digest = push_kit(bundle, ref, registry=registry)
    pinned = str(ref.pinned(digest))
    if output_json:
        _emit_json({"reference": pinned, "digest": digest})
    else:
        click.echo(pinned)


@kit_group.command("pull")
@click.argument("reference")
@click.option("--out", "out", required=True, type=click.Path(path_type=Path))
@click.option(
    "--layout", is_flag=True, help="Write an OCI image layout instead of a kit directory."
)
@json_option
@handle_errors
def pull_kit(reference: str, out: Path, layout: bool, output_json: bool) -> None:
    """Fetch and verify a kit from a registry into OUT, without installing it."""
    if out.exists():
        raise click.UsageError(f"{out} already exists")
    # The layout keeps the published bytes exactly, so it carries the same digest.
    image, origin = fetch_kit_image(reference)
    bundle = unpack_artifact(KIT_ARTIFACT, image)
    if layout:
        write_layout(image, out, ref=origin)
    else:
        write_kit_directory(bundle, out)
    if output_json:
        _emit_json({"source": origin, "content_digest": bundle.manifest.content_digest})
    else:
        click.echo(f"{origin}: {len(bundle.artifacts)} artifacts")


@kit_group.command("status")
@json_option
@handle_errors
def kit_status(output_json: bool) -> None:
    """List installed kits and any kit paths edited since install."""
    result = _server_call(
        lambda client, instance_id: client.kit_status(instance_id),
        command_name="cruxible kit status",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.kits:
        click.echo("No kits installed.")
    for kit in result.kits:
        click.echo(f"{kit.kit_id} {kit.version} {kit.content_digest}")
        for path in kit.drifted:
            click.echo(f"  edited locally: {path}")


@kit_group.command("remove")
@click.argument("kit_id")
@change_control_options
@json_option
@handle_errors
def remove_kit(kit_id: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Propose retiring every artifact KIT_ID installed (previews by default)."""
    request = KitRemoveRequest(kit_id=kit_id, dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.remove_kit(instance_id, request),
        command_name="cruxible kit remove",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_kit_change(result)


@playbill_group.group("document")
def document_group() -> None:
    """Propose and read governed Documents."""


@document_group.command("propose")
@click.option("--envelope", type=click.Path(exists=True, dir_okay=False))
@click.option("--example", type=click.Choice(["document"]))
@click.option("--name", "proposal_name")
@change_control_options
@json_option
@handle_errors
def propose_document(
    envelope: str | None,
    example: str | None,
    proposal_name: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Use the sanctioned command-local Document proposal path."""

    if (envelope is None) == (example is None):
        raise click.UsageError("choose exactly one of --envelope or --example")
    if example is not None:
        if proposal_name is not None:
            raise click.UsageError("--name applies only when --envelope is supplied")
        _emit_json(document_example().model_dump(mode="json"))
        return
    if proposal_name is None:
        raise click.UsageError("--name is required with --envelope")
    assert envelope is not None
    shell = _read_model(envelope, DocumentShell)
    result = _server_call(
        lambda client, instance_id: client.propose_document(
            instance_id,
            shell=shell.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible document propose",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("capture")
def capture_group() -> None:
    """Read retained evidence from completed observations and Procedures."""


@capture_group.command("read")
@click.argument("capture_digest")
@click.option("--max-bytes", type=click.IntRange(min=0), default=4 * 1024 * 1024, show_default=True)
@handle_errors
def read_capture(capture_digest: str, max_bytes: int) -> None:
    from cruxible_client.contracts.capture_reads import CaptureReadRequest

    result = _server_call(
        lambda client, instance_id: client.read_capture(
            instance_id, CaptureReadRequest(capture_digest=capture_digest, max_bytes=max_bytes)
        ),
        command_name="cruxible capture read",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("proposal")
def proposal_group() -> None:
    """Inspect, review, approve, and activate candidates."""


@proposal_group.command("list")
@click.option("--status", type=click.Choice(["open", "settled", "incomplete"]), default=None)
@click.option(
    "--limit",
    default=contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.PROPOSAL_LIST_MAX_LIMIT),
    help="Proposals per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same listing.")
@json_option
@handle_errors
def list_proposals(status: str | None, limit: int, cursor: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_proposals(
            instance_id,
            status=cast(Any, status),
            limit=limit,
            cursor=cursor,
        ),
        command_name="cruxible proposal list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo("STATUS  TERMINAL_REASON  PROPOSAL_ID  TARGET_REF  COORDINATE_TIME")
    for entry in result.entries:
        terminal = ",".join(entry.incomplete_reasons) or entry.terminal_reason or "-"
        click.echo(
            f"{entry.status}  {terminal}  {entry.proposal_id}  "
            f"{entry.target_ref or '-'}  {entry.admitted_at or '-'}"
        )
    click.echo(f"Coordinate: {result.coordinate.git_oid}")
    _echo_list_continuation(result.next_cursor)


@proposal_group.command("readmit")
@click.argument("proposal_id")
@change_control_options
@json_option
@handle_errors
def readmit_proposal(
    proposal_id: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    result = _server_call(
        lambda client, instance_id: client.readmit_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible proposal readmit",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    proposal = result.proposal.proposal
    evaluation = proposal.get("evaluation", {})
    admission = proposal.get("admission", {})
    if result.proposal.status != "admitted":
        click.echo(f"{result.proposal.status}  from {result.source_proposal_id}")
        echo_preview_next(result.proposal.status, result.proposal.accepted_coordinate)
        return
    click.echo(
        f"{evaluation.get('verdict')}  {admission.get('proposal_id')}  "
        f"from {result.source_proposal_id}"
    )


@proposal_group.command("withdraw")
@click.argument("proposal_id")
@click.option(
    "--reason",
    required=True,
    help="Why this proposal will never be settled. Recorded verbatim.",
)
@change_control_options
@json_option
@handle_errors
def withdraw_proposal(
    proposal_id: str, reason: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Retire an open proposal that will never be activated."""

    result = _server_call(
        lambda client, instance_id: client.withdraw_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            reason=reason,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible proposal withdraw",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    already = " (already withdrawn)" if result.already_withdrawn else ""
    click.echo(f"{result.status}  {result.proposal_id}  {result.withdrawn_at}{already}")
    click.echo(f"Reason: {result.reason}")
    echo_preview_next(result.status, result.coordinate)


@proposal_group.command("inspect")
@click.argument("proposal_id")
@json_option
@handle_errors
def inspect_proposal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.inspect_proposal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
        ),
        command_name="cruxible proposal inspect",
    )
    _emit_json(result.model_dump(mode="json"))


@proposal_group.command("refusal")
@click.argument("proposal_id")
@json_option
@handle_errors
def inspect_refusal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.inspect_refusal(
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
        ),
        command_name="cruxible proposal refusal",
    )
    _emit_json(result.model_dump(mode="json"))


@proposal_group.command("review")
@click.argument("proposal_id")
@click.option("--include-body/--redacted", default=True)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose projection catalog and markers are observed locally.",
)
@json_option
@handle_errors
def review_proposal(
    proposal_id: str,
    include_body: bool,
    workspace_root: str,
    output_json: bool,
) -> None:
    workspace = Path(workspace_root)

    def _review_at_observed_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.ProposalReview:
        head = client.head(instance_id)
        next_observation = observe_next_workspace(workspace)
        observation: dict[str, object] = {
            "tag": "playbill-review-workspace-observation-v1",
            "presentation_policy": next_observation.get("presentation_policy"),
            "presentation_policy_notes": next_observation.get("presentation_policy_notes", []),
            "projection_coverage": None,
        }
        projection = observe_projection_coverage(
            workspace,
            coordinate=contracts.AcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
        )
        if projection is not None:
            observation["projection_coverage"] = projection
        resolved_id = client.resolve_proposal_selector(instance_id, proposal_id).proposal_id
        return client.review_proposal(
            instance_id,
            resolved_id,
            include_body=include_body,
            workspace_observation=observation,
        )

    result = _server_call(
        _review_at_observed_coordinate,
        command_name="cruxible proposal review",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        review = ProposalReview.model_validate(result.model_dump(mode="json"))
        click.echo(render_playbill_proposal_review_pointer(review), nl=False)


@proposal_group.command("approve")
@click.argument("proposal_id")
@click.option(
    "--signer-id",
    default=None,
    help="Signing principal (default: CRUXIBLE_PRINCIPAL_ID or the global --principal-id).",
)
@click.option(
    "--key",
    "private_key_path",
    required=True,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help="The signer's private key (also CRUXIBLE_PRINCIPAL_KEY).",
)
@click.option("--yes", is_flag=True, help="Approve after rendering without an interactive prompt.")
@json_option
@handle_errors
def approve_proposal(
    proposal_id: str,
    signer_id: str | None,
    private_key_path: str,
    yes: bool,
    output_json: bool,
) -> None:
    configured = _root_ctx_obj().get("principal_id")
    if signer_id is None:
        if configured is None:
            raise click.UsageError(
                "proposal approve needs the signing principal; repair: pass --signer-id ID "
                "or set CRUXIBLE_PRINCIPAL_ID"
            )
        signer_id = str(configured)
    resolved_signer: str = signer_id

    def _resolve_and_prepare(
        client: CruxibleClient, instance_id: str
    ) -> tuple[str, contracts.ApprovalChallenge]:
        resolved_id = client.resolve_proposal_selector(instance_id, proposal_id).proposal_id
        return resolved_id, client.prepare_approval(
            instance_id, resolved_id, signer_id=resolved_signer, include_body=True
        )

    resolved_id, challenge = _server_call(
        _resolve_and_prepare,
        command_name="cruxible proposal approve",
    )
    review = ProposalReview.model_validate(challenge.review.model_dump(mode="json"))
    if not output_json:
        click.echo(render_playbill_proposal_review(review), nl=False)
    if not yes and not click.confirm("Sign this exact candidate?"):
        raise click.Abort()
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=resolved_signer,
        private_key_path=Path(private_key_path),
        expected_public_key=principal.public_key,
        forbidden_roots=_custody_forbidden_roots(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge.statement))
    result = _server_call(
        lambda client, instance_id: client.submit_approval(
            instance_id,
            resolved_id,
            attestation=attestation.model_dump(mode="json"),
        ),
        command_name="cruxible proposal approve",
    )
    result = _with_git_workspace_note(result)
    if output_json:
        _emit_json(_json_receipt(result))
    else:
        click.echo(f"Approved {result.candidate_digest} as {result.signer_id}")


@proposal_group.command("activate")
@click.argument("proposal_id")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace holding .cruxible/coverage.json and its optional floor output.",
)
@click.option("--no-sync", is_flag=True, help="Skip the activating workspace's block sync.")
@brief_option
@json_option
@handle_errors
def activate_proposal(
    proposal_id: str,
    workspace_root: str,
    no_sync: bool,
    output_brief: bool,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: activate_with_workspace_refresh(
            client,
            instance_id,
            client.resolve_proposal_selector(instance_id, proposal_id).proposal_id,
            workspace=Path(workspace_root),
            sync=not no_sync,
        ),
        command_name="cruxible proposal activate",
    )
    payload = result.model_dump(mode="json")
    if result.floor_refresh.status == "failed":
        message = result.floor_refresh.message or "unknown client workspace error"
        _emit_json(payload)
        raise click.ClickException(
            f"proposal activation status={result.status}; floor refresh failed: {message}"
        )
    if result.block_sync is not None and result.block_sync.has_refusals:
        _emit_json(payload)
        raise click.ClickException(
            f"proposal activation status={result.status}; block sync reported refusals; "
            "repair: cruxible block sync --all"
        )
    if output_brief:
        _emit_brief(
            outcome=result.status,
            ids={
                "proposal": result.proposal_id,
                "coordinate": (
                    None
                    if result.accepted_coordinate is None
                    else result.accepted_coordinate.git_oid
                ),
            },
            next_command="cruxible next --brief",
        )
        return
    _emit_json(payload)


@playbill_group.command("whoami")
@json_option
@handle_errors
def whoami(output_json: bool) -> None:
    """Explain the transport-derived actor, permission mode, and principal status."""

    result = _server_call(
        lambda client, instance_id: client.whoami(instance_id),
        command_name="cruxible whoami",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Actor: {result.actor_id or 'none (this credential acts as no principal)'}")
    if result.actor_id_source == "runtime_credential":
        click.echo(
            f"Actor ID is the principal this bearer credential is bound to "
            f"(credential: {result.credential_label})"
        )
    elif result.actor_id_source == "unbound_credential":
        click.echo(
            f"Bearer credential {result.credential_label} is bound to no principal: it keeps "
            "its transport authority but cannot author"
        )
    elif result.actor_id_source == "principal_claim":
        click.echo("Actor ID comes from the configured principal ID (CRUXIBLE_PRINCIPAL_ID)")
    else:
        click.echo("Actor ID comes from the local operator identity (no principal configured)")
    if not result.authenticated:
        click.echo(
            "Identity is a claim, not authentication: daemon auth is off, so every "
            "process of this OS user is equally trusted."
        )
    click.echo(f"Credential permission mode: {result.credential_permission_mode}")
    click.echo(f"Principal registration: {result.principal_registration_status or 'none'}")
    click.echo(f"Active principals: {', '.join(result.active_principal_ids) or 'none'}")
    if result.authoring_refusal is None:
        click.echo("Can author: yes")
    else:
        refusal = result.authoring_refusal
        click.echo(f"Can author: no ({refusal.code})")
        click.echo(f"  Why: {refusal.detail}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@playbill_group.group("sources")
def sources_group() -> None:
    """Compile declared local files into path-free exact-byte bundles."""


def _source_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option("--root", "repository_root", required=True)(function)
    function = click.option("--local-catalog", default=None)(function)
    function = click.option("--root-alias", multiple=True, help="Repeat NAME=PATH.")(function)
    return click.option("--catalog", "portable_catalog", required=True)(function)


@sources_group.command("compile")
@_source_options
@click.option("--output", required=True, type=click.Path(dir_okay=False))
@json_option
@handle_errors
def compile_sources(
    portable_catalog: str,
    local_catalog: str | None,
    root_alias: tuple[str, ...],
    repository_root: str,
    output: str,
    output_json: bool,
) -> None:
    catalog = _catalog(portable_catalog, local_catalog)
    bundle = _server_call(
        lambda client, instance_id: _compile_remote_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=Path(repository_root),
            aliases=_root_aliases(root_alias),
        ),
        command_name="cruxible sources compile",
    )
    _write_bundle(output, bundle)
    if output_json:
        _emit_json(bundle.manifest.model_dump(mode="json"))
    else:
        click.echo(f"Compiled {bundle.manifest.compilation_digest} -> {output}")


@sources_group.command("check")
@_source_options
@json_option
@handle_errors
def check_sources(
    portable_catalog: str,
    local_catalog: str | None,
    root_alias: tuple[str, ...],
    repository_root: str,
    output_json: bool,
) -> None:
    catalog = _catalog(portable_catalog, local_catalog)

    def call(client: CruxibleClient, instance_id: str) -> contracts.SourceCheckResult:
        bundle = _compile_remote_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=Path(repository_root),
            aliases=_root_aliases(root_alias),
        )
        return client.check_source_bundle(instance_id, bundle=bundle.model_dump(mode="json"))

    result = _server_call(call, command_name="cruxible sources check")
    _emit_json(result.model_dump(mode="json"))


@sources_group.command("propose")
@click.option(
    "--bundle", "bundle_path", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--source", "source_name", required=True)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def propose_sources(
    bundle_path: str,
    source_name: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    bundle = _read_model(bundle_path, SourceCompilationBundle)
    result = _server_call(
        lambda client, instance_id: client.propose_source_bundle(
            instance_id,
            bundle=bundle.model_dump(mode="json"),
            source_name=source_name,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible sources propose",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("principal")
def principal_group() -> None:
    """List and govern ordinary/recovery public keys."""


@principal_group.command("add")
@click.argument("principal_id")
@click.option(
    "--kind",
    type=click.Choice(("ordinary", "recovery")),
    default="ordinary",
    show_default=True,
    help="Closed principal kind; daemon is instance-owned.",
)
@click.option(
    "--key-dir",
    required=True,
    help="New principal's custody directory: receives its key and its cruxible.env settings.",
)
@click.option(
    "--name", "proposal_name", default=None, help="Proposal name (default: add-PRINCIPAL_ID)."
)
@click.option(
    "--signer-key",
    default=None,
    envvar=PRINCIPAL_KEY_ENV,
    type=click.Path(dir_okay=False),
    help=(
        "Your own private key (also CRUXIBLE_PRINCIPAL_KEY). Approves and activates the "
        "registration in the same command; without it the registration is only proposed."
    ),
)
@click.option(
    "--mode",
    "permission_mode",
    type=click.Choice(("read_only", "governed_write", "graph_write", "admin")),
    default="governed_write",
    show_default=True,
    help=(
        "Tier of the bearer credential minted for the new principal when the daemon runs "
        "with auth. governed_write proposes and authors but cannot approve or activate."
    ),
)
@change_control_options
@json_option
@handle_errors
def add_principal(
    principal_id: str,
    kind: str,
    key_dir: str,
    proposal_name: str | None,
    signer_key: str | None,
    permission_mode: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Set up one principal (an agent) in one command.

    Generates the principal's key in `--key-dir`, proposes its registration,
    and, with `--signer-key` (your own key), approves and activates it. When
    the daemon runs with auth it also mints the principal's bearer credential,
    signed with the new key. Everything the agent needs -- connection settings,
    principal ID, key path, and that credential -- lands in `DIR/cruxible.env`.
    `--dry-run` evaluates the registration and writes nothing: no key, no
    settings, no proposal.
    """

    principal_kind = cast(PrincipalKind, kind)
    try:
        ref_name = canonical_proposal_ref_name(proposal_name or f"add-{principal_id}")
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--name") from exc
    custody = Path(key_dir).expanduser()
    mode = cast(contracts.RuntimeCredentialPermissionMode, permission_mode)
    if dry_run:
        preview = _preview_principal_change(
            principal_id=principal_id,
            kind=principal_kind,
            key_dir=custody,
            proposal_name=ref_name,
            at=at,
            refuse_existing=True,
        )
        _emit_principal_preview(preview, output_json=output_json)
        return

    def call(client: CruxibleClient, instance_id: str) -> _PrincipalAddOutcome:
        existing = principal_records(client, instance_id)
        if any(item.principal_id == principal_id for item in existing):
            raise click.ClickException(f"Cruxible principal already exists: {principal_id}")
        material = generate_client_principal_key(
            custody,
            principal_id=principal_id,
            kind=principal_kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        proposed = client.propose_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=ref_name,
            dry_run=False if dry_run is False else None,
            at=at,
        )
        outcome = _PrincipalAddOutcome(
            instance_id=instance_id,
            material=material,
            proposal=proposed,
            proposal_id=_admitted_proposal_id(proposed),
        )
        if signer_key is None or outcome.proposal_id is None:
            return outcome
        identity = client.whoami(instance_id)
        if identity.actor_id is None:
            return outcome
        outcome.signer_id = identity.actor_id
        _approve_with_key(
            client,
            instance_id,
            outcome.proposal_id,
            signer_id=identity.actor_id,
            private_key_path=Path(signer_key).expanduser(),
        )
        activated = client.activate_proposal(instance_id, outcome.proposal_id)
        outcome.activated = activated.status == "accepted"
        if outcome.activated and identity.authenticated and principal_kind == "ordinary":
            minted = client.create_runtime_credential(
                instance_id,
                principal_id=principal_id,
                permission_mode=mode,
                principal_proof=sign_runtime_credential_mint(
                    instance_id=instance_id,
                    principal_id=principal_id,
                    permission_mode=mode,
                    label=principal_id,
                    private_key_path=material.private_key_path,
                    forbidden_roots=_custody_forbidden_roots(),
                ),
            )
            outcome.credential = minted
        return outcome

    outcome = _server_call(call, command_name="cruxible principal add")
    token = None if outcome.credential is None else outcome.credential.token
    settings = write_principal_settings(
        custody,
        ctx_obj=_root_ctx_obj(),
        instance_id=outcome.instance_id,
        principal_id=principal_id,
        private_key_path=outcome.material.private_key_path,
        token=token,
        written_by="cruxible principal add",
    )
    next_steps = _principal_add_next_steps(outcome, principal_id, custody, permission_mode)
    if output_json:
        _emit_json(
            {
                "principal_id": principal_id,
                "status": "active" if outcome.activated else "proposed",
                "proposal_id": outcome.proposal_id,
                "private_key_path": str(outcome.material.private_key_path),
                "settings_path": str(settings),
                "credential": (
                    None
                    if outcome.credential is None
                    else outcome.credential.credential.model_dump(mode="json")
                ),
                "next_steps": next_steps,
                "proposal": outcome.proposal.model_dump(mode="json"),
            }
        )
        return
    state = "registered and active" if outcome.activated else "proposed, not yet active"
    click.echo(f"Principal {principal_id}: {state}")
    if outcome.proposal_id is not None:
        click.echo(f"Proposal: {outcome.proposal_id}")
    click.echo(f"Private key retained locally at: {outcome.material.private_key_path}")
    if outcome.credential is not None:
        credential = outcome.credential.credential
        click.echo(
            f"Bearer credential: {credential.credential_id} ({credential.permission_mode}), "
            "written to the settings file, not printed"
        )
    click.echo(f"Settings: {settings}")
    click.echo(f"The agent loads them with: set -a; . {settings}; set +a")
    for step in next_steps:
        click.echo(f"Next: {step}")


@dataclass
class _PrincipalAddOutcome:
    instance_id: str
    material: GeneratedKeyMaterial
    proposal: contracts.ProposalInspection
    proposal_id: str | None
    signer_id: str | None = None
    activated: bool = False
    credential: contracts.RuntimeCredentialResult | None = None


def _admitted_proposal_id(proposed: contracts.ProposalInspection) -> str | None:
    admission = proposed.proposal.get("admission")
    if isinstance(admission, Mapping) and isinstance(admission.get("proposal_id"), str):
        return str(admission["proposal_id"])
    return None


def _approve_with_key(
    client: CruxibleClient,
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    private_key_path: Path,
) -> None:
    challenge = client.prepare_approval(instance_id, proposal_id, signer_id=signer_id)
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=signer_id,
        private_key_path=private_key_path,
        expected_public_key=principal.public_key,
        forbidden_roots=_custody_forbidden_roots(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge.statement))
    client.submit_approval(
        instance_id, proposal_id, attestation=attestation.model_dump(mode="json")
    )


def _principal_add_next_steps(
    outcome: _PrincipalAddOutcome, principal_id: str, custody: Path, permission_mode: str
) -> list[str]:
    if outcome.activated:
        return []
    if outcome.proposal_id is None:
        return [
            "the registration proposal was not admitted; inspect it with `cruxible proposal list`"
        ]
    signer = outcome.signer_id or "YOUR_PRINCIPAL_ID"
    steps = []
    if outcome.signer_id is None:
        steps.append(
            f"cruxible proposal approve {outcome.proposal_id} --signer-id {signer} "
            "--key YOUR_PRIVATE_KEY"
        )
    steps.append(f"cruxible proposal activate {outcome.proposal_id}")
    steps.append(
        f"with daemon auth on: cruxible credential mint --principal-id {principal_id} "
        f"--key-dir {custody} --mode {permission_mode} (writes the token into "
        f"{PRINCIPAL_SETTINGS_FILE})"
    )
    return steps


def _preview_principal_change(
    *,
    principal_id: str,
    kind: PrincipalKind | None,
    key_dir: Path,
    proposal_name: str,
    at: str | None,
    refuse_existing: bool,
) -> contracts.ProposalInspection:
    """Preview a principal change whose key would be generated: nothing is written.

    The record carries a throwaway in-memory public key; the change set, the
    principal-lifecycle law and the approval it needs are exactly the commit's.
    """

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == principal_id), None)
        if refuse_existing and target is not None:
            raise click.ClickException(f"Cruxible principal already exists: {principal_id}")
        if not refuse_existing and target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {principal_id}")
        principal_kind = kind if target is None else target.kind
        assert principal_kind is not None
        record = preview_client_principal(
            key_dir,
            principal_id=principal_id,
            kind=principal_kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        return client.propose_principal_change(
            instance_id,
            principal=record.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=True,
            at=at,
        )

    return _server_call(call, command_name="cruxible principal change")


def _emit_principal_preview(preview: contracts.ProposalInspection, *, output_json: bool) -> None:
    if output_json:
        _emit_json(preview.model_dump(mode="json"))
        return
    click.echo(f"Principal change: {preview.status}; nothing was written")
    echo_preview_next(preview.status, preview.accepted_coordinate)


def _principal_successor(
    *,
    target_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
) -> contracts.ProposalInspection:
    if dry_run:
        return _preview_principal_change(
            principal_id=target_id,
            kind=None,
            key_dir=Path(key_dir).expanduser(),
            proposal_name=proposal_name,
            at=at,
            refuse_existing=False,
        )

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == target_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {target_id}")
        material = generate_client_principal_key(
            Path(key_dir).expanduser(),
            principal_id=target_id,
            kind=target.kind,
            forbidden_roots=_custody_forbidden_roots(),
        )
        return client.propose_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    return _server_call(call, command_name="cruxible principal change")


@principal_group.command("rotate")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def rotate_principal(
    principal_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose a self-rotation; activation requires the actor's key signature."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
    )
    _emit_json(result.model_dump(mode="json"))


@principal_group.command("recover")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def recover_principal(
    principal_id: str,
    key_dir: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Use recovery identity for a narrowly governed key replacement."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
        dry_run=dry_run,
        at=at,
    )
    _emit_json(result.model_dump(mode="json"))


@principal_group.command("revoke")
@click.argument("principal_id")
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def revoke_principal(
    principal_id: str, proposal_name: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Propose revoking a principal (and, once active, every credential that acts as it)."""

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        matches = principal_records(client, instance_id)
        target = next((item for item in matches if item.principal_id == principal_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Cruxible principal: {principal_id}")
        revoked = target.model_copy(update={"status": "revoked"})
        return client.propose_principal_change(
            instance_id,
            principal=revoked.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    result = _server_call(call, command_name="cruxible principal revoke")
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("claim-type")
def claim_type_group() -> None:
    """Propose and read the governed predicate vocabulary."""


@claim_type_group.command("propose")
@click.option("--input", "input_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--envelope", type=click.Path(exists=True, dir_okay=False), hidden=True)
@click.option(
    "--template",
    is_flag=True,
    help="Print one complete model-generated ClaimTypeInputRecord without contacting the daemon.",
)
@click.option("--name", "proposal_name")
@change_control_options
@json_option
@handle_errors
def propose_claim_type(
    input_path: str | None,
    envelope: str | None,
    template: bool,
    proposal_name: str | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Use the sanctioned typed-input ClaimType proposal path."""

    if sum((input_path is not None, envelope is not None, template)) != 1:
        raise click.UsageError("provide exactly one of --input or --template")
    if template:
        _emit_json(claim_type_input_template().model_dump(mode="json"))
        return
    if envelope is not None:
        envelope_payload = _read_mapping(envelope)
        resolved_name = proposal_name or envelope_payload.get("predicate")
        if not isinstance(resolved_name, str) or not resolved_name:
            raise click.UsageError("--name is required when the ClaimType payload has no predicate")
        envelope_result = _server_call(
            lambda client, instance_id: client.propose_claim_type(
                instance_id,
                claim_type=envelope_payload,
                proposal_name=resolved_name,
                dry_run=dry_run,
                at=at,
            ),
            command_name="cruxible claim-type propose",
        )
        _emit_json(envelope_result.model_dump(mode="json"))
        return
    assert input_path is not None
    try:
        claim_type_input = ClaimTypeInputRecord.model_validate(_read_mapping(input_path))
    except ValidationError as exc:
        raise click.ClickException(
            "Invalid ClaimType input: "
            + "; ".join(
                f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
                for item in exc.errors(include_url=False)
            )
            + ". Pass a complete ClaimTypeInputRecord whose evidence_admission_policy.rules "
            "match its capture contracts"
        ) from exc
    input_result = _server_call(
        lambda client, instance_id: client.propose_claim_type_input(
            instance_id,
            input=claim_type_input.model_dump(mode="json"),
            proposal_name=proposal_name or claim_type_input.predicate,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible claim-type propose",
    )
    _emit_json(input_result.model_dump(mode="json"))


@claim_type_group.command("migrate")
@click.argument("request_file", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def migrate_claim_type(request_file: str, output_json: bool) -> None:
    """Propose one ClaimType successor and every dependent disposition atomically."""

    try:
        request = _CLAIM_TYPE_MIGRATION_ADAPTER.validate_python(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid ClaimType migration: {validation_summary(exc)}"
        ) from exc
    result = _server_call(
        lambda client, instance_id: client.migrate_claim_type(
            instance_id,
            request=request.model_dump(mode="json"),
        ),
        command_name="cruxible claim-type migrate",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.tag == "playbill-claim-type-migration-preflight-v1":
        click.echo("ClaimType migration preflight")
        click.echo(f"Coordinate: {result.coordinate.git_oid}")
        click.echo(f"Successor: {result.successor_artifact_digest}")
        click.echo(f"Blast radius: {len(result.dependents)} dependent(s)")
        for dependent in result.dependents:
            identity = dependent.get("identity", {})
            identity_name = identity.get("name", dependent.get("claim_id", "<unknown>"))
            click.echo(
                f"  {identity.get('kind', 'Claim')}:{identity_name}"
                f" ({dependent.get('artifact_kind', 'claim')})"
            )
    else:
        click.echo("ClaimType migration proposal")
        click.echo(f"Operation: {result.operation_digest}")
        click.echo(f"Dependents: {len(result.dependents)}")
        admission = result.proposal.proposal.get("admission", {})
        proposal_id = admission.get("proposal_id", "<submitted>")
        click.echo(f"Proposal: {proposal_id}")
        click.echo(f"Next: cruxible proposal approve {proposal_id}")
    click.echo("Semantic delta:")
    if not result.semantic_delta:
        click.echo("  (no semantic field changes)")
    for row in result.semantic_delta:
        before = (
            "<absent>"
            if row.before.state == "absent"
            else json.dumps(row.before.value, sort_keys=True, ensure_ascii=False)
        )
        after = (
            "<absent>"
            if row.after.state == "absent"
            else json.dumps(row.after.value, sort_keys=True, ensure_ascii=False)
        )
        click.echo(f"  {row.field_path or '/'}: {before} -> {after}")
    click.echo("Lint:")
    if result.lint is None or not result.lint.warnings:
        click.echo("  none")
    else:
        for warning in result.lint.warnings:
            click.echo(f"  {warning.get('field_path', '$')}: {warning.get('code', 'warning')}")


@claim_type_group.command("upgrade")
@click.option(
    "--claim-type",
    "claim_types",
    multiple=True,
    help="Predicate to upgrade; repeat for more. Default: every live ClaimType before v7.",
)
@click.option(
    "--revision-evidence",
    type=click.Choice(["replace", "accumulate"]),
    default="replace",
    show_default=True,
    help=(
        "What a revision that changes its statement keeps: only the evidence it cites "
        "(replace) or everything its predecessors cited too (accumulate, the meaning "
        "before v7)."
    ),
)
@change_control_options
@json_option
@handle_errors
def upgrade_claim_types(
    claim_types: tuple[str, ...],
    revision_evidence: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Propose moving live ClaimTypes to v7, one reviewed change set.

    v7 states what a ClaimType's Claims need (evidence_requirement, kept at
    self) and what a statement-changing revision keeps (revision_evidence).
    Every Claim is carried with its backing intact. It previews by default;
    commit the preview with ``--commit --at OID``, then approve as usual.
    """

    request = ClaimTypeUpgradeRequest.model_validate(
        {
            "claim_types": claim_types,
            "revision_evidence": revision_evidence,
            "dry_run": dry_run,
            "at": at,
        }
    )
    result = _server_call(
        lambda client, instance_id: client.upgrade_claim_types(instance_id, request),
        command_name="cruxible claim-type upgrade",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"ClaimType upgrade: {result.status}")
    for item in result.upgraded:
        click.echo(
            f"  upgraded {item.claim_type} ({item.from_format}): revision evidence "
            f"{item.revision_evidence_before} -> {item.revision_evidence_after}"
        )
        for version in item.widened_versions:
            click.echo(f"    now also admits {version}")
    for name in result.unchanged:
        click.echo(f"  already v7 {name}")
    for refusal in result.refused:
        click.echo(f"  left as is {refusal.claim_type}: {refusal.reason}")
    if result.carried_claims:
        click.echo(f"Claims carried: {result.carried_claims}")
    if result.detail:
        click.echo(result.detail)
    if result.proposal_id:
        click.echo(f"Next: cruxible proposal approve {result.proposal_id}")
    echo_preview_next(result.status, result.coordinate)


@claim_type_group.command("upgrade-evidence-rules")
@change_control_options
@json_option
@handle_errors
def upgrade_evidence_rules(dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Propose moving every live ClaimType to identity evidence rules (v6).

    Needs compiler revision 31. Each rule converts only when it keeps its meaning;
    the rest are reported for an explicit decision. It previews by default;
    commit the preview with ``--commit --at OID``, then approve as usual.
    """

    request = EvidenceRuleUpgradeRequest(dry_run=dry_run, at=at)
    result = _server_call(
        lambda client, instance_id: client.upgrade_evidence_rules(instance_id, request),
        command_name="cruxible claim-type upgrade-evidence-rules",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Evidence rule upgrade: {result.status}")
    for item in result.converted:
        click.echo(f"  converted {item.claim_type}")
        for version in item.widened_versions:
            click.echo(f"    now also admits {version}")
    for refusal in result.refused:
        click.echo(f"  left as is {refusal.claim_type}: {refusal.reason}")
    if result.carried_claims:
        click.echo(f"Claims carried: {result.carried_claims}")
    if result.detail:
        click.echo(result.detail)
    if result.proposal_id:
        click.echo(f"Next: cruxible proposal approve {result.proposal_id}")
    echo_preview_next(result.status, result.coordinate)


@playbill_group.group("claim")
def claim_group() -> None:
    """Propose, read, and explain first-class governed Claims."""


@playbill_group.command("resolution-contracts")
@click.argument("claim_id", required=False)
@click.option(
    "--request",
    "request_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Advanced: a ResolutionContractsRequest file with an exact hypothesis reference.",
)
@json_option
@handle_errors
def resolution_contracts(claim_id: str | None, request_file: str | None, output_json: bool) -> None:
    """Find accepted tests of a Claim, by Claim ID (CLM-... or Claim:CLM-...).

    The daemon resolves the Claim's accepted version; `--request FILE` takes an
    exact ClaimVersionReference hypothesis instead.
    """
    if (claim_id is None) == (request_file is None):
        raise click.UsageError("provide exactly one of CLAIM_ID or --request FILE")
    request = (
        _read_model(request_file, contracts.ResolutionContractsRequest)
        if request_file is not None
        else contracts.ResolutionContractsRequest(hypothesis=cast(str, claim_id))
    )
    result = _server_call(
        lambda client, instance_id: client.resolution_contracts(instance_id, request=request),
        command_name="cruxible resolution-contracts",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.contracts:
        click.echo("No resolution contracts test this Claim version.")
        return
    for view in result.contracts:
        click.echo(
            f"{view.reference.identity.qualified} {view.reference.artifact_digest} "
            f"{view.contract.lifecycle.state}"
        )


@playbill_group.command("predict")
@click.argument("request_file", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def predict(request_file: str, output_json: bool) -> None:
    """Submit a governed resolution contract for an accepted Claim."""

    try:
        request = contracts.PredictRequest.model_validate(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid prediction request: {validation_summary(exc)}"
        ) from exc
    result = _server_call(
        lambda client, instance_id: client.predict(instance_id, request=request),
        command_name="cruxible predict",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Contract: {result.contract_identity}")
    click.echo(f"Proposal: {result.proposal_id}")


@playbill_group.command("settle")
@click.argument("prediction_id")
@click.option(
    "--observation",
    "observation",
    help="Claim ID (CLM-...) of the accepted observation that settles the prediction.",
)
@click.option(
    "--request",
    "request_file",
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Advanced: a SettleRequest file (exact contract reference, anchor event, "
        "or terminal evidence)."
    ),
)
@json_option
@handle_errors
def settle(
    prediction_id: str,
    observation: str | None,
    request_file: str | None,
    output_json: bool,
) -> None:
    """Settle one prediction from a later accepted observation.

    PREDICTION_ID is the contract name or the bound window id (RSC-...) that
    `cruxible next` names; the daemon resolves the exact contract, window and
    observation version from it and `--observation CLM-...`.
    """

    if (observation is None) == (request_file is None):
        raise click.UsageError(
            "provide --observation CLAIM_ID (the accepted observation that settles it) "
            "or --request FILE"
        )
    try:
        request = (
            contracts.SettleRequest.model_validate(_read_mapping(request_file))
            if request_file is not None
            else contracts.SettleRequest(observation=observation)
        )
    except ValidationError as exc:
        raise click.ClickException(
            f"Invalid settlement request: {validation_summary(exc)}"
        ) from exc
    result = _server_call(
        lambda client, instance_id: client.settle_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        command_name="cruxible settle",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Prediction {result.prediction_id}: settled")
    click.echo(f"Outcome: {result.resolution['settlement_outcome']}")


@claim_group.command("recover-attestation")
@handle_errors
def recover_claim_attestations() -> None:
    """Roll the sole durable unpublished attestation forward after a poison refusal.

    Run this when a Claim write or attestation refuses because the
    Claim-attestation evidence ledger requires recovery.
    """

    _server_call(
        lambda client, instance_id: client.recover_claim_attestations(instance_id),
        command_name="cruxible claim recover-attestation",
    )
    click.echo("Claim-attestation evidence ledger recovered.")


@claim_group.command("attest")
@click.argument("claim_id")
@click.option("--support", is_flag=True)
@click.option("--contradict", is_flag=True)
@click.option("--unsure", is_flag=True)
@click.option("--note")
@click.option(
    "--valid-until",
    default=None,
    help=(
        "ISO-8601 end of this attestation. An --unsure hold on stale or uncovered "
        "evidence otherwise lapses after the ClaimType's unsure_hold_for (default 30 days)."
    ),
)
@json_option
@handle_errors
def attest_claim(
    claim_id: str,
    support: bool,
    contradict: bool,
    unsure: bool,
    note: str | None,
    valid_until: str | None,
    output_json: bool,
) -> None:
    """Sign that this caller examined the current exact Claim.

    --unsure holds the Claim's contested `next` rows until what you examined
    changes, instead of forcing a judgment you are not confident in.
    """

    selected = tuple(
        value
        for enabled, value in (
            (support, "support"),
            (contradict, "contradict"),
            (unsure, "unsure"),
        )
        if enabled
    )
    if len(selected) != 1:
        raise click.UsageError("choose exactly one of --support, --contradict, or --unsure")
    stance = selected[0]

    def call(client: CruxibleClient, instance_id: str):  # type: ignore[no-untyped-def]
        signer = local_attestation_signer_from_environment(
            client,
            instance_id,
            workspace_root=_custody_workspace_root(),
        )
        return append_prepared_claim_attestation(
            client,
            instance_id,
            prepared=PreparedClaimAttestationRequest(
                claim_id=claim_id.removeprefix("Claim:"),
                attestation_basis="examined_existing",
                stance=cast(ClaimStance, stance),
                attested_at=datetime.now(UTC),
                valid_until=(
                    None
                    if valid_until is None
                    else datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                ),
                note=note,
            ),
            signer=signer,
        )

    result = _server_call(call, command_name="cruxible claim attest")
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("authoring")
def authoring_group() -> None:
    """Author, preflight, submit, and resume ergonomic governed writes."""


_EXPECTATION_ID_HELP = (
    "Which publication expectation this call is about. An intent that publishes "
    "several Claims owns one per publishing member; a singular Claim intent owns "
    "exactly one and may omit it."
)


@authoring_group.command("create")
@click.argument(
    "payload",
    required=False,
    metavar="PAYLOAD_FILE",
    type=click.Path(exists=True, dir_okay=False),
)
@click.option(
    "--example",
    "example_name",
    type=click.Choice(AUTHORING_EXAMPLE_NAMES),
    help="Print one model-generated payload template and exit.",
)
@json_option
@click.option(
    "--attestation-claim-id",
    help="Claim ID an attestation-door example revises (with --capture-digest).",
)
@click.option("--capture-digest")
@handle_errors
@click.pass_context
def create_authoring_intent(
    ctx: click.Context,
    payload: str | None,
    example_name: str | None,
    attestation_claim_id: str | None,
    capture_digest: str | None,
    output_json: bool,
) -> None:
    """Create a durable authoring intent or print a schema-derived example.

    \b
    Input kind family: claim | procedure | subject | query_definition |
    approval_policy | procedure_runtime_policy | procedure_mandate |
    acquisition_policy | line | trigger | change_set (tagless).

    \b
    Change-set member kind family: claim | claim_type | claim_type_succession |
    claim_retirement | subject | query_definition | procedure_mandate |
    acquisition_policy | line | trigger | procedure. claim_type, claim_type_succession and
    claim_retirement are member kinds only -- none is a top-level input.
    approval_policy and procedure_runtime_policy are the reverse: the member
    union parses either, but a change set refuses either, so author each as its
    own singleton input.

    \b
    One intent is one changeset: a change_set input carries any mix of those
    member kinds, lowers once, and admits or refuses whole, typed to the member
    index that offends. A claim_type_succession member succeeds an accepted
    ClaimType and dispositions its whole reverse-pin closure in the same
    generation, so a Claim member that speaks the new vocabulary lands with it.

    Use --example for a model-generated starting point; --example change-set
    prints a mixed set and --example claim-type-succession a vocabulary
    evolution. --example procedure, line, trigger, acquisition-policy and
    procedure-mandate are accepted together as members of one change set.
    """

    if (payload is None) == (example_name is None):
        raise click.UsageError("provide exactly one of PAYLOAD or --example")
    if payload is not None and (attestation_claim_id is not None or capture_digest is not None):
        raise click.UsageError("--attestation-claim-id/--capture-digest require --example")
    if example_name is not None:
        try:
            example = authoring_example(
                cast(AuthoringExampleName, example_name),
                claim_id=attestation_claim_id,
                capture_digest=capture_digest,
            )
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        click.echo(
            json.dumps(
                example.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        note = authoring_example_note(cast(AuthoringExampleName, example_name))
        if note is not None:
            # Beside the payload, never in it: stdout stays one JSON document.
            click.echo(f"# {note}", err=True)
        return
    assert payload is not None
    parsed_input = _read_authoring_input(payload)
    _echo_write_target("active", ctx.params)
    result = _server_call(
        lambda client, instance_id: client.create_authoring_input(
            instance_id, input=parsed_input.model_dump(mode="json")
        ),
        command_name="cruxible authoring create",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("get")
@click.argument("intent_id")
@json_option
@handle_errors
def get_authoring_intent(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring get",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("resume")
@click.argument("intent_id")
@json_option
@handle_errors
def resume_authoring_intent(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.resume_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring resume",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("list")
@json_option
@handle_errors
def list_pending_authoring_intents(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_pending_authoring_intents(instance_id),
        command_name="cruxible authoring list",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("compile")
@click.argument("payload", type=click.Path(exists=True, dir_okay=False))
@click.option("--intent-id", default=None)
@json_option
@handle_errors
def compile_authoring(payload: str, intent_id: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.compile_authoring_input(
            instance_id,
            input=_read_authoring_input(payload).model_dump(mode="json"),
            intent_id=intent_id,
        ),
        command_name="cruxible authoring compile",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("bind")
@click.option("--file", "source_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--anchor", required=True)
@click.option("--window-lines", type=click.IntRange(min=0), default=None)
@click.option(
    "--occurrence",
    type=click.IntRange(min=1),
    default=None,
    help="Select the 1-based anchor occurrence when the anchor is not unique.",
)
@click.option(
    "--payload-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Claim stub whose source contains only the working tag and logical source_id.",
)
@json_option
@handle_errors
def bind_authoring_selection(
    source_path: str,
    anchor: str,
    window_lines: int | None,
    occurrence: int | None,
    payload_file: str,
    output_json: bool,
) -> None:
    """Derive a Flow-A observation from one exact local source anchor, then compile."""

    source = Path(source_path).expanduser()
    try:
        content = source.read_bytes()
    except OSError as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    parsed_input = _read_authoring_input(payload_file)
    if not isinstance(parsed_input, ClaimInput):
        raise click.ClickException("authoring bind accepts only a claim input")
    payload = bind_working_selection_input(
        parsed_input,
        content=content,
        anchor=anchor,
        window_lines=window_lines,
        occurrence=occurrence,
    )
    result = _server_call(
        lambda client, instance_id: client.compile_authoring(
            instance_id,
            payload=payload.model_dump(mode="json"),
            intent_id=None,
        ),
        command_name="cruxible authoring bind",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("preflight")
@click.argument("intent_id")
@brief_option
@json_option
@handle_errors
def preflight_authoring_intent(intent_id: str, output_brief: bool, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.preflight_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring preflight",
    )
    if output_brief:
        codes = [
            str(item.get("code"))
            for item in (result.frontier.get("diagnostics") or [])
            if isinstance(item, dict)
        ]
        _emit_brief(
            outcome=result.verdict + (f" ({', '.join(codes)})" if codes else ""),
            ids={"intent": intent_id},
            next_command=(
                f"cruxible authoring submit {intent_id}"
                if result.verdict == "passed"
                else f"cruxible authoring create  # repair, then preflight {intent_id}"
            ),
        )
        return
    _emit_json(result.model_dump(mode="json"))
    if result.verdict == "refused":
        intent = _server_call(
            lambda client, instance_id: client.get_authoring_intent(instance_id, intent_id),
            command_name="cruxible authoring preflight",
        ).intent
        if intent.get("base_coordinate") != result.certificate.get("accepted_coordinate"):
            click.echo(
                f"Hint: run cruxible authoring rebase {intent_id}; resume does not advance "
                "a stale intent coordinate.",
                err=True,
            )


@authoring_group.command("rebase")
@click.argument("intent_id")
@json_option
@handle_errors
def rebase_authoring_intent(intent_id: str, output_json: bool) -> None:
    """Advance one refused, unsubmitted intent to the accepted head."""

    result = _server_call(
        lambda client, instance_id: client.rebase_authoring_intent(instance_id, intent_id),
        command_name="cruxible authoring rebase",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("submit")
@click.argument("intent_id")
@and_activate_option
@click.option(
    "--workspace-root",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    show_default=True,
    help="Workspace whose floor is refreshed when --and-activate activates.",
)
@brief_option
@json_option
@handle_errors
def submit_authoring_intent(
    intent_id: str,
    and_activate: bool,
    workspace_root: str,
    output_brief: bool,
    output_json: bool,
) -> None:
    def call(client: CruxibleClient, instance_id: str) -> tuple[Any, Any]:
        submitted = client.submit_authoring_intent(instance_id, intent_id)
        if not and_activate or submitted.status.state != "ready_to_activate":
            return submitted, None
        # Only a candidate that needs nothing further is activated here. Anything
        # else returns the submit result untouched: a partly-activated candidate
        # would be a worse answer than an unactivated one.
        proposal_id = submitted.status.proposal_id
        if proposal_id is None:  # pragma: no cover - ready_to_activate carries one
            return submitted, None
        return submitted, activate_with_workspace_refresh(
            client, instance_id, proposal_id, workspace=workspace_root
        )

    submitted, activation = _server_call(call, command_name="cruxible authoring submit")
    payload: dict[str, Any] = {"submit": submitted.model_dump(mode="json")}
    if activation is not None:
        payload["activation"] = activation.model_dump(mode="json")
    elif and_activate:
        payload["activation_note"] = _not_activated_note(submitted.status.state)
    if output_brief:
        _emit_brief(
            outcome=(
                "accepted" if activation is not None else f"submitted ({submitted.status.state})"
            ),
            ids={
                "intent": intent_id,
                "proposal": submitted.status.proposal_id,
                "coordinate": (
                    activation.accepted_coordinate.git_oid if activation is not None else None
                ),
                "receipt": activation.tag if activation is not None else None,
            },
            reason=_submit_refusal_reason(submitted),
            next_command=_submit_next_command(submitted, activated=activation is not None),
        )
        return
    _emit_json(payload if (and_activate or activation is not None) else payload["submit"])


def _not_activated_note(state: str) -> str:
    """Say why --and-activate stopped, in the caller's terms."""

    if state == "awaiting_external_approval":
        return (
            "not activated: the candidate needs an external approval. "
            "Collect it with `cruxible proposal approve`, then activate."
        )
    return f"not activated: the candidate is {state}, not ready_to_activate"


def _submit_refusal_reason(submitted: Any) -> str | None:
    """Render the complete typed preflight refusal on one transcript line."""

    if submitted.status.state != "preflight_refused" or not isinstance(submitted.intent, Mapping):
        return None
    preflight = submitted.intent.get("last_preflight")
    frontier = preflight.get("frontier") if isinstance(preflight, Mapping) else None
    diagnostics = frontier.get("diagnostics") if isinstance(frontier, Mapping) else None
    blocked_checks = frontier.get("blocked_checks") if isinstance(frontier, Mapping) else None
    if not isinstance(diagnostics, list | tuple) and not isinstance(blocked_checks, list | tuple):
        return "preflight refused without a delivered diagnostic"
    rendered: list[str] = []
    for diagnostic in diagnostics if isinstance(diagnostics, list | tuple) else ():
        if not isinstance(diagnostic, Mapping):
            continue
        code = diagnostic.get("code")
        message = diagnostic.get("message")
        if not isinstance(code, str) or not isinstance(message, str):
            continue
        one_line_message = " ".join(message.split())
        rendered.append(f"{code}: {one_line_message}")
    for blocked in blocked_checks if isinstance(blocked_checks, list | tuple) else ():
        if not isinstance(blocked, Mapping):
            continue
        check = blocked.get("check")
        reason = blocked.get("reason")
        blocked_by = blocked.get("blocked_by")
        if (
            not isinstance(check, str)
            or not isinstance(reason, str)
            or not isinstance(blocked_by, list | tuple)
            or not all(isinstance(dependency, str) for dependency in blocked_by)
        ):
            continue
        dependencies = ", ".join(blocked_by)
        one_line_reason = " ".join(reason.split())
        rendered.append(f"blocked {check} by {dependencies}: {one_line_reason}")
    return "; ".join(rendered) or "preflight refused without a delivered diagnostic"


def _submit_next_command(submitted: Any, *, activated: bool) -> str | None:
    if activated:
        return None
    proposal_id = submitted.status.proposal_id
    if submitted.status.state == "ready_to_activate" and proposal_id:
        return f"cruxible proposal activate {proposal_id}"
    if submitted.status.state == "awaiting_external_approval" and proposal_id:
        return f"cruxible proposal approve {proposal_id}"
    if submitted.status.state == "preflight_refused":
        return f"cruxible authoring preflight {submitted.intent['intent_id']}"
    return None


@authoring_group.command("status")
@click.argument("intent_id")
@json_option
@handle_errors
def authoring_intent_status(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.authoring_intent_status(instance_id, intent_id),
        command_name="cruxible authoring status",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("abandon-insertion")
@click.argument("intent_id")
@click.option("--expectation-id", default=None, help=_EXPECTATION_ID_HELP)
@json_option
@handle_errors
def abandon_authoring_insertion(
    intent_id: str,
    expectation_id: str | None,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.abandon_authoring_insertion(
            instance_id,
            intent_id,
            expectation_id=expectation_id,
        ),
        command_name="cruxible authoring abandon-insertion",
    )
    _emit_json(result.model_dump(mode="json"))


# -- the write verbs: set, retire, write --------------------------------------------


class _WriteFileV1(BaseModel):
    """What ``cruxible write FILE`` reads: the changes, and why."""

    model_config = ConfigDict(extra="forbid")

    because: str | None = Field(
        default=None, min_length=1, description="Why; --because overrides it."
    )
    subject: SubjectRef | None = Field(
        default=None,
        description="The Subject (kind/id) of every change that names none.",
    )
    changes: list[Change] = Field(min_length=1)


def _write_options(function: Callable[..., Any]) -> Callable[..., Any]:
    for option in reversed(
        (
            click.option("--dry-run", is_flag=True, help="Run every check; write nothing."),
            click.option(
                "--no-accept",
                is_flag=True,
                help="Only propose, even when policy would let this write be accepted now.",
            ),
            click.option(
                "--at",
                "at_oid",
                default=None,
                help="The git oid (or 12+ hex prefix) you read at; refuse if the slot moved.",
            ),
            json_option,
        )
    ):
        function = option(function)
    return function


def _write_text(outcome: WriteOutcome) -> None:
    head = outcome.status.replace("_", " ")
    click.echo(f"{head} (generation {outcome.coordinate.generation}, {outcome.coordinate.git_oid})")
    for change in outcome.changes:
        target = " ".join(part for part in (change.subject, change.field) if part)
        if change.op == "retire":
            line = f"  retire {target}: {_get_value_text(change.before)}"
        elif change.op == "add":
            line = f"  add {target}: {_get_value_text(change.after)}"
        elif change.before is None:
            line = f"  set {target}: {_get_value_text(change.after)}"
        else:
            line = (
                f"  set {target}: {_get_value_text(change.before)} -> "
                f"{_get_value_text(change.after)}"
            )
        details = [item for item in (change.claim,) if item]
        if change.already_live:
            details.append("already live")
        if change.verdict is not None:
            details.append(f"verdict {change.verdict}")
        if change.capture is not None:
            details.append(f"evidence {change.capture}")
        if change.retired:
            details.append(f"also retires {', '.join(change.retired)}")
        if change.contenders_created:
            details.append(f"contends with {', '.join(change.contenders_created)}")
        click.echo(line + (f"  [{'; '.join(details)}]" if details else ""))
    for subject in outcome.subjects_added:
        click.echo(f"  + subject {subject}")
    if outcome.proposal is not None and outcome.status != "accepted":
        click.echo(f"proposal: {outcome.proposal.proposal_id} ({outcome.proposal.state})")
    if outcome.approval is not None:
        approval = outcome.approval
        who = ", ".join(approval.eligible_approvers) or "you, with a tier that may activate"
        click.echo(f"awaiting: {approval.reason.replace('_', ' ')}; approvers: {who}")
    for warning in outcome.warnings:
        click.echo(f"warning {warning.code}: {warning.message}", err=True)
    if outcome.refusal is not None:
        refusal = outcome.refusal
        where = "" if refusal.change is None else f" (change {refusal.change})"
        click.echo(f"{refusal.code}{where}: {refusal.message}", err=True)
        if refusal.candidates:
            click.echo(f"  nearest: {', '.join(refusal.candidates)}", err=True)
        if refusal.repair:
            click.echo(f"  repair: {refusal.repair}", err=True)
    if outcome.next is not None:
        click.echo(f"next: {outcome.next}")


def _finish_write(outcome: WriteOutcome, *, output_json: bool) -> None:
    if output_json:
        _emit_json(outcome.model_dump(mode="json"))
    else:
        _write_text(outcome)
    if outcome.refused:
        raise SystemExit(1)


def _write_request(model: type[ResultT], fields: Mapping[str, Any], *, example: str) -> ResultT:
    validator = getattr(model, "model_validate")
    try:
        return cast(ResultT, validator({**fields, "surface": "cli"}))
    except ValidationError as exc:
        raise click.UsageError(
            "; ".join(_model_field_errors(exc)) + f" (example: {example})"
        ) from None


def _expect_option_value(values: tuple[str, ...]) -> str | tuple[str, ...] | None:
    """``--expect`` given once is the value; given again, every live value."""

    if not values:
        return None
    return values[0] if len(values) == 1 else values


_EXPECT_HELP = (
    "Refuse unless the field holds this value now (compare-and-set); repeat it "
    "for every value of a many-valued field."
)


def _evidence_option_value(
    evidence_file: str | None,
    capture: str | None,
    workspace_root: str,
    contract: str | None = None,
) -> dict[str, Any] | None:
    given = [
        flag
        for flag, value in (
            ("--evidence-file", evidence_file),
            ("--capture", capture),
            ("--evidence-contract", contract),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise click.UsageError(f"pass one of {', '.join(given)}, not both")
    if capture is not None:
        return {"kind": "capture", "capture": capture}
    if contract is not None:
        return {"kind": "contract", "contract": contract}
    if evidence_file is None:
        return None
    try:
        observed = observe_evidence(
            FileEvidence(file=evidence_file), workspace=Path(workspace_root)
        )
    except (ValidationError, CoreError) as exc:
        raise click.UsageError(f"--evidence-file {evidence_file}: {exc}") from None
    assert observed is not None
    return observed.model_dump(mode="json")


@playbill_group.command("set")
@click.argument("subject")
@click.argument("field")
@click.argument("value")
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help="PATH#ANCHOR: cite text found once in a catalogued workspace file.",
)
@click.option(
    "--capture",
    default=None,
    help="Cite an existing Capture: its handle CAP-<12+ hex>, or its sha256 digest.",
)
@click.option(
    "--evidence-contract",
    default=None,
    help="Cite the newest verified Capture of this CaptureContract about SUBJECT.",
)
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option("--contend", is_flag=True, help="Contest the live value instead of replacing it.")
@click.option("--expect", "expect", multiple=True, help=_EXPECT_HELP)
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse unless the field holds no value now (compare-and-set on an empty field).",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose source catalog --evidence-file reads.",
)
@_write_options
@handle_errors
def set_value(
    subject: str,
    field: str,
    value: str,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    role: str | None,
    contend: bool,
    expect: tuple[str, ...],
    expect_absent: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Set FIELD of SUBJECT (kind/id) to VALUE, replacing the live value.

    The Claim it replaces is found for you. A Subject of a known kind that does
    not exist yet is added. VALUE is text: an enum member, a number or true/false
    for such fields, a Subject as kind/id, or the text itself for exact content.
    """

    if expect_absent and expect:
        raise click.UsageError("pass --expect or --expect-absent, not both")
    request = _write_request(
        SetRequest,
        {
            "subject": subject,
            "field": field,
            "value": value,
            "because": because,
            "evidence": _evidence_option_value(
                evidence_file, capture, workspace_root, evidence_contract
            ),
            "role": role,
            "contend": contend,
            "expect": () if expect_absent else _expect_option_value(expect),
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example='cruxible set dev.item/tidy-cli status done --because "Shipped."',
    )
    outcome = _server_call(
        lambda client, instance_id: client.set(instance_id, request=request),
        command_name="cruxible set",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("add")
@click.argument("subject")
@click.argument("field")
@click.argument("value")
@click.option("--because", required=True, help="Why; also the default evidence.")
@click.option(
    "--evidence-file",
    default=None,
    help="PATH#ANCHOR: cite text found once in a catalogued workspace file.",
)
@click.option(
    "--capture",
    default=None,
    help="Cite an existing Capture: its handle CAP-<12+ hex>, or its sha256 digest.",
)
@click.option(
    "--evidence-contract",
    default=None,
    help="Cite the newest verified Capture of this CaptureContract about SUBJECT.",
)
@click.option("--role", default=None, help="Only when the field permits several roles.")
@click.option(
    "--expect-absent",
    is_flag=True,
    help="Refuse when the value is already there, instead of answering it as done.",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose source catalog --evidence-file reads.",
)
@_write_options
@handle_errors
def add_value(
    subject: str,
    field: str,
    value: str,
    because: str,
    evidence_file: str | None,
    capture: str | None,
    evidence_contract: str | None,
    role: str | None,
    expect_absent: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Add VALUE to many-valued FIELD of SUBJECT (kind/id), beside the values there.

    A value already there is answered as done (--expect-absent refuses instead).
    A Subject of a known kind that does not exist yet is added. VALUE is text, as
    for set: a Subject as kind/id for a Subject-valued field.
    """

    request = _write_request(
        WriteRequest,
        {
            "changes": [
                {
                    "op": "add",
                    "subject": subject,
                    "field": field,
                    "value": value,
                    "evidence": _evidence_option_value(
                        evidence_file, capture, workspace_root, evidence_contract
                    ),
                    "role": role,
                    "expect_absent": expect_absent,
                }
            ],
            "because": because,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example=(
            'cruxible add dev.item/tidy-cli governs dev.item/cli-docs --because "Linked in review."'
        ),
    )
    outcome = _server_call(
        lambda client, instance_id: client.write(instance_id, request=request),
        command_name="cruxible add",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("retire")
@click.argument("target")
@click.argument("field", required=False)
@click.option("--because", required=True, help="Why it ends.")
@click.option(
    "--reason",
    type=click.Choice(["was-rescinded", "was-wrong", "superseded"]),
    default="was-rescinded",
    show_default=True,
    help="was-rescinded: withdrawn; was-wrong: it was false; superseded: its shape is gone.",
)
@click.option("--expect", "expect", multiple=True, help=_EXPECT_HELP)
@_write_options
@handle_errors
def retire(
    target: str,
    field: str | None,
    because: str,
    reason: str,
    expect: tuple[str, ...],
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Retire one live Claim: TARGET is a Claim ID, or a Subject (kind/id) and its FIELD.

    Claims that depend on it retire with it, in the same change set.
    """

    request = _write_request(
        RetireRequest,
        {
            "target": target if field is None else {"subject": target, "field": field},
            "because": because,
            "reason": reason,
            "expect": _expect_option_value(expect),
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example='cruxible retire CLM-0123456789abcdef0123456789abcdef --because "Wrong."',
    )
    outcome = _server_call(
        lambda client, instance_id: client.retire(instance_id, request=request),
        command_name="cruxible retire",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("write")
@click.argument("file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--because", default=None, help="Why; overrides the file's because.")
@click.option("--schema", is_flag=True, help="Print the JSON schema FILE is validated against.")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose source catalog file evidence reads.",
)
@_write_options
@handle_errors
def write_changes(
    file: str | None,
    because: str | None,
    schema: bool,
    workspace_root: str,
    dry_run: bool,
    no_accept: bool,
    at_oid: str | None,
    output_json: bool,
) -> None:
    """Apply FILE's set, add and retire changes as one change set.

    FILE (YAML or JSON) holds {"because": ..., "changes": [...]}, or a bare list
    of changes with --because. Each change is {"op": "set" | "add", "subject",
    "field", "value"} or {"op": "retire", "target"}; a top-level "subject" is
    the Subject of every change that names none. --schema prints the schema.
    """

    if schema:
        _emit_json(_WriteFileV1.model_json_schema())
        return
    if file is None:
        raise click.UsageError("pass FILE, or --schema to see what FILE holds")
    source = Path(file).expanduser()
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if isinstance(payload, list):
        payload = {"changes": payload}
    try:
        parsed = _WriteFileV1.model_validate(payload)
    except ValidationError as exc:
        raise DataValidationError(
            f"{source} is not a valid write file (cruxible write --schema prints it)",
            errors=_model_field_errors(exc),
        ) from exc
    rationale = because or parsed.because
    if rationale is None:
        raise click.UsageError("give the write a reason: --because, or because in FILE")
    request = _write_request(
        WriteRequest,
        {
            "changes": observe_changes(parsed.changes, workspace=Path(workspace_root)),
            "subject": parsed.subject,
            "because": rationale,
            "dry_run": dry_run,
            "accept": "never" if no_accept else "if_allowed",
            "at": at_oid,
        },
        example="cruxible write changes.yaml",
    )
    outcome = _server_call(
        lambda client, instance_id: client.write(instance_id, request=request),
        command_name="cruxible write",
    )
    _finish_write(outcome, output_json=output_json)


@playbill_group.command("get")
@click.argument("ref")
@click.option(
    "--detail",
    type=click.Choice(["summary", "evidence", "why", "history", "proof", "body"]),
    default="summary",
    show_default=True,
    help="How deep to read the one thing REF names.",
)
@click.option(
    "--range",
    "byte_range",
    default=None,
    help="Document body bytes start:end (with --detail body).",
)
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "Accepted git oid, a unique 12+ hex prefix, or a generation number (all digits, "
        "11 or fewer, is always a generation); default head."
    ),
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Revisions per --detail history page, newest first (default 20).",
)
@click.option("--cursor", default=None, help="Continue --detail history from its next_cursor.")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="With --detail body: write the body's exact bytes to this new file (read in ranges).",
)
@json_option
@handle_errors
def get_by_ref(
    ref: str,
    detail: str,
    byte_range: str | None,
    at_oid: str | None,
    evaluation_time: str | None,
    limit: int | None,
    cursor: str | None,
    output_path: str | None,
    output_json: bool,
) -> None:
    """Read one governed thing by reference, values first.

    REF is any reference form: CLM-... (or a unique prefix), kind/id, a predicate,
    ClaimType:/Document:/Procedure:/query:/CaptureContract:<name>, an artifact
    path, a proposal id or prefix, or an operational reference: Line:<name> (or
    the Line identity digest next names), CAP-<12+ hex> or Capture:<digest>,
    ResolutionContract:<name>, Mandate:<name>.
    """

    from cruxible_client.contracts.get_reads import ByteRange, GetRequest

    try:
        request = GetRequest.model_validate(
            {
                "ref": ref,
                "detail": detail,
                "range": None if byte_range is None else ByteRange.parse(byte_range),
                "at": at_oid,
                "evaluation_time": evaluation_time,
                "surface": "cli",
                "limit": limit,
                "cursor": cursor,
            }
        )
    except (ValidationError, ValueError) as exc:
        errors = _model_field_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
        raise click.UsageError(
            "; ".join(errors) + " (example: cruxible get Document:design "
            "--detail body --range 0:4096)"
        ) from None
    if output_path is not None:
        if detail != "body":
            raise click.UsageError("--output writes a Document body; pass --detail body")
        _write_body(request, Path(output_path), whole=byte_range is None)
        return
    result = _server_call(
        lambda client, instance_id: client.get(instance_id, request=request),
        command_name="cruxible get",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _emit_get_text(result)
    if result.next_cursor is not None:
        click.echo(
            f"next: cruxible get {shlex.quote(ref)} --detail history --cursor {result.next_cursor}"
        )


def _write_body(request: Any, destination: Path, *, whole: bool) -> None:
    """Write a Document body's exact bytes to a new file, binary-safe.

    Without a range the whole body is read range by range at the first read's
    coordinate, so a body past the whole-body cap still lands in one file.
    """

    import base64

    from cruxible_client.contracts.get_reads import GET_BODY_RANGE_MAX_BYTES, ByteRange

    def read(window: Any, at: Any) -> Any:
        ranged = request.model_copy(update={"range": window, "at": at})
        result = _server_call(
            lambda client, instance_id: client.get(instance_id, request=ranged),
            command_name="cruxible get",
        )
        assert result.body is not None
        return result

    first = read(
        ByteRange(start=0, end=GET_BODY_RANGE_MAX_BYTES) if whole else request.range,
        request.at,
    )
    body = first.body
    pinned = first.coordinate.git_oid
    chunks: list[bytes] = []

    def chunk(part: Any) -> bytes:
        if part.text is not None:
            return str(part.text).encode("utf-8")
        return base64.b64decode(part.content_base64 or "", validate=True)

    chunks.append(chunk(body))
    end = 0 if body.range is None else body.range.end
    while whole and end < body.size:
        window = ByteRange(start=end, end=min(end + GET_BODY_RANGE_MAX_BYTES, body.size))
        part = read(window, pinned).body
        chunks.append(chunk(part))
        end = part.range.end
    with destination.open("xb") as handle:
        handle.write(b"".join(chunks))
    click.echo(
        f"wrote {sum(len(item) for item in chunks)} bytes to {destination} ({body.body_digest})"
    )


def _get_value_text(
    value: object, *, width: int = GET_CLI_VALUE_WIDTH, evidence_hint: bool = True
) -> str:
    return get_value_display(value, width=width, evidence_hint=evidence_hint).text


def _emit_get_text(result: Any) -> None:
    """Values first, per kind; ``--json`` carries the whole structured result."""

    if result.card is not None:
        card = result.card.model_dump(mode="json")
        nexts = card.pop("next", [])
        if result.kind == "claim":
            click.echo(f"{card['subject']}  {card['predicate']} = {_get_value_text(card['value'])}")
            click.echo(
                f"{card['claim']}  verdict={card['verdict']} status={card['status']} "
                f"revision={card['revision']}"
                + (f" accepted={card['accepted']}" if card.get("accepted") else "")
            )
            for contender in card.get("contenders", []):
                click.echo(
                    f"  contender {contender['claim']} = {_get_value_text(contender['value'])} "
                    f"[{contender['verdict']}]"
                )
        elif result.kind == "subject":
            click.echo(
                f"{card['subject']}  ({card['lifecycle']}, {card['incoming_count']} incoming)"
            )
            rows = card["claims"]
            width = max((len(row["predicate"]) for row in rows), default=0)
            for row in rows:
                flags = f"  [{', '.join(row['flags'])}]" if row["flags"] else ""
                click.echo(
                    f"  {row['predicate'].ljust(width)}  {_get_value_text(row['value'])}{flags}"
                )
        else:
            for key, value in card.items():
                if value in (None, [], {}):
                    continue
                if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                    click.echo(f"{key}:")
                    for item in value:
                        click.echo("  " + "  ".join(str(part) for part in item.values()))
                    continue
                if isinstance(value, dict):
                    value = "  ".join(
                        f"{name}={', '.join(map(str, part)) if isinstance(part, list) else part}"
                        for name, part in value.items()
                    )
                if isinstance(value, list):
                    # Names are never cut: a truncated name is not a usable reference.
                    click.echo(f"{key}: {printable(', '.join(map(str, value)))}")
                    continue
                click.echo(f"{key}: {_get_value_text(value, width=200, evidence_hint=False)}")
        if card.get("flags"):
            click.echo(f"flags: {', '.join(card['flags'])}")
        for step in nexts:
            click.echo(f"next: {step}")
        return
    if result.evidence is not None:
        # Evidence carries the whole value; a summary card cuts a long one.
        whole = result.evidence.model_dump(mode="json")["value"]
        if isinstance(whole, str):
            click.echo(f"value: {printable(whole)}")
        else:
            click.echo(f"value: {printable(json.dumps(whole, ensure_ascii=False))}")
        if result.evidence.content_digest is not None:
            click.echo(f"content_digest: {result.evidence.content_digest}")
        for capture in result.evidence.captures:
            click.echo(
                f"capture {capture.capture}  {capture.contract} v{capture.version}  "
                f"source={capture.source}  observed={capture.observed_at.isoformat()}  "
                f"{capture.role}{' admitted' if capture.admitted else ' not admitted'}"
            )
        for attestation in result.evidence.attestations:
            click.echo(
                f"attestation {attestation.stance} by {attestation.principal} "
                f"at {attestation.at.isoformat()}{'' if attestation.current else ' (not current)'}"
            )
        if result.evidence.rationale:
            click.echo(f"rationale: {printable(result.evidence.rationale)}")
        return
    if result.history is not None:
        for revision in result.history.revisions:
            value = (
                ""
                if revision.value is None
                else f"  = {_get_value_text(revision.value, width=GET_CLI_HISTORY_VALUE_WIDTH)}"
            )
            click.echo(
                f"rev {revision.revision}  seq {revision.sequence} at {revision.git_oid}  "
                f"{revision.accepted}  by {revision.actor or '-'}{value}"
            )
            for step in revision.next:
                click.echo(f"next: {step}")
        return
    if result.body is not None:
        body = result.body
        click.echo(body.text if body.text is not None else body.content_base64 or "", nl=False)
        if body.range is not None and body.range.end < body.size:
            following = min(body.size, 2 * body.range.end - body.range.start)
            click.echo(
                f"\n(bytes {body.range.start}:{body.range.end} of {body.size}; next: "
                f"--range {body.range.end}:{following})",
                err=True,
            )
        return
    _emit_json(result.why if result.why is not None else result.proof)


@playbill_group.group("block")
def block_group() -> None:
    """Maintain local declarations without rendering or replacing authored prose."""


@block_group.command("depublish")
@click.argument("source_id")
@click.argument("block_id")
@change_control_options
@json_option
@handle_errors
def depublish_projection(
    source_id: str, block_id: str, dry_run: bool | None, at: str | None, output_json: bool
) -> None:
    """Release the publication registration that demands one page block.

    The registration is what `next` reads to decide a removed marker is a
    blocking row. Releasing it does not edit the page and does not touch the
    Claim the block was backed by: strip the markers with `block sync --detach`
    or by hand, retire the Claim through the ordinary retirement road, and use
    this when the block itself is not coming back.
    """

    result = _server_call(
        lambda client, instance_id: client.depublish_block(
            instance_id,
            source_id,
            block_id,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible block depublish",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{result.source_id}#{result.block_id}: {result.outcome}")
    click.echo(f"Backing Claim: {result.claim_identity}")
    echo_preview_next(result.outcome, result.coordinate)


@block_group.command("repin")
@click.argument("source_id")
@click.argument("block_id")
@click.option("--claim", "claims", multiple=True, help="Accepted Claim backing identity.")
@click.option("--query", "queries", multiple=True, help="Accepted QueryDefinition identity.")
@click.option(
    "--artifact", "artifacts", multiple=True, help="Subject or ClaimType qualified identity."
)
@click.option("--clear-claims", is_flag=True)
@click.option("--clear-queries", is_flag=True)
@click.option("--clear-artifacts", is_flag=True)
@click.option("--currency-policy", type=click.Choice(["warn", "require_current"]), default=None)
@click.option(
    "--backing",
    "backing_digest",
    default=None,
    help="Exact successor artifact digest selected from an ambiguity refusal.",
)
@click.option(
    "--params",
    "parameters",
    multiple=True,
    help="Canonical JSON object corresponding positionally to each --query.",
)
@click.option("--workspace-root", default=".", show_default=True, type=click.Path(file_okay=False))
@click.option("--evaluation-time", default=None, help="Explicit absolute ISO-8601 instant.")
@click.option("--dry-run", is_flag=True, help="Compute and check the stamp; write nothing.")
@json_option
@handle_errors
def repin_projection(
    source_id: str,
    block_id: str,
    claims: tuple[str, ...],
    queries: tuple[str, ...],
    artifacts: tuple[str, ...],
    clear_claims: bool,
    clear_queries: bool,
    clear_artifacts: bool,
    currency_policy: Literal["warn", "require_current"] | None,
    backing_digest: str | None,
    parameters: tuple[str, ...],
    workspace_root: str,
    evaluation_time: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Refresh one declaration marker without writing its body or closing line."""

    if (clear_claims and claims) or (clear_queries and queries) or (clear_artifacts and artifacts):
        raise click.ClickException("a backing category cannot be cleared and replaced together")
    if backing_digest is not None and (
        claims
        or queries
        or parameters
        or artifacts
        or clear_claims
        or clear_queries
        or clear_artifacts
    ):
        raise click.ClickException(
            "--backing cannot be combined with --claim, --query, or --params"
        )
    if parameters and len(parameters) != len(queries):
        raise click.ClickException("--params must appear once for each --query or not at all")
    resolved: list[tuple[str, Mapping[str, object]]] = []
    for index, name in enumerate(queries):
        if parameters:
            try:
                payload = json.loads(parameters[index])
                if (
                    not isinstance(payload, dict)
                    or canonical_bytes(payload).decode() != parameters[index]
                ):
                    raise ValueError("expected one canonical JSON object")
            except (CanonicalEncodingError, ValueError, TypeError) as exc:
                raise click.ClickException(
                    f"--params for query {name!r} is not canonical JSON"
                ) from exc
        else:
            payload = {}
        resolved.append((name, payload))
    try:
        instant = (
            datetime.now(UTC)
            if evaluation_time is None
            else datetime.fromisoformat(evaluation_time.replace("Z", "+00:00"))
        )
    except ValueError as exc:
        raise click.ClickException(
            "--evaluation-time must be an absolute ISO-8601 instant"
        ) from exc
    stamp = _server_call(
        lambda client, instance_id: repin_projection_block(
            client,
            instance_id,
            workspace=workspace_root,
            source_id=source_id,
            block_id=block_id,
            claims=claims if claims or clear_claims else None,
            queries=resolved if queries or clear_queries else None,
            artifacts=tuple(parse_artifact_identity(x) for x in artifacts)
            if artifacts or clear_artifacts
            else None,
            currency_policy=currency_policy,
            backing_digest=backing_digest,
            evaluation_time=instant,
            dry_run=dry_run,
        ),
        command_name="cruxible block repin",
    )
    if output_json:
        _emit_json(stamp.model_dump(mode="json"))
        return
    if dry_run:
        click.echo(
            f"Would repin {source_id}#{block_id} at generation {stamp.declared_generation}; "
            "nothing was written."
        )
        return
    click.echo(f"Repinned {source_id}#{block_id} at generation {stamp.declared_generation}.")


@block_group.command("sync")
@click.argument("paths", nargs=-1, type=click.Path(dir_okay=False))
@click.option("--all", "all_sources", is_flag=True, help="Synchronize every catalog source.")
@click.option(
    "--check",
    is_flag=True,
    help="Check without applying requested detach edits.",
)
@click.option(
    "--detach",
    "detach_paths",
    multiple=True,
    type=click.Path(dir_okay=False),
    help="Strip markers from retired blocks while preserving their current body.",
)
@click.option("--workspace-root", default=".", show_default=True, type=click.Path(file_okay=False))
@json_option
@handle_errors
def sync_projection(
    paths: tuple[str, ...],
    all_sources: bool,
    check: bool,
    detach_paths: tuple[str, ...],
    workspace_root: str,
    output_json: bool,
) -> None:
    """Check dependencies and report drift under each block's currency policy."""

    result = _server_call(
        lambda client, instance_id: sync_projection_blocks(
            client,
            instance_id,
            workspace=workspace_root,
            paths=paths,
            all_sources=all_sources,
            check=check,
            detach_paths=(*detach_paths,),
        ),
        command_name="cruxible block sync",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        for item in result.items:
            target = item.path
            if item.block_id is not None:
                target += f"#{item.block_id}"
            suffix = "" if item.reason is None else f":{item.reason}"
            click.echo(f"{target}: {item.outcome}{suffix}")
            if item.repair is not None:
                click.echo(f"  repair: {render_served_repair(item.repair)}")
    # Warn findings stay advisory; explicit strict blocks gate this check.
    if (check and result.would_change) or result.has_refusals:
        raise click.exceptions.Exit(1)


def _echo_list_continuation(next_cursor: str | None) -> None:
    if next_cursor is not None:
        click.echo(f"Truncated. Next: --cursor {next_cursor}")


@playbill_group.group("compiler")
def compiler_group() -> None:
    """Explicit, reviewed changes to the instance's accepted compiler."""


@compiler_group.command("upgrade")
@click.option(
    "--to", "target_digest", required=True, help="Exact installed target compiler digest."
)
@click.option("--name", "proposal_name", required=True)
@change_control_options
@json_option
@handle_errors
def propose_compiler_upgrade(
    target_digest: str,
    proposal_name: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    """Create an upgrade proposal at the selected accepted head; does not activate it."""
    from cruxible_client.contracts.types import CompilerCoordinate

    def call(client: CruxibleClient, instance_id: str) -> contracts.ProposalInspection:
        head = client.head(instance_id)
        return client.propose_compiler_upgrade(
            instance_id,
            target=CompilerCoordinate(rule_digest=target_digest),
            base=contracts.AcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        )

    result = _server_call(call, command_name="cruxible compiler upgrade")
    _emit_json(result.model_dump(mode="json"))


class _QueryCommand(click.Command):
    """``cruxible query [KIND] ...``: KIND, when given, comes before every option."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit()
        ctx.meta["playbill_query_args"] = list(args)
        if not args[0].startswith("-"):
            ctx.meta["playbill_query_kind"] = args[0]
            args = args[1:]
        return super().parse_args(ctx, args)


def _split_fields(values: Sequence[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def _query_param_value(raw: str) -> object:
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw
    # JSON null binds an optional parameter explicitly (omitting it takes the
    # default); pass '"null"' for the four-letter string.
    return decoded if decoded is None or isinstance(decoded, str | int | bool) else raw


def _without_cursor(args: Sequence[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for item in args:
        if skip:
            skip = False
            continue
        if item == "--cursor":
            skip = True
            continue
        if item.startswith("--cursor="):
            continue
        kept.append(item)
    return kept


def _validation_problems(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'request'}: {error['msg']}"
        for error in exc.errors()
    )


_FOLLOW_OPTIONS = {"--follow": "forward", "--follow-in": "reverse"}


def _follow_order(
    raw: Sequence[str], forward: Sequence[str], reverse: Sequence[str]
) -> list[tuple[str, str]]:
    """Every follow as (option, spec), in command-line order.

    The order is read back from the raw arguments and used only when it names
    exactly the specs click parsed; otherwise forward follows come first.
    """

    parsed = [
        *(("--follow", item) for item in forward),
        *(("--follow-in", item) for item in reverse),
    ]
    seen: list[tuple[str, str]] = []
    index = 0
    while index < len(raw):
        token = raw[index]
        name, equals, value = token.partition("=")
        if name in _FOLLOW_OPTIONS and equals:
            seen.append((name, value))
        elif token in _FOLLOW_OPTIONS and index + 1 < len(raw):
            seen.append((token, raw[index + 1]))
            index += 1
        index += 1
    if all(
        [item for item in seen if item[0] == option] == [i for i in parsed if i[0] == option]
        for option in _FOLLOW_OPTIONS
    ):
        return seen
    return parsed


def _follow_entry(spec: str, option: str) -> dict[str, str]:
    field, _, alias = spec.partition(":")
    if not field or not alias:
        example = "dev.batch.delivers:batch" if option == "--follow-in" else "closed_by:batch"
        raise click.BadParameter(
            f"{spec!r} is not field:alias, for example {example}", param_hint=option
        )
    entry = {"field": field, "as": alias}
    if _FOLLOW_OPTIONS[option] == "reverse":
        entry["direction"] = "reverse"
    return entry


@playbill_group.command("query", cls=_QueryCommand, context_settings={"allow_extra_args": True})
@click.option(
    "--where",
    "where_expressions",
    multiple=True,
    help=f"Filter, repeatable (all-of): {WHERE_SYNTAX}.",
)
@click.option("--contains", default=None, help="Case-insensitive text in any live Claim value.")
@click.option("--select", "select_fields", multiple=True, help="Columns: a,b (repeatable).")
@click.option(
    "--follow",
    "follow_specs",
    multiple=True,
    help="Follow a relation forward (repeatable): field:alias, a predicate of KIND.",
)
@click.option(
    "--follow-in",
    "follow_in_specs",
    multiple=True,
    help=(
        "Follow a relation backwards (repeatable): field:alias, another kind's predicate "
        "that points at KIND, e.g. dev.batch.delivers:batch."
    ),
)
@click.option("--order-by", "order_fields", multiple=True, help="Order: f or -f (repeatable).")
@click.option("--limit", type=click.IntRange(1, contracts.QUERY_MAX_LIMIT), default=None)
@click.option("--cursor", default=None, help="Continue a truncated page.")
@click.option(
    "--spec",
    "spec_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="A QueryDefinitionSpec file (JSON or YAML).",
)
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(["live", "overturned", "refused", "retired"]),
    help="Which Claims cells show (repeatable): live (default), overturned, refused, retired.",
)
@click.option("--claims", "with_claims", is_flag=True, help="Also show each cell's Claims.")
@click.option("--name", "query_name", default=None, help="Run an accepted named query.")
@click.option("--param", "param_pairs", multiple=True, help="Named query parameter k=v.")
@click.option(
    "--budgets",
    "budgets_json",
    default=None,
    help='Named query budgets as JSON, e.g. \'{"max_results": 100, "max_traversal_depth": 0}\'.',
)
@click.option(
    "--receipt",
    type=click.Choice(["compact", "full"]),
    default="compact",
    help="full adds a named query's replay receipt (Claims read, paths, verdict).",
)
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "Read at this accepted git oid (or a unique 12+ hex prefix), or a generation "
        "number (all digits, 11 or fewer, is always a generation)."
    ),
)
@click.option("--evaluation-time", default=None, help="ISO-8601 instant; default now.")
@json_option
@click.pass_context
@handle_errors
def query_group(
    ctx: click.Context,
    where_expressions: tuple[str, ...],
    contains: str | None,
    select_fields: tuple[str, ...],
    follow_specs: tuple[str, ...],
    follow_in_specs: tuple[str, ...],
    order_fields: tuple[str, ...],
    limit: int | None,
    cursor: str | None,
    spec_path: str | None,
    statuses: tuple[str, ...],
    with_claims: bool,
    query_name: str | None,
    param_pairs: tuple[str, ...],
    budgets_json: str | None,
    receipt: str,
    at_oid: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Query accepted state: cruxible query [KIND] [--where 'f=v']...

    This answers one query and prints its values as a table with flags, then
    the next command when the page is truncated. KIND is a Subject kind, or
    ClaimType / Procedure for definitions; --name runs an accepted named query
    (orient --section queries lists them), --spec a full definition.
    """

    if ctx.args:
        raise click.UsageError(
            f"{ctx.args[0]!r} is not an option; put KIND first: cruxible query KIND [--where ...]",
            ctx=ctx,
        )
    try:
        where = [parse_where(expression) for expression in where_expressions]
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--where") from exc
    follow = [
        _follow_entry(spec, option)
        for option, spec in _follow_order(
            ctx.meta.get("playbill_query_args", []), follow_specs, follow_in_specs
        )
    ]
    params: dict[str, object] | None = None
    if param_pairs:
        params = {}
        for pair in param_pairs:
            key, sep, raw = pair.partition("=")
            if not sep or not key:
                raise click.BadParameter(f"{pair!r} is not k=v", param_hint="--param")
            params[key] = _query_param_value(raw)
    spec: object = None
    if spec_path is not None:
        from cruxible_client.contracts.query.definitions import QueryDefinitionSpec

        try:
            spec = QueryDefinitionSpec.model_validate(_read_mapping(spec_path))
        except ValidationError as exc:
            raise click.ClickException(
                f"{spec_path} is not a QueryDefinitionSpec: {_validation_problems(exc)}"
            ) from exc
    fields: dict[str, object] = {
        "kind": ctx.meta.get("playbill_query_kind"),
        "where": [item.model_dump(mode="json", by_alias=True) for item in where],
        "contains": contains,
        "select": _split_fields(select_fields),
        "follow": follow,
        "order_by": _split_fields(order_fields),
        "cursor": cursor,
        "spec": spec,
        "name": query_name,
        "params": params,
        "claims": with_claims,
        "receipt": receipt,
        "at": at_oid,
        "evaluation_time": None if evaluation_time is None else parse_datetime(evaluation_time),
    }
    if limit is not None:
        fields["limit"] = limit
    if statuses:
        fields["status"] = list(dict.fromkeys(statuses))
    if budgets_json is not None:
        try:
            fields["budgets"] = json.loads(budgets_json)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--budgets") from exc
    try:
        request = contracts.QueryRequest.model_validate(fields)
    except ValidationError as exc:
        raise click.ClickException(f"invalid query: {_validation_problems(exc)}") from exc
    result = _server_call(
        lambda client, instance_id: client.query(instance_id, request=request),
        command_name="cruxible query",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(render_query_table(result))
    for row in result.rows if with_claims else ():
        for column, entries in (row.get("claims") or {}).items():
            for entry in entries:
                click.echo(
                    f"claim {row.get('subject', '')} {column}: {entry['claim']} "
                    f"{entry['status']} {entry['verdict']} {entry['role']}"
                )
    if result.truncated and result.next_cursor is not None:
        again = _without_cursor(ctx.meta.get("playbill_query_args", []))
        click.echo(
            "next: cruxible query "
            + " ".join(shlex.quote(item) for item in again)
            + f" --cursor {result.next_cursor}"
        )


@playbill_group.group("procedure")
def procedure_group() -> None:
    """Inspect, bind, run, and measure accepted Procedures."""


@procedure_group.command("readiness")
@click.argument("name")
@click.option("--evaluation-time", required=True, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def procedure_readiness(name: str, evaluation_time: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.procedure_readiness(
            instance_id,
            name,
            evaluation_time=evaluation_time,
        ),
        command_name="cruxible procedure readiness",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{name}: {result.state}")
    click.echo(f"Next: {result.next_operation['kind']}")
    for slot in result.required_slots:
        click.echo(f"Required slot: {slot}")
    for node in result.unsupported_nodes:
        click.echo(f"Unsupported node: {node['node_id']} ({node['kind']})")


@procedure_group.command("bind")
@click.argument("name")
@click.argument("request_file", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def bind_procedure(name: str, request_file: str, output_json: bool) -> None:
    request = _read_model(request_file, ProcedureBindRequest)
    result = _server_call(
        lambda client, instance_id: client.bind_procedure(
            instance_id,
            name,
            bindings=[item.model_dump(mode="json") for item in request.bindings],
        ),
        command_name="cruxible procedure bind",
    )
    _emit_json(result.model_dump(mode="json"))


#: Repair arguments a CLI leaf takes as its positional operand.
_POSITIONAL_REPAIR_ARGUMENTS = frozenset(
    {"line", "name", "claim_id", "proposal_id", "prediction_id", "run_id"}
)


def _cli_repair(repair: Any) -> str:
    """Render a served repair as the CLI command that performs it."""

    operation = normalize_code(repair.operation) if isinstance(repair, RepairOperation) else ""
    if not isinstance(repair, RepairOperation) or not operation.startswith("cruxible."):
        return render_served_repair(repair)
    parts = ["cruxible", *operation.removeprefix("cruxible.").split(".")]
    for key, value in repair.arguments.items():
        if key in _POSITIONAL_REPAIR_ARGUMENTS:
            parts.append(shlex.quote(str(value)))
        elif value is True:
            parts.append("--" + key.replace("_", "-"))
        elif value not in (None, False):
            parts.extend(["--" + key.replace("_", "-"), shlex.quote(str(value))])
    return " ".join(parts)


def _echo_run_outcome(result: contracts.ProcedureRunState, label: str) -> None:
    """Lead with the answer: the result on success, the code and repair on refusal."""

    click.echo(f"{label}: {result.status}")
    if result.status == "succeeded" and result.result is not None:
        click.echo("Result: " + json.dumps(result.result, sort_keys=True))
    terminal = result.terminal
    code = getattr(terminal, "code", None)
    if code is not None:
        click.echo(f"Refused: {code}: {getattr(terminal, 'message', '')}")
        details = getattr(terminal, "details", None)
        if isinstance(details, dict) and details.get("field_path"):
            click.echo(f"Field: {details['field_path']}")
        repair = getattr(terminal, "repair", None)
        if repair is not None:
            click.echo(f"Repair: {_cli_repair(repair)}")
        elif isinstance(details, dict) and isinstance(details.get("repair"), str):
            click.echo(f"Repair: {details['repair']}")
    elif isinstance(terminal, ProcedureHaltTerminal) and terminal.reason:
        click.echo(f"Halted at {terminal.node_id}: {terminal.reason}")
    click.echo(f"Next: {result.next_operation['kind']}")


def _echo_source_observations(result: contracts.ProcedureRunState) -> None:
    """Print what each admitted Source occurrence really observed.

    A run with no Source occurrence prints nothing extra. `--json` already
    carries the whole receipt; this is the one-glance version of it.
    """

    for observation in result.source_observations:
        receipt = observation.source_read_receipt
        where = observation.input_name or observation.occurrence_path
        if receipt is not None:
            click.echo(
                f"Read {where}: {receipt.relative_path} "
                f"({receipt.byte_length} bytes, {receipt.bytes_digest})"
            )
        if observation.capture_digest is not None:
            click.echo(f"Capture {where}: {observation.capture_digest}")
    _echo_terminal_egress(result)


def _echo_terminal_egress(result: contracts.ProcedureRunState) -> None:
    """Print what each terminal of the run did, and the handles a manager needs.

    A delivered `propose_change_set` prints the proposal id and the exact
    candidate digest, which are the two arguments the existing proposal
    verbs take; a settled `settle_change_set` prints the generation it
    accepted, and one that fell back prints its proposal and why. A capped
    terminal names the authority it needed and the authority the run had; a
    refused one prints the code the run refused with.
    """

    for egress in result.terminal_egress:
        if egress.settle_outcome == "settled":
            click.echo(
                f"Settled {egress.node_id}: accepted {egress.accepted_git_oid} "
                f"(proposal {egress.proposal_id})"
            )
        elif egress.verdict == "delivered" and egress.proposal_id is not None:
            fallback = (
                f" (settle fell back: {egress.fallback_reason})"
                if egress.settle_outcome == "proposed"
                else ""
            )
            click.echo(
                f"Proposal {egress.node_id}: {egress.proposal_id} "
                f"candidate {egress.candidate_digest}{fallback}"
            )
            for child in egress.children:
                if child.path is not None:
                    click.echo(f"  {child.path}")
        elif egress.verdict == "delivered":
            click.echo(f"Terminal {egress.node_id}: {egress.kind} delivered")
        elif egress.verdict == "refused_effective_authority":
            click.echo(
                f"Terminal {egress.node_id}: {egress.kind} needs {egress.required_authority}; "
                f"the {egress.limiting_term} term allowed {egress.effective_authority}"
            )
        else:
            code = egress.refusal_code or egress.verdict
            click.echo(f"Terminal {egress.node_id}: {egress.kind} {egress.verdict} ({code})")


@procedure_group.command("run")
@click.argument("name")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@click.option(
    "--at",
    "at_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="AcceptedCoordinate JSON/YAML file; its presence selects replay lane.",
)
@click.option(
    "--resolution-contract",
    "contract_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Exact accepted ResolutionContract reference JSON/YAML.",
)
@click.option(
    "--trigger-event",
    "event_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Exact retained Capture event reference JSON/YAML.",
)
@json_option
@handle_errors
def run_procedure(
    name: str,
    input_file: str,
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
    contract_file: str | None,
    event_file: str | None,
) -> None:
    resolution_contract = (
        None if contract_file is None else _read_model(contract_file, ResolutionContractReference)
    )
    trigger_event = None if event_file is None else _read_model(event_file, TriggerEventReference)
    at = None if at_file is None else _read_model(at_file, AcceptedCoordinate)
    result = _server_call(
        lambda client, instance_id: client.run_procedure(
            instance_id,
            name,
            evaluation_time=evaluation_time,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            at=None if at is None else at.model_dump(mode="json"),
            input=_read_mapping(input_file),
        ),
        command_name="cruxible procedure run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_run_outcome(result, result.run_id or name)
    if result.receipt_digest is not None:
        click.echo(f"Receipt: {result.receipt_digest}")
    _echo_source_observations(result)


@procedure_group.command("status")
@click.argument("run_id")
@json_option
@handle_errors
def procedure_run_status(run_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_procedure_run(instance_id, run_id),
        command_name="cruxible procedure status",
    )
    _emit_json(result.model_dump(mode="json"))


@procedure_group.command("measure")
@click.argument("name")
@click.option("--run-id", default=None, help="Credit this finalized run's exact grain.")
@click.option(
    "--measurement",
    "measurements",
    multiple=True,
    help="Evaluate only these declared measurements (default: every declaration).",
)
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 OBSERVATION instant (default: now).",
)
@click.option(
    "--at",
    "at_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="AcceptedCoordinate JSON/YAML file naming the OBSERVATION coordinate.",
)
@json_option
@handle_errors
def procedure_measure(
    name: str,
    run_id: str | None,
    measurements: tuple[str, ...],
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
) -> None:
    """Evaluate due measurements from real evidence; retry replays, never duplicates."""

    at = None if at_file is None else _read_model(at_file, AcceptedCoordinate)
    request = contracts.ProcedureMeasureRequest(
        run_id=run_id,
        measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
        evaluation_time=(
            None
            if evaluation_time is None
            else datetime.fromisoformat(evaluation_time.replace("Z", "+00:00"))
        ),
        at=at,
    )
    result = _server_call(
        lambda client, instance_id: client.measure_procedure(instance_id, name, request=request),
        command_name="cruxible procedure measure",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for row in result.rows:
        line = f"{row.measurement_name}: {row.status}"
        if row.resolution is not None:
            line += f" ({row.resolution.verdict}, {row.resolution.resolution_id})"
        if row.reading_status != "not_requested":
            line += f" reading={row.reading_status}"
            if row.reading is not None:
                line += f" {row.reading.reading_id}"
        click.echo(line)
        if row.detail:
            click.echo(f"  {row.detail}")


@procedure_group.command("readings")
@click.argument("name")
@click.option("--run-id", default=None, help="Only readings crediting this run.")
@click.option("--measurement", "measurements", multiple=True, help="Only these measurements.")
@click.option("--limit", default=50, show_default=True, type=click.IntRange(1, 200))
@click.option("--cursor", default=None, help="Continue a previous page.")
@json_option
@handle_errors
def procedure_readings(
    name: str,
    run_id: str | None,
    measurements: tuple[str, ...],
    limit: int,
    cursor: str | None,
    output_json: bool,
) -> None:
    """Inspect measurement standing and retained readings. Read-only."""

    request = contracts.ProcedureReadingsRequest(
        run_id=run_id,
        measurement_names=tuple(sorted(set(measurements), key=lambda item: item.encode())),
        limit=limit,
        cursor=cursor,
    )
    result = _server_call(
        lambda client, instance_id: client.list_procedure_readings(
            instance_id, name, request=request
        ),
        command_name="cruxible procedure readings",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for contract in result.contracts:
        line = f"{contract.measurement_name}: {contract.status} readings={contract.reading_count}"
        if contract.resolution is not None:
            line += f" ({contract.resolution.verdict}, {contract.resolution.resolution_id})"
        click.echo(line)
    for reading in result.readings:
        click.echo(
            f"{reading.reading_id} {reading.measurement_name} {reading.subject_grain} "
            f"{reading.verdict} run={reading.run_id}"
        )
    if result.cursor is not None:
        click.echo(f"Next: --cursor {result.cursor}")


@playbill_group.group("line")
def line_group() -> None:
    """Trigger accepted Lines."""


@line_group.command("check")
@click.argument("line")
@click.option("--since", default=None, help="Inclusive eligibility timestamp.")
@click.option("--until", default=None, help="Exclusive eligibility timestamp.")
@click.option("--limit", default=100, type=click.IntRange(1, 256))
@click.option("--cursor", default=None)
@json_option
@handle_errors
def check_line(
    line: str,
    since: str | None,
    until: str | None,
    limit: int,
    cursor: str | None,
    output_json: bool,
) -> None:
    from cruxible_client.contracts.line_dispatch import LineTriggerCheckRequest

    request = LineTriggerCheckRequest.model_validate(
        dict(since=since, until=until, limit=limit, cursor=cursor)
    )
    result = _server_call(
        lambda client, instance_id: client.check_line(instance_id, line, request=request),
        command_name="cruxible line check",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"{result.line}: {result.status} ({len(result.occurrences)} occurrences)")
        if result.detail:
            click.echo(result.detail)
        if result.cursor:
            click.echo(
                f"Next cursor: {result.cursor}; retain --until {result.checked_until.isoformat()}"
            )


def _echo_line_arm(result: Any) -> None:
    state = "armed" if result.state == "armed" else f"stopped ({result.stop_reason})"
    unchanged = {
        "already_armed": "already armed",
        "already_disarmed": "already disarmed",
        "would_arm": "preview: would arm",
        "would_rearm": "preview: would rearm",
        "would_disarm": "preview: would disarm",
    }
    note = unchanged.get(result.outcome or "")
    click.echo(f"{result.line}: {state}" + (f" ({note}; nothing changed)" if note else ""))
    click.echo(f"Armed by: {result.armed_by.label} at {result.armed_at.isoformat()}")
    click.echo(f"Matched through: {result.evaluated_until.isoformat()}")
    click.echo(
        f"Pending: {result.pending_automatic} automatic, "
        f"{result.pending_explicit} awaiting explicit dispatch"
    )
    if result.detail:
        click.echo(result.detail)
    echo_preview_next(result.outcome or "", result.coordinate)


@line_group.command("arm")
@click.argument("line")
@change_control_options
@json_option
@handle_errors
def arm_line(line: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Admit what this Line matches from now on, under your credential."""

    result = _server_call(
        lambda client, instance_id: client.arm_line(instance_id, line, dry_run=dry_run, at=at),
        command_name="cruxible line arm",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_arm(result)


@line_group.command("disarm")
@click.argument("line")
@change_control_options
@json_option
@handle_errors
def disarm_line(line: str, dry_run: bool | None, at: str | None, output_json: bool) -> None:
    """Stop admitting work automatically; admitted runs keep going."""

    result = _server_call(
        lambda client, instance_id: client.disarm_line(instance_id, line, dry_run=dry_run, at=at),
        command_name="cruxible line disarm",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_arm(result)


@line_group.command("status")
@click.argument("line")
@json_option
@handle_errors
def line_status(line: str, output_json: bool) -> None:
    """Show whether the Line is armed and why an arm stopped."""

    result = _server_call(
        lambda client, instance_id: client.line_status(instance_id, line),
        command_name="cruxible line status",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        _echo_line_arm(result)


@line_group.command("evaluate")
@click.argument("line")
@click.option("--since", required=True)
@click.option("--until", required=True)
@click.option("--limit", default=100, type=click.IntRange(1, 256))
@click.option("--cursor", default=None)
@json_option
@handle_errors
def evaluate_line(
    line: str, since: str, until: str, limit: int, cursor: str | None, output_json: bool
) -> None:
    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest

    request = LineEvaluateRequest.model_validate(
        dict(since=since, until=until, limit=limit, cursor=cursor)
    )
    result = _server_call(
        lambda client, instance_id: client.evaluate_line(instance_id, line, request=request),
        command_name="cruxible line evaluate",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"{result.line}: {result.status} ({len(result.occurrences)} occurrences)")
        if result.detail:
            click.echo(result.detail)
        if result.cursor:
            click.echo(
                f"Next cursor: {result.cursor}; retain --until {result.checked_until.isoformat()}"
            )


@line_group.command("dispatch")
@click.option(
    "--retry",
    is_flag=True,
    help="Explicitly retry --occurrence-id against the current Line in the same epoch.",
)
@click.argument("line")
@click.option("--occurrence-id", default=None)
@click.option("--limit", default=1, type=click.IntRange(1, 100))
@json_option
@handle_errors
def dispatch_line(
    line: str, occurrence_id: str | None, limit: int, retry: bool, output_json: bool
) -> None:
    from cruxible_client.contracts.line_dispatch import LineDispatchRequest

    result = _server_call(
        lambda client, instance_id: client.dispatch_line(
            instance_id,
            line,
            request=LineDispatchRequest(occurrence_id=occurrence_id, limit=limit, retry=retry),
        ),
        command_name="cruxible line dispatch",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    elif not result.items:
        click.echo("No pending occurrences for this Line epoch.")
    else:
        for item in result.items:
            click.echo(
                f"{item.occurrence_id}: {item.status}"
                + (f" run={item.run_id}" if item.run_id else "")
            )
            if item.detail:
                click.echo(item.detail)


@line_group.command("run")
@click.argument("line")
@click.option(
    "--trigger",
    default=None,
    help="The Trigger this occurrence fires on; omit for a Line no Trigger aims at.",
)
@click.option("--occurrence-id", default=None, help="Assert the daemon-derived occurrence id.")
@click.option("--evaluation-time", required=True, help="Explicit ISO-8601 evaluation time.")
@click.option(
    "--resolution-contract",
    "contract_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Exact accepted ResolutionContract reference JSON/YAML.",
)
@click.option(
    "--trigger-event",
    "event_file",
    type=click.Path(exists=True, dir_okay=False),
    help="Exact retained Capture event reference JSON/YAML.",
)
@json_option
@handle_errors
def run_line(
    line: str,
    trigger: str | None,
    occurrence_id: str | None,
    evaluation_time: str | None,
    output_json: bool,
    contract_file: str | None,
    event_file: str | None,
) -> None:
    resolution_contract = (
        None if contract_file is None else _read_model(contract_file, ResolutionContractReference)
    )
    trigger_event = None if event_file is None else _read_model(event_file, TriggerEventReference)
    request = LineRunRequest.model_validate(
        {
            "line": line,
            "trigger": trigger,
            "occurrence_id": occurrence_id,
            "evaluation_time": evaluation_time,
            "resolution_contract": resolution_contract,
            "trigger_event": trigger_event,
        }
    )
    result = _server_call(
        lambda client, instance_id: client.run_line(
            instance_id,
            line,
            trigger=request.trigger,
            occurrence_id=request.occurrence_id,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            evaluation_time=(
                None if request.evaluation_time is None else request.evaluation_time.isoformat()
            ),
        ),
        command_name="cruxible line run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_run_outcome(result, result.run_id or line)
    _echo_source_observations(result)


@playbill_group.command("next")
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 evaluation time; otherwise the client stamps the current UTC time.",
)
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--expiring-within",
    callback=_parse_expiring_duration,
    default="P7D",
    show_default=True,
    help="ISO-8601 lead window for evidence-expiration warnings (for example P7D or PT12H).",
)
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace whose configured floor is observed locally.",
)
@click.option(
    "--delta",
    "since_result_digest",
    default=None,
    help="A prior result_digest; return only the rows new since that queue.",
)
@click.option(
    "--limit",
    default=contracts.NEXT_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.NEXT_MAX_LIMIT),
    help="Rows per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same queue.")
@brief_option
@json_option
@handle_errors
def next_work(
    evaluation_time: str | None,
    access_profile_path: str | None,
    expiring_within: int,
    workspace_root: str,
    since_result_digest: str | None,
    limit: int,
    cursor: str | None,
    output_brief: bool,
    output_json: bool,
) -> None:
    stamped_evaluation_time = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if evaluation_time is None
        else evaluation_time
    )
    profile = (
        CoverageAccessProfile(
            profile_id="cli-next",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )
    workspace_observation = observe_next_workspace(Path(workspace_root))

    def _next_at_scanned_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.NextResult:
        observed, coordinate = observe_next_workspace_with_coverage(
            client,
            instance_id,
            Path(workspace_root),
            observation=workspace_observation,
            access_profile=profile,
        )
        return client.next(
            instance_id,
            evaluation_time=stamped_evaluation_time,
            access_profile=profile,
            at=coordinate,
            expiring_within={"microseconds": expiring_within},
            workspace_observation=observed,
            since_result_digest=since_result_digest,
            limit=limit,
            cursor=cursor,
        )

    result = _server_call(
        _next_at_scanned_coordinate,
        command_name="cruxible next",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    _echo_next_status(result.status)
    if not result.items:
        click.echo(
            "No changes since the requested queue digest."
            if result.delta_since is not None
            else "No repair work in the observed domains."
        )
    removed_ids = frozenset(result.removed_item_ids)
    for item in result.items:
        change = (
            "removed  "
            if result.delta_since is not None and item.item_id in removed_ids
            else "added  "
            if result.delta_since is not None
            else ""
        )
        row = f"{change}{item.severity}  {item.reason}  {item.subject_identity}"
        if item.repair is None:
            needs = _next_requirement_hint(item.repair_requires)
            click.echo(row + ("" if output_brief else f"  repair withheld: {needs}"))
            if not output_brief:
                for finding in item.findings:
                    click.echo(
                        f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}"
                    )
            continue
        if output_brief:
            click.echo(row + (f"  next={item.repair.command}" if item.repair.command else ""))
            continue
        click.echo(f"{row}  next={item.repair.operation}")
        click.echo(f"  repair: {_next_repair_hint(item.repair)}")
        for finding in item.findings:
            click.echo(f"  also: {finding.severity}  {finding.reason}  {finding.subject_identity}")
    if result.unobserved_domains and not output_brief:
        click.echo("Unobserved: " + ", ".join(result.unobserved_domains))
    if result.next_cursor is not None:
        click.echo(
            f"Showing {len(result.items)} of {result.total_items} rows. "
            f"Next: --cursor {result.next_cursor}"
        )


def _next_requirement_hint(requires: contracts.NextRepairRequirement | None) -> str:
    """What running a withheld repair needs, in one phrase."""

    if requires is None:
        return "this caller cannot run it"
    needs = []
    if "tier" in requires.because:
        needs.append(f"the {requires.tier} tier")
    if "profile" in requires.because:
        needs.append(f"the {requires.profile} MCP tool profile")
    refusal = requires.authoring_refusal
    if "authoring" in requires.because and refusal is not None:
        # The identity repair comes first: no tier helps a caller that cannot author.
        needs.insert(0, f"a caller that can author ({refusal.code}: {refusal.detail})")
    return f"{requires.tool} needs " + " and ".join(needs)


def _next_repair_hint(repair: contracts.NextRepair) -> str:
    """The runnable command, or else the operation and the change it must make."""

    if repair.command is not None:
        return repair.command
    operation = "hand edit" if repair.operation == "hand_edit" else repair.operation
    return f"{operation} {repair.target}: {repair.required_change}"


#: Facet states the status header shows; healthy and unobserved facets stay quiet.
_NEXT_STATUS_ATTENTION = {
    "instance": {"decommissioned"},
    "floor": {"missing", "stale"},
    "ledger_mirror": {"behind", "never_published"},
    "provider_lane": {"unavailable"},
    "procedure_catalog": {"missing"},
    "compiler": {"upgrade_available"},
    "line_dispatch": {"due"},
    "consumers": {"lagging"},
    "triggers": {"unscheduled"},
}


def _echo_next_status(status: contracts.NextStatus) -> None:
    """Print the environment facets that need attention above the work rows."""

    if status.blocking:
        click.echo("BLOCKING: this instance refuses every write.")
    for facet, states in _NEXT_STATUS_ATTENTION.items():
        health: contracts.NextHealth = getattr(status, facet)
        if health.state not in states:
            continue
        repair = health.repair
        hint = None if repair is None else repair.command or repair.required_change
        if health.repair_hidden:
            hint = f"(repair withheld: {_next_requirement_hint(health.repair_requires)})"
        label = facet.replace("_", " ")
        click.echo(f"Status: {label} {health.state}" + (f"  next={hint}" if hint else ""))
    arms = (
        status.consumers.detail.get("line_arms")
        if isinstance(status.consumers.detail, dict)
        else None
    )
    if isinstance(arms, dict) and (arms.get("stalled") or arms.get("stopped")):
        click.echo(
            f"Status: line arms stalled={arms.get('stalled', 0)} stopped={arms.get('stopped', 0)}"
            "  next=cruxible orient --section lines"
        )


@playbill_group.group("curation")
def curation_group() -> None:
    """Inspect mechanically detected ontology-maintenance patterns."""


@curation_group.command("list")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace scanned explicitly for declared-block observations.",
)
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--limit",
    default=contracts.CURATION_LIST_DEFAULT_LIMIT,
    show_default=True,
    type=click.IntRange(1, contracts.CURATION_LIST_MAX_LIMIT),
    help="Queue items per page.",
)
@click.option("--cursor", default=None, help="Continue a previous page of the same queue.")
@json_option
@handle_errors
def curation_list(
    workspace_root: str,
    access_profile_path: str | None,
    limit: int,
    cursor: str | None,
    output_json: bool,
) -> None:
    observation = observe_next_workspace(Path(workspace_root))
    profile = (
        CoverageAccessProfile(
            profile_id="cli-curation",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )

    def _curation_at_scanned_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.CurationListResult:
        observed, _coordinate = observe_next_workspace_with_coverage(
            client,
            instance_id,
            Path(workspace_root),
            observation=observation,
            access_profile=profile,
        )
        return client.list_curation(
            instance_id,
            evaluation_time=datetime.now(UTC).isoformat(),
            access_profile=profile,
            workspace_observation=observed,
            limit=limit,
            cursor=cursor,
        )

    result = _server_call(
        _curation_at_scanned_coordinate,
        command_name="cruxible curation list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        f"Curation queue at generation {result.generation}: {len(result.items)} item(s); "
        f"observed {result.observation_coverage['observed_block_count']} declared block(s)."
    )
    _echo_list_continuation(result.next_cursor)


@curation_group.command("overrule")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@change_control_options
@json_option
@handle_errors
def curation_overrule(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.overrule_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation overrule",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_record":
        click.echo(f"Would record the ruling on {result.item['item_id']}; nothing was appended")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Curation item {result.item['item_id']}: {result.item['status']}")


@curation_group.command("accept-fixed")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@click.option("--proposal-id", required=True)
@click.option("--changeset-digest", required=True)
@change_control_options
@json_option
@handle_errors
def curation_accept_fixed(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    proposal_id: str,
    changeset_digest: str,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.accept_fixed_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=proposal_id,
            accepted_changeset_digest=changeset_digest,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation accept-fixed",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_record":
        click.echo(f"Would record the ruling on {result.item['item_id']}; nothing was appended")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Curation item {result.item['item_id']}: {result.item['status']}")


@curation_group.command("suppress")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@click.option("--scope", type=click.Choice(("item", "pattern", "instance")), required=True)
@click.option("--until-generation", type=click.IntRange(min=0))
@change_control_options
@json_option
@handle_errors
def curation_suppress(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: str,
    until_generation: int | None,
    dry_run: bool | None,
    at: str | None,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.suppress_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=cast(Any, scope),
            until_generation=until_generation,
            dry_run=dry_run,
            at=at,
        ),
        command_name="cruxible curation suppress",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if result.status == "would_record":
        click.echo(f"Would record the ruling on {result.item['item_id']}; nothing was appended")
        echo_preview_next(result.status, result.coordinate)
        return
    click.echo(f"Curation item {result.item['item_id']}: suppressed ({scope})")


@playbill_group.command("audit")
@click.option("--claim-type", "claim_types", multiple=True)
@click.option("--subject-kind", "subject_kinds", multiple=True)
@click.option(
    "--max-rows",
    default=AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    show_default=True,
    type=click.IntRange(AUDIT_BUDGET_MIN_MAX_ROWS, AUDIT_BUDGET_MAX_MAX_ROWS),
)
@click.option(
    "--max-bytes",
    default=AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    show_default=True,
    type=click.IntRange(AUDIT_BUDGET_MIN_MAX_BYTES, AUDIT_BUDGET_MAX_MAX_BYTES),
)
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--cursor",
    "cursor_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="AuditCursor JSON/YAML returned by a prior page.",
)
@json_option
@handle_errors
def audit(
    claim_types: tuple[str, ...],
    subject_kinds: tuple[str, ...],
    max_rows: int,
    max_bytes: int,
    access_profile_path: str | None,
    cursor_path: str | None,
    output_json: bool,
) -> None:
    """Read the deterministic verification patrol without changing governed state."""

    profile = (
        CoverageAccessProfile(
            profile_id="cli-audit",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfile).model_dump(mode="json")
    )
    cursor = None if cursor_path is None else _read_model(cursor_path, contracts.AuditCursor)
    result = _server_call(
        lambda client, instance_id: client.audit(
            instance_id,
            evaluation_time=datetime.now(UTC).isoformat(),
            access_profile=profile,
            claim_type_identities=tuple(
                sorted(set(claim_types), key=lambda item: item.encode("utf-8"))
            ),
            subject_kinds=tuple(sorted(set(subject_kinds), key=lambda item: item.encode("utf-8"))),
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        command_name="cruxible audit",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for row in result.rows:
        click.echo(
            f"{row.rank_score}  {row.claim_identity['kind']}:{row.claim_identity['name']}  "
            f"stake={row.factors.stake} weakness={row.factors.weakness} "
            f"staleness={row.factors.staleness}"
        )
    if not result.rows:
        click.echo("No Claims in the visible audit scope.")
    if result.next_cursor is not None:
        click.echo(f"More: {result.next_cursor.cursor_digest}")


@playbill_group.command("since")
@click.argument("generation", type=click.IntRange(min=0))
@click.option("--max-rows", default=100, show_default=True, type=click.IntRange(1, 1000))
@click.option("--max-bytes", default=65_536, show_default=True, type=click.IntRange(1, 1_048_576))
@click.option(
    "--access-profile",
    "access_profile_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="CoverageAccessProfile JSON/YAML; defaults to public and instance access.",
)
@click.option(
    "--cursor",
    "cursor_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="SinceCursor JSON/YAML returned by a prior page.",
)
@json_option
@handle_errors
def since(
    generation: int,
    max_rows: int,
    max_bytes: int,
    access_profile_path: str | None,
    cursor_path: str | None,
    output_json: bool,
) -> None:
    profile = (
        CoverageAccessProfile(
            profile_id="cli-since",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_since_access_profile(access_profile_path)
    )
    cursor = None if cursor_path is None else _read_mapping(cursor_path)
    result = _server_call(
        lambda client, instance_id: client.since(
            instance_id,
            generation=generation,
            access_profile=profile,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        command_name="cruxible since",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for row in result.rows:
        click.echo(f"{row.generation}  {row.disposition}  {row.artifact_kind}  {row.member_path}")
    if result.next_cursor is not None:
        click.echo(f"More: {result.next_cursor.cursor_digest}")


def _orient_predicate_line(item: Mapping[str, Any]) -> str:
    kind = item["type"]
    if item.get("members"):
        kind = "enum[" + "|".join(str(member) for member in item["members"]) + "]"
    parts = [str(item["name"]), str(item["cardinality"]), kind]
    if item.get("live_claims") is not None:
        parts.append(f"claims={item['live_claims']}")
    if item.get("stale_after"):
        parts.append(f"stale_after={item['stale_after']}")
    if item.get("evidence") is not None:
        parts.append("evidence=" + (",".join(item["evidence"]) or "(none)"))
    return "  ".join(parts)


def _render_orient(result: Mapping[str, Any]) -> str:
    lines = [
        f"Cruxible {result['instance']} generation={result['generation']} "
        f"at {result['coordinate']['git_oid'][:12]} accepted {result['accepted_at']}"
    ]
    floor = result.get("floor")
    if floor is not None:
        behind = floor["generations_behind"]
        lines.append(
            f"Floor: .cruxible/floor at {floor['at'][:12]}, "
            + (
                "current"
                if behind == 0
                else "generations behind unknown"
                if behind is None
                else f"{behind} generation(s) behind"
            )
            + ("" if behind == 0 else "; refresh: cruxible floor export --force")
        )
    you = result.get("you")
    if you is not None:
        refusal = you.get("authoring_refusal") or {}
        verdict = (
            "can author"
            if you["can_author"]
            else f"cannot author ({refusal.get('code')}): {refusal.get('detail')}"
        )
        lines.append(f"You: {you['actor'] or '(no actor)'}, {verdict}")
    kinds = result.get("kinds")
    if kinds is not None:
        lines.append(f"Kinds ({len(kinds)}):")
        for kind in kinds:
            shared = kind.get("evidence")
            lines.append(
                f"  {kind['kind']}  subjects={kind['subjects']}"
                + (f"  evidence={','.join(shared)}" if shared else "")
            )
            lines.extend(f"    {_orient_predicate_line(item)}" for item in kind["predicates"])
    artifacts = result.get("artifacts")
    if artifacts is not None:
        lines.append(
            "Artifacts: " + " ".join(f"{name}={count}" for name, count in artifacts.items())
        )
    detail = result.get("kind_detail")
    if detail is not None:
        shared = detail.get("evidence")
        lines.append(
            f"Kind {detail['kind']}  subjects={detail['subjects']}"
            + (f"  evidence={','.join(shared)}" if shared else "")
        )
        lines.extend(f"  {_orient_predicate_line(item)}" for item in detail["predicates"])
        if detail.get("incoming"):
            lines.append("Incoming (--follow-in): " + ", ".join(detail["incoming"]))
        if detail["sample_subject_ids"]:
            lines.append("Sample subjects: " + ", ".join(detail["sample_subject_ids"]))
    section = result.get("section")
    if section is not None and not result[section]:
        lines.append(f"(no {section.replace('_', ' ')})")
    if section in {
        "documents",
        "procedures",
        "runs",
        "lines",
        "captures",
        "capture_contracts",
        "predictions",
        "mandates",
    }:
        for row in result[section]:
            lines.append("  ".join(str(value) for value in row.values()))
    if section == "interfaces":
        for row in result["interfaces"]:
            providers = ",".join(row.get("providers", ())) or "(no provider)"
            lines.append(f"{row['name']}  effect={row['effect']}  providers={providers}")
            if row.get("description"):
                lines.append(f"  {row['description']}")
            lines.append(f"  in:  {', '.join(row.get('input', ())) or '-'}")
            lines.append(f"  out: {', '.join(row.get('output', ())) or '-'}")
    if section == "claim_types":
        for row in result["claim_types"]:
            lines.append(
                f"{row['predicate']}  kinds={','.join(row['subject_kinds'])}  "
                + _orient_predicate_line(row).split("  ", 1)[1]
            )
    queries = result.get("queries")
    if queries:
        if section is None:
            lines.append("Queries:")
        for query in queries:
            params = ", ".join(query["params"])
            description = f"  {query['description']}" if query.get("description") else ""
            lines.append(f"  {query['name']}({params}){description}")
    attention = result.get("attention")
    if attention is not None:
        lines.append(
            f"Attention: next={attention['next_items']} "
            f"open_proposals={attention['open_proposals']}"
        )
        lines.extend(f"  {line}" for line in attention["top"])
        lines.extend(f"  note: {line}" for line in attention.get("notes", ()))
        arms = attention.get("arms")
        if arms is not None:
            lines.append(
                f"  Line arms: running={arms['running']} stalled={arms['stalled']} "
                f"stopped={arms['stopped']}"
            )
            lines.extend(f"    {line}" for line in arms.get("needs_attention", ()))
    if result.get("next"):
        lines.append("Next:")
        lines.extend(f"  {line}" for line in result["next"])
    return "\n".join(lines)


@playbill_group.command("orient")
@click.option("--kind", default=None, help="Read one Subject kind in full.")
@click.option(
    "--section",
    type=click.Choice(list(get_args(contracts.OrientSection))),
    default=None,
    help="Page one artifact family instead of the map.",
)
@click.option(
    "--limit",
    type=click.IntRange(1, contracts.ORIENT_MAX_LIMIT),
    default=contracts.ORIENT_DEFAULT_LIMIT,
    show_default=True,
)
@click.option("--cursor", default=None, help="next_cursor from the previous page.")
@click.option(
    "--at",
    "at_oid",
    default=None,
    help=(
        "An accepted generation's Git OID, a unique 12+ hex prefix, or its number "
        "(all digits, 11 or fewer, is always a generation number)."
    ),
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def orient(
    kind: str | None,
    section: str | None,
    limit: int,
    cursor: str | None,
    at_oid: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Map accepted state: kinds, predicates, artifacts, attention and next commands."""

    result = _server_call(
        lambda client, instance_id: client.orient(
            instance_id,
            kind=kind,
            section=cast(Any, section),
            limit=limit,
            cursor=cursor,
            at=at_oid,
            evaluation_time=evaluation_time,
            surface="cli",
        ),
        command_name="cruxible orient",
    )
    workspace_root = containing_git_workspace_root(Path.cwd())
    if workspace_root is not None:
        result = workspace_floor_freshness(workspace_root, result)
    rendered = result.model_dump(mode="json")
    if output_json:
        _emit_json(rendered)
        return
    click.echo(_render_orient(rendered))


@playbill_group.command("stub")
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Write the stub to this path instead of standard output.",
)
@handle_errors
def world_stub(out_path: str | None) -> None:
    """Write a .pyi stub typing this instance's accepted world.

    A world is discovered at runtime, so an editor and a model both see `Any`
    where the instance's own kinds, Subjects and predicates are. This writes
    them down as types at exactly one accepted coordinate, which the stub names
    in its header. Regenerate it after every activation.
    """

    rendered = _server_call(
        lambda client, instance_id: render_world_stub_for(
            client,
            instance_id,
            workspace=Path.cwd(),
        ),
        command_name="cruxible stub",
    )
    if out_path is None:
        click.echo(rendered, nl=False)
        return
    target = Path(out_path).expanduser()
    if target.parent != Path("."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    click.echo(f"Wrote {target}")


@playbill_group.group("floor")
def floor_group() -> None:
    """Materialize the deterministic greppable floor of accepted state."""


@floor_group.command("export")
@click.option("--force", is_flag=True, help="Replace a non-empty .cruxible/floor cache.")
@click.option(
    "--with-discovery",
    is_flag=True,
    help=(
        "Also write the discovery cards (subjects/, claim-types/, procedures/, "
        "coverage-manifest.json); refresh after activation keeps them."
    ),
)
@json_option
@handle_errors
def export_floor(
    force: bool,
    with_discovery: bool,
    output_json: bool,
) -> None:
    """Write the accepted greppable floor to a deterministic local tree."""

    include: tuple[contracts.FloorExportPart, ...] = ("discovery",) if with_discovery else ()
    workspace_resolution = _local_git_workspace_root()
    _emit_git_workspace_note(workspace_resolution)
    workspace_root = workspace_resolution.workspace_root
    if workspace_root is None:
        raise click.UsageError("cruxible floor export must run inside one Git worktree")
    transport = _workspace_config_transport()
    if include:
        # The discovery cards are a full export's; they never travel in a delta.
        result, written = _server_call(
            lambda client, instance_id: write_workspace_floor(
                lambda: client.export_floor(instance_id, **floor_export_parts(include)),
                delivery=lambda: daemon_floor_delivery(
                    client, instance_id, workspace_root, include=include
                ),
                instance_id=instance_id,
                workspace=workspace_root,
                include=include,
                force=force,
                **transport,
            ),
            command_name="cruxible floor export",
        )
        manifest: dict[str, Any] = dict(result.manifest)
        touched = len(result.files)
    else:
        # The default floor goes through the one shared apply: the daemon is
        # sent this floor's own generation and answers with only what changed.
        delta, written = _server_call(
            lambda client, instance_id: write_workspace_floor_delta(
                lambda generation, renderer: client.floor_delta(
                    instance_id, base_generation=generation, base_renderer=renderer
                ),
                delivery=lambda: daemon_floor_delivery(client, instance_id, workspace_root),
                instance_id=instance_id,
                workspace=workspace_root,
                force=force,
                **transport,
            ),
            command_name="cruxible floor export",
        )
        manifest = json.loads(Path(written.destination, "manifest.json").read_text("utf-8"))
        touched = len(delta.files) + len(delta.tombstones)
    written = _with_git_workspace_note(written)
    if output_json:
        payload = manifest
        if written.git_workspace_note is not None:
            payload["git_workspace_note"] = written.git_workspace_note.model_dump(mode="json")
        _emit_json(payload)
        return
    if written.status == "unchanged":
        click.echo(f"Floor already current at {written.destination}; nothing written")
    else:
        click.echo(f"Wrote {touched} floor file(s) to {written.destination}")
    click.echo(f"Floor digest: {manifest['floor_digest']}")
    click.echo(f"Coordinate: {written.coordinate.git_oid}")


@playbill_group.group("coverage")
def coverage_group() -> None:
    """Deliver what working files have to do with accepted state."""


def _coverage_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--bind",
        "bind_values",
        multiple=True,
        help="Declare one binding as PATH=PLANE:IDENTITY. Repeat per working file.",
    )(function)
    function = click.option(
        "--bindings",
        "bindings_path",
        default=None,
        type=click.Path(exists=True, dir_okay=False),
        help="A mapping of working path to PLANE:IDENTITY.",
    )(function)
    function = click.option(
        "--root",
        default=".",
        show_default=True,
        type=click.Path(file_okay=False),
        help="Working root every bound path is read under.",
    )(function)
    return function


def _coverage_bindings(
    bind_values: tuple[str, ...],
    bindings_path: str | None,
) -> WorkingPathBindingsV1:
    """Collect the declared path bindings; coverage never infers one."""

    declared: dict[str, str] = {}
    if bindings_path is not None:
        for path, value in _read_mapping(bindings_path).items():
            if not isinstance(value, str):
                raise click.BadParameter("each binding value must be PLANE:IDENTITY")
            declared[str(path)] = value
    for entry in bind_values:
        path, separator, value = entry.partition("=")
        if not separator or not path or not value:
            raise click.BadParameter("a binding must be PATH=PLANE:IDENTITY")
        declared[path] = value

    return bindings_from_mapping(declared)


def _coverage_observations(
    bindings: WorkingPathBindingsV1,
    *,
    root: Path,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_path: str | None,
    whole_working_set: bool,
) -> tuple[WorkingSourceObservation, ...]:
    """Read the working set locally and hand the operation observations.

    Whole-source and windowed requests over the same path collapse to one
    observation, because a source is observed once per snapshot. A path named
    as changed is asked about whole, which is what makes an edit's drift
    visible without the caller having to guess which window moved.
    """

    grep_text = (
        None if grep_path is None else Path(grep_path).expanduser().read_text(encoding="utf-8")
    )
    return observe_workspace(
        bindings,
        root=root,
        files=files,
        ranges=ranges,
        grep_text=grep_text,
        whole_working_set=whole_working_set,
    )


def _resolved_coverage(
    observations: tuple[WorkingSourceObservation, ...],
    *,
    command_name: str,
    scan_budget: CoverageScanBudget | None = None,
    instance_id: str | None = None,
) -> CoverageResultV3:
    def resolve(
        client: CruxibleClient,
        selected_instance_id: str,
    ) -> contracts.CoverageResult:
        return client.resolve_coverage(
            selected_instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
            scan_budget=None if scan_budget is None else scan_budget.model_dump(mode="json"),
        )

    if instance_id is None:
        result = _server_call(resolve, command_name=command_name)
    else:
        dispatched = _dispatch_cli(
            lambda client: resolve(client, instance_id),
            lambda: None,
            allow_local=False,
            command_name=command_name,
        )
        if dispatched is None:
            raise click.ClickException("coverage resolver returned no result")
        result = dispatched
    return CoverageResultV3.model_validate(result.result)


@coverage_group.command("resolve")
@_coverage_options
@click.option("--file", "files", multiple=True, help="A changed or read working path.")
@click.option("--range", "ranges", multiple=True, help="A read selection as PATH:START-END.")
@click.option(
    "--grep-results",
    "grep_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="A `grep -n` result batch to resolve as one operation.",
)
@click.option("--all", "whole_working_set", is_flag=True, help="Resolve the whole declared scope.")
@brief_option
@json_option
@handle_errors
def resolve_coverage(
    bind_values: tuple[str, ...],
    bindings_path: str | None,
    root: str,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_path: str | None,
    whole_working_set: bool,
    output_brief: bool,
    output_json: bool,
) -> None:
    """Resolve what the working files you just read or changed are governed by.

    Governed spans are annotated inline; the ungoverned majority is summarized
    once. Resolving coverage changes no accepted state and appends no receipt.
    """

    bindings = _coverage_bindings(bind_values, bindings_path)
    observations = _coverage_observations(
        bindings,
        root=Path(root).expanduser(),
        files=files,
        ranges=ranges,
        grep_path=grep_path,
        whole_working_set=whole_working_set,
    )
    result = _resolved_coverage(observations, command_name="cruxible coverage resolve")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if output_brief:
        _emit_brief(
            outcome=(
                f"{result.health} ({result.summary.exact} exact, {result.summary.drifted} drifted)"
            ),
            ids={"coordinate": result.at.git_oid, "epoch": str(result.epoch)},
            next_command=(
                "cruxible next --brief"
                if result.summary.drifted or result.health != "complete"
                else None
            ),
        )
        return
    for line in render_coverage_result(result):
        click.echo(line)


@coverage_group.command("status")
@_coverage_options
@json_option
@handle_errors
def coverage_status(
    bind_values: tuple[str, ...],
    bindings_path: str | None,
    root: str,
    output_json: bool,
) -> None:
    """Render the coverage manifest: epoch, health, completeness, and scope."""

    bindings = _coverage_bindings(bind_values, bindings_path)
    observations = _coverage_observations(
        bindings,
        root=Path(root).expanduser(),
        files=(),
        ranges=(),
        grep_path=None,
        whole_working_set=True,
    )
    result = _resolved_coverage(observations, command_name="cruxible coverage status")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for line in render_coverage_manifest(result):
        click.echo(line)


@playbill_group.group("hook")
def hook_group() -> None:
    """Deprecated/parked harness adapter retained for compatibility."""

    # PC-DEL3 parks this shipped Claude Code adapter. It remains registered and
    # behavior-compatible, but new integrations should consume coverage through
    # the client middleware rather than extending this vendor-specific surface.


def _hook_resolver(config: CoverageWorkspaceConfig) -> ResolveCoverage:
    """Resolve through the served operation, as every other coverage caller does.

    The workspace's declared scan budget rides along here rather than inside the
    middleware, because bounding how many bytes are hashed looking for relocated
    content is a property of the operation, not of the adapter that calls it.
    """

    def resolve(observations: Sequence[WorkingSourceObservation]) -> CoverageResultV3:
        return _resolved_coverage(
            tuple(observations),
            command_name="cruxible hook post-tool-use",
            scan_budget=config.scan_budget,
            instance_id=config.instance_id,
        )

    return resolve


def _hook_floor_generation_resolver() -> ResolveFloorGenerations:
    """Resolve old and current generations through the head read."""

    def orientation(at: AcceptedCoordinate | None) -> int:
        result = _server_call(
            lambda client, instance_id: client.head(
                instance_id, at=None if at is None else at.model_dump(mode="json")
            ),
            command_name="cruxible hook floor freshness",
        )
        return result.generation

    def resolve(coordinate: AcceptedCoordinate) -> FloorGenerationPairV1:
        floor_generation = orientation(coordinate)
        current_generation = orientation(None)
        return FloorGenerationPairV1(
            floor_generation=floor_generation,
            current_generation=current_generation,
        )

    return resolve


@hook_group.command("post-tool-use")
@click.option(
    "--root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace root holding .cruxible/coverage.json.",
)
def post_tool_use_hook(root: str) -> None:
    """Annotate a Claude Code tool result with coverage, reading the hook JSON on stdin.

    Wire this as a PostToolUse hook for Read, Grep, Edit, and Write; the
    settings fragment is in `integrations/claude-code/`. Grep content-mode
    results are annotated in place. Read, Edit, and Write are observed -- which
    refreshes the local freshness manifest so the next Grep answers against a
    current snapshot -- and their output is returned unchanged, because those
    tools' result shapes cannot carry an annotation without fabricating file
    content. The middleware API is the full-fidelity path for a harness that
    owns its tool executor.

    Always exits 0 and always emits one JSON object: a coverage failure may
    never break the agent's tool call.
    """

    payload: Any = None
    text = ""
    diagnostic: str | None = None
    try:
        payload = json.loads(sys.stdin.read() or "null")
        workspace = Path(root).expanduser()
        event = read_post_tool_use_event(payload, workspace_root=workspace)
        if event is not None:
            config = load_coverage_config(workspace)
            middleware = coverage_middleware(
                root=workspace,
                config=config,
                resolve=_hook_resolver(config),
                resolve_floor_generations=_hook_floor_generation_resolver(),
            )
            delivery = middleware.after_tool(event)
            text = delivery.appended_coverage_text
            if delivery.failure_code == "coverage_operation_unavailable":
                if config.instance_id is None:
                    try:
                        _require_instance_id()
                    except click.UsageError:
                        diagnostic = "cruxible.coverage_hook.instance_id_missing"
    except CoverageRuleTagError:
        diagnostic = "cruxible.coverage_hook.rule_tag_invalid"
        text = ""
    except PostToolUseResponseError:
        diagnostic = "cruxible.coverage_hook.tool_response_invalid"
        text = ""
    except Exception:  # noqa: BLE001 - fail open; a broken hook is not the agent's problem
        text = ""
    if diagnostic is not None:
        click.echo(diagnostic, err=True)
    _emit_json(post_tool_use_response(annotated_tool_output(payload, text)))


__all__ = ["playbill_group"]
