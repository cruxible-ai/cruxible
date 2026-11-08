"""Fresh Git clones, identical scripted changes and isolated per-run state."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from .adapters import Context, MemoryReport
from .config import Arm, BenchmarkConfig, Command, Patch
from .results import SizeMetrics, SourceSnapshot


def execute(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = 60,
    content: bytes | None = None,
) -> bytes:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        input=content,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {list(argv)!r}\n"
            + result.stderr.decode(errors="replace")
        )
    return result.stdout


def isolated_environment(root: Path, workspace: Path) -> dict[str, str]:
    # No remembered Cruxible socket, instance, credential, CLI context or network
    # endpoint is inherited. No host HOME variable is changed.
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")
        if key in os.environ
    }
    for name in ("tmp", "config", "cache", "state"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CRUXIBLE_STATE_ROOT": str(root / "state"),
            "CRUXIBLE_SERVER_SOCKET": str(root / "state" / "run" / "daemon.sock"),
            "CRUXIBLE_MCP_WORKSPACE_ROOT": str(workspace),
            "CRUXIBLE_CLI_CONTEXT_PATH": str(root / "client-context.json"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "TMPDIR": str(root / "tmp"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def prepare_context(config: BenchmarkConfig, arm: Arm, repetition: int, root: Path) -> Context:
    if config.target is None:
        raise ValueError("target is an unfilled slot; supply git_url and a pinned base_commit")
    root.mkdir(parents=True)
    workspace = root / "workspace"
    env = isolated_environment(root, workspace)
    execute(
        [
            "git",
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            "--",
            config.target.git_url,
            str(workspace),
        ],
        cwd=root,
        environment=env,
    )
    execute(
        ["git", "checkout", "--detach", config.target.base_commit], cwd=workspace, environment=env
    )
    actual = execute(["git", "rev-parse", "HEAD"], cwd=workspace, environment=env).decode().strip()
    if actual != config.target.base_commit:
        raise ValueError("checked-out target does not match the pinned base commit")
    context = Context(
        arm=arm,
        repetition=repetition,
        model=config.model,
        budget=config.budget,
        workspace=workspace,
        state_root=root / "state" if arm.id != "A" else None,
        run_root=root,
        environment=env,
    )
    tracked = (
        execute(["git", "ls-files", "-z"], cwd=workspace, environment=env).decode().split("\0")
    )
    if any(name and is_memory(name, arm) for name in tracked):
        raise ValueError("configured memory paths overlap the target's tracked source")
    return context


def is_memory(name: str, arm: Arm) -> bool:
    path = Path(name)
    return any(path == Path(root) or Path(root) in path.parents for root in arm.memory_paths)


def source_snapshot(context: Context) -> SourceSnapshot:
    """Offline measurement/validity guard, intentionally O(source files + bytes).

    This traversal is harness instrumentation, outside agent usage and product
    service paths. Untracked, non-ignored new source files are included.
    """
    names = (
        execute(
            ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"],
            cwd=context.workspace,
            environment=context.environment,
        )
        .decode()
        .split("\0")
    )
    digest = hashlib.sha256()
    size = count = 0
    for name in sorted(set(names)):
        if not name or is_memory(name, context.arm):
            continue
        path = context.workspace / name
        if path.is_symlink():
            data = os.readlink(path).encode()
            mode = b"symlink"
        elif path.is_file():
            data = path.read_bytes()
            mode = b"executable" if path.stat().st_mode & 0o111 else b"file"
        elif not path.exists():
            continue  # a scripted deletion, even before its next Git commit
        else:
            raise ValueError(f"unsupported source entry (for example a submodule): {name}")
        digest.update(name.encode() + b"\0" + mode + b"\0" + hashlib.sha256(data).digest())
        size += len(data)
        count += 1
    return SourceSnapshot(digest="sha256:" + digest.hexdigest(), bytes=size, files=count)


def apply_changes(
    context: Context, changes: tuple[Patch | Command, ...], directory: Path
) -> list[dict[str, object]]:
    logs = []
    for change in changes:
        start = time.perf_counter()
        if isinstance(change, Patch):
            if change.path is not None:
                content = (directory / change.path).read_bytes()
            else:
                assert change.text is not None
                content = change.text.encode()
            execute(
                ["git", "apply", "--check", "-"],
                cwd=context.workspace,
                environment=context.environment,
                content=content,
            )
            output = execute(
                ["git", "apply", "-"],
                cwd=context.workspace,
                environment=context.environment,
                content=content,
            )
        else:
            output = execute(
                change.argv,
                cwd=context.workspace,
                environment=context.environment,
                timeout=change.timeout_seconds,
            )
        logs.append(
            {
                "change": change.model_dump(mode="json"),
                "stdout": output.decode(errors="replace"),
                "wall_seconds": time.perf_counter() - start,
            }
        )
    return logs


def size_metrics(context: Context, source: SourceSnapshot, report: MemoryReport) -> SizeMetrics:
    """One explicit size measurement per session, O(retained memory files + bytes metadata)."""
    started = time.perf_counter()
    roots = [context.workspace / name for name in context.arm.memory_paths]
    if context.state_root is not None:
        roots.append(context.state_root)
    seen: set[Path] = set()
    state_bytes = 0
    for root in roots:
        if root.is_symlink():
            continue
        for path in root.rglob("*") if root.is_dir() else (root,):
            if not path.is_symlink() and path.is_file() and path not in seen:
                seen.add(path)
                state_bytes += path.stat().st_size
    assessed = report.copy_in_assessed_claims
    failed = report.copy_in_failed_claims
    return SizeMetrics(
        state_bytes=state_bytes,
        state_to_source_ratio=state_bytes / source.bytes if source.bytes else None,
        claims_per_source_file=report.claims / source.files
        if report.claims is not None and source.files
        else None,
        copy_in_failure_share=failed / assessed if assessed and failed is not None else None,
        report=report,
        measurement_seconds=time.perf_counter() - started,
    )
