"""CaptureKind body access (C14): the field, its keys, and who reads restricted bytes."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import (
    CaptureFormatError,
    capture_body_access,
    capture_contract_digest,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_core.runtime.permissions import (
    PERMISSION_REQUIREMENTS,
    RUNTIME_OPERATION_PERMISSIONS,
    TOOL_PERMISSIONS,
    PermissionMode,
    reset_permissions,
)
from tests.core_support._light_capture_support import record_contract

PATH = "capture-contracts/test.sealed.json"


def _restricted() -> object:
    return lower_capture_contract_input(
        CaptureContractInputRecord(
            name="test.sealed", shape="database_snapshot", body_access="restricted"
        ),
        named=True,
    )


def test_open_is_the_default_and_leaves_every_kind_digest_unchanged() -> None:
    contract = record_contract()
    assert capture_body_access(contract) == "open"
    assert b"body_access" not in render_capture_contract(contract)
    explicit = record_contract(body_access="open")
    assert capture_contract_digest(explicit) == capture_contract_digest(contract)


def test_a_restricted_kind_round_trips_and_is_fixed_with_its_name() -> None:
    contract = _restricted()
    parsed = parse_capture_contract(render_capture_contract(contract), path=PATH)  # type: ignore[arg-type]
    assert capture_body_access(parsed) == "restricted"
    # An edit that leaves it out keeps it; one that changes it needs a new name.
    kept = lower_capture_contract_input(
        CaptureContractInputRecord(name="test.sealed", max_bytes=32 * 1024 * 1024),
        predecessor=parsed,
        named=True,
    )
    assert capture_body_access(kept) == "restricted"
    with pytest.raises(CaptureFormatError, match="body_access changed; use a new name"):
        lower_capture_contract_input(
            CaptureContractInputRecord(name="test.sealed", body_access="open"),
            predecessor=parsed,
            named=True,
        )


def test_body_and_capture_reads_are_read_only_and_restricted_bytes_need_the_key() -> None:
    assert RUNTIME_OPERATION_PERMISSIONS["cruxible_body_read"] == PermissionMode.READ_ONLY
    assert TOOL_PERMISSIONS["cruxible_capture_read"] == PermissionMode.READ_ONLY
    # Not an MCP tool: every body-reading surface consults it per Capture.
    assert "cruxible_body_read_restricted" not in TOOL_PERMISSIONS
    assert RUNTIME_OPERATION_PERMISSIONS["cruxible_body_read_restricted"] == (
        PermissionMode.GOVERNED_WRITE
    )
    assert PERMISSION_REQUIREMENTS["cruxible_body_read_restricted"] == (
        PermissionMode.GOVERNED_WRITE
    )


@pytest.mark.parametrize(
    ("mode", "restricted"),
    [("read_only", False), ("governed_write", True), ("graph_write", True), ("admin", True)],
)
def test_the_served_access_reads_restricted_bytes_only_at_governed_write(
    monkeypatch: pytest.MonkeyPatch, mode: str, restricted: bool
) -> None:
    from cruxible_core.runtime import playbill_api

    monkeypatch.setenv("CRUXIBLE_MODE", mode)
    reset_permissions()
    try:
        access = playbill_api._access("inst_any", include_body=True)
        assert access.can_read_body is True
        assert access.can_read_restricted is restricted
        assert playbill_api._access("inst_any", include_body=False).can_read_restricted is False
    finally:
        monkeypatch.delenv("CRUXIBLE_MODE")
        reset_permissions()


def test_coverage_shows_restricted_evidence_only_to_the_key_holder() -> None:
    from cruxible_core.service.discovery.coverage import coverage_access_profile

    reader = coverage_access_profile()
    holder = coverage_access_profile(restricted=True)
    assert reader.permits("instance") and not reader.permits("restricted")
    assert holder.permits("restricted") and holder.profile_id != reader.profile_id
    assert reader.disclose_restricted_existence and holder.disclose_restricted_existence
