"""PB-C Document v1, inert CAS, digest, and acceptance-law tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.documents import (
    AcceptedDocument,
    DocumentAuthority,
    DocumentLifecycle,
    DocumentLink,
    DocumentPin,
    DocumentShell,
    document_digest,
    evaluate_document_law,
    parse_document,
    render_document,
)
from cruxible_client.contracts.errors import DocumentFormatError, PlaybillCasError
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.instance import PlaybillInstance
from tests.test_playbill._support import initialize_local


def _store(tmp_path: Path) -> ContentAddressedBodyStore:
    root = tmp_path / "cas"
    root.mkdir()
    return ContentAddressedBodyStore(root)


def _shell(
    body_digest: str,
    *,
    revision: int = 1,
    predecessor_digest: str | None = None,
    title: str = "Playbill design",
) -> DocumentShell:
    return DocumentShell(
        identity="document:playbill-design",
        document_kind="design",
        title=title,
        media_type="text/markdown",
        body_digest=body_digest,
        links=(
            DocumentLink(
                relation="implements",
                target_identity="document:implementation-program",
            ),
        ),
        pins=(
            DocumentPin(
                role="reference",
                target_identity="document:reference",
                target_digest="sha256:" + "11" * 32,
            ),
        ),
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        predecessor_digest=predecessor_digest,
        lifecycle=DocumentLifecycle(revision=revision),
    )


def test_cas_store_is_inert_idempotent_verified_and_access_controlled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = "# Café\n\nExact bytes.\n".encode()
    first = store.store(body)
    second = store.store(body)
    assert first == second
    assert store.verify(first.digest)

    denied = BodyAccessContext(principal_id="reader")
    allowed = BodyAccessContext(principal_id="owner", can_read_body=True)
    assert store.metadata(first.digest, access=denied).model_dump() == {
        "digest": first.digest,
        "present": True,
        "byte_length": None,
        "redacted": True,
    }
    with pytest.raises(PlaybillCasError, match="denied"):
        store.read(first.digest, access=denied)
    assert store.read(first.digest, access=allowed) == body
    assert store.metadata(first.digest, access=allowed).byte_length == len(body)

    digest_hex = first.digest.removeprefix("sha256:")
    stored_path = store.root / "sha256" / digest_hex[:2] / digest_hex
    stored_path.write_bytes(b"swapped")
    with pytest.raises(PlaybillCasError, match="do not match"):
        store.verify(first.digest)


def test_document_wire_format_is_strict_discriminated_and_path_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = store.store(b"body")
    shell = _shell(body.digest)
    rendered = render_document(shell)
    assert parse_document(rendered, path="documents/playbill-design.yaml") == shell

    payload = json.loads(rendered)
    payload["body_path"] = "/tmp/body.md"
    with pytest.raises(DocumentFormatError, match="strict v1"):
        parse_document(json.dumps(payload).encode(), path="documents/playbill-design.yaml")

    payload = json.loads(rendered)
    payload["tag"] = "playbill-document-v2"
    with pytest.raises(DocumentFormatError, match="unsupported"):
        parse_document(json.dumps(payload).encode(), path="documents/playbill-design.yaml")

    with pytest.raises(DocumentFormatError, match="identity/path"):
        parse_document(rendered, path="documents/renamed.yaml")


def test_document_model_refuses_malformed_media_links_pins_and_authority(tmp_path: Path) -> None:
    digest = _store(tmp_path).store(b"body").digest
    shell = _shell(digest)
    payload = shell.model_dump(mode="json")

    with pytest.raises(ValidationError, match="media_type"):
        DocumentShell.model_validate({**payload, "media_type": "README.md"})
    with pytest.raises(ValidationError, match="links"):
        DocumentShell.model_validate({**payload, "links": [shell.links[0], shell.links[0]]})
    with pytest.raises(ValidationError, match="target_digest"):
        DocumentPin(role="reference", target_identity="document:x", target_digest="latest")
    authority = payload["authority"]
    assert isinstance(authority, dict)
    authority["approval_roles"] = ["owner"]
    with pytest.raises(ValidationError, match="approval_roles"):
        DocumentShell.model_validate(payload)


def test_document_acceptance_requires_exact_body_and_predecessor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    body = store.store(b"first body")
    initial = _shell(body.digest)
    accepted = evaluate_document_law(
        initial,
        path="documents/playbill-design.yaml",
        bodies=store,
        predecessor=None,
    )
    assert accepted.verdict == "accepted"
    assert accepted.envelope_digest == document_digest(initial).tagged
    assert accepted.required_tier == "graph_write"
    assert accepted.activation_policy == "snapshot"

    missing = initial.model_copy(update={"body_digest": "sha256:" + "ff" * 32})
    refusal = evaluate_document_law(
        missing,
        path="documents/playbill-design.yaml",
        bodies=store,
        predecessor=None,
    )
    assert refusal.verdict == "refused"
    assert [item.code for item in refusal.diagnostics] == ["playbill.document.body_missing"]

    predecessor = AcceptedDocument(
        path="documents/playbill-design.yaml",
        shell=initial,
        envelope_digest=document_digest(initial).tagged,
    )
    next_body = store.store(b"second body")
    successor = _shell(
        next_body.digest,
        revision=2,
        predecessor_digest=predecessor.envelope_digest,
        title="Playbill design v2",
    )
    next_result = evaluate_document_law(
        successor,
        path=predecessor.path,
        bodies=store,
        predecessor=predecessor,
    )
    assert next_result.verdict == "accepted"
    assert next_result.envelope_digest != predecessor.envelope_digest

    stale = successor.model_copy(update={"predecessor_digest": "sha256:" + "22" * 32})
    stale_result = evaluate_document_law(
        stale,
        path=predecessor.path,
        bodies=store,
        predecessor=predecessor,
    )
    assert [item.code for item in stale_result.diagnostics] == [
        "playbill.document.stale_predecessor"
    ]


def test_corrupt_cas_object_refuses_document_binding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    metadata = store.store(b"original")
    shell = _shell(metadata.digest)
    digest_hex = metadata.digest.removeprefix("sha256:")
    path = store.root / "sha256" / digest_hex[:2] / digest_hex
    os.chmod(path, 0o600)
    path.write_bytes(b"substituted")

    result = evaluate_document_law(
        shell,
        path="documents/playbill-design.yaml",
        bodies=store,
        predecessor=None,
    )
    assert result.verdict == "refused"
    assert result.diagnostics[0].code == "playbill.document.body_missing"


def test_cas_reads_refuse_a_symlinked_digest_shard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = store.digest_bytes(b"body")
    external = tmp_path / "external"
    external.mkdir()
    (external / digest.value).write_bytes(b"body")
    shard = store.root / "sha256" / digest.value[:2]
    shard.symlink_to(external, target_is_directory=True)

    with pytest.raises(PlaybillCasError, match="shard"):
        store.verify(digest.tagged)


def test_real_instance_body_store_survives_reopen_without_changing_main(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    before = instance.inspect()
    stored = instance.store_document_body(b"inert proposal body")
    assert instance.inspect().head_oid == before.head_oid

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.body_store().verify(stored.digest)
    assert reopened.inspect().semantic_root == before.semantic_root
