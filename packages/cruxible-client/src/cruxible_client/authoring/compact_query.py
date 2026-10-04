"""Client-side helpers for the ``query`` read verb.

- ``QueryResult`` wraps one answer page: ``.rows`` (dicts), ``.columns``,
  ``.truncated``, ``.next_page()``, ``.table()`` and iteration over the rows.
- ``CompactQuery`` is the typed sugar a World kind hands back:
  ``w.dev.roadmap_item.where(adoption_state="adopted").select("task_title")``.
  Keyword filters take operator suffixes (``__ne``, ``__lt``, ``__lte``,
  ``__gt``, ``__gte``, ``__in``, ``__exists``, ``__contains``); every name and
  enum value is checked against the World's vocabulary before the wire. A
  field leaf that is ``self``, a Python keyword, contains ``__`` or ends in
  ``_`` is spelled with one trailing underscore (``self_``, ``class_``,
  ``status__ne_``), before any operator suffix (``self___ne``).
- ``parse_where`` reads the CLI's ``f=v`` / ``f!=v`` / ``f<v`` / ``f in a,b`` /
  ``f exists`` / ``f~text`` expressions, and ``render_query_table`` prints a page
  as an aligned table of values and flags.
"""

from __future__ import annotations

import difflib
import keyword
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cruxible_client.authoring.sdk_types import LiteralValue, SdkError, SubjectRef
from cruxible_client.contracts.compact_query import (
    QUERY_FILTER_OPERATORS,
    QueryColumn,
    QueryFilter,
    QueryRequest,
    QueryResultRecord,
    query_filter,
)
from cruxible_client.contracts.get_display import exact_content_marker_text
from cruxible_client.contracts.temporal import format_datetime

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.world import World


class QueryNameError(SdkError):
    """A query names a field, operator or value the World's vocabulary does not hold."""

    code = "playbill.sdk.query_name_refused"

    def __init__(self, message: str, *, nearest: Sequence[str] = ()) -> None:
        self.nearest = tuple(nearest)
        hint = f"; nearest: {', '.join(self.nearest)}" if self.nearest else ""
        super().__init__(f"{message}{hint}")


# -- the answer page --------------------------------------------------------------


class QueryResult:
    """One page of a ``query`` answer, values first.

    Iterate it for row dicts. Next: ``result.next_page()`` while ``truncated``; ``cx.get(ref)``
    on a row's ref or Claim ID for evidence and history.
    """

    def __init__(
        self,
        page: QueryResultRecord,
        *,
        fetch: Callable[[str], QueryResult] | None = None,
    ) -> None:
        self.page = page
        self._fetch = fetch

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The page's rows as plain dicts, one per Subject or artifact. Next:
        ``result.table()``.
        """

        return [dict(row) for row in self.page.rows]

    @property
    def columns(self) -> tuple[QueryColumn, ...]:
        """What each column holds: field, predicate and value shape. Next: ``result.rows``."""

        return self.page.columns

    @property
    def truncated(self) -> bool:
        """Whether more rows follow this page. Next: ``result.next_page()``."""

        return self.page.truncated

    @property
    def next_cursor(self) -> str | None:
        """The cursor that continues this answer, when truncated. Next: ``result.next_page()``."""

        return self.page.next_cursor

    @property
    def notes(self) -> tuple[str, ...]:
        """What the daemon noted about the answer: dropped fields, cuts, hints.

        Next: act on a note, or ``result.rows``.
        """

        return self.page.notes

    @property
    def receipt(self) -> Any:
        """The replay receipt: the request, coordinate and result digest.

        Next: ``cx.at(...)`` with its coordinate to read the same state again.
        """

        return self.page.receipt

    def next_page(self) -> QueryResult | None:
        """The page after this one, or None when this page is the last.

        Next: ``page.rows``, or ``result.pages()`` to walk them all.
        """

        if self.page.next_cursor is None or self._fetch is None:
            return None
        return self._fetch(self.page.next_cursor)

    def pages(self) -> Iterator[QueryResult]:
        """This page and every page after it.

        Next: ``row`` dicts from each page's ``rows``.
        """

        current: QueryResult | None = self
        while current is not None:
            yield current
            current = current.next_page()

    def table(self) -> str:
        """The page as an aligned text table of values and flags, then its notes.

        Next: ``print(result.table())``.
        """

        return render_query_table(self.page)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.page.rows)

    def __repr__(self) -> str:
        return (
            f"<QueryResult rows={len(self.page.rows)} truncated={self.page.truncated} "
            f"mode={self.page.receipt.mode}>"
        )


# -- the table ----------------------------------------------------------------------

_CELL_WIDTH = 60


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping) and "exact_content" in value and "content_digest" in value:
        return exact_content_marker_text(value)
    if isinstance(value, Mapping) and value.get("truncated") is True and "value" in value:
        text = " ".join(str(value["value"]).split())
        return (text if len(text) < _CELL_WIDTH else text[: _CELL_WIDTH - 1]) + "…"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        text = ", ".join(_cell(item) for item in value) if value else "-"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= _CELL_WIDTH else text[: _CELL_WIDTH - 1] + "…"


def render_query_table(page: QueryResultRecord) -> str:
    """An aligned table of the page's values and flags, then its notes."""

    names = [column.name for column in page.columns]
    rows = [dict(row) for row in page.rows]
    header: list[str] = []
    if rows and "subject" in rows[0]:
        header.append("subject")
    header.extend(name for name in names if name not in header)
    if rows and "flags" in rows[0]:
        header.append("flags")
    if not rows:
        header = header or names
    cells = [[_cell(row.get(name)) for name in header] for row in rows]
    widths = [
        max([len(name), *(len(line[index]) for line in cells)]) for index, name in enumerate(header)
    ]
    lines = ["  ".join(name.ljust(widths[i]) for i, name in enumerate(header)).rstrip()]
    lines.extend(
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(line)).rstrip() for line in cells
    )
    if not rows:
        lines.append("(no rows)")
    lines.extend(f"note: {note}" for note in page.notes)
    return "\n".join(lines)


# -- CLI where expressions --------------------------------------------------------

_IN_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s+in\s+(.+?)\s*$")
_EXISTS_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s+(!?)exists\s*$")
_OPERATOR_RE = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*(!=|<=|>=|=|<|>|~)\s*(.*?)\s*$")
_SYMBOLS = {"=": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte", "~": "contains"}
WHERE_SYNTAX = "'f=v', 'f!=v', 'f<v', 'f<=v', 'f>v', 'f>=v', 'f in a,b', 'f exists', 'f~text'"


def parse_where(expression: str) -> QueryFilter:
    """Read one CLI filter expression; values stay strings for the daemon to type."""

    match = _IN_RE.fullmatch(expression)
    if match is not None:
        values = [item.strip() for item in match.group(2).split(",") if item.strip()]
        if values:
            return query_filter(match.group(1), "in", values)
    match = _EXISTS_RE.fullmatch(expression)
    if match is not None:
        return query_filter(match.group(1), "exists", match.group(2) != "!")
    match = _OPERATOR_RE.fullmatch(expression)
    if match is not None and match.group(3):
        return query_filter(match.group(1), _SYMBOLS[match.group(2)], match.group(3))
    raise ValueError(
        f"cannot read the filter {expression!r}; write one of {WHERE_SYNTAX}, "
        "for example 'adoption_state=adopted'"
    )


# -- the typed World sugar ----------------------------------------------------------

_SUFFIXES = tuple(operator for operator in QUERY_FILTER_OPERATORS if operator != "eq")
_ORDERED = frozenset({"integer", "number", "string"})


def keyword_name(field: str) -> str | None:
    """The ``where()`` keyword a field leaf is spelled as, or None if none can be.

    A leaf that is ``self``, a Python keyword, carries the operator separator
    ``__``, or already ends in ``_`` takes one trailing underscore: ``self_``,
    ``class_``, ``status__ne_``. Every other identifier is its own keyword. The
    escaped names are exactly those ending in ``_``, so the rule is injective,
    and no keyword, bare or operator-suffixed, reads as another field's.
    """

    if not field.isidentifier():
        return None
    if field == "self" or keyword.iskeyword(field) or "__" in field or field.endswith("_"):
        return field + "_"
    return field


def keyword_field(key: str) -> tuple[str, str]:
    """Split one ``where()`` keyword into the field it names and its operator."""

    name, operator = key, "eq"
    head, _, suffix = key.rpartition("__")
    if head and suffix in _SUFFIXES:
        name, operator = head, suffix
    if name.endswith("_") and keyword_name(name[:-1]) == name:
        name = name[:-1]
    return name, operator


def _nearest(value: str, names: Sequence[str]) -> tuple[str, ...]:
    return tuple(difflib.get_close_matches(value, sorted(set(names)), n=5, cutoff=0.5))


def _wire_value(value: object) -> object:
    if isinstance(value, LiteralValue):
        return value.value
    if isinstance(value, SubjectRef):
        return value.address
    if isinstance(value, datetime):
        return format_datetime(value)
    return value


class CompactQuery:
    """A compact query over one World kind, built one step at a time.

    Each step returns a new query, so a partial query can be reused. ``run()``
    answers one page; iterating the query walks every page.
    """

    def __init__(
        self,
        world: World,
        kind: str,
        *,
        where: tuple[QueryFilter, ...] = (),
        select: tuple[str, ...] = (),
        order_by: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> None:
        self._world = world
        self._kind = kind
        self._where = where
        self._select = select
        self._order_by = order_by
        self._limit = limit

    def _with(self, **changes: Any) -> CompactQuery:
        fields: dict[str, Any] = {
            "where": self._where,
            "select": self._select,
            "order_by": self._order_by,
            "limit": self._limit,
        }
        fields.update(changes)
        return CompactQuery(self._world, self._kind, **fields)

    # -- vocabulary --------------------------------------------------------------

    def _fields(self) -> dict[str, str]:
        """Every name a field may be spelled as, mapped to its full predicate."""

        names: dict[str, str] = {}
        for leaf, predicates in self._world._leaf_map(self._kind).items():
            for predicate in predicates:
                names[predicate] = predicate
            if len(predicates) == 1:
                names[leaf] = predicates[0]
        return names

    def _predicate(self, name: str) -> str | None:
        if name == "subject_id":
            return None
        names = self._fields()
        if name not in names:
            raise QueryNameError(
                f"{self._kind} has no field {name!r}",
                nearest=_nearest(name, [*names, "subject_id"]),
            )
        return names[name]

    def _check(self, name: str, operator: str, value: object) -> object:
        predicate = self._predicate(name)
        if predicate is None:
            if operator == "exists":
                raise QueryNameError("subject_id always exists; use eq, ne, in or contains")
            return value
        claim_type = self._world.claim_type(predicate)
        schema = claim_type.literal_schema or {}
        object_kind = claim_type.object_kind.value
        members = claim_type.members
        declared = schema.get("type") if object_kind == "literal" else object_kind
        if operator == "exists":
            if not isinstance(value, bool):
                raise QueryNameError(f"{name}__exists takes true or false")
            return value
        if object_kind == "exact_content":
            raise QueryNameError(f"{name} holds exact content; only {name}__exists applies")
        if operator in {"lt", "lte", "gt", "gte"} and (members or declared not in _ORDERED):
            raise QueryNameError(
                f"{operator!r} does not apply to {name}; use eq, ne, in, exists"
                + (", contains" if members or declared == "string" else "")
            )
        if operator == "contains" and declared not in {"string", "subject"}:
            raise QueryNameError(f"'contains' does not apply to {name}, which is not text")
        values = list(value) if operator == "in" and isinstance(value, Sequence) else [value]
        if operator == "in" and (isinstance(value, str) or not values):
            raise QueryNameError(f"{name}__in takes a nonempty list of values")
        wired = [_wire_value(item) for item in values]
        if members and operator != "contains":
            for item in wired:
                if item not in members:
                    raise QueryNameError(
                        f"{item!r} is not a member of {predicate}", nearest=tuple(members)
                    )
        return wired if operator == "in" else wired[0]

    # -- building ------------------------------------------------------------------

    def where(self, /, **filters: object) -> CompactQuery:
        """Add all-of filters: ``field=value`` or ``field__<op>=value``.

        A leaf that is ``self``, a Python keyword, contains ``__`` or ends in
        ``_`` is spelled with one trailing underscore (see ``keyword_name``).

        Next: ``.select(...)``, then ``.run()`` or iterate it.
        """

        added: list[QueryFilter] = []
        for key, value in filters.items():
            name, operator = keyword_field(key)
            head, _, suffix = key.rpartition("__")
            if (
                operator == "eq"
                and head
                and suffix
                and name != "subject_id"
                and name not in self._fields()
            ):
                raise QueryNameError(
                    f"{key!r} names no operator {suffix!r}",
                    nearest=_nearest(suffix, _SUFFIXES),
                )
            checked = self._check(name, operator, value)
            added.append(query_filter(self._wire_field(name), operator, checked))
        return self._with(where=(*self._where, *added))

    def _wire_field(self, name: str) -> str:
        """The full predicate a World name sends, which the daemon resolves as itself."""

        return self._predicate(name) or "subject_id"

    def select(self, *fields: str) -> CompactQuery:
        """Choose the columns, by short or full predicate name.

        Next: ``.run()`` or iterate it.
        """

        wired = tuple(self._wire_field(name) for name in fields)
        return self._with(select=(*self._select, *wired))

    def order_by(self, *fields: str) -> CompactQuery:
        """Order rows by fields; prefix ``-`` for descending.

        Next: ``.run()``.
        """

        wired = tuple(
            ("-" if name.startswith("-") else "") + self._wire_field(name.removeprefix("-"))
            for name in fields
        )
        return self._with(order_by=(*self._order_by, *wired))

    def limit(self, count: int) -> CompactQuery:
        """Cap the page at ``count`` rows. Next: ``.run()``, then ``result.next_page()``."""

        return self._with(limit=count)

    def request(self) -> QueryRequest:
        """The ``cx.query`` request this builds, checked against the World.

        Next: ``.run()``, or pass it to ``cx.query(...)`` yourself.
        """

        fields: dict[str, Any] = {
            "kind": self._kind,
            "where": self._where,
            "select": self._select,
            "order_by": self._order_by,
        }
        if self._limit is not None:
            fields["limit"] = self._limit
        return QueryRequest(**fields)

    def run(self) -> QueryResult:
        """Answer one page at the World's coordinate.

        Next: ``result.rows`` or ``result.table()``; ``result.next_page()`` while truncated.
        """

        self._world._assert_current()
        return self._world._playbill._run_query_request(self.request())

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for page in self.run().pages():
            yield from page

    def __repr__(self) -> str:
        return f"<CompactQuery {self._kind} where={len(self._where)} select={self._select}>"


def filters_from_mappings(where: Sequence[QueryFilter | Mapping[str, Any]]) -> tuple[Any, ...]:
    """Normalize SDK ``where`` items (typed filters or plain mappings) for a request."""

    return tuple(
        item.model_dump(mode="json", by_alias=True) if hasattr(item, "model_dump") else dict(item)
        for item in where
    )


__all__ = [
    "CompactQuery",
    "QueryNameError",
    "QueryResult",
    "WHERE_SYNTAX",
    "exact_content_marker_text",
    "filters_from_mappings",
    "keyword_field",
    "keyword_name",
    "parse_where",
    "render_query_table",
]
