"""CI guard: every bundled kit's committed lock is present, current, and portable.

Kit locks have drifted silently before: kit materialization at init verifies
lock PRESENCE only, so a stale or hand-divergent ``cruxible.lock.yaml`` ships
without any failure until a stranger hits the digest mismatch. These tests pin
the canonical generation path (``build_kit_root_lock`` — the same function
behind ``cruxible lock --kit-dir``) and assert regen-is-noop for every bundled
kit, so any config/provider/artifact edit that lands without a lock regen is a
CI failure with the fix command in the message.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import cruxible_core.runtime  # noqa: F401
from cruxible_core.workflow.compiler import LOCK_FILE_NAME, build_kit_root_lock

_KITS_ROOT = Path(__file__).resolve().parents[2] / "kits"
_KIT_ROOTS = sorted(path for path in _KITS_ROOT.iterdir() if (path / "cruxible-kit.yaml").exists())


def _regen_hint(kit_name: str) -> str:
    return f"regenerate with: uv run cruxible lock --kit-dir kits/{kit_name}"


def test_bundled_kits_discovered() -> None:
    # Guard the parametrization itself: an empty discovery would silently skip
    # every check below (e.g. after a kits/ layout move).
    assert _KIT_ROOTS, f"no kit manifests found under {_KITS_ROOT}"


@pytest.mark.parametrize("kit_root", _KIT_ROOTS, ids=lambda path: path.name)
def test_kit_lock_present_and_current(kit_root: Path) -> None:
    lock_path = kit_root / LOCK_FILE_NAME
    assert lock_path.exists(), (
        f"{kit_root.name}: committed {LOCK_FILE_NAME} is missing — "
        f"fresh-clone init of this kit will fail; {_regen_hint(kit_root.name)}"
    )

    committed = yaml.safe_load(lock_path.read_text())
    rebuilt = build_kit_root_lock(kit_root)

    assert committed.get("config_digest") == rebuilt.config_digest, (
        f"{kit_root.name}: config.yaml changed without a lock regen; {_regen_hint(kit_root.name)}"
    )
    assert committed.get("lock_digest") == rebuilt.lock_digest, (
        f"{kit_root.name}: committed lock does not match a fresh regen "
        f"(provider entrypoints, artifact digests, or lock shape drifted); "
        f"{_regen_hint(kit_root.name)}"
    )


@pytest.mark.parametrize("kit_root", _KIT_ROOTS, ids=lambda path: path.name)
def test_kit_lock_is_portable(kit_root: Path) -> None:
    # A committed kit lock must make sense on any machine: artifact URIs stay
    # as written in config.yaml (kit-relative), never resolved to absolute
    # paths, and provider refs stay kit:// or importable module refs.
    committed = yaml.safe_load((kit_root / LOCK_FILE_NAME).read_text())
    for name, artifact in (committed.get("artifacts") or {}).items():
        uri = str(artifact.get("uri") or "")
        assert not Path(uri).is_absolute(), (
            f"{kit_root.name}: artifact '{name}' has a machine-absolute uri {uri!r}; "
            f"{_regen_hint(kit_root.name)}"
        )
    for name, provider in (committed.get("providers") or {}).items():
        ref = str(provider.get("ref") or "")
        assert not ref.startswith("/"), (
            f"{kit_root.name}: provider '{name}' has a machine-absolute ref {ref!r}; "
            f"{_regen_hint(kit_root.name)}"
        )
