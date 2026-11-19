"""Git pack v2 / index v2 storage for SHA-1 and SHA-256 repositories.

Published names are immutable and checksum-derived, including provisional packs.
Writes publish protections first, the pack next, and its matching index last,
before retiring the superseded pair under the store lock; no per-object files.
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
from collections.abc import Mapping, Sequence
from pathlib import Path

from cruxible_client.contracts.errors import GitError

_KINDS = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_CODES = {value: key for key, value in _KINDS.items()}

_EMPTY_TREES = {
    hashlib.sha1(b"tree 0\x00").hexdigest(),  # noqa: S324 - Git object identity
    hashlib.sha256(b"tree 0\x00").hexdigest(),
}


# Bounded relisting of a pack directory that a concurrent repack rewrites.
_DISCOVERY_ATTEMPTS = 8


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
        self.members: dict[Path, dict[str, int]] = {}
        self.maps: OrderedDict[Path, mmap.mmap] = OrderedDict()
        self.stamp: tuple[int, int, int] | None = None

    def _discover(self, width: int) -> None:
        stamp = _stamp(self.directory)
        if stamp == self.stamp:
            return
        # A concurrent `git repack -d` (live compaction) publishes its
        # consolidated pack before it retires the packs it replaces, so a pair
        # that vanishes mid-walk had its replacement on disk first. When the
        # listing gained no index meanwhile, that replacement was walked; when
        # it did, walk the fresh listing. Each repack adds one index, so this
        # settles at once; the bound only refuses a directory churning forever.
        listing = self._listing()
        for _ in range(_DISCOVERY_ATTEMPTS):
            locations, members, vanished = self._walk(listing, width)
            if not vanished:
                break
            relisted = self._listing()
            if set(relisted) <= set(listing):
                break
            listing = relisted
        else:
            raise GitError("Git pack directory kept changing during discovery")
        self.locations = locations
        self.members = members
        # Close stale mappings on an explicit external repack. In-process writes
        # register their own locations without this population walk.
        for mapped in self.maps.values():
            mapped.close()
        self.maps.clear()
        self.stamp = stamp

    def _listing(self) -> list[Path]:
        # On cold discovery, durable packs win duplicate-object locations.
        return sorted(
            self.directory.glob("*.idx"),
            key=lambda item: not item.with_suffix(".pending").exists(),
        )

    def _walk(
        self, listing: list[Path], width: int
    ) -> tuple[dict[str, tuple[Path, int]], dict[Path, dict[str, int]], bool]:
        """Index one listing; report whether any listed pair vanished beneath it."""

        locations: dict[str, tuple[Path, int]] = {}
        members: dict[Path, dict[str, int]] = {}
        vanished = False
        for index in listing:
            pending_index = index.with_suffix(".pending").exists()
            try:
                indexed: dict[str, int] = {}
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
                pack_path = index.with_suffix(".pack")
                for i in range(count):
                    oid = data[start + i * width : start + (i + 1) * width].hex()
                    offset = struct.unpack_from(">I", data, offsets + i * 4)[0]
                    if offset & 0x80000000:
                        offset = struct.unpack_from(
                            ">Q", data, offsets + count * 4 + (offset & 0x7FFFFFFF) * 8
                        )[0]
                    indexed[oid] = offset
                if not pack_path.is_file():
                    # Git itself skips an index whose pack is gone: a repack
                    # retires the pair file by file.
                    raise FileNotFoundError(pack_path)
                locations.update((oid, (pack_path, offset)) for oid, offset in indexed.items())
                members[pack_path] = indexed
            except FileNotFoundError:
                # Retired by a concurrent repack after the listing was taken.
                vanished = True
                continue
            except (OSError, ValueError, IndexError, struct.error, GitError):
                if not pending_index:
                    raise
                # Advisory publication can disappear or tear before acceptance.
                # Its objects are absent; a durable pack is still authoritative.
                continue
        return locations, members, vanished

    def write(
        self,
        objects: Mapping[str, tuple[str, bytes]],
        *,
        keep: bool = False,
        durable: bool = True,
        replacing: Path | None = None,
        encoded: dict[str, bytes] | None = None,
    ) -> Path | None:
        if not objects:
            return None
        width = len(next(iter(objects))) // 2
        with self.lock:
            self.directory.mkdir(exist_ok=True)
            self._discover(width)
            pack = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
            rows: list[tuple[bytes, int, int]] = []
            for oid, (kind, body) in objects.items():
                offset = len(pack)
                entry = None if encoded is None else encoded.get(oid)
                if entry is None:
                    entry = _pack_header(kind, len(body)) + zlib.compress(
                        body, level=0 if kind == "tree" else -1
                    )
                    if encoded is not None:
                        encoded[oid] = entry
                pack.extend(entry)
                rows.append((bytes.fromhex(oid), zlib.crc32(entry), offset))
            checksum = _hash(bytes(pack), width)
            pack.extend(checksum)
            rows.sort()
            fanout = [0] * 256
            for raw_oid, _, _ in rows:
                fanout[raw_oid[0]] += 1
            for i in range(1, 256):
                fanout[i] += fanout[i - 1]
            index = bytearray(b"\xfftOc" + struct.pack(">I", 2))
            index.extend(struct.pack(">256I", *fanout))
            index.extend(b"".join(row[0] for row in rows))
            index.extend(b"".join(struct.pack(">I", row[1]) for row in rows))
            large: list[int] = []
            for _, _, offset in rows:
                if offset >= 0x80000000:
                    index.extend(struct.pack(">I", 0x80000000 | len(large)))
                    large.append(offset)
                else:
                    index.extend(struct.pack(">I", offset))
            index.extend(b"".join(struct.pack(">Q", offset) for offset in large))
            index.extend(checksum)
            index.extend(_hash(bytes(index), width))
            # Git readers cannot acquire our lock. Never change bytes beneath
            # an index they may already have discovered or mapped. A request
            # still retains only its current cumulative object frontier.
            stem = self.directory / f"pack-{checksum.hex()}"
            path = stem.with_suffix(".pack")
            existing = path.exists() and stem.with_suffix(".idx").exists()
            if not durable and not existing:
                stem.with_suffix(".pending").touch(exist_ok=True)
            if keep:
                # Git honors .keep even for an as-yet-unreferenced pack. Make
                # the protection visible before either pack/index publication.
                stem.with_suffix(".keep").touch(exist_ok=True)
            for suffix, content in ((".pack", pack), (".idx", index)):
                destination = stem.with_suffix(suffix)
                if destination.exists():
                    # The checksum names these exact bytes. Reuse an existing
                    # pair (or a pack left before index publication) in place.
                    # Acceptance must nevertheless synchronize advisory bytes.
                    if durable:
                        with destination.open("rb") as stream:
                            os.fsync(stream.fileno())
                    continue
                fd, name = tempfile.mkstemp(prefix="tmp_pack_", dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        if durable:
                            os.fsync(stream.fileno())
                    os.chmod(name, 0o444)
                    os.replace(name, destination)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            if durable:
                stem.with_suffix(".pending").unlink(missing_ok=True)
                # Make the replacement durable BEFORE removing an older
                # durable copy. Retirement need not survive a crash; a stale
                # duplicate is safe. This remains one accepting directory sync.
                _directory_sync(self.directory)
            if replacing is not None and replacing != path:
                # An identical pair may have been borrowed from an earlier
                # durable batch. A subsequent advisory append cannot retire
                # that batch's last durable copy. It belongs to ordinary Git
                # compaction, not this request's provisional frontier.
                if durable or replacing.with_suffix(".pending").exists():
                    replacing.with_suffix(".idx").unlink(missing_ok=True)
                    replacing.unlink(missing_ok=True)
                    replacing.with_suffix(".pending").unlink(missing_ok=True)
                    mapped = self.maps.pop(replacing, None)
                    if mapped is not None:
                        mapped.close()
                    self.members.pop(replacing, None)
                replacing.with_suffix(".keep").unlink(missing_ok=True)
            for raw_oid, _, offset in rows:
                self.locations[raw_oid.hex()] = (path, offset)
            self.members[path] = {raw_oid.hex(): offset for raw_oid, _, offset in rows}
            self.stamp = _stamp(self.directory)
            return path

    def release(self, packs: list[Path]) -> None:
        with self.lock:
            for path in packs:
                path.with_suffix(".keep").unlink(missing_ok=True)
            self.stamp = _stamp(self.directory)

    def release_oid(self, oid: str) -> None:
        with self.lock:
            self._discover(len(oid) // 2)
            location = self.locations.get(oid)
            if location is not None:
                self.release([location[0]])

    def is_pending(self, oid: str) -> bool:
        """Whether this object's pack still carries a best-effort publication marker."""
        with self.lock:
            self._discover(len(oid) // 2)
            location = self.locations.get(oid)
            return location is not None and location[0].with_suffix(".pending").exists()

    def info(self, oid: str) -> tuple[str, int] | None:
        return self.infos((oid,))[oid]

    def infos(self, oids: Sequence[str]) -> dict[str, tuple[str, int] | None]:
        """Read headers only, discovering/validating the pack directory once per batch."""
        if not oids:
            return {}
        with self.lock:
            self._discover(len(oids[0]) // 2)
            result: dict[str, tuple[str, int] | None] = {}
            for oid in oids:
                try:
                    try:
                        result[oid] = self._info(oid)
                    except FileNotFoundError:
                        # A concurrent repack retired the pack between
                        # discovery and mapping; its replacement holds the oid.
                        self.stamp = None
                        self._discover(len(oid) // 2)
                        result[oid] = self._info(oid)
                except (OSError, ValueError, IndexError, struct.error, zlib.error, GitError):
                    if not self.is_pending(oid):
                        raise
                    result[oid] = None
            return result

    def _info(self, oid: str) -> tuple[str, int] | None:
        width = len(oid) // 2
        location = self.locations.get(oid)
        if location is not None:
            return self._info_at(*location, width=width, depth=0)
        loose = self.repository / "objects" / oid[:2] / oid[2:]
        try:
            with loose.open("rb") as stream:
                inflater = zlib.decompressobj()
                header = bytearray()
                while b"\x00" not in header and len(header) < 128:
                    chunk = inflater.unconsumed_tail or stream.read(64)
                    if not chunk:
                        break
                    header.extend(inflater.decompress(chunk, 1))
        except FileNotFoundError:
            # A concurrent repack packs loose objects, then prunes them: a
            # changed pack directory is walked again (one stat when unchanged).
            self._discover(width)
            location = self.locations.get(oid)
            if location is not None:
                return self._info_at(*location, width=width, depth=0)
            return ("tree", 0) if oid in _EMPTY_TREES else None
        raw_kind, separator, raw_size = bytes(header).partition(b"\x00")[0].partition(b" ")
        if b"\x00" not in header or not separator or raw_kind.decode() not in _CODES:
            raise GitError("malformed loose Git object header")
        size = int(raw_size)
        if size < 0:
            raise GitError("malformed loose Git object size")
        return raw_kind.decode(), size

    def _info_at(self, path: Path, offset: int, *, width: int, depth: int) -> tuple[str, int]:
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
        if kind in _KINDS:
            return _KINDS[kind], size
        if kind == 6:
            byte = mapped[cursor]
            cursor += 1
            distance = byte & 127
            while byte & 128:
                byte = mapped[cursor]
                cursor += 1
                distance = ((distance + 1) << 7) | (byte & 127)
            base_path, base_offset = path, offset - distance
        elif kind == 7:
            base_oid = mapped[cursor : cursor + width].hex()
            cursor += width
            base = self.locations.get(base_oid)
            if base is None:
                raise GitError("Git pack delta base is unavailable")
            base_path, base_offset = base
        else:
            raise GitError("unsupported Git pack object type")
        # Delta streams begin with base-size and result-size varints. Only
        # these bounded header bytes are inflated, never the delta payload.
        inflater = zlib.decompressobj()
        compressed = mapped[cursor : cursor + 64]
        sizes = []
        for _ in range(2):
            value = shift = 0
            while True:
                decoded = inflater.decompress(compressed, 1)
                compressed = inflater.unconsumed_tail
                # Dynamic-Huffman tables can consume an entire input chunk
                # before emitting the first header byte. Feed more compressed
                # input while still limiting output to exactly one byte.
                while not decoded and not inflater.eof:
                    if not compressed:
                        cursor += 64
                        compressed = mapped[cursor : cursor + 64]
                        if not compressed:
                            break
                    decoded = inflater.decompress(compressed, 1)
                    compressed = inflater.unconsumed_tail
                if not decoded or shift > 63:
                    raise GitError("malformed Git delta header")
                byte = decoded[0]
                value |= (byte & 127) << shift
                shift += 7
                if not byte & 128:
                    break
            sizes.append(value)
        base_kind, _ = self._info_at(base_path, base_offset, width=width, depth=depth + 1)
        return base_kind, sizes[1]

    def read(self, oid: str) -> tuple[str, bytes] | None:
        try:
            try:
                return self._read(oid)
            except FileNotFoundError:
                # An external gc may replace a pack between discovery and open.
                # Rediscover once, never on an ordinary missing-object probe.
                with self.lock:
                    self.stamp = None
                    self._discover(len(oid) // 2)
                return self._read(oid)
        except (OSError, ValueError, IndexError, struct.error, zlib.error, GitError) as exc:
            if self.is_pending(oid):
                return None
            if isinstance(exc, GitError):
                raise
            raise GitError(f"Git object storage is corrupt or unavailable: {oid}") from exc

    def _read(self, oid: str) -> tuple[str, bytes] | None:
        width = len(oid) // 2
        with self.lock:
            loose = self.repository / "objects" / oid[:2] / oid[2:]
            try:
                raw = zlib.decompress(loose.read_bytes()) if loose.exists() else None
            except FileNotFoundError:
                raw = None
            if raw is not None:
                header, sep, body = raw.partition(b"\x00")
                raw_kind, _, size = header.partition(b" ")
                if not sep or int(size) != len(body):
                    raise GitError("malformed loose Git object")
                result = raw_kind.decode("ascii"), body
            else:
                self._discover(width)
                location = self.locations.get(oid)
                if location is None:
                    # Git exposes the empty tree even without a physical
                    # object; physical objects still pass the hash check.
                    return ("tree", b"") if oid in _EMPTY_TREES else None
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

    def remove(self, oids: set[str]) -> tuple[str, ...]:
        """Explicit loser cleanup: rewrite affected packs, retaining shared objects.

        This cold repair path may enumerate the pack index. Ordinary writes do
        not call it. Callers prove these objects are unreachable under ledger
        publication locks before handing them here.
        """
        if not oids:
            return ()
        with self.lock:
            self._discover(len(next(iter(oids))) // 2)
            affected = {
                path for path, members in self.members.items() if oids.intersection(members)
            }
            retained = {
                oid: self.read(oid)
                for oid, (path, _) in self.locations.items()
                if path in affected and oid not in oids
            }
            self.write({oid: value for oid, value in retained.items() if value is not None})
            for path in affected:
                mapped = self.maps.pop(path, None)
                if mapped is not None:
                    mapped.close()
                path.with_suffix(".keep").unlink(missing_ok=True)
                path.with_suffix(".idx").unlink(missing_ok=True)
                path.with_suffix(".pending").unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            _directory_sync(self.directory)
            self.stamp = None
            self._discover(len(next(iter(oids))) // 2)
            return tuple(sorted(oids))

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
