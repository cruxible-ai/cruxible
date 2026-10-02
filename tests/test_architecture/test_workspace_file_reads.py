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


_DIRECTORY_FLAGS = frozenset({"O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"})
_CREATE_FLAGS = frozenset({"O_WRONLY", "O_CREAT", "O_EXCL", "O_NOFOLLOW", "O_CLOEXEC"})


def _audited_open(node: ast.Call, tree: ast.Module, os_names: set[str]) -> bool:
    bindings = {}
    imported = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
        if (
            isinstance(statement, ast.ImportFrom)
            and statement.module == "cruxible_client.authoring.floor_apply"
        ):
            for alias in statement.names:
                if alias.name in {"_DIRECTORY", "_CREATE"}:
                    imported[alias.asname or alias.name] = (
                        _DIRECTORY_FLAGS if alias.name == "_DIRECTORY" else _CREATE_FLAGS
                    )

    def flags(expression, resolving=frozenset()):
        if isinstance(expression, ast.Name) and expression.id not in resolving:
            if expression.id in bindings:
                return flags(bindings[expression.id], resolving | {expression.id})
            return imported.get(expression.id)
        if (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id in os_names
            and expression.attr.startswith("O_")
        ):
            return frozenset({expression.attr})
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.BitOr):
            left, right = flags(expression.left, resolving), flags(expression.right, resolving)
            return None if left is None or right is None else left | right
        if isinstance(expression, ast.Constant) and expression.value == 0:
            return frozenset()
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Name)
            and expression.func.id == "getattr"
            and len(expression.args) == 3
            and isinstance(expression.args[0], ast.Name)
            and expression.args[0].id in os_names
            and isinstance(expression.args[1], ast.Constant)
            and expression.args[1].value == "O_CLOEXEC"
            and isinstance(expression.args[2], ast.Constant)
            and expression.args[2].value == 0
        ):
            return frozenset({"O_CLOEXEC"})
        return None

    expression = (
        node.args[1]
        if len(node.args) >= 2
        else next((keyword.value for keyword in node.keywords if keyword.arg == "flags"), None)
    )
    value = flags(expression)
    return value is not None and (
        {"O_DIRECTORY", "O_NOFOLLOW"} <= value <= _DIRECTORY_FLAGS
        or {"O_CREAT", "O_EXCL"} <= value <= _CREATE_FLAGS
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
    raw_open_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "os"
            )
        if isinstance(node, ast.ImportFrom) and node.module in {"builtins", "io"}:
            open_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "open"
            )
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            raw_open_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "open"
            )
    bad = []
    for node in ast.walk(tree):
        if node in allowed or not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in raw_open_names:
            if not _audited_open(node, tree, os_names):
                bad.append(node.lineno)
        elif isinstance(function, ast.Name) and function.id in open_names:
            bad.append(node.lineno)
        elif isinstance(function, ast.Attribute):
            if function.attr in {"read", "read_text", "read_bytes"}:
                bad.append(node.lineno)
            elif function.attr == "open":
                if not (
                    isinstance(function.value, ast.Name)
                    and function.value.id in os_names
                    and _audited_open(node, tree, os_names)
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


@pytest.mark.parametrize(
    "source",
    [
        "import os; os.open(path, os.O_RDONLY)",
        "import os; os.open(path, os.O_DIRECTORY)",
        "import os; os.open(path, os.O_NOFOLLOW)",
        "import os; os.open(path, os.O_WRONLY | os.O_CREAT)",
        "import os; os.open(path, flags=0)",
        "import os; _DIRECTORY = os.O_RDONLY; os.open(path, _DIRECTORY)",
        "from os import open as raw; raw(path, 0)",
    ],
)
def test_unaudited_raw_opens_are_rejected(source):
    assert _unsafe_reads(source)


@pytest.mark.parametrize(
    "source",
    [
        "import os; os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)",
        "import os; os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)",
        "import os; _DIR = os.O_DIRECTORY | os.O_NOFOLLOW; os.open(path, _DIR)",
        "import os; from cruxible_client.authoring.floor_apply import _DIRECTORY as DIR; "
        "os.open(path, DIR)",
        "import os; from os import open as raw; raw(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)",
    ],
)
def test_audited_raw_opens_are_allowed(source):
    assert not _unsafe_reads(source)
