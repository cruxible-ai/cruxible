"""Round-5 diagnostic: characterise the cross-session best-effort window."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.provider_process_leases import ProviderProcessLeaseStore

from .test_v3_fence import _child, _grew, _invoke, _reap


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 4])
def test_diag_post_envelope_setsid_escape(short_root: Path, attempt: int) -> None:
    marker = short_root / f"m-diag-{attempt}"
    interpreter = _child(
        short_root / f"c-diag-{attempt}.py", marker=marker, mode="setsid", when="after"
    )
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=0.1,
    )
    try:
        _invoke(store, interpreter, "sha256:" + f"{attempt:064d}")
        survived = _grew(marker)
        print(f"POST-ENVELOPE-SETSID attempt={attempt} survived={survived}")
    finally:
        _reap(str(marker))
