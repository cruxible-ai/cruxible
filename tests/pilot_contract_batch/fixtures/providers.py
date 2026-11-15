"""Small test providers that fit, or do not fit, a slot: plain code plus a schema-3 registration.

Each :class:`PilotPackage` is what a provider wheel would carry after the batch
(note §2.1): a runtime manifest, a registration document at schema 3 whose
operations declare ``description``, ``source``, ``serves`` and
``may_truncate``, a ``declares`` block holding full copies of every Contract
and CaptureKind layout the operations reach, and ``built_against`` naming what
was copied. The entrypoints are plain functions from a call's input to its
output, the way ``tests/support/operation_world.Invoker`` answers; scenario
code answers provider calls with :meth:`PilotPackage.answer`.

Nothing here registers anything: the owner of ``surfaces.install_provider_package``
(slice 3) lowers a package into an accepted build.

Copies of base-kit shapes are read from the base kit directory core will ship
(``src/cruxible_core/kits/base``, note §7.4.2) byte for byte, as a provider
build script copies them (§7.4.5). Until that directory exists the base-kit
packages cannot be built from it; the scenarios that use them stop earlier, at
the base kit's precondition. ``test_fixtures.py`` builds every package from the
wave 0 example artifacts instead (:meth:`BaseKit.examples`) and checks each
registration against the wave 0 models (``provider_registration.py``): the
models are the authority on the registration's shape.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_KIT_DIR = REPO_ROOT / "src/cruxible_core/kits/base"
#: The wave 0 example artifacts of the base kit: ``capabilities/``, ``contracts/`` and
#: ``capture-kinds/`` (one ``{name, decoding, layout}`` copy per kind).
EXAMPLE_BASE_KIT = REPO_ROOT / "tests/pilot_contract_batch/examples/base"
#: Where a package keeps its runtime manifest; the registration names it with its digest.
MANIFEST_PATH = "manifest.json"
SOURCE_PATH = "tests/pilot_contract_batch/fixtures/providers.py"
REPOSITORY = "https://github.com/cruxible-ai/cruxible"

SEARCH = "web.search"
RESULTS = "web.search.results"
FETCH = "web.fetch"
RESULTS_KIND = "web.search-results"
#: The raw kind a Procedure names for the search response (a capture<*> field).
SEARCH_RESPONSE_KIND = "pilot.search-response"
SEARCH_ORIGIN = "https://search.pilot.test"

Answer = Callable[[Mapping[str, Any]], dict[str, Any]]


# -- the base kit's shapes, as a provider build copies them -------------------------------


class BaseKit:
    """Reads the base kit's artifact files (capabilities, Contracts, CaptureKinds).

    ``artifacts`` is the kit's artifact directory; ``kinds`` the subdirectory
    holding its CaptureKinds.
    """

    def __init__(
        self, artifacts: Path = BASE_KIT_DIR / "artifacts", *, kinds: str = "capture-contracts"
    ) -> None:
        self.root = artifacts
        self.kinds = kinds

    @classmethod
    def examples(cls) -> BaseKit:
        """The wave 0 example artifacts, standing in for the base kit until it ships."""

        return cls(EXAMPLE_BASE_KIT, kinds="capture-kinds")

    def _read(self, relative: str) -> dict[str, Any]:
        path = self.root / relative
        if not path.is_file():
            raise FileNotFoundError(f"the base kit has no {relative} (looked in {self.root})")
        return json.loads(path.read_bytes())

    def capability(self, name: str, version: int = 1) -> dict[str, Any]:
        return self._read(f"capabilities/{name}/v{version}.json")

    def contract(self, name: str, version: int) -> dict[str, Any]:
        return self._read(f"contracts/{name}/v{version}.json")

    def capture_kind(self, name: str) -> dict[str, Any]:
        return self._read(f"{self.kinds}/{name}.json")

    def capability_bytes(self, name: str, version: int = 1) -> bytes:
        return (self.root / f"capabilities/{name}/v{version}.json").read_bytes()

    def closure(self, name: str, version: int) -> list[dict[str, Any]]:
        """The Contract and every Contract it nests, each once, nested ones first."""

        seen: dict[tuple[str, int], dict[str, Any]] = {}

        def visit(ref_name: str, ref_version: int) -> None:
            if (ref_name, ref_version) in seen:
                return
            document = self.contract(ref_name, ref_version)
            for nested in _nested_refs(document):
                visit(*nested)
            seen[(ref_name, ref_version)] = document

        visit(name, version)
        return list(seen.values())

    def capability_shapes(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """``(in, out)`` pins of a capability, as ``{name, version, digest}``."""

        document = self.capability(name)
        return document["in"], document["out"]


def _nested_refs(document: Mapping[str, Any]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("kind") == "contract" and isinstance(value.get("contract"), Mapping):
                found.append((value["contract"]["name"], int(value["contract"]["version"])))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document.get("fields", {}))
    return found


# -- packages -------------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotPackage:
    """One provider as a wheel would carry it after the batch (registration schema 3)."""

    provider_id: str
    version: str
    registration: dict[str, Any]
    entrypoints: Mapping[str, Answer] = field(default_factory=dict)

    @property
    def manifest(self) -> dict[str, Any]:
        """The runtime manifest (``ProviderRuntimeManifest`` fields)."""

        return _manifest(self.provider_id, self.version, self.registration["operations"])

    @property
    def manifest_bytes(self) -> bytes:
        """The manifest file's bytes: what ``registration["manifest"]`` names by digest."""

        return _manifest_bytes(self.manifest)

    def operation(self, name: str) -> dict[str, Any]:
        (found,) = [item for item in self.registration["operations"] if item["name"] == name]
        return found

    def answer(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.entrypoints[self.operation(operation)["entrypoint"]](payload)

    @property
    def spelling(self) -> str:
        """``<provider>#<operation>`` for a one-operation package."""

        (only,) = self.registration["operations"]
        return f"{self.provider_id}#{only['name']}"


def _operation(
    name: str,
    *,
    serves: list[str],
    request: str,
    result: str,
    effect: str,
    description: str,
    entrypoint: str,
    may_truncate: bool = False,
    refusals: Mapping[str, Any] | None = None,
    repository: bool = True,
) -> dict[str, Any]:
    source = {"path": SOURCE_PATH, **({"repository": REPOSITORY} if repository else {})}
    return {
        "name": name,
        "description": description,
        "source": source,
        "serves": sorted(serves),
        "in": request,
        "out": result,
        "effect": effect,
        "may_truncate": may_truncate,
        "refusals": dict(refusals or {}),
        "entrypoint": entrypoint,
    }


def _manifest(provider_id: str, version: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    entrypoints = sorted({item["entrypoint"] for item in operations})
    return {
        "provider_id": provider_id,
        "distribution": {"name": provider_id, "version": version},
        "supported_protocol_majors": [2],
        "implementations": [
            {"entrypoint": entry, "backends": ["local_env"], "deterministic": True}
            for entry in entrypoints
        ],
    }


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def _utf8_key(*parts: object) -> tuple[object, ...]:
    return tuple(part.encode("utf-8") if isinstance(part, str) else part for part in parts)


def _registration(
    provider_id: str,
    version: str,
    operations: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    *,
    capture_kinds: list[dict[str, Any]] | None = None,
    built_against: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A schema-3 registration in the order the model requires.

    Operations sorted by name; ``declares.contracts`` by name then version, once
    each; ``capture_kinds`` by name; ``built_against`` by kind, name, version.
    Empty ``capture_kinds`` and ``built_against`` are left out, as the model
    renders them.
    """

    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for document in contracts:
        unique.setdefault((document["name"], int(document["version"])), document)
    declares: dict[str, Any] = {
        "contracts": [unique[key] for key in sorted(unique, key=lambda key: _utf8_key(*key))]
    }
    if capture_kinds:
        declares["capture_kinds"] = sorted(capture_kinds, key=lambda item: _utf8_key(item["name"]))
    if built_against:
        declares["built_against"] = sorted(
            built_against,
            key=lambda item: _utf8_key(item["kind"], item["name"], item.get("version", 0)),
        )
    digest = hashlib.sha256(_manifest_bytes(_manifest(provider_id, version, operations)))
    return {
        "schema_version": 3,
        "manifest": {"path": MANIFEST_PATH, "digest": "sha256:" + digest.hexdigest()},
        "operations": sorted(operations, key=lambda item: _utf8_key(item["name"])),
        "declares": declares,
    }


def _contract(name: str, fields: Mapping[str, Any], description: str) -> dict[str, Any]:
    return {
        "artifact_format": "cruxible-contract-v1",
        "name": name,
        "version": 1,
        "description": description,
        "fields": dict(fields),
    }


def _string(max_len: int = 256) -> dict[str, Any]:
    return {"type": {"kind": "string", "max_len": max_len}}


def _capture(kind: str = "*") -> dict[str, Any]:
    return {"type": {"kind": "capture", "capture_kind": kind}}


def _side(base: BaseKit, capability: str, side: str) -> tuple[str, list[dict[str, Any]]]:
    """The capability's ``in`` or ``out`` as ``name@N``, and its closure copied."""

    pin = base.capability(capability)[side]
    return f"{pin['name']}@{pin['version']}", base.closure(pin["name"], int(pin["version"]))


def _copied(base: BaseKit, capability: str) -> tuple[str, str, list[dict[str, Any]]]:
    """The capability's ``in`` and ``out`` as ``name@N``, and their closures copied."""

    request, request_contracts = _side(base, capability, "in")
    result, result_contracts = _side(base, capability, "out")
    return request, result, [*request_contracts, *result_contracts]


def _layout_contracts(base: BaseKit, kind: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every Contract a carried layout reaches (row Contracts and field-rule types),
    with their closures: the registration law requires them in ``declares``."""

    from cruxible_client.contracts.capture_layouts import DeclaredCaptureKind, layout_rule_types
    from cruxible_client.contracts.shapes import type_refs

    layout = DeclaredCaptureKind.model_validate(kind).layout
    if layout is None:
        return []
    refs = [*layout.row_refs()]
    refs.extend(ref for _entry, rule in layout_rule_types(layout) for ref in type_refs(rule))
    return [document for ref in refs for document in base.closure(ref.name, ref.version)]


def _built_against(base: BaseKit, capability: str, *kinds: str) -> list[dict[str, Any]]:
    """The record of what was copied (note §2.1); nothing gates on it.

    Digests are the wave 0 ones: ``capability_digest`` of the capability and
    ``declared_capture_kind_digest`` of each carried kind copy.
    """

    from cruxible_client.contracts.capabilities import capability_digest, parse_capability
    from cruxible_client.contracts.capture_layouts import (
        DeclaredCaptureKind,
        declared_capture_kind_digest,
    )

    document = base.capability(capability)
    parsed = parse_capability(
        base.capability_bytes(capability), path=f"capabilities/{capability}/v1.json"
    )
    out = [
        {
            "kind": "Capability",
            "name": capability,
            "version": document["version"],
            "digest": capability_digest(parsed).tagged,
        }
    ]
    for kind in kinds:
        copied = DeclaredCaptureKind.model_validate(_kind_copy(base, kind))
        out.append(
            {
                "kind": "CaptureKind",
                "name": kind,
                "digest": declared_capture_kind_digest(copied).tagged,
            }
        )
    return out


def _kind_copy(base: BaseKit, kind: str) -> dict[str, Any]:
    document = base.capture_kind(kind)
    return {
        "name": kind,
        "decoding": copy.deepcopy(document["decoding"]),
        "layout": copy.deepcopy(document["layout"]),
    }


# -- behaviour: a recorded search answer and its normalizing derive -----------------------

#: A search vendor's answer, verbatim (its own field names; D-R37: a fetch never reshapes).
RECORDED_HITS: dict[str, list[dict[str, Any]]] = {
    "cruxible": [
        {"link": "https://cruxible.test/", "name": "Cruxible", "blurb": "Hard state."},
        {"link": "https://cruxible.test/docs", "name": "Docs", "blurb": "How to start."},
        {"link": "https://cruxible.test/kits", "name": "Kits", "blurb": None},
    ],
}


def wire(content: bytes, url: str, *, source: str) -> dict[str, Any]:
    """One Capture as a provider returns it (the skeleton's wire form)."""

    from tests.test_e2e.test_skeleton_capture_lists import _wire

    return _wire(content, url, source=source)


def search_answer(payload: Mapping[str, Any]) -> dict[str, Any]:
    query = str(payload["query"])
    body = json.dumps({"q": query, "hits": RECORDED_HITS.get(query, [])}, sort_keys=True).encode()
    return {"response": wire(body, f"{SEARCH_ORIGIN}/?q={query}", source="pilot.search")}


def normalized(response: Mapping[str, Any]) -> bytes:
    """The derive: the vendor's hits as ``web.search-results`` rows (url, title, snippet?, rank)."""

    vendor = json.loads(base64.b64decode(response["content_base64"]))
    rows = []
    for rank, hit in enumerate(vendor["hits"], start=1):
        row: dict[str, Any] = {"url": hit["link"], "title": hit["name"], "rank": rank}
        if hit.get("blurb"):
            row["snippet"] = hit["blurb"]
        rows.append(row)
    document = {"query": vendor["q"], "results": rows}
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def results_answer(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = normalized(payload["response"])
    return {"results": wire(body, "https://derive.pilot.test/results", source="pilot.results")}


def empty_results_answer(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A derive document with the ``results`` record set present and empty: the query only.

    Checked on today's engine with today's full extract: a document with no
    ``results`` key at all is read as incomplete and retires nothing; one whose
    ``results`` is present and empty is a complete read of zero records, and
    ``mirror`` retires every rank. This answer takes the second form. Scenario
    7 states when that read is honoured (the producer's layout declares
    ``results``) and when binding refuses the producer first (it does not).
    """

    vendor = json.loads(base64.b64decode(payload["response"]["content_base64"]))
    body = json.dumps({"query": vendor["q"], "results": []}, sort_keys=True).encode()
    return {"results": wire(body, "https://derive.pilot.test/empty", source="pilot.results")}


# -- scenario 3: a provider no kit knows ------------------------------------------------------


def echo_package() -> PilotPackage:
    """``pilot-echo``: two pure operations serving capabilities no installed kit declares.

    Its Contracts exist only inside its own ``declares``; installing it needs no
    kit and accepts no Contract on the instance.
    """

    request = _contract("pilot.echo.request", {"text": _string()}, "One line of text to give back.")
    result = _contract(
        "pilot.echo.result", {"text": _string()}, "The same text, or the text in capitals."
    )
    declined = _contract(
        "pilot.echo.refusal.provider_declined",
        {"reason": _string()},
        "Why the provider declined the call.",
    )
    operations = [
        _operation(
            "echo",
            serves=["pilot.echo"],
            request="pilot.echo.request@1",
            result="pilot.echo.result@1",
            effect="pure",
            description="Gives back the text it is sent.",
            entrypoint="pilot_echo:Echo",
            refusals={
                "provider_declined": {
                    "contract": "pilot.echo.refusal.provider_declined@1",
                    "absorbable": False,
                }
            },
        ),
        _operation(
            "upper",
            serves=["pilot.upper"],
            request="pilot.echo.request@1",
            result="pilot.echo.result@1",
            effect="pure",
            description="Gives back the text it is sent, in capitals.",
            entrypoint="pilot_echo:Upper",
            repository=False,
        ),
    ]
    return PilotPackage(
        provider_id="pilot-echo",
        version="1.0.0",
        registration=_registration("pilot-echo", "1.0.0", operations, [request, result, declined]),
        entrypoints={
            "pilot_echo:Echo": lambda payload: {"text": payload["text"]},
            "pilot_echo:Upper": lambda payload: {"text": str(payload["text"]).upper()},
        },
    )


# -- scenarios 4, 5 and 7: the base kit's search pair -------------------------------------


def search_package(base: BaseKit) -> PilotPackage:
    """``pilot-search``: serves ``web.search`` with byte copies of its Contracts."""

    request, result, contracts = _copied(base, SEARCH)
    operation = _operation(
        "search",
        serves=[SEARCH],
        request=request,
        result=result,
        effect="external_read",
        description="Asks the recorded search origin; returns its answer verbatim.",
        entrypoint="pilot_search:Search",
    )
    return PilotPackage(
        provider_id="pilot-search",
        version="1.0.0",
        registration=_registration(
            "pilot-search",
            "1.0.0",
            [operation],
            contracts,
            built_against=_built_against(base, SEARCH),
        ),
        entrypoints={"pilot_search:Search": search_answer},
    )


def results_package(
    base: BaseKit,
    provider_id: str = "pilot-results",
    *,
    description: str = "Reads a search answer and writes its hits as web.search-results.",
    entrypoint: str = "pilot_results:Results",
    answer: Answer = results_answer,
) -> PilotPackage:
    """Fits ``web.search.results`` exactly: byte copies of its Contracts and the kind's layout."""

    request, result, contracts = _copied(base, RESULTS)
    kind = _kind_copy(base, RESULTS_KIND)
    operation = _operation(
        RESULTS,
        serves=[RESULTS],
        request=request,
        result=result,
        effect="pure",
        description=description,
        entrypoint=entrypoint,
    )
    return PilotPackage(
        provider_id=provider_id,
        version="1.0.0",
        registration=_registration(
            provider_id,
            "1.0.0",
            [operation],
            [*contracts, *_layout_contracts(base, kind)],
            capture_kinds=[kind],
            built_against=_built_against(base, RESULTS, RESULTS_KIND),
        ),
        entrypoints={entrypoint: answer},
    )


def empty_results_package(base: BaseKit) -> PilotPackage:
    """Fits ``web.search.results`` exactly, layout included, and answers with ``results``
    present and empty (:func:`empty_results_answer`).

    Its layout declares the ``results`` record set, so binding accepts it and
    its empty document is a complete read of zero records (scenario 7).
    """

    return results_package(
        base,
        "pilot-results-empty",
        description="Reads a search answer and writes web.search-results with no hits.",
        entrypoint="pilot_results_empty:Results",
        answer=empty_results_answer,
    )


def wide_results_package(base: BaseKit) -> PilotPackage:
    """Fits ``web.search.results`` structurally: its own request accepts one more optional field."""

    result, contracts = _side(base, RESULTS, "out")
    kind = _kind_copy(base, RESULTS_KIND)
    wide = _contract(
        "pilot.results-wide.request",
        {
            "response": _capture(),
            "max_results": {
                "optional": True,
                "type": {"kind": "int", "min": 1, "max": 100},
            },
        },
        "A search answer to normalize; optionally how many hits to keep.",
    )
    operation = _operation(
        RESULTS,
        serves=[RESULTS],
        request="pilot.results-wide.request@1",
        result=result,
        effect="pure",
        description="Normalizes a search answer; accepts an optional hit limit.",
        entrypoint="pilot_results_wide:Results",
    )
    return PilotPackage(
        provider_id="pilot-results-wide",
        version="1.0.0",
        registration=_registration(
            "pilot-results-wide",
            "1.0.0",
            [operation],
            [wide, *contracts, *_layout_contracts(base, kind)],
            capture_kinds=[kind],
            built_against=_built_against(base, RESULTS, RESULTS_KIND),
        ),
        entrypoints={"pilot_results_wide:Results": results_answer},
    )


def fetch_only_package(base: BaseKit) -> PilotPackage:
    """Does not fit a ``web.search.results`` slot: it serves ``web.fetch`` only."""

    request, result, contracts = _copied(base, FETCH)
    operation = _operation(
        FETCH,
        serves=[FETCH],
        request=request,
        result=result,
        effect="external_read",
        description="Fetches one URL.",
        entrypoint="pilot_fetch:Fetch",
    )
    return PilotPackage(
        provider_id="pilot-fetch-only",
        version="1.0.0",
        registration=_registration(
            "pilot-fetch-only",
            "1.0.0",
            [operation],
            contracts,
            built_against=_built_against(base, FETCH),
        ),
        entrypoints={},
    )


def narrow_request_package(base: BaseKit) -> PilotPackage:
    """Does not fit: it declares ``web.search.results`` but has no ``response`` request field."""

    result, contracts = _side(base, RESULTS, "out")
    kind = _kind_copy(base, RESULTS_KIND)
    narrow = _contract(
        "pilot.results-narrow.request",
        {"responses": {"type": {"kind": "list", "item": _capture()["type"], "max_items": 8}}},
        "Up to eight search answers to normalize together.",
    )
    operation = _operation(
        RESULTS,
        serves=[RESULTS],
        request="pilot.results-narrow.request@1",
        result=result,
        effect="pure",
        description="Normalizes a list of search answers.",
        entrypoint="pilot_results_narrow:Results",
    )
    return PilotPackage(
        provider_id="pilot-results-narrow",
        version="1.0.0",
        registration=_registration(
            "pilot-results-narrow",
            "1.0.0",
            [operation],
            [narrow, *contracts, *_layout_contracts(base, kind)],
            capture_kinds=[kind],
        ),
        entrypoints={},
    )


def untyped_result_package(base: BaseKit) -> PilotPackage:
    """Does not fit: it returns ``capture<*>`` where the capability expects web.search-results."""

    request, contracts = _side(base, RESULTS, "in")
    untyped = _contract(
        "pilot.results-untyped.result",
        {"results": _capture()},
        "One derived document, of no declared kind.",
    )
    operation = _operation(
        RESULTS,
        serves=[RESULTS],
        request=request,
        result="pilot.results-untyped.result@1",
        effect="pure",
        description="Normalizes a search answer into a document it does not type.",
        entrypoint="pilot_results_untyped:Results",
    )
    return PilotPackage(
        provider_id="pilot-results-untyped",
        version="1.0.0",
        registration=_registration(
            "pilot-results-untyped", "1.0.0", [operation], [untyped, *contracts]
        ),
        entrypoints={"pilot_results_untyped:Results": results_answer},
    )


def thin_layout_package(base: BaseKit) -> PilotPackage:
    """Names ``capture<web.search-results>`` but its copy of the kind declares no ``results``
    record set.

    A layout declares at least one record set (``CaptureLayout.records`` has
    ``min_length=1``), and the base kind has only ``results``, so the copy
    carries the kind's decoding and no layout: it promises no record set at all.
    The shapes fit by name; only layout coverage (note §2.3) can refuse it. Run,
    it writes an empty ``results`` (see :func:`empty_results_answer`): a complete
    read with no records, on which ``when_absent: mirror`` retires every rank.
    """

    request, result, contracts = _copied(base, RESULTS)
    kind = {key: value for key, value in _kind_copy(base, RESULTS_KIND).items() if key != "layout"}
    operation = _operation(
        RESULTS,
        serves=[RESULTS],
        request=request,
        result=result,
        effect="pure",
        description="Normalizes a search answer to its query only.",
        entrypoint="pilot_results_thin:Results",
    )
    return PilotPackage(
        provider_id="pilot-results-thin",
        version="1.0.0",
        registration=_registration(
            "pilot-results-thin", "1.0.0", [operation], contracts, capture_kinds=[kind]
        ),
        entrypoints={"pilot_results_thin:Results": empty_results_answer},
    )


def other_capability_packages(count: int) -> list[PilotPackage]:
    """``count`` providers, each serving a capability of its own (``pilot.other.<n>``).

    For the binding cost measure: the check for one slot must not grow with them.
    """

    packages = []
    for index in range(count):
        name = f"pilot.other.{index:03d}"
        request = _contract(f"{name}.request", {"text": _string()}, "Text in.")
        result = _contract(f"{name}.result", {"text": _string()}, "Text out.")
        operation = _operation(
            "other",
            serves=[name],
            request=f"{name}.request@1",
            result=f"{name}.result@1",
            effect="pure",
            description=f"Serves {name} only.",
            entrypoint=f"pilot_other_{index:03d}:Other",
        )
        packages.append(
            PilotPackage(
                provider_id=f"pilot-other-{index:03d}",
                version="1.0.0",
                registration=_registration(
                    f"pilot-other-{index:03d}", "1.0.0", [operation], [request, result]
                ),
                entrypoints={},
            )
        )
    return packages


__all__ = [
    "BASE_KIT_DIR",
    "EXAMPLE_BASE_KIT",
    "BaseKit",
    "PilotPackage",
    "echo_package",
    "empty_results_answer",
    "empty_results_package",
    "fetch_only_package",
    "narrow_request_package",
    "other_capability_packages",
    "results_package",
    "search_package",
    "thin_layout_package",
    "untyped_result_package",
    "wide_results_package",
]
