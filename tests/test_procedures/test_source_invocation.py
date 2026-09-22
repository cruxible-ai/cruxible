"""Exact child calls and control-flow proof for successful child values."""

import pytest

from cruxible_client.contracts.procedures.source_compiler import SourceCompileError
from cruxible_client.contracts.procedures.source_program import SourceProcedureBinding
from tests.test_procedures.test_source_compiler import INPUT, OUTPUT, compile


def binding(**changes):
    return SourceProcedureBinding(
        name="child",
        version="sha256:" + "a" * 64,
        input=INPUT.schema_,
        output=OUTPUT.schema_,
        **changes,
    )


def program(after):
    return (
        """def example(request, bindings):
    observed = invoke(
        bindings.child,
        input=bindings.child.input(choice=request.choice, count=request.count),
    )
"""
        + after
    )


def test_exact_child_binding_requires_no_authored_hash_and_retains_success_proof():
    compiled = compile(
        program("""    if not observed.succeeded:
        return halt("Child did not succeed")
    return Output.value(value=observed.value.value)
"""),
        bindings={"child": binding()},
    )
    node = compiled.definition.nodes[0]
    assert node.kind == "invoke"
    assert node.procedure.target.name == "child"
    assert "sha256:" not in compiled.definition.source.text
    assert compiled.definition.nodes[-1].fields == {"value": f"$steps.{node.as_}.value.value"}


def test_short_circuit_and_require_prove_child_success():
    compile(
        program("""    require(
        observed.succeeded and observed.value.value == "ready",
        code="child", message="not ready",
    )
    return Output.value(value=observed.value.value)
"""),
        bindings={"child": binding()},
    )


@pytest.mark.parametrize(
    "after,code",
    [
        ("    return Output.value(value=observed.value.value)\n", "child_result_unavailable"),
        (
            """    if observed.succeeded:
        marker = "yes"
    else:
        marker = "no"
    return Output.value(value=observed.value.value)
""",
            "child_result_unavailable",
        ),
        (
            """    require(observed.succeeded, code="child", message="failed")
    evidence = observed.terminal.capture
    return Output.value(value="ready")
""",
            "child_capture_unavailable",
        ),
    ],
)
def test_child_data_is_never_available_without_the_required_proof(after, code):
    with pytest.raises(SourceCompileError) as caught:
        compile(program(after), bindings={"child": binding()})
    assert caught.value.diagnostic.code == "playbill.source." + code
