"""Pull and push Cruxible artifacts over the OCI distribution (registry v2) API.

Only what artifacts need: bearer-token auth (anonymous, or basic credentials
exchanged at the registry's token realm), manifests by tag or digest, and blobs
by digest. Every blob is checked against its descriptor, a manifest pulled by
digest is checked against that digest, and blobs are cached by digest and
re-hashed on every cache hit.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from cruxible_client.artifacts.oci import (
    MANIFEST_MEDIA_TYPE,
    ArtifactImage,
    assemble_image,
    sha256_digest,
)

DEFAULT_NAMESPACE = "ghcr.io/cruxible-ai/kits"
_NAME_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

Credentials = Callable[[str], tuple[str, str] | None]


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"

    def __str__(self) -> str:
        base = f"{self.registry}/{self.repository}"
        if self.digest is not None:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag or 'latest'}"

    def pinned(self, digest: str) -> Reference:
        return Reference(self.registry, self.repository, self.tag, digest)


def parse_reference(text: str, *, default_namespace: str = DEFAULT_NAMESPACE) -> Reference:
    """``name[:tag|@digest]`` under the default namespace, or a full registry reference."""

    digest = None
    if "@" in text:
        text, digest = text.split("@", 1)
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"{digest} is not a sha256 digest")
    tag = None
    head, _, last = text.rpartition("/")
    if ":" in last:
        last, tag = last.split(":", 1)
        if not _TAG_RE.fullmatch(tag):
            raise ValueError(f"{tag!r} is not a valid tag")
    text = f"{head}/{last}" if head else last
    first, _, rest = text.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, _, namespace = default_namespace.partition("/")
        repository = f"{namespace}/{text}" if namespace else text
    if not _NAME_RE.fullmatch(repository):
        raise ValueError(f"{repository!r} is not a valid repository name")
    return Reference(registry=registry, repository=repository, tag=tag, digest=digest)


def environment_credentials(registry: str) -> tuple[str, str] | None:
    """Basic credentials from CRUXIBLE_REGISTRY_USERNAME/PASSWORD, else anonymous."""

    username = os.environ.get("CRUXIBLE_REGISTRY_USERNAME")
    password = os.environ.get("CRUXIBLE_REGISTRY_PASSWORD")
    return (username, password) if username and password else None


def default_cache_root() -> Path:
    raw = os.environ.get("CRUXIBLE_ARTIFACT_CACHE")
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "cruxible" / "artifacts"


class BlobCache:
    """Blobs by sha256 digest; a cached blob is re-hashed before it is trusted."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"{digest} is not a sha256 digest")
        return self.root / "blobs" / "sha256" / digest.removeprefix("sha256:")

    def get(self, digest: str) -> bytes | None:
        path = self._path(digest)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        if sha256_digest(content) != digest:
            path.unlink(missing_ok=True)
            return None
        return content

    def put(self, content: bytes) -> str:
        digest = sha256_digest(content)
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, path)
        return digest


_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')


class RegistryClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        credentials: Credentials = environment_credentials,
        cache: BlobCache | None = None,
        timeout: float = 120.0,
        scheme: str = "https",
    ) -> None:
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)
        self._credentials = credentials
        self._cache = cache if cache is not None else BlobCache(default_cache_root())
        self._scheme = scheme
        self._tokens: dict[tuple[str, str], str] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _url(self, ref: Reference, suffix: str) -> str:
        return f"{self._scheme}://{ref.registry}/v2/{ref.repository}/{suffix}"

    def _token(self, ref: Reference, challenge: str, scope: str) -> str | None:
        if not challenge.lower().startswith("bearer "):
            return None
        params = dict(_CHALLENGE_RE.findall(challenge))
        realm = params.get("realm")
        if realm is None:
            return None
        query = {"service": params.get("service", ref.registry), "scope": scope}
        # Credentials go only to a token realm the registry itself serves: a
        # challenge naming any other host gets an anonymous token request.
        realm_url = urllib.parse.urlsplit(realm)
        registry_host = ref.registry.partition(":")[0]
        trusted = realm_url.scheme == self._scheme and (
            realm_url.hostname == registry_host
            or (realm_url.hostname or "").endswith("." + registry_host)
        )
        credentials = self._credentials(ref.registry) if trusted else None
        response = self._http.get(
            realm + "?" + urllib.parse.urlencode(query),
            auth=credentials,
        )
        response.raise_for_status()
        body = response.json()
        token = body.get("token") or body.get("access_token")
        return token if isinstance(token, str) else None

    def _request(
        self,
        method: str,
        ref: Reference,
        url: str,
        *,
        push: bool = False,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        scope = f"repository:{ref.repository}:{'pull,push' if push else 'pull'}"
        key = (ref.registry, scope)
        sent = dict(headers or {})
        if key in self._tokens:
            sent["Authorization"] = f"Bearer {self._tokens[key]}"
        response = self._http.request(method, url, headers=sent, content=content)
        if response.status_code == 401:
            token = self._token(ref, response.headers.get("www-authenticate", ""), scope)
            if token is not None:
                self._tokens[key] = token
                sent["Authorization"] = f"Bearer {token}"
                response = self._http.request(method, url, headers=sent, content=content)
        return response

    def pull(self, ref: Reference) -> ArtifactImage:
        """The artifact at ``ref``; a digest reference must match the manifest bytes."""

        response = self._request(
            "GET",
            ref,
            self._url(ref, f"manifests/{ref.reference}"),
            headers={"Accept": MANIFEST_MEDIA_TYPE},
        )
        response.raise_for_status()
        manifest = response.content
        if ref.digest is not None and sha256_digest(manifest) != ref.digest:
            raise ValueError(f"{ref} returned a manifest with a different digest")

        def fetch(descriptor: dict[str, object]) -> bytes:
            digest = str(descriptor["digest"])
            cached = self._cache.get(digest)
            if cached is not None:
                return cached
            blob = self._request("GET", ref, self._url(ref, f"blobs/{digest}"))
            blob.raise_for_status()
            if sha256_digest(blob.content) != digest:
                raise ValueError(f"{ref} served blob {digest} with different bytes")
            self._cache.put(blob.content)
            return blob.content

        return assemble_image(manifest, fetch)

    def push(self, image: ArtifactImage, ref: Reference) -> str:
        """Upload every blob the registry lacks, then the manifest; returns its digest."""

        for blob in image.blobs():
            exists = self._request("HEAD", ref, self._url(ref, f"blobs/{blob.digest}"), push=True)
            if exists.status_code == 200:
                continue
            started = self._request("POST", ref, self._url(ref, "blobs/uploads/"), push=True)
            if started.status_code != 202 or "location" not in started.headers:
                started.raise_for_status()
                raise ValueError(f"{ref.registry} did not start a blob upload")
            location = urllib.parse.urljoin(str(started.url), started.headers["location"])
            separator = "&" if "?" in location else "?"
            finished = self._request(
                "PUT",
                ref,
                f"{location}{separator}digest={urllib.parse.quote(blob.digest)}",
                push=True,
                content=blob.content,
                headers={"Content-Type": "application/octet-stream"},
            )
            finished.raise_for_status()
        response = self._request(
            "PUT",
            ref,
            self._url(ref, f"manifests/{ref.tag or image.digest}"),
            push=True,
            content=image.manifest,
            headers={"Content-Type": MANIFEST_MEDIA_TYPE},
        )
        response.raise_for_status()
        returned = response.headers.get("docker-content-digest")
        if returned is not None and returned != image.digest:
            raise ValueError(f"{ref.registry} stored the manifest under a different digest")
        return image.digest
