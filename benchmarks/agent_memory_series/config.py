"""Frozen, typed inputs; a target is deliberately optional until launch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")]


class Target(Model):
    git_url: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class Budget(Model):
    max_tokens: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    max_seconds: float = Field(default=900, gt=0)


class Integration(Model):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    # Host installation/activation is a live adapter's responsibility.
    path: Path | None = None


class Arm(Model):
    id: Literal["A", "B", "C"]
    method: Literal["cloudflare-skill", "cruxible-security-kit", "cruxible-no-kit"]
    skill: Integration | None = None
    plugin: Integration | None = None
    kit: Integration | None = None
    memory_paths: tuple[str, ...] = ()

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
        for name in self.memory_paths:
            path = Path(name)
            if not name or path.is_absolute() or ".." in path.parts or path.parts[0] == ".git":
                raise ValueError("memory paths must be relative paths outside .git")
            if path == Path("."):
                raise ValueError("the whole source workspace cannot be a memory path")
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


Change = Annotated[Patch | Command, Field(discriminator="kind")]


class Session(Model):
    id: Identifier
    description: str = Field(min_length=1)
    changes: tuple[Change, ...] = ()


QuestionKind = Literal["recall", "change", "contradiction", "coverage", "control"]


class Criterion(Model):
    id: Identifier
    description: str = Field(min_length=1)
    # Session-dependent expected facts are grading inputs, never agent prompts.
    expected_by_session: dict[int, str] = Field(default_factory=dict)


class Question(Model):
    id: Identifier
    kind: QuestionKind
    prompt: str = Field(min_length=1)
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    after_sessions: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Question:
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion ids must be unique within a question")
        if len(set(self.after_sessions)) != len(self.after_sessions):
            raise ValueError("question session positions must be unique")
        return self


class BenchmarkConfig(Model):
    version: Literal[1] = 1
    name: Identifier
    target: Target | None = None
    sessions: tuple[Session, ...] = Field(min_length=1)
    arms: tuple[Arm, ...] = Field(min_length=2, max_length=3)
    model: str = Field(min_length=1)
    budget: Budget
    repetitions: int = Field(gt=0)
    questions: tuple[Question, ...] = Field(min_length=1)
    questions_file: Path | None = None
    setup_prompt: str = "Prepare the configured memory method for auditing this repository."
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
            for criterion in question.criteria:
                if set(criterion.expected_by_session) - set(question.after_sessions):
                    raise ValueError("expected answers must name a scheduled question session")
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
