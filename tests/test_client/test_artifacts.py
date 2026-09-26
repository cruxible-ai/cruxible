"""Cruxible artifacts: deterministic OCI packing, layouts and registry transport."""

from __future__ import annotations

import base64
import io
import json
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest

from cruxible_client.artifacts import (
    ArtifactImage,
    ArtifactKind,
    BlobCache,
    RegistryClient,
    pack_artifact,
    pack_files,
    parse_reference,
    read_layout,
    unpack_artifact,
    unpack_files,
    write_layout,
)
from cruxible_client.artifacts.oci import sha256_digest
from cruxible_client.artifacts.registry import environment_credentials
from cruxible_client.contracts.kits import (
    KitArtifactBytesV1,
    KitArtifactV1,
    KitBundleV1,
    KitManifestV1,
)
from cruxible_client.kits import KIT_ARTIFACT, fetch_kit_image, resolve_kit, write_kit_layout


class FakeRegistry:
    """The registry-v2 subset artifacts use, behind a bearer-token challenge."""

    def __init__(
        self, *, realm: str = "https://registry.test/token", upload_origin: str | None = None
    ) -> None:
        self.realm = realm
        self.upload_origin = upload_origin
        self.upload_requests: list[httpx.Request] = []
        self.oversized: set[str] = set()
        self.cdn_origin: str | None = None
        self.cdn_requests: list[httpx.Request] = []
        self.upload_redirect: str | None = None
        self.token_body: bytes | None = None
        self.error_body: bytes | None = None
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, str] = {}
        self.blob_gets = 0
        self.token_requests: list[httpx.Request] = []
        self.corrupt: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/token":
            self.token_requests.append(request)
            if self.token_body is not None:
                return httpx.Response(200, content=self.token_body)
            return httpx.Response(200, json={"token": "granted"})
        if url.path.startswith("/cdn/"):
            self.cdn_requests.append(request)
            return httpx.Response(200, content=self.blobs[url.path.removeprefix("/cdn/")])
        if url.path.startswith("/upload/") and self.upload_redirect is not None:
            self.upload_requests.append(request)
            return httpx.Response(307, headers={"location": self.upload_redirect})
        if self.error_body is not None and request.headers.get("authorization"):
            return httpx.Response(500, content=self.error_body)
        if url.path.startswith("/upload/"):
            self.upload_requests.append(request)
            self.blobs[url.params["digest"]] = request.content
            return httpx.Response(201)
        if request.headers.get("authorization") != "Bearer granted":
            challenge = (
                f'Bearer realm="{self.realm}",service="registry.test",scope="repository:x:pull"'
            )
            return httpx.Response(401, headers={"www-authenticate": challenge})
        parts = url.path.split("/")
        # /v2/<repo...>/<kind>/<ref>
        kind_index = max(
            index for index, part in enumerate(parts) if part in {"blobs", "manifests"}
        )
        repository = "/".join(parts[2:kind_index])
        kind, rest = parts[kind_index], "/".join(parts[kind_index + 1 :])
        if kind == "blobs" and rest.startswith("uploads"):
            if request.method == "POST":
                upload = uuid.uuid4().hex
                self.uploads[upload] = repository
                location = (
                    f"{self.upload_origin}/upload/{upload}"
                    if self.upload_origin
                    else f"/v2/{repository}/blobs/uploads/{upload}"
                )
                return httpx.Response(202, headers={"location": location})
            digest = url.params["digest"]
            assert sha256_digest(request.content) == digest
            self.blobs[digest] = request.content
            return httpx.Response(201)
        if kind == "blobs":
            if rest not in self.blobs:
                return httpx.Response(404)
            if request.method == "HEAD":
                return httpx.Response(200)
            if self.cdn_origin is not None:
                return httpx.Response(307, headers={"location": f"{self.cdn_origin}/cdn/{rest}"})
            self.blob_gets += 1
            body = self.blobs[rest]
            if rest in self.oversized:
                return httpx.Response(200, content=body + b"x" * (1024 * 1024))
            if rest in self.corrupt:  # same length, different bytes
                body = body[:-1] + bytes([body[-1] ^ 1])
            return httpx.Response(200, content=body)
        if request.method == "PUT":
            digest = sha256_digest(request.content)
            self.manifests[(repository, rest)] = request.content
            self.manifests[(repository, digest)] = request.content
            return httpx.Response(201, headers={"docker-content-digest": digest})
        manifest = self.manifests.get((repository, rest))
        if manifest is None:
            return httpx.Response(404)
        return httpx.Response(200, content=manifest)

    def client(self, cache_root: Path, **kwargs: object) -> RegistryClient:
        return RegistryClient(
            transport=httpx.MockTransport(self.handler),
            cache=BlobCache(cache_root),
            **kwargs,  # type: ignore[arg-type]
        )


NOTE: ArtifactKind[dict[str, str]] = ArtifactKind(
    name="note",
    artifact_type="application/vnd.test.note.v1",
    config_media_type="application/vnd.test.note.config.v1+json",
    layer_media_type="application/vnd.test.note.layer.v1.tar",
    pack=lambda value: (
        json.dumps(sorted(value)).encode(),
        (pack_files({key: text.encode() for key, text in value.items()}),),
    ),
    unpack=lambda config, layers: {
        key: content.decode() for key, content in unpack_files(layers[0]).items()
    },
)


def _bundle() -> KitBundleV1:
    content = b'{"x": 1}\n'
    path = "claim-types/acme.account/seats.json"
    return KitBundleV1(
        manifest=KitManifestV1(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            artifacts=(KitArtifactV1(path=path, artifact_digest="sha256:" + "a" * 64),),
        ),
        artifacts=(KitArtifactBytesV1.of(path, content),),
    )


def test_packing_is_deterministic_and_order_independent() -> None:
    first = pack_files({"b/two.json": b"2", "a/one.json": b"1"})
    second = pack_files({"a/one.json": b"1", "b/two.json": b"2"})
    assert first == second
    assert unpack_files(first) == {"a/one.json": b"1", "b/two.json": b"2"}
    assert pack_artifact(NOTE, {"k": "v"}).digest == pack_artifact(NOTE, {"k": "v"}).digest


def _tar(member: tarfile.TarInfo, content: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("name", ["../escape.json", "/abs.json", "a/./b.json", "a//b.json"])
def test_a_layer_refuses_paths_outside_itself(name: str) -> None:
    with pytest.raises(ValueError, match="canonical relative path"):
        unpack_files(_tar(tarfile.TarInfo(name), b"{}"))


def test_a_layer_refuses_links() -> None:
    link = tarfile.TarInfo("claim-types/link.json")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    with pytest.raises(ValueError, match="not a regular file"):
        unpack_files(_tar(link))


def test_an_oci_layout_round_trips_and_refuses_tampered_blobs(tmp_path: Path) -> None:
    image = pack_artifact(NOTE, {"hello.txt": "hi"})
    write_layout(image, tmp_path / "layout", ref="note:1")
    assert unpack_artifact(NOTE, read_layout(tmp_path / "layout")) == {"hello.txt": "hi"}

    layer = image.layers[0].digest.removeprefix("sha256:")
    (tmp_path / "layout" / "blobs" / "sha256" / layer).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match its descriptor"):
        read_layout(tmp_path / "layout")


def test_an_artifact_of_another_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="not a kit"):
        unpack_artifact(KIT_ARTIFACT, pack_artifact(NOTE, {"a": "b"}))


def test_a_kind_the_library_has_never_seen_travels_through_a_registry(tmp_path: Path) -> None:
    registry = FakeRegistry()
    ref = parse_reference("registry.test/test/notes:1")
    with registry.client(tmp_path / "cache") as client:
        digest = client.push(pack_artifact(NOTE, {"a.txt": "alpha"}), ref)
        pulled = client.pull(ref.pinned(digest))
    assert unpack_artifact(NOTE, pulled) == {"a.txt": "alpha"}


def test_a_kit_pushes_and_resolves_by_digest_through_the_cache(tmp_path: Path) -> None:
    registry = FakeRegistry()
    bundle = _bundle()
    ref = parse_reference("registry.test/cruxible-ai/kits/acme:1.0.0")
    with registry.client(tmp_path / "cache") as client:
        digest = client.push(pack_artifact(KIT_ARTIFACT, bundle), ref)
        resolved, origin = resolve_kit(str(ref), registry=client)
        assert resolved == bundle
        assert origin == f"registry.test/cruxible-ai/kits/acme@{digest}"
        gets = registry.blob_gets
        again, _ = resolve_kit(origin, registry=client)
    assert again == bundle
    assert registry.blob_gets == gets  # served from the digest cache


def test_a_registry_serving_different_bytes_is_refused(tmp_path: Path) -> None:
    registry = FakeRegistry()
    ref = parse_reference("registry.test/test/notes:1")
    image = pack_artifact(NOTE, {"a.txt": "alpha"})
    with registry.client(tmp_path / "push-cache") as client:
        client.push(image, ref)
    registry.corrupt.add(image.layers[0].digest)
    with registry.client(tmp_path / "pull-cache") as client:
        with pytest.raises(ValueError, match="different bytes"):
            client.pull(ref)


def test_a_corrupted_cache_entry_is_fetched_again(tmp_path: Path) -> None:
    registry = FakeRegistry()
    ref = parse_reference("registry.test/test/notes:1")
    image = pack_artifact(NOTE, {"a.txt": "alpha"})
    with registry.client(tmp_path / "cache") as client:
        client.push(image, ref)
        client.pull(ref)
        cached = tmp_path / "cache" / "blobs" / "sha256"
        (cached / image.layers[0].digest.removeprefix("sha256:")).write_bytes(b"rot")
        before = registry.blob_gets
        assert unpack_artifact(NOTE, client.pull(ref)) == {"a.txt": "alpha"}
    assert registry.blob_gets == before + 1


@pytest.mark.parametrize(
    ("realm", "sent"),
    [("https://registry.test/token", True), ("https://attacker.test/token", False)],
)
def test_credentials_go_only_to_the_registrys_own_token_realm(
    tmp_path: Path, realm: str, sent: bool
) -> None:
    registry = FakeRegistry(realm=realm)
    with registry.client(
        tmp_path / "cache", credentials=lambda _host: ("user", "secret")
    ) as client:
        client.push(pack_artifact(NOTE, {"a.txt": "alpha"}), parse_reference("registry.test/t/n:1"))
    expected = "Basic " + base64.b64encode(b"user:secret").decode()
    assert any(r.headers.get("authorization") == expected for r in registry.token_requests) is sent


@pytest.mark.parametrize(
    ("text", "registry", "repository", "reference"),
    [
        ("project-state", "ghcr.io", "cruxible-ai/kits/project-state", "latest"),
        ("project-state:1.0.0", "ghcr.io", "cruxible-ai/kits/project-state", "1.0.0"),
        ("ghcr.io/acme/kits/foo:2", "ghcr.io", "acme/kits/foo", "2"),
        (
            "localhost:5000/kits/foo@sha256:" + "b" * 64,
            "localhost:5000",
            "kits/foo",
            "sha256:" + "b" * 64,
        ),
    ],
)
def test_references_resolve_short_names_under_the_default_namespace(
    text: str, registry: str, repository: str, reference: str
) -> None:
    ref = parse_reference(text)
    assert (ref.registry, ref.repository, ref.reference) == (registry, repository, reference)


def test_a_kit_layout_is_a_kit_source(tmp_path: Path) -> None:
    bundle = _bundle()
    digest = write_kit_layout(bundle, tmp_path / "acme-layout")
    resolved, origin = resolve_kit(str(tmp_path / "acme-layout"))
    assert resolved == bundle
    assert origin == f"acme-layout@{digest}"


def test_an_upload_location_on_another_origin_never_receives_the_registry_token(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(upload_origin="https://uploads.test")
    with registry.client(tmp_path / "cache") as client:
        client.push(pack_artifact(NOTE, {"a.txt": "alpha"}), parse_reference("registry.test/t/n:1"))
    assert registry.upload_requests
    assert all("authorization" not in request.headers for request in registry.upload_requests)


def test_an_upload_location_downgrading_to_http_is_refused(tmp_path: Path) -> None:
    registry = FakeRegistry(upload_origin="http://registry.test")
    with registry.client(tmp_path / "cache") as client:
        with pytest.raises(ValueError, match="does not use https"):
            client.push(
                pack_artifact(NOTE, {"a.txt": "alpha"}), parse_reference("registry.test/t/n:1")
            )


def test_environment_credentials_belong_to_one_named_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_REGISTRY", "ghcr.io")
    monkeypatch.setenv("CRUXIBLE_REGISTRY_USERNAME", "user")
    monkeypatch.setenv("CRUXIBLE_REGISTRY_PASSWORD", "secret")
    assert environment_credentials("ghcr.io") == ("user", "secret")
    assert environment_credentials("attacker.example") is None
    monkeypatch.delenv("CRUXIBLE_REGISTRY")
    assert environment_credentials("ghcr.io") is None


def test_a_blob_larger_than_its_descriptor_is_refused_while_streaming(tmp_path: Path) -> None:
    registry = FakeRegistry()
    ref = parse_reference("registry.test/test/notes:1")
    image = pack_artifact(NOTE, {"a.txt": "alpha"})
    with registry.client(tmp_path / "push-cache") as client:
        client.push(image, ref)
    registry.oversized.add(image.layers[0].digest)
    with registry.client(tmp_path / "pull-cache") as client:
        with pytest.raises(ValueError, match="more than"):
            client.pull(ref)


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_a_layer_with_extension_headers_is_refused_before_they_are_parsed(fmt: int) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as archive:
        info = tarfile.TarInfo("claim-types/" + "x" * 200 + ".json")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="not a regular file"):
        unpack_files(buffer.getvalue())


def test_an_offline_pull_keeps_the_published_bytes_and_digest(tmp_path: Path) -> None:
    registry = FakeRegistry()
    image = pack_artifact(KIT_ARTIFACT, _bundle())
    # A valid artifact built by another tool: same content, different manifest bytes.
    manifest = json.loads(image.manifest)
    manifest["annotations"] = {"org.opencontainers.image.source": "https://example.test"}
    published = ArtifactImage(
        manifest=json.dumps(manifest).encode(), config=image.config, layers=image.layers
    )
    ref = parse_reference("registry.test/cruxible-ai/kits/acme:1.0.0")
    with registry.client(tmp_path / "cache") as client:
        digest = client.push(published, ref)
        fetched, origin = fetch_kit_image(str(ref), registry=client)
    write_layout(fetched, tmp_path / "offline", ref=origin)
    assert read_layout(tmp_path / "offline").digest == digest != image.digest


def test_a_redirect_to_another_origin_is_followed_without_the_registry_token(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry()
    ref = parse_reference("registry.test/test/notes:1")
    with registry.client(tmp_path / "push-cache") as client:
        client.push(pack_artifact(NOTE, {"a.txt": "alpha"}), ref)
    registry.cdn_origin = "https://cdn.test"
    with registry.client(tmp_path / "pull-cache") as client:
        assert unpack_artifact(NOTE, client.pull(ref)) == {"a.txt": "alpha"}
    assert registry.cdn_requests
    assert all("authorization" not in request.headers for request in registry.cdn_requests)


def test_a_redirect_that_downgrades_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    registry = FakeRegistry(upload_origin="https://registry.test")
    registry.upload_redirect = "http://registry.test/plain"
    with registry.client(tmp_path / "cache") as client:
        with pytest.raises(ValueError, match="does not use https"):
            client.push(
                pack_artifact(NOTE, {"a.txt": "alpha"}), parse_reference("registry.test/t/n:1")
            )
    assert all(request.url.scheme == "https" for request in registry.upload_requests)


def test_an_oversized_token_response_is_refused(tmp_path: Path) -> None:
    registry = FakeRegistry()
    registry.token_body = b"x" * (1024 * 1024)
    with registry.client(tmp_path / "cache") as client:
        with pytest.raises(ValueError, match="more than"):
            client.pull(parse_reference("registry.test/t/n:1"))


def test_an_oversized_error_body_is_read_only_up_to_its_bound(tmp_path: Path) -> None:
    registry = FakeRegistry()
    registry.error_body = b"x" * (8 * 1024 * 1024)
    with registry.client(tmp_path / "cache") as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.pull(parse_reference("registry.test/t/n:1"))


def test_an_artifact_over_the_total_budget_is_refused_before_any_blob_is_fetched(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry()
    image = pack_artifact(NOTE, {"a.txt": "alpha"})
    manifest = json.loads(image.manifest)
    manifest["layers"] = [dict(manifest["layers"][0], size=300 * 1024 * 1024)] * 2
    registry.manifests[("t/n", "1")] = json.dumps(manifest).encode()
    with registry.client(tmp_path / "cache") as client:
        with pytest.raises(ValueError, match="larger than"):
            client.pull(parse_reference("registry.test/t/n:1"))
    assert registry.blob_gets == 0
