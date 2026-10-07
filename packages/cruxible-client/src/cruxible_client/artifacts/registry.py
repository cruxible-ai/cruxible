"""Pull and push Cruxible artifacts over the OCI distribution (registry v2) API.

Only what artifacts need: bearer-token auth (anonymous, or basic credentials
exchanged at the registry's token realm), manifests by tag or digest, and blobs
by digest. Every blob is checked against its descriptor, a manifest pulled by
digest is checked against that digest, and blobs are cached by digest and
re-hashed on every cache hit.
"""

from __future__ import annotations

import base64
import json
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
    manifest_descriptors,
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
    """Basic credentials for exactly the registry named by CRUXIBLE_REGISTRY.

    CRUXIBLE_REGISTRY_USERNAME and CRUXIBLE_REGISTRY_PASSWORD apply only to the
    host in CRUXIBLE_REGISTRY (for example ``ghcr.io``); every other registry is
    contacted anonymously, so fetching a kit from elsewhere never offers them.
    """

    bound = os.environ.get("CRUXIBLE_REGISTRY")
    username = os.environ.get("CRUXIBLE_REGISTRY_USERNAME")
    password = os.environ.get("CRUXIBLE_REGISTRY_PASSWORD")
    if not bound or bound != registry or not username or not password:
        return None
    return username, password


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
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
# One artifact's blobs together, checked from the manifest before any is fetched.
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_LAYERS = 64
MAX_TOKEN_BYTES = 64 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_ACK_BYTES = 64 * 1024
MAX_REDIRECTS = 5


def _read(response: httpx.Response, limit: int, *, truncate: bool) -> bytes:
    chunks = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            if truncate:
                chunks.append(chunk[: len(chunk) - (total - limit)])
                break
            raise ValueError(f"{response.request.url} returned more than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, (parts.hostname or ""), parts.port


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
        # Redirects (blob stores redirect to CDNs) are followed hop by hop in
        # ``_send``, where each destination is checked before anything is sent.
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)
        self._credentials = credentials
        self._cache = cache if cache is not None else BlobCache(default_cache_root())
        self._scheme = scheme
        # Tokens are held per exact origin and scope, and only ever sent there.
        self._tokens: dict[tuple[tuple[str, str, int | None], str], str] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _url(self, ref: Reference, suffix: str) -> str:
        return f"{self._scheme}://{ref.registry}/v2/{ref.repository}/{suffix}"

    def _token(self, host: str, challenge: str, scope: str) -> str | None:
        """A bearer token for ``host``; credentials only to that host's own realm."""

        if not challenge.lower().startswith("bearer "):
            return None
        params = dict(_CHALLENGE_RE.findall(challenge))
        realm = params.get("realm")
        if realm is None:
            return None
        realm_url = urllib.parse.urlsplit(realm)
        bare = host.partition(":")[0]
        trusted = realm_url.hostname == bare or (realm_url.hostname or "").endswith("." + bare)
        credentials = self._credentials(host) if trusted else None
        headers = {}
        if credentials is not None:
            pair = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
            headers["Authorization"] = f"Basic {pair}"
        query = {"service": params.get("service", host), "scope": scope}
        response, body = self._send(
            "GET", realm + "?" + urllib.parse.urlencode(query), headers, None, MAX_TOKEN_BYTES
        )
        response.raise_for_status()
        payload = json.loads(body)
        token = payload.get("token") or payload.get("access_token")
        return token if isinstance(token, str) else None

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes | None,
        limit: int,
    ) -> tuple[httpx.Response, bytes]:
        """One request, following redirects by hand, with every body read under a bound.

        Each hop is checked before it is sent: it must keep the client's scheme,
        and a hop to another origin drops the Authorization header. A successful
        body is refused past ``limit``; an error body is read no further than
        ``MAX_ERROR_BYTES``; a redirect body is never read.
        """

        sent = dict(headers)
        origin = _origin(url)
        for _hop in range(MAX_REDIRECTS + 1):
            if _origin(url)[0] != self._scheme:
                raise ValueError(f"{url} does not use {self._scheme}")
            request = self._http.build_request(method, url, headers=sent, content=content)
            response = self._http.send(request, stream=True, follow_redirects=False)
            try:
                if response.is_redirect:
                    target = urllib.parse.urljoin(url, response.headers["location"])
                    if _origin(target) != origin:
                        sent.pop("Authorization", None)
                        origin = _origin(target)
                    if response.status_code in {301, 302, 303} and method not in {"GET", "HEAD"}:
                        method, content = "GET", None
                    url = target
                    continue
                if response.status_code >= 300:
                    return response, _read(response, MAX_ERROR_BYTES, truncate=True)
                return response, _read(response, limit, truncate=False)
            finally:
                response.close()
        raise ValueError(f"{url} redirected more than {MAX_REDIRECTS} times")

    def _request(
        self,
        method: str,
        ref: Reference,
        url: str,
        *,
        push: bool = False,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        limit: int = MAX_ACK_BYTES,
    ) -> tuple[httpx.Response, bytes]:
        origin = _origin(url)
        if origin[0] != self._scheme:
            raise ValueError(f"{url} does not use {self._scheme}")
        scope = f"repository:{ref.repository}:{'pull,push' if push else 'pull'}"
        key = (origin, scope)
        sent = dict(headers or {})
        if key in self._tokens:
            sent["Authorization"] = f"Bearer {self._tokens[key]}"
        response, body = self._send(method, url, sent, content, limit)
        if response.status_code == 401:
            host = origin[1] if origin[2] is None else f"{origin[1]}:{origin[2]}"
            token = self._token(host, response.headers.get("www-authenticate", ""), scope)
            if token is not None:
                self._tokens[key] = token
                sent["Authorization"] = f"Bearer {token}"
                response, body = self._send(method, url, sent, content, limit)
        return response, body

    def pull(self, ref: Reference) -> ArtifactImage:
        """The artifact at ``ref``; a digest reference must match the manifest bytes."""

        response, manifest = self._request(
            "GET",
            ref,
            self._url(ref, f"manifests/{ref.reference}"),
            headers={"Accept": MANIFEST_MEDIA_TYPE},
            limit=MAX_MANIFEST_BYTES,
        )
        response.raise_for_status()
        if ref.digest is not None and sha256_digest(manifest) != ref.digest:
            raise ValueError(f"{ref} returned a manifest with a different digest")
        config, layers = manifest_descriptors(manifest)
        if len(layers) > MAX_LAYERS:
            raise ValueError(f"{ref} names more than {MAX_LAYERS} layers")
        if sum(int(item["size"]) for item in (config, *layers)) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"{ref} is larger than {MAX_ARTIFACT_BYTES} bytes in total")

        def fetch(descriptor: dict[str, object]) -> bytes:
            digest = str(descriptor["digest"])
            size = descriptor["size"]
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"{ref} names blob {digest} of unacceptable size")
            cached = self._cache.get(digest)
            if cached is not None:
                return cached
            blob, content = self._request("GET", ref, self._url(ref, f"blobs/{digest}"), limit=size)
            blob.raise_for_status()
            if sha256_digest(content) != digest:
                raise ValueError(f"{ref} served blob {digest} with different bytes")
            self._cache.put(content)
            return content

        return assemble_image(manifest, fetch)

    def list_tags(self, ref: Reference) -> tuple[str, ...]:
        """The tags the registry holds for ``ref``'s repository."""

        response, body = self._request(
            "GET", ref, self._url(ref, "tags/list?n=1000"), limit=MAX_MANIFEST_BYTES
        )
        response.raise_for_status()
        payload = json.loads(body)
        tags = payload.get("tags") if isinstance(payload, dict) else None
        if tags is None:
            return ()
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"{ref.registry} answered a malformed tag list")
        return tuple(tags)

    def push(self, image: ArtifactImage, ref: Reference) -> str:
        """Upload every blob the registry lacks, then the manifest; returns its digest."""

        for blob in image.blobs():
            exists, _ = self._request(
                "HEAD", ref, self._url(ref, f"blobs/{blob.digest}"), push=True
            )
            if exists.status_code == 200:
                continue
            started, _ = self._request("POST", ref, self._url(ref, "blobs/uploads/"), push=True)
            if started.status_code != 202 or "location" not in started.headers:
                started.raise_for_status()
                raise ValueError(f"{ref.registry} did not start a blob upload")
            # An upload location on another origin is contacted with that
            # origin's own credentials (none, unless it challenges), never with
            # the registry's token; a downgrade to another scheme is refused.
            location = urllib.parse.urljoin(str(started.url), started.headers["location"])
            separator = "&" if "?" in location else "?"
            finished, _ = self._request(
                "PUT",
                ref,
                f"{location}{separator}digest={urllib.parse.quote(blob.digest)}",
                push=True,
                content=blob.content,
                headers={"Content-Type": "application/octet-stream"},
            )
            finished.raise_for_status()
        response, _ = self._request(
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
