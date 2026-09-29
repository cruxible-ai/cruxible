"""Find and fetch one provider release from a package index, by name.

An install by name asks the operator's indexes, in order, for the package's
PEP 503 page. The first index that lists the package is the only one consulted,
so a later index can never substitute a same-named package. Only files the page
hashes are candidates, and the fetched bytes must match that hash. The lock that
pins the provider's environment comes from inside the wheel, never from the
index, so the materialization is exactly the one the package was released with.
"""

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit
from zipfile import ZipFile

from packaging.tags import sys_tags
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from cruxible_core.errors import ConfigError
from cruxible_core.providers.package_materialization import ArtifactTransport, toolchain

# PyPI's simple index and the origin it serves files from. Used when the
# operator configured no indexes and asked for a package by name.
DEFAULT_PROVIDER_INDEX_URLS = ("https://pypi.org/simple/", "https://files.pythonhosted.org/")

EMBEDDED_LOCK = "extra_metadata/uv.lock"


@dataclass(frozen=True)
class IndexRelease:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    index_url: str


@dataclass(frozen=True)
class _File:
    filename: str
    url: str
    sha256: str | None
    yanked: bool


class _SimplePage(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.files: list[_File] = []
        self._anchor: dict[str, str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._anchor, self._text = dict(attrs), []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._anchor is None:
            return
        href = self._anchor.get("href")
        if href:
            url, fragment = urldefrag(urljoin(self.base_url, href))
            algorithm, _, digest = fragment.partition("=")
            self.files.append(
                _File(
                    filename="".join(self._text).strip(),
                    url=url,
                    sha256=digest.lower() if algorithm == "sha256" and digest else None,
                    yanked="data-yanked" in self._anchor,
                )
            )
        self._anchor = None


def _page_url(index_url: str, name: str) -> str:
    page = index_url.rstrip("/") + f"/{canonicalize_name(name)}/"
    return page + "index.html" if urlsplit(index_url).scheme == "file" else page


def _listing(
    index_url: str, name: str, transport: ArtifactTransport
) -> tuple[str, list[_File]] | None:
    url = _page_url(index_url, name)
    try:
        response = transport.get(url)
    except FileNotFoundError:
        return None
    if response.status == 404:
        return None
    if response.status != 200 or response.final_url != url:
        raise ConfigError(f"provider index {index_url} did not answer for {name!r}")
    page = _SimplePage(url)
    page.feed(response.body.decode("utf-8", errors="replace"))
    return url, page.files


def find_release(
    index_urls: tuple[str, ...],
    name: str,
    version: str | None,
    transport: ArtifactTransport,
) -> IndexRelease:
    """The newest final release (or exactly ``version``) installable here."""

    try:
        wanted = None if version is None else Version(version)
    except InvalidVersion as exc:
        raise ConfigError(f"{version!r} is not a release version") from exc
    label = name if version is None else f"{name}=={version}"
    supported = [str(tag) for tag in sys_tags()]
    for index_url in index_urls:
        listing = _listing(index_url, name, transport)
        if listing is None:
            continue
        candidates = []
        for item in listing[1]:
            try:
                found, found_version, _build, tags = parse_wheel_filename(item.filename)
            except InvalidWheelFilename:
                continue
            if found != canonicalize_name(name) or item.sha256 is None:
                continue
            if wanted is not None and found_version != wanted:
                continue
            if wanted is None and (found_version.is_prerelease or item.yanked):
                continue
            ranks = [supported.index(str(tag)) for tag in tags if str(tag) in supported]
            if ranks:
                candidates.append((found_version, -min(ranks), item))
        if not candidates:
            raise ConfigError(f"{index_url} lists no installable wheel for {label}")
        found_version, _rank, item = max(candidates, key=lambda row: (row[0], row[1]))
        return IndexRelease(
            name=str(canonicalize_name(name)),
            version=str(found_version),
            filename=item.filename,
            url=item.url,
            sha256=item.sha256 or "",
            index_url=index_url,
        )
    raise ConfigError(f"no configured provider index lists {label}")


def fetch_release(
    release: IndexRelease,
    index_urls: tuple[str, ...],
    transport: ArtifactTransport,
) -> bytes:
    """The release's bytes, from a covered origin, matching the index's hash."""

    index = toolchain("index")
    fetcher = index.ArtifactFetcher(index.IndexConfig(index_urls=index_urls), transport)
    body: bytes = fetcher.fetch_url(release.url, "sha256:" + release.sha256, release.name)
    return body


def embedded_lock(wheel: Path) -> bytes:
    """The lock a provider wheel was released with."""

    name, version, _build, _tags = parse_wheel_filename(wheel.name)
    member = f"{str(name).replace('-', '_')}-{version}.dist-info/{EMBEDDED_LOCK}"
    with ZipFile(wheel) as archive:
        if member not in archive.namelist():
            raise ConfigError(
                f"{wheel.name} does not embed its lock, so it cannot be installed by name; "
                "transfer the wheel with its lock instead"
            )
        return archive.read(member)
