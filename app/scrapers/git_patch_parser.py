"""Extract changed content lines from GitHub unified-diff patches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.scrapers.github_client import GitHubFilePatch


class ChangeKind(StrEnum):
    """The operation represented by a line in a unified diff."""

    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class ChangedLine:
    """One added or removed content line, in its original patch order."""

    kind: ChangeKind
    content: str


@dataclass(frozen=True, slots=True)
class ParsedPatch:
    """Changed lines extracted from a file patch."""

    filename: str | None
    lines: tuple[ChangedLine, ...]

    @property
    def added_lines(self) -> tuple[str, ...]:
        """Return added content in patch order."""
        return tuple(line.content for line in self.lines if line.kind is ChangeKind.ADDED)

    @property
    def removed_lines(self) -> tuple[str, ...]:
        """Return removed content in patch order."""
        return tuple(line.content for line in self.lines if line.kind is ChangeKind.REMOVED)


class GitPatchParser:
    """Parse changed lines from unified diffs supplied by GitHub's commit API."""

    @staticmethod
    def parse(patch: str | None, *, filename: str | None = None) -> ParsedPatch:
        """Extract additions and removals, excluding unified-diff metadata."""
        if not patch:
            return ParsedPatch(filename=filename, lines=())

        changed: list[ChangedLine] = []
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")):
                continue
            if line.startswith("+"):
                changed.append(ChangedLine(ChangeKind.ADDED, line[1:]))
            elif line.startswith("-"):
                changed.append(ChangedLine(ChangeKind.REMOVED, line[1:]))
        return ParsedPatch(filename=filename, lines=tuple(changed))

    @classmethod
    def parse_file(
        cls,
        file_patch: GitHubFilePatch,
        *,
        target_readme_paths: set[str] | None = None,
    ) -> ParsedPatch | None:
        """Parse a README patch, or return ``None`` when the file is not targeted."""
        if not cls._is_target_readme(file_patch.filename, target_readme_paths):
            return None
        return cls.parse(file_patch.patch, filename=file_patch.filename)

    @classmethod
    def parse_files(
        cls,
        file_patches: tuple[GitHubFilePatch, ...],
        *,
        target_readme_paths: set[str] | None = None,
    ) -> tuple[ParsedPatch, ...]:
        """Parse only matching README patches while preserving file order."""
        parsed = (
            cls.parse_file(file_patch, target_readme_paths=target_readme_paths)
            for file_patch in file_patches
        )
        return tuple(result for result in parsed if result is not None)

    @staticmethod
    def _is_target_readme(filename: str, targets: set[str] | None) -> bool:
        normalized = filename.replace("\\", "/")
        is_readme = normalized.rsplit("/", maxsplit=1)[-1].casefold() in {
            "readme.md",
            "readme.mdx",
        }
        return is_readme and (targets is None or filename in targets)
