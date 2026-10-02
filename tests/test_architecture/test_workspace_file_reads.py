"""Floor pipeline file reads stay behind the one opened-descriptor reader.

This smoke check prohibits direct stream/Path reads in the four callers; the
reader's FIFO, swap and byte-budget tests enforce its runtime behavior.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "packages/cruxible-client/src/cruxible_client"
MODULES = (
    "authoring/workspace.py",
    "authoring/selectors.py",
    "contracts/declared_blocks.py",
    "authoring/floor_apply.py",
    "_safe_files.py",
)


def _unsafe_reads(source: str, *, reader_module: bool = False) -> list[int]:
    tree = ast.parse(source)
    allowed = {
        descendant
        for node in ast.walk(tree)
        if reader_module and isinstance(node, ast.FunctionDef) and node.name == "read_regular_file"
        for descendant in ast.walk(node)
    }
    os_names = {"os"}
    open_names = {"open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "os"
            )
        if isinstance(node, ast.ImportFrom) and node.module in {"builtins", "io"}:
            open_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "open"
            )
    bad = []
    for node in ast.walk(tree):
        if node in allowed or not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in open_names:
            bad.append(node.lineno)
        elif isinstance(function, ast.Attribute):
            if function.attr in {"read", "read_text", "read_bytes"}:
                bad.append(node.lineno)
            elif function.attr == "open" and not (
                isinstance(function.value, ast.Name) and function.value.id in os_names
            ):
                bad.append(node.lineno)
    return bad


@pytest.mark.parametrize("module", MODULES)
def test_floor_pipeline_reads_use_the_safe_reader(module):
    assert not _unsafe_reads(
        (ROOT / module).read_text(), reader_module=module == "_safe_files.py"
    ), module


@pytest.mark.parametrize(
    "source",
    [
        "open(path, 'rb').read()",
        "path.open('rb')",
        "path.read_bytes()",
        "path.read_text(encoding='utf-8')",
        "from builtins import open as load; load(path)",
        "import io; io.open(path)",
        "def _read_at(path):\n    return path.read_bytes()",
        "import os; handle = os.open(path, 0); content = os.read(handle, 3)",
    ],
)
def test_direct_workspace_reads_are_rejected(source):
    assert _unsafe_reads(source)


def test_shared_reader_calls_and_descriptor_writes_are_allowed():
    assert not _unsafe_reads(
        "from cruxible_client._safe_files import read_regular_file\n"
        "content = read_regular_file(path, max_bytes=3)\n"
        "import os\n"
        "handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)\n"
        "os.fdopen(handle, 'wb').write(content)"
    )


def test_only_the_reader_function_itself_is_allowlisted():
    assert _unsafe_reads(
        "def another_helper(path):\n    return path.read_bytes()", reader_module=True
    )
    assert _unsafe_reads("def read_regular_file(path):\n    return path.read_bytes()")
