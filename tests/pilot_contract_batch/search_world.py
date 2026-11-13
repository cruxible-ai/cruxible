"""The world scenarios 4, 5 and 7 share: the base kit's search pair writing page titles.

A Procedure reads the instance's pages, asks ``web.search@1`` (a raw fetch,
verbatim answer) and ``web.search.results@1`` (a pure derive into the base
kit's ``web.search-results`` kind), extracts the ``results`` record set by name
(the thin form of note §4.3, overriding the ``url`` key part with this
instance's Subject key), and writes ``pilot.page.title`` for each page, mirroring
absence on a complete read.

Everything before the base kit exists is ordinary authoring today (vocabulary,
Subjects, a Query). The Procedure, the provider installs and the Line go
through ``surfaces`` helpers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from tests.pilot_contract_batch import surfaces
from tests.pilot_contract_batch.fixtures import providers
from tests.pilot_contract_batch.harness import Scenario

PAGE = "pilot.page"
URL = PAGE + ".url"
TITLE = PAGE + ".title"
PAGES_QUERY = "pilot.pages"
PAGE_ROW = "pilot.page-row"
PROCEDURE = "pilot.search-titles"
LINE = "pilot.search-titles"
QUERY = "cruxible"
URLS = tuple(hit["link"] for hit in providers.RECORDED_HITS[QUERY])

#: The eight capabilities the base kit declares (note §7.4.1; ruling O2 split file reading).
BASE_CAPABILITIES = (
    "web.fetch",
    "web.fetch.pages",
    "web.render",
    "web.render.dom",
    "web.search",
    "web.search.results",
    "workspace.file",
    "workspace.files",
)


def fresh_instance(tmp_path: Path) -> Any:
    from tests.core_support._support import initialize_local

    root = tmp_path / "instance"
    root.mkdir(parents=True, exist_ok=True)
    instance, _owner = initialize_local(root)
    return instance


def show_capabilities(pilot: Scenario, instance: Any) -> tuple[surfaces.CapabilityEntry, ...]:
    """``cruxible list Capability``: the precondition every base-kit scenario starts from."""

    pilot.call("cruxible list Capability")
    listed = surfaces.list_capabilities(instance)
    for entry in listed:
        pilot.got(f"{entry.pin.spelling:24} {entry.effect:14} kit {entry.kit_id}")
    assert {entry.pin.name for entry in listed} >= set(BASE_CAPABILITIES)
    return listed


def refs(value: Any) -> Any:
    """``$steps``/``$item``/``$params`` strings as typed references (the kits' own helper)."""

    from cruxible_client.contracts.refs import ref_from_graph_text

    if isinstance(value, str):
        found = ref_from_graph_text(value)
        return value if found is None else found.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {key: refs(member) for key, member in value.items()}
    if isinstance(value, list | tuple):
        return [refs(member) for member in value]
    return value


def author_vocabulary(pilot: Scenario, instance: Any) -> None:
    """The raw search-response kind, the page Subjects, their ClaimTypes and a pages Query."""

    from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput, SubjectInput
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from cruxible_client.contracts.query.authored import QuerySpecInput
    from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import JSON_DECODING, http_contract
    from tests.core_support._subject_kind_support import keyed_subject_kind
    from tests.support.operation_world import author
    from tests.support.procedures import boundary_contract, contract_files
    from tests.test_e2e.test_skeleton_kev_epss import _claim_type, _key_type

    title = _claim_type(
        TITLE,
        "string",
        allowed_subject_kinds=[PAGE],
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="captured",
                    claim_roles=("observation",),
                    capture_contracts=(providers.RESULTS_KIND,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    row = boundary_contract(PAGE_ROW, {"url": "string{max_len: 2048}"})
    kind = http_contract(name=providers.SEARCH_RESPONSE_KIND, decoding=JSON_DECODING)
    accept_tree(
        instance,
        {
            **contract_files(row),
            capture_contract_path(kind.identity.name): render_capture_contract(kind),
            subject_kind_path(PAGE): render_subject_kind(keyed_subject_kind(PAGE, (URL,))),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (title, _key_type(URL, PAGE))
            },
        },
        name="pilot-vocabulary",
    )
    for index, url in enumerate(URLS):
        author(
            instance,
            SubjectInput(kind="subject", subject_kind=PAGE, key={"url": url}, rationale="Watched."),
            name=f"page-{index}",
        )
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": PAGES_QUERY,
                    "from": {"p": PAGE},
                    "row": f"{PAGE_ROW}@1",
                    "fields": ["p.url"],
                }
            ),
        ),
        name="pages-query",
    )
    pilot.call("cruxible authoring submit vocabulary.json")
    pilot.got(
        f"accepted: CaptureKind {providers.SEARCH_RESPONSE_KIND}, SubjectKind {PAGE}, "
        f"ClaimTypes {URL} and {TITLE}, Query {PAGES_QUERY}, {len(URLS)} pages"
    )


HARD_CAPS = {
    "wall_clock": "PT5M",
    "provider_calls": 2,
    "capture_bytes": 1_048_576,
    "items": 1_000,
    "read_rows": 1_000,
    "repeat_attempts": 1,
}
SEARCH_STEP = {
    "call": "search",
    "slot": "search",
    "captures": {"response": providers.SEARCH_RESPONSE_KIND},
    "input": {"query": QUERY},
}
RANKED_STEP = {
    "call": "ranked",
    "slot": "results",
    "captures": {"results": providers.RESULTS_KIND},
    "input": {"response": "$steps.search.response"},
}


def calls_spec(*, results: str = f"{providers.RESULTS}@1") -> dict[str, Any]:
    """The two calls only: what binding needs (no extract, so no layout is read)."""

    return refs(
        {
            "procedure": PROCEDURE,
            "description": "Ask the search pair about the project.",
            "input": {},
            "output": {},
            "slots": {
                "search": {"capability": f"{providers.SEARCH}@1"},
                "results": {"capability": results},
            },
            "hard_caps": HARD_CAPS,
            "steps": [SEARCH_STEP, RANKED_STEP],
        }
    )


def titles_spec() -> dict[str, Any]:
    """The whole Procedure: read pages, the two calls, a thin extract, the title Claims."""

    return refs(
        {
            "procedure": PROCEDURE,
            "description": "Ask the search pair about the project and record each watched "
            "page's title as the results list it.",
            "input": {},
            "output": {},
            "slots": {
                "search": {"capability": f"{providers.SEARCH}@1"},
                "results": {"capability": f"{providers.RESULTS}@1"},
            },
            "hard_caps": HARD_CAPS,
            "steps": [
                {"read": "pages", "query": PAGES_QUERY},
                SEARCH_STEP,
                RANKED_STEP,
                # The thin form (note §4.3): the record set by name; the url key part
                # takes this instance's Subject key type.
                {
                    "extract": "hits",
                    "capture": "$steps.ranked.results",
                    "records": "results",
                    "key": {"url": {"type": f"key_part<{PAGE}:url>"}},
                },
                {
                    "id": "write",
                    "for_each": "$steps.hits.records",
                    "subject": {"kind": PAGE, "key": {"url": "$item.key.url"}, "create": "never"},
                    "expect": "$steps.pages.rows",
                    "claims": [
                        {
                            "type": TITLE,
                            "value": "$item.fields.title",
                            "when_absent": "mirror",
                            "rationale": "The search results title {$item.key.url} "
                            "{$item.fields.title}.",
                        }
                    ],
                },
            ],
        }
    )


def line_input(bindings: Mapping[str, str], *, name: str = LINE) -> dict[str, Any]:
    return {
        "kind": "line",
        "name": name,
        "procedure_name": PROCEDURE,
        "bindings": dict(bindings),
        "parameters": {},
        "acquisition": {"search.response": {"origins": [providers.SEARCH_ORIGIN]}},
        "max_authority": "propose",
    }


def grant(instance: Any, line: str = LINE) -> None:
    """A propose Mandate for the accepted Line (the e2e skeleton's own helper)."""

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
    from tests.core_support._identity_support import accept_tree
    from tests.support.mandates import procedure_mandate

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line)
    accepted = parse_line_spec(tree[path], path=path)
    mandate = procedure_mandate(
        line,
        namespace=("claims",),
        line=ArtifactPin(
            role="line", target=accepted.identity, artifact_digest=line_spec_digest(accepted).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name=line + "-mandate",
    )


def operator(bound: Mapping[str, providers.PilotPackage]) -> Any:
    """Answers each call from the package bound to its operation name."""

    from tests.support.operation_world import Invoker, Operator

    def output(context: Any) -> dict[str, Any]:
        return bound[context.operation].answer(context.operation, context.input)

    return Operator(Invoker(output=output))


def run(instance: Any, bound: Mapping[str, providers.PilotPackage], *, minute: int = 0) -> Any:
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.support.operation_world import NOW, actor
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=LINE,
        request=LineRunRequest(line=LINE),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=operator(bound),
    )


def activate(instance: Any, state: Any) -> None:
    from tests.test_procedures.test_claims_terminal import activate as activate_proposal

    activate_proposal(instance, state)


def titles(instance: Any) -> dict[str, str]:
    """url -> title, from the live ``pilot.page.title`` Claims."""

    from tests.pilot_contract_batch.security_kit import live_claims

    urls = {
        claim.statement.subject.artifact_path: str(claim.statement.object.value)
        for claim in live_claims(instance, URL)
    }
    return {
        urls[claim.statement.subject.artifact_path]: str(claim.statement.object.value)
        for claim in live_claims(instance, TITLE)
    }


def evidence_shape(instance: Any, state: Any) -> list[tuple[str, int]]:
    """Per proposed title Claim: (url, how many Captures back it), sorted."""

    from tests.pilot_contract_batch.security_kit import live_claims
    from tests.test_procedures.test_claims_terminal import candidate_claims

    urls = {
        claim.statement.subject.artifact_path: str(claim.statement.object.value)
        for claim in live_claims(instance, URL)
    }
    return sorted(
        (urls[claim.statement.subject.artifact_path], len(claim.backing.capture_digests))
        for claim in candidate_claims(instance, state)
        if claim.statement.predicate == TITLE
    )


def setup(
    pilot: Scenario, tmp_path: Path, spec: Mapping[str, Any]
) -> tuple[Any, providers.BaseKit]:
    """A fresh instance with the base kit checked, the vocabulary authored, the search
    provider installed and the Procedure ``spec`` accepted."""

    instance = fresh_instance(tmp_path)
    show_capabilities(pilot, instance)
    author_vocabulary(pilot, instance)
    base = providers.BaseKit()
    install(pilot, instance, providers.search_package(base))
    pilot.call(f"cruxible authoring submit procedure.json   ({PROCEDURE})")
    pins = surfaces.author_capability_procedure(instance, spec)
    for slot, pin in sorted(pins.items()):
        pilot.got(f"slot {slot}: {pin.spelling} ({pin.digest[:19]}...)")
    return instance, base


def install(pilot: Scenario, instance: Any, package: providers.PilotPackage) -> Any:
    pilot.call(f"cruxible provider install {package.provider_id}")
    installed = surfaces.install_provider_package(instance, package)
    for operation in installed.operations:
        pilot.got(f"{installed.build}#{operation.name} serves {', '.join(operation.serves)}")
    return installed


def show_refusal(pilot: Scenario, refused: Any) -> tuple[str, str]:
    """Print a ``RequestRefusedError`` as the CLI shows it; its (code, message)."""

    code = refused.error_code
    message = str(refused).removeprefix(f"{code}: ")
    pilot.got(f"refused {code}")
    pilot.got(message)
    return code, message
