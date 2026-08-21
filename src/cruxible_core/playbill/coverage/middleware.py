"""The owned/custom-harness coverage middleware (§11.7).

§11.7 names four adapter dispositions, and this is the one that owns its tool
executor: "before-tool, after-tool, and after-filesystem-change middleware
against the same operation contract." §11.8 points TauBench at exactly this
shape -- "where TauBench owns its tool executor, implement the adapter in the
benchmark harness against the vendor-neutral hook contract" -- which makes this
module, not the Claude Code plugin, the delivery mechanism for arm 4.

Why the middleware never writes to a model channel
--------------------------------------------------
The one thing this module deliberately does *not* do is splice. Every entry
point returns a :class:`CoverageDeliveryV1` carrying the original tool output
and the appended coverage text as two separate strings, and the caller joins
them. That is not squeamishness about strings: it is the §11.8 integrity rule
"original tool output is preserved and annotated, not replaced or suppressed"
made structural. A middleware that returned one blob could silently drop or
rewrite the tool's own output and no test could tell; a middleware that returns
the original *unmodified* alongside a pure addendum cannot, and the tests below
assert exactly that by comparing the returned original against the input.

It also keeps the module honest about a harness fact discovered while building
the Claude Code plugin: a vendor's tool-result channel may not be able to carry
free text at all. Splicing is a property of the harness, so it belongs to the
harness.

No semantic logic, enforced by what is in scope
-----------------------------------------------
Classification lives in the resolver and nowhere else. This module maps tool
events to `(logical source, optional byte window)` pairs through
:mod:`.adapter`, hands them to an injected resolve callable, and renders the
answer through :mod:`.render`. It never inspects a match state, never decides
equivalence, and never composes a coverage line of its own -- every byte it
emits came out of the reference renderer, including the one fail-open note.

The resolve callable is injected rather than imported because the coverage
package may not reach the service layer (an architecture test holds that line).
That constraint turned out to be the feature: the same middleware embeds in the
CLI, in TauBench's executor, and in any harness that can reach a Playbill
instance, because the only thing it needs from any of them is a function from
observations to a result.

Fail open on infrastructure, fail closed on semantics
------------------------------------------------------
An unreadable working file or an unreachable daemon must never break the
agent's tool call, so both degrade to the original output plus one note. But
nothing here ever manufactures a match state: a degraded delivery carries *no
cards at all*, and the guarantee that a degraded resolve cannot answer `exact`
stays where it already lives, in the resolver. The middleware has no code path
that could circumvent it because it has no code path that writes a card.

Unbound paths are silent
------------------------
A working path with no declared binding is not covered, and §11.6.4's rule
against context spam means it produces nothing: no card, no note, no mention.
Nothing false is said about it either -- an unbound path yields no span, so no
`none` is claimed inside a boundary that never contained it. The count is
recorded on the delivery for a harness that wants to measure configuration
coverage, and it is never rendered.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingPathBindingV1,
    WorkingSourceObservationV1,
    observe_working_source,
    parse_grep_batch,
    read_working_path,
    selection_for_lines,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    CoverageResultAny,
    CoverageSelectionV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.coverage.render import (
    CoverageUnavailableCodeV1,
    render_coverage_result,
    render_unavailable_note,
)

CONFIG_RELATIVE_PATH = ".playbill/coverage.json"

PATH_IDENTITY_NORMALIZER: Literal["playbill-coverage-path-identity-v1"] = (
    "playbill-coverage-path-identity-v1"
)

HarnessToolKindV1 = Literal["read", "grep", "edit", "write"]


class _StrictMiddlewareModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- workspace configuration -----------------------------------------------


class CoverageExactPathRuleV1(_StrictMiddlewareModel):
    """One working path bound to one logical source, spelled out in full.

    The form with no inference in it at all, and the only form that can bind a
    ledger source, whose identity is a canonical artifact path rather than
    anything derivable from a working tree.
    """

    tag: Literal["playbill-coverage-exact-path-rule-v1"] = "playbill-coverage-exact-path-rule-v1"
    path: str
    plane: Literal["ledger", "external"]
    identity: str


class CoveragePathPrefixRuleV1(_StrictMiddlewareModel):
    """A declared prefix rule, with its normalizer named rather than assumed.

    §11.6.1 forbids inferring a binding from a filename, and a prefix rule is
    not an exception to that: the rule is an explicit declaration that
    everything under `path_prefix` was authored under `identity_prefix` through
    a named, published transformation. The maintainer who accepted the Claims
    declared the same identities by the same rule; the rule is written down so
    both sides can be checked against it.

    `playbill-coverage-path-identity-v1` is deliberately **non-lossy**: strip
    the declared prefix, replace `/` with `.`, prepend the declared identity
    prefix, and stop. Nothing is lowercased, no extension is dropped, and no
    character is folded, because every lossy step is a way for two distinct
    working files to collide onto one accepted source -- which is precisely the
    cross-source confusion §11.6.1 exists to prevent. `corpus/handbook.md`
    under prefix `corpus/` and identity prefix `corpus.` is
    `corpus.handbook.md`, extension and all.

    A produced identity that does not satisfy the frozen identity grammar binds
    nothing. It is not an error: the path is simply unbound, and unbound is
    silent.
    """

    tag: Literal["playbill-coverage-path-prefix-rule-v1"] = "playbill-coverage-path-prefix-rule-v1"
    path_prefix: str
    plane: Literal["ledger", "external"]
    identity_prefix: str = ""
    normalizer: Literal["playbill-coverage-path-identity-v1"] = PATH_IDENTITY_NORMALIZER

    @field_validator("path_prefix")
    @classmethod
    def _path_prefix(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("a path prefix must be a non-empty trimmed value")
        if value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("a path prefix must be relative and may not traverse upward")
        return value

    def identity_for(self, path: str) -> str | None:
        """Apply the named normalizer, or decline."""

        if not path.startswith(self.path_prefix):
            return None
        remainder = path[len(self.path_prefix) :]
        if not remainder:
            return None
        return self.identity_prefix + remainder.replace("/", ".")


CoveragePathRuleV1 = Annotated[
    CoverageExactPathRuleV1 | CoveragePathPrefixRuleV1,
    Field(discriminator="tag"),
]


class CoverageWorkspaceConfigV1(_StrictMiddlewareModel):
    """`.playbill/coverage.json`: which instance, which bindings, which budget.

    Rule precedence is deterministic and stated rather than emergent: an exact
    rule wins over every prefix rule, and among prefix rules the longest
    matching prefix wins. Two exact rules for one path, or two prefix rules with
    one prefix, are refused at load rather than resolved by file order.
    """

    tag: Literal["playbill-coverage-workspace-config-v1"] = "playbill-coverage-workspace-config-v1"
    instance_id: str | None = None
    server_url: str | None = None
    root: str = "."
    rules: tuple[CoveragePathRuleV1, ...] = ()
    scan_budget: CoverageScanBudgetV1 | None = None
    max_observed_paths: int = Field(default=64, ge=1)

    @model_validator(mode="after")
    def _rules_are_unambiguous(self) -> "CoverageWorkspaceConfigV1":
        exact = [item.path for item in self.rules if isinstance(item, CoverageExactPathRuleV1)]
        if len(set(exact)) != len(exact):
            raise ValueError("a working path may declare at most one exact coverage rule")
        prefixes = [
            item.path_prefix for item in self.rules if isinstance(item, CoveragePathPrefixRuleV1)
        ]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("a path prefix may declare at most one coverage rule")
        return self

    def source_for(self, path: str) -> LogicalSourceIdentityV1 | None:
        """Resolve one working path to a declared logical source, or to nothing.

        Returning ``None`` rather than raising is the §11.6.4 silence rule in
        the type: an unbound path is a fact about configuration, not a failure
        of the operation, and the caller drops it without a word.
        """

        for rule in self.rules:
            if isinstance(rule, CoverageExactPathRuleV1) and rule.path == path:
                return _logical_source(rule.plane, rule.identity)

        best: LogicalSourceIdentityV1 | None = None
        best_length = -1
        for rule in self.rules:
            if not isinstance(rule, CoveragePathPrefixRuleV1):
                continue
            identity = rule.identity_for(path)
            if identity is None or len(rule.path_prefix) <= best_length:
                continue
            source = _logical_source(rule.plane, identity)
            if source is not None:
                best, best_length = source, len(rule.path_prefix)
        return best

    def bindings_for(self, paths: Iterable[str]) -> tuple[WorkingPathBindingsV1, tuple[str, ...]]:
        """Split the named paths into declared bindings and silent unbound ones."""

        bound: list[WorkingPathBindingV1] = []
        claimed: set[bytes] = set()
        unbound: list[str] = []
        for path in sorted(set(paths)):
            source = self.source_for(path)
            if source is None or source.sort_key in claimed:
                # A second working path claiming one accepted source is not a
                # coverage answer either -- a snapshot names each source once --
                # so it is dropped exactly as silently as an unbound path.
                unbound.append(path)
                continue
            claimed.add(source.sort_key)
            bound.append(WorkingPathBindingV1(path=path, source=source))
        return WorkingPathBindingsV1(bindings=tuple(bound)), tuple(unbound)


def _logical_source(plane: str, identity: str) -> LogicalSourceIdentityV1 | None:
    try:
        return LogicalSourceIdentityV1(plane=plane, identity=identity)  # type: ignore[arg-type]
    except ValueError:
        return None


def load_coverage_config(root: Path) -> CoverageWorkspaceConfigV1:
    """Read `.playbill/coverage.json` from a workspace root."""

    path = root.expanduser() / CONFIG_RELATIVE_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CoverageError(f"coverage configuration could not be read: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CoverageError(f"coverage configuration is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise CoverageError(f"coverage configuration must be one mapping: {path}")
    try:
        return CoverageWorkspaceConfigV1.model_validate(payload)
    except ValueError as exc:
        raise CoverageError(f"coverage configuration is not valid: {exc}") from exc


# -- the harness event model ------------------------------------------------


class HarnessLineRangeV1(_StrictMiddlewareModel):
    """One 1-based inclusive line window a tool reported reading."""

    tag: Literal["playbill-coverage-harness-line-range-v1"] = (
        "playbill-coverage-harness-line-range-v1"
    )
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _increasing(self) -> "HarnessLineRangeV1":
        if self.end_line < self.start_line:
            raise ValueError("a line range must be increasing")
        return self


class HarnessGrepHitV1(_StrictMiddlewareModel):
    """One `path:line` hit out of a search batch."""

    tag: Literal["playbill-coverage-harness-grep-hit-v1"] = "playbill-coverage-harness-grep-hit-v1"
    path: str
    line: int = Field(ge=1)


class HarnessToolEventV1(_StrictMiddlewareModel):
    """One tool event, in the only vocabulary the middleware understands.

    Four kinds, because §11.7's Read/Grep/Edit/Write disposition is about what a
    tool *did to a source*, not what a vendor named it. A harness whose tools
    are called `open_file` and `search` maps them here and everything downstream
    is identical -- which is what makes arms 3 and 4 differ by only this adapter.

    `original_output` is carried through untouched and handed back untouched.
    The middleware never parses it for meaning; for a grep batch the caller may
    instead pass `grep_output`, which is read as `path:line:...` locations and
    nothing else.
    """

    tag: Literal["playbill-coverage-harness-tool-event-v1"] = (
        "playbill-coverage-harness-tool-event-v1"
    )
    kind: HarnessToolKindV1
    tool_name: str = ""
    paths: tuple[str, ...] = ()
    ranges: tuple[HarnessLineRangeV1, ...] = ()
    grep_hits: tuple[HarnessGrepHitV1, ...] = ()
    original_output: str = ""


def grep_event(
    grep_output: str,
    *,
    tool_name: str = "",
    original_output: str | None = None,
) -> HarnessToolEventV1:
    """Build a grep event from `grep -n`-shaped output.

    The shape a search tool already emits, so a harness pipes its own output in
    without a translation layer of its own.
    """

    return HarnessToolEventV1(
        kind="grep",
        tool_name=tool_name,
        grep_hits=tuple(
            HarnessGrepHitV1(path=path, line=line) for path, line in parse_grep_batch(grep_output)
        ),
        original_output=grep_output if original_output is None else original_output,
    )


# -- what a delivery is -----------------------------------------------------


class CoverageDeliveryV1(_StrictMiddlewareModel):
    """Original output, appended coverage text, and the answer behind them.

    Two strings, never one. The caller splices; see the module docstring for why
    that is a structural guarantee rather than a style choice.
    """

    tag: Literal["playbill-coverage-delivery-v1"] = "playbill-coverage-delivery-v1"
    original_output: str = ""
    lines: tuple[str, ...] = ()
    result: CoverageResultAny | None = None
    unbound_paths: tuple[str, ...] = ()
    observed_sources: int = Field(default=0, ge=0)
    failure_code: CoverageUnavailableCodeV1 | None = None

    @property
    def appended_coverage_text(self) -> str:
        """Exactly the rendered lines, or the empty string when there are none."""

        return "\n".join(self.lines)

    @property
    def failed_open(self) -> bool:
        return self.failure_code is not None

    def spliced(self, *, separator: str = "\n") -> str:
        """The convenience join, for a harness that wants one and not the seam."""

        if not self.lines:
            return self.original_output
        if not self.original_output:
            return self.appended_coverage_text
        return self.original_output + separator + self.appended_coverage_text


ResolveCoverage = Callable[[Sequence[WorkingSourceObservationV1]], CoverageResultAny]


class CoverageMiddlewareV1:
    """The §11.7 owned-harness adapter: three entry points over one operation.

    Construct it with a workspace root, a loaded configuration, and a callable
    that resolves observations against a Playbill instance. Everything else is
    mechanical.
    """

    def __init__(
        self,
        *,
        root: Path,
        config: CoverageWorkspaceConfigV1,
        resolve: ResolveCoverage,
    ) -> None:
        self._root = root.expanduser()
        self._config = config
        self._resolve = resolve

    @property
    def config(self) -> CoverageWorkspaceConfigV1:
        return self._config

    # -- entry points -------------------------------------------------------

    def before_tool(self, event: HarnessToolEventV1) -> CoverageDeliveryV1:
        """Resolve what the sources are *about to* be, and annotate nothing.

        A tool that has not run has no output to annotate, so this never carries
        rendered text. It still returns the answer, because the §11.8 metrics --
        governed-span edits acknowledged before completion, unacknowledged-drift
        rate -- need to know what a span's relationship was before the edit, and
        a harness that asks afterwards is asking a different question.
        """

        delivery = self._deliver(event)
        return delivery.model_copy(update={"lines": (), "original_output": event.original_output})

    def after_tool(self, event: HarnessToolEventV1) -> CoverageDeliveryV1:
        """Resolve and render what the tool just read or changed.

        For `edit` and `write` this is the §11.8 same-turn scenario: the changed
        paths are observed whole, the resolver reports the governed span's new
        relationship, and the drifted card rides the tool result the agent is
        already reading. Nothing is compiled, proposed, or accepted, and the
        card grants the changed material no governance fact.
        """

        return self._deliver(event)

    def after_filesystem_change(self, paths: Iterable[str]) -> CoverageDeliveryV1:
        """The changed-paths form, for edits the middleware did not see as tools.

        A formatter, a checkout, or a sibling process moves bytes too, and
        §11.6.6 wants that visible at the next tool result rather than at the
        next compile.
        """

        return self._deliver(HarnessToolEventV1(kind="write", paths=tuple(paths)))

    # -- the one path all three share ---------------------------------------

    def _deliver(self, event: HarnessToolEventV1) -> CoverageDeliveryV1:
        try:
            observations, unbound = self._observe(event)
        except CoverageError:
            return self._failed_open(event, "working_source_unreadable")

        if not observations:
            # Every path was unbound, or the event named none. There is nothing
            # to ask about, so nothing is asked and nothing is said.
            return CoverageDeliveryV1(original_output=event.original_output, unbound_paths=unbound)

        try:
            result = self._resolve(observations)
        except Exception:  # noqa: BLE001 - a broken hook may not break the tool call
            # Deliberately total. The contract with the caller is that a
            # coverage failure degrades to the original output plus one note,
            # and an injected callable may raise anything at all: a transport
            # error, a refusal, a typed client error this package may not
            # import. None of them are the agent's problem mid-tool-call.
            return self._failed_open(event, "coverage_operation_unavailable")

        return CoverageDeliveryV1(
            original_output=event.original_output,
            lines=render_coverage_result(result),
            result=result,
            unbound_paths=unbound,
            observed_sources=len(observations),
        )

    def _failed_open(
        self,
        event: HarnessToolEventV1,
        code: CoverageUnavailableCodeV1,
    ) -> CoverageDeliveryV1:
        return CoverageDeliveryV1(
            original_output=event.original_output,
            lines=render_unavailable_note(code),
            failure_code=code,
        )

    # -- event -> observations ----------------------------------------------

    def _observe(
        self,
        event: HarnessToolEventV1,
    ) -> tuple[tuple[WorkingSourceObservationV1, ...], tuple[str, ...]]:
        """Reduce any of the four event kinds to observations and windows.

        Whole-source and windowed requests over one path collapse to a single
        observation, because a source is observed once per snapshot. A changed
        path is always asked about whole: an edit's drift is visible without the
        caller having to guess which window moved.
        """

        whole: set[str] = set(event.paths)
        windows: dict[str, set[tuple[int, int]]] = {}
        for item in event.ranges:
            windows.setdefault(item.path, set()).add((item.start_line, item.end_line))
        for hit in event.grep_hits:
            windows.setdefault(hit.path, set()).add((hit.line, hit.line))

        named = sorted(whole | set(windows))[: self._config.max_observed_paths]
        bindings, unbound = self._config.bindings_for(named)

        observations: list[WorkingSourceObservationV1] = []
        for path in bindings.paths:
            content = read_working_path(path, root=self._root)
            selections: tuple[CoverageSelectionV1, ...] = (
                ()
                if path in whole
                else tuple(
                    selection_for_lines(content, start_line=start, end_line=end)
                    for start, end in sorted(windows[path])
                )
            )
            observations.append(
                observe_working_source(bindings.source_for(path), content, selections=selections)
            )
        return tuple(observations), unbound


def coverage_middleware(
    *,
    root: Path,
    resolve: ResolveCoverage,
    config: CoverageWorkspaceConfigV1 | None = None,
) -> CoverageMiddlewareV1:
    """Build a middleware over a workspace, loading its configuration if needed."""

    return CoverageMiddlewareV1(
        root=root,
        config=config if config is not None else load_coverage_config(root),
        resolve=resolve,
    )


__all__ = [
    "CONFIG_RELATIVE_PATH",
    "PATH_IDENTITY_NORMALIZER",
    "CoverageDeliveryV1",
    "CoverageExactPathRuleV1",
    "CoverageMiddlewareV1",
    "CoveragePathPrefixRuleV1",
    "CoveragePathRuleV1",
    "CoverageWorkspaceConfigV1",
    "HarnessGrepHitV1",
    "HarnessLineRangeV1",
    "HarnessToolEventV1",
    "HarnessToolKindV1",
    "ResolveCoverage",
    "coverage_middleware",
    "grep_event",
    "load_coverage_config",
]
