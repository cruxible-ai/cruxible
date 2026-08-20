"""Playbill Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import click
import yaml

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _echo_write_target,
    _emit_json,
    _require_instance_id,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.playbill import native
from cruxible_core.playbill.attestations import ApprovalStatement
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingPathBindingV1,
    WorkingSourceObservationV1,
    observe_working_source,
    parse_grep_batch,
    read_working_path,
    selection_for_lines,
)
from cruxible_core.playbill.coverage.claude_code import (
    annotated_tool_output,
    post_tool_use_response,
    read_post_tool_use_event,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageResultV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.coverage.middleware import (
    CoverageWorkspaceConfigV1,
    ResolveCoverage,
    coverage_middleware,
    load_coverage_config,
)
from cruxible_core.playbill.coverage.render import (
    render_coverage_manifest,
    render_coverage_result,
)
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.playbill.native.grammar import NativeRenderError
from cruxible_core.playbill.native.manifest import parse_native_manifest
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.review import (
    PlaybillProposalReview,
    render_playbill_proposal_review,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
from cruxible_core.playbill.source_catalog import (
    SourceCatalog,
    SourceCompilationBundle,
    compile_source_catalog,
    merge_source_catalogs,
)
from cruxible_core.playbill.types import PrincipalRecord

ResultT = TypeVar("ResultT")


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


def _read_model(path: str, model: type[ResultT]) -> ResultT:
    source = Path(path).expanduser()
    try:
        payload = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{source} must contain one mapping")
    validator = getattr(model, "model_validate")
    return cast(ResultT, validator(payload))


def _read_mapping(path: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        payload = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{source} must contain one mapping")
    return cast(dict[str, Any], payload)


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
    aliases: dict[str, Path] = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in aliases:
            raise click.BadParameter("root aliases must be unique NAME=PATH values")
        aliases[name] = Path(path).expanduser()
    return aliases


def _catalog(portable_path: str, local_path: str | None) -> SourceCatalog:
    portable = _read_model(portable_path, SourceCatalog)
    local = _read_model(local_path, SourceCatalog) if local_path is not None else None
    return merge_source_catalogs(portable, local)


def _compile_remote_context(
    client: CruxibleClient,
    instance_id: str,
    *,
    catalog: SourceCatalog,
    repository_root: Path,
    aliases: dict[str, Path],
) -> SourceCompilationBundle:
    context = client.playbill_source_context(instance_id)
    accepted = {
        shell.document_id: shell
        for value in context.documents
        for shell in (DocumentShell.model_validate(value),)
    }
    return compile_source_catalog(
        catalog,
        repository_root=repository_root,
        root_aliases=aliases,
        accepted_base=AcceptedCoordinate.model_validate(
            context.accepted_coordinate.model_dump(mode="json")
        ),
        accepted_documents=accepted,
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
@click.option("--recovery-key-dir", default=None, help="Optional offline recovery custody dir.")
@click.option("--recovery-principal-id", default="recovery", show_default=True)
@click.option("--profile", type=click.Choice(["local", "cloud"]), default="local")
@json_option
@handle_errors
def init_playbill(
    key_dir: str,
    principal_id: str,
    recovery_key_dir: str | None,
    recovery_principal_id: str,
    profile: str,
    output_json: bool,
) -> None:
    """Create client owner/recovery keys locally, then bootstrap daemon state."""

    workspace = Path.cwd().resolve()
    owner = generate_client_principal_key(
        Path(key_dir).expanduser(),
        principal_id=principal_id,
        authority_roles=("owner",),
        forbidden_roots=(workspace,),
    )
    principals = [owner.principal]
    if recovery_key_dir is not None:
        recovery = generate_client_principal_key(
            Path(recovery_key_dir).expanduser(),
            principal_id=recovery_principal_id,
            authority_roles=("recovery",),
            forbidden_roots=(workspace,),
        )
        principals.append(recovery.principal)
    result = _server_call(
        lambda client, selected: client.init_playbill(
            selected,
            principals=[item.model_dump(mode="json") for item in principals],
            operating_profile=cast(Any, profile),
        ),
        command_name="playbill init",
    )
    if output_json:
        _emit_json(result.model_dump(mode="json"))
        return
    click.echo(f"Playbill initialized at {result.coordinate.git_oid}")
    click.echo(f"Owner public key: {owner.principal.public_key}")
    click.echo(f"Owner private key retained locally at: {owner.private_key_path}")


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
@click.option("--envelope", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_document(envelope: str, proposal_name: str, output_json: bool) -> None:
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
@json_option
@handle_errors
def activate_proposal(proposal_id: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.activate_playbill_proposal(instance_id, proposal_id),
        command_name="playbill proposal activate",
    )
    _emit_json(result.model_dump(mode="json"))


@playbill_group.command("explain")
@click.argument("identity")
@click.option("--detail", type=click.Choice(["summary", "evidence", "proof"]), default="summary")
@click.option("--include-body", is_flag=True)
@json_option
@handle_errors
def explain(identity: str, detail: str, include_body: bool, output_json: bool) -> None:
    def call(
        client: CruxibleClient, instance_id: str
    ) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
        document = client.get_playbill_document(instance_id, identity)
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
    """List and govern owner/reviewer/recovery public keys."""


@principal_group.command("list")
@json_option
@handle_errors
def list_principals(output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.list_playbill_principals(instance_id),
        command_name="playbill principal list",
    )
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
            authority_roles=target.authority_roles,
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
    """Self-rotate a principal key; the acceptance law verifies the actor."""

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
@click.option("--envelope", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_subject(envelope: str, proposal_name: str, output_json: bool) -> None:
    shell = _read_mapping(envelope)
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_subject(
            instance_id,
            shell=shell,
            proposal_name=proposal_name,
        ),
        command_name="playbill subject propose",
    )
    _emit_json(result.model_dump(mode="json"))


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
@click.option("--envelope", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_claim_type(envelope: str, proposal_name: str, output_json: bool) -> None:
    claim_type = _read_mapping(envelope)
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_claim_type(
            instance_id,
            claim_type=claim_type,
            proposal_name=proposal_name,
        ),
        command_name="playbill claim-type propose",
    )
    _emit_json(result.model_dump(mode="json"))


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


@claim_group.command("propose")
@click.option("--authoring", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_claim(authoring: str, proposal_name: str, output_json: bool) -> None:
    request = _read_mapping(authoring)
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_claim(
            instance_id,
            authoring=request,
            proposal_name=proposal_name,
        ),
        command_name="playbill claim propose",
    )
    _emit_json(result.model_dump(mode="json"))


@claim_group.command("propose-batch")
@click.option(
    "--authoring",
    "authorings",
    required=True,
    multiple=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Repeat once per Claim; the whole set settles as one generation.",
)
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_claims(authorings: tuple[str, ...], proposal_name: str, output_json: bool) -> None:
    requests = [_read_mapping(path) for path in authorings]
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_claims(
            instance_id,
            authorings=requests,
            proposal_name=proposal_name,
        ),
        command_name="playbill claim propose-batch",
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
@json_option
@handle_errors
def get_claim(identity: str, output_json: bool) -> None:
    result = _server_call(
        lambda client, instance_id: client.get_playbill_claim(instance_id, identity),
        command_name="playbill claim get",
    )
    _emit_json(result.model_dump(mode="json"))


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
    _emit_json(result.model_dump(mode="json"))


@playbill_group.group("query")
def query_group() -> None:
    """Propose, read, and execute governed named entrypoints."""


@query_group.command("propose")
@click.option("--envelope", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", "proposal_name", required=True)
@json_option
@handle_errors
def propose_query_definition(envelope: str, proposal_name: str, output_json: bool) -> None:
    definition = _read_mapping(envelope)
    result = _server_call(
        lambda client, instance_id: client.propose_playbill_query_definition(
            instance_id,
            query=definition,
            proposal_name=proposal_name,
        ),
        command_name="playbill query propose",
    )
    _emit_json(result.model_dump(mode="json"))


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
    for hit in result.page.get("hits", []):
        click.echo(f"{hit['kind']}  {hit['label']}  {hit['address']['artifact_path']}")
    click.echo(f"Vocabulary entries: {result.vocabulary_entry_count}")


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
def export_floor(output: str, force: bool, output_json: bool) -> None:
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


@playbill_group.group("native")
def native_group() -> None:
    """Check out accepted knowledge as an editable in-repo working tree."""


def _native_state(
    client: CruxibleClient,
    instance_id: str,
    *,
    at: dict[str, Any] | None = None,
    boundary: native.NativeCoverageBoundaryV1 | None = None,
) -> native.NativeAcceptedStateV1:
    """Assemble the render input from the served reads that already exist.

    No operation is added for this. The lens is a pure function of accepted
    state, and every part of that state is already reachable: the artifact reads
    return the envelopes and facts, and the floor export publishes the §11.6.3
    coverage boundary the render manifest inherits. Rendering here rather than
    on the daemon also keeps the §11.9.5 explicit-sync law structural -- the
    daemon has no path by which it could write into a user's repository, because
    it never produced the bytes.

    A Claim's verdict comes from its accepted ``current_verdict`` projection,
    which the list read already carries, so this costs one call per artifact kind
    rather than one call per Claim.

    ``at`` and ``boundary`` are what a compile needs and a render does not: to
    reconstruct the *baseline*, this reads an older generation and is handed the
    boundary that generation's render already committed to, because exporting a
    fresh floor would answer about the head.
    """

    if boundary is None:
        floor = client.export_playbill_floor(instance_id, at=at)
        boundary_file = next(
            (item for item in floor.files if item.path == "coverage-manifest.json"), None
        )
        if boundary_file is None:
            raise click.ClickException("The floor export carries no coverage boundary to inherit.")
        boundary = native.native_boundary_from_floor(
            json.loads(base64.b64decode(boundary_file.content_base64, validate=True))
        )
        coordinate = AcceptedCoordinate.model_validate(floor.coordinate.model_dump(mode="json"))
    else:
        if at is None:  # pragma: no cover - callers pass both or neither
            raise click.ClickException("A declared coverage boundary needs its own coordinate.")
        coordinate = AcceptedCoordinate.model_validate(at)
    return native.build_native_state(
        instance_id=instance_id,
        at=coordinate,
        boundary=boundary,
        subjects=[
            native.artifact_record_from_projection("Subject", view.envelope)
            for view in client.list_playbill_subjects(instance_id, at=at).subjects
        ],
        claim_types=[
            native.artifact_record_from_projection(
                "ClaimType",
                view.envelope,
                path=view.path,
                identity=view.identity,
                artifact_digest=view.artifact_digest,
            )
            for view in client.list_playbill_claim_types(instance_id, at=at).claim_types
        ],
        query_definitions=[
            native.artifact_record_from_projection(
                "QueryDefinition",
                view.envelope,
                path=view.path,
                identity=view.identity,
                artifact_digest=view.artifact_digest,
            )
            for view in client.list_playbill_query_definitions(instance_id, at=at).query_definitions
        ],
        documents=[
            native.artifact_record_from_projection("Document", view.envelope)
            for view in client.list_playbill_documents(instance_id, at=at).documents
        ],
        claims=[
            native.claim_record_from_projection(view.envelope, view.facts)
            for view in client.list_playbill_claims(instance_id, at=at).claims
        ],
    )


def _read_native_tree(root: Path) -> dict[str, bytes]:
    """Read the rendered working tree as it currently is, manifest included."""

    if not root.is_dir():
        raise click.ClickException(f"Not a rendered knowledge directory: {root}")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.endswith(".md") or relative == native.NATIVE_RENDER_MANIFEST_PATH:
            files[relative] = path.read_bytes()
    return files


def _native_manifest(root: Path) -> native.NativeRenderManifestV1:
    target = root / native.NATIVE_RENDER_MANIFEST_PATH
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise click.ClickException(
            f"No render manifest at {target}. Run `cruxible playbill native render` first."
        ) from exc
    try:
        return parse_native_manifest(content)
    except NativeRenderError as exc:
        raise click.ClickException(str(exc)) from exc


def _write_atomically(target: Path, content: bytes) -> None:
    """Publish one local file whole, or not at all.

    A reader of a stash directory either sees a complete entry or does not see
    it; there is no window in which half an entry could be read back as the
    edits somebody asked to keep.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _stash_entries(root: Path) -> list[native.NativeStashFileV1]:
    """Read every stash entry under a render root, deleting any that do not verify.

    A stash is a disposable local cache, so an entry whose digest no longer
    reproduces is removed rather than reported: it cannot be restored, and
    keeping it would only offer bytes nobody can vouch for.
    """

    directory = root / native.NATIVE_STASH_DIRECTORY
    if not directory.is_dir():
        return []
    entries: list[native.NativeStashFileV1] = []
    for path in sorted(directory.glob(f"{native.NATIVE_STASH_FILE_PREFIX}*.json")):
        try:
            entries.append(native.parse_native_stash(path.read_bytes()))
        except (OSError, native.NativeStashError):
            path.unlink(missing_ok=True)
    return entries


def _write_stash(root: Path, body: native.NativeStashBodyV1, *, written_at: str) -> str:
    """Capture one stash entry under the render root and return its identity."""

    stash_id = native.native_stash_digest(body).tagged
    _write_atomically(
        root / native.native_stash_entry_path(stash_id),
        native.render_native_stash(body, written_at=written_at),
    )
    return stash_id


def _resolve_stash(root: Path, stash_id: str) -> native.NativeStashFileV1:
    try:
        return native.resolve_native_stash(_stash_entries(root), stash_id)
    except native.NativeStashError as exc:
        raise click.ClickException(str(exc)) from exc


@native_group.command("render")
@click.option("--output", required=True, type=click.Path(file_okay=False))
@click.option(
    "--evaluation-time",
    default=None,
    help="Explicit ISO-8601 read time. Defaults to now; the renderer never reads a clock.",
)
@click.option(
    "--stash",
    "stash",
    is_flag=True,
    help="Keep local edits in editable regions in the stash, then re-render over them.",
)
@click.option(
    "--discard",
    is_flag=True,
    help="Discard local edits in editable regions instead of refusing to overwrite them.",
)
@json_option
@handle_errors
def render_native(
    output: str,
    evaluation_time: str | None,
    stash: bool,
    discard: bool,
    output_json: bool,
) -> None:
    """Check out accepted knowledge as a browsable, editable Markdown tree.

    Rendering and writing are separate acts. The lens returns bytes; this command
    writes them, and it refuses to overwrite an editable region you have edited
    unless you say what should happen to the edit: compile it into a proposal,
    --stash it, or --discard it.
    """

    read_at = (
        datetime.now(UTC) if evaluation_time is None else datetime.fromisoformat(evaluation_time)
    )
    if read_at.tzinfo is None:
        raise click.BadParameter("an evaluation time must carry an explicit offset")
    state = _server_call(_native_state, command_name="playbill native render")
    ctx = native.whole_scope_context(
        instance_id=state.instance_id,
        at=state.at,
        evaluation_time=read_at,
        access_profile=CoverageAccessProfileV1(profile_id=state.boundary.access_profile_id),
    )
    render = native.build_native_render(state, ctx)

    destination = Path(output).expanduser()
    existing = _read_native_tree(destination) if destination.is_dir() else {}
    stash_id: str | None = None
    if native.NATIVE_RENDER_MANIFEST_PATH in existing:
        existing_manifest = _native_manifest(destination)
        plan = native.plan_native_render(
            existing,
            manifest=existing_manifest,
            render=render,
            discard=discard,
            stash=stash,
        )
        if plan.stashed_region_ids:
            # Captured before a byte of the re-render lands: a stash that ran
            # after the overwrite would be a stash of the overwrite.
            body = native.native_stash_body(existing, manifest=existing_manifest)
            if body is not None:
                stash_id = _write_stash(destination, body, written_at=read_at.isoformat())
    else:
        plan = native.NativeRenderPlanV1(
            write_paths=tuple(sorted(render.files, key=lambda item: item.encode("utf-8")))
        )

    root = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for relative in plan.write_paths:
        target = (destination / relative).resolve()
        if not target.is_relative_to(root):
            raise click.ClickException(f"Refusing to write outside the render root: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(render.files[relative])
    for relative in plan.delete_paths:
        (destination / relative).unlink(missing_ok=True)

    if output_json:
        _emit_json(
            {
                "manifest": render.manifest.model_dump(mode="json"),
                "plan": plan.model_dump(mode="json"),
                "stash_id": stash_id,
            }
        )
        return
    click.echo(f"Rendered {len(render.files)} file(s) into {destination}")
    click.echo(f"Wrote {len(plan.write_paths)}, unchanged {len(plan.unchanged_paths)}")
    if stash_id is not None:
        click.echo(
            f"Stashed {len(plan.stashed_region_ids)} dirty region(s) as {stash_id}; "
            "restore them with `playbill native stash restore`."
        )
    click.echo(f"Generation: {render.manifest.coordinate.generation_root}")
    click.echo(f"Lens: {render.manifest.lens.lens_id} v{render.manifest.lens.lens_version}")
    click.echo(f"Render digest: {render.manifest.render_digest}")


@native_group.command("status")
@click.argument("directory", type=click.Path(file_okay=False))
@json_option
@handle_errors
def native_status_cmd(directory: str, output_json: bool) -> None:
    """Report which rendered fields are clean, edited, or tampered with.

    A working-tree question, answered against the render baseline in the
    directory, so it needs no daemon and grants nothing. The derived display
    beside a dirty field is invalidated through the coverage resolver rather
    than by this command's own reckoning: the rendered file is a working source,
    and the resolver is the one place a working occurrence is compared with what
    accepted state says about it.
    """

    root = Path(directory).expanduser()
    files = _read_native_tree(root)
    manifest = _native_manifest(root)
    parsed = native.parse_native_tree(files, manifest=manifest)
    status = native.native_status(files, manifest=manifest, parsed=parsed)
    invalidation = native.resolve_native_invalidation(
        files,
        manifest=manifest,
        ctx=native.render_context_from_manifest(manifest),
        parsed=parsed,
    )
    if output_json:
        _emit_json(
            {
                "status": status.model_dump(mode="json"),
                "invalidation": invalidation.model_dump(mode="json"),
            }
        )
        return
    click.echo(
        f"Playbill native render: generation {status.baseline_generation_root}, "
        f"lens {status.lens_id} v{status.lens_version}"
    )
    click.echo(f"rendered at {status.evaluation_time}, render digest {status.render_digest}")
    for item in status.files:
        click.echo(
            f"{item.state:>11}  {item.path}  "
            f"{item.region_count} region(s): {item.clean_regions} clean, "
            f"{item.dirty_regions} dirty, {item.tampered_regions} tampered, "
            f"{item.ambiguous_regions} ambiguous, {item.unbaselined_regions} unbaselined"
        )
    for path in status.missing_paths:
        click.echo(f"    missing  {path}  (removal is never inferred as retirement)")
    for path in status.untracked_paths:
        click.echo(f"  untracked  {path}  (no render baseline; not compilable)")
    for diagnostic in status.diagnostics:
        if diagnostic.severity == "refusal":
            click.echo(f"    refusal  {diagnostic.code}  {diagnostic.message}")
    for line in render_coverage_result(invalidation.coverage):
        click.echo(line)
    if invalidation.invalidated_region_ids:
        click.echo(
            f"invalidated derived fields: {len(invalidation.invalidated_region_ids)} "
            f"beside {len(invalidation.drifted_addresses)} edited statement(s); "
            "no governance fact reaches the edited material"
        )


@native_group.group("stash")
def native_stash_group() -> None:
    """Keep and restore the local edits a re-render would otherwise overwrite."""


@native_stash_group.command("list")
@click.argument("directory", type=click.Path(file_okay=False))
@json_option
@handle_errors
def native_stash_list(directory: str, output_json: bool) -> None:
    """List the stashed edits held beside a rendered knowledge directory.

    A working-tree question with no daemon in it: the stash is local material
    under the render root, and nothing accepted refers to it.
    """

    root = Path(directory).expanduser()
    if not root.is_dir():
        raise click.ClickException(f"Not a rendered knowledge directory: {root}")
    entries = _stash_entries(root)
    if output_json:
        _emit_json({"stashes": [item.model_dump(mode="json") for item in entries]})
        return
    if not entries:
        click.echo(f"No stashed edits under {root / native.NATIVE_STASH_DIRECTORY}")
        return
    for entry in entries:
        click.echo(
            f"{entry.stash_id}  {len(entry.body.regions)} region(s)  "
            f"generation {entry.body.at.generation_root}  written {entry.written_at or '(unset)'}"
        )
        for region in entry.body.regions:
            click.echo(f"    {region.region_kind:>19}  {region.path}  {region.byte_length} byte(s)")


@native_stash_group.command("show")
@click.argument("directory", type=click.Path(file_okay=False))
@click.argument("stash_id")
@json_option
@handle_errors
def native_stash_show(directory: str, stash_id: str, output_json: bool) -> None:
    """Show one stashed edit, including the exact bytes it kept."""

    entry = _resolve_stash(Path(directory).expanduser(), stash_id)
    if output_json:
        _emit_json(entry.model_dump(mode="json"))
        return
    click.echo(f"stash: {entry.stash_id}")
    click.echo(f"generation: {entry.body.at.generation_root}")
    click.echo(f"lens: {entry.body.lens.lens_id} v{entry.body.lens.lens_version}")
    click.echo(f"render digest: {entry.body.render_digest}")
    for region in entry.body.regions:
        click.echo(f"--- {region.region_kind}  {region.path}  ({region.region_id})")
        click.echo(region.body.decode("utf-8", errors="replace").rstrip("\n"))


@native_stash_group.command("restore")
@click.argument("directory", type=click.Path(file_okay=False))
@click.argument("stash_id")
@click.option(
    "--drop",
    is_flag=True,
    help="Delete the stash entry once every region in it was restored.",
)
@json_option
@handle_errors
def native_stash_restore(directory: str, stash_id: str, drop: bool, output_json: bool) -> None:
    """Re-apply stashed edits to the current render, by region identity.

    Region identity carries no path, so a stashed edit lands correctly even
    after the field moved to another file. A stashed field the current render no
    longer has -- or one that no longer binds unambiguously -- is reported and
    left in the stash rather than placed somewhere it might not belong.
    """

    root = Path(directory).expanduser()
    files = _read_native_tree(root)
    manifest = _native_manifest(root)
    entry = _resolve_stash(root, stash_id)
    restored = native.restore_native_stash(files, manifest=manifest, stash=entry)

    for relative in restored.write_paths:
        (root / relative).write_bytes(restored.files[relative])
    dropped = bool(drop and not restored.unresolved_region_ids)
    if dropped:
        (root / native.native_stash_entry_path(entry.stash_digest)).unlink(missing_ok=True)

    if output_json:
        _emit_json(
            {
                "restore": restored.model_dump(mode="json", exclude={"files"}),
                "dropped": dropped,
            }
        )
    else:
        click.echo(f"Restored {len(restored.restored_region_ids)} region(s) from {entry.stash_id}")
        for relative in restored.write_paths:
            click.echo(f"    wrote  {relative}")
        for diagnostic in restored.diagnostics:
            click.echo(f"  {diagnostic.severity:>9}  {diagnostic.code}  {diagnostic.message}")
        if dropped:
            click.echo("Dropped the stash entry: every region in it was restored.")
    if restored.unresolved_region_ids:
        raise SystemExit(1)


def _read_dispositions(path: str | None) -> tuple[native.NativeDraftDispositionV1, ...]:
    """Read draft dispositions supplied beside the tree rather than inside it."""

    if path is None:
        return ()
    source = Path(path).expanduser()
    try:
        payload = yaml.safe_load(source.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Could not read {source}: {exc}") from exc
    if not isinstance(payload, list):
        raise click.ClickException(f"{source} must contain a list of draft dispositions")
    try:
        return tuple(native.NativeDraftDispositionV1.model_validate(item) for item in payload)
    except ValueError as exc:
        raise click.ClickException(f"{source} is not a valid disposition list: {exc}") from exc


def _render_compile_result(result: native.NativeCompileResultV1) -> list[str]:
    """One rendering of the compile result; preview and submit both use it."""

    lines = [
        f"Compile: baseline generation {result.baseline.generation_root}",
        f"         accepted head      {result.head.generation_root}",
        f"         lens {result.lens_id} v{result.lens_version}, read at {result.evaluation_time}",
    ]
    for item in result.three_way:
        lines.append(
            f"  three-way  {item.outcome:>18}  {item.claim_path}  "
            f"{len(item.region_ids)} edited region(s)"
        )
    for draft in result.drafts:
        stated = "none" if draft.disposition is None else draft.disposition.kind
        lines.append(
            f"      draft  {draft.draft_id}  {draft.path}  disposition: {stated}  "
            f"{len(draft.candidates)} candidate(s)"
        )
        for candidate in draft.candidates:
            mark = "blocking" if candidate.blocking else "advisory"
            lines.append(f"             {mark}  {candidate.identity}  ({candidate.kind})")
        for generated in draft.generated_distinct_from:
            lines.append(f"             lowers to semantic.distinct_from {generated.artifact_path}")
    for member in result.members:
        lines.append(f"     member  {member.kind}  {member.predicate}  {member.subject_path}")
    for notice in result.notices:
        lines.append(f"     notice  {notice.code}  {notice.message}")
    for refusal in result.refusals:
        lines.append(f"    refusal  {refusal.code}  {refusal.message}")
        lines.append(f"             action: {refusal.required_action}")
        for named in refusal.candidates:
            lines.append(f"             candidate: {named}")
    if result.baseline != result.head:
        lines.append(
            "The accepted head moved under this baseline; submitting binds the baseline and "
            "the proposal receive path performs the deterministic rebase."
        )
    if result.rebase_expected:
        lines.append(
            "At least one edited Claim also changed at the head, so that rebase has real "
            "work to do and may report a typed member conflict instead of a candidate."
        )
    return lines


@native_group.command("compile")
@click.argument("directory", type=click.Path(file_okay=False))
@click.option(
    "--submit/--preview",
    "submit",
    default=False,
    show_default=True,
    help="Preview the change set, or submit it as one governed proposal.",
)
@click.option(
    "--name", "proposal_name", default=None, help="Proposal ref name, required to submit."
)
@click.option(
    "--dispositions",
    "dispositions_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="A list of draft dispositions, for drafts not dispositioned in the file itself.",
)
@json_option
@handle_errors
def compile_native(
    directory: str,
    submit: bool,
    proposal_name: str | None,
    dispositions_path: str | None,
    output_json: bool,
) -> None:
    """Compile local edits into one governed proposal, or preview the one it would make.

    Editing proposed nothing and this command accepts nothing: compile is the
    middle of three gates. It binds the baseline the tree was rendered from, so
    when the accepted head has moved the proposal receive path performs the
    deterministic three-way rebase -- no merge here decides what is admissible.
    """

    root = Path(directory).expanduser()
    files = _read_native_tree(root)
    manifest = _native_manifest(root)
    baseline_at = manifest.coordinate.model_dump(mode="json")
    dispositions = _read_dispositions(dispositions_path)

    def _compile(client: CruxibleClient, instance_id: str) -> native.NativeCompileResultV1:
        return native.compile_native_tree(
            files,
            manifest=manifest,
            baseline_state=_native_state(
                client,
                instance_id,
                at=baseline_at,
                boundary=native.native_boundary_from_manifest(manifest),
            ),
            accepted_state_at_head=_native_state(client, instance_id),
            ctx=native.render_context_from_manifest(manifest),
            dispositions=dispositions,
        )

    try:
        result = _server_call(_compile, command_name="playbill native compile")
    except NativeRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    proposal: dict[str, Any] | None = None
    if submit and result.compilable:
        if proposal_name is None:
            raise click.BadParameter("submitting a compile requires --name")
        _echo_write_target("active", {})
        batch = _server_call(
            lambda client, instance_id: client.propose_playbill_claims(
                instance_id,
                authorings=result.authorings,
                proposal_name=proposal_name,
                base=baseline_at,
            ),
            command_name="playbill native compile",
        )
        proposal = batch.proposal.proposal

    # A submitted compile can still be refused by the ledger -- a rebase member
    # conflict is exactly that, and it is the outcome compile deliberately does
    # not predict. Reporting it as success would make the one thing this loop
    # promises to be headlessly checkable unreadable from an exit code.
    refused_by_evaluation = (
        proposal is not None and proposal["evaluation"]["verdict"] != "candidate"
    )

    if output_json:
        _emit_json(
            {
                "compile": result.model_dump(mode="json"),
                "proposal": proposal,
            }
        )
    else:
        for line in _render_compile_result(result):
            click.echo(line)
        if not result.members and not result.refusals:
            click.echo("Nothing to compile: every rendered field matches accepted state.")
        if proposal is not None:
            evaluation = proposal["evaluation"]
            click.echo(f"Proposal: {proposal['admission']['proposal_id']}")
            click.echo(f"Verdict: {evaluation['verdict']}, rebased {evaluation['rebased']}")
            click.echo(f"Candidate: {evaluation['candidate_digest']}")
            for diagnostic in evaluation["diagnostics"]:
                click.echo(f"    refusal  {diagnostic['code']}  {diagnostic['message']}")
            if refused_by_evaluation:
                click.echo(
                    "Refused: the proposal was admitted and evaluated, and no candidate was "
                    "produced. Nothing is pending approval."
                )
        elif submit and result.refusals:
            click.echo("Refused: nothing was submitted.")
    if result.refusals or refused_by_evaluation:
        raise SystemExit(1)


def _signer_ids(review: contracts.PlaybillProposalReview) -> tuple[str, ...]:
    attestations = review.attestation_coverage.get("attestations") or ()
    return tuple(str(item["signer_id"]) for item in attestations)


def _proposal_target_ref(proposal_id: str) -> str:
    """Read the proposal ref one proposal was admitted against.

    Admissions in one lineage share a target ref, so this is what makes "an
    earlier proposal for the same work" a checked statement rather than a claim
    the caller makes. It is read through the ordinary proposal inspection; no
    operation was added for it.
    """

    inspection = _server_call(
        lambda client, instance_id: client.inspect_playbill_proposal(instance_id, proposal_id),
        command_name="playbill native review-current",
    )
    admission = inspection.proposal.get("admission") or {}
    return str(admission.get("target_ref", ""))


@native_group.command("review-current")
@click.argument("proposal_id")
@click.option(
    "--bound",
    "bound_candidate_digest",
    default=None,
    help="The candidate digest the collected review evidence was signed against.",
)
@click.option(
    "--superseded-proposal",
    "superseded_proposal_id",
    default=None,
    help="An earlier proposal in this lineage; its candidate digest and signers are read.",
)
@json_option
@handle_errors
def native_review_current(
    proposal_id: str,
    bound_candidate_digest: str | None,
    superseded_proposal_id: str | None,
    output_json: bool,
) -> None:
    """Check headlessly that review evidence binds the candidate that would settle.

    Approvals are stored under the exact candidate digest they signed, so a
    rebase does not weaken prior evidence -- it moves the candidate out from
    under it. Naming the earlier act is how it re-enters the answer, which is
    then reported as superseded_by_rebase.

    There are two ways to name it. `--bound` states the digest directly, which
    needs nothing but the digest. `--superseded-proposal` names the earlier
    proposal instead, and this command reads its candidate digest *and* its
    signers through the ordinary review operation, refusing a proposal admitted
    against a different target ref -- a proposal from another lineage is not
    evidence about this one.

    Neither can enumerate the lineage. Admissions for one target ref are not
    listable through any served read, so an earlier proposal has to be named;
    closing that gap wants one read operation over admissions, which is a served
    surface this batch does not add.
    """

    if bound_candidate_digest is not None and superseded_proposal_id is not None:
        raise click.BadParameter("name the superseded evidence once: --bound or its proposal")

    superseded_signer_ids: tuple[str, ...] = ()
    if superseded_proposal_id is not None:
        if superseded_proposal_id == proposal_id:
            raise click.BadParameter("a proposal does not supersede itself")
        lineage = _proposal_target_ref(proposal_id)
        earlier_ref = _proposal_target_ref(superseded_proposal_id)
        if not lineage or lineage != earlier_ref:
            raise click.ClickException(
                f"Proposal {superseded_proposal_id} was admitted against {earlier_ref or '(none)'} "
                f"and {proposal_id} against {lineage or '(none)'}; a proposal from another "
                "lineage is not superseded review evidence for this one."
            )
        earlier = _server_call(
            lambda client, instance_id: client.review_playbill_proposal(
                instance_id, str(superseded_proposal_id)
            ),
            command_name="playbill native review-current",
        )
        bound_candidate_digest = earlier.candidate_digest
        superseded_signer_ids = _signer_ids(earlier)

    review = _server_call(
        lambda client, instance_id: client.review_playbill_proposal(instance_id, proposal_id),
        command_name="playbill native review-current",
    )
    binding = _signer_ids(review)
    currency = native.native_review_currency(
        proposal_id=proposal_id,
        candidate_digest=review.candidate_digest,
        parent_semantic_root=review.parent_semantic_root,
        attestation_signer_ids=binding,
        bound_candidate_digest=bound_candidate_digest,
        superseded_signer_ids=(
            superseded_signer_ids if bound_candidate_digest != review.candidate_digest else ()
        ),
    )
    if output_json:
        _emit_json(currency.model_dump(mode="json"))
    else:
        click.echo(f"review-current: {currency.status}")
        click.echo(f"candidate: {currency.candidate_digest}")
        click.echo(f"parent semantic root: {currency.parent_semantic_root}")
        if currency.binding_signer_ids:
            click.echo(f"binding approvals: {', '.join(currency.binding_signer_ids)}")
        if currency.bound_candidate_digest is not None:
            click.echo(f"evidence bound: {currency.bound_candidate_digest}")
        if currency.superseded_signer_ids:
            click.echo(f"superseded approvals: {', '.join(currency.superseded_signer_ids)}")
        click.echo(f"action: {currency.required_action}")
    if currency.status != "current":
        raise SystemExit(1)


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

    bindings: list[WorkingPathBindingV1] = []
    for path, value in sorted(declared.items()):
        plane, separator, identity = value.partition(":")
        if not separator or plane not in {"ledger", "external"}:
            raise click.BadParameter(f"binding for {path} must name the ledger or external plane")
        bindings.append(
            WorkingPathBindingV1(
                path=path,
                source=LogicalSourceIdentityV1(
                    plane=cast(Any, plane),
                    identity=identity,
                ),
            )
        )
    if not bindings:
        raise click.BadParameter("coverage needs at least one declared --bind or --bindings entry")
    return WorkingPathBindingsV1(bindings=tuple(bindings))


def _line_range(value: str) -> tuple[str, int, int]:
    path, separator, span = value.rpartition(":")
    start_text, dash, end_text = span.partition("-")
    if not separator or not path or not start_text.isdigit():
        raise click.BadParameter("a range must be PATH:START-END")
    end_text = end_text if dash else start_text
    if not end_text.isdigit():
        raise click.BadParameter("a range must be PATH:START-END")
    return path, int(start_text), int(end_text)


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

    whole = set(bindings.paths) if whole_working_set else set(files)
    windows: dict[str, set[tuple[int, int]]] = {}
    for value in ranges:
        path, start_line, end_line = _line_range(value)
        windows.setdefault(path, set()).add((start_line, end_line))
    if grep_path is not None:
        text = Path(grep_path).expanduser().read_text(encoding="utf-8")
        for path, line in parse_grep_batch(text):
            windows.setdefault(path, set()).add((line, line))

    observations: list[WorkingSourceObservationV1] = []
    for path in sorted(whole | set(windows)):
        content = read_working_path(path, root=root)
        selections = (
            ()
            if path in whole
            else tuple(
                selection_for_lines(content, start_line=start, end_line=end)
                for start, end in sorted(windows[path])
            )
        )
        observations.append(
            observe_working_source(bindings.source_for(path), content, selections=selections)
        )
    if not observations:
        raise click.BadParameter("name at least one --file, --range, --grep-results, or --all")
    return tuple(observations)


def _resolved_coverage(
    observations: tuple[WorkingSourceObservationV1, ...],
    *,
    command_name: str,
    scan_budget: CoverageScanBudgetV1 | None = None,
) -> CoverageResultV1:
    result = _server_call(
        lambda client, instance_id: client.resolve_playbill_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
            scan_budget=None if scan_budget is None else scan_budget.model_dump(mode="json"),
        ),
        command_name=command_name,
    )
    return CoverageResultV1.model_validate(result.result)


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
    """Deliver coverage into a harness's own tool results."""


def _hook_resolver(config: CoverageWorkspaceConfigV1) -> ResolveCoverage:
    """Resolve through the served operation, as every other coverage caller does.

    The workspace's declared scan budget rides along here rather than inside the
    middleware, because bounding how many bytes are hashed looking for relocated
    content is a property of the operation, not of the adapter that calls it.
    """

    def resolve(observations: Sequence[WorkingSourceObservationV1]) -> CoverageResultV1:
        return _resolved_coverage(
            tuple(observations),
            command_name="playbill hook post-tool-use",
            scan_budget=config.scan_budget,
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
            )
            text = middleware.after_tool(event).appended_coverage_text
    except Exception:  # noqa: BLE001 - fail open; a broken hook is not the agent's problem
        text = ""
    _emit_json(post_tool_use_response(annotated_tool_output(payload, text)))


__all__ = ["playbill_group"]
