"""Parse Simplify-style Markdown table rows into ingestion payloads."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from app.schemas.job import RawJobPayload

_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_HTML_TAG = re.compile(r"<[^>]+>")
_IMAGE = re.compile(r"!\[[^]]*]\([^)]*\)")
_BARE_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_HEADER_ALIASES = {
    "company": {"company", "employer"},
    "title": {"role", "title", "position", "job title"},
    "location": {"location", "locations"},
    "apply_url": {"application", "application link", "apply", "link"},
    "date": {"date", "date posted", "posted", "posting date"},
}
_DEFAULT_COLUMNS = ("company", "title", "location", "apply_url", "date")


def _split_row(row: str) -> list[str]:
    """Split a table row on unescaped pipes, retaining escaped literal pipes."""
    text = row.strip()
    if not text.startswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in text[1:]:
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
    if current:
        cells.append("".join(current).strip())
    if cells and not cells[-1] and text.endswith("|"):
        cells.pop()
    return cells


def _plain_text(cell: str, *, location: bool = False) -> str:
    """Remove presentation markup while retaining meaningful cell text."""
    value = html.unescape(cell)
    if location:
        value = re.sub(r"<br\s*/?>", "; ", value, flags=re.IGNORECASE)
    value = _IMAGE.sub("", value)
    value = _replace_links_with_labels(value)
    value = _HTML_TAG.sub("", value)
    value = re.sub(r"(?<!\\)[*_`]", "", value)
    value = value.replace(r"\|", "|").replace(r"\*", "*").replace(r"\_", "_")
    return " ".join(value.split()).strip()


def _replace_links_with_labels(value: str) -> str:
    """Replace Markdown links using a balanced-parenthesis URL scanner."""
    output: list[str] = []
    cursor = 0
    while True:
        label_start = value.find("[", cursor)
        if label_start < 0:
            output.append(value[cursor:])
            break
        label_end = value.find("](", label_start + 1)
        if label_end < 0:
            output.append(value[cursor:])
            break
        url_end = _balanced_url_end(value, label_end + 2)
        if url_end is None:
            output.append(value[cursor:])
            break
        output.extend((value[cursor:label_start], value[label_start + 1 : label_end]))
        cursor = url_end + 1
    return "".join(output)


def _balanced_url_end(value: str, start: int) -> int | None:
    depth = 0
    for index in range(start, len(value)):
        character = value[index]
        if character == "(" and (index == 0 or value[index - 1] != "\\"):
            depth += 1
        elif character == ")" and (index == 0 or value[index - 1] != "\\"):
            if depth == 0:
                return index
            depth -= 1
    return None


def _extract_url(cell: str) -> str | None:
    value = html.unescape(cell).strip()
    cursor = 0
    while True:
        marker = value.find("](", cursor)
        if marker < 0:
            break
        end = _balanced_url_end(value, marker + 2)
        if end is None:
            break
        candidate = value[marker + 2 : end].strip()
        if candidate.lower().startswith(("https://", "http://")):
            return candidate.replace(r"\)", ")")
        cursor = end + 1
    bare = _BARE_URL.search(value)
    return bare.group(0).rstrip(".,;") if bare else None


def _canonical_columns(headers: Sequence[str] | None, cell_count: int) -> list[str]:
    if headers is None:
        return [*_DEFAULT_COLUMNS, *("extra" for _ in range(max(0, cell_count - 5)))]
    canonical: list[str] = []
    for header in headers:
        normalized = _plain_text(header).casefold()
        canonical.append(
            next(
                (name for name, aliases in _HEADER_ALIASES.items() if normalized in aliases),
                "extra",
            )
        )
    return canonical


def parse_markdown_table_row(
    row: str,
    *,
    headers: Sequence[str] | None = None,
    source: str = "github_markdown",
    source_id: str | None = None,
) -> RawJobPayload | None:
    """Parse one Markdown table row, returning ``None`` for non-job rows.

    ``headers`` may describe alternate/reordered table layouts. Without them,
    the conventional Company, Role, Location, Application Link, Date order is
    used. Raw cells and the original row remain in ``payload`` for later state
    classification, including closed-role detection.
    """
    cells = _split_row(row)
    if len(cells) < 4 or all(_SEPARATOR_CELL.fullmatch(cell.strip()) for cell in cells):
        return None

    columns = _canonical_columns(headers, len(cells))
    if len(columns) < len(cells):
        columns.extend("extra" for _ in range(len(cells) - len(columns)))
    mapped = {name: cells[index] for index, name in enumerate(columns) if name != "extra"}
    if not {"company", "title", "location", "apply_url"}.issubset(mapped):
        return None

    company = _plain_text(mapped["company"])
    title = _plain_text(mapped["title"])
    location = _plain_text(mapped["location"], location=True)
    apply_url = _extract_url(mapped["apply_url"])
    header_values = {alias for aliases in _HEADER_ALIASES.values() for alias in aliases}
    if (
        not company
        or not title
        or not location
        or apply_url is None
        or company.casefold() in header_values
        or title.casefold() in header_values
    ):
        return None

    date = _plain_text(mapped.get("date", "")) or None
    return RawJobPayload(
        source=source,
        source_id=source_id,
        company=company,
        title=title,
        apply_url=apply_url,
        location=location,
        payload={"raw_row": row, "raw_cells": cells, "date_posted": date},
    )
