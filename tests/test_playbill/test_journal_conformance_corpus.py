"""Conformance of Core's journal surfaces against the frozen corpus.

The corpus under ``tests/goldens/playbill/journal_corpus/`` is a version-frozen
persisted format. Its bytes were produced by calling only the pure Core
encoders in ``cruxible_core.playbill.exhaust`` and are committed verbatim; no
generator lives in this repository and no test here may rewrite them. Every
assertion runs the *committed* bytes through Core's own parser, verifier, and
local journal backend.

A positive vector that stops verifying, or a negative fixture that starts being
accepted, is a persisted-format break to be reviewed under a new format tag --
never a regeneration event.

What is deliberately not frozen is exception prose: Core publishes
``PlaybillJournalError`` and no typed journal diagnostic codes, so each negative
fixture names a stable law category and the stage that must refuse it, and the
tests assert refusal by that error type alone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillJournalError
from cruxible_core.playbill.exhaust import (
    JournalExportBundleV1,
    JournalHeadManifestV1,
    JournalHeadVectorV1,
    JournalPartitionHeadV1,
    JournalRangeV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureJournalRecordDraftV1,
    StoredProcedureJournalRecordV1,
    build_journal_export,
    import_journal_export,
    parse_journal_export,
    render_journal_export,
    verify_journal_head_manifest,
)

CORPUS = Path(__file__).resolve().parents[1] / "goldens" / "playbill" / "journal_corpus"
CORPUS_FORMAT = "cloud-journal-conformance-corpus-v1"
INDEX_CORPUS_ID = "index"
INDEX_RELPATH = "index.json"

LAW_CATEGORIES = (
    "bad_boundary",
    "duplicated_record",
    "expected_head",
    "fencing",
    "forked_chain",
    "missing_record",
    "missing_segment",
    "overlapping_segments",
    "reordered_segments",
    "tampered_manifest",
)
REFUSAL_STAGES = ("parse", "import")

# The registered SHA-256 of every committed corpus file, one `corpus_id digest`
# pair per line, transcribed from the corpus owner's contract registration.
# Regenerating or editing a frozen vector fails here first, and loudly.
CORPUS_DIGEST_REGISTRATION = """
export-alpha-a-1-3 643380f99784e1facc9539ddd66868c841beebaba00783ca7d6d644885ea835c
export-alpha-a-4-6 138a341e49b8e52e2f4a999f09bd72e94b13e049cf20d0213d35c5de67094e83
export-duplicated-record d9ef17f119ef9108172f30f940ca24eaea1871bcb1bfccf77814bbbc65648b80
export-false-oversized-claim 871a6a1ebcd6afb3a9cbd7e1ea5c7dba3e33edd5c28fead3c0d4ba67f19eea3d
export-forked-chain 350d0c36ed015a1930a9244913e9e4dd97571a1780e24002a3e7e368c8a04d5b
export-missing-record 56a2878f45763eeab5b82aa3a28465daeed8eef661444e1909d2386c0437b69d
export-missing-segment db7a4f427ceb91cba37b18f9a0877ba954db76fcd9ff1d52f3411a830b5ddbf8
export-overlapping-segments 9678d4dd52b83b3772e50b0e9976e34208d23ec04ccee812df7d9a072b245a0e
export-reordered-segments b77f192655728976f440638d0de398328b2cb3b9f234e43f6cf3c68e9717464f
export-segment-boundary 37a83b27d92905c8e8d0f02e929cde0ba3b71bc0f24d5ec095a83ae205dcfa4c
export-tampered-head-signature b103ca8c6483c77e5d4d76f5b6569b6a4717d4d9d467d3aa109151cecd668271
export-tampered-head-vector 188aef4bed107b1fcb3a48cda5e8c57e574c53b7a57594282ac807f6354b4757
export-two-partitions c131873b21fb14eda4c64ef73538ee01ce565759dbce7c376dd79a1eb914c92f
export-unknown-boundary-rule de6da45e8ffdc27410e266331b6bc0f2713628fe2370bc92d75102019978d5a8
head-manifest-two-partitions 5419f92db75cc836dd0043549b27e8eb644ab150b04cf18b95de4671c4ce6675
head-vector-two-partitions 79de4ec6e0d1086a9461fd0622147ea261626dc2034e5c3b17ecc959200b0ba4
index 905e2aaf35e5a17dee7504ae32ec4305c6abede26654dfedbf8f4f92f9411836
journal-range-alpha-a-1-3 92dcf12efe4c393521549417a3589380e767e095d668c027ee9891526c48389e
partition-head-alpha-a-3 0236e98e4ca3b7eade27f7dcacdd2294f108ec734576e060b9b96e4f23bc5caf
partition-head-genesis ce290a1f9dea659f8b909ab8935f6ef07563ac64438cb3cc836b2140b4a480cd
stored-record-alpha-a-1 f1dd635d62eca30fcc25b26eba6297a5640f18b49a717019f89d8e4ade708458
stream-identity-alpha 6b9553c0a5dd64ae665941577760057dc78f26d6cbaa9ff5d5b368660b410134
stream-identity-beta d20adf507b2fba2636175bdc7fe10faeaff47143bbd2041d465dfc4ac4a553f9
"""

CORPUS_DIGESTS: dict[str, str] = {
    corpus_id: digest
    for corpus_id, digest in (
        line.split() for line in CORPUS_DIGEST_REGISTRATION.strip().splitlines()
    )
}


def _corpus_bytes(relative_path: str) -> bytes:
    """Read one committed corpus file's exact bytes through a safe relative path."""
    if (
        relative_path.startswith("/")
        or ".." in relative_path.split("/")
        or "\\" in relative_path
        or not relative_path.endswith(".json")
    ):
        raise AssertionError(f"corpus path {relative_path!r} is not a safe relative JSON path")
    return (CORPUS / relative_path).read_bytes()


def _load_index() -> dict[str, Any]:
    index: dict[str, Any] = json.loads(_corpus_bytes(INDEX_RELPATH))
    assert index["tag"] == CORPUS_FORMAT, "unknown corpus format tag; this branch fails closed"
    return index


INDEX = _load_index()
POSITIVE: tuple[dict[str, Any], ...] = tuple(INDEX["positive"])
NEGATIVE: tuple[dict[str, Any], ...] = tuple(INDEX["negative"])
SCENARIOS: tuple[dict[str, Any], ...] = tuple(INDEX["scenarios"])
HEAD_PUBLIC_KEY: str = INDEX["head_public_key"]
WITNESS_PUBLIC_KEY: str = INDEX["witness_public_key"]


def _vector(vector_id: str) -> dict[str, Any]:
    for entry in POSITIVE:
        if entry["vector_id"] == vector_id:
            return entry
    raise AssertionError(f"corpus index has no positive vector {vector_id!r}")


def _scenario(law_category: str, kind: str) -> dict[str, Any]:
    for entry in SCENARIOS:
        if entry["law_category"] == law_category and entry["kind"] == kind:
            return entry
    raise AssertionError(f"corpus index has no {law_category!r}/{kind!r} refusal scenario")


def _committed_relative_paths() -> tuple[str, ...]:
    return tuple(
        sorted(str(path.relative_to(CORPUS)) for path in CORPUS.rglob("*") if path.is_file())
    )


def _canonical(model: Any) -> bytes:
    """Render a Core model the way the committed corpus was rendered."""
    return canonical_bytes(model.model_dump(mode="json")) + b"\n"


def _backend(tmp_path: Path, name: str) -> LocalJournalBackend:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return LocalJournalBackend(root)


def _loaded_backend(
    tmp_path: Path, vector: dict[str, Any], name: str = "home"
) -> LocalJournalBackend:
    """Import a vector's prerequisites into a fresh journal home."""
    backend = _backend(tmp_path, name)
    for prerequisite in vector.get("prerequisites", ()):
        import_journal_export(
            backend,
            parse_journal_export(_corpus_bytes(_vector(prerequisite)["path"])),
            expected_head_public_key=HEAD_PUBLIC_KEY,
        )
    return backend


def _draft_from(stored: StoredProcedureJournalRecordV1) -> ProcedureJournalRecordDraftV1:
    """Derive an appendable draft from a frozen stored record, chain coordinates dropped."""
    document = stored.record.model_dump(mode="json")
    for assigned in ("tag", "sequence", "previous_record_digest"):
        document.pop(assigned)
    return ProcedureJournalRecordDraftV1.model_validate(document)


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


def _corpus_id_paths() -> tuple[tuple[str, str], ...]:
    """Return ``(corpus_id, corpus-relative path)`` for the index and every vector."""
    entries = [(INDEX_CORPUS_ID, INDEX_RELPATH)]
    entries.extend((entry["vector_id"], entry["path"]) for entry in POSITIVE)
    entries.extend((entry["fixture_id"], entry["path"]) for entry in NEGATIVE)
    return tuple(entries)


def test_the_committed_corpus_matches_its_registered_digests() -> None:
    """Any regeneration or edit of a frozen vector fails here."""
    actual = {
        path: hashlib.sha256(_corpus_bytes(path)).hexdigest()
        for path in _committed_relative_paths()
    }
    registered = {path: CORPUS_DIGESTS[corpus_id] for corpus_id, path in _corpus_id_paths()}
    assert actual == registered


def test_the_index_registers_every_committed_corpus_file_exactly_once() -> None:
    identifiers = [corpus_id for corpus_id, _ in _corpus_id_paths()]
    paths = [path for _, path in _corpus_id_paths()]

    assert len(set(identifiers)) == len(identifiers)
    assert sorted(paths) == sorted(_committed_relative_paths())
    assert set(identifiers) == set(CORPUS_DIGESTS)


def test_every_declared_law_category_and_refusal_stage_is_covered() -> None:
    covered = {entry["law_category"] for entry in NEGATIVE}
    covered |= {entry["law_category"] for entry in SCENARIOS}
    assert covered == set(LAW_CATEGORIES)
    assert {entry["refusal_stage"] for entry in NEGATIVE} == set(REFUSAL_STAGES)


# ---------------------------------------------------------------------------
# Positive vectors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", POSITIVE, ids=lambda vector: str(vector["vector_id"]))
def test_every_positive_vector_verifies_through_core(
    vector: dict[str, Any], tmp_path: Path
) -> None:
    raw = _corpus_bytes(vector["path"])
    decoded = json.loads(raw)
    expectations = vector["expectations"]
    shape = vector["shape"]

    if shape == "playbill-journal-stream-identity-v1":
        identity = JournalStreamIdentityV1.model_validate(decoded)
        assert _canonical(identity) == raw
        assert identity.identity_digest == expectations["identity_digest"]
    elif shape == "playbill-journal-partition-head-v1":
        head = JournalPartitionHeadV1.model_validate(decoded)
        assert _canonical(head) == raw
        assert head.sequence == expectations["sequence"]
        assert head.record_digest == expectations["record_digest"]
    elif shape == "playbill-journal-head-vector-v1":
        head_vector = JournalHeadVectorV1.model_validate(decoded)
        assert _canonical(head_vector) == raw
        assert head_vector.vector_digest == expectations["vector_digest"]
        assert len(head_vector.partitions) == expectations["partition_count"]
    elif shape == "playbill-journal-head-manifest-v1":
        manifest = JournalHeadManifestV1.model_validate(decoded)
        assert _canonical(manifest) == raw
        verify_journal_head_manifest(manifest, expected_public_key=HEAD_PUBLIC_KEY)
        assert manifest.statement.signer_id == expectations["signer_id"]
        assert manifest.statement.signing_key_id == expectations["signing_key_id"]
        assert manifest.statement.signing_role == expectations["signing_role"]
        assert (
            _partition_head_facts(manifest.statement.head_vector.partitions)
            == expectations["partition_heads"]
        )
    elif shape == "playbill-journal-range-v1":
        journal_range = JournalRangeV1.model_validate(decoded)
        assert _canonical(journal_range) == raw
        assert journal_range.first_sequence == expectations["first_sequence"]
        assert journal_range.last_sequence == expectations["last_sequence"]
        assert journal_range.expected_head_digest == expectations["expected_head_digest"]
    elif shape == "playbill-stored-procedure-journal-record-v1":
        stored = StoredProcedureJournalRecordV1.model_validate(decoded)
        assert _canonical(stored) == raw
        assert stored.record_digest == expectations["record_digest"]
        assert stored.record.sequence == expectations["sequence"]
    elif shape == "playbill-journal-export-bundle-v1":
        bundle = parse_journal_export(raw)
        assert render_journal_export(bundle) == raw
        assert bundle.manifest.boundary_rule == expectations["boundary_rule"]
        assert len(bundle.manifest.segments) == expectations["segment_count"]
        assert (
            sum(segment.record_count for segment in bundle.manifest.segments)
            == expectations["record_count"]
        )
        backend = _loaded_backend(tmp_path, vector)
        heads = import_journal_export(backend, bundle, expected_head_public_key=HEAD_PUBLIC_KEY)
        assert _partition_head_facts(heads) == expectations["partition_heads"]
    else:  # pragma: no cover - the index is frozen and declares no other shape
        raise AssertionError(f"unhandled vector shape {shape!r}")


def _partition_head_facts(
    heads: tuple[JournalPartitionHeadV1, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "instance_id": head.stream.instance_id,
            "journal_family": head.stream.journal_family,
            "partition_id": head.partition_id,
            "record_digest": head.record_digest,
            "sequence": head.sequence,
            "stream_id": head.stream.stream_id,
        }
        for head in heads
    ]


def _export_vectors() -> tuple[dict[str, Any], ...]:
    return tuple(
        entry for entry in POSITIVE if entry["shape"] == "playbill-journal-export-bundle-v1"
    )


@pytest.mark.parametrize("vector", _export_vectors(), ids=lambda vector: str(vector["vector_id"]))
def test_every_export_round_trips_byte_identically_through_a_local_home(
    vector: dict[str, Any], tmp_path: Path
) -> None:
    """Import the frozen bytes into a fresh home; re-export reproduces them exactly."""
    raw = _corpus_bytes(vector["path"])
    bundle = parse_journal_export(raw)
    backend = _loaded_backend(tmp_path, vector)
    import_journal_export(backend, bundle, expected_head_public_key=HEAD_PUBLIC_KEY)

    reexported = build_journal_export(
        backend,
        ranges=bundle.manifest.ranges,
        head_manifest=bundle.manifest.head_manifest,
    )
    assert render_journal_export(reexported) == raw


def test_importing_the_same_export_twice_is_idempotent(tmp_path: Path) -> None:
    vector = _vector("export-alpha-a-1-3")
    bundle = parse_journal_export(_corpus_bytes(vector["path"]))
    backend = _loaded_backend(tmp_path, vector)

    first = import_journal_export(backend, bundle, expected_head_public_key=HEAD_PUBLIC_KEY)
    second = import_journal_export(backend, bundle, expected_head_public_key=HEAD_PUBLIC_KEY)

    assert first == second


# ---------------------------------------------------------------------------
# Negative fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", NEGATIVE, ids=lambda fixture: str(fixture["fixture_id"]))
def test_every_negative_fixture_is_refused_at_its_declared_stage(
    fixture: dict[str, Any], tmp_path: Path
) -> None:
    raw = _corpus_bytes(fixture["path"])

    if fixture["refusal_stage"] == "parse":
        with pytest.raises(PlaybillJournalError):
            parse_journal_export(raw)
        return

    bundle = parse_journal_export(raw)
    with pytest.raises(PlaybillJournalError):
        import_journal_export(
            _backend(tmp_path, "home"),
            bundle,
            expected_head_public_key=HEAD_PUBLIC_KEY,
        )


@pytest.mark.parametrize("fixture", NEGATIVE, ids=lambda fixture: str(fixture["fixture_id"]))
def test_no_negative_fixture_is_accidentally_a_positive_vector(fixture: dict[str, Any]) -> None:
    raw = _corpus_bytes(fixture["path"])
    assert all(_corpus_bytes(entry["path"]) != raw for entry in POSITIVE)


# ---------------------------------------------------------------------------
# Refusal scenarios: preconditions rather than byte patterns
# ---------------------------------------------------------------------------


def test_an_expected_head_gap_refuses_a_missing_prefix(tmp_path: Path) -> None:
    """A well-formed export that skips a prefix is refused by an empty home."""
    scenario = _scenario("expected_head", "import_into_empty_backend")
    vector = _vector(scenario["vector_id"])
    assert vector["prerequisites"], "the scenario needs a vector that extends a prefix"
    bundle = parse_journal_export(_corpus_bytes(vector["path"]))

    with pytest.raises(PlaybillJournalError):
        import_journal_export(
            _backend(tmp_path, "empty"),
            bundle,
            expected_head_public_key=HEAD_PUBLIC_KEY,
        )


def _fenced_home(
    tmp_path: Path,
) -> tuple[LocalJournalBackend, ProcedureJournalRecordDraftV1, JournalPartitionHeadV1]:
    """A home holding the frozen prefix, plus the next draft its writer would append."""
    bundle = parse_journal_export(_corpus_bytes(_vector("export-alpha-a-1-3")["path"]))
    backend = _backend(tmp_path, "home")
    (head,) = import_journal_export(backend, bundle, expected_head_public_key=HEAD_PUBLIC_KEY)
    stored = StoredProcedureJournalRecordV1.model_validate(
        json.loads(_corpus_bytes(_vector("stored-record-alpha-a-1")["path"]))
    )
    return backend, _draft_from(stored), head


def test_an_unknown_fencing_token_refuses_an_append(tmp_path: Path) -> None:
    scenario = _scenario("fencing", "append_under_unknown_token")
    assert scenario["vector_id"] == "export-alpha-a-1-3"
    backend, draft, head = _fenced_home(tmp_path)

    with pytest.raises(PlaybillJournalError):
        backend.append(
            draft,
            expected_head=head,
            fencing_token="cj0-fencing-token-never-activated",
        )


def test_a_superseded_fencing_token_refuses_an_append(tmp_path: Path) -> None:
    scenario = _scenario("fencing", "append_under_superseded_token")
    assert scenario["vector_id"] == "export-alpha-a-1-3"
    backend, draft, head = _fenced_home(tmp_path)
    token = "cj0-conformance-writer-token"

    backend.activate_writer(
        draft.stream, draft.partition_id, fencing_token=token, expected_head=head
    )
    backend.fence_writer(draft.stream, draft.partition_id, expected_fencing_token=token)

    with pytest.raises(PlaybillJournalError):
        backend.append(draft, expected_head=head, fencing_token=token)


def test_the_same_append_succeeds_under_the_active_fencing_token(tmp_path: Path) -> None:
    """Positive control: the fencing refusals above isolate the token, not the draft."""
    backend, draft, head = _fenced_home(tmp_path)
    token = "cj0-conformance-writer-token"
    backend.activate_writer(
        draft.stream, draft.partition_id, fencing_token=token, expected_head=head
    )

    stored = backend.append(draft, expected_head=head, fencing_token=token)

    assert stored.record.sequence == head.sequence + 1
    assert stored.record.previous_record_digest == head.record_digest


# ---------------------------------------------------------------------------
# The signed head commits a role, not merely a signature
# ---------------------------------------------------------------------------


def test_the_head_signer_role_is_distinct_from_the_witness() -> None:
    assert HEAD_PUBLIC_KEY != WITNESS_PUBLIC_KEY
    manifest = JournalHeadManifestV1.model_validate(
        json.loads(_corpus_bytes(_vector("head-manifest-two-partitions")["path"]))
    )

    verify_journal_head_manifest(manifest, expected_public_key=HEAD_PUBLIC_KEY)
    with pytest.raises(PlaybillJournalError):
        verify_journal_head_manifest(manifest, expected_public_key=WITNESS_PUBLIC_KEY)


def test_an_export_carrying_a_foreign_head_key_never_imports(tmp_path: Path) -> None:
    bundle: JournalExportBundleV1 = parse_journal_export(
        _corpus_bytes(_vector("export-alpha-a-1-3")["path"])
    )

    with pytest.raises(PlaybillJournalError):
        import_journal_export(
            _backend(tmp_path, "home"),
            bundle,
            expected_head_public_key=WITNESS_PUBLIC_KEY,
        )
