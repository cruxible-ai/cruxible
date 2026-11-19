"""Frozen, typed inputs; a target is deliberately optional until launch."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")]

# Variables the harness owns for isolation; an arm cannot override them.
ISOLATION_VARIABLES = frozenset(
    {
        "CRUXIBLE_STATE_ROOT",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_MCP_WORKSPACE_ROOT",
        "CRUXIBLE_CLI_CONTEXT_PATH",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "HOME",
        "PATH",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_TERMINAL_PROMPT",
        "AGENT_MEMORY_FEEDS_DIR",
        "CLAUDE_CONFIG_DIR",
        "CRUXIBLE_MCP_PROFILE",
        "CRUXIBLE_MCP_TOOLS",
        "CRUXIBLE_MCP_TOOL_ALLOWLIST",
    }
)
# Host-agent instructions/memory in the target are removed identically for every
# arm (any path component with one of these names), so no arm inherits them.
DEFAULT_STRIP = ("CLAUDE.md", "CLAUDE.local.md", ".claude", "AGENTS.md", ".codex")


class Target(Model):
    git_url: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RunawayGuard(Model):
    """A ceiling far above normal use, not a budget.

    Usage is measured, never capped. A live adapter should stop an invocation
    that crosses a ceiling; the runner also checks after each invocation.
    Either way the hit is recorded as an outcome and the run continues.
    """

    max_seconds_per_invocation: float = Field(default=4 * 3600, gt=0)
    max_cost_usd_per_invocation: float | None = Field(default=100.0, gt=0)


class Integration(Model):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    # Host installation/activation is a setup step or a live adapter's job.
    path: Path | None = None


class SetupCommand(Model):
    """A deterministic setup program the harness runs, off the clock, no model."""

    kind: Literal["command"] = "command"
    description: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=300, gt=0)
    # A long-running helper (for example a scratch daemon), stopped when the
    # arm/repetition closes. `wait_for` names a path that must appear first.
    background: bool = False
    wait_for: str | None = None

    @model_validator(mode="after")
    def background_shape(self) -> SetupCommand:
        if self.wait_for is not None and not self.background:
            raise ValueError("wait_for applies to background setup commands")
        return self


class SetupAgent(Model):
    """An agent invocation during setup, off the clock; its usage is recorded as setup."""

    kind: Literal["agent"] = "agent"
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


SetupStep = Annotated[SetupCommand | SetupAgent, Field(discriminator="kind")]


class CruxibleSurfaces(Model):
    """Every Cruxible surface the B/C agent may choose from (broader than the plugin default).

    `mcp_tools: null` registers the full MCP catalog (CRUXIBLE_MCP_PROFILE=full).
    A tuple is an explicit allowlist, the fallback if the pilot shows the host
    loads the full catalog eagerly and that would skew token counts.
    `bin_dir` is prepended to PATH: it holds the matching `cruxible` CLI and a
    Python in which the SDK (`cruxible_client`) is importable.
    """

    mcp_tools: tuple[str, ...] | None = None
    bin_dir: str | None = None

    @model_validator(mode="after")
    def tools_shape(self) -> CruxibleSurfaces:
        if self.mcp_tools is not None:
            if not self.mcp_tools or len(set(self.mcp_tools)) != len(self.mcp_tools):
                raise ValueError("an MCP allowlist must be non-empty and unique")
            if any(not re.fullmatch(r"cruxible_[a-z_]+", name) for name in self.mcp_tools):
                raise ValueError("MCP allowlist entries are cruxible_* tool names")
        return self


def _relative(name: str, label: str) -> None:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
        raise ValueError(f"{label} must be relative paths outside .git")
    if path == Path("."):
        raise ValueError(f"the whole source workspace cannot be a {label} entry")


class Arm(Model):
    id: Literal["A", "B", "C"]
    method: Literal["cloudflare-skill", "cruxible-security-kit", "cruxible-no-kit"]
    skill: Integration | None = None
    plugin: Integration | None = None
    kit: Integration | None = None
    memory_paths: tuple[str, ...] = ()
    # Installed host tooling inside the workspace (for example a project-level
    # skill directory): excluded from source identity and from memory size.
    tooling_paths: tuple[str, ...] = ()
    # B/C only: which Cruxible surfaces the agent gets (MCP, CLI, SDK).
    cruxible: CruxibleSurfaces | None = None
    # Off-the-clock setup: what a normal user does before the first session.
    setup: tuple[SetupStep, ...] = ()
    # Host settings a normal setup gives the agent's tools (for example the MCP
    # server's CRUXIBLE_MCP_KEY_DIR). Values may use the placeholders of
    # workspace.placeholder_paths ({run_root}, {workspace}, {state_root}, {keys}, ...).
    environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent(self) -> Arm:
        expected = {"A": "cloudflare-skill", "B": "cruxible-security-kit", "C": "cruxible-no-kit"}
        if self.method != expected[self.id]:
            raise ValueError("arm id and method disagree")
        if self.id == "A" and (self.skill is None or self.plugin or self.kit):
            raise ValueError("A requires only the Cloudflare skill integration")
        if self.id != "A" and (self.plugin is None or self.skill is not None):
            raise ValueError("B/C require the shipped Cruxible plugin, without extra skills")
        if (self.id == "B") != (self.kit is not None):
            raise ValueError("only B requires a security kit integration")
        if self.id == "A" and self.cruxible is not None:
            raise ValueError("A gets no Cruxible surfaces")
        for name in self.memory_paths:
            _relative(name, "memory paths")
        for name in self.tooling_paths:
            _relative(name, "tooling paths")
        for key in self.environment:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
                raise ValueError(f"invalid environment variable name {key!r}")
            if key in ISOLATION_VARIABLES:
                raise ValueError(f"{key} is owned by the harness isolation")
        return self


class Patch(Model):
    description: str = Field(min_length=1)
    kind: Literal["patch"] = "patch"
    path: Path | None = None
    text: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> Patch:
        if (self.path is None) == (self.text is None):
            raise ValueError("a patch needs exactly one of path or text")
        return self


class Command(Model):
    description: str = Field(min_length=1)
    kind: Literal["command"] = "command"
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=60, gt=0)


class Feed(Model):
    """A frozen advisory snapshot (for example KEV or EPSS) written outside the source.

    It lands at `{feeds}/<name>` (AGENT_MEMORY_FEEDS_DIR), identical for every
    arm, so a threat-intelligence update is a scripted session like a code change.
    """

    description: str = Field(min_length=1)
    kind: Literal["feed"] = "feed"
    name: str = Field(min_length=1)
    path: Path | None = None
    text: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> Feed:
        if (self.path is None) == (self.text is None):
            raise ValueError("a feed needs exactly one of path or text")
        _relative(self.name, "feed names")
        return self


Change = Annotated[Patch | Command | Feed, Field(discriminator="kind")]


class Session(Model):
    id: Identifier
    description: str = Field(min_length=1)
    changes: tuple[Change, ...] = ()


QuestionKind = Literal["recall", "change", "contradiction", "coverage", "control"]


class Question(Model):
    """A fixed question; the reviewer judges answers by eye, not against a rubric.

    `reviewer_context_by_session` is what the reviewer should know when reading
    that session's answers (what changed, what is now true). It goes only to
    the review sheet, never to an agent.
    """

    id: Identifier
    kind: QuestionKind
    prompt: str = Field(min_length=1)
    after_sessions: tuple[int, ...] = Field(min_length=1)
    reviewer_context_by_session: dict[int, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique(self) -> Question:
        if len(set(self.after_sessions)) != len(self.after_sessions):
            raise ValueError("question session positions must be unique")
        if set(self.reviewer_context_by_session) - set(self.after_sessions):
            raise ValueError("reviewer context must name a scheduled question session")
        return self


class BenchmarkConfig(Model):
    version: Literal[2] = 2
    name: Identifier
    target: Target | None = None
    sessions: tuple[Session, ...] = Field(min_length=1)
    arms: tuple[Arm, ...] = Field(min_length=2, max_length=3)
    model: str = Field(min_length=1)
    guard: RunawayGuard = Field(default_factory=RunawayGuard)
    repetitions: int = Field(gt=0)
    questions: tuple[Question, ...] = Field(min_length=1)
    questions_file: Path | None = None
    strip_from_target: tuple[str, ...] = DEFAULT_STRIP
    session_prompt: str = "Audit the current source and update your retained findings and evidence."

    @model_validator(mode="after")
    def references(self) -> BenchmarkConfig:
        ids = [arm.id for arm in self.arms]
        if len(set(ids)) != len(ids) or not {"A", "B"} <= set(ids):
            raise ValueError("exactly one A and B are required; C is optional")
        for label, values in (("session", self.sessions), ("question", self.questions)):
            if len({value.id for value in values}) != len(values):
                raise ValueError(f"{label} ids must be unique")
        for question in self.questions:
            if any(not 1 <= index <= len(self.sessions) for index in question.after_sessions):
                raise ValueError("question schedules must name existing 1-based session positions")
        return self


def read_data(path: Path) -> object:
    """JSON is also valid YAML; safe_load never constructs arbitrary Python objects."""
    return yaml.safe_load(path.read_text())


def load_config(path: Path) -> BenchmarkConfig:
    data = read_data(path)
    if not isinstance(data, dict):
        raise ValueError("benchmark configuration must be an object")
    if data.get("questions_file") is not None:
        if "questions" in data:
            raise ValueError("choose inline questions or questions_file")
        questions = read_data(path.parent / data["questions_file"])
        data["questions"] = questions
    return BenchmarkConfig.model_validate(data)


def digest(value: object) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
    )
