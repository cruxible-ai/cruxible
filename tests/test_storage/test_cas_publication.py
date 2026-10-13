"""A CAS object becomes addressable only once its bytes are complete.

``store`` once created the object at its final name and wrote into it there. A
concurrent ``store`` of the same bytes (two submissions of one shared body) met
the half-written file, verified it and failed "CAS object bytes do not match
their content address"; a reader saw it as corrupt. Bytes are now written under
a private temporary name, verified, and then linked to the object name.

The link names the temporary file again, so it must publish the inode whose
bytes were verified: a symlink or another file substituted at the temporary
name between read-back and link is refused, never published.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import CasError
from cruxible_core.storage import cas
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore

READ = BodyAccessContext(principal_id="test", can_read_body=True)


def test_a_store_of_bytes_another_writer_is_still_writing_never_sees_them_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    content = b"shared tree\n" * 4096
    digest = store.digest_bytes(content).tagged
    paused, release = threading.Event(), threading.Event()
    real_write = os.write
    first: dict[str, threading.Thread] = {}

    def write_part_then_pause(descriptor: int, data: bytes | memoryview) -> int:
        if threading.current_thread() is first.get("writer") and not paused.is_set():
            written = real_write(descriptor, bytes(data[:16]))
            paused.set()
            release.wait(10)
            return written
        return real_write(descriptor, data)

    monkeypatch.setattr(cas.os, "write", write_part_then_pause)
    bumps: list[None] = []
    bump = store._bump_evidence_generation

    def count_bump() -> None:
        bumps.append(None)
        bump()

    monkeypatch.setattr(store, "_bump_evidence_generation", count_bump)
    outcome: dict[str, object] = {}

    def first_store() -> None:
        try:
            outcome["first"] = store.store(content)
        except BaseException as exc:  # noqa: BLE001
            outcome["first"] = exc

    writer = threading.Thread(target=first_store)
    first["writer"] = writer
    writer.start()
    try:
        assert paused.wait(10), "the first writer never started writing"
        # Mid-write, the object is not there yet, never there and corrupt.
        assert store.availability(digest) == "missing"
        second = store.store(content)
        assert second.digest == digest
    finally:
        release.set()
        writer.join(10)
    assert not isinstance(outcome["first"], BaseException), outcome["first"]
    assert outcome["first"].digest == digest  # type: ignore[union-attr]
    assert store.read(digest, access=READ) == content
    shard = store._algorithm_root / digest.removeprefix("sha256:")[:2]
    assert sorted(path.name for path in shard.iterdir()) == [digest.removeprefix("sha256:")]
    # One body arrived, so the evidence generation advanced once: the loser
    # verified the winner's object and published nothing of its own.
    assert len(bumps) == 1


@pytest.mark.parametrize("substitute", ["symlink", "hard link"])
def test_a_temporary_name_substituted_after_readback_is_refused_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, substitute: str
) -> None:
    root = tmp_path / "cas"
    root.mkdir()
    store = ContentAddressedBodyStore(root)
    content = b"valid bytes, but outside the store's custody\n"
    digest = store.digest_bytes(content).tagged
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    generation = store.evidence_generation()
    real_link = os.link

    def substitute_then_link(src: str, dst: str, **kwargs: object) -> None:
        # Between read-back and link, the temporary name stops being the
        # verified file: same bytes, different (or no) inode.
        source_dir = kwargs["src_dir_fd"]
        assert isinstance(source_dir, int)
        os.unlink(src, dir_fd=source_dir)
        if substitute == "symlink":
            os.symlink(str(outside), src, dir_fd=source_dir)
        else:
            real_link(outside, src, dst_dir_fd=source_dir)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patch:
        patch.setattr(cas.os, "link", substitute_then_link)
        with pytest.raises(CasError, match="did not link the verified body"):
            store.store(content)

    assert store.availability(digest) == "missing"
    assert store.evidence_generation() == generation
    shard = store._algorithm_root / digest.removeprefix("sha256:")[:2]
    assert list(shard.iterdir()) == []
    # Nothing outside the store gained a link into it, and it is unchanged.
    assert os.stat(outside).st_nlink == 1
    assert outside.read_bytes() == content
    # The refusal left the address free for an honest store.
    assert store.store(content).digest == digest
    assert store.read(digest, access=READ) == content
