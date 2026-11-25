"""0.6.0: ``kit add`` proposes its missing providers and its definitions as ONE change.

A commit prepares every missing provider package first (nothing registered or
proposed), then submits one proposal holding every ProviderBuild, the kit's
definitions and its receipt: one review lists every provider's operations and
reach beside the definitions, and one approval lands them all in one generation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.kits import KitBundle
from cruxible_client.contracts.proposal_summary import (
    ProposalReviewSummary,
    ReviewProviderDefinition,
)
from cruxible_client.contracts.providers import parse_provider
from cruxible_client.contracts.text_display import render_review_summary
from cruxible_core.errors import ConfigError
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import PROVIDER_RUNTIME_CONFIG_PATH
from cruxible_core.service.procedures import provider_installation
from cruxible_core.service.proposals.review import service_review_playbill_proposal
from tests.support.provider_checkout import ProviderCheckout
from tests.test_server.test_kit_providers import (
    BLUEPRINT,
    PROCEDURE,
    _add,
    _author,
    _build,
    _install,
    _package,
    _tree,
    _worlds,
)

# _author authors Procedure step kinds the template closes at authoring.
pytestmark = pytest.mark.executor_only

PROVIDERS = ("kit-call", "kit-echo")


def _release(tmp_path: Path, checkout: ProviderCheckout, publisher: Any) -> KitBundle:
    """A kit bundling two provider packages, beside open Procedures."""

    projects = [
        _package(tmp_path / f"kit-source-{name}", checkout, name=name) for name in PROVIDERS
    ]
    for project in projects:
        _install(publisher, project, control_domain="publisher")
    _author(publisher)
    release = _build(publisher, *projects)
    assert {item.provider_id for item in release.manifest.providers} == set(PROVIDERS)
    return release


def _generation(world: Any) -> int:
    instance = get_playbill_manager().get(world.instance_id)
    with instance.accepted_history_reader() as history:
        return int(history.sequence)


def _proposals(world: Any) -> set[str]:
    listed = playbill_api.playbill_list_proposals(world.instance_id, limit=200)
    assert not listed.truncated
    return {entry.proposal_id for entry in listed.entries}


def _acme_resumes(world: Any) -> tuple[Any, ...]:
    rows = playbill_api.playbill_kit_status(world.instance_id).resumes
    return tuple(row for row in rows if row.kit_id == "acme")


def _builds(tree: dict[str, bytes]) -> dict[str, str]:
    """Provider id -> build path, for the kit's providers live in ``tree``."""

    found: dict[str, str] = {}
    for path in tree:
        for name in PROVIDERS:
            if path.startswith(f"providers/{name}/"):
                provider = parse_provider(tree[path], path=path)
                assert provider.lifecycle.state == "live"
                found[name] = path
    return found


def _deployments(tmp_path: Path) -> list[Any]:
    config = tmp_path / "server-state" / PROVIDER_RUNTIME_CONFIG_PATH
    return list(json.loads(config.read_text()).get("deployments", []))


def _installations(world: Any) -> set[str]:
    root = get_playbill_manager().get(world.instance_id).root
    directory = root / "exhaust" / "provider-installations"
    return {path.name for path in directory.iterdir()} if directory.is_dir() else set()


@pytest.fixture
def worlds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: Any) -> Iterator[Any]:
    independent = bool(getattr(request, "param", False))
    opened = _worlds(tmp_path, monkeypatch, preaccept=False, independent=independent)
    pair = next(opened)
    try:
        yield pair
    finally:
        for _ in opened:
            pass


@pytest.mark.parametrize("worlds", [True], indirect=True)
def test_independent_approval_one_proposal_one_review_one_approval(
    tmp_path: Path, provider_checkout: ProviderCheckout, worlds: Any
) -> None:
    publisher, consumer = worlds
    release = _release(tmp_path, provider_checkout, publisher)
    before_tree = _tree(consumer)
    before_generation = _generation(consumer)
    before_proposals = _proposals(consumer)

    added = _add(consumer, release, dry_run=False)

    # One proposal, awaiting the reviewer; no kit_resume wait and nothing landed.
    assert added.status == "proposed", added
    assert added.approval_required
    assert added.proposal_id is not None
    assert _proposals(consumer) - before_proposals == {added.proposal_id}
    assert {step.provider_id: step.action for step in added.providers} == {
        name: "awaiting_approval" for name in PROVIDERS
    }
    assert {step.proposal_id for step in added.providers} == {added.proposal_id}
    assert _acme_resumes(consumer) == ()
    assert "proposed automatically" not in (added.detail or "")
    assert _tree(consumer) == before_tree
    assert _generation(consumer) == before_generation
    # Both deployments are registered for the reviewer's approval to run them.
    assert len(_deployments(tmp_path)) >= len(PROVIDERS)

    # The review lists every added provider first, each with its operations and
    # reach, then the kit's definitions.
    instance = get_playbill_manager().get(consumer.instance_id)
    review = service_review_playbill_proposal(
        instance,
        proposal_id=added.proposal_id,
        access=BodyAccessContext(principal_id="operator", can_read_body=True),
    )
    assert isinstance(review, ProposalReviewSummary)
    kinds = [item.kind for item in review.definitions]
    assert kinds[: len(PROVIDERS)] == ["Provider"] * len(PROVIDERS)
    providers = [item for item in review.definitions if item.kind == "Provider"]
    assert all(item.action == "created" for item in providers)
    details = [item.detail for item in providers]
    assert all(isinstance(detail, ReviewProviderDefinition) for detail in details)
    built = [detail for detail in details if isinstance(detail, ReviewProviderDefinition)]
    assert {detail.provider for detail in built} == set(PROVIDERS)
    for detail in built:
        assert detail.operations, detail
        assert all(operation.effect == "pure" for operation in detail.operations)
    names = {item.name for item in review.definitions}
    assert {PROCEDURE, BLUEPRINT} <= names
    assert any(item.kind == "Document" for item in review.definitions)
    text = render_review_summary(review)
    for name in PROVIDERS:
        assert f"Provider {name}/" in text
    assert text.count("    op local.increment: pure") == len(PROVIDERS)
    assert text.index("Provider ") < text.index(PROCEDURE)

    # One approval activates every build and every definition together.
    consumer.approve(added.proposal_id)
    after = _tree(consumer)
    assert _generation(consumer) - before_generation == 1
    assert set(_builds(after)) == set(PROVIDERS)
    assert all(after[path] == content for path, content in release.contents().items())
    assert _acme_resumes(consumer) == ()
    (installed,) = [
        kit
        for kit in playbill_api.playbill_kit_status(consumer.instance_id).kits
        if kit.kit_id == "acme"
    ]
    assert installed.version == "1.0.0"


def test_self_approval_lands_providers_and_definitions_in_one_generation(
    tmp_path: Path, provider_checkout: ProviderCheckout, worlds: Any
) -> None:
    publisher, consumer = worlds
    release = _release(tmp_path, provider_checkout, publisher)
    before_generation = _generation(consumer)
    assert not _builds(_tree(consumer))

    added = _add(consumer, release, dry_run=False)

    assert added.status == "accepted", added
    assert added.proposal_id is not None
    assert {step.provider_id: step.action for step in added.providers} == {
        name: "install" for name in PROVIDERS
    }
    assert {step.proposal_id for step in added.providers} == {added.proposal_id}
    assert _generation(consumer) - before_generation == 1
    after = _tree(consumer)
    assert set(_builds(after)) == set(PROVIDERS)
    assert all(after[path] == content for path, content in release.contents().items())
    assert _acme_resumes(consumer) == ()

    # The same release again changes nothing and prepares nothing new.
    again = _add(consumer, release, dry_run=False)
    assert again.status == "unchanged", again
    assert {step.action for step in again.providers} == {"unchanged"}
    assert _generation(consumer) - before_generation == 1


@pytest.mark.parametrize("worlds", [False, True], indirect=True)
def test_dry_run_stays_offline_and_read_only(
    tmp_path: Path, provider_checkout: ProviderCheckout, worlds: Any
) -> None:
    publisher, consumer = worlds
    release = _release(tmp_path, provider_checkout, publisher)
    before_tree = _tree(consumer)
    before_generation = _generation(consumer)
    before_proposals = _proposals(consumer)
    before_deployments = _deployments(tmp_path)
    before_installations = _installations(consumer)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a kit add preview never prepares a provider")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(provider_installation, "_prepare_installation", refuse)
        preview = _add(consumer, release)

    assert preview.status == "would_propose", preview
    assert preview.proposal_id is None
    assert {step.provider_id: step.action for step in preview.providers} == {
        name: "would_install" for name in PROVIDERS
    }
    assert "single combined proposal" in (preview.detail or "")
    assert preview.plan
    assert _tree(consumer) == before_tree
    assert _generation(consumer) == before_generation
    assert _proposals(consumer) == before_proposals
    assert _deployments(tmp_path) == before_deployments
    assert _installations(consumer) == before_installations
    assert _acme_resumes(consumer) == ()


def test_a_provider_that_cannot_be_prepared_refuses_before_anything_is_proposed(
    tmp_path: Path,
    provider_checkout: ProviderCheckout,
    worlds: Any,
) -> None:
    publisher, consumer = worlds
    release = _release(tmp_path, provider_checkout, publisher)
    first, second = release.manifest.providers
    before_tree = _tree(consumer)
    before_generation = _generation(consumer)
    before_proposals = _proposals(consumer)
    before_deployments = _deployments(tmp_path)

    prepare = provider_installation._prepare_installation
    prepared: list[str] = []

    def failing(instance: Any, operator: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        assert request.wheel is not None
        if request.wheel.filename == second.wheel.filename:
            raise ConfigError(f"simulated preparation failure for {second.provider_id}")
        result = prepare(instance, operator, request, *args, **kwargs)
        prepared.append(request.wheel.filename)
        return result

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(provider_installation, "_prepare_installation", failing)
        with pytest.raises(Exception, match="simulated preparation failure"):
            _add(consumer, release, dry_run=False)

    # The first provider was prepared; nothing was registered, proposed or accepted.
    assert prepared == [first.wheel.filename]
    assert _tree(consumer) == before_tree
    assert _generation(consumer) == before_generation
    assert _proposals(consumer) == before_proposals
    assert _deployments(tmp_path) == before_deployments
    assert _acme_resumes(consumer) == ()

    # Once preparation succeeds, the same add lands atomically, reusing the
    # retained preparation of the first provider.
    added = _add(consumer, release, dry_run=False)
    assert added.status == "accepted", added
    assert _generation(consumer) - before_generation == 1
    assert set(_builds(_tree(consumer))) == set(PROVIDERS)


def test_admission_refusing_the_combined_candidate_leaves_deployments_registered(
    tmp_path: Path,
    provider_checkout: ProviderCheckout,
    worlds: Any,
) -> None:
    """``register_all`` runs just before the kit submits (as a lone install registers
    before it proposes): a refusal at admission leaves the deployments registered,
    with no build accepted and nothing proposed."""

    from cruxible_core.service import kits

    publisher, consumer = worlds
    release = _release(tmp_path, provider_checkout, publisher)
    before_tree = _tree(consumer)
    before_generation = _generation(consumer)
    before_proposals = _proposals(consumer)
    batches = provider_installation.BatchedProviderPreparations
    real_register_all = batches.register_all
    registered: list[tuple[str, ...]] = []

    def register_all(self: Any) -> None:
        registered.append(tuple(sorted(item.provider_id for item in self.items)))
        real_register_all(self)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        assert registered, "register_all runs before the kit submits"
        raise ConfigError("simulated admission refusal of the combined candidate")

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(batches, "register_all", register_all)
        patched.setattr(kits, "_submit", refuse)
        with pytest.raises(Exception, match="simulated admission refusal"):
            _add(consumer, release, dry_run=False)

    assert registered == [PROVIDERS]
    assert _tree(consumer) == before_tree
    assert _generation(consumer) == before_generation
    assert _proposals(consumer) == before_proposals
    assert len(_deployments(tmp_path)) >= len(PROVIDERS)
    assert not _builds(_tree(consumer))

    # Registration grants nothing on its own: the same add then lands atomically.
    added = _add(consumer, release, dry_run=False)
    assert added.status == "accepted", added
    assert _generation(consumer) - before_generation == 1
    assert set(_builds(_tree(consumer))) == set(PROVIDERS)


def test_only_providers_whose_build_the_proposal_adds_name_it() -> None:
    """A provider already accepted here keeps its own step: it adds no write, so it
    neither waits on the kit's proposal nor names it."""

    from cruxible_client.contracts.kits import KitChangeResult, KitProviderStep
    from cruxible_core.service.kits import _finalize_provider_steps

    proposal = "sha256:" + "a" * 64
    steps = (
        KitProviderStep(
            provider_id="kit-call", package="kit-call", version="1.0.0", action="would_install"
        ),
        KitProviderStep(
            provider_id="kit-echo", package="kit-echo", version="1.0.0", action="install"
        ),
    )
    result = KitChangeResult(
        kit_id="acme",
        version="1.0.0",
        status="proposed",
        proposal_id=proposal,
        providers=steps,
    )

    finalized = _finalize_provider_steps(result, frozenset({"kit-call"}))

    by_id = {step.provider_id: step for step in finalized.providers}
    assert (by_id["kit-call"].action, by_id["kit-call"].proposal_id) == (
        "awaiting_approval",
        proposal,
    )
    assert (by_id["kit-echo"].action, by_id["kit-echo"].proposal_id) == ("install", None)
