"""Typed pin-only automation succession; consumer authority never transfers.

Only the affected reverse-pin closure calls this module. A Line successor pins
its Procedure's successor (or rebinds a moved build); each Mandate pinning the
old Line digest takes a propose-only successor pinning the new one, because a
grant never follows a changed Line (ruling C3). Literal digest-looking
parameters and conditions are data, never reference replacement targets.
"""

from collections.abc import Mapping

from cruxible_client.contracts.artifacts import ArtifactLifecycle, ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import normalize_canonical
from cruxible_client.contracts.content_memo import ParseMemo, sealed_memo
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    ProcedureMandateAny,
    mandate_grant,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.artifacts import AcceptedProcedure
from cruxible_client.contracts.procedures.contracts import (
    OwnedProcedureContractValidator,
    ProcedureContractValidationError,
)
from cruxible_client.contracts.procedures.line_specs import (
    LineSpec,
    line_spec_digest,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.provider_installation import (
    AutomationCarryV1,
    CarriedLineV1,
    CarriedMandateV1,
)

# These newly selected families have no contract-level parse memo yet. Share the
# exact-byte models between upgrade planning and the generation findings worker.
_LINES: ParseMemo[LineSpec] = ParseMemo(capacity=2048, max_bytes=32 * 1024 * 1024)
_MANDATES: ParseMemo[ProcedureMandateAny] = ParseMemo(capacity=2048, max_bytes=16 * 1024 * 1024)
_line_digest = sealed_memo(line_spec_digest)
_mandate_digest = sealed_memo(procedure_mandate_digest)


def parse_carry_line(content: bytes, *, path: str) -> LineSpec:
    return _LINES.parse(content, path, None, lambda: parse_line_spec(content, path=path))


def validate_carried_line(content: bytes, *, path: str, procedure: AcceptedProcedure) -> None:
    """Apply ordinary Line-authoring input semantics to the exact final Procedure.

    Called once per affected Line at settlement; parameters remain unchanged.
    """

    line = parse_carry_line(content, path=path)
    contract = procedure.procedure.definition.contract_in
    if not isinstance(contract, ArtifactPin | ArtifactNameRef):
        return
    try:
        OwnedProcedureContractValidator(procedure).validate_contract(
            contract=contract,
            payload=normalize_canonical(line.parameters),
            direction="input",
        )
    except ProcedureContractValidationError as exc:
        field = "parameters" if not exc.field_path else f"parameters.{exc.field_path}"
        raise ValueError(
            f"{line.identity.qualified}: final {procedure.procedure.identity.qualified} "
            f"at {procedure.artifact_digest} refuses {field}: {exc}; "
            "explicitly author compatible Line parameters against the final Procedure's "
            "input contract before retrying the upgrade"
        ) from exc


def carry_line(
    path: str, content: bytes, replacements: Mapping[str, str], *, planned: bool = False
) -> tuple[bytes | None, AutomationCarryV1]:
    """One Line successor pinning its Procedure's final version (``replacements``)."""

    line = parse_carry_line(content, path=path)
    old = _line_digest(line).tagged
    target = replacements.get(line.procedure.artifact_digest)
    successor = line.model_copy(
        update={
            "procedure": line.procedure.model_copy(update={"artifact_digest": target}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=old),
        }
    )
    return (
        None if planned else render_line_spec(successor),
        CarriedLineV1(
            identity=line.identity.qualified,
            procedure=line.procedure.target.qualified,
            old_procedure_digest=line.procedure.artifact_digest,
            new_procedure_digest=target,
            old_artifact_digest=old,
            new_artifact_digest=None if planned else line_spec_digest(successor).tagged,
            phase="planned" if planned else "validated",
        ),
    )


def carry_mandate(
    path: str, content: bytes, line_replacements: Mapping[str, str], *, planned: bool = False
) -> tuple[bytes | None, AutomationCarryV1]:
    """One propose-only Mandate successor pinning its Line's successor."""

    mandate = _MANDATES.parse(
        content,
        path,
        None,
        lambda: parse_procedure_mandate_any(content, path=path),
    )
    old = _mandate_digest(mandate).tagged
    target = line_replacements.get(mandate.line.artifact_digest)
    successor_mandate = (
        ProcedureMandate(
            identity=mandate.identity,
            line=mandate.line.model_copy(update={"artifact_digest": target}),
            grants="propose",
            resource_ceiling=mandate.resource_ceiling,
            namespace=mandate.namespace,
            valid_from=mandate.valid_from,
            expires_at=mandate.expires_at,
            suspended=mandate.suspended,
            lifecycle=ArtifactLifecycle(predecessor_digest=old),
        )
        if not planned and target is not None
        else None
    )
    grant = mandate_grant(mandate)
    return (
        None if successor_mandate is None else render_procedure_mandate(successor_mandate),
        CarriedMandateV1(
            identity=mandate.identity.qualified,
            line=mandate.line.target.qualified,
            old_line_digest=mandate.line.artifact_digest,
            new_line_digest=target,
            old_artifact_digest=old,
            new_artifact_digest=(
                None
                if successor_mandate is None
                else procedure_mandate_digest(successor_mandate).tagged
            ),
            phase="planned" if planned else "validated",
            old_grant=grant,
            loses_settle=grant == "settle",
            subject_creation_namespace_limitation=bool(mandate.subject_creation),
        ),
    )


def carry_automation(
    path: str, content: bytes, replacements: Mapping[str, str], *, planned: bool = False
) -> tuple[bytes | None, AutomationCarryV1]:
    """One accepted predecessor -> one final successor, or an inventory-only row.

    For a Line, ``replacements`` maps Procedure digests; for a Mandate, Line
    digests.
    """

    if path.startswith("lines/"):
        return carry_line(path, content, replacements, planned=planned)
    return carry_mandate(path, content, replacements, planned=planned)
