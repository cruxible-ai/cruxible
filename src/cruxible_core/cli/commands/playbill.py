"""Playbill Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import click
import yaml
from pydantic import TypeAdapter, ValidationError

from cruxible_client import (
    CruxibleClient,
    activate_with_workspace_refresh,
    contracts,
    observe_playbill_next_workspace,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
    local_attestation_signer_from_environment,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.blocks import repin_projection_block
from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_FACTORIES,
    AUTHORING_EXAMPLE_NAMES,
    AuthoringExampleName,
    authoring_example,
    document_example,
)
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_client.authoring.sources import (
    compile_client_source_context,
    load_source_catalog,
    root_aliases,
)
from cruxible_client.authoring.workspace import observe_playbill_next_workspace_with_coverage
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_attestations import (
    ClaimStance,
    PreparedClaimAttestationRequestV1,
)
from cruxible_client.contracts.claims import ClaimRetireRequestV1
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    DocumentNotFoundError,
    PlaybillDeprecatedWriteError,
    PlaybillSinceRequestInvalid,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.proposal_models import canonical_proposal_ref_name
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_catalog import SourceCatalog, SourceCompilationBundle
from cruxible_client.contracts.types import PrincipalKind, PrincipalRecord
from cruxible_client.errors import DataValidationError
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _echo_write_target,
    _emit_brief,
    _emit_json,
    _require_instance_id,
    and_activate_option,
    brief_option,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.claim_type_migrations import ClaimTypeMigrationRequest
from cruxible_core.playbill.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingSourceObservationV1,
)
from cruxible_core.playbill.coverage.claude_code import (
    annotated_tool_output,
    post_tool_use_response,
    read_post_tool_use_event,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1, CoverageResultV3
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.coverage.middleware import (
    CoverageWorkspaceConfig,
    FloorGenerationPairV1,
    ResolveCoverage,
    ResolveFloorGenerations,
    coverage_middleware,
    load_coverage_config,
)
from cruxible_core.playbill.coverage.render import (
    render_coverage_manifest,
    render_coverage_result,
)
from cruxible_core.playbill.coverage.workspace import bindings_from_mapping, observe_workspace
from cruxible_core.playbill.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
    AUDIT_BUDGET_MAX_MAX_BYTES,
    AUDIT_BUDGET_MAX_MAX_ROWS,
    AUDIT_BUDGET_MIN_MAX_BYTES,
    AUDIT_BUDGET_MIN_MAX_ROWS,
)
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.review import (
    PlaybillProposalReview,
    render_playbill_proposal_review,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
from cruxible_core.service.playbill_procedure_runs import ProcedureBindRequestV1

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
    result = _dispatch_cli(
        lambda client: operation(client, _require_instance_id()),
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
    """Read a CoverageAccessProfileV1 file for since, filling model defaults.

    A file that is not a valid profile surfaces the same typed since refusal
    the daemon would give, before any request is built.
    """
    payload = _read_mapping(path)
    try:
        return CoverageAccessProfileV1.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise PlaybillSinceRequestInvalid.from_validation_errors(
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


_AUTHORING_INPUT_ADAPTER: TypeAdapter[AuthoringInputV1] = TypeAdapter(AuthoringInputV1)
_CLAIM_TYPE_MIGRATION_ADAPTER: TypeAdapter[ClaimTypeMigrationRequest] = TypeAdapter(
    ClaimTypeMigrationRequest
)
_CLAIM_RETIRE_ADAPTER = TypeAdapter(ClaimRetireRequestV1)


def _claim_retire_example() -> ClaimRetireRequestV1:
    return ClaimRetireRequestV1(
        mode="preflight",
        claim_ref="Claim:CLM-0123456789abcdef0123456789abcdef",
        reason="was-wrong",
        effective_until=None,
        expected_coordinate=AcceptedCoordinate(
            git_oid="0" * 40,
            semantic_root="sha256:" + "0" * 64,
            generation_root="sha256:" + "0" * 64,
            compiler_digest="sha256:" + "0" * 64,
        ),
        dependents=(),
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


def _read_authoring_input(path: str) -> AuthoringInputV1:
    payload = _read_mapping(path)
    try:
        return _AUTHORING_INPUT_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        examples = ", ".join(
            f"playbill authoring create --example {name}"
            for name in _authoring_examples_for(payload)
        )
        errors = "; ".join(
            f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
            for item in exc.errors(include_url=False)
        )
        raise click.ClickException(
            f"Invalid authoring input: {errors}. Matching example: {examples}"
        ) from exc


def _write_floor(destination: Path, export: contracts.PlaybillFloorExport, *, force: bool) -> None:
    """Materialize the floor bytes; the daemon never writes a client path."""

    import base64

    if destination.exists() and any(destination.iterdir()) and not force:
        raise click.ClickException(
            f"Refusing to write the floor into a non-empty directory: {destination}. "
            "Pass --force to overwrite."
        )
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    for item in export.files:
        target = (destination / item.path).resolve()
        if not target.is_relative_to(root):
            raise click.ClickException(f"Refusing to write outside the export root: {item.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(item.content_base64, validate=True))


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
    """Govern Documents through Playbill's proposal and acceptance ledger."""


@playbill_group.group("host")
def host_group() -> None:
    """Allocate daemon-owned hosts without adopting config or semantic state."""


@host_group.command("create")
@click.option("--instance-id", default=None, help="Optional caller-selected opaque ID.")
@json_option
@handle_errors
def create_host(instance_id: str | None, output_json: bool) -> None:
    """Allocate an empty host and remember it as the active instance."""

    result = _dispatch_cli(
        lambda client: client.create_playbill_host(instance_id=instance_id),
        lambda: None,
        allow_local=False,
        command_name="playbill host create",
    )
    assert isinstance(result, contracts.PlaybillHostResult)
    _activate_server_instance(result.instance_id)
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Playbill host: {result.instance_id} ({result.status})")


@playbill_group.command("init")
@click.option("--key-dir", required=True, help="Client custody directory outside the workspace.")
@click.option("--principal-id", default="bootstrap-admin", show_default=True)
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
@json_option
@handle_errors
def init_playbill(
    key_dir: str,
    principal_id: str,
    reviewer_key_dir: str | None,
    require_independent_approval: bool,
    recovery_key_dir: str | None,
    recovery_principal_id: str,
    profile: str,
    output_json: bool,
) -> None:
    """Create client custody and bootstrap the governed approval policy."""

    workspace = Path.cwd().resolve()
    if require_independent_approval and reviewer_key_dir is None:
        raise click.UsageError("--require-independent-approval requires --reviewer-key-dir")
    owner = generate_client_principal_key(
        Path(key_dir).expanduser(),
        principal_id=principal_id,
        kind="ordinary",
        forbidden_roots=(workspace,),
    )
    reviewer = (
        None
        if reviewer_key_dir is None
        else generate_client_principal_key(
            Path(reviewer_key_dir).expanduser(),
            principal_id="reviewer",
            kind="ordinary",
            forbidden_roots=(workspace,),
        )
    )
    principals = [owner.principal]
    if reviewer is not None:
        principals.append(reviewer.principal)
    if recovery_key_dir is not None:
        recovery = generate_client_principal_key(
            Path(recovery_key_dir).expanduser(),
            principal_id=recovery_principal_id,
            kind="recovery",
            forbidden_roots=(workspace,),
        )
        principals.append(recovery.principal)
    result = _server_call(
        lambda client, selected: client.init_playbill(
            selected,
            principals=[item.model_dump(mode="json") for item in principals],
            operating_profile=cast(Any, profile),
            require_independent_approval=require_independent_approval,
        ),
        command_name="playbill init",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Playbill initialized at {result.coordinate.git_oid}")
    click.echo(f"Approval policy: {result.approval_policy_mode}")
    click.echo(f"Owner public key: {owner.principal.public_key}")
    click.echo(f"Owner private key retained locally at: {owner.private_key_path}")
    if reviewer is not None:
        click.echo(f"Reviewer public key: {reviewer.principal.public_key}")
        click.echo(f"Reviewer private key retained locally at: {reviewer.private_key_path}")


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
        lambda client, instance_id: client.store_playbill_body(instance_id, content),
        command_name="playbill body store",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(result.digest)


@playbill_group.group("document")
def document_group() -> None:
    """Propose and read governed Documents."""


@document_group.command("propose")
@click.option("--envelope", type=click.Path(exists=True, dir_okay=False))
@click.option("--example", type=click.Choice(["document"]))
@click.option("--name", "proposal_name")
@json_option
@handle_errors
def propose_document(
    envelope: str | None,
    example: str | None,
    proposal_name: str | None,
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
        lambda client, instance_id: client.propose_playbill_document(
            instance_id,
            shell=shell.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        command_name="playbill document propose",
    )
    _emit_json(result.model_dump(mode="json"))


@document_group.command("list")
@json_option
@handle_errors
def list_documents(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_documents(instance_id),
        command_name="playbill document list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for document in result.documents:
        click.echo(f"{document.envelope['identity']}  {document.envelope['path']}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@document_group.command("get")
@click.argument("identity")
@json_option
@handle_errors
def get_document(identity: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_document(instance_id, identity),
        command_name="playbill document get",
    )
    _emit_json(result.model_dump(mode="json"))


@document_group.command("body")
@click.argument("identity")
@click.option("--output", type=click.Path(dir_okay=False), default=None)
@json_option
@handle_errors
def get_document_body(identity: str, output: str | None, output_json: bool) -> None:
    import base64

    result = _server_call(
        lambda client, instance_id: client.dereference_playbill_document(instance_id, identity),
        command_name="playbill document body",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    content = base64.b64decode(result.content_base64, validate=True)
    if output is None:
        click.echo(content.decode("utf-8"), nl=False)
    else:
        destination = Path(output)
        with destination.open("xb") as handle:
            handle.write(content)


@document_group.command("history")
@click.argument("identity")
@json_option
@handle_errors
def document_history(identity: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.playbill_document_history(instance_id, identity),
        command_name="playbill document history",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("proposal")
def proposal_group() -> None:
    """Inspect, review, approve, and activate candidates."""


@proposal_group.command("list")
@click.option("--status", type=click.Choice(["open", "settled"]), default=None)
@json_option
@handle_errors
def list_proposals(status: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_proposals(
            instance_id,
            status=cast(Any, status),
        ),
        command_name="playbill proposal list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for entry in result.entries:
        terminal = "" if entry.terminal_reason is None else f" {entry.terminal_reason}"
        click.echo(
            f"{entry.status}{terminal}  {entry.proposal_id}  "
            f"{entry.target_ref}  {entry.admitted_at}"
        )
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@proposal_group.command("readmit")
@click.argument("proposal_id")
@json_option
@handle_errors
def readmit_proposal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.readmit_playbill_proposal(instance_id, proposal_id),
        command_name="playbill proposal readmit",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    proposal = result.proposal.proposal
    evaluation = proposal.get("evaluation", {})
    admission = proposal.get("admission", {})
    click.echo(
        f"{evaluation.get('verdict')}  {admission.get('proposal_id')}  "
        f"from {result.source_proposal_id}"
    )


@proposal_group.command("inspect")
@click.argument("proposal_id")
@json_option
@handle_errors
def inspect_proposal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.inspect_playbill_proposal(instance_id, proposal_id),
        command_name="playbill proposal inspect",
    )
    _emit_json(result.model_dump(mode="json"))


@proposal_group.command("refusal")
@click.argument("proposal_id")
@json_option
@handle_errors
def inspect_refusal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.inspect_playbill_refusal(instance_id, proposal_id),
        command_name="playbill proposal refusal",
    )
    _emit_json(result.model_dump(mode="json"))


@proposal_group.command("review")
@click.argument("proposal_id")
@click.option("--include-body/--redacted", default=True)
@json_option
@handle_errors
def review_proposal(proposal_id: str, include_body: bool, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.review_playbill_proposal(
            instance_id, proposal_id, include_body=include_body
        ),
        command_name="playbill proposal review",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        review = PlaybillProposalReview.model_validate(result.model_dump(mode="json"))
        click.echo(render_playbill_proposal_review(review), nl=False)


@proposal_group.command("approve")
@click.argument("proposal_id")
@click.option("--signer-id", required=True)
@click.option("--key", "private_key_path", required=True, type=click.Path(dir_okay=False))
@click.option("--yes", is_flag=True, help="Approve after rendering without an interactive prompt.")
@json_option
@handle_errors
def approve_proposal(
    proposal_id: str,
    signer_id: str,
    private_key_path: str,
    yes: bool,
    output_json: bool,
) -> None:
    challenge = _server_call(
        lambda client, instance_id: client.prepare_playbill_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=True,
        ),
        command_name="playbill proposal approve",
    )
    review = PlaybillProposalReview.model_validate(challenge.review.model_dump(mode="json"))
    if not output_json:
        click.echo(render_playbill_proposal_review(review), nl=False)
    if not yes and not click.confirm("Sign this exact candidate?"):
        raise click.Abort()
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    signer = LocalEd25519ApprovalSigner.open(
        signer_id=signer_id,
        private_key_path=Path(private_key_path),
        expected_public_key=principal.public_key,
        forbidden_roots=(Path.cwd(),),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(challenge.statement))
    result = _server_call(
        lambda client, instance_id: client.submit_playbill_approval(
            instance_id,
            proposal_id,
            attestation=attestation.model_dump(mode="json"),
        ),
        command_name="playbill proposal approve",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
    else:
        click.echo(f"Approved {result.candidate_digest} as {result.signer_id}")


@proposal_group.command("activate")
@click.argument("proposal_id")
@click.option(
    "--workspace-root",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False),
    help="Workspace holding .playbill/coverage.json and its optional floor output.",
)
@brief_option
@json_option
@handle_errors
def activate_proposal(
    proposal_id: str, workspace_root: str, output_brief: bool, output_json: bool
) -> None:
    result = _server_call(
        lambda client, instance_id: activate_with_workspace_refresh(
            client,
            instance_id,
            proposal_id,
            workspace=Path(workspace_root),
        ),
        command_name="playbill proposal activate",
    )
    payload = result.model_dump(mode="json")
    if result.floor_refresh.status == "failed":
        message = result.floor_refresh.message or "unknown client workspace error"
        _emit_json(payload)
        raise click.ClickException(
            f"proposal activation status={result.status}; floor refresh failed: {message}"
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
            next_command="cruxible playbill next --brief",
        )
        return
    _emit_json(payload)


@playbill_group.command("whoami")
@json_option
@handle_errors
def whoami(output_json: bool) -> None:
    """Explain the transport-derived actor, permission mode, and principal status."""

    result = _server_call(
        lambda client, instance_id: client.playbill_whoami(instance_id),
        command_name="playbill whoami",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Actor: {result.actor_id}")
    if result.actor_id_source == "runtime_credential_label":
        click.echo(f"Actor ID comes from credential label: {result.credential_label}")
    else:
        click.echo("Actor ID comes from the local operator identity")
    click.echo(f"Credential permission mode: {result.credential_permission_mode}")
    click.echo(f"Principal registration: {result.principal_registration_status}")
    click.echo(f"Active principals: {', '.join(result.active_principal_ids) or 'none'}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


# `playbill explain` resolves a Document identity and explains the Subject that
# Document is. An identity of another kind used to reach the daemon and come back
# as a bare `CoreError: <what you typed>`, naming neither the accepted shape nor
# the command that does answer for that kind. Each entry routes one recognizable
# identity shape to the verb that actually explains it.
_EXPLAIN_ROUTES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"^(?:Claim:)?(?P<rest>CLM-[0-9a-f]+)$"), "Claim", "claim explain {rest}"),
    (re.compile(r"^ClaimType:(?P<rest>.+)$"), "ClaimType", "claim-type get {rest}"),
    (
        re.compile(r"^Subject:(?P<kind>[^/]+)/(?P<id>.+)$"),
        "Subject",
        "subject get {kind} {id}",
    ),
    (re.compile(r"^Procedure:(?P<rest>.+)$"), "Procedure", "procedure readiness {rest}"),
    (re.compile(r"^QueryDefinition:(?P<rest>.+)$"), "QueryDefinition", "query get {rest}"),
)

_EXPLAIN_ACCEPTS = (
    "playbill explain accepts one accepted Document identity "
    "(for example document:fleet.policy-note)"
)


def _explain_route(identity: str) -> tuple[str, str] | None:
    """Name the kind and the exact command that explains it, when the shape says so."""
    for pattern, kind, template in _EXPLAIN_ROUTES:
        match = pattern.match(identity)
        if match is not None:
            return kind, "cruxible playbill " + template.format(**match.groupdict())
    return None


@playbill_group.command("explain")
@click.argument("identity")
@click.option("--detail", type=click.Choice(["summary", "evidence", "proof"]), default="summary")
@click.option("--include-body", is_flag=True)
@json_option
@handle_errors
def explain(identity: str, detail: str, include_body: bool, output_json: bool) -> None:
    """Explain one accepted Document by identity."""

    route = _explain_route(identity)
    if route is not None:
        kind, command = route
        raise DataValidationError(
            f"{identity} is a {kind} identity, not a Document identity: "
            f"{_EXPLAIN_ACCEPTS}. Use `{command}` to explain this {kind}."
        )

    def call(
        client: CruxibleClient, instance_id: str
    ) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
        try:
            document = client.get_playbill_document(instance_id, identity)
        except DocumentNotFoundError as exc:
            raise DocumentNotFoundError(
                f"no accepted Document has identity {identity}: {_EXPLAIN_ACCEPTS}. "
                "Other kinds have their own explainers: `cruxible playbill claim explain` "
                "for a Claim, `cruxible playbill subject get` for a Subject, "
                "`cruxible playbill expand` for any accepted artifact path."
            ) from exc
        path = str(document.envelope["path"])
        return client.explain_playbill_subject(
            instance_id,
            subject=SemanticAddress.whole_artifact(path).model_dump(mode="json"),
            at=document.coordinate,
            detail=cast(Any, detail),
            include_body=include_body,
        )

    result = _server_call(call, command_name="playbill explain")
    _emit_json(result.model_dump(mode="json"))


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
        command_name="playbill sources compile",
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

    def call(client: CruxibleClient, instance_id: str) -> contracts.PlaybillSourceCheckResult:
        bundle = _compile_remote_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=Path(repository_root),
            aliases=_root_aliases(root_alias),
        )
        return client.check_playbill_source_bundle(
            instance_id, bundle=bundle.model_dump(mode="json")
        )

    result = _server_call(call, command_name="playbill sources check")
    _emit_json(result.model_dump(mode="json"))


@sources_group.command("propose")
@click.option(
    "--bundle", "bundle_path", required=True, type=click.Path(exists=True, dir_okay=False)
)
@click.option("--source", "source_name", required=True)
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_sources(
    bundle_path: str,
    source_name: str,
    proposal_name: str,
    output_json: bool,
) -> None:
    bundle = _read_model(bundle_path, SourceCompilationBundle)
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_source_bundle(
            instance_id,
            bundle=bundle.model_dump(mode="json"),
            source_name=source_name,
            proposal_name=proposal_name,
        ),
        command_name="playbill sources propose",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("principal")
def principal_group() -> None:
    """List and govern ordinary/recovery public keys."""


@principal_group.command("list")
@json_option
@handle_errors
def list_principals(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_principals(instance_id),
        command_name="playbill principal list",
    )
    _emit_json(result.model_dump(mode="json"))


@principal_group.command("add")
@click.argument("principal_id")
@click.option(
    "--kind",
    type=click.Choice(("ordinary", "recovery")),
    default="ordinary",
    show_default=True,
    help="Closed principal kind; daemon is instance-owned.",
)
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def add_principal(
    principal_id: str,
    kind: str,
    key_dir: str,
    proposal_name: str,
    output_json: bool,
) -> None:
    """Generate a client-held key and propose principal registration."""

    principal_kind = cast(PrincipalKind, kind)
    try:
        ref_name = canonical_proposal_ref_name(proposal_name)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--name") from exc

    def call(client: CruxibleClient, instance_id: str) -> contracts.PlaybillProposalInspection:
        existing = client.list_playbill_principals(instance_id)
        if any(item.get("principal_id") == principal_id for item in existing.principals):
            raise click.ClickException(f"Playbill principal already exists: {principal_id}")
        material = generate_client_principal_key(
            Path(key_dir).expanduser(),
            principal_id=principal_id,
            kind=principal_kind,
            forbidden_roots=(Path.cwd(),),
        )
        return client.propose_playbill_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=ref_name,
        )

    result = _server_call(call, command_name="playbill principal add")
    _emit_json(result.model_dump(mode="json"))


def _principal_successor(
    *,
    target_id: str,
    key_dir: str,
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    def call(client: CruxibleClient, instance_id: str) -> contracts.PlaybillProposalInspection:
        listing = client.list_playbill_principals(instance_id)
        matches = [PrincipalRecord.model_validate(item) for item in listing.principals]
        target = next((item for item in matches if item.principal_id == target_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Playbill principal: {target_id}")
        material = generate_client_principal_key(
            Path(key_dir).expanduser(),
            principal_id=target_id,
            kind=target.kind,
            forbidden_roots=(Path.cwd(),),
        )
        return client.propose_playbill_principal_change(
            instance_id,
            principal=material.principal.model_dump(mode="json"),
            proposal_name=proposal_name,
        )

    return _server_call(call, command_name="playbill principal change")


@principal_group.command("rotate")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def rotate_principal(
    principal_id: str, key_dir: str, proposal_name: str, output_json: bool
) -> None:
    """Propose a self-rotation; activation requires the actor's key signature."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
    )
    _emit_json(result.model_dump(mode="json"))


@principal_group.command("recover")
@click.argument("principal_id")
@click.option("--key-dir", required=True)
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def recover_principal(
    principal_id: str, key_dir: str, proposal_name: str, output_json: bool
) -> None:
    """Use recovery identity for a narrowly governed key replacement."""

    result = _principal_successor(
        target_id=principal_id,
        key_dir=key_dir,
        proposal_name=proposal_name,
    )
    _emit_json(result.model_dump(mode="json"))


@principal_group.command("revoke")
@click.argument("principal_id")
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def revoke_principal(principal_id: str, proposal_name: str, output_json: bool) -> None:
    def call(client: CruxibleClient, instance_id: str) -> contracts.PlaybillProposalInspection:
        listing = client.list_playbill_principals(instance_id)
        matches = [PrincipalRecord.model_validate(item) for item in listing.principals]
        target = next((item for item in matches if item.principal_id == principal_id), None)
        if target is None:
            raise click.ClickException(f"Unknown Playbill principal: {principal_id}")
        revoked = target.model_copy(update={"status": "revoked"})
        return client.propose_playbill_principal_change(
            instance_id,
            principal=revoked.model_dump(mode="json"),
            proposal_name=proposal_name,
        )

    result = _server_call(call, command_name="playbill principal revoke")
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("subject")
def subject_group() -> None:
    """Propose and read identity-only governed Subjects."""


@subject_group.command("propose")
@click.option(
    "--envelope",
    type=click.Path(exists=True, dir_okay=False),
    help="Deprecated and ignored by this compatibility shim.",
)
@click.option(
    "--name",
    "proposal_name",
    help="Deprecated and ignored by this compatibility shim.",
)
@json_option
@handle_errors
def propose_subject(
    envelope: str | None,
    proposal_name: str | None,
    output_json: bool,
) -> None:
    """Deprecated: use playbill authoring create then authoring submit."""

    del envelope, proposal_name, output_json
    raise PlaybillDeprecatedWriteError(
        replacement="the authoring coordinator with payload kind 'subject'"
    )


@subject_group.command("list")
@json_option
@handle_errors
def list_subjects(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_subjects(instance_id),
        command_name="playbill subject list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for subject in result.subjects:
        click.echo(f"{subject.envelope['identity']}  {subject.envelope['path']}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@subject_group.command("get")
@click.argument("subject_kind")
@click.argument("subject_id")
@json_option
@handle_errors
def get_subject(subject_kind: str, subject_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_subject(
            instance_id, subject_kind, subject_id
        ),
        command_name="playbill subject get",
    )
    _emit_json(result.model_dump(mode="json"))


@subject_group.command("history")
@click.argument("subject_kind")
@click.argument("subject_id")
@json_option
@handle_errors
def subject_history(subject_kind: str, subject_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.playbill_subject_history(
            instance_id, subject_kind, subject_id
        ),
        command_name="playbill subject history",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("claim-type")
def claim_type_group() -> None:
    """Propose and read the governed predicate vocabulary."""


@claim_type_group.command("propose")
@click.option("--input", "input_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--envelope", type=click.Path(exists=True, dir_okay=False), hidden=True)
@click.option("--name", "proposal_name")
@json_option
@handle_errors
def propose_claim_type(
    input_path: str | None,
    envelope: str | None,
    proposal_name: str | None,
    output_json: bool,
) -> None:
    """Use the sanctioned typed-input ClaimType proposal path."""

    if (input_path is None) == (envelope is None):
        raise click.UsageError("provide exactly one ClaimType input with --input")
    if envelope is not None:
        envelope_payload = _read_mapping(envelope)
        resolved_name = proposal_name or envelope_payload.get("predicate")
        if not isinstance(resolved_name, str) or not resolved_name:
            raise click.UsageError("--name is required when the ClaimType payload has no predicate")
        envelope_result = _server_call(
            lambda client, instance_id: client.propose_playbill_claim_type(
                instance_id,
                claim_type=envelope_payload,
                proposal_name=resolved_name,
            ),
            command_name="playbill claim-type propose",
        )
        _emit_json(envelope_result.model_dump(mode="json"))
        return
    assert input_path is not None
    try:
        claim_type_input = ClaimTypeInputV1.model_validate(_read_mapping(input_path))
    except ValidationError as exc:
        raise click.ClickException(
            "Invalid ClaimType input: "
            + "; ".join(
                f"{_validation_path(tuple(item['loc']))}: {item['msg']}"
                for item in exc.errors(include_url=False)
            )
            + ". Pass a complete ClaimTypeInputV1 whose evidence_admission_policy.rules "
            "match its capture contracts"
        ) from exc
    input_result = _server_call(
        lambda client, instance_id: client.propose_playbill_claim_type_input(
            instance_id,
            input=claim_type_input.model_dump(mode="json"),
            proposal_name=proposal_name or claim_type_input.predicate,
        ),
        command_name="playbill claim-type propose",
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
        raise click.ClickException(f"Invalid ClaimType migration: {exc}") from exc
    result = _server_call(
        lambda client, instance_id: client.migrate_playbill_claim_type(
            instance_id,
            request=request.model_dump(mode="json"),
        ),
        command_name="playbill claim-type migrate",
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
        click.echo(f"Proposal: {result.proposal.proposal.get('proposal_id', '<submitted>')}")
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


@claim_type_group.command("list")
@json_option
@handle_errors
def list_claim_types(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_claim_types(instance_id),
        command_name="playbill claim-type list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for claim_type in result.claim_types:
        click.echo(f"{claim_type.predicate}  {claim_type.artifact_digest}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@claim_type_group.command("get")
@click.argument("predicate")
@json_option
@handle_errors
def get_claim_type(predicate: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_claim_type(instance_id, predicate),
        command_name="playbill claim-type get",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("claim")
def claim_group() -> None:
    """Propose, read, and explain first-class governed Claims."""


@playbill_group.group("claim-attestation")
def claim_attestation_group() -> None:
    """Operate the principal-authored Claim-attestation evidence ledger."""


@claim_attestation_group.command("recover")
@handle_errors
def recover_claim_attestations() -> None:
    """Roll the sole durable unpublished attestation forward after a poison refusal."""

    _server_call(
        lambda client, instance_id: client.recover_playbill_claim_attestations(instance_id),
        command_name="playbill claim-attestation recover",
    )
    click.echo("Claim-attestation evidence ledger recovered.")


@claim_group.command("attest")
@click.argument("claim_id")
@click.option("--support", is_flag=True)
@click.option("--contradict", is_flag=True)
@click.option("--unsure", is_flag=True)
@click.option("--note")
@json_option
@handle_errors
def attest_claim(
    claim_id: str,
    support: bool,
    contradict: bool,
    unsure: bool,
    note: str | None,
    output_json: bool,
) -> None:
    """Sign that this caller examined the current exact Claim."""

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
            workspace_root=Path.cwd(),
        )
        return append_prepared_claim_attestation(
            client,
            instance_id,
            prepared=PreparedClaimAttestationRequestV1(
                claim_id=claim_id.removeprefix("Claim:"),
                attestation_basis="examined_existing",
                stance=cast(ClaimStance, stance),
                attested_at=datetime.now(UTC),
                note=note,
            ),
            signer=signer,
        )

    result = _server_call(call, command_name="playbill claim attest")
    _emit_json(result.model_dump(mode="json"))


@claim_group.command("retire")
@click.argument("claim_id", required=False)
@click.argument("request_file", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option("--example", is_flag=True, help="Print a valid retirement request file.")
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
def retire_claim(
    claim_id: str | None,
    request_file: str | None,
    example: bool,
    and_activate: bool,
    workspace_root: str,
    output_brief: bool,
    output_json: bool,
) -> None:
    """Preflight or submit one attributed Claim retirement closure."""

    if example:
        if claim_id is not None or request_file is not None:
            raise click.UsageError("--example does not accept CLAIM_ID or REQUEST_FILE")
        click.echo(
            json.dumps(
                _claim_retire_example().model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    if claim_id is None or request_file is None:
        raise click.UsageError("provide CLAIM_ID REQUEST_FILE or --example")
    try:
        request = _CLAIM_RETIRE_ADAPTER.validate_python(_read_mapping(request_file))
    except ValidationError as exc:
        raise click.ClickException(f"Invalid Claim retirement: {exc}") from exc

    def call(client: CruxibleClient, instance_id: str) -> tuple[Any, Any]:
        retired = client.retire_playbill_claim(
            instance_id,
            claim_id,
            request=request.model_dump(mode="json"),
        )
        proposal_id = _retire_proposal_id(retired)
        if not and_activate or proposal_id is None:
            return retired, None
        return retired, activate_with_workspace_refresh(
            client, instance_id, proposal_id, workspace=workspace_root
        )

    result, activation = _server_call(call, command_name="playbill claim retire")
    payload: dict[str, Any] = {"retire": result.model_dump(mode="json")}
    if activation is not None:
        payload["activation"] = activation.model_dump(mode="json")
    elif and_activate:
        payload["activation_note"] = (
            "not activated: this retirement produced no activatable proposal "
            f"(outcome {getattr(result, 'outcome', 'preflight')})"
        )
    if output_brief:
        proposal_id = _retire_proposal_id(result)
        _emit_brief(
            outcome=(
                "accepted"
                if activation is not None
                else str(getattr(result, "outcome", "preflight"))
            ),
            ids={"claim": claim_id, "proposal": proposal_id},
            next_command=(
                None
                if activation is not None or proposal_id is None
                else f"cruxible playbill proposal activate {proposal_id}"
            ),
        )
        return
    _emit_json(payload if (and_activate or activation is not None) else payload["retire"])


def _retire_proposal_id(result: Any) -> str | None:
    """Return the activatable proposal a submitted retirement produced, if any.

    A preflight produces none, and neither does an `already_retired` replay.
    """

    proposal = getattr(result, "proposal", None)
    if proposal is None:
        return None
    admission = proposal.proposal.get("admission") if isinstance(proposal.proposal, dict) else None
    if not isinstance(admission, dict):
        return None
    proposal_id = admission.get("proposal_id")
    return proposal_id if isinstance(proposal_id, str) else None


@playbill_group.group("authoring")
def authoring_group() -> None:
    """Author, preflight, submit, and resume ergonomic governed writes."""


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
@click.option("--claim-id")
@click.option("--capture-digest")
@handle_errors
@click.pass_context
def create_authoring_intent(
    ctx: click.Context,
    payload: str | None,
    example_name: str | None,
    claim_id: str | None,
    capture_digest: str | None,
    output_json: bool,
) -> None:
    """Create a durable authoring intent or print a schema-derived example.

    \b
    Input kind family: claim | procedure | subject | query_definition |
    approval_policy | procedure_runtime_policy | change_set (tagless).

    Use --example for a model-generated starting point.
    """

    if (payload is None) == (example_name is None):
        raise click.UsageError("provide exactly one of PAYLOAD or --example")
    if payload is not None and (claim_id is not None or capture_digest is not None):
        raise click.UsageError("--claim-id/--capture-digest require --example")
    if example_name is not None:
        try:
            example = authoring_example(
                cast(AuthoringExampleName, example_name),
                claim_id=claim_id,
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
        return
    assert payload is not None
    _echo_write_target("active", ctx.params)
    result = _server_call(
        lambda client, instance_id: client.create_playbill_authoring_input(
            instance_id, input=_read_authoring_input(payload).model_dump(mode="json")
        ),
        command_name="playbill authoring create",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("get")
@click.argument("intent_id")
@json_option
@handle_errors
def get_authoring_intent(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_authoring_intent(instance_id, intent_id),
        command_name="playbill authoring get",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("resume")
@click.argument("intent_id")
@json_option
@handle_errors
def resume_authoring_intent(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.resume_playbill_authoring_intent(instance_id, intent_id),
        command_name="playbill authoring resume",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("list")
@json_option
@handle_errors
def list_pending_authoring_intents(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_pending_playbill_authoring_intents(instance_id),
        command_name="playbill authoring list",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("compile")
@click.argument("payload", type=click.Path(exists=True, dir_okay=False))
@click.option("--intent-id", default=None)
@json_option
@handle_errors
def compile_authoring(payload: str, intent_id: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.compile_playbill_authoring_input(
            instance_id,
            input=_read_authoring_input(payload).model_dump(mode="json"),
            intent_id=intent_id,
        ),
        command_name="playbill authoring compile",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("bind")
@click.option("--file", "source_path", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--anchor", required=True)
@click.option("--window-lines", type=click.IntRange(min=0), default=None)
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
    )
    result = _server_call(
        lambda client, instance_id: client.compile_playbill_authoring(
            instance_id,
            payload=payload.model_dump(mode="json"),
            intent_id=None,
        ),
        command_name="playbill authoring bind",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("preflight")
@click.argument("intent_id")
@brief_option
@json_option
@handle_errors
def preflight_authoring_intent(intent_id: str, output_brief: bool, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.preflight_playbill_authoring_intent(
            instance_id, intent_id
        ),
        command_name="playbill authoring preflight",
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
                f"cruxible playbill authoring submit {intent_id}"
                if result.verdict == "passed"
                else f"cruxible playbill authoring create  # repair, then preflight {intent_id}"
            ),
        )
        return
    _emit_json(result.model_dump(mode="json"))
    if result.verdict == "refused":
        intent = _server_call(
            lambda client, instance_id: client.get_playbill_authoring_intent(
                instance_id, intent_id
            ),
            command_name="playbill authoring preflight",
        ).intent
        if intent.get("base_coordinate") != result.certificate.get("accepted_coordinate"):
            click.echo(
                f"Hint: run playbill authoring rebase {intent_id}; resume does not advance "
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
        lambda client, instance_id: client.rebase_playbill_authoring_intent(instance_id, intent_id),
        command_name="playbill authoring rebase",
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
        submitted = client.submit_playbill_authoring_intent(instance_id, intent_id)
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

    submitted, activation = _server_call(call, command_name="playbill authoring submit")
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
            "Collect it with `cruxible playbill proposal approve`, then activate."
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
        return f"cruxible playbill proposal activate {proposal_id}"
    if submitted.status.state == "awaiting_external_approval" and proposal_id:
        return f"cruxible playbill proposal approve {proposal_id}"
    if submitted.status.state == "preflight_refused":
        return f"cruxible playbill authoring preflight {submitted.intent['intent_id']}"
    return None


@authoring_group.command("status")
@click.argument("intent_id")
@json_option
@handle_errors
def authoring_intent_status(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.playbill_authoring_intent_status(instance_id, intent_id),
        command_name="playbill authoring status",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("confirm-insertion")
@click.argument("intent_id")
@click.argument("observation", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def confirm_authoring_insertion(
    intent_id: str,
    observation: str,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.confirm_playbill_authoring_insertion(
            instance_id,
            intent_id,
            observation=_read_mapping(observation),
        ),
        command_name="playbill authoring confirm-insertion",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("prepare-publication")
@click.argument("intent_id")
@click.argument("observation", type=click.Path(exists=True, dir_okay=False))
@json_option
@handle_errors
def prepare_authoring_publication(
    intent_id: str,
    observation: str,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.prepare_playbill_authoring_publication(
            instance_id,
            intent_id,
            observation=_read_mapping(observation),
        ),
        command_name="playbill authoring prepare-publication",
    )
    _emit_json(result.model_dump(mode="json"))


@authoring_group.command("abandon-insertion")
@click.argument("intent_id")
@json_option
@handle_errors
def abandon_authoring_insertion(intent_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.abandon_playbill_authoring_insertion(
            instance_id,
            intent_id,
        ),
        command_name="playbill authoring abandon-insertion",
    )
    _emit_json(result.model_dump(mode="json"))


@claim_group.command("list")
@click.option("--subject", "subject_path", default=None, help="Subject artifact path filter.")
@click.option("--predicate", default=None)
@click.option("--include-retired", is_flag=True)
@json_option
@handle_errors
def list_claims(
    subject_path: str | None,
    predicate: str | None,
    include_retired: bool,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_claims(
            instance_id,
            subject_path=subject_path,
            predicate=predicate,
            include_retired=include_retired,
        ),
        command_name="playbill claim list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for claim in result.claims:
        click.echo(f"{claim.envelope['identity']}  {claim.envelope['path']}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@claim_group.command("get")
@click.argument("identity")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def get_claim(identity: str, evaluation_time: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_claim(
            instance_id,
            identity,
            evaluation_time=evaluation_time,
        ),
        command_name="playbill claim get",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{result.envelope['identity']}  {result.envelope['path']}")
    _emit_admission_accounts(result.admission_accounts)


def _emit_admission_accounts(
    accounts: Sequence[contracts.PlaybillCaptureAdmissionAccount],
) -> None:
    for account in accounts:
        if account.status == "not_evidence":
            detail = "not evidence (copy citation)"
        else:
            rendered: list[str] = []
            for decision in account.decisions:
                if decision.status == "admitted":
                    rendered.append(f"{decision.evidence_kind}: admitted by {decision.rule_id}")
                else:
                    repair = (
                        f"closest {decision.closest_rule_id}"
                        if decision.closest_rule_id is not None
                        else "no rule admits this contract"
                    )
                    rendered.append(
                        f"{decision.evidence_kind}: NOT admitted "
                        f"({decision.refusal_code}; {repair})"
                    )
            detail = "; ".join(rendered) or "NOT admitted (contract declares no evidence kind)"
        click.echo(
            f"Capture {account.capture_digest} [{account.capture_contract_identity} "
            f"{account.capture_contract_digest}]: {detail}"
        )


@claim_group.command("history")
@click.argument("identity")
@json_option
@handle_errors
def claim_history(identity: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.playbill_claim_history(instance_id, identity),
        command_name="playbill claim history",
    )
    _emit_json(result.model_dump(mode="json"))


@claim_group.command("explain")
@click.argument("identity")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def explain_claim(identity: str, evaluation_time: str | None, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.explain_playbill_claim(
            instance_id, identity, evaluation_time=evaluation_time
        ),
        command_name="playbill claim explain",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        f"{result.claim.envelope['identity']}  verdict={result.verdict['verdict']} "
        f"at {result.evaluation_time}"
    )
    _emit_admission_accounts(result.admission_accounts)


@playbill_group.group("block")
def block_group() -> None:
    """Maintain local declarations without rendering or replacing authored prose."""


@block_group.command("repin")
@click.argument("source_id")
@click.argument("block_id")
@click.option("--claim", "claims", multiple=True, help="Accepted Claim backing identity.")
@click.option("--query", "queries", multiple=True, help="Accepted QueryDefinition identity.")
@click.option(
    "--params",
    "parameters",
    multiple=True,
    help="Canonical JSON object corresponding positionally to each --query.",
)
@click.option("--workspace-root", default=".", show_default=True, type=click.Path(file_okay=False))
@click.option("--evaluation-time", default=None, help="Explicit absolute ISO-8601 instant.")
@json_option
@handle_errors
def repin_projection(
    source_id: str,
    block_id: str,
    claims: tuple[str, ...],
    queries: tuple[str, ...],
    parameters: tuple[str, ...],
    workspace_root: str,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Refresh one declaration marker without writing its body or closing line."""

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
            claims=claims,
            queries=resolved,
            evaluation_time=instant,
        ),
        command_name="playbill block repin",
    )
    if output_json:
        _emit_json(stamp.model_dump(mode="json"))
        return
    click.echo(f"Repinned {source_id}#{block_id} at generation {stamp.declared_generation}.")


@playbill_group.group("policy")
def policy_group() -> None:
    """Read governed policies in force."""


@policy_group.command("list")
@json_option
@handle_errors
def list_policies_in_force(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_policies_in_force(instance_id),
        command_name="playbill policy list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for policy in result.policies:
        click.echo(
            f"{policy.declaring_artifact_identity}  {policy.field_path}  {policy.policy_kind}"
        )
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@playbill_group.group("query")
def query_group() -> None:
    """Propose, read, and execute governed named entrypoints."""


@query_group.command("propose")
@click.option(
    "--envelope",
    type=click.Path(exists=True, dir_okay=False),
    help="Deprecated and ignored by this compatibility shim.",
)
@click.option(
    "--example",
    type=click.Choice(["query-claims-by-type"]),
    help="Deprecated and ignored by this compatibility shim.",
)
@click.option(
    "--name",
    "proposal_name",
    help="Deprecated and ignored by this compatibility shim.",
)
@json_option
@handle_errors
def propose_query_definition(
    envelope: str | None,
    example: str | None,
    proposal_name: str | None,
    output_json: bool,
) -> None:
    """Deprecated: use playbill authoring create then authoring submit."""

    del envelope, example, proposal_name, output_json
    raise PlaybillDeprecatedWriteError(
        replacement="the authoring coordinator with payload kind 'query_definition'"
    )


@query_group.command("list")
@json_option
@handle_errors
def list_query_definitions(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_query_definitions(instance_id),
        command_name="playbill query list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for definition in result.query_definitions:
        click.echo(f"{definition.name}  {definition.artifact_digest}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@query_group.command("get")
@click.argument("name")
@json_option
@handle_errors
def get_query_definition(name: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_query_definition(instance_id, name),
        command_name="playbill query get",
    )
    _emit_json(result.model_dump(mode="json"))


@query_group.command("run")
@click.argument("name")
@click.option(
    "--parameters",
    "parameters_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="Mapping of resolved query parameters.",
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def run_query(
    name: str,
    parameters_path: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    parameters = None if parameters_path is None else _read_mapping(parameters_path)
    result = _server_call(
        lambda client, instance_id: client.run_playbill_query(
            instance_id,
            name,
            parameters=parameters,
            evaluation_time=evaluation_time,
        ),
        command_name="playbill query run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    receipt = result.receipt
    rows = result.result.get("rows") or []
    click.echo(f"{result.name}: {receipt['verdict']} with {len(rows)} row(s)")
    click.echo(f"Receipt definition: {receipt['definition_digest']}")
    click.echo(f"Receipt parameters: {receipt['parameter_digest']}")
    click.echo(f"Receipt result digest: {receipt['result_digest']}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


@playbill_group.group("procedure")
def procedure_group() -> None:
    """Inspect, bind, and run accepted query-only Procedures."""


@procedure_group.command("readiness")
@click.argument("name")
@click.option("--evaluation-time", required=True, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def procedure_readiness(name: str, evaluation_time: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.playbill_procedure_readiness(
            instance_id,
            name,
            evaluation_time=evaluation_time,
        ),
        command_name="playbill procedure readiness",
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
    request = _read_model(request_file, ProcedureBindRequestV1)
    result = _server_call(
        lambda client, instance_id: client.bind_playbill_procedure(
            instance_id,
            name,
            bindings=[item.model_dump(mode="json") for item in request.bindings],
        ),
        command_name="playbill procedure bind",
    )
    _emit_json(result.model_dump(mode="json"))


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
@json_option
@handle_errors
def run_procedure(
    name: str,
    input_file: str,
    evaluation_time: str | None,
    at_file: str | None,
    output_json: bool,
) -> None:
    at = None if at_file is None else _read_model(at_file, AcceptedCoordinate)
    result = _server_call(
        lambda client, instance_id: client.run_playbill_procedure(
            instance_id,
            name,
            evaluation_time=evaluation_time,
            at=None if at is None else at.model_dump(mode="json"),
            input=_read_mapping(input_file),
        ),
        command_name="playbill procedure run",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"{result.run_id}: {result.status}")
    click.echo(f"Next: {result.next_operation['kind']}")
    if result.receipt_digest is not None:
        click.echo(f"Receipt: {result.receipt_digest}")


@procedure_group.command("status")
@click.argument("run_id")
@json_option
@handle_errors
def procedure_run_status(run_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_procedure_run(instance_id, run_id),
        command_name="playbill procedure status",
    )
    _emit_json(result.model_dump(mode="json"))


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
@brief_option
@json_option
@handle_errors
def next_work(
    evaluation_time: str | None,
    access_profile_path: str | None,
    expiring_within: int,
    workspace_root: str,
    since_result_digest: str | None,
    output_brief: bool,
    output_json: bool,
) -> None:
    stamped_evaluation_time = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if evaluation_time is None
        else evaluation_time
    )
    profile = (
        CoverageAccessProfileV1(
            profile_id="cli-next",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfileV1).model_dump(mode="json")
    )
    workspace_observation = observe_playbill_next_workspace(Path(workspace_root))

    def _next_at_scanned_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.PlaybillNextResult:
        observed, coordinate = observe_playbill_next_workspace_with_coverage(
            client,
            instance_id,
            Path(workspace_root),
            observation=workspace_observation,
            access_profile=profile,
        )
        return client.next_playbill(
            instance_id,
            evaluation_time=stamped_evaluation_time,
            access_profile=profile,
            at=coordinate,
            expiring_within={"microseconds": expiring_within},
            workspace_observation=observed,
            since_result_digest=since_result_digest,
        )

    result = _server_call(
        _next_at_scanned_coordinate,
        command_name="playbill next",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if not result.items:
        click.echo(
            "No changes since the requested queue digest."
            if result.delta_since is not None
            else "No repair work in the observed domains."
        )
    removed_ids = frozenset(result.removed_item_ids)
    for item in result.items:
        repair = item["repair"]
        change = (
            "removed  "
            if result.delta_since is not None and item["item_id"] in removed_ids
            else "added  "
            if result.delta_since is not None
            else ""
        )
        click.echo(
            f"{change}{item['severity']}  {item['reason']}  {item['subject_identity']}  "
            f"next={repair['operation']}"
        )
    if result.unobserved_domains:
        click.echo("Unobserved: " + ", ".join(result.unobserved_domains))


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
@json_option
@handle_errors
def curation_list(workspace_root: str, access_profile_path: str | None, output_json: bool) -> None:
    observation = observe_playbill_next_workspace(Path(workspace_root))
    profile = (
        CoverageAccessProfileV1(
            profile_id="cli-curation",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfileV1).model_dump(mode="json")
    )

    def _curation_at_scanned_coordinate(
        client: CruxibleClient, instance_id: str
    ) -> contracts.PlaybillCurationListResult:
        observed, _coordinate = observe_playbill_next_workspace_with_coverage(
            client,
            instance_id,
            Path(workspace_root),
            observation=observation,
            access_profile=profile,
        )
        return client.list_playbill_curation(
            instance_id,
            evaluation_time=datetime.now(UTC).isoformat(),
            access_profile=profile,
            workspace_observation=observed,
        )

    result = _server_call(
        _curation_at_scanned_coordinate,
        command_name="playbill curation list",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(
        f"Curation queue at generation {result.generation}: {len(result.items)} item(s); "
        f"observed {result.observation_coverage['observed_block_count']} declared block(s)."
    )


@curation_group.command("overrule")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@json_option
@handle_errors
def curation_overrule(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.overrule_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
        ),
        command_name="playbill curation overrule",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Curation item {result.item['item_id']}: {result.item['status']}")


@curation_group.command("accept-fixed")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@click.option("--proposal-id", required=True)
@click.option("--changeset-digest", required=True)
@json_option
@handle_errors
def curation_accept_fixed(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    proposal_id: str,
    changeset_digest: str,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.accept_fixed_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=proposal_id,
            accepted_changeset_digest=changeset_digest,
        ),
        command_name="playbill curation accept-fixed",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Curation item {result.item['item_id']}: {result.item['status']}")


@curation_group.command("suppress")
@click.argument("item_id")
@click.option("--expected-latest-event-digest", required=True)
@click.option("--reason", required=True)
@click.option("--scope", type=click.Choice(("item", "pattern", "instance")), required=True)
@click.option("--until-generation", type=click.IntRange(min=0))
@json_option
@handle_errors
def curation_suppress(
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: str,
    until_generation: int | None,
    output_json: bool,
) -> None:
    result = _server_call(
        lambda client, instance_id: client.suppress_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=cast(Any, scope),
            until_generation=until_generation,
        ),
        command_name="playbill curation suppress",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
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
    help="PlaybillAuditCursor JSON/YAML returned by a prior page.",
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
        CoverageAccessProfileV1(
            profile_id="cli-audit",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_model(access_profile_path, CoverageAccessProfileV1).model_dump(mode="json")
    )
    cursor = (
        None if cursor_path is None else _read_model(cursor_path, contracts.PlaybillAuditCursor)
    )
    result = _server_call(
        lambda client, instance_id: client.audit_playbill(
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
        command_name="playbill audit",
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
    help="PlaybillSinceCursor JSON/YAML returned by a prior page.",
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
        CoverageAccessProfileV1(
            profile_id="cli-since",
            permitted_access_classes=("instance", "public"),
        ).model_dump(mode="json")
        if access_profile_path is None
        else _read_since_access_profile(access_profile_path)
    )
    cursor = None if cursor_path is None else _read_mapping(cursor_path)
    result = _server_call(
        lambda client, instance_id: client.since_playbill(
            instance_id,
            generation=generation,
            access_profile=profile,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        command_name="playbill since",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    for row in result.rows:
        click.echo(f"{row.generation}  {row.disposition}  {row.artifact_kind}  {row.member_path}")
    if result.next_cursor is not None:
        click.echo(f"More: {result.next_cursor.cursor_digest}")


@playbill_group.command("discover")
@click.option("--query", "query_text", default=None, help="Exact or lexical match term.")
@click.option("--entrypoint", default=None, help="Named QueryDefinition entrypoint.")
@click.option(
    "--profile",
    type=click.Choice(["interfaces", "subjects", "all"]),
    default="interfaces",
)
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def discover(
    query_text: str | None,
    entrypoint: str | None,
    profile: str,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Find accepted interfaces and Subjects without knowing their names."""

    result = _server_call(
        lambda client, instance_id: client.discover_playbill(
            instance_id,
            query=query_text,
            entrypoint=entrypoint,
            profile=cast(Any, profile),
            evaluation_time=evaluation_time,
        ),
        command_name="playbill discover",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if isinstance(result, contracts.PlaybillInterfaceInventory):
        if result.provider_status == "not_installed":
            click.echo("No provider interfaces installed.")
        else:
            for interface in result.interfaces:
                click.echo(
                    f"{interface.identity}  {interface.interface_digest}  "
                    f"({interface.interface_basis})"
                )
        return
    for hit in result.page.get("hits", []):
        click.echo(f"{hit['kind']}  {hit['label']}  {hit['address']['artifact_path']}")
    click.echo(f"Vocabulary entries: {result.vocabulary_entry_count}")


def _headless_search(
    *,
    mode: str,
    query_text: str | None,
    kinds: tuple[str, ...],
    statuses: tuple[str, ...],
    subject_path: str | None,
    cursor_json: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    selected_kinds = tuple(sorted(set(kinds or ("claim", "demand", "procedure"))))
    selected_statuses = tuple(sorted(set(statuses)))
    subject = (
        None
        if subject_path is None
        else SemanticAddress.whole_artifact(subject_path).model_dump(mode="json")
    )
    try:
        parsed_cursor = None if cursor_json is None else json.loads(cursor_json)
    except ValueError as exc:
        raise click.ClickException("--cursor must be one complete cursor JSON object") from exc
    if parsed_cursor is not None and not isinstance(parsed_cursor, dict):
        raise click.ClickException("--cursor must be one complete cursor JSON object")

    def search_request(
        request_mode: str, request_kinds: tuple[str, ...]
    ) -> contracts.PlaybillSearchResult:
        return _server_call(
            lambda client, instance_id: client.search_playbill(
                instance_id,
                mode=cast(Any, request_mode),
                query=query_text if request_mode == mode else None,
                kinds=request_kinds,
                subject=subject,
                statuses=selected_statuses,
                cursor=parsed_cursor if request_mode == mode else None,
                evaluation_time=evaluation_time,
            ),
            command_name=f"playbill {request_mode}",
        )

    result = search_request(mode, selected_kinds)
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    orientation = (
        result if mode == "orient" else search_request("orient", ("claim", "demand", "procedure"))
    )
    if orientation.orientation is None:
        raise click.ClickException("Playbill orient returned no orientation summary")
    click.echo(_render_orientation_header(orientation.orientation))
    if mode == "orient":
        return
    for row in result.rows:
        click.echo(f"{row['kind']}  {row['status']}  {row['identity']}  {row['title']}")
    if result.next_cursor is not None:
        click.echo("Next cursor: " + canonical_json(result.next_cursor))


def _render_orientation_header(orientation: Mapping[str, Any]) -> str:
    counts = {item["key"]: item["count"] for item in orientation["counts_by_kind"]}
    availability = {item["kind"]: item["availability"] for item in orientation["kind_availability"]}
    demand: object = (
        "not_installed"
        if availability.get("demand") == "not_installed"
        else counts.get("demand", 0)
    )
    return (
        f"Playbill generation={orientation['generation']} "
        f"claim={counts.get('claim', 0)} "
        f"procedure={counts.get('procedure', 0)} "
        f"demand={demand} "
        f"conflicted={orientation['conflicted_count']}"
    )


_SEARCH_KIND = click.Choice(["claim", "procedure", "demand"])
_SEARCH_STATUS = click.Choice(["accepted", "conflicted", "overturned", "refused", "retired"])


@playbill_group.command("search")
@click.argument("query_text")
@click.option("--kind", "kinds", type=_SEARCH_KIND, multiple=True)
@click.option("--status", "statuses", type=_SEARCH_STATUS, multiple=True)
@click.option("--subject-path", default=None, help="Exact governed Subject artifact path.")
@click.option("--cursor", "cursor_json", default=None, help="Opaque cursor JSON from a prior page.")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def search(
    query_text: str,
    kinds: tuple[str, ...],
    statuses: tuple[str, ...],
    subject_path: str | None,
    cursor_json: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Find accepted Claims, Procedures, or installed demands."""

    _headless_search(
        mode="search",
        query_text=query_text,
        kinds=kinds,
        statuses=statuses,
        subject_path=subject_path,
        cursor_json=cursor_json,
        evaluation_time=evaluation_time,
        output_json=output_json,
    )


@playbill_group.command("list")
@click.option("--kind", "kinds", type=_SEARCH_KIND, multiple=True)
@click.option("--status", "statuses", type=_SEARCH_STATUS, multiple=True)
@click.option("--subject-path", default=None, help="Exact governed Subject artifact path.")
@click.option("--cursor", "cursor_json", default=None, help="Opaque cursor JSON from a prior page.")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def search_list(
    kinds: tuple[str, ...],
    statuses: tuple[str, ...],
    subject_path: str | None,
    cursor_json: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """List accepted write/read artifacts in deterministic pages."""

    _headless_search(
        mode="list",
        query_text=None,
        kinds=kinds,
        statuses=statuses,
        subject_path=subject_path,
        cursor_json=cursor_json,
        evaluation_time=evaluation_time,
        output_json=output_json,
    )


@playbill_group.command("orient")
@click.option("--kind", "kinds", type=_SEARCH_KIND, multiple=True)
@click.option("--status", "statuses", type=_SEARCH_STATUS, multiple=True)
@click.option("--subject-path", default=None, help="Exact governed Subject artifact path.")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def orient(
    kinds: tuple[str, ...],
    statuses: tuple[str, ...],
    subject_path: str | None,
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Summarize accepted state and return exact follow-up filters."""

    _headless_search(
        mode="orient",
        query_text=None,
        kinds=kinds,
        statuses=statuses,
        subject_path=subject_path,
        cursor_json=None,
        evaluation_time=evaluation_time,
        output_json=output_json,
    )


@playbill_group.command("expand")
@click.argument("artifact_path")
@click.option("--facet", "facets", multiple=True, help="Repeat to request one facet.")
@click.option("--evaluation-time", default=None, help="Explicit ISO-8601 evaluation time.")
@json_option
@handle_errors
def expand(
    artifact_path: str,
    facets: tuple[str, ...],
    evaluation_time: str | None,
    output_json: bool,
) -> None:
    """Expand one accepted address into a bounded context capsule."""

    address = SemanticAddress.whole_artifact(artifact_path).model_dump(mode="json")
    result = _server_call(
        lambda client, instance_id: client.expand_playbill(
            instance_id,
            address=address,
            facets=sorted(set(facets), key=lambda item: item.encode("utf-8")),
            evaluation_time=evaluation_time,
        ),
        command_name="playbill expand",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("floor")
def floor_group() -> None:
    """Materialize the deterministic greppable floor of accepted state."""


@floor_group.command("export")
@click.option("--output", required=True, type=click.Path(file_okay=False))
@click.option("--force", is_flag=True, help="Overwrite a non-empty output directory.")
@json_option
@handle_errors
def export_floor(
    output: str,
    force: bool,
    output_json: bool,
) -> None:
    """Write the accepted floor to a deterministic local tree."""

    result = _server_call(
        lambda client, instance_id: client.export_playbill_floor(instance_id),
        command_name="playbill floor export",
    )
    destination = Path(output).expanduser()
    _write_floor(destination, result, force=force)
    if output_json:
        _emit_json(result.manifest)
        return
    click.echo(f"Wrote {len(result.files)} floor file(s) to {destination}")
    click.echo(f"Floor digest: {result.manifest['floor_digest']}")
    click.echo(f"Coordinate: {result.coordinate.git_oid}")


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
) -> tuple[WorkingSourceObservationV1, ...]:
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
    observations: tuple[WorkingSourceObservationV1, ...],
    *,
    command_name: str,
    scan_budget: CoverageScanBudgetV1 | None = None,
) -> CoverageResultV3:
    result = _server_call(
        lambda client, instance_id: client.resolve_playbill_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
            scan_budget=None if scan_budget is None else scan_budget.model_dump(mode="json"),
        ),
        command_name=command_name,
    )
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
    result = _resolved_coverage(observations, command_name="playbill coverage resolve")
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    if output_brief:
        drifted = [span for span in result.spans if span.match_state == "drifted"]
        _emit_brief(
            outcome=f"{result.health} ({result.summary.exact} exact, {len(drifted)} drifted)",
            ids={"coordinate": result.at.git_oid, "epoch": str(result.epoch)},
            next_command=(
                "cruxible playbill next --brief" if drifted or result.health != "complete" else None
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
    result = _resolved_coverage(observations, command_name="playbill coverage status")
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

    def resolve(observations: Sequence[WorkingSourceObservationV1]) -> CoverageResultV3:
        return _resolved_coverage(
            tuple(observations),
            command_name="playbill hook post-tool-use",
            scan_budget=config.scan_budget,
        )

    return resolve


def _hook_floor_generation_resolver() -> ResolveFloorGenerations:
    """Resolve old and current sequence numbers through the existing orient wire."""

    def orientation(at: AcceptedCoordinate | None) -> int:
        result = _server_call(
            lambda client, instance_id: client.search_playbill(
                instance_id,
                mode="orient",
                kinds=("claim", "demand", "procedure"),
                at=None if at is None else at.model_dump(mode="json"),
            ),
            command_name="playbill hook floor freshness",
        )
        if result.orientation is None:
            raise click.ClickException("Playbill orient returned no floor generation")
        generation = result.orientation.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise click.ClickException("Playbill orient returned an invalid floor generation")
        return generation

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
    help="Workspace root holding .playbill/coverage.json.",
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
            text = middleware.after_tool(event).appended_coverage_text
    except Exception:  # noqa: BLE001 - fail open; a broken hook is not the agent's problem
        text = ""
    _emit_json(post_tool_use_response(annotated_tool_output(payload, text)))


__all__ = ["playbill_group"]
