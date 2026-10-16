"""Git pack v2 / index v2 storage for SHA-1 and SHA-256 repositories.

Writes publish a pack and its index together (index last); no per-object files.
A cold read indexes existing packs once. Our publications extend that index by
just their new entries; external gc/repack changes trigger a cold rediscovery.
"""

from __future__ import annotations

import hashlib
import mmap
import os
import struct
import tempfile
import threading
import zlib
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.contracts.errors import GitError

_KINDS = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_CODES = {value: key for key, value in _KINDS.items()}


def _hash(data: bytes, width: int) -> bytes:
    return hashlib.new("sha1" if width == 20 else "sha256", data).digest()


def _directory_sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
        return st.st_ino, st.st_mtime_ns, st.st_ctime_ns
    except FileNotFoundError:
        return None


def _pack_header(kind: str, size: int) -> bytes:
    first = (_CODES[kind] << 4) | (size & 15)
    size >>= 4
    out = bytearray([first | (128 if size else 0)])
    while size:
        part = size & 127
        size >>= 7
        out.append(part | (128 if size else 0))
    return bytes(out)


class PackStore:
    def __init__(self, repository: Path) -> None:
        self.repository = repository
        self.directory = repository / "objects" / "pack"
        self.lock = threading.RLock()
        self.locations: dict[str, tuple[Path, int]] = {}
        self.maps: OrderedDict[Path, mmap.mmap] = OrderedDict()
        self.stamp: tuple[int, int, int] | None = None

    def _discover(self, width: int) -> None:
        stamp = _stamp(self.directory)
        if stamp == self.stamp:
            return
        locations: dict[str, tuple[Path, int]] = {}
        for index in self.directory.glob("*.idx"):
            data = index.read_bytes()
            if data[:8] != b"\xfftOc\x00\x00\x00\x02":
                raise GitError("unsupported Git pack index version")
            if _hash(data[:-width], width) != data[-width:]:
                raise GitError("Git pack index checksum mismatch")
            count = struct.unpack_from(">I", data, 8 + 255 * 4)[0]
            start = 8 + 256 * 4
            offsets = start + count * (width + 4)
            if offsets + count * 4 + width * 2 > len(data):
                raise GitError("truncated Git pack index")
            for i in range(count):
                oid = data[start + i * width : start + (i + 1) * width].hex()
                offset = struct.unpack_from(">I", data, offsets + i * 4)[0]
                if offset & 0x80000000:
                    offset = struct.unpack_from(
                        ">Q", data, offsets + count * 4 + (offset & 0x7FFFFFFF) * 8
                    )[0]
                locations[oid] = (index.with_suffix(".pack"), offset)
        self.locations = locations
        # Close stale mappings on an explicit external repack. In-process writes
        # register their own locations without this population walk.
        for mapped in self.maps.values():
            mapped.close()
        self.maps.clear()
        self.stamp = stamp

    def write(self, objects: Mapping[str, tuple[str, bytes]]) -> None:
        if not objects:
            return
        width = len(next(iter(objects))) // 2
        with self.lock:
            self.directory.mkdir(exist_ok=True)
            self._discover(width)
            pack = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
            rows: list[tuple[bytes, int, int]] = []
            for oid, (kind, body) in objects.items():
                offset = len(pack)
                encoded = _pack_header(kind, len(body)) + zlib.compress(
                    body, level=0 if kind == "tree" else -1
                )
                pack.extend(encoded)
                rows.append((bytes.fromhex(oid), zlib.crc32(encoded), offset))
            checksum = _hash(bytes(pack), width)
            pack.extend(checksum)
            rows.sort()
            fanout = [0] * 256
            for oid, _, _ in rows:
                fanout[oid[0]] += 1
            for i in range(1, 256):
                fanout[i] += fanout[i - 1]
            index = bytearray(b"\xfftOc" + struct.pack(">I", 2))
            index.extend(struct.pack(">256I", *fanout))
            index.extend(b"".join(row[0] for row in rows))
            index.extend(b"".join(struct.pack(">I", row[1]) for row in rows))
            large = []
            for _, _, offset in rows:
                if offset >= 0x80000000:
                    index.extend(struct.pack(">I", 0x80000000 | len(large)))
                    large.append(offset)
                else:
                    index.extend(struct.pack(">I", offset))
            index.extend(b"".join(struct.pack(">Q", offset) for offset in large))
            index.extend(checksum)
            index.extend(_hash(bytes(index), width))
            stem = self.directory / f"pack-{checksum.hex()}"
            for suffix, content in ((".pack", pack), (".idx", index)):
                fd, name = tempfile.mkstemp(prefix="tmp_pack_", dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(name, 0o444)
                    os.replace(name, stem.with_suffix(suffix))
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            _directory_sync(self.directory)
            for oid, _, offset in rows:
                self.locations[oid.hex()] = (stem.with_suffix(".pack"), offset)
            self.stamp = _stamp(self.directory)

    def read(self, oid: str) -> tuple[str, bytes] | None:
        width = len(oid) // 2
        with self.lock:
            loose = self.repository / "objects" / oid[:2] / oid[2:]
            try:
                raw = zlib.decompress(loose.read_bytes()) if loose.exists() else None
            except FileNotFoundError:
                raw = None
            if raw is not None:
                header, sep, body = raw.partition(b"\x00")
                kind, _, size = header.partition(b" ")
                if not sep or int(size) != len(body):
                    raise GitError("malformed loose Git object")
                result = kind.decode("ascii"), body
            else:
                self._discover(width)
                location = self.locations.get(oid)
                if location is None:
                    return None
                result = self._at(*location, width=width, depth=0)
            kind, body = result
            if _hash(f"{kind} {len(body)}".encode() + b"\x00" + body, width).hex() != oid:
                raise GitError(f"ledger object bytes do not hash to their ID: {oid}")
            return result

    def _mapping(self, path: Path) -> mmap.mmap:
        mapped = self.maps.get(path)
        if mapped is None:
            with path.open("rb") as stream:
                mapped = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            self.maps[path] = mapped
            if len(self.maps) > 64:
                _, old = self.maps.popitem(last=False)
                old.close()
        self.maps.move_to_end(path)
        return mapped

    def _at(self, path: Path, offset: int, *, width: int, depth: int) -> tuple[str, bytes]:
        if depth > 128:
            raise GitError("Git pack delta chain exceeds limit")
        mapped = self._mapping(path)
        cursor = offset
        byte = mapped[cursor]
        cursor += 1
        kind, size, shift = (byte >> 4) & 7, byte & 15, 4
        while byte & 128:
            byte = mapped[cursor]
            cursor += 1
            size |= (byte & 127) << shift
            shift += 7
        base_offset = None
        base_oid = None
        if kind == 6:
            byte = mapped[cursor]
            cursor += 1
            distance = byte & 127
            while byte & 128:
                byte = mapped[cursor]
                cursor += 1
                distance = ((distance + 1) << 7) | (byte & 127)
            base_offset = offset - distance
        elif kind == 7:
            base_oid = mapped[cursor : cursor + width].hex()
            cursor += width
        inflater = zlib.decompressobj()
        parts = []
        while not inflater.eof:
            chunk = mapped[cursor : cursor + 65536]
            if not chunk:
                raise GitError("truncated Git pack object")
            parts.append(inflater.decompress(chunk))
            cursor += len(chunk)
        body = b"".join(parts)
        if len(body) != size:
            raise GitError("Git pack object size mismatch")
        if kind in _KINDS:
            return _KINDS[kind], body
        if base_offset is not None:
            base = self._at(path, base_offset, width=width, depth=depth + 1)
        elif base_oid is not None:
            location = self.locations.get(base_oid)
            if location is None:
                raise GitError("Git pack delta base is unavailable")
            base = self._at(*location, width=width, depth=depth + 1)
        else:
            raise GitError("unsupported Git pack object type")
        return base[0], _apply_delta(base[1], body)

    def close(self) -> None:
        with self.lock:
            for mapped in self.maps.values():
                mapped.close()
            self.maps.clear()


def _apply_delta(base: bytes, delta: bytes) -> bytes:
    cursor = 0

    def integer() -> int:
        nonlocal cursor
        value = shift = 0
        while True:
            byte = delta[cursor]
            cursor += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
            shift += 7

    if integer() != len(base):
        raise GitError("Git delta base size mismatch")
    expected = integer()
    out = bytearray()
    while cursor < len(delta):
        command = delta[cursor]
        cursor += 1
        if command & 128:
            offset = size = 0
            for i in range(4):
                if command & (1 << i):
                    offset |= delta[cursor] << (8 * i)
                    cursor += 1
            for i in range(3):
                if command & (16 << i):
                    size |= delta[cursor] << (8 * i)
                    cursor += 1
            size = size or 65536
            if offset + size > len(base):
                raise GitError("Git delta copy exceeds base")
            out.extend(base[offset : offset + size])
        elif command:
            out.extend(delta[cursor : cursor + command])
            cursor += command
        else:
            raise GitError("invalid Git delta instruction")
    if len(out) != expected:
        raise GitError("Git delta result size mismatch")
    return bytes(out)
