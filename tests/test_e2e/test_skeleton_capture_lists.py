"""The template over a list of Captures (D-R37 a): fetch-list -> (derive) -> extract -> claims.

One fetch returns the KEV catalog as three pages, a ``list<capture<*>>`` whose
members are each a Capture under the KEV kind. Extract over the list merges
the pages' records; each Claim cites the one page its record came from (a5).
Absence over the list retires only when every page is complete, with a
witness citing all of them (a6). A failed page (here: off the permitted
origins) is dropped, the list is incomplete, and absence never retires (a4).
A derive over the list (b4) narrows the pages to one derived view; its
incompleteness follows the list's.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.claim_evidence import CaptureBasis
from tests.core_support._identity_support import accept_tree
from tests.support.operation_world import NOW, Invoker, author_line, author_procedure, build_files
from tests.support.procedures import boundary_contract, contract_files, procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.support.provider_checkout import require_provider_checkout
from tests.test_e2e.test_skeleton_kev_epss import (
    CATALOG,
    DATE_ADDED,
    DELISTED,
    EXCHANGES,
    FIXTURES,
    KEV_KIND,
    KEV_ROW,
    KEV_VIEW_KIND,
    LISTED_IN,
    UNLISTED,
    VULN,
    WATCH_ROW,
    WATCHED,
    _by,
    _contract_digest_in,
    _cve,
    _envelope,
    _manifest,
    _summaries,
    activate,
    pinned_extract_digest,
    proposed,
    run,
    skeleton_world,
)

PAGES_PROVIDER = "test-pages"
ORIGIN = "https://pages.test"
LOGICAL_SOURCE = "cisa.kev.catalog"
PAGES_REQUEST = boundary_contract("test.pages.request", {"feed": "string"})
PAGES_RESULT = boundary_contract("test.pages.result", {"pages": "list<capture<*>>{max_items: 3}"})
PAGES_DERIVE = boundary_contract(
    "test.pages-derive.request",
    {
        "sources": "list<capture<*>>{max_items: 3}",
        "watched": "list<test.watched-row@1>{max_items: 100}",
    },
    WATCH_ROW,
)
VIEW_URL = "https://derive.test/kev-pages-view"


def _pages_build(checkout: Path) -> Any:
    """A core test-double provider: a paged fetch and a pure derive over its pages."""

    from cruxible_client.contracts.provider_operations import ProviderOperation
    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from cruxible_client.contracts.shapes import contract_digest
    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    digests = {
        (item.ref.name, item.ref.version): contract_digest(item).tagged
        for item in (PAGES_REQUEST, PAGES_RESULT, PAGES_DERIVE)
    }
    digests[("web.fetch.result", 1)] = _contract_digest_in(checkout, "web.fetch.result")
    served = tuple(
        ProviderOperation.model_validate(
            {
                "name": name,
                "in": request,
                "out": result,
                "effect": effect,
                "refusals": {},
                "entrypoint": "demo.runtime:Pages",
            }
        )
        for name, request, result, effect in (
            ("fetch_pages", "test.pages.request@1", "test.pages.result@1", "external_read"),
            ("derive_pages", "test.pages-derive.request@1", "web.fetch.result@1", "pure"),
        )
    )
    manifest = ProviderRuntimeManifest(
        provider_id=PAGES_PROVIDER,
        distribution=ProviderDistributionRef(name=PAGES_PROVIDER, version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Pages", backends=("local_env",), deterministic=True
            ),
        ),
    )
    document = PackageRegistrationDocumentV1(schema_version=2, manifest=manifest, operations=served)
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=PAGES_PROVIDER,
            version="1.0.0",
            filename="test_pages-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "e" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain=PAGES_PROVIDER,
        operations=document.pinned_operations(lambda ref: digests.get((ref.name, ref.version))),
    )


def pages_world(tmp_path: Path) -> Any:
    instance = skeleton_world(tmp_path)
    accept_tree(
        instance, contract_files(PAGES_REQUEST, PAGES_RESULT, PAGES_DERIVE), name="pages-contracts"
    )
    accept_tree(instance, build_files(_pages_build(require_provider_checkout())), name="pages")
    return instance


def catalog(exchange: str = "kev-catalog") -> list[dict[str, Any]]:
    body = (FIXTURES / EXCHANGES[exchange]["body"]).read_bytes()
    return list(json.loads(body)["vulnerabilities"])


def split(entries: list[dict[str, Any]]) -> list[bytes]:
    """The catalog as three pages, round-robin."""

    return [
        json.dumps({"vulnerabilities": entries[index::3]}, sort_keys=True).encode()
        for index in range(3)
    ]


def _wire(content: bytes, url: str, *, source: str = LOGICAL_SOURCE) -> dict[str, Any]:
    return {
        "source_identity": source,
        "coordinate_type": "http-response-v1",
        "coordinate": {
            "url": url,
            "final_url": url,
            "content_type": "application/json",
            "request_digest": "sha256:" + hashlib.sha256(url.encode()).hexdigest(),
            "source": "packaged-recording",
            "status": 200,
        },
        "selector_type": "whole-response-v1",
        "selector": {},
        "replayability": "attested_only",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "content_encoding": "base64",
        "byte_length": len(content),
        "bytes_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def view(sources: list[dict[str, Any]], watched: list[dict[str, Any]]) -> bytes:
    """The derive: every page's watched entries, as one canonical document."""

    wanted = {row["cve_id"] for row in watched}
    rows = [
        {"cveID": item["cveID"], "dateAdded": item["dateAdded"]}
        for source in sources
        for item in json.loads(base64.b64decode(source["content_base64"]))["vulnerabilities"]
        if item["cveID"] in wanted
    ]
    document = {"vulnerabilities": sorted(rows, key=lambda row: row["cveID"])}
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Pages:
    """Answers the paged fetch with ``pages`` (one URL each) and the derive over them."""

    def __init__(
        self,
        pages: list[bytes],
        *,
        urls: list[str] | None = None,
        sources: list[str] | None = None,
        incomplete_captures: list[str] | None = None,
    ) -> None:
        self.pages = pages
        self.incomplete_captures = incomplete_captures or []
        self.urls = urls or [f"{ORIGIN}/kev/{index}" for index in range(len(pages))]
        self.sources = sources or [LOGICAL_SOURCE] * len(pages)
        self.derive_inputs: list[dict[str, Any]] = []

    def invoker(self) -> Invoker:
        def output(context: Any) -> dict[str, Any]:
            if "feed" in context.input:
                return {
                    "pages": [
                        _wire(body, url, source=source)
                        for body, url, source in zip(
                            self.pages, self.urls, self.sources, strict=True
                        )
                    ]
                }
            self.derive_inputs.append(dict(context.input))
            body = view(context.input["sources"], context.input["watched"])
            wire = _wire(body, VIEW_URL, source="test.kev-view")
            return {**wire, "tag": "playbill-provider-result-to-external-capture-v1"}

        return Invoker(output=output, incomplete_captures=self.incomplete_captures)


def _claims_step(entries: str, *, absence: bool) -> dict[str, Any]:
    """The KEV claims; with ``absence``, over the watched CVEs (inverse + mirror)."""

    listed: dict[str, Any] = {
        "type": LISTED_IN,
        "value": {"kind": CATALOG, "key": {"catalog_id": "cisa-kev"}},
        "rationale": "A KEV catalog page lists this vulnerability.",
    }
    dated: dict[str, Any] = {"type": DATE_ADDED, "value": "$item.fields.date_added"}
    step: dict[str, Any] = {
        "id": "write",
        "for_each": entries,
        "subject": {
            "kind": VULN,
            "key": {"cve_id": "$item.key.cve_id"},
            "create": "never",
            "within": "$steps.watched.rows",
        },
        "claims": [listed, dated],
    }
    if absence:
        step["expect"] = {"$ref": "step", "step": "watched", "path": ["rows"]}
        listed["when_absent"] = "inverse"
        dated["when_absent"] = "mirror"
    return step


def _extract_step(capture: str, **changes: Any) -> dict[str, Any]:
    return {
        "extract": "entries",
        "capture": capture,
        "records": "/vulnerabilities/*",
        "row": KEV_ROW.ref.spelling,
        "key": {"cve_id": {"at": "cveID", "type": f"key_part<{VULN}:cve_id>"}},
        "duplicates": "refuse_key",
        "fields": {"date_added": {"at": "dateAdded", "parse": "date"}},
        **changes,
    }


def list_spec(
    *,
    absence: bool = True,
    derive: bool = False,
    narrow: bool = False,
    extract: dict[str, Any] | None = None,
) -> Any:
    """fetch-list (-> derive) -> extract -> claims; ``narrow`` puts a filter before claims."""

    steps: list[dict[str, Any]] = [
        {
            "call": "pages",
            "slot": "pages",
            "captures": {"pages": KEV_KIND},
            "input": {"feed": "kev"},
        },
        {"read": "watched", "query": "test.watched-vulnerabilities"},
    ]
    if derive:
        steps.append(
            {
                "call": "view",
                "slot": "derive",
                "captures": {"capture": KEV_VIEW_KIND},
                "input": {"sources": "$steps.pages.pages", "watched": "$steps.watched.rows"},
            }
        )
    steps.append(
        _extract_step("$steps.view.capture" if derive else "$steps.pages.pages", **(extract or {}))
    )
    entries = "$steps.entries.records"
    if narrow:
        steps.append(
            {
                "transform": "selected",
                "kind": "filter",
                "spec": {
                    "input": "$steps.entries.records",
                    "where": {"op": "eq", "left": {"literal": 1}, "right": {"literal": 1}},
                },
            }
        )
        entries = "$steps.selected.records"
    steps.append(_claims_step(entries, absence=absence))
    slots = {
        "pages": {
            "in": "test.pages.request@1",
            "out": "test.pages.result@1",
            "effect": "external_read",
        }
    }
    if derive:
        slots["derive"] = {
            "in": "test.pages-derive.request@1",
            "out": "web.fetch.result@1",
            "effect": "pure",
        }
    name = (
        "test.kev-pages"
        + ("" if absence else "-present")
        + ("-derive" if derive else "")
        + ("-narrow" if narrow else "")
    )
    return build_spec(
        name,
        reference_text=True,
        input={},
        output={},
        slots=slots,
        hard_caps=procedure_limits(provider_calls=2, capture_bytes=1_048_576, read_rows=500),
        steps=steps,
    )


def install(instance: Any, spec: Any, *, line: str, origins: bool = False) -> None:
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        parse_line_spec,
    )
    from tests.support.mandates import procedure_mandate

    author_procedure(instance, spec, name=line + "-procedure")
    author_line(
        instance,
        name=line,
        procedure=spec.procedure,
        parameters={},
        max_authority="propose",
        acquisition={"pages.pages": {"origins": [ORIGIN]}} if origins else None,
        label=line,
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line)
    accepted = parse_line_spec(tree[path], path=path)
    mandate = procedure_mandate(
        line,
        namespace=("claims", "subjects"),
        line=ArtifactPin(
            role="line", target=accepted.identity, artifact_digest=line_spec_digest(accepted).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name=line + "-mandate",
    )


def run_pages(instance: Any, pages: Pages, *, line: str, minute: int = 0) -> Any:
    return run(instance, pages, line=line, minute=minute)  # type: ignore[arg-type]


def page_of(page_bodies: list[bytes]) -> dict[str, int]:
    return {
        item["cveID"]: index
        for index, body in enumerate(page_bodies)
        for item in json.loads(body)["vulnerabilities"]
    }


def listed_watched() -> list[str]:
    return [cve for cve in WATCHED if cve != UNLISTED]


def test_extract_over_a_list_cites_each_records_page(tmp_path: Path) -> None:
    """a5: records merge across pages; each Claim cites its own page, page-local locator."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(absence=False), line="test.kev-pages-line")
    page_bodies = split(catalog())
    state = run_pages(instance, Pages(page_bodies), line="test.kev-pages-line")
    assert state.status == "succeeded", state.terminal
    assert state.completeness is None
    claims = _by(proposed(instance, state))
    listed = listed_watched()
    assert set(claims) == {(_cve(c), p) for c in listed for p in (LISTED_IN, DATE_ADDED)}
    (call,) = (node for node in state.nodes if node.node == "pages")
    members = list(call.summary.capture_digests)
    assert len(members) == 3
    where = page_of(page_bodies)
    spec_digest = pinned_extract_digest(instance, "test.kev-pages-present", "entries")
    for cve in listed:
        for predicate in (DATE_ADDED, LISTED_IN):
            receipt = claims[(_cve(cve), predicate)].backing.procedure_evidence.value
            assert receipt.capture_digest == members[where[cve]]
            assert receipt.extract_spec_digest == spec_digest
            page = json.loads(page_bodies[where[cve]])["vulnerabilities"]
            position = [item["cveID"] for item in page].index(cve)
            assert receipt.record_locator == f"/vulnerabilities/{position}"
        assert claims[(_cve(cve), LISTED_IN)].backing.capture_digests == (members[where[cve]],)
    (extracted,) = _summaries(state, "extract")
    assert extracted.complete and extracted.records == sum(
        len(json.loads(body)["vulnerabilities"]) for body in page_bodies
    )


def test_absence_over_a_complete_list_cites_every_page_and_retires(tmp_path: Path) -> None:
    """a6: every page complete, so absence infers and retires; the witness cites all pages."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line")
    first = run_pages(instance, Pages(split(catalog())), line="test.kev-pages-line")
    assert first.status == "succeeded", first.terminal
    assert first.completeness is None
    claims = _by(proposed(instance, first))
    assert set(claims) == {
        *((_cve(c), LISTED_IN) for c in WATCHED),
        *((_cve(c), DATE_ADDED) for c in listed_watched()),
    }
    (call,) = (node for node in first.nodes if node.node == "pages")
    members = tuple(call.summary.capture_digests)
    unlisted = claims[(_cve(UNLISTED), LISTED_IN)].backing.procedure_evidence
    assert isinstance(unlisted.value, CaptureBasis)
    assert unlisted.absence is not None
    assert unlisted.absence.captures == unlisted.absence.members == members
    assert unlisted.value.capture_digest == members[0] == unlisted.absence.capture
    assert unlisted.absence.logical_source == LOGICAL_SOURCE
    activate(instance, first)

    # The catalog delists one CVE: its date retires and its listing inverts, on a
    # witness citing all three pages of the second run.
    delisted = split(catalog("kev-catalog-delisted"))
    second = run_pages(instance, Pages(delisted), line="test.kev-pages-line", minute=1)
    assert second.status == "succeeded", second.terminal
    changed = _by(proposed(instance, second))
    assert set(changed) == {(_cve(DELISTED), LISTED_IN), (_cve(DELISTED), DATE_ADDED)}
    retired = changed[(_cve(DELISTED), DATE_ADDED)]
    assert retired.lifecycle.state == "retired" and retired.retirement.absence is not None
    (second_call,) = (node for node in second.nodes if node.node == "pages")
    second_members = tuple(second_call.summary.capture_digests)
    assert retired.retirement.absence.captures == second_members
    inverse = changed[(_cve(DELISTED), LISTED_IN)].backing.procedure_evidence
    assert inverse.absence is not None and inverse.absence.captures == second_members
    # Acceptance verifies the witness page by page.
    activate(instance, second)


def test_a_failed_page_leaves_the_list_incomplete_and_absence_suppressed(
    tmp_path: Path,
) -> None:
    """a4 + a6: the off-origin page is dropped; present records still write."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line", origins=True)
    page_bodies = split(catalog())
    urls = [f"{ORIGIN}/kev/0", "https://elsewhere.test/kev/1", f"{ORIGIN}/kev/2"]
    state = run_pages(instance, Pages(page_bodies, urls=urls), line="test.kev-pages-line")
    assert state.status == "succeeded", state.terminal
    assert state.completeness is not None and not state.completeness.complete
    claims = _by(proposed(instance, state))
    where = page_of(page_bodies)
    kept = [cve for cve in listed_watched() if where[cve] != 1]
    assert kept and len(kept) < len(listed_watched())
    # Only the kept pages' records write; nothing is inferred absent.
    assert set(claims) == {
        *((_cve(c), LISTED_IN) for c in kept),
        *((_cve(c), DATE_ADDED) for c in kept),
    }
    (written,) = _summaries(state, "claims")
    assert written.inverse_written == 0


@pytest.mark.parametrize("absence", [False, True])
def test_a_key_in_two_pages_writes_nothing_for_it(tmp_path: Path, absence: bool) -> None:
    """a5: a cross-member duplicate is a duplicate: no Claim for it, and no absence."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(absence=absence), line="test.kev-pages-line")
    entries = catalog()
    page_bodies = split(entries)
    where = page_of(page_bodies)
    twice = listed_watched()[0]
    other = (where[twice] + 1) % 3
    page = json.loads(page_bodies[other])
    page["vulnerabilities"].append(next(item for item in entries if item["cveID"] == twice))
    page_bodies[other] = json.dumps(page, sort_keys=True).encode()
    state = run_pages(instance, Pages(page_bodies), line="test.kev-pages-line")
    assert state.status == "succeeded", state.terminal
    claims = _by(proposed(instance, state))
    assert (_cve(twice), LISTED_IN) not in claims and (_cve(twice), DATE_ADDED) not in claims
    # The duplicate makes the merged records incomplete: nothing is inferred absent.
    assert {c for c, _ in claims} == {_cve(c) for c in listed_watched() if c != twice}
    assert state.completeness is not None and not state.completeness.complete
    (extracted,) = _summaries(state, "extract")
    assert not extracted.complete and extracted.duplicates == 2
    assert any(item.kind == "duplicate_key" for item in state.findings)


def test_a_list_extract_narrowed_before_claims_stays_refused(tmp_path: Path) -> None:
    """a8: list extract -> filter -> claims keeps no per-record lineage; narrow in derive."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(narrow=True), line="test.kev-pages-narrow-line")
    state = run_pages(instance, Pages(split(catalog())), line="test.kev-pages-narrow-line")
    assert state.status != "succeeded"
    assert state.terminal.code == "proposal_item_evidence_ambiguous", state.terminal


@pytest.mark.parametrize("declared", ["header", "complete_when"])
def test_an_extract_over_a_list_declares_no_header_or_complete_when(
    tmp_path: Path, declared: str
) -> None:
    instance = pages_world(tmp_path)
    changes = (
        {"header": {"catalog_version": {"at": "/catalogVersion"}}}
        if declared == "header"
        else {"complete_when": {"count_at": "/count"}}
    )
    # The capture law refuses it when the Procedure is accepted.
    with pytest.raises(AssertionError, match="cruxible.procedure.extract_format_unsupported"):
        author_procedure(
            instance, list_spec(absence=False, extract=changes), name="refused-procedure"
        )


@pytest.mark.parametrize("failed", [False, True])
def test_a_derive_over_the_list_follows_its_completeness(tmp_path: Path, failed: bool) -> None:
    """fetch-list -> derive -> extract -> claims: the view's absence is the list's."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(derive=True), line="test.kev-pages-derive-line", origins=True)
    page_bodies = split(catalog())
    urls = [f"{ORIGIN}/kev/{index}" for index in range(3)]
    if failed:
        urls[1] = "https://elsewhere.test/kev/1"
    pages = Pages(page_bodies, urls=urls)
    first = run_pages(instance, pages, line="test.kev-pages-derive-line")
    assert first.status == "succeeded", first.terminal
    (call,) = (node for node in first.nodes if node.node == "pages")
    members = tuple(call.summary.capture_digests)
    assert len(members) == (2 if failed else 3)
    # The derive received every kept page as its exact bytes.
    (derive_input,) = pages.derive_inputs
    assert [base64.b64decode(item["content_base64"]) for item in derive_input["sources"]] == [
        body for index, body in enumerate(page_bodies) if not (failed and index == 1)
    ]
    claims = _by(proposed(instance, first))
    where = page_of(page_bodies)
    kept = [cve for cve in listed_watched() if not (failed and where[cve] == 1)]
    root = claims[(_cve(kept[0]), DATE_ADDED)].backing.procedure_evidence.value.capture_digest
    # The view records every page it read; each Claim cites the view, its one root.
    assert set(_manifest(instance, root).input_capture_digests) == set(members)
    assert (
        _envelope(instance, root).capture_contract_digest
        != _envelope(instance, members[0]).capture_contract_digest
    )
    if failed:
        # The list was incomplete, so the view is: nothing is inferred absent.
        assert first.completeness is not None and not first.completeness.complete
        assert set(claims) == {
            *((_cve(c), LISTED_IN) for c in kept),
            *((_cve(c), DATE_ADDED) for c in kept),
        }
        return
    assert first.completeness is None
    unlisted = claims[(_cve(UNLISTED), LISTED_IN)].backing.procedure_evidence
    assert unlisted.absence is not None and unlisted.absence.captures == (root,)
    activate(instance, first)

    # Every page complete: the delisting retires, cited to the view over all pages.
    second = run_pages(
        instance,
        Pages(split(catalog("kev-catalog-delisted")), urls=urls),
        line="test.kev-pages-derive-line",
        minute=1,
    )
    assert second.status == "succeeded", second.terminal
    changed = _by(proposed(instance, second))
    retired = changed[(_cve(DELISTED), DATE_ADDED)]
    assert retired.lifecycle.state == "retired"
    witness = retired.retirement.absence
    (second_call,) = (node for node in second.nodes if node.node == "pages")
    assert set(_manifest(instance, witness.capture).input_capture_digests) == set(
        second_call.summary.capture_digests
    )
    activate(instance, second)


# -- Every member of a witness is owed proof (review-template-b, finding 1) ----------


def _delisting(instance: Any, *, line: str = "test.kev-pages-line", **pages: Any) -> Any:
    """Run the complete catalog, activate it, then run the delisted catalog."""

    first = run_pages(instance, Pages(split(catalog())), line=line)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    second = run_pages(
        instance, Pages(split(catalog("kev-catalog-delisted")), **pages), line=line, minute=1
    )
    return second


def _absence_paths(instance: Any, state: Any) -> tuple[Any, Any]:
    """The delisting's inverse (backing witness) and retirement (retirement witness)."""

    from cruxible_client.contracts.claims import claim_path

    changed = _by(proposed(instance, state))
    inverse = changed[(_cve(DELISTED), LISTED_IN)]
    retired = changed[(_cve(DELISTED), DATE_ADDED)]
    assert inverse.backing.procedure_evidence.absence.members
    assert retired.retirement.absence.members
    return claim_path(inverse.identity.name), claim_path(retired.identity.name)


def _later_member(state: Any) -> str:
    (call,) = (node for node in state.nodes if node.node == "pages")
    members = tuple(call.summary.capture_digests)
    assert len(members) == 3
    return members[-1]


def _law_refusals(monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    """Every Claim path the Claim law refuses from here on, with its codes.

    Recorded at the law itself, so a refusal at delivery's evaluation and one
    at settlement's re-evaluation read alike.
    """

    from cruxible_core.proposals import proposals

    refused: dict[str, set[str]] = {}
    evaluate = proposals.evaluate_claim_law

    def recording(claim: Any, **kwargs: Any) -> Any:
        result = evaluate(claim, **kwargs)
        for item in result.diagnostics if result.verdict == "refused" else ():
            refused.setdefault(kwargs["path"], set()).add(item.code)
        return result

    monkeypatch.setattr(proposals, "evaluate_claim_law", recording)
    return refused


@pytest.mark.parametrize("defect", ["missing", "corrupt", "unproduced"])
def test_acceptance_verifies_every_member_of_an_inverse_and_a_retirement_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    """Only the LAST page is missing, corrupt or unproduced: the inverse and the retirement
    both refuse, though the receipt each carries cites the first page."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line")
    second = _delisting(instance)
    assert second.status == "succeeded", second.terminal
    inverse_path, retired_path = _absence_paths(instance, second)
    later = _later_member(second)
    store = instance.body_store()
    envelope = _envelope(instance, later)
    if defect == "missing":
        assert store.erase(envelope.commitment.digest)
    elif defect == "corrupt":
        target = store._path(envelope.commitment.digest)
        target.chmod(0o644)
        target.write_bytes(b"not the page")
    else:
        from cruxible_core.exhaust.producer_receipts import JournalProducerReceiptResolver

        produced = JournalProducerReceiptResolver.produced_capture

        def unproduced(self: Any, capture_digest: str, *, run_id: str) -> Any:
            return () if capture_digest == later else produced(self, capture_digest, run_id=run_id)

        monkeypatch.setattr(JournalProducerReceiptResolver, "produced_capture", unproduced)
    refused = _law_refusals(monkeypatch)
    from cruxible_client.contracts.errors import SettlementIntegrityError

    with pytest.raises(SettlementIntegrityError):
        activate(instance, second)
    assert refused.get(inverse_path) == {"cruxible.claim.evidence_unavailable"}, refused
    assert refused.get(retired_path) == {"cruxible.claim.evidence_unavailable"}, refused


def test_acceptance_refuses_a_witness_whose_later_member_names_another_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last page names another logical source. A run that (here, wrongly) let it into
    the enumeration still cannot land the inverse or the retirement: the law reads every
    member's own envelope."""

    from cruxible_client.contracts.claims import claim_path
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line")
    first = run_pages(instance, Pages(split(catalog())), line="test.kev-pages-line")
    held = _by(proposed(instance, first))
    activate(instance, first)
    real = AcceptedClaimsContext.logical_source

    def blind(self: Any, capture: str) -> str | None:
        found = real(self, capture)
        return LOGICAL_SOURCE if found == "other.source" else found

    monkeypatch.setattr(AcceptedClaimsContext, "logical_source", blind)
    refused = _law_refusals(monkeypatch)
    sources = [LOGICAL_SOURCE, LOGICAL_SOURCE, "other.source"]
    second = run_pages(
        instance,
        Pages(split(catalog("kev-catalog-delisted")), sources=sources),
        line="test.kev-pages-line",
        minute=1,
    )
    assert second.status != "succeeded"
    # The inverse revises the held listing; the retirement retires the held date.
    assert refused == {
        claim_path(held[(_cve(DELISTED), predicate)].identity.name): {
            "cruxible.claim.evidence_unavailable"
        }
        for predicate in (LISTED_IN, DATE_ADDED)
    }, refused


@pytest.mark.parametrize("defect", [None, "unavailable", "invalid", "other_source"])
def test_an_absence_reobservation_verifies_every_member_of_its_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str | None
) -> None:
    """A held inverse re-observed on a complete list records only when every page stands.

    Only the LAST page is defective: unavailable, failing verification, or naming another
    logical source. The re-observation's receipt cites the first page, which is sound.
    """

    from cruxible_core.evidence.claim_reobservations import ClaimReobservationStore
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext
    from cruxible_core.service.evidence import capture_reads

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line")
    first = run_pages(instance, Pages(split(catalog())), line="test.kev-pages-line")
    held = _by(proposed(instance, first))
    activate(instance, first)
    delisted = split(catalog("kev-catalog-delisted"))
    second = run_pages(instance, Pages(delisted), line="test.kev-pages-line", minute=1)
    activate(instance, second)
    inverse = held[(_cve(DELISTED), LISTED_IN)].identity.name
    assert ClaimReobservationStore(instance).history(inverse) == ()

    last = f"{ORIGIN}/kev/2"
    sources = None
    if defect == "other_source":
        sources = [LOGICAL_SOURCE, LOGICAL_SOURCE, "other.source"]
        real_source = AcceptedClaimsContext.logical_source

        def blind(self: Any, capture: str) -> str | None:
            found = real_source(self, capture)
            return LOGICAL_SOURCE if found == "other.source" else found

        monkeypatch.setattr(AcceptedClaimsContext, "logical_source", blind)
    elif defect is not None:
        real_verify = capture_reads.verify_accepted_capture

        def failing(instance: Any, coordinate: Any, digest: str, **kwargs: Any) -> Any:
            verified = real_verify(instance, coordinate, digest, **kwargs)
            if isinstance(verified, str) or verified.envelope.source.coordinate.get("url") != last:
                return verified
            if defect == "unavailable":
                return "capture_unavailable"
            raise capture_reads.CaptureReadInvalid("Capture verification failed: corrupt")

        monkeypatch.setattr(capture_reads, "verify_accepted_capture", failing)
    third = run_pages(
        instance, Pages(delisted, sources=sources), line="test.kev-pages-line", minute=2
    )
    assert third.status == "succeeded", third.terminal
    (call,) = (node for node in third.nodes if node.node == "pages")
    members = tuple(call.summary.capture_digests)
    recorded = ClaimReobservationStore(instance).history(inverse)
    if defect is None:
        assert [record.capture_digest for record in recorded] == [members[0]]
    else:
        assert recorded == ()


def test_a_list_the_provider_says_is_incomplete_never_retires(tmp_path: Path) -> None:
    """All three pages land, but the provider names ``pages`` incomplete (a page it failed
    itself): the delisting neither retires nor inverts, as for a page core dropped."""

    instance = pages_world(tmp_path)
    install(instance, list_spec(), line="test.kev-pages-line")
    first = run_pages(instance, Pages(split(catalog())), line="test.kev-pages-line")
    activate(instance, first)
    second = run_pages(
        instance,
        Pages(split(catalog("kev-catalog-delisted")), incomplete_captures=["pages"]),
        line="test.kev-pages-line",
        minute=1,
    )
    assert second.status == "succeeded", second.terminal
    (call,) = (node for node in second.nodes if node.node == "pages")
    assert len(call.summary.capture_digests) == 3
    assert second.completeness is not None and not second.completeness.complete
    (written,) = _summaries(second, "claims")
    assert written.inverse_written == 0
    # Nothing changed: no retirement, no inverse, so nothing is proposed.
    assert second.terminal_egress[0].proposal_id is None
    assert any(
        item.kind == "claim_not_in_source" and item.source_incomplete for item in second.findings
    ), second.findings
