"""Fresh Git clones, identical scripted changes and isolated per-run state."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from .adapters import Context, MemoryReport
from .config import Arm, BenchmarkConfig, Command, Feed, Patch, SetupCommand
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
    for name in ("tmp", "config", "cache", "state", "feeds", "tooling"):
        (root / name).mkdir(parents=True, exist_ok=True)
    # Approval keys live outside the workspace and the state root (custody rule).
    (root / "keys").mkdir(mode=0o700, exist_ok=True)
    env.update(
        {
            "CRUXIBLE_STATE_ROOT": str(root / "state"),
            "CRUXIBLE_SERVER_SOCKET": str(root / "state" / "run" / "daemon.sock"),
            "CRUXIBLE_MCP_WORKSPACE_ROOT": str(workspace),
            "CRUXIBLE_CLI_CONTEXT_PATH": str(root / "client-context.json"),
            "AGENT_MEMORY_FEEDS_DIR": str(root / "feeds"),
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
    paths = placeholder_paths(root)
    # Arm host settings (for example the MCP server's key directory) come last;
    # the config refuses isolation variables, so they cannot be overridden.
    env.update({key: expand_with(value, paths) for key, value in arm.environment.items()})
    if arm.cruxible is not None:
        env["CRUXIBLE_MCP_PROFILE"] = "full"
        if arm.cruxible.mcp_tools is not None:
            env["CRUXIBLE_MCP_TOOLS"] = ",".join(arm.cruxible.mcp_tools)
        if arm.cruxible.bin_dir is not None:
            bin_dir = expand_with(arm.cruxible.bin_dir, paths)
            env["PATH"] = os.pathsep.join(filter(None, (bin_dir, env.get("PATH"))))
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
    strip_host_instructions(workspace, env, config.strip_from_target)
    context = Context(
        arm=arm,
        repetition=repetition,
        model=config.model,
        guard=config.guard,
        workspace=workspace,
        state_root=root / "state" if arm.id != "A" else None,
        run_root=root,
        keys_dir=root / "keys",
        feeds_dir=root / "feeds",
        tooling_dir=root / "tooling",
        environment=env,
    )
    tracked = (
        execute(["git", "ls-files", "-z"], cwd=workspace, environment=env).decode().split("\0")
    )
    if any(name and is_memory(name, arm) and (workspace / name).exists() for name in tracked):
        raise ValueError("configured memory/tooling paths overlap the target's tracked source")
    return context


def strip_host_instructions(workspace: Path, env: dict[str, str], names: tuple[str, ...]) -> None:
    """Delete tracked files under any path component named in `names`, identically per arm.

    Deletion is in the working tree only, so the base snapshot (taken after
    this) excludes them and every arm starts from the same stripped source.
    """
    if not names:
        return
    tracked = execute(["git", "ls-files", "-z"], cwd=workspace, environment=env).decode()
    for name in filter(None, tracked.split("\0")):
        if set(Path(name).parts) & set(names):
            (workspace / name).unlink(missing_ok=True)


def expand_with(value: str, paths: dict[str, Path]) -> str:
    for placeholder, path in paths.items():
        value = value.replace(placeholder, str(path))
    return value


def placeholder_paths(root: Path) -> dict[str, Path]:
    """The placeholders setup argv and arm environment values may name, under one run root."""
    return {
        "{run_root}": root,
        "{workspace}": root / "workspace",
        "{state_root}": root / "state",
        "{keys}": root / "keys",
        "{feeds}": root / "feeds",
        "{tooling}": root / "tooling",
        "{socket}": root / "state" / "run" / "daemon.sock",
        "{python}": Path(sys.executable),
    }


def expand(value: str, context: Context) -> str:
    """Substitute this run's isolated paths for config placeholders."""
    return expand_with(value, placeholder_paths(context.run_root))


def is_memory(name: str, arm: Arm) -> bool:
    """Memory and installed tooling are both outside source identity."""
    path = Path(name)
    return any(
        path == Path(root) or Path(root) in path.parents
        for root in (*arm.memory_paths, *arm.tooling_paths)
    )


def run_setup_command(
    context: Context, step: SetupCommand
) -> tuple[dict[str, object], subprocess.Popen[bytes] | None]:
    """Run one off-the-clock setup command; a background one is returned to stop later."""
    argv = [expand(part, context) for part in step.argv]
    start = time.perf_counter()
    if not step.background:
        output = execute(
            argv,
            cwd=context.workspace,
            environment=context.environment,
            timeout=step.timeout_seconds,
        )
        log: dict[str, object] = {"stdout": output.decode(errors="replace")}
        process = None
    else:
        logs = context.run_root / "setup-logs"
        logs.mkdir(exist_ok=True)
        with (logs / f"background-{time.monotonic_ns()}.log").open("wb") as handle:
            process = subprocess.Popen(
                argv,
                cwd=context.workspace,
                env=context.environment,
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        log = {"pid": process.pid, "log": handle.name}
        if step.wait_for is not None:
            ready = Path(expand(step.wait_for, context))
            deadline = time.monotonic() + step.timeout_seconds
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    stop_process(process)
                    raise RuntimeError(f"background setup command did not produce {ready}")
                time.sleep(0.1)
    log.update(
        {
            "step": step.model_dump(mode="json"),
            "argv": argv,
            "wall_seconds": time.perf_counter() - start,
        }
    )
    return log, process


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


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


def feeds_snapshot(context: Context) -> str:
    """Digest of the frozen advisory snapshots every arm sees at this session."""
    digest = hashlib.sha256()
    for path in sorted(context.feeds_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            name = path.relative_to(context.feeds_dir).as_posix()
            digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


def apply_changes(
    context: Context, changes: tuple[Patch | Command | Feed, ...], directory: Path
) -> list[dict[str, object]]:
    logs = []
    for change in changes:
        start = time.perf_counter()
        if isinstance(change, Feed):
            data = (
                (directory / change.path).read_bytes()
                if change.path is not None
                else (change.text or "").encode()
            )
            destination = context.feeds_dir / change.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            output = f"wrote {len(data)} bytes to {destination}".encode()
        elif isinstance(change, Patch):
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
    roots = [context.workspace / name for name in context.arm.memory_paths]  # not tooling
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
