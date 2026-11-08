"""Agent and model-grader execution are explicit extension points, never implicit CLI calls."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .config import Arm, Budget, Model


class Usage(Model):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    turns: int | None = Field(default=None, ge=0)
    # Adapter-reported compute, distinct from harness wall time / service latency.
    compute_seconds: float | None = Field(default=None, ge=0)

    @property
    def tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


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


class Reply(Model):
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    memory: MemoryReport = Field(default_factory=MemoryReport)
    first_useful_state: bool = False
    upkeep_compute_seconds: float | None = Field(default=None, ge=0)


class Invocation(Model):
    phase: Literal["setup", "session", "question"]
    session_index: int = Field(ge=0)
    prompt: str
    question_id: str | None = None
    question_kind: str | None = None
    # No rubric, expected answers, other arm's output, or conversation history.


@dataclass(frozen=True)
class Context:
    arm: Arm
    repetition: int
    model: str
    budget: Budget
    workspace: Path
    state_root: Path | None
    run_root: Path
    environment: dict[str, str]


class AgentAdapter(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def dry_run(self) -> bool: ...

    def run(self, context: Context, invocation: Invocation) -> Reply: ...

    def close(self, context: Context) -> None: ...


class ScriptedTurn(Model):
    phase: Literal["setup", "session", "question"]
    session_index: int = Field(ge=0)
    question_id: str | None = None
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
        self.turns: dict[tuple[str | None, str, int, str | None], Reply] = {}
        for turn in turns:
            key = (turn.arm, turn.phase, turn.session_index, turn.question_id)
            if key in self.turns:
                raise ValueError(f"duplicate scripted turn: {key}")
            self.turns[key] = turn.reply

    def run(self, context: Context, invocation: Invocation) -> Reply:
        if self.respond is not None:
            return self.respond(context, invocation)
        for arm in (context.arm.id, None):
            key = (arm, invocation.phase, invocation.session_index, invocation.question_id)
            if key in self.turns:
                return self.turns[key]
        return Reply(
            text="[scripted dry run: no model was called]",
            usage=Usage(input_tokens=0, output_tokens=0, turns=0, compute_seconds=0),
        )

    def close(self, context: Context) -> None:
        if self.cleanup is not None:
            self.cleanup(context)


class CLIInvocation(Model):
    client: Literal["claude-code", "codex"]
    executable: str
    model: str
    budget: Budget
    cwd: Path
    environment: dict[str, str]
    arm: Arm
    invocation: Invocation
    remaining: tuple[str, ...] = (
        "Implement host-specific CLI flags and shipped skill/plugin activation",
        "Bootstrap B through the shipped plugin/general adopt-kit skill; C starts empty",
        "Enforce budgets and parse actual input/output token and turn usage",
        "Use a fresh conversation per invocation; retain only the configured external memory",
        "Collect served Claim counts/copy-in assessments without reading projection databases",
        "Stop any daemon started under this run's state root in a finally block",
    )


class CLIAdapterStub:
    """A reviewable invocation contract, deliberately incapable of invoking a real model."""

    dry_run = False

    def __init__(self, client: Literal["claude-code", "codex"]) -> None:
        self.name: Literal["claude-code", "codex"] = client

    def plan(self, context: Context, invocation: Invocation) -> CLIInvocation:
        return CLIInvocation(
            client=self.name,
            executable="claude" if self.name == "claude-code" else "codex",
            model=context.model,
            budget=context.budget,
            cwd=context.workspace,
            environment=context.environment,
            arm=context.arm,
            invocation=invocation,
        )

    def run(self, context: Context, invocation: Invocation) -> Reply:
        raise NotImplementedError(
            f"{self.name} live execution is a stub; "
            "inspect CLIAdapterStub.plan() and implement it first"
        )

    def close(self, context: Context) -> None:
        pass  # The stub never starts a process.
