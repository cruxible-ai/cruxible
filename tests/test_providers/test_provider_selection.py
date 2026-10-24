import pytest

from cruxible_client.authoring.procedures import ProviderBinding, ProviderSelectionError
from cruxible_client.contracts import ProviderInterfaceEntry, ProviderInterfaceImplementation


def test_bare_name_does_not_fall_back_to_old_only_fitting_build():
    old = "Provider:demo/" + "a" * 64
    entry = ProviderInterfaceEntry(
        tag="playbill-provider-interface-entry-v1",
        identity="ProviderInterface:demo.interface/v1",
        artifact_digest="sha256:" + "2" * 64,
        artifact_kind="ProviderInterface",
        pin_role="provider-interface",
        interface_digest="sha256:" + "3" * 64,
        effect_class="pure",
        interface_basis="accepted_registration",
        providers=[
            ProviderInterfaceImplementation(
                provider_identity=old,
                provider_artifact_digest="sha256:" + "4" * 64,
                implementation_digest="sha256:" + "5" * 64,
                newest=False,
                newest_builds=("Provider:demo/" + "b" * 64,),
            )
        ],
    )
    assert entry.effect_class == "pure"
    assert entry.identity == "ProviderInterface:demo.interface/v1"
    # The latest build of demo implements another contract, so it is absent
    # from this older registration's inventory. An exact old build is valid.
    assert ProviderBinding.from_interface(entry, provider=old).provider == old
    with pytest.raises(ProviderSelectionError) as refused:
        ProviderBinding.from_interface(entry, provider="demo")
    assert refused.value.newest_builds == ("Provider:demo/" + "b" * 64,)
    assert refused.value.fitting_builds == (old,)
    assert old in str(refused.value)
    assert "Provider:demo/" + "b" * 64 in str(refused.value)
    # A same-generation tie also refuses when just one tied build fits.
    tied = entry.model_copy(
        update={
            "providers": [
                entry.providers[0].model_copy(
                    update={"newest_builds": (old, "Provider:demo/" + "b" * 64)}
                )
            ]
        }
    )
    with pytest.raises(ProviderSelectionError):
        ProviderBinding.from_interface(tied, provider="demo")
