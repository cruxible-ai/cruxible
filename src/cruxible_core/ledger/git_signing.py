"""Ed25519 SSHSIG commit signatures, as specified by OpenSSH PROTOCOL.sshsig.

https://github.com/openssh/openssh-portable/blob/master/PROTOCOL.sshsig
Git's namespace is ``git``; SHA-256 repositories use ``gpgsig-sha256``.
"""

from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

_MAGIC = b"SSHSIG"
_BEGIN = b"-----BEGIN SSH SIGNATURE-----"
_END = b"-----END SSH SIGNATURE-----"


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


class _Reader:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0

    def string(self) -> bytes:
        size = struct.unpack_from(">I", self.value, self.offset)[0]
        self.offset += 4
        end = self.offset + size
        if end > len(self.value):
            raise ValueError("truncated SSH string")
        result = self.value[self.offset : end]
        self.offset = end
        return result

    def end(self) -> None:
        if self.offset != len(self.value):
            raise ValueError("trailing SSH signature data")


def _preimage(payload: bytes, namespace: bytes, reserved: bytes, algorithm: bytes) -> bytes:
    if algorithm not in (b"sha256", b"sha512"):
        raise ValueError("unsupported SSH signature hash")
    return (
        _MAGIC
        + _string(namespace)
        + _string(reserved)
        + _string(algorithm)
        + _string(hashlib.new(algorithm.decode("ascii"), payload).digest())
    )


def sign_commit(payload: bytes, key: Ed25519PrivateKey, *, sha256: bool) -> bytes:
    public = _string(b"ssh-ed25519") + _string(key.public_key().public_bytes_raw())
    signature = key.sign(_preimage(payload, b"git", b"", b"sha512"))
    blob = (
        _MAGIC
        + struct.pack(">I", 1)
        + _string(public)
        + _string(b"git")
        + _string(b"")
        + _string(b"sha512")
        + _string(_string(b"ssh-ed25519") + _string(signature))
    )
    encoded = base64.b64encode(blob)
    armor = b"\n".join([_BEGIN, *(encoded[i : i + 70] for i in range(0, len(encoded), 70)), _END])
    header = b"gpgsig-sha256 " if sha256 else b"gpgsig "
    headers, separator, message = payload.partition(b"\n\n")
    if not separator:
        raise ValueError("commit has no message separator")
    return headers + b"\n" + header + armor.replace(b"\n", b"\n ") + b"\n\n" + message


def verify_commit(body: bytes, key: Ed25519PublicKey, *, sha256: bool) -> bool:
    try:
        headers, separator, message = body.partition(b"\n\n")
        if not separator:
            return False
        prefix = b"gpgsig-sha256 " if sha256 else b"gpgsig "
        unsigned: list[bytes] = []
        signatures: list[list[bytes]] = []
        active = False
        for line in headers.split(b"\n"):
            if line.startswith(prefix):
                signatures.append([line[len(prefix) :]])
                active = True
            elif active and line.startswith(b" "):
                signatures[-1].append(line[1:])
            else:
                active = False
                unsigned.append(line)
        if len(signatures) != 1:
            return False
        lines = signatures[0]
        if len(lines) < 3 or lines[0] != _BEGIN or lines[-1] != _END:
            return False
        raw = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        if raw[:10] != _MAGIC + struct.pack(">I", 1):
            return False
        reader = _Reader(raw[10:])
        public, namespace, reserved, algorithm, signature = (reader.string() for _ in range(5))
        reader.end()
        expected = _string(b"ssh-ed25519") + _string(key.public_bytes_raw())
        if public != expected or namespace != b"git":
            return False
        signed = _Reader(signature)
        if signed.string() != b"ssh-ed25519":
            return False
        signature = signed.string()
        signed.end()
        payload = b"\n".join(unsigned) + b"\n\n" + message
        key.verify(signature, _preimage(payload, namespace, reserved, algorithm))
        return True
    except (ValueError, struct.error, InvalidSignature):
        return False


def load_private(data: bytes) -> Ed25519PrivateKey:
    key = serialization.load_ssh_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("daemon signing key must be Ed25519")
    return key
