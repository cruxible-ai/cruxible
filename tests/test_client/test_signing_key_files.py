"""Key custody checks apply to the descriptor read, even if an inode is reused."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.errors import SigningKeyError


@pytest.mark.parametrize("module", ["signing", "attestations"])
def test_looser_mode_replacement_with_reused_inode_is_refused_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    loader = importlib.import_module(f"cruxible_client.authoring.{module}")
    custody = tmp_path / "keys"
    custody.mkdir(mode=0o700)
    key = custody / "key"
    content = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    key.write_bytes(content)
    key.chmod(0o600)
    original = key.stat()
    replacement = custody / "replacement"
    replacement.write_bytes(content)
    replacement.chmod(0o644)
    opened: list[int] = []
    real_open, real_fstat, real_read = os.open, os.fstat, os.read
    reads: list[int] = []

    def open_replacement(path, flags, *args, **kwargs):
        if path == key:
            assert flags & os.O_NOFOLLOW
            replacement.replace(key)
            descriptor = real_open(path, flags, *args, **kwargs)
            opened.append(descriptor)
            return descriptor
        return real_open(path, flags, *args, **kwargs)

    def reused_inode(descriptor):
        metadata = real_fstat(descriptor)
        if descriptor in opened:
            # ext4 can reuse an unlinked inode; preserve the descriptor's actual mode.
            fields = list(metadata)
            fields[1], fields[2] = original.st_ino, original.st_dev
            return os.stat_result(fields)
        return metadata

    def read(descriptor, size):
        reads.append(descriptor)
        return real_read(descriptor, size)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_replacement)
        patch.setattr(os, "fstat", reused_inode)
        patch.setattr(os, "read", read)
        with pytest.raises(SigningKeyError, match="permissions"):
            loader._load_private_key(key)
    assert reads == []
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")
@pytest.mark.parametrize("module", ["signing", "attestations"])
def test_key_replaced_by_fifo_after_stat_refuses_without_waiting_or_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    loader = importlib.import_module(f"cruxible_client.authoring.{module}")
    custody = tmp_path / "keys"
    custody.mkdir(mode=0o700)
    key = custody / "key"
    key.write_bytes(b"regular before open")
    key.chmod(0o600)
    real_open, real_fstat = os.open, os.fstat
    opened: list[int] = []

    def replace_with_fifo(path, flags, *args, **kwargs):
        if path == key:
            key.unlink()
            os.mkfifo(key, mode=0o600)
            # Fail safely on regression instead of hanging this test in open().
            assert flags & os.O_NONBLOCK
            descriptor = real_open(path, flags, *args, **kwargs)
            opened.append(descriptor)
            return descriptor
        return real_open(path, flags, *args, **kwargs)

    def no_read(*args, **kwargs):
        raise AssertionError("a nonregular key must refuse before reading")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", replace_with_fifo)
        patch.setattr(os, "read", no_read)
        with pytest.raises(SigningKeyError):
            loader._load_private_key(key)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])


@pytest.mark.parametrize("module", ["signing", "attestations"])
def test_nonblocking_key_descriptor_still_loads_an_ordinary_key(
    tmp_path: Path, module: str
) -> None:
    loader = importlib.import_module(f"cruxible_client.authoring.{module}")
    custody = tmp_path / "keys"
    custody.mkdir(mode=0o700)
    key = custody / "key"
    private_key = Ed25519PrivateKey.generate()
    key.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    key.chmod(0o600)
    loaded = loader._load_private_key(key)
    private_key.public_key().verify(loaded.sign(b"key probe"), b"key probe")
