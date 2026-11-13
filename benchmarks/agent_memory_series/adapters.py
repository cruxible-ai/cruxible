"""Agent execution is an explicit extension point, never an implicit CLI call."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .config import Arm, Model, RunawayGuard


class Usage(Model):
    """Adapter-reported usage. Measured, never capped; unknown stays None."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    turns: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    # All model requests, including subagents'; subagent_calls counts spawned subagents.
    model_calls: int | None = Field(default=None, ge=0)
    subagent_calls: int | None = Field(default=None, ge=0)
    # Adapter-reported compute, distinct from harness wall time / service latency.
    compute_seconds: float | None = Field(default=None, ge=0)

    @property
    def tokens(self) -> int | None:
        """Uncached input plus output tokens."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int | None:
        """Input, output and cache tokens; unknown unless every counter is reported.

        Known absence of cache use must be reported as an explicit zero.
        """
        parts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )
        return None if any(part is None for part in parts) else sum(p or 0 for p in parts)

    @classmethod
    def none_used(cls) -> Usage:
        """Known zero usage: harness work with no model involved."""
        return cls(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            turns=0,
            cost_usd=0,
            model_calls=0,
            subagent_calls=0,
            compute_seconds=0,
        )


class MemoryReport(Model):
    claims: int | None = Field(default=None, ge=0)
    copy_in_assessed_claims: int | None = Field(default=None, ge=0)
    copy_in_failed_claims: int | None = Field(default=None, ge=0)
    source: str = "not_reported"

    @model_validator(mode="after")
    def counts(self) -> MemoryReport:
        assessed, failed = self.copy_in_assessed_claims, self.copy_in_failed_claims
        if (assessed is None) != (failed is None):
            raise ValueError("report both assessed and failed copy-in counts, or neither")
        if assessed is not None:
            assert failed is not None
            if failed > assessed or self.claims is not None and assessed > self.claims:
                raise ValueError("failed <= assessed <= total Claims is required")
        return self


class SurfaceAction(Model):
    """One kind of Cruxible action, by surface: MCP tool, CLI subcommand or SDK call."""

    surface: Literal["mcp", "cli", "sdk"]
    name: str = Field(min_length=1)
    calls: int = Field(default=1, ge=1)


class Reply(Model):
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    memory: MemoryReport = Field(default_factory=MemoryReport)
    first_useful_state: bool = False
    upkeep_compute_seconds: float | None = Field(default=None, ge=0)
    # Set when the adapter itself stopped the invocation at the runaway guard.
    stopped_by_guard: Literal["wall_clock", "cost"] | None = None
    # The model the host actually reported, and every Cruxible action by surface.
    model_id: str | None = None
    actions: tuple[SurfaceAction, ...] = ()


class Invocation(Model):
    phase: Literal["setup", "session", "question"]
    session_index: int = Field(ge=0)
    prompt: str
    question_id: str | None = None
    question_kind: str | None = None
    setup_step: int | None = Field(default=None, ge=0)
    # No review context, other arm's output, or conversation history.

    @model_validator(mode="after")
    def setup_shape(self) -> Invocation:
        if (self.phase == "setup") != (self.setup_step is not None):
            raise ValueError("only setup invocations name a setup step")
        return self


@dataclass(frozen=True)
class Context:
    arm: Arm
    repetition: int
    model: str
    guard: RunawayGuard
    workspace: Path
    state_root: Path | None
    run_root: Path
    keys_dir: Path
    feeds_dir: Path
    # Installed skill/plugin for this arm; a live adapter copies it into each
    # invocation's fresh client configuration directory.
    tooling_dir: Path
    environment: dict[str, str]


class AgentAdapter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def dry_run(self) -> bool: ...

    def versions(self, context: Context) -> dict[str, str]:
        """Per arm/repetition: host client version, model ID, skill commit, plugin/kit versions."""
        ...

    def run(self, context: Context, invocation: Invocation) -> Reply: ...

    def close(self, context: Context) -> None: ...


class ScriptedTurn(Model):
    phase: Literal["setup", "session", "question"]
    session_index: int = Field(ge=0)
    question_id: str | None = None
    setup_step: int | None = None
    arm: Literal["A", "B", "C"] | None = None
    reply: Reply


class ScriptedAdapter:
    """Offline/no-op by default; supplied turns or a callable drive fixture experiments."""

    name = "scripted"
    dry_run = True

    def __init__(
        self,
        turns: tuple[ScriptedTurn, ...] = (),
        respond: Callable[[Context, Invocation], Reply] | None = None,
        cleanup: Callable[[Context], None] | None = None,
    ) -> None:
        self.respond = respond
        self.cleanup = cleanup
        self.turns: dict[tuple[str | None, str, int, str | None, int | None], Reply] = {}
        for turn in turns:
            key = (turn.arm, turn.phase, turn.session_index, turn.question_id, turn.setup_step)
            if key in self.turns:
                raise ValueError(f"duplicate scripted turn: {key}")
            self.turns[key] = turn.reply

    def versions(self, context: Context) -> dict[str, str]:
        return {"adapter": "scripted", "model": context.model}

    def run(self, context: Context, invocation: Invocation) -> Reply:
        if self.respond is not None:
            return self.respond(context, invocation)
        for arm in (context.arm.id, None):
            key = (
                arm,
                invocation.phase,
                invocation.session_index,
                invocation.question_id,
                invocation.setup_step,
            )
            if key in self.turns:
                return self.turns[key]
        return Reply(text="[scripted dry run: no model was called]", usage=Usage.none_used())

    def close(self, context: Context) -> None:
        if self.cleanup is not None:
            self.cleanup(context)


FORBIDDEN_FLAGS = ("--bare", "--continue", "--resume", "-c", "-r")


class CLIInvocation(Model):
    """The live Claude Code recipe (maintainer decision 2026-11-09), not yet executed.

    Not --bare (it skips subagents and MCP). A fresh client configuration
    directory per invocation (Claude Code auto memory has no off switch and is
    keyed by that directory), holding only this arm's skill (A) or plugin (B).
    Each invocation is a fresh `claude -p`, never --continue/--resume.
    """

    client: Literal["claude-code", "codex"]
    argv: tuple[str, ...]
    model: str
    guard: RunawayGuard
    cwd: Path
    config_dir: Path
    environment: dict[str, str]
    arm: Arm
    invocation: Invocation
    subagents_allowed: bool = True
    remaining: tuple[str, ...] = (
        "Copy the arm's tooling_dir (skill or plugin) into config_dir before launch",
        "B: register the MCP server with the arm's catalog; cruxible CLI and SDK on PATH",
        "Parse stream-json: tokens, cache, cost, turns, model calls, subagents, model ID",
        "Log every Cruxible action by surface (mcp__cruxible__* tool, `cruxible` Bash "
        "subcommand, cruxible_client call) into Reply.actions",
        "Stop the process at the runaway guard and report stopped_by_guard",
        "Collect served Claim counts/copy-in assessments without reading projection databases",
        "Stop any daemon started under this run's state root in close()",
    )

    @model_validator(mode="after")
    def fresh_conversation(self) -> CLIInvocation:
        if any(flag in FORBIDDEN_FLAGS for flag in self.argv):
            raise ValueError("each invocation is a fresh session: no --bare/--continue/--resume")
        return self


class CLIAdapterStub:
    """A reviewable invocation contract, deliberately incapable of invoking a real model."""

    dry_run = False

    def __init__(self, client: Literal["claude-code", "codex"]) -> None:
        self.name: Literal["claude-code", "codex"] = client

    def plan(self, context: Context, invocation: Invocation) -> CLIInvocation:
        label = invocation.question_id or (
            f"step-{invocation.setup_step}" if invocation.setup_step is not None else "audit"
        )
        config_dir = (
            context.run_root
            / "client-config"
            / f"{invocation.phase}-{invocation.session_index:02d}-{label}"
        )
        argv = (
            ("claude", "-p", invocation.prompt, "--model", context.model)
            + ("--output-format", "stream-json", "--verbose")
            if self.name == "claude-code"
            else ("codex", "exec", "--model", context.model, invocation.prompt)
        )
        return CLIInvocation(
            client=self.name,
            argv=argv,
            model=context.model,
            guard=context.guard,
            cwd=context.workspace,
            config_dir=config_dir,
            environment={**context.environment, "CLAUDE_CONFIG_DIR": str(config_dir)},
            arm=context.arm,
            invocation=invocation,
        )

    def versions(self, context: Context) -> dict[str, str]:
        return {"client": self.name, "model": context.model}

    def run(self, context: Context, invocation: Invocation) -> Reply:
        raise NotImplementedError(
            f"{self.name} live execution is a stub; "
            "inspect CLIAdapterStub.plan() and implement it first"
        )

    def close(self, context: Context) -> None:
        pass  # The stub never starts a process.
