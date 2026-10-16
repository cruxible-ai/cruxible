"""Typed pin-only automation succession; consumer authority never transfers.

Only the affected reverse-pin closure calls this module. Literal digest-looking
parameters and conditions are data, never reference replacement targets.
"""

from collections.abc import Mapping

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandate,
    mandate_grant,
    parse_procedure_mandate_any,
    procedure_mandate_digest,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    parse_line_spec,
    render_line_spec,
)
from cruxible_client.contracts.provider_installation import (
    AutomationCarryV1,
    CarriedLineV1,
    CarriedMandateV1,
)


def carry_automation(
    path: str, content: bytes, replacements: Mapping[str, str], *, planned: bool = False
) -> tuple[bytes | None, AutomationCarryV1]:
    """One accepted predecessor -> one final successor, or an inventory-only row."""

    if path.startswith("lines/"):
        line = parse_line_spec(content, path=path)
        old = line_spec_digest(line).tagged
        target = replacements.get(line.procedure.artifact_digest)
        pin = line.procedure.model_copy(update={"artifact_digest": target})
        successor = line.model_copy(
            update={
                "procedure": pin,
                "pins": tuple(
                    sorted(
                        (pin if item == line.procedure else item for item in line.pins),
                        key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
                    )
                ),
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
    mandate = parse_procedure_mandate_any(content, path=path)
    old = procedure_mandate_digest(mandate).tagged
    target = replacements.get(mandate.procedure.artifact_digest)
    successor_mandate = (
        ProcedureMandate(
            identity=mandate.identity,
            procedure=mandate.procedure.model_copy(update={"artifact_digest": target}),
            grants="propose",
            resource_ceiling=(
                mandate.resource_ceiling
                if isinstance(mandate, ProcedureMandate)
                else mandate.authority_ceiling
            ),
            namespace=mandate.namespace,
            valid_from=mandate.valid_from,
            expires_at=mandate.expires_at,
            suspended=mandate.suspended if isinstance(mandate, ProcedureMandate) else False,
            lifecycle=ArtifactLifecycle(predecessor_digest=old),
        )
        if not planned
        else None
    )
    grant = mandate_grant(mandate)
    return (
        None if successor_mandate is None else render_procedure_mandate(successor_mandate),
        CarriedMandateV1(
            identity=mandate.identity.qualified,
            procedure=mandate.procedure.target.qualified,
            old_procedure_digest=mandate.procedure.artifact_digest,
            new_procedure_digest=target,
            old_artifact_digest=old,
            new_artifact_digest=(
                None
                if successor_mandate is None
                else procedure_mandate_digest(successor_mandate).tagged
            ),
            phase="planned" if planned else "validated",
            old_grant=grant,
            loses_settle=grant == "settle",
            subject_creation_namespace_limitation=(
                isinstance(mandate, ProcedureMandate) and bool(mandate.subject_creation)
            ),
        ),
    )
