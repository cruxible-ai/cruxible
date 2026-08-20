"""Playbill Family-1 CLI, including local compilation and client-held signing."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import click
import yaml

from cruxible_client import CruxibleClient, contracts
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _emit_json,
    _require_instance_id,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.playbill.attestations import ApprovalStatement
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.keys import generate_client_principal_key
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


__all__ = ["playbill_group"]
