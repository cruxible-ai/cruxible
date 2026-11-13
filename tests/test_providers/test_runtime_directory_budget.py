"""The Provider lane's runtime-directory repair names its byte budget and a real path.

With a state root too long for the control sockets, the daemon falls back to a
private namespace under TMPDIR (macOS) or XDG_RUNTIME_DIR (Linux). When that
refuses, the cause says how many bytes the directory may take, names a concrete
short directory to use, and leaves nothing behind in a rejected directory.
"""

from __future__ import annotations

import platform
from pathlib import Path

import pytest

from cruxible_core.providers.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
)

# "/cruxible/<12 hex>/c/<16 hex>.sock" takes 46 of the 103-byte socket path.
BUDGET = 103 - 46


def _variable_and_example() -> tuple[str, str]:
    if platform.system() == "Linux":
        return "XDG_RUNTIME_DIR", "/run/user/$UID"
    return "TMPDIR", "$(getconf DARWIN_USER_TEMP_DIR)"


def _overlong_root(tmp_path: Path) -> Path:
    deep = tmp_path / ("x" * 90)
    deep.mkdir()
    return deep


@pytest.mark.skipif(platform.system() not in {"Darwin", "Linux"}, reason="no runtime directory")
def test_an_overlong_runtime_directory_refuses_with_its_budget_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variable, example = _variable_and_example()
    runtime_root = tmp_path / ("r" * 40)
    runtime_root.mkdir(mode=0o700)
    assert len(str(runtime_root).encode()) > BUDGET
    monkeypatch.setenv(variable, str(runtime_root))
    deep = _overlong_root(tmp_path)

    with pytest.raises(ProviderLocalRuntimeRefused) as refusal:
        ProviderProcessLeaseStore(deep / "l", control_root=deep / "c")

    message = str(refusal.value)
    assert refusal.value.code == "provider_runtime_directory_unavailable"
    assert f"names a directory of {len(str(runtime_root).encode())} bytes" in message
    assert f"{variable} must name a private directory of at most {BUDGET} bytes" in message
    assert f"such as {variable}={example}" in message
    assert "bytes shorter fits" in message
    # The rejected directory keeps no empty namespace behind.
    assert list(runtime_root.iterdir()) == []
    # The message carries byte counts and a templated example, never a path.
    assert str(tmp_path) not in message


@pytest.mark.skipif(platform.system() not in {"Darwin", "Linux"}, reason="no runtime directory")
def test_an_unset_runtime_directory_names_the_budget_and_a_concrete_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variable, example = _variable_and_example()
    monkeypatch.delenv(variable, raising=False)
    deep = _overlong_root(tmp_path)

    with pytest.raises(ProviderLocalRuntimeRefused) as refusal:
        ProviderProcessLeaseStore(deep / "l", control_root=deep / "c")

    message = str(refusal.value)
    assert f"{variable} is not set" in message
    assert f"at most {BUDGET} bytes, such as {variable}={example}" in message
    # The declared repair still follows the cause.
    assert "--state-root" in message
