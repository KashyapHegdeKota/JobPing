"""Parse Simplify-style Markdown table rows into ingestion payloads."""

from __future__ import annotations

import html
import re
from collections.abc import Sequence

from app.schemas.job import RawJobPayload

_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_HTML_TAG = re.compile(r"<[^<>]*>")
_HTML_TABLE_CELL = re.compile(r"<td\b[^>]*>(.*?)</td\s*>", re.IGNORECASE | re.DOTALL)
_IMAGE = re.compile(r"!\[[^]\r\n]*]\([^\r\n)]*\)")
_BARE_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HTML_HREF = re.compile(
    r"<a\b[^<>]*?\bhref\s*=\s*(?P<quote>['\"])(?P<url>https?://.*?)\1",
    re.IGNORECASE,
)
_LOCK_ICON = re.compile(r"\U0001f512[\ufe0e\ufe0f]?\ufe0f?")
_STRIKETHROUGH = re.compile(r"~~(?=\S)[^\r\n~]*(?<=\S)~~")
_MARKDOWN_MARKUP = re.compile(r"(?<!\\)[*_`]")
_STRIKE_MARKER = re.compile(r"~~")
_BREAK_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LOCATION_SEPARATOR = re.compile(r"\s*(?:[•·]|\n+)\s*")
_COMPANY_BADGE = re.compile(r"^[\s🔥⭐🆕]+")
_CLOSED_STATUS = re.compile(r"^[\s*_`~]*(?:closed)[\s*_`~]*$", re.IGNORECASE)
_HEADER_ALIASES = {
    "company": {"company", "employer"},
    "title": {"role", "title", "position", "job title"},
    "location": {"location", "locations"},
    "apply_url": {"application", "application link", "apply", "link"},
    "date": {"date", "date posted", "posted", "posting date"},
}
_DEFAULT_COLUMNS = ("company", "title", "location", "apply_url", "date")


def _split_row(row: str) -> list[str]:
    """Split a row on structural pipes using a bounded, single-pass scanner."""
    text = row.strip()
    if not text.startswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    html_quote: str | None = None
    in_html_tag = False
    link_depth = 0
    previous = ""
    for character in text[1:]:
        if in_html_tag:
            if html_quote:
                if character == html_quote and not escaped:
                    html_quote = None
            elif character in {'"', "'"}:
                html_quote = character
            elif character == ">":
                in_html_tag = False
        elif character == "<":
            in_html_tag = True
        elif character == "(" and previous == "]" and not escaped:
            link_depth = 1
        elif link_depth and character == "(" and not escaped:
            link_depth += 1
        elif link_depth and character == ")" and not escaped:
            link_depth -= 1

        if character == "|" and not escaped and not in_html_tag and link_depth == 0:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        escaped = character == "\\" and not escaped
        if character != "\\":
            escaped = False
        previous = character
    if current:
        cells.append("".join(current).strip())
    if cells and not cells[-1] and text.endswith("|"):
        cells.pop()
    return cells


def _plain_text(cell: str, *, location: bool = False) -> str:
    """Remove presentation markup while retaining meaningful cell text."""
    value = cell
    if location:
        value = _BREAK_TAG.sub("; ", value)
    value = _IMAGE.sub("", value)
    value = _replace_links_with_labels(value)
    value = _HTML_TAG.sub("", value)
    value = html.unescape(value)
    if location:
        value = _LOCATION_SEPARATOR.sub("; ", value)
    value = _LOCK_ICON.sub("", value)
    value = _STRIKE_MARKER.sub("", value)
    value = _MARKDOWN_MARKUP.sub("", value)
    value = value.replace(r"\|", "|").replace(r"\*", "*").replace(r"\_", "_")
    return " ".join(value.split()).strip()


def _is_closed_row(cells: Sequence[str], mapped: dict[str, str]) -> bool:
    """Detect explicit closed-state presentation without matching ordinary prose."""
    relevant = (
        mapped.get("company", ""),
        mapped.get("title", ""),
        mapped.get("location", ""),
        mapped.get("apply_url", ""),
    )
    return (
        any(_LOCK_ICON.search(_without_url_targets(cell)) for cell in cells)
        or any(_STRIKETHROUGH.search(_without_url_targets(cell)) for cell in relevant)
        or any(_is_closed_status(cell) for cell in cells)
    )


def _without_url_targets(cell: str) -> str:
    """Retain visible Markdown labels but exclude URLs from state signals."""
    visible = _replace_links_with_labels(html.unescape(cell))
    return _BARE_URL.sub("", visible)


def _clean_status_artifacts(value: str) -> str:
    """Remove a standalone closed tag while preserving legitimate wording."""
    return "" if _CLOSED_STATUS.fullmatch(value) else value


def _is_closed_status(cell: str) -> bool:
    """Return whether visible cell text is exactly a formatted closed status."""
    visible = html.unescape(_HTML_TAG.sub("", cell)).strip()
    return _CLOSED_STATUS.fullmatch(visible) is not None


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
    anchor = _HTML_HREF.search(value)
    if anchor:
        return anchor.group("url").strip()
    if "<a" in value.casefold():
        return None
    cursor = 0
    saw_markdown_target = False
    while True:
        marker = value.find("](", cursor)
        if marker < 0:
            break
        saw_markdown_target = True
        end = _balanced_url_end(value, marker + 2)
        if end is None:
            return None
        candidate = value[marker + 2 : end].strip()
        if candidate.lower().startswith(("https://", "http://")):
            return candidate.replace(r"\)", ")")
        cursor = end + 1
    if saw_markdown_target:
        return None
    bare = _BARE_URL.search(value)
    return _trim_url_punctuation(bare.group(0)) if bare else None


def _trim_url_punctuation(value: str) -> str:
    """Trim prose punctuation without removing balanced URL parentheses."""
    value = value.rstrip(".,;:!?")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value


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
    html_cells = _HTML_TABLE_CELL.findall(row) if "<td" in row.casefold() else []
    if len(html_cells) > len(_DEFAULT_COLUMNS):
        html_cells = html_cells[-len(_DEFAULT_COLUMNS) :]
    cells = html_cells or _split_row(row)
    if len(cells) < 4 or all(_SEPARATOR_CELL.fullmatch(cell.strip()) for cell in cells):
        return None

    columns = _canonical_columns(headers, len(cells))
    if len(columns) < len(cells):
        columns.extend("extra" for _ in range(len(cells) - len(columns)))
    mapped = {
        name: cells[index] for index, name in enumerate(columns[: len(cells)]) if name != "extra"
    }
    if not {"company", "title", "location", "apply_url"}.issubset(mapped):
        return None

    is_closed = _is_closed_row(cells, mapped)
    company = _COMPANY_BADGE.sub("", _clean_status_artifacts(_plain_text(mapped["company"])))
    title = _clean_status_artifacts(_plain_text(mapped["title"]))
    location = _clean_status_artifacts(_plain_text(mapped["location"], location=True))
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
        is_closed=is_closed,
        payload={"raw_row": row, "raw_cells": cells, "date_posted": date},
    )
